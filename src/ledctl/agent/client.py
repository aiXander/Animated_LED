"""Thin OpenAI-compatible client pointed at OpenRouter.

Why not call `openai` directly from the FastAPI route? Because we want the
route to stay focused on session/tool plumbing, and we want unit tests to
mock the LLM cleanly (one class, one method, one mock).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai import OpenAI

log = logging.getLogger("ledctl.agent")


class MissingApiKey(RuntimeError):
    """Raised when the configured `api_key_env` is unset.

    Caller (FastAPI route) catches this and surfaces a clear 503. The render
    loop is unaffected — the agent is opt-in.
    """


@dataclass
class CompletionResult:
    """One non-streamed chat completion's relevant bits."""

    text: str
    tool_calls: list[dict[str, Any]]  # [{id, name, arguments(dict)}]
    raw_message: dict[str, Any]       # full {role, content, tool_calls?} for buffer
    finish_reason: str = ""
    model: str = ""
    usage: dict[str, int] | None = None  # {input_tokens, output_tokens, total_tokens}


class AgentClient:
    """OpenRouter wrapper using the OpenAI SDK.

    `complete(...)` is one round-trip: send messages + tools → get back
    `assistant` content + at most one `update_leds` tool call. We don't loop
    here; multi-step orchestration is explicitly out of scope (per roadmap).
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key_env: str,
        model: str,
        request_timeout_seconds: float = 60.0,
        debug_logging: bool = False,
    ):
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.model = model
        self.timeout = float(request_timeout_seconds)
        self.debug_logging = bool(debug_logging)
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI:
        if self._client is not None:
            return self._client
        api_key = os.environ.get(self.api_key_env, "").strip()
        if not api_key:
            raise MissingApiKey(
                f"environment variable {self.api_key_env!r} is not set; "
                f"set it in `.env` or the shell before /agent/chat works"
            )
        # Imported lazily so importing the agent module doesn't require
        # `openai` to be installed at test time when the client isn't used.
        from openai import OpenAI

        self._client = OpenAI(
            base_url=self.base_url,
            api_key=api_key,
            timeout=self.timeout,
        )
        return self._client

    def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
    ) -> CompletionResult:
        """Synchronous; the FastAPI route runs this off the event loop."""
        client = self._get_client()
        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        effective_model = model or self.model

        if self.debug_logging:
            log.info(
                "agent.request: model=%s messages=%d tools=%s system_prompt_chars=%d",
                effective_model,
                len(full_messages),
                [t.get("function", {}).get("name") for t in tools],
                len(system_prompt),
            )
            for i, m in enumerate(full_messages):
                role = m.get("role")
                content = m.get("content") or ""
                preview = content if len(content) <= 240 else content[:240] + "…"
                tcs = m.get("tool_calls")
                if tcs:
                    log.info(
                        "  msg[%d] role=%s tool_calls=%s",
                        i, role, [tc.get("function", {}).get("name") for tc in tcs],
                    )
                else:
                    log.info("  msg[%d] role=%s content=%r", i, role, preview)

        try:
            resp = client.chat.completions.create(
                model=effective_model,
                messages=full_messages,
                tools=tools,
                tool_choice="auto",
                # Single-tool flow: one `update_leds` per turn. Disabling parallel
                # tool calls keeps providers from emitting multiple update_leds
                # calls in one assistant turn (the second would just clobber the
                # first via crossfade_to anyway).
                parallel_tool_calls=False,
            )
        except Exception:
            # Re-raise so the route can surface a 502, but log the full body
            # first — the OpenAI SDK puts the upstream error payload on the
            # exception's `body`/`response` attrs and that's the most useful
            # thing for the operator.
            log.exception(
                "agent.request_failed: model=%s base_url=%s",
                effective_model,
                self.base_url,
            )
            raise

        if self.debug_logging:
            try:
                dumped = resp.model_dump()
            except Exception:  # noqa: BLE001
                dumped = {"_repr": repr(resp)}
            log.info("agent.response: %s", json.dumps(dumped, default=str))

        if not resp.choices:
            # OpenRouter returns an empty `choices` list with a top-level
            # `error` payload when the upstream provider rejects the request
            # (bad tool schema, content policy, quota, etc.). Surface that
            # error explicitly — operator can't act on a silent failure.
            err = (
                getattr(resp, "error", None)
                or (resp.model_extra or {}).get("error")
                or resp.model_extra
                or {}
            )
            log.warning(
                "agent.empty_choices: model=%s error=%s full=%s",
                effective_model,
                err,
                _safe_dump(resp),
            )
            return CompletionResult(
                text=f"(provider returned no choices: {err!r})",
                tool_calls=[],
                raw_message={"role": "assistant", "content": ""},
                finish_reason="error",
                model=resp.model or effective_model,
                usage=_extract_usage(resp),
            )
        choice = resp.choices[0]
        msg = choice.message
        finish = choice.finish_reason or ""
        # The SDK returns a pydantic-ish object; convert to a plain dict so the
        # session buffer stays JSON-trivial.
        tool_calls: list[dict[str, Any]] = []
        raw_tool_calls: list[dict[str, Any]] = []
        for tc in msg.tool_calls or []:
            args_str = tc.function.arguments or "{}"
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError as e:
                log.warning(
                    "agent.tool_args_parse_error: tool=%s err=%s raw=%r",
                    tc.function.name, e, args_str,
                )
                args = {"_raw": args_str, "_parse_error": True}
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                }
            )
            raw_tool_calls.append(
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": args_str,
                    },
                }
            )
        raw_message: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content or "",
        }
        if raw_tool_calls:
            raw_message["tool_calls"] = raw_tool_calls

        # Silent-failure case: provider returned a choice but no content and no
        # tool calls. This is what surfaces in the UI as
        # "(no response · finish_reason=…)". Always log it.
        if not (msg.content or "").strip() and not tool_calls:
            log.warning(
                "agent.empty_completion: model=%s finish_reason=%s "
                "native_finish=%s full=%s",
                effective_model,
                finish,
                getattr(choice, "native_finish_reason", None),
                _safe_dump(resp),
            )
        elif self.debug_logging:
            log.info(
                "agent.completion: finish_reason=%s text_chars=%d tool_calls=%s",
                finish,
                len(msg.content or ""),
                [tc["name"] for tc in tool_calls],
            )

        return CompletionResult(
            text=msg.content or "",
            tool_calls=tool_calls,
            raw_message=raw_message,
            finish_reason=finish,
            model=resp.model or effective_model,
            usage=_extract_usage(resp),
        )


def _extract_usage(resp: Any) -> dict[str, int] | None:
    """Pull `prompt_tokens` / `completion_tokens` / `total_tokens` off the SDK
    response and return a normalised `{input_tokens, output_tokens, total_tokens}`
    dict, or None if the provider didn't include a usage block."""
    u = getattr(resp, "usage", None)
    if u is None:
        return None
    pt = getattr(u, "prompt_tokens", None)
    ct = getattr(u, "completion_tokens", None)
    tt = getattr(u, "total_tokens", None)
    if pt is None and ct is None and tt is None:
        return None
    out: dict[str, int] = {}
    if pt is not None:
        out["input_tokens"] = int(pt)
    if ct is not None:
        out["output_tokens"] = int(ct)
    if tt is not None:
        out["total_tokens"] = int(tt)
    return out or None


def _safe_dump(obj: Any) -> str:
    """Best-effort `model_dump → json.dumps` for log lines."""
    try:
        return json.dumps(obj.model_dump(), default=str)
    except Exception:  # noqa: BLE001
        return repr(obj)
