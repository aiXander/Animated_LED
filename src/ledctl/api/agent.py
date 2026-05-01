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

    @router.get("/config")
    async def agent_config() -> dict:
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
        }

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

        system_prompt = build_system_prompt(
            topology=topology,
            engine=engine,
            audio_state=audio_state,
            presets_dir=presets_dir,
        )

        user_msg = {"role": "user", "content": req.message}
        # Send the full buffered history + the new user message. The system
        # prompt is *not* in the buffer — it's regenerated fresh per turn.
        msgs_for_call = list(sess.messages) + [user_msg]
        client: AgentClient = app.state.agent_client

        turn = AgentTurn(user=req.message)
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
            sess.turns.append(turn)
            raise HTTPException(status_code=502, detail=f"LLM call failed: {e}") from e

        # Persist user + assistant in the rolling buffer.
        new_messages: list[dict[str, Any]] = [user_msg, result.raw_message]

        # The model may emit zero or more tool calls. We only honour the first
        # `update_leds`; anything else is reflected back as a "no_op" tool
        # result so the buffer stays well-formed (every assistant tool_calls
        # entry must be paired with a `tool` reply).
        primary_call: dict[str, Any] | None = None
        for tc in result.tool_calls:
            if tc["name"] == UPDATE_LEDS_TOOL_NAME and primary_call is None:
                primary_call = tc
                tool_result = apply_update_leds(
                    tc["arguments"],
                    engine=engine,
                    default_crossfade_seconds=cfg.default_crossfade_seconds,
                )
            else:
                tool_result = {
                    "ok": False,
                    "error": "unsupported_tool",
                    "details": (
                        f"only {UPDATE_LEDS_TOOL_NAME!r} is supported; "
                        f"got {tc['name']!r}"
                    ),
                }
            new_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": _serialise_tool_result(tool_result),
                }
            )

        sess.append_messages(new_messages)
        turn.assistant_text = result.text
        if primary_call is not None:
            turn.tool_call = {
                "name": primary_call["name"],
                "arguments": primary_call["arguments"],
            }
            # Replay the corresponding result for the operator.
            for m in reversed(new_messages):
                if m.get("role") == "tool" and m.get("tool_call_id") == primary_call["id"]:
                    turn.tool_result = _deserialise_tool_result(m["content"])
                    break
        sess.turns.append(turn)

        if cfg.debug_logging:
            log.info(
                "agent.chat_done: session=%s finish=%s text_chars=%d "
                "tool=%s tool_ok=%s",
                sess.id,
                result.finish_reason,
                len(result.text or ""),
                primary_call["name"] if primary_call else None,
                turn.tool_result.get("ok") if turn.tool_result else None,
            )
        elif primary_call and turn.tool_result and not turn.tool_result.get("ok"):
            # Always surface tool-result failures so the operator sees them
            # even with debug_logging off.
            log.warning(
                "agent.tool_failed: session=%s tool=%s error=%s details=%s",
                sess.id,
                primary_call["name"],
                turn.tool_result.get("error"),
                turn.tool_result.get("details"),
            )

        return {
            "session_id": sess.id,
            "model": result.model,
            "assistant_text": turn.assistant_text,
            "tool_call": turn.tool_call,
            "tool_result": turn.tool_result,
            "finish_reason": result.finish_reason,
            "history_size": len(sess.messages),
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
