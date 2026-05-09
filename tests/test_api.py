"""REST endpoint smoke tests against TestClient.

The agent endpoint is exercised in test_agent.py; here we verify the layered
composition + persistence routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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


def test_state_endpoint(client: TestClient):
    r = client.get("/state")
    assert r.status_code == 200
    body = r.json()
    assert "live" in body and "preview" in body
    assert body["mode"] in ("live", "design")
    assert body["live"]["layers"][0]["name"] == "pulse_mono"


def test_topology_endpoint(client: TestClient):
    r = client.get("/topology")
    assert r.status_code == 200
    assert r.json()["pixel_count"] == 1800


def test_list_effects_includes_examples(client: TestClient):
    r = client.get("/effects")
    assert r.status_code == 200
    names = {e["name"] for e in r.json()["effects"]}
    assert "pulse_mono" in names
    assert "twin_comets_with_sparkles" in names


def test_load_preview_changes_preview_only(client: TestClient):
    r = client.post("/effects/twin_comets_with_sparkles/load_preview", json={})
    assert r.status_code == 200
    state = client.get("/state").json()
    assert state["preview"]["layers"][0]["name"] == "twin_comets_with_sparkles"
    assert state["live"]["layers"][0]["name"] == "pulse_mono"


def test_promote_swaps_live(client: TestClient):
    client.post("/effects/twin_comets_with_sparkles/load_preview", json={})
    r = client.post("/promote")
    assert r.status_code == 200
    state = client.get("/state").json()
    assert state["live"]["layers"][0]["name"] == "twin_comets_with_sparkles"


def test_pull_live_to_preview(client: TestClient):
    # Live = pulse_mono, preview = pulse_mono initially.
    client.post("/effects/twin_comets_with_sparkles/load_preview", json={})
    # Now preview has comets, live has pulse_mono.
    r = client.post("/pull_live_to_preview")
    assert r.status_code == 200
    state = client.get("/state").json()
    assert state["preview"]["layers"][0]["name"] == "pulse_mono"


def test_param_patch_updates_selected_preview_layer(client: TestClient):
    r = client.patch("/preview/params", json={"values": {"color": "#abcdef"}})
    assert r.status_code == 200
    body = r.json()
    assert body["values"]["color"] == "#abcdef"


def test_mode_toggle(client: TestClient):
    r = client.post("/mode", json={"mode": "design"})
    assert r.status_code == 200
    assert r.json()["mode"] == "design"


def test_blackout_resume(client: TestClient):
    r = client.post("/blackout")
    assert r.status_code == 200
    assert r.json()["blackout"] is True
    r = client.post("/resume")
    assert r.status_code == 200
    assert r.json()["blackout"] is False


def test_layer_meta_blend(client: TestClient):
    # Add a second layer to preview so we have something to patch.
    # Use load_preview with add_layer=True
    r = client.post("/effects/twin_comets_with_sparkles/load_preview",
                    json={"add_layer": True, "blend": "add", "opacity": 0.7})
    assert r.status_code == 200
    state = client.get("/state").json()
    assert len(state["preview"]["layers"]) == 2
    # Patch blend on the first layer.
    r = client.patch("/preview/layer/blend",
                     json={"index": 0, "blend": "screen", "opacity": 0.5})
    assert r.status_code == 200


def test_layer_remove(client: TestClient):
    client.post("/effects/twin_comets_with_sparkles/load_preview",
                json={"add_layer": True})
    state = client.get("/state").json()
    assert len(state["preview"]["layers"]) == 2
    r = client.post("/preview/layer/remove", json={"index": 1})
    assert r.status_code == 200
    state = client.get("/state").json()
    assert len(state["preview"]["layers"]) == 1


def test_masters_patch(client: TestClient):
    r = client.patch("/masters", json={"brightness": 0.5})
    assert r.status_code == 200
    assert abs(r.json()["brightness"] - 0.5) < 1e-6


def test_audio_state(client: TestClient):
    r = client.get("/audio/state")
    assert r.status_code == 200
    body = r.json()
    assert "connected" in body


def test_preview_save_round_trips_through_library(client: TestClient):
    """Operator hits 💾 save → /preview/save → effect reappears in /effects."""
    r = client.post(
        "/preview/save",
        json={"name": "operator_saved", "summary": "the band's intro look"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["saved"] == "operator_saved"

    listing = client.get("/effects").json()["effects"]
    assert any(e["name"] == "operator_saved" for e in listing)


def test_preview_save_rejects_bad_name(client: TestClient):
    r = client.post("/preview/save", json={"name": "Bad Name With Spaces"})
    assert r.status_code == 422


def test_preview_save_409_when_no_overwrite_and_exists(client: TestClient):
    client.post("/preview/save", json={"name": "first_save"})
    r = client.post("/preview/save",
                    json={"name": "first_save", "overwrite": False})
    assert r.status_code == 409


def test_load_preview_wipes_agent_history(client: TestClient):
    """Library pull replaces preview source — the LLM's rolling buffer must
    be cleared so it doesn't reference stale source on the next turn."""
    # Seed the rolling buffer manually (skip the LLM round-trip).
    sessions = client.app.state.agent_sessions
    sess = sessions.get_or_create(None)
    sess.append_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ])
    assert len(sess.messages) == 2

    r = client.post("/effects/twin_comets_with_sparkles/load_preview", json={})
    assert r.status_code == 200
    assert len(sess.messages) == 0
    # Operator-visible transcript is untouched (we only wipe the model buffer).
    # `turns` was empty in this test anyway, but assert it didn't blow up.
    assert sess.turns == []
