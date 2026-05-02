from dataclasses import dataclass


@dataclass
class AudioState:
    """Shared audio analysis snapshot.

    Written by a single audio thread (the source's callback runs on PortAudio's
    RT thread for SoundDeviceSource), read by the asyncio render loop and HTTP
    handlers. All fields are scalar floats or ints, so torn reads at worst
    cause a one-frame visual blip — not worth a lock on the render hot path.

    Fields are *raw and instantaneous*: each one reflects the most recent FFT
    window straight out of the analyser, with no EMA smoothing and no peak
    hold. The asymmetric attack/release smoothing happens per-binding in
    `effects/modulator.Envelope`, so each LED control can pick its own time
    constants without inheriting a shared lag.

    `*_norm` counterparts are the raw values divided by a rolling-window
    ceiling estimate, so they always span ~[0, 1] across the recent dynamic
    range of the room. Modulators consume the `_norm` values, and the level
    meter UI shows them too — the raw fields stay close to zero on quiet
    inputs and made the bars unreadable.

    Only the three frequency bands are tracked: full-band RMS was too coarse
    for visual modulation (a wash of "loudness" with no musical structure)
    and full-band peak was too noisy (a single sample spike in any band hits
    the bar). Pick low/mid/high to track a specific musical element.
    """

    samplerate: int = 48000
    blocksize: int = 128
    fft_window: int = 512
    channels: int = 1
    device_name: str = ""
    enabled: bool = False
    error: str = ""
    block_count: int = 0
    low: float = 0.0          # raw low-band FFT energy
    mid: float = 0.0          # raw mid-band FFT energy
    high: float = 0.0         # raw high-band FFT energy
    low_norm: float = 0.0     # low / rolling-window ceiling, in [0, 1]
    mid_norm: float = 0.0
    high_norm: float = 0.0

    def reset_levels(self) -> None:
        self.low = 0.0
        self.mid = 0.0
        self.high = 0.0
        self.low_norm = 0.0
        self.mid_norm = 0.0
        self.high_norm = 0.0
        self.block_count = 0
