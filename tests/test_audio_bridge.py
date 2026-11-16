"""The audio bridge against real OSC packets on a real local UDP socket."""

from __future__ import annotations

import time

from pythonosc.osc_bundle_builder import IMMEDIATELY, OscBundleBuilder
from pythonosc.osc_message_builder import OscMessageBuilder
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

        # The less common wire shapes: a bundle, and a message with a string.
        bundle = OscBundleBuilder(IMMEDIATELY)
        for address, value in (("/audio/bpm", 128.0), ("/audio/level", 0.5)):
            msg = OscMessageBuilder(address)
            msg.add_arg(value)
            bundle.add_content(msg.build())
        client.send(bundle.build())
        client.send_message(
            "/audio/meta", [48000, 256, 129, 20.0, 200.0, 200.0, 2e3, 2e3, 16e3, "USB mic"],
        )
        assert _wait_for(lambda: state.device_name == "USB mic")
        assert state.bpm == 128.0 and state.level == 0.5 and state.samplerate == 48000
    finally:
        listener.stop()
