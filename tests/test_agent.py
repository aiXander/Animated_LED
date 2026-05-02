"""Tests for the language-driven control panel against the new surface."""

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
from ledctl.surface import REGISTRY
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


def test_build_system_prompt_contains_install_and_catalogue(agent_client_app: TestClient):
    engine = agent_client_app.app.state.engine
    prompt = build_system_prompt(
        topology=engine.topology,
        engine=engine,
        audio_state=engine.audio_state,
        presets_dir=PRESETS,
        masters=engine.masters,
    )
    assert "1800 LEDs" in prompt
    assert "CONTROL SURFACE" in prompt
    # Core primitives appear
    for kind in ("wave", "radial", "noise", "sparkles", "audio_band", "envelope",
                 "palette_lookup", "palette_named"):
        assert kind in prompt
    assert "fire" in prompt and "mono_<hex>" in prompt
    assert "screen" in prompt
    # Presets list
    assert "peak" in prompt
    # Anti-patterns block + rubric
    assert "ANTI-PATTERNS" in prompt
    assert "RUBRIC" in prompt
    # Audio off in tests; prompt should say so
    assert "Audio capture is OFF" in prompt
    # Masters block present
    assert "OPERATOR MASTERS" in prompt


def test_build_system_prompt_includes_audio_when_enabled(agent_client_app: TestClient):
    engine = agent_client_app.app.state.engine
    audio = engine.audio_state
    audio.enabled = True
    audio.device_name = "fake-mic"
    audio.rms = 0.42
    audio.rms_norm = 0.55
    prompt = build_system_prompt(
        topology=engine.topology,
        engine=engine,
        audio_state=audio,
        presets_dir=PRESETS,
        masters=engine.masters,
    )
    assert "fake-mic" in prompt
    assert "0.420" in prompt or "0.42" in prompt


# ---- tool schema + handler ----


def test_update_leds_schema_is_a_function_tool():
    schema = update_leds_tool_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == UPDATE_LEDS_TOOL_NAME
    params = schema["function"]["parameters"]
    assert params["type"] == "object"
    assert "layers" in params["properties"]
    # `crossfade_seconds` is deliberately *not* exposed to the LLM — the
    # operator's master crossfade slider is the single source of truth.
    assert "crossfade_seconds" not in params["properties"]
    assert "blackout" in params["properties"]


def test_update_leds_schema_pins_node_kinds_to_registry():
    schema = update_leds_tool_schema()
    layer = schema["function"]["parameters"]["properties"]["layers"]["items"]
    node = layer["properties"]["node"]
    assert "enum" in node["properties"]["kind"]
    assert set(node["properties"]["kind"]["enum"]) == set(REGISTRY)


def test_apply_update_leds_unknown_param_returns_hint(agent_client_app: TestClient):
    engine = agent_client_app.app.state.engine
    before = engine.layer_state()
    result = apply_update_leds(
        {
            "layers": [
                {
                    "node": {
                        "kind": "palette_lookup",
                        "params": {
                            "scalar": {
                                "kind": "wave",
                                "params": {"axis": "x", "scroll_phase": [0, 0, 0]},
                            },
                            "palette": "fire",
                        },
                    }
                }
            ]
        },
        engine=engine,
        default_crossfade_seconds=1.0,
    )
    assert result["ok"] is False
    assert result["error"] == "layer_validation_failed"
    msg = json.dumps(result["details"])
    assert "scroll_phase" in msg or "Extra" in msg
    assert engine.layer_state() == before


def test_apply_update_leds_unknown_kind_returns_structured_error(agent_client_app: TestClient):
    engine = agent_client_app.app.state.engine
    before = engine.layer_state()
    result = apply_update_leds(
        {"layers": [{"node": {"kind": "not-a-real-kind", "params": {}}}]},
        engine=engine,
        default_crossfade_seconds=1.0,
    )
    assert result["ok"] is False
    assert result["error"] == "layer_validation_failed"
    assert any("not-a-real-kind" in (d.get("msg") or "") for d in result["details"])
    assert engine.layer_state() == before


