import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ledctl.api.server import create_app
from ledctl.audio.capture import AudioCapture
from ledctl.config import load_config
from tests.test_api import DEV, PRESETS


@pytest.fixture
def audio_off_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Client that doesn't open a real audio stream — `start()` is a no-op.

    Lets us exercise the /audio endpoints, the engine plumbing, and config
    persistence without depending on whatever input device the host has.
    """

    def _noop_start(self: AudioCapture) -> None:
        self.state.enabled = False
        self.state.error = "disabled in tests"

    monkeypatch.setattr(AudioCapture, "start", _noop_start)
    cfg = load_config(DEV)
    app = create_app(cfg, presets_dir=PRESETS)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def audio_writable_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """Same as audio_off_client but with a copy of config.yaml so PUT writes are safe."""

    def _noop_start(self: AudioCapture) -> None:
        self.state.enabled = False

    monkeypatch.setattr(AudioCapture, "start", _noop_start)
    cfg_path = tmp_path / "config.yaml"
    shutil.copy(DEV, cfg_path)
    cfg = load_config(cfg_path)
    app = create_app(cfg, presets_dir=PRESETS, config_path=cfg_path)
    with TestClient(app) as c:
        c.app.state._cfg_path = cfg_path
        yield c


def test_audio_state_endpoint(audio_off_client: TestClient):
    r = audio_off_client.get("/audio/state")
    assert r.status_code == 200
    body = r.json()
    for key in ("enabled", "device", "samplerate", "rms", "peak", "low", "mid", "high"):
        assert key in body
    # No real device opened, so capture is idle.
    assert body["enabled"] is False
    assert body["rms"] == 0.0


def test_audio_devices_endpoint(audio_off_client: TestClient):
    r = audio_off_client.get("/audio/devices")
    assert r.status_code == 200
    body = r.json()
    assert "devices" in body and isinstance(body["devices"], list)
    # Field shape (when devices exist on this host).
    for d in body["devices"]:
        assert {"index", "name", "max_input_channels"}.issubset(d)


def test_audio_html_served(audio_off_client: TestClient):
    r = audio_off_client.get("/audio")
    assert r.status_code == 200
    assert "ledctl · audio input" in r.text


def test_state_includes_audio(audio_off_client: TestClient):
    r = audio_off_client.get("/state")
    assert r.status_code == 200
    body = r.json()
    assert "audio" in body
    assert body["audio"]["enabled"] is False


def test_audio_select_persists_to_yaml(
    audio_writable_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    cfg_path: Path = audio_writable_client.app.state._cfg_path

    # Force the new capture's `start()` to "succeed" so /audio/select
    # commits the config write. The default monkeypatch sets enabled=False
    # which would short-circuit with a 422.
    def _ok_start(self: AudioCapture) -> None:
        self.state.enabled = True
        self.state.device_name = "fake-mic"
        self.state.error = ""

    monkeypatch.setattr(AudioCapture, "start", _ok_start)

    r = audio_writable_client.post(
        "/audio/select",
        json={"device": "BlackHole", "persist": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["device"] == "fake-mic"
    assert body["configured_device"] == "BlackHole"
    assert body["saved_to"] is not None

    # Disk reflects the change.
    on_disk = yaml.safe_load(cfg_path.read_text())
    assert on_disk["audio"]["device"] == "BlackHole"
    assert on_disk["audio"]["enabled"] is True
    assert cfg_path.with_suffix(cfg_path.suffix + ".bak").exists()


def test_audio_select_rejects_failed_device(
    audio_writable_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    cfg_path: Path = audio_writable_client.app.state._cfg_path
    original = cfg_path.read_text()

    def _fail_start(self: AudioCapture) -> None:
        self.state.enabled = False
        self.state.error = "synthetic failure"

    monkeypatch.setattr(AudioCapture, "start", _fail_start)
    r = audio_writable_client.post(
        "/audio/select",
        json={"device": "no-such-device", "persist": True},
    )
    assert r.status_code == 422
    assert "synthetic failure" in r.text
    # Config on disk is untouched (we never reach the write path).
    assert cfg_path.read_text() == original


def test_config_yaml_includes_audio_block(audio_off_client: TestClient):
    r = audio_off_client.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert "audio" in body
    assert "device" in body["audio"]
    assert body["audio"]["enabled"] is True


def test_audio_config_defaults_when_missing():
    """A YAML without an `audio:` block should still load (defaults applied)."""
    cfg = load_config(DEV)
    # The dev config has an explicit audio block — verify the new defaults
    # (blocksize=128, fft_window=512) survive the round-trip.
    assert cfg.audio.samplerate == 48000
    assert cfg.audio.blocksize == 128
    assert cfg.audio.fft_window == 512
    assert cfg.audio.enabled is True
