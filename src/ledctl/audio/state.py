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
    range of the room. Modulators consume the `_norm` values; the level meter
    UI keeps showing raw so the user can see the actual mic input.
    """

    samplerate: int = 48000
    blocksize: int = 128
    fft_window: int = 512
    channels: int = 1
    device_name: str = ""
    enabled: bool = False
    error: str = ""
    block_count: int = 0
    rms: float = 0.0          # raw RMS over the current FFT window, ~[0, 1]
    peak: float = 0.0         # raw max |sample| over the current FFT window
    low: float = 0.0          # raw low-band FFT energy
    mid: float = 0.0          # raw mid-band FFT energy
    high: float = 0.0         # raw high-band FFT energy
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