def test_update_leds_input_round_trips_a_minimal_spec():
    spec = {
        "layers": [
            {
                "node": {
                    "kind": "palette_lookup",
                    "params": {
                        "scalar": {"kind": "wave", "params": {"speed": 0.5}},
                        "palette": "fire",
                    },
                }
            }
        ],
    }
    parsed = UpdateLedsInput.model_validate(spec)
    assert parsed.layers[0].node.kind == "palette_lookup"
    assert parsed.layers[0].blend == "normal"
    assert parsed.blackout is False


def test_update_leds_input_rejects_crossfade_field():
    """The LLM must not pick transition speed — schema forbids it."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        UpdateLedsInput.model_validate({"layers": [], "crossfade_seconds": 0.5})


def test_apply_update_leds_calls_engine_crossfade(agent_client_app: TestClient):
    engine = agent_client_app.app.state.engine
    args = {
        "layers": [
            {
                "node": {
                    "kind": "palette_lookup",
                    "params": {
                        "scalar": {"kind": "wave", "params": {"speed": 0.5}},
                        "palette": "fire",
                    },
                }
            }
        ],
    }
    result = apply_update_leds(args, engine=engine, default_crossfade_seconds=1.0)
    assert result["ok"] is True
    assert result["crossfade_seconds"] == 1.0
    assert len(result["layers"]) == 1
    assert engine.layer_state()[0]["node"]["params"]["palette"] == "fire"


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


def test_apply_update_leds_clears_blackout_on_normal_call(agent_client_app: TestClient):
    engine = agent_client_app.app.state.engine
    engine.mixer.blackout = True
    result = apply_update_leds(
        {
            "layers": [
                {
                    "node": {
                        "kind": "palette_lookup",
                        "params": {
                            "scalar": {"kind": "wave", "params": {}},
                            "palette": "ice",
                        },
                    }
                }
            ],
        },
        engine=engine,
        default_crossfade_seconds=1.0,
    )
    assert result["ok"] is True
    assert engine.mixer.blackout is False


def test_apply_update_leds_bad_palette_returns_compile_error(agent_client_app: TestClient):
    engine = agent_client_app.app.state.engine
    result = apply_update_leds(
        {
            "layers": [
                {
                    "node": {
                        "kind": "palette_lookup",
                        "params": {
                            "scalar": {"kind": "constant", "params": {"value": 0.0}},
                            "palette": "puce-fluorescent",
                        },
                    }
                }
            ]
        },
        engine=engine,
        default_crossfade_seconds=1.0,
    )
    assert result["ok"] is False
    assert result["error"] == "layer_validation_failed"


def test_apply_update_leds_uses_default_crossfade_when_omitted(agent_client_app: TestClient):
    engine = agent_client_app.app.state.engine
    result = apply_update_leds(
        {"layers": [{"node": {"kind": "palette_lookup", "params": {
            "scalar": {"kind": "constant", "params": {"value": 0.0}},
            "palette": "white",
        }}}]},
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
    sess.append_messages(
        [{"role": "user", "content": f"msg {i}"} for i in range(8)]
    )
    assert len(sess.messages) == 5


def test_session_heals_dangling_tool_message():
    sess = ChatSession(id="s", history_max=4)
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
    roles = [m["role"] for m in sess.messages]
    assert roles[0] != "tool"
    assert "tool" in roles


def test_session_rate_limit_blocks_when_exceeded():
    sess = ChatSession(id="s", history_max=10)
    assert sess.check_rate_limit(3, now=1000.0)
    assert sess.check_rate_limit(3, now=1000.5)
    assert sess.check_rate_limit(3, now=1001.0)
    assert sess.check_rate_limit(3, now=1001.5) is False
    assert sess.check_rate_limit(3, now=1062.0)


# ---- HTTP integration (mocked LLM) ----


def _completion_with_tool(args: dict[str, Any], text: str = "") -> CompletionResult:
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


def _ice_wave_args() -> dict[str, Any]:
    return {
        "layers": [
            {
                "node": {
                    "kind": "palette_lookup",
                    "params": {
                        "scalar": {"kind": "wave", "params": {"speed": 0.4}},
                        "palette": "ice",
                    },
                }
            }
        ],
    }


def test_agent_chat_applies_tool_call_and_morphs_engine(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    captured: dict[str, Any] = {}

    def fake_complete(self, **kw):
        captured.update(kw)
        return _completion_with_tool(_ice_wave_args(), text="cool ice wave")

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post("/agent/chat", json={"message": "icy slow wave"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assistant_text"] == "cool ice wave"
    assert body["tool_call"]["name"] == UPDATE_LEDS_TOOL_NAME
    assert body["tool_result"]["ok"] is True
    assert body["session_id"]
    assert "1800 LEDs" in captured["system_prompt"]
    state = agent_client_app.get("/state").json()
    assert state["layers"][0]["node"]["params"]["palette"] == "ice"


def test_agent_chat_session_buffer_grows_across_turns(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def fake_complete(self, **kw):
        return _completion_with_tool(_ice_wave_args())

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    agent_client_app.app.state.agent_sessions.history_max = 20
    r1 = agent_client_app.post("/agent/chat", json={"message": "m1"})
    sid = r1.json()["session_id"]
    sess = agent_client_app.app.state.agent_sessions.get(sid)
    sess.history_max = 20
    r2 = agent_client_app.post(
        "/agent/chat", json={"message": "m2", "session_id": sid}
    )
    assert r2.status_code == 200
    assert r2.json()["history_size"] == 6
    sess_resp = agent_client_app.get(f"/agent/sessions/{sid}").json()
    assert len(sess_resp["turns"]) == 2


def test_agent_chat_buffer_rolls_over_at_history_cap(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    agent_client_app.app.state.agent_sessions.history_max = 6

    def fake_complete(self, **kw):
        return _completion_with_tool(_ice_wave_args())

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    sid: str | None = None
    for i in range(5):
        r = agent_client_app.post(
            "/agent/chat", json={"message": f"m{i}", "session_id": sid}
        )
        sid = r.json()["session_id"]
        sess = agent_client_app.app.state.agent_sessions.get(sid)
        sess.history_max = 6
    sess = agent_client_app.app.state.agent_sessions.get(sid)
    assert len(sess.messages) <= 6
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
    # Disable retries so the first failure surfaces directly.
    agent_client_app.app.state.agent_cfg.retry_on_tool_error = 0

    def fake_complete(self, **kw):
        return _completion_with_tool(
            {"layers": [{"node": {"kind": "no-such-kind", "params": {}}}]}
        )

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post("/agent/chat", json={"message": "broken"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool_result"]["ok"] is False
    assert body["tool_result"]["error"] == "layer_validation_failed"
    assert body["retries_used"] == 0


def test_agent_chat_auto_retries_and_self_corrects(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """A failed tool call triggers a second LLM round-trip with the error in
    the buffer; the model corrects on the retry and the operator sees success
    in one /agent/chat response."""
    agent_client_app.app.state.agent_cfg.retry_on_tool_error = 2
    agent_client_app.app.state.agent_sessions.history_max = 50

    call_log: list[list[dict[str, Any]]] = []
    completions = [
        # Attempt 1: broken (unknown primitive kind) — tool result will be
        # `ok: false` with a structured `layer_validation_failed`.
        _completion_with_tool(
            {"layers": [{"node": {"kind": "no-such-kind", "params": {}}}]},
            text="oops let me try again",
        ),
        # Attempt 2: correct.
        _completion_with_tool(_ice_wave_args(), text="fixed"),
    ]

    def fake_complete(self, **kw):
        call_log.append(list(kw["messages"]))
        return completions[len(call_log) - 1]

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post("/agent/chat", json={"message": "icy slow wave"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Final result is the corrected one.
    assert body["tool_result"]["ok"] is True
    assert body["assistant_text"] == "fixed"
    assert body["retries_used"] == 1
    # The retry-attempt request payload must include the failed tool result so
    # the model can self-correct.
    assert len(call_log) == 2
    retry_msgs = call_log[1]
    tool_msg = next(m for m in reversed(retry_msgs) if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    assert payload["ok"] is False
    assert payload["error"] == "layer_validation_failed"
    # Engine state reflects the *successful* second call.
    state = agent_client_app.get("/state").json()
    assert state["layers"][0]["node"]["params"]["palette"] == "ice"


def test_agent_chat_retry_budget_exhausted_surfaces_failure(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Model never recovers; after `retry_on_tool_error` extra attempts the
    final failure surfaces to the operator."""
    agent_client_app.app.state.agent_cfg.retry_on_tool_error = 2
    agent_client_app.app.state.agent_sessions.history_max = 50

    call_count = {"n": 0}

    def fake_complete(self, **kw):
        call_count["n"] += 1
        return _completion_with_tool(
            {"layers": [{"node": {"kind": "no-such-kind", "params": {}}}]}
        )

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post("/agent/chat", json={"message": "broken"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool_result"]["ok"] is False
    assert body["tool_result"]["error"] == "layer_validation_failed"
    # 1 initial + 2 retries = 3 LLM calls total
    assert call_count["n"] == 3
    assert body["retries_used"] == 2


def test_agent_chat_no_retry_when_tool_succeeds(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """A successful tool call must not trigger any retry, regardless of budget."""
    agent_client_app.app.state.agent_cfg.retry_on_tool_error = 3
    call_count = {"n": 0}

    def fake_complete(self, **kw):
        call_count["n"] += 1
        return _completion_with_tool(_ice_wave_args())

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post("/agent/chat", json={"message": "icy"})
    assert r.status_code == 200, r.text
    assert r.json()["retries_used"] == 0
    assert call_count["n"] == 1


def test_agent_chat_no_retry_when_no_tool_call(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Model just chats (no tool call) — retry budget is irrelevant."""
    agent_client_app.app.state.agent_cfg.retry_on_tool_error = 3
    call_count = {"n": 0}

    def fake_complete(self, **kw):
        call_count["n"] += 1
        return CompletionResult(
            text="hi there",
            tool_calls=[],
            raw_message={"role": "assistant", "content": "hi there"},
            finish_reason="stop",
            model="mock",
        )

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post("/agent/chat", json={"message": "hello"})
    assert r.status_code == 200, r.text
    assert r.json()["retries_used"] == 0
    assert call_count["n"] == 1


def test_agent_chat_rate_limited(
    agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch
):
    agent_client_app.app.state.agent_cfg.rate_limit_per_minute = 2

    def fake_complete(self, **kw):
        return _completion_with_tool(_ice_wave_args())

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r1 = agent_client_app.post("/agent/chat", json={"message": "a"})
    sid = r1.json()["session_id"]
    agent_client_app.post("/agent/chat", json={"message": "b", "session_id": sid})
    r3 = agent_client_app.post("/agent/chat", json={"message": "c", "session_id": sid})
    assert r3.status_code == 429


def test_agent_config_endpoint_exposes_settings(agent_client_app: TestClient):
    r = agent_client_app.get("/agent/config")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "openrouter"
    assert body["model"]
    assert body["history_max_messages"] >= 2
    assert body["api_key_env"] == "OPENROUTER_API_KEY"
    assert body["retry_on_tool_error"] >= 0


def test_agent_sessions_get_404_for_unknown(agent_client_app: TestClient):
    r = agent_client_app.get("/agent/sessions/nope")
    assert r.status_code == 404


def test_agent_sessions_delete(agent_client_app: TestClient, monkeypatch: pytest.MonkeyPatch):
    def fake_complete(self, **kw):
        return _completion_with_tool(_ice_wave_args())

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post("/agent/chat", json={"message": "hi"})
    sid = r.json()["session_id"]
    r2 = agent_client_app.delete(f"/agent/sessions/{sid}")
    assert r2.status_code == 200
    r3 = agent_client_app.get(f"/agent/sessions/{sid}")
    assert r3.status_code == 404


def test_chat_html_served(agent_client_app: TestClient):
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
            tool_calls=[{"id": "call_X", "name": "do_something_else", "arguments": {}}],
            raw_message=raw,
            finish_reason="tool_calls",
            model="mock",
        )

    monkeypatch.setattr(AgentClient, "complete", fake_complete)
    r = agent_client_app.post("/agent/chat", json={"message": "x"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool_call"] is None


def test_config_includes_agent_block(agent_client_app: TestClient):
    r = agent_client_app.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert "agent" in body
    assert body["agent"]["provider"] == "openrouter"
    assert body["agent"]["model"]
