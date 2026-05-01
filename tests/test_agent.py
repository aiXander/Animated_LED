"""Tests for Phase 6 — language-driven control panel.

The LLM is mocked at the AgentClient layer (one method, one mock) so these
tests never touch the network. Anything that needs the OpenRouter client
patches `AgentClient.complete` to return a synthetic CompletionResult.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ledctl.agent import (
    UPDATE_LEDS_TOOL_NAME,
    UpdateLedsInput,
    apply_update_leds,
    build_system_prompt,
    update_leds_tool_schema,
)
from ledctl.agent.client import AgentClient, CompletionResult, MissingApiKey
from ledctl.agent.session import ChatSession, SessionStore
from ledctl.api.server import create_app
from ledctl.audio.capture import AudioCapture
from ledctl.config import load_config
from tests.test_api import DEV, PRESETS


@pytest.fixture
def agent_client_app(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with audio stubbed out so the engine boots in tests."""

    def _noop_start(self: AudioCapture) -> None:
        self.state.enabled = False

    monkeypatch.setattr(AudioCapture, "start", _noop_start)
    cfg = load_config(DEV)
    app = create_app(cfg, presets_dir=PRESETS)
    with TestClient(app) as c:
        yield c


# ---- system prompt ----


def test_build_system_prompt_contains_install_and_catalogue(
    agent_client_app: TestClient,
):
    engine = agent_client_app.app.state.engine
    prompt = build_system_prompt(
        topology=engine.topology,
        engine=engine,
        audio_state=engine.audio_state,
        presets_dir=PRESETS,
    )
    # Install summary
    assert "1800 LEDs" in prompt
    # Effects catalogue — at least the registered names show up
    assert "scroll" in prompt
    assert "sparkle" in prompt
    assert "noise" in prompt
    assert "radial" in prompt
    # Palette guidance
    assert "fire" in prompt
    assert "mono_<hex>" in prompt
    # Bindings rubric
    assert "audio.rms" in prompt
    assert "brightness" in prompt
    # Blend modes
    assert "screen" in prompt
    # Presets list
    assert "peak" in prompt
    # Examples + rubric headers
    assert "EXAMPLES" in prompt
    assert "RUBRIC" in prompt
    # Current-state JSON includes the default boot layer
    assert "scroll" in prompt
    # Audio off in tests; prompt should say so explicitly
    assert "Audio capture is OFF" in prompt


def test_system_prompt_embeds_full_per_effect_schemas(
    agent_client_app: TestClient,
):
    """The LLM-facing prompt must carry every effect's full Params schema,
    including nested palette + bindings, with `additionalProperties: false`
    so the model knows extras will be rejected."""
    engine = agent_client_app.app.state.engine
    prompt = build_system_prompt(
        topology=engine.topology,
        engine=engine,
        audio_state=engine.audio_state,
        presets_dir=PRESETS,
    )
    # Each effect's schema is dumped (look for fields that don't appear
    # elsewhere in the prompt prose).
    assert "cross_phase" in prompt        # scroll-specific
    assert "octaves" in prompt            # noise-specific
    assert "decay" in prompt              # sparkle-specific
    assert "center" in prompt             # radial-specific
    # Strictness signal
    assert "additionalProperties" in prompt
    # Nested types reference
    assert "ModulatorSpec" in prompt
    assert "PaletteSpec" in prompt
    # Recipes + anti-patterns sections
    assert "RECIPES" in prompt
    assert "ANTI-PATTERNS" in prompt
    assert "pulsating" in prompt or "pulsate" in prompt
    # Modulator sources enumerated
    assert "lfo.sin" in prompt
    assert "audio.low" in prompt


def test_build_system_prompt_includes_audio_when_enabled(
    agent_client_app: TestClient,
):
    engine = agent_client_app.app.state.engine
    audio = engine.audio_state
    audio.enabled = True
    audio.device_name = "fake-mic"
    audio.rms = 0.42
    audio.rms_norm = 0.55
    audio.low = 0.6
    audio.low_norm = 0.7
    prompt = build_system_prompt(
        topology=engine.topology,
        engine=engine,
        audio_state=audio,
        presets_dir=PRESETS,
    )
    assert "fake-mic" in prompt
    assert "0.420" in prompt or "0.42" in prompt
    assert "20–250 Hz" in prompt or "20-250 Hz" in prompt


