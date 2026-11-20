"""MIDI control: the `/ws/midi` wire contract (shared with WZRD's forwarder),
the saved mapping, and the master effects the faders drive."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from ledctl.api.server import create_app
from ledctl.config import load_config
from ledctl.masters import MasterControls
from ledctl.surface.runtime import Runtime
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


def _app(tmp_path):
    # The library one level down, so `midi.yaml` lands in this test's folder.
    return create_app(load_config(DEV), effects_dir=tmp_path / "effects")


def _wait(cond, what: str) -> None:
    deadline = time.monotonic() + 5.0
    while not cond():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def _lights(ws, want: dict[int, int]) -> None:
    """Read light frames until every note in `want` shows its value."""
    seen: dict[int, int] = {}
    while any(seen.get(n) != v for n, v in want.items()):
        status, note, value = ws.receive_bytes()
        assert status == 0x90
        seen[note] = value


def test_controller_plays_the_leds_over_the_websocket(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        info = client.get("/midi").json()
        pad = {t: int(hw[5:]) for hw, t in info["bindings"].items() if t.startswith("effect:")}
        live_pad, other_pad = pad["effect:pulse_mono"], pad["effect:audio_radial"]
        live_name = lambda: client.get("/state").json()["live"]["layers"][0]["name"]  # noqa: E731

        with client.websocket_connect("/ws/midi") as ws:
            # On connect: the live effect's pad green, the others yellow.
            _lights(ws, {live_pad: 1, other_pad: 5})

            # A pad press crossfades LIVE to its effect and the lights follow.
            ws.send_bytes(bytes((0x90, other_pad, 127)))
            ws.send_bytes(bytes((0x80, other_pad, 0)))
            _wait(lambda: live_name() == "audio_radial", "the pad's effect on LIVE")
            _lights(ws, {other_pad: 1, live_pad: 5})

            # Fader 1 is brightness (1.0 = the middle). A fader sitting far
            # from the value does nothing; once it reaches it, it takes over.
            masters = lambda: client.get("/masters").json()  # noqa: E731
            ws.send_bytes(bytes((0xB0, 48, 10)))
            ws.send_bytes(bytes((0xB0, 48, 20)))
            ws.send_bytes(bytes((0x90, 82, 127)))            # blackout: a marker to wait on
            _lights(ws, {82: 1})
            assert masters()["brightness"] == 1.0
            ws.send_bytes(bytes((0xB0, 48, 64)))
            ws.send_bytes(bytes((0xB0, 48, 127)))
            ws.send_bytes(bytes((0x90, 82, 127)))            # blackout off again
            _lights(ws, {82: 0})
            assert masters()["brightness"] == 2.0
            assert client.get("/state").json()["blackout"] is False

            # "sync" sends every light again (WZRD asks on entering its LEDs mode).
            ws.send_text("sync")
            _lights(ws, {other_pad: 1, live_pad: 5})


def test_mapping_edits_are_validated_and_survive_a_restart(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        ok = client.put("/midi/bindings", json={"bindings": {
            "note:0": "effect:pulse_mono", "cc:48": "master.strobe", "note:82": None}})
        assert ok.status_code == 200
        for bad in (
            {"cc:56": "master.speed"},        # fader 9 belongs to WZRD
            {"note:1": "master.speed"},       # a fader target on a pad
            {"note:1": "effect:not_there"},
            {"note:1": "nonsense"},
        ):
            assert client.put("/midi/bindings", json={"bindings": bad}).status_code == 422
    with TestClient(_app(tmp_path)) as client:
        got = client.get("/midi").json()
        assert got["custom"] is True
        assert got["bindings"]["note:0"] == "effect:pulse_mono"
        assert got["bindings"]["cc:48"] == "master.strobe"
        assert "note:82" not in got["bindings"] and "note:1" not in got["bindings"]
        assert client.post("/midi/reset").json()["bindings"]["cc:48"] == "master.brightness"


def test_master_effects_are_off_at_zero_and_never_light_a_blackout():
    topo = Topology.from_config(load_config(DEV))
    frame = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    frame[::3] = (0.5, 0.2, 0.1)

    def run(rt: Runtime, frames: int = 40) -> list[np.ndarray]:
        out = []
        for i in range(frames):
            buf = frame.copy()
            rt._apply_master_output(buf, i / 60, fx=rt._fx_live, dt=1 / 60,
                                    beat=1.0 if i % 6 == 0 else 0.0)
            out.append(buf)
        return out

    off = run(Runtime(topo, MasterControls()))
    assert all(np.array_equal(buf, frame) for buf in off)       # 0 = exactly as before

    for fx in ("sparkle", "glitch", "strobe", "trails"):
        rt = Runtime(topo, MasterControls(**{fx: 1.0}))
        out = np.stack(run(rt))
        assert out.min() >= 0.0 and out.max() <= 1.0 and np.isfinite(out).all()
        if fx != "trails":       # a still picture has nothing to trail
            assert any(not np.array_equal(buf, frame) for buf in out), fx
        rt.blackout = True
        dark = np.zeros_like(frame)
        rt._apply_master_output(dark, 1.0, fx=rt._fx_live, dt=1 / 60, beat=1.0)
        assert not dark.any(), fx
