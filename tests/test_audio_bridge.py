"""The audio bridge against real OSC packets on a real local UDP socket."""

from __future__ import annotations

import time

from pythonosc.udp_client import SimpleUDPClient

from ledctl.audio import AudioState, OscFeatureListener


def _wait_for(cond, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_onset_and_level_packets_reach_audio_state():
    state = AudioState()
    listener = OscFeatureListener(state, host="127.0.0.1", port=0)
    kicks: list[int] = []
    listener.kick_callback = lambda: kicks.append(1)
    listener.start()
    try:
        client = SimpleUDPClient("127.0.0.1", listener.port)
        client.send_message("/audio/lmh", [0.5, 0.25, 0.125])
        client.send_message("/audio/level", 0.75)
        assert _wait_for(lambda: state.level is not None)
        assert state.level == 0.75
        assert state.low == 0.5 and state.connected
        # Band packets carry no beat and must not wake the render loop.
        assert state.beat_count == 0 and kicks == []

        client.send_message("/audio/onset/low", 1.0)
        assert _wait_for(lambda: state.beat_count == 1)
        assert kicks == [1]
    finally:
        listener.stop()
