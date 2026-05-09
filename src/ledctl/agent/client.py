"""Thin OpenAI-compatible client pointed at OpenRouter."""

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
    """Raised when the configured `api_key_env` is unset."""


@dataclass
class CompletionResult:
    text: str
    tool_calls: list[dict[str, Any]]
    raw_message: dict[str, Any]
    finish_reason: str = ""
    model: str = ""
    usage: dict[str, int] | None = None


class AgentClient:
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
        client = self._get_client()
        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        effective_model = model or self.model
        if self.debug_logging:
            log.info(
                "agent.request: model=%s messages=%d tools=%s system_chars=%d",
                effective_model,
                len(full_messages),
                [t.get("function", {}).get("name") for t in tools],
                len(system_prompt),
            )
        try:
            resp = client.chat.completions.create(
                model=effective_model,
                messages=full_messages,
                tools=tools,
                tool_choice="auto",
                parallel_tool_calls=False,
            )
        except Exception:
            log.exception(
                "agent.request_failed: model=%s base_url=%s",
                effective_model, self.base_url,
            )
            raise

        if not resp.choices:
            err = (
                getattr(resp, "error", None)
                or (resp.model_extra or {}).get("error")
                or resp.model_extra
                or {}
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
        tool_calls: list[dict[str, Any]] = []
        raw_tool_calls: list[dict[str, Any]] = []
        for tc in msg.tool_calls or []:
            args_str = tc.function.arguments or "{}"
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                args = {"_raw": args_str, "_parse_error": True}
            tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
            raw_tool_calls.append(
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": args_str},
                }
            )
        raw_message: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if raw_tool_calls:
            raw_message["tool_calls"] = raw_tool_calls
        return CompletionResult(
            text=msg.content or "",
            tool_calls=tool_calls,
            raw_message=raw_message,
            finish_reason=finish,
            model=resp.model or effective_model,
            usage=_extract_usage(resp),
        )


def _extract_usage(resp: Any) -> dict[str, int] | None:
    u = getattr(resp, "usage", None)
    if u is None:
        return None
    pt = getattr(u, "prompt_tokens", None)
    ct = getattr(u, "completion_tokens", None)
    tt = getattr(u, "total_tokens", None)
    out: dict[str, int] = {}
    if pt is not None:
        out["input_tokens"] = int(pt)
    if ct is not None:
        out["output_tokens"] = int(ct)
    if tt is not None:
        out["total_tokens"] = int(tt)
    return out or None
