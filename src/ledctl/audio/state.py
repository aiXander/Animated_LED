from dataclasses import dataclass


@dataclass
class AudioState:
    """Shared, mutable audio analysis snapshot.

    Written by the PortAudio callback thread (one writer), read by the asyncio
    render loop and HTTP handlers (many readers). All fields are scalar floats
    or ints, so torn reads at worst cause a one-frame visual blip — not worth
    a lock on the render hot path.

    The raw `rms`/`peak`/`low`/`mid`/`high` fields are smoothed mic levels in
    their natural [0, 1]-ish scale (depends on input device gain). The `*_norm`
    counterparts are the same values divided by a rolling-window ceiling
    estimate, so they always span ~[0, 1] across the recent dynamic range
    of the room. Modulators consume the `_norm` values; the level meter UI
    keeps showing raw so the user can see the actual mic input.
    """

    samplerate: int = 48000
    blocksize: int = 512
    channels: int = 1
    device_name: str = ""
    enabled: bool = False
    error: str = ""
    block_count: int = 0
    rms: float = 0.0          # smoothed RMS in [0, 1]ish
    peak: float = 0.0         # decaying peak hold in [0, 1]
    low: float = 0.0          # smoothed low-band energy
    mid: float = 0.0          # smoothed mid-band energy
    high: float = 0.0         # smoothed high-band energy
    rms_norm: float = 0.0     # rms / rolling-window ceiling, in [0, 1]
    peak_norm: float = 0.0
    low_norm: float = 0.0
    mid_norm: float = 0.0
    high_norm: float = 0.0

    def reset_levels(self) -> None:
        self.rms = 0.0
        self.peak = 0.0
        self.low = 0.0
        self.mid = 0.0
        self.high = 0.0
        self.rms_norm = 0.0
        self.peak_norm = 0.0
        self.low_norm = 0.0
        self.mid_norm = 0.0
        self.high_norm = 0.0
        self.block_count = 0