# ---- tool schema + handler ----


def test_update_leds_schema_is_a_function_tool():
    schema = update_leds_tool_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == UPDATE_LEDS_TOOL_NAME
    params = schema["function"]["parameters"]
    assert params["type"] == "object"
    assert "layers" in params["properties"]
    assert "crossfade_seconds" in params["properties"]
    assert "blackout" in params["properties"]


def test_update_leds_schema_pins_effect_names_to_registered_set():
    """The `effect` field is enum'd to the catalogue so the LLM can't invent
    names like 'pulse' or 'breathing' and have them accepted by the tool spec."""
    schema = update_leds_tool_schema()
    layer = schema["function"]["parameters"]["properties"]["layers"]["items"]
    effect = layer["properties"]["effect"]
    assert effect["type"] == "string"
    assert "enum" in effect
    # Every registered effect appears; nothing unexpected.
    from ledctl.effects.registry import list_effects
    assert set(effect["enum"]) == set(list_effects())


def test_apply_update_leds_unknown_param_returns_hint(
    agent_client_app: TestClient,
):
    """Unknown param keys (a real failure mode in practice — `scroll_phase`,
    `width` on noise, etc.) must come back as a structured error with the
    list of valid keys, so the LLM can self-correct in one turn."""
    engine = agent_client_app.app.state.engine
    before = engine.layer_state()
    result = apply_update_leds(
        {
            "layers": [
                {
                    "effect": "scroll",
                    "params": {"scroll_phase": [0, 0, 0], "palette": "fire"},
                }
            ]
        },
        engine=engine,
        default_crossfade_seconds=1.0,
    )
    assert result["ok"] is False
    assert result["error"] == "layer_validation_failed"
    msg = json.dumps(result["details"])
    assert "scroll_phase" in msg
    assert "cross_phase" in msg or "valid keys" in msg
    # Engine state untouched.
    assert engine.layer_state() == before


def test_apply_update_leds_misnested_bindings_rejected(
    agent_client_app: TestClient,
):
    """`bindings` belongs INSIDE `params`. If the model puts it at the layer
    top level we want a clean error, not a silent drop."""
    engine = agent_client_app.app.state.engine
    result = apply_update_leds(
        {
            "layers": [
                {
                    "effect": "scroll",
                    "params": {"palette": "mono_ff0000"},
                    "bindings": {  # wrong nesting: belongs in params
                        "brightness": {"source": "lfo.sin", "period_s": 1.0}
                    },
                }
            ]
        },
        engine=engine,
        default_crossfade_seconds=1.0,
    )
    assert result["ok"] is False
    # `UpdateLedsLayer` itself is `extra="forbid"`, so this fails at the
    # outer schema layer rather than per-effect — either way, not a no-op.
    assert "bindings" in json.dumps(result["details"])


def test_update_leds_input_round_trips_a_minimal_spec():
    spec = {
        "layers": [
            {
                "effect": "scroll",
                "params": {"speed": 0.5, "palette": "fire"},
            }
        ],
        "crossfade_seconds": 0.5,
    }
    parsed = UpdateLedsInput.model_validate(spec)
    assert parsed.layers[0].effect == "scroll"
    assert parsed.layers[0].blend == "normal"
    assert parsed.crossfade_seconds == 0.5
    assert parsed.blackout is False


def test_apply_update_leds_calls_engine_crossfade(agent_client_app: TestClient):
    engine = agent_client_app.app.state.engine
    args = {
        "layers": [
            {
                "effect": "scroll",
                "params": {"speed": 0.5, "palette": "fire"},
            }
        ],
        "crossfade_seconds": 0.0,
    }
    result = apply_update_leds(args, engine=engine, default_crossfade_seconds=1.0)
    assert result["ok"] is True
    assert result["crossfade_seconds"] == 0.0
    assert [l_["effect"] for l_ in result["layers"]] == ["scroll"]
    # The engine state actually changed.
    assert engine.layer_state()[0]["params"]["palette"]["name"] == "fire"


