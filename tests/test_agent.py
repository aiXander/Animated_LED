"""Mock-LLM agent test: verify the chat → write_effect → preview pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from ledctl.agent.client import CompletionResult
from ledctl.api.server import create_app
from ledctl.config import load_config

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "config" / "config.dev.yaml"


@pytest.fixture
def client(tmp_path):
    cfg = load_config(DEV)
    app = create_app(cfg, effects_dir=tmp_path)
    with TestClient(app) as c:
        yield c


def _fake_completion(*, code: str, params: list[dict[str, Any]],
                     name: str = "agent_effect") -> CompletionResult:
    """Build a CompletionResult that the route's tool dispatch will consume."""
    import json
    raw_args = {"name": name, "summary": "from mock LLM", "code": code, "params": params}
    return CompletionResult(
        text="ok, here's your effect",
        tool_calls=[{
            "id": "call_1",
            "name": "write_effect",
            "arguments": raw_args,
        }],
        raw_message={
            "role": "assistant",
            "content": "ok, here's your effect",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "write_effect",
                    "arguments": json.dumps(raw_args),
                },
            }],
        },
        finish_reason="tool_calls",
        model="mock-model",
        usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    )


def test_agent_chat_writes_to_preview(client: TestClient):
    fake = _fake_completion(
        code=(
            "class AgentSolid(Effect):\n"
            "    def render(self, ctx):\n"
            "        col = hex_to_rgb(ctx.params.tint)\n"
            "        self.out[:] = col[None, :]\n"
            "        return self.out\n"
        ),
        params=[{"key": "tint", "control": "color", "default": "#88ff88"}],
        name="agent_solid",
    )
    with patch("ledctl.agent.client.AgentClient.complete", return_value=fake):
        # Bypass the missing-key check by stubbing the client at the route level.
        r = client.post("/agent/chat", json={"message": "make it green"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool_call"]["name"] == "write_effect"
    assert body["tool_result"]["ok"] is True
    # Preview swapped, live untouched.
    state = client.get("/state").json()
    assert state["preview"]["layers"][0]["name"] == "agent_solid"
    assert state["live"]["layers"][0]["name"] == "pulse_mono"


def test_agent_chat_surfaces_compile_error(client: TestClient):
    fake = _fake_completion(
        code="import os\nclass X(Effect):\n    def render(self, ctx): return self.out\n",
        params=[],
        name="bad_effect",
    )
    with patch("ledctl.agent.client.AgentClient.complete", return_value=fake):
        r = client.post("/agent/chat", json={"message": "do a bad thing"})
    assert r.status_code == 200
    body = r.json()
    assert body["tool_result"]["ok"] is False
    assert "compile_failed" in body["tool_result"]["error"]
    # Preview unchanged.
    state = client.get("/state").json()
    assert state["preview"]["layers"][0]["name"] == "pulse_mono"


def test_agent_session_transcript(client: TestClient):
    fake = _fake_completion(
        code="class A(Effect):\n    def render(self, ctx): return self.out\n",
        params=[],
        name="a",
    )
    with patch("ledctl.agent.client.AgentClient.complete", return_value=fake):
        r = client.post("/agent/chat", json={"message": "hi"})
    sid = r.json()["session_id"]
    r2 = client.get(f"/agent/sessions/{sid}")
    assert r2.status_code == 200
    body = r2.json()
    assert len(body["turns"]) == 1
    assert body["turns"][0]["user"] == "hi"
