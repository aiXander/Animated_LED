"""Unit tests for the OSC bridge to the external audio-feature server.

The bridge has three loosely-coupled pieces, tested independently here:

  - `OscFeatureListener` — bind a UDP socket and translate `/audio/lmh` +
    `/audio/meta` into AudioState scalars; flip `connected` to False after
    `stale_after_s` of silence.
  - `AudioServerSupervisor` — spawn the audio-server subprocess; degrade
    cleanly (warning, no crash) if the binary is missing.
  - `AudioBridge` — owns the listener + optional supervisor; one
    `start()` / `stop()` pair the engine wires up.

We send real OSC packets through `python_osc.udp_client.SimpleUDPClient` to a
listener on a free localhost port. That keeps the contract honest — the
listener has to deal with the same wire format the audio-server emits.
"""

from __future__ import annotations

import socket
import time

import pytest

from ledctl.audio.bridge import (
    AudioBridge,
    AudioServerSupervisor,
    OscFeatureListener,
)
from ledctl.audio.state import AudioState


def _free_udp_port() -> int:
    """Bind on port 0, read back the assigned port, close. Tiny race window
    between close and the listener's bind, but it's fine in practice."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _wait_for(predicate, timeout_s: float = 2.0, period_s: float = 0.01) -> bool:
    """Poll until `predicate()` is truthy or `timeout_s` elapses."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(period_s)
    return False


@pytest.fixture
def osc_client():
    """python-osc client; skip the suite if the package isn't installed."""
    pythonosc = pytest.importorskip("pythonosc")
    from pythonosc.udp_client import SimpleUDPClient
    yield SimpleUDPClient, pythonosc


# ---- OSC listener ----


def test_listener_translates_lmh(osc_client):
    SimpleUDPClient, _ = osc_client
    state = AudioState()
    port = _free_udp_port()
    listener = OscFeatureListener(state, host="127.0.0.1", port=port, stale_after_s=5.0)
    listener.start()
    try:
        c = SimpleUDPClient("127.0.0.1", port)
        c.send_message("/audio/lmh", [0.25, 0.5, 0.75])
        assert _wait_for(lambda: state.low > 0)
        assert state.low == pytest.approx(0.25, abs=1e-5)
        assert state.mid == pytest.approx(0.5, abs=1e-5)
        assert state.high == pytest.approx(0.75, abs=1e-5)
        assert state.connected is True
    finally:
        listener.stop()


def test_listener_parses_meta(osc_client):
    SimpleUDPClient, _ = osc_client
    state = AudioState()
    port = _free_udp_port()
    listener = OscFeatureListener(state, host="127.0.0.1", port=port, stale_after_s=5.0)
    listener.start()
    try:
        c = SimpleUDPClient("127.0.0.1", port)
        c.send_message(
            "/audio/meta",
            [48000, 256, 128, 30.0, 250.0, 250.0, 4000.0, 4000.0, 16000.0],
        )
        assert _wait_for(lambda: state.samplerate == 48000)
        assert state.blocksize == 256
        assert state.n_fft_bins == 128
        assert state.low_lo == pytest.approx(30.0, abs=1e-5)
        assert state.high_hi == pytest.approx(16000.0, abs=1e-5)
    finally:
        listener.stop()


def test_listener_picks_up_device_name_in_meta(osc_client):
    """Newer audio-server builds may append a device-name string to /audio/meta;
    if present, surface it in AudioState.device_name without breaking the
    minimal 9-arg contract."""
    SimpleUDPClient, _ = osc_client
    state = AudioState()
    port = _free_udp_port()
    listener = OscFeatureListener(state, host="127.0.0.1", port=port, stale_after_s=5.0)
    listener.start()
    try:
        c = SimpleUDPClient("127.0.0.1", port)
        c.send_message(
            "/audio/meta",
            [48000, 256, 128, 30.0, 250.0, 250.0, 4000.0, 4000.0, 16000.0, "BlackHole"],
        )
        assert _wait_for(lambda: state.device_name == "BlackHole")
    finally:
        listener.stop()


