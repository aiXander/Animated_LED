"""HTTP-surface tests for the external-audio bridge integration.

The OSC listener is exercised in tests/test_audio_bridge.py — here we just
prove the FastAPI app stitches the bridge into /state, /audio/state, and
/audio/ui correctly, and that boot still works when the bridge is disabled.
"""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ledctl.api.server import create_app
from ledctl.audio.bridge import AudioBridge, AudioServerSupervisor
from ledctl.config import load_config
from tests.test_api import DEV, PRESETS


def _load_dev_audio_disabled(tmp_path: Path) -> tuple[Path, "load_config"]:
    """Materialise a copy of config.dev.yaml with audio_server.enabled = false
    so create_app() runs without trying to spawn or bind anything."""
    base = yaml.safe_load(DEV.read_text())
    base.setdefault("audio_server", {})
    base["audio_server"]["enabled"] = False
    base["audio_server"]["autostart"] = False
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(base))
    return p, load_config(p)


@pytest.fixture
def disabled_bridge_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Client with audio_server.enabled = false. The engine still boots and
    the API still serves /audio/state — the payload just shows 'disabled'."""
    cfg_path, cfg = _load_dev_audio_disabled(tmp_path)
    app = create_app(cfg, presets_dir=PRESETS, config_path=cfg_path)
    with TestClient(app) as c:
        c.app.state._cfg_path = cfg_path
        yield c


@pytest.fixture
def bridge_no_subprocess_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Client where the audio bridge is created but the supervisor's spawn is
    a no-op. Lets us drive AudioState directly and assert /audio/state echoes
    it back without needing a real audio-server on the host."""

    def _noop_start(self: AudioServerSupervisor) -> None:
        # Mark as 'didn't start' but no error — caller asserts the soft path.
        self._proc = None

    monkeypatch.setattr(AudioServerSupervisor, "start", _noop_start)
    cfg = load_config(DEV)
    app = create_app(cfg, presets_dir=PRESETS)
    with TestClient(app) as c:
        yield c


def test_audio_state_payload_when_disabled(disabled_bridge_client: TestClient):
    r = disabled_bridge_client.get("/audio/state")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["connected"] is False
    assert body["low"] == 0.0
    # ui_url should still come from config so the UI can offer the link if
    # the operator wants to flip the server back on.
    assert "ui_url" in body
    assert body["ui_url"].startswith("http://")


def test_audio_state_endpoint_includes_bridge_fields(bridge_no_subprocess_client: TestClient):
    r = bridge_no_subprocess_client.get("/audio/state")
    assert r.status_code == 200
    body = r.json()
    for key in ("enabled", "connected", "low", "mid", "high", "device", "ui_url", "bands"):
        assert key in body
    bands = body["bands"]
    assert set(bands.keys()) == {"low", "mid", "high"}


def test_audio_state_reflects_synthetic_lmh_packet(bridge_no_subprocess_client: TestClient):
    """Writing into AudioState directly is what the OSC listener does in prod.
    The HTTP payload must mirror the latest scalars without lag."""
    bridge: AudioBridge = bridge_no_subprocess_client.app.state.audio_bridge
    bridge.state.mark_packet()
    bridge.state.low = 0.4
    bridge.state.mid = 0.6
    bridge.state.high = 0.8
    bridge.state.device_name = "fake-mic"
    body = bridge_no_subprocess_client.get("/audio/state").json()
    assert body["connected"] is True
    assert body["low"] == 0.4
    assert body["mid"] == 0.6
    assert body["high"] == 0.8
    assert body["device"] == "fake-mic"


def test_audio_ui_endpoint_returns_configured_url(disabled_bridge_client: TestClient):
    r = disabled_bridge_client.get("/audio/ui")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["ui_url"].startswith("http://")


def test_state_includes_audio_block(bridge_no_subprocess_client: TestClient):
    r = bridge_no_subprocess_client.get("/state")
    assert r.status_code == 200
    body = r.json()
    assert "audio" in body
    assert "low" in body["audio"]


def test_legacy_audio_yaml_block_is_silently_dropped(tmp_path: Path):
    """An older config.yaml shipping the deprecated `audio:` block must still
    load. The before-validator on AppConfig drops the legacy key so users
    don't have to edit YAMLs by hand after upgrading."""
    base = yaml.safe_load(DEV.read_text())
    base["audio"] = {"enabled": True, "device": "BlackHole"}  # deprecated block
    p = tmp_path / "legacy.yaml"
    p.write_text(yaml.safe_dump(base))
    cfg = load_config(p)
    assert not hasattr(cfg, "audio")
    assert cfg.audio_server.enabled is True


def test_config_yaml_includes_audio_server_block(bridge_no_subprocess_client: TestClient):
    r = bridge_no_subprocess_client.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert "audio_server" in body
    assert body["audio_server"]["enabled"] is True
    assert body["audio_server"]["osc_listen_port"] == 9000
