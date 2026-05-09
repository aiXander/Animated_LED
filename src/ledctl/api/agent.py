"""FastAPI routes for the language-driven control panel.

  POST   /agent/chat              — send a message; returns assistant + tool result
  GET    /agent/sessions/{id}     — full transcript for UI rehydration
  DELETE /agent/sessions/{id}     — wipe a session
  GET    /agent/config            — model id, history cap (read-only)
  PATCH  /agent/config            — adjust default crossfade between turns
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..agent import AgentClient, AgentTurn, ChatSession, MissingApiKey, SessionStore
from ..config import AgentConfig
from ..surface import (
    WRITE_EFFECT_TOOL_NAME,
    apply_write_effect,
    build_system_prompt,
    write_effect_tool_schema,
)

log = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str | None = None
    model: str | None = None


class AgentConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_crossfade_seconds: float | None = Field(None, ge=0.0, le=30.0)


def _attach_agent_state(
    app: FastAPI,
    cfg: AgentConfig,
    presets_dir: Path | None,  # legacy, ignored
) -> None:
    app.state.agent_cfg = cfg
    app.state.agent_sessions = SessionStore(history_max=cfg.history_max_messages)
    app.state.agent_client = AgentClient(
        base_url=cfg.base_url,
        api_key_env=cfg.api_key_env,
        model=cfg.model,
        request_timeout_seconds=cfg.request_timeout_seconds,
        debug_logging=cfg.debug_logging,
    )


def build_router(app: FastAPI) -> APIRouter:
    router = APIRouter(prefix="/agent", tags=["agent"])

    def _config_payload() -> dict:
        cfg: AgentConfig = app.state.agent_cfg
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

    @router.get("/config")
    async def agent_config() -> dict:
        return _config_payload()

    @router.patch("/config")
    async def patch_agent_config(body: AgentConfigPatch) -> dict:
        cfg: AgentConfig = app.state.agent_cfg
        if body.default_crossfade_seconds is not None:
            cfg.default_crossfade_seconds = body.default_crossfade_seconds
            # Mirror onto the live runtime so the next install/promote uses it.
            engine = app.state.engine
            engine.runtime.crossfade_seconds = float(body.default_crossfade_seconds)
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
            "turns": [asdict(t) for t in sess.turns],
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
        if not sess.check_rate_limit(cfg.rate_limit_per_minute):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"rate limit exceeded: {cfg.rate_limit_per_minute} requests/minute"
                ),
            )

        engine = app.state.engine
        runtime = engine.runtime
        topology = engine.topology
        audio_state = engine.audio_state
        effect_store = app.state.effect_store

        user_msg = {"role": "user", "content": req.message}
        client: AgentClient = app.state.agent_client
        turn = AgentTurn(user=req.message)

        accumulated: list[dict[str, Any]] = [user_msg]
        msgs_for_call: list[dict[str, Any]] = list(sess.messages) + [user_msg]

        last_error: dict[str, Any] | None = None
        primary_call: dict[str, Any] | None = None
        primary_tool_result: dict[str, Any] | None = None
        primary_usage: dict[str, int] | None = None
        result = None
        retries_used = 0

        max_attempts = 1 + max(0, int(cfg.retry_on_tool_error))

        for attempt in range(max_attempts):
            system_prompt = build_system_prompt(
                topology=topology,
                runtime=runtime,
                audio_state=audio_state,
                masters=engine.masters,
                crossfade_seconds=cfg.default_crossfade_seconds,
                last_error=last_error,
            )
            try:
                result = await asyncio.to_thread(
                    client.complete,
                    system_prompt=system_prompt,
                    messages=msgs_for_call,
                    tools=[write_effect_tool_schema()],
                    model=req.model,
                )
            except MissingApiKey as e:
                raise HTTPException(status_code=503, detail=str(e)) from e
            except Exception as e:
                log.exception("agent: LLM call failed")
                turn.error = f"llm_call_failed: {e}"
                turn.retries_used = retries_used
                sess.turns.append(turn)
                raise HTTPException(
                    status_code=502, detail=f"LLM call failed: {e}",
                ) from e

            attempt_msgs: list[dict[str, Any]] = [result.raw_message]
            attempt_primary: dict[str, Any] | None = None
            attempt_primary_result: dict[str, Any] | None = None
            any_tool_failed = False

            for tc in result.tool_calls:
                if tc["name"] == WRITE_EFFECT_TOOL_NAME and attempt_primary is None:
                    attempt_primary = tc
                    tool_result = apply_write_effect(
                        tc["arguments"],
                        runtime=runtime,
                        store=effect_store,
                    )
                    attempt_primary_result = tool_result
                else:
                    tool_result = {
                        "ok": False,
                        "error": "unsupported_tool",
                        "details": (
                            f"only {WRITE_EFFECT_TOOL_NAME!r} is supported; "
                            f"got {tc['name']!r}"
                        ),
                    }
                if not tool_result.get("ok"):
                    any_tool_failed = True
                attempt_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(tool_result, default=str),
                    }
                )

            accumulated.extend(attempt_msgs)
            msgs_for_call = msgs_for_call + attempt_msgs

            if attempt_primary is not None:
                primary_call = attempt_primary
                primary_tool_result = attempt_primary_result
                primary_usage = result.usage
            elif primary_usage is None:
                primary_usage = result.usage

            # Done if model didn't tool-call or every tool succeeded.
            if not result.tool_calls or not any_tool_failed:
                break
            if attempt + 1 >= max_attempts:
                break
            # Otherwise: surface the failure to the LLM for the next attempt.
            last_error = {
                "error": (attempt_primary_result or {}).get("error"),
                "details": (attempt_primary_result or {}).get("details"),
            }
            retries_used = attempt + 1
            log.warning(
                "agent.retry: session=%s attempt=%d/%d error=%s",
                sess.id, attempt + 1, max_attempts,
                last_error.get("error"),
            )

        sess.append_messages(accumulated)
        turn.assistant_text = (result.text if result else "") or ""
        if primary_call is not None:
            turn.tool_call = {
                "name": primary_call["name"],
                "arguments": primary_call["arguments"],
            }
            turn.tool_result = primary_tool_result
        turn.usage = primary_usage
        turn.retries_used = retries_used
        sess.turns.append(turn)

        return {
            "session_id": sess.id,
            "model": result.model if result else "",
            "assistant_text": turn.assistant_text,
            "tool_call": turn.tool_call,
            "tool_result": turn.tool_result,
            "finish_reason": result.finish_reason if result else "",
            "history_size": len(sess.messages),
            "retries_used": retries_used,
            "usage": turn.usage,
        }

    return router


def install_agent_routes(
    app: FastAPI,
    cfg: AgentConfig,
    presets_dir: Path | None = None,  # legacy
) -> None:
    _attach_agent_state(app, cfg, presets_dir)
    app.include_router(build_router(app))