def test_apply_update_leds_blackout_short_circuits(agent_client_app: TestClient):
    engine = agent_client_app.app.state.engine
    result = apply_update_leds(
        {"blackout": True, "layers": []},
        engine=engine,
        default_crossfade_seconds=1.0,
    )
    assert result["ok"] is True
    assert result["blackout"] is True
    assert engine.mixer.blackout is True


def test_apply_update_leds_clears_blackout_on_normal_call(
    agent_client_app: TestClient,
):
    engine = agent_client_app.app.state.engine
    engine.mixer.blackout = True
    result = apply_update_leds(
        {
            "layers": [{"effect": "scroll", "params": {"palette": "ice"}}],
            "crossfade_seconds": 0.0,
        },
        engine=engine,
        default_crossfade_seconds=1.0,
    )
    assert result["ok"] is True
    assert engine.mixer.blackout is False


def test_apply_update_leds_unknown_effect_returns_structured_error(
    agent_client_app: TestClient,
):
    engine = agent_client_app.app.state.engine
    before = engine.layer_state()
    result = apply_update_leds(
        {"layers": [{"effect": "not-a-real-effect"}]},
        engine=engine,
        default_crossfade_seconds=1.0,
    )
    assert result["ok"] is False
    assert result["error"] == "layer_validation_failed"
    assert any("not-a-real-effect" in d.get("msg", "") for d in result["details"])
    # Engine untouched.
    assert engine.layer_state() == before


def test_apply_update_leds_bad_palette_returns_pydantic_error(
    agent_client_app: TestClient,
):
    engine = agent_client_app.app.state.engine
    result = apply_update_leds(
        {
            "layers": [
                {"effect": "scroll", "params": {"palette": "puce-fluorescent"}}
            ]
        },
        engine=engine,
        default_crossfade_seconds=1.0,
    )
    assert result["ok"] is False
    assert result["error"] == "layer_validation_failed"


def test_apply_update_leds_uses_default_crossfade_when_omitted(
    agent_client_app: TestClient,
):
    engine = agent_client_app.app.state.engine
    result = apply_update_leds(
        {"layers": [{"effect": "scroll"}]},
        engine=engine,
        default_crossfade_seconds=2.5,
    )
    assert result["ok"] is True
    assert result["crossfade_seconds"] == 2.5


# ---- session store + rolling buffer ----


def test_session_store_creates_and_recovers_session():
    store = SessionStore(history_max=10)
    sess1 = store.get_or_create(None)
    sess2 = store.get_or_create(sess1.id)
    assert sess1 is sess2
    assert store.get(sess1.id) is sess1
    assert store.delete(sess1.id) is True
    assert store.get(sess1.id) is None


def test_session_buffer_caps_at_history_max():
    sess = ChatSession(id="s", history_max=5)
    # Push 8 plain user messages — buffer should keep the latest 5.
    sess.append_messages(
        [{"role": "user", "content": f"msg {i}"} for i in range(8)]
    )
    assert len(sess.messages) == 5
    contents = [m["content"] for m in sess.messages]
    assert contents == [f"msg {i}" for i in range(3, 8)]


def test_session_heals_dangling_tool_message():
    sess = ChatSession(id="s", history_max=4)
    # Simulate trim leaving a dangling `tool` at the front.
    sess.append_messages(
        [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
            {"role": "tool", "tool_call_id": "x", "content": "{}"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "y"}]},
            {"role": "tool", "tool_call_id": "y", "content": "{}"},
        ]
    )
    # First two would be trimmed, leaving (tool, user, asst, tool); the heal
    # drops the leading tool so the buffer is well-formed.
    roles = [m["role"] for m in sess.messages]
    assert roles[0] != "tool"
    assert "tool" in roles  # later tool entries preserved


def test_session_rate_limit_blocks_when_exceeded():
    sess = ChatSession(id="s", history_max=10)
    # 3 calls at t=0..1.99 should all pass; the 4th in the same minute fails.
    assert sess.check_rate_limit(3, now=1000.0)
    assert sess.check_rate_limit(3, now=1000.5)
    assert sess.check_rate_limit(3, now=1001.0)
    assert sess.check_rate_limit(3, now=1001.5) is False
    # 61 s later, the window has rolled over → allowed again.
    assert sess.check_rate_limit(3, now=1062.0)


