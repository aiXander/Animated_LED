"""Shared snapshot of the external audio-feature server's state.

Written by the OSC listener thread (one writer), read by the asyncio render loop
and HTTP handlers (many lock-free readers). Scalar fields, so torn reads at
worst cause a one-frame visual blip — not worth a lock on the render hot path.

`low` / `mid` / `high` are the post-autoscale band energies in ~[0, 1] that the
external server publishes on `/audio/lmh`. The companion meta values
(samplerate, blocksize, device name, band cutoffs) come from `/audio/meta`.

`connected` flips True the first time we receive an OSC packet, and the
listener watchdog flips it False if no packet arrives for `stale_after_s`
seconds — that's the signal the LED server uses to fall back to "no
reactivity" without crashing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic


@dataclass
class AudioState:
    samplerate: int = 0
    blocksize: int = 0
    n_fft_bins: int = 0
    device_name: str = ""
    low_lo: float = 0.0
    low_hi: float = 0.0
    mid_lo: float = 0.0
    mid_hi: float = 0.0
    high_lo: float = 0.0
    high_hi: float = 0.0
    low: float = 0.0
    mid: float = 0.0
    high: float = 0.0
    # Onset / tempo metadata published by the audio server's upcoming
    # `/audio/beat` (binary rising-edge trigger; only sent on onset blocks,
    # never `0`) and `/audio/bpm` (continuous tempo) addresses. While those
    # addresses haven't shipped yet, `beat_count` stays 0 and `bpm` stays
    # None — the surface primitives that read them (`audio_beat`,
    # `audio_bpm`) soft-fail to 0 / fallback. As soon as the server starts
    # publishing, the LED side picks them up automatically.
    bpm: float | None = None
    beat_count: int = 0
    last_beat_at: float = 0.0
    connected: bool = False
    last_packet_at: float = 0.0
    error: str = ""

    @property
    def enabled(self) -> bool:
        """Compatibility alias used by the system prompt and UI status pill."""
        return self.connected

    def reset_levels(self) -> None:
        self.low = 0.0
        self.mid = 0.0
        self.high = 0.0

    def mark_packet(self) -> None:
        self.connected = True
        self.last_packet_at = monotonic()
        self.error = ""


@dataclass
class _LMH:
    """Tiny helper for tests that want to push synthetic packets without OSC."""
    low: float = 0.0
    mid: float = 0.0
    high: float = 0.0
    extras: dict = field(default_factory=dict)
