"""FastAPI routes for the language-driven control panel (Phase 6).

The HTTP surface is intentionally narrow:
  POST   /agent/chat              — send a message; returns assistant + tool result
  GET    /agent/sessions/{id}     — full transcript for UI rehydration
  DELETE /agent/sessions/{id}     — wipe a session
  GET    /agent/config            — model id, history cap (read-only)

Streaming: v1 returns the assistant reply and tool result in one JSON
response. SSE token streaming is mentioned in the roadmap as nice-to-have but
the tool-call payload is the load-bearing thing — small enough to deliver as
a single event. Easy to retrofit later by mirroring `client.complete` to a
streaming variant.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..agent import (
    UPDATE_LEDS_TOOL_NAME,
    AgentClient,
    AgentTurn,
    ChatSession,
    MissingApiKey,
    SessionStore,
    apply_update_leds,
    build_system_prompt,
    update_leds_tool_schema,
)
from ..config import AgentConfig

log = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str | None = Field(
        None, description="Reuse an existing session id; omit to start a new one"
    )
    model: str | None = Field(
        None,
        description=(
            "Override `agent.model` for this turn (still routed through "
            "OpenRouter)"
        ),
    )


class AgentConfigPatch(BaseModel):
    """Live-tunable subset of the agent config — affects future turns only."""

    model_config = ConfigDict(extra="forbid")
    default_crossfade_seconds: float | None = Field(None, ge=0.0, le=30.0)


def _turn_to_dict(turn: AgentTurn) -> dict[str, Any]:
    d = asdict(turn)
    return d


def _attach_agent_state(
    app: FastAPI,
    cfg: AgentConfig,
    presets_dir: Path | None,
) -> None:
    app.state.agent_cfg = cfg
    app.state.agent_presets_dir = presets_dir
    app.state.agent_sessions = SessionStore(history_max=cfg.history_max_messages)
    app.state.agent_client = AgentClient(
        base_url=cfg.base_url,
        api_key_env=cfg.api_key_env,
        model=cfg.model,
        request_timeout_seconds=cfg.request_timeout_seconds,
        debug_logging=cfg.debug_logging,
    )


def build_router(app: FastAPI) -> APIRouter:
    """Wire up /agent/* against the engine + config already on `app.state`.

    `app.state` is expected to have: `engine`, `config`, `agent_cfg`,
    `agent_sessions`, `agent_client`, `agent_presets_dir`. `_attach_agent_state`
    sets all of the agent_* ones.
    """
    router = APIRouter(prefix="/agent", tags=["agent"])

    def _config_payload() -> dict:
        cfg: AgentConfig = app.state.agent_cfg
        # Never echo the api key env *value* — only the var name.
        return {
            "enabled": cfg.enabled,
            "provider": cfg.provider,
            "base_url": cfg.base_url,
            "model": cfg.model,
            "history_max_messages": cfg.history_max_messages,
            "default_crossfade_seconds": cfg.default_crossfade_seconds,
            "rate_limit_per_minute": cfg.rate_limit_per_minute,
            "api_key_env": cfg.api_key_env,
            "retry_on_tool_error": cfg.retry_on_tool_error,
        }

    @router.get("/config")
    async def agent_config() -> dict:
        return _config_payload()

    @router.patch("/config")
    async def patch_agent_config(body: AgentConfigPatch) -> dict:
        cfg: AgentConfig = app.state.agent_cfg
        if body.default_crossfade_seconds is not None:
            cfg.default_crossfade_seconds = body.default_crossfade_seconds
        return _config_payload()

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        sess: ChatSession | None = app.state.agent_sessions.get(session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        return {
            "id": sess.id,
            "created_at": sess.created_at,
            "history_max_messages": sess.history_max,
            "turns": [_turn_to_dict(t) for t in sess.turns],
        }

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict:
        deleted = app.state.agent_sessions.delete(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_id}")
        return {"deleted": session_id}

    @router.post("/chat")
    async def chat(req: ChatRequest) -> dict:
        cfg: AgentConfig = app.state.agent_cfg
        if not cfg.enabled:
            raise HTTPException(status_code=503, detail="agent is disabled in config")
        store: SessionStore = app.state.agent_sessions
        sess = store.get_or_create(req.session_id)
        if cfg.debug_logging:
            log.info(
                "agent.chat: session=%s buffer=%d msg=%r",
                sess.id, len(sess.messages),
                req.message if len(req.message) <= 200 else req.message[:200] + "…",
            )
        if not sess.check_rate_limit(cfg.rate_limit_per_minute):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"rate limit exceeded for session {sess.id}: "
                    f"{cfg.rate_limit_per_minute} requests/minute"
                ),
            )

        engine = app.state.engine
        topology = engine.topology
        audio_state = engine.audio_state
        presets_dir: Path | None = app.state.agent_presets_dir

        user_msg = {"role": "user", "content": req.message}
        client: AgentClient = app.state.agent_client
        turn = AgentTurn(user=req.message)

        # Buffer of messages we'll commit to the session at the very end. We
        # accumulate across retry attempts so the LLM sees its prior failed
        # attempts (and the structured tool errors) on each retry.
        accumulated: list[dict[str, Any]] = [user_msg]
        # Mirror of `sess.messages + accumulated`, fed back to the LLM each
        # attempt without mutating the session until we're done.
        msgs_for_call: list[dict[str, Any]] = list(sess.messages) + [user_msg]

        # 1 initial attempt + up to N retries on tool-error.
        max_attempts = 1 + max(0, int(cfg.retry_on_tool_error))

        # Last-attempt artefacts (populated on every iteration; the final
        # values are what the operator UI sees).
        result = None
        primary_call: dict[str, Any] | None = None
        primary_tool_result: dict[str, Any] | None = None
        # Usage from the LLM call that produced `primary_call` (or the most
        # recent attempt if no tool call was ever emitted).
        primary_usage: dict[str, int] | None = None

        for attempt in range(max_attempts):
            # Regenerate per-turn prompt so each retry sees a fresh audio
            # snapshot + the latest engine state. The install/topology
            # doesn't change between attempts, so this is cheap.
            system_prompt = build_system_prompt(
                topology=topology,
                engine=engine,
                audio_state=audio_state,
                presets_dir=presets_dir,
                masters=engine.masters,
            )
            try:
                result = await asyncio.to_thread(
                    client.complete,
                    system_prompt=system_prompt,
                    messages=msgs_for_call,
                    tools=[update_leds_tool_schema()],
                    model=req.model,
                )
            except MissingApiKey as e:
                raise HTTPException(status_code=503, detail=str(e)) from e
            except Exception as e:  # noqa: BLE001 — surface the cause to the caller
                log.exception("agent: LLM call failed")
                turn.error = f"llm_call_failed: {e}"
                turn.retries_used = attempt
                sess.turns.append(turn)
                raise HTTPException(status_code=502, detail=f"LLM call failed: {e}") from e

            # Per-attempt messages (assistant + each tool reply). Appended to
            # both `accumulated` (for session commit) and `msgs_for_call` (for
            # the next retry's request payload).
            attempt_msgs: list[dict[str, Any]] = [result.raw_message]

            # The model may emit zero or more tool calls. We only honour the
            # first `update_leds`; anything else is reflected back as an
            # `unsupported_tool` result so the buffer stays well-formed
            # (every assistant tool_calls entry must be paired with a `tool`
            # reply).
            attempt_primary_call: dict[str, Any] | None = None
            attempt_primary_result: dict[str, Any] | None = None
            any_tool_failed = False
            for tc in result.tool_calls:
                if tc["name"] == UPDATE_LEDS_TOOL_NAME and attempt_primary_call is None:
                    attempt_primary_call = tc
                    tool_result = apply_update_leds(
                        tc["arguments"],
                        engine=engine,
                        default_crossfade_seconds=cfg.default_crossfade_seconds,
                    )
                    attempt_primary_result = tool_result
                else:
                    tool_result = {
                        "ok": False,
                        "error": "unsupported_tool",
                        "details": (
                            f"only {UPDATE_LEDS_TOOL_NAME!r} is supported; "
                            f"got {tc['name']!r}"
                        ),
                    }
                if not tool_result.get("ok"):
                    any_tool_failed = True
                attempt_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": _serialise_tool_result(tool_result),
                    }
                )

            accumulated.extend(attempt_msgs)
            msgs_for_call = msgs_for_call + attempt_msgs
            if attempt_primary_call is not None:
                primary_call = attempt_primary_call
                primary_tool_result = attempt_primary_result
                primary_usage = result.usage
            elif primary_call is None:
                # No tool call this attempt, and none from a prior attempt
                # either — fall back to this attempt's usage so the UI still
                # has token info for chat-only turns.
                primary_usage = result.usage

            # Decide whether to retry.
            #   - No tool calls at all → model is just chatting; done.
            #   - Every tool call succeeded → done.
            #   - Some tool call failed → retry if budget remains.
            if not result.tool_calls or not any_tool_failed:
                break
            if attempt + 1 >= max_attempts:
                break
            log.warning(
                "agent.tool_failed_retrying: session=%s attempt=%d/%d tool=%s "
                "error=%s",
                sess.id,
                attempt + 1,
                max_attempts,
                attempt_primary_call["name"] if attempt_primary_call else None,
                (attempt_primary_result or {}).get("error")
                if attempt_primary_result
                else "unsupported_tool",
            )
            turn.retries_used = attempt + 1

        # Loop exited — commit the entire run to the session in one shot.
        sess.append_messages(accumulated)
        turn.assistant_text = (result.text if result else "") or ""
        if primary_call is not None:
            turn.tool_call = {
                "name": primary_call["name"],
                "arguments": primary_call["arguments"],
            }
            turn.tool_result = primary_tool_result
        turn.usage = primary_usage
        sess.turns.append(turn)

        finish_reason = result.finish_reason if result else ""
        model_id = result.model if result else ""

        if cfg.debug_logging:
            log.info(
                "agent.chat_done: session=%s finish=%s text_chars=%d "
                "tool=%s tool_ok=%s retries=%d",
                sess.id,
                finish_reason,
                len(turn.assistant_text),
                primary_call["name"] if primary_call else None,
                turn.tool_result.get("ok") if turn.tool_result else None,
                turn.retries_used,
            )
        elif primary_call and turn.tool_result and not turn.tool_result.get("ok"):
            # Always surface tool-result failures so the operator sees them
            # even with debug_logging off.
            log.warning(
                "agent.tool_failed: session=%s tool=%s error=%s details=%s "
                "retries=%d",
                sess.id,
                primary_call["name"],
                turn.tool_result.get("error"),
                turn.tool_result.get("details"),
                turn.retries_used,
            )

        return {
            "session_id": sess.id,
            "model": model_id,
            "assistant_text": turn.assistant_text,
            "tool_call": turn.tool_call,
            "tool_result": turn.tool_result,
            "finish_reason": finish_reason,
            "history_size": len(sess.messages),
            "retries_used": turn.retries_used,
            "usage": turn.usage,
        }

    return router


def _serialise_tool_result(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, default=str)


def _deserialise_tool_result(content: str) -> Any:
    import json

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"_raw": content}


def install_agent_routes(
    app: FastAPI,
    cfg: AgentConfig,
    presets_dir: Path | None,
) -> None:
    """One-line install: attach state + mount the router on the existing app."""
    _attach_agent_state(app, cfg, presets_dir)
    app.include_router(build_router(app))