# ---- HTTP integration (mocked LLM) ----


def _completion_with_tool(args: dict[str, Any], text: str = "") -> CompletionResult:
    """Build a CompletionResult that emits a single update_leds call."""
    raw = {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": UPDATE_LEDS_TOOL_NAME,
                    "arguments": json.dumps(args),
                },
            }
        ],
    }
    return CompletionResult(
        text=text,
        tool_calls=[{"id": "call_1", "name": UPDATE_LEDS_TOOL_NAME, "arguments": args}],
        raw_message=raw,
        finish_reason="tool_calls",
        model="mocked-model",
    )


def test_agent_chat_applies_tool_call_and_morphs_engine(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    captured: dict[str, Any] = {}

    def fake_complete(self, **kw):
        captured.update(kw)
        return _completion_with_tool(
            {
                "layers": [
                    {
                        "effect": "scroll",
                        "params": {"palette": "ice", "speed": 0.4},
                    }
                ],
                "crossfade_seconds": 0.0,
            },
            text="cool ice wave",
        )

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post(
        "/agent/chat", json={"message": "icy slow wave"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assistant_text"] == "cool ice wave"
    assert body["tool_call"]["name"] == UPDATE_LEDS_TOOL_NAME
    assert body["tool_result"]["ok"] is True
    assert body["session_id"]
    # System prompt was passed in fresh.
    assert "1800 LEDs" in captured["system_prompt"]
    # First user turn → only the new user message in the call (the buffer was
    # empty before).
    assert captured["messages"][-1]["role"] == "user"
    # Engine state actually changed.
    state = agent_client_app.get("/state").json()
    assert state["layers"][0]["params"]["palette"]["name"] == "ice"


def test_agent_chat_session_buffer_grows_across_turns(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    counter = {"n": 0}

    def fake_complete(self, **kw):
        counter["n"] += 1
        return _completion_with_tool(
            {"layers": [{"effect": "scroll", "params": {}}], "crossfade_seconds": 0.0}
        )

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    # Make sure the cap is large enough to hold both turns for this assertion.
    agent_client_app.app.state.agent_sessions.history_max = 20
    r1 = agent_client_app.post("/agent/chat", json={"message": "m1"})
    sid = r1.json()["session_id"]
    sess = agent_client_app.app.state.agent_sessions.get(sid)
    sess.history_max = 20
    r2 = agent_client_app.post(
        "/agent/chat", json={"message": "m2", "session_id": sid}
    )
    assert r2.status_code == 200
    # 2 turns × 3 messages each = 6 in the buffer.
    assert r2.json()["history_size"] == 6
    sess_resp = agent_client_app.get(f"/agent/sessions/{sid}").json()
    assert len(sess_resp["turns"]) == 2


def test_agent_chat_buffer_rolls_over_at_history_cap(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    # Shrink the cap on the live store so we can hit it with few requests.
    agent_client_app.app.state.agent_sessions.history_max = 6

    def fake_complete(self, **kw):
        return _completion_with_tool(
            {"layers": [{"effect": "scroll", "params": {}}], "crossfade_seconds": 0.0}
        )

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    sid: str | None = None
    for i in range(5):  # 5 turns × 3 = 15 messages, capped at 6
        r = agent_client_app.post(
            "/agent/chat", json={"message": f"m{i}", "session_id": sid}
        )
        sid = r.json()["session_id"]
        # The store cap is the running session's cap, so set it on the session.
        sess = agent_client_app.app.state.agent_sessions.get(sid)
        sess.history_max = 6
    sess = agent_client_app.app.state.agent_sessions.get(sid)
    assert len(sess.messages) <= 6
    # First message in the buffer should not be a `tool` reply (heal worked).
    assert sess.messages[0]["role"] != "tool"


def test_agent_chat_returns_503_on_missing_api_key(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def boom(self, **kw):
        raise MissingApiKey("environment variable 'OPENROUTER_API_KEY' is not set")

    monkeypatch.setattr(AgentClient, "complete", boom)
    r = agent_client_app.post("/agent/chat", json={"message": "hi"})
    assert r.status_code == 503
    assert "OPENROUTER_API_KEY" in r.text


def test_agent_chat_surfaces_tool_validation_error_to_buffer(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def fake_complete(self, **kw):
        # Bogus arguments → handler returns ok=False; buffer still gets the
        # tool-result so the next turn can self-correct.
        return _completion_with_tool(
            {"layers": [{"effect": "no-such-effect"}]},
        )

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post("/agent/chat", json={"message": "broken"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool_result"]["ok"] is False
    assert body["tool_result"]["error"] == "layer_validation_failed"


def test_agent_chat_rate_limited(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    # Drop the per-minute cap to 2 just for this test; force everything through
    # one session so the rate state actually compounds.
    agent_client_app.app.state.agent_cfg.rate_limit_per_minute = 2

    def fake_complete(self, **kw):
        return _completion_with_tool(
            {"layers": [{"effect": "scroll"}], "crossfade_seconds": 0.0}
        )

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r1 = agent_client_app.post("/agent/chat", json={"message": "a"})
    sid = r1.json()["session_id"]
    agent_client_app.post(
        "/agent/chat", json={"message": "b", "session_id": sid}
    )
    r3 = agent_client_app.post(
        "/agent/chat", json={"message": "c", "session_id": sid}
    )
    assert r3.status_code == 429


def test_agent_config_endpoint_exposes_settings(agent_client_app: TestClient):
    r = agent_client_app.get("/agent/config")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "openrouter"
    assert body["model"]
    assert body["history_max_messages"] >= 2
    assert body["api_key_env"] == "OPENROUTER_API_KEY"


def test_agent_sessions_get_404_for_unknown(agent_client_app: TestClient):
    r = agent_client_app.get("/agent/sessions/nope")
    assert r.status_code == 404


def test_agent_sessions_delete(agent_client_app: TestClient,
                                monkeypatch: pytest.MonkeyPatch):
    def fake_complete(self, **kw):
        return _completion_with_tool(
            {"layers": [{"effect": "scroll"}], "crossfade_seconds": 0.0}
        )

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post("/agent/chat", json={"message": "hi"})
    sid = r.json()["session_id"]
    r2 = agent_client_app.delete(f"/agent/sessions/{sid}")
    assert r2.status_code == 200
    r3 = agent_client_app.get(f"/agent/sessions/{sid}")
    assert r3.status_code == 404


def test_chat_html_served(agent_client_app: TestClient):
    # The chat UI is now part of the main landing page (Phase 7); /chat as a
    # standalone route was removed.
    r = agent_client_app.get("/")
    assert r.status_code == 200
    assert 'id="chat"' in r.text
    assert "/agent/chat" in r.text


def test_agent_chat_disabled_returns_503(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    agent_client_app.app.state.agent_cfg.enabled = False
    r = agent_client_app.post("/agent/chat", json={"message": "hi"})
    assert r.status_code == 503
    assert "disabled" in r.text


def test_unsupported_tool_call_recorded_as_error(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def fake_complete(self, **kw):
        # Synthesise an unknown tool call.
        raw = {
            "role": "assistant",
            "content": "trying something",
            "tool_calls": [
                {
                    "id": "call_X",
                    "type": "function",
                    "function": {"name": "do_something_else", "arguments": "{}"},
                }
            ],
        }
        return CompletionResult(
            text="trying something",
            tool_calls=[
                {"id": "call_X", "name": "do_something_else", "arguments": {}}
            ],
            raw_message=raw,
            finish_reason="tool_calls",
            model="mock",
        )

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post("/agent/chat", json={"message": "x"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Primary tool was not update_leds → no tool_call/tool_result on the turn.
    assert body["tool_call"] is None


def test_config_includes_agent_block(agent_client_app: TestClient):
    r = agent_client_app.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert "agent" in body
    assert body["agent"]["provider"] == "openrouter"
    assert body["agent"]["model"]