def test_listener_watchdog_marks_stale(osc_client):
    """After `stale_after_s` of silence, the watchdog flips connected=False so
    the engine can fall back to non-reactive output."""
    SimpleUDPClient, _ = osc_client
    state = AudioState()
    port = _free_udp_port()
    listener = OscFeatureListener(
        state, host="127.0.0.1", port=port, stale_after_s=0.15
    )
    listener.start()
    try:
        c = SimpleUDPClient("127.0.0.1", port)
        c.send_message("/audio/lmh", [0.1, 0.2, 0.3])
        assert _wait_for(lambda: state.connected)
        assert _wait_for(lambda: not state.connected, timeout_s=1.0)
        assert "no /audio/lmh" in state.error
    finally:
        listener.stop()


def test_listener_bind_failure_is_soft(osc_client, caplog):
    """If two listeners try to bind the same port the second one logs and
    leaves AudioState alone — no exception escapes start()."""
    SimpleUDPClient, _ = osc_client
    port = _free_udp_port()
    a = OscFeatureListener(AudioState(), host="127.0.0.1", port=port)
    a.start()
    try:
        state2 = AudioState()
        b = OscFeatureListener(state2, host="127.0.0.1", port=port)
        b.start()  # must not raise
        assert b.running is False
        assert "osc bind failed" in state2.error or state2.error.startswith(
            "osc bind failed"
        )
    finally:
        a.stop()


def test_listener_ignores_short_lmh_packets(osc_client):
    """Truncated /audio/lmh (e.g. partial corruption) shouldn't poison the state."""
    SimpleUDPClient, _ = osc_client
    state = AudioState()
    port = _free_udp_port()
    listener = OscFeatureListener(state, host="127.0.0.1", port=port, stale_after_s=5.0)
    listener.start()
    try:
        c = SimpleUDPClient("127.0.0.1", port)
        c.send_message("/audio/lmh", [0.42])  # missing mid/high
        time.sleep(0.05)
        assert state.low == 0.0  # never written; short packets dropped silently
        c.send_message("/audio/lmh", [0.1, 0.2, 0.3])
        assert _wait_for(lambda: state.low == pytest.approx(0.1, abs=1e-5))
    finally:
        listener.stop()


# ---- subprocess supervisor ----


def test_supervisor_logs_warning_when_binary_missing(caplog):
    """The supervisor must not raise when the audio-server binary is absent —
    we want the LED render loop to keep running with audio disabled."""
    import logging

    sup = AudioServerSupervisor(command=["__definitely_not_a_real_binary__"])
    with caplog.at_level(logging.WARNING):
        sup.start()
    assert sup.running is False
    assert "not found" in sup.error


def test_supervisor_handles_no_command():
    """An empty command list is silently a no-op — useful for tests where we
    don't want to spawn anything but still want to assert lifecycle plumbing."""
    sup = AudioServerSupervisor(command=[])
    sup.start()
    assert sup.running is False
    assert "no audio-server command" in sup.error


def test_supervisor_resolves_python_token_to_sys_executable():
    """A bare `python` in the command rewrites to sys.executable so a config
    like `["python", "-m", "server.main"]` works in any venv layout."""
    import sys

    resolved = AudioServerSupervisor._resolve_command(["python", "-m", "server.main"])
    assert resolved is not None
    assert resolved[0] == sys.executable
    assert resolved[1:] == ["-m", "server.main"]


def test_supervisor_can_spawn_and_stop_a_real_subprocess():
    """End-to-end: spawn a long-running shell command, confirm `running` flips,
    stop it, confirm clean exit."""
    sup = AudioServerSupervisor(command=["sh", "-c", "while true; do sleep 0.1; done"])
    sup.start()
    try:
        assert sup.running is True
    finally:
        sup.stop()
    assert sup.running is False


# ---- AudioBridge end-to-end ----


def test_bridge_round_trip_listener_only(osc_client):
    """Bridge with listener only (no supervisor) — start/stop and OSC passthrough."""
    SimpleUDPClient, _ = osc_client
    state = AudioState()
    port = _free_udp_port()
    listener = OscFeatureListener(state, host="127.0.0.1", port=port, stale_after_s=5.0)
    bridge = AudioBridge(listener=listener, supervisor=None, ui_url="http://x:1")
    bridge.start()
    try:
        c = SimpleUDPClient("127.0.0.1", port)
        c.send_message("/audio/lmh", [0.5, 0.5, 0.5])
        assert _wait_for(lambda: bridge.state.low > 0)
        assert bridge.state.connected is True
        assert bridge.ui_url == "http://x:1"
    finally:
        bridge.stop()
    assert bridge.state.connected is False
