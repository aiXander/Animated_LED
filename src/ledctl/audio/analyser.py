"""Real-time audio analyser.

Drives an `AudioSource`, accumulates the most recent `fft_window` samples in a
ring buffer, and computes RMS / peak / band features per source block. Writes
*raw* (unsmoothed) values into a shared `AudioState` along with their
rolling-window-normalised counterparts. Temporal smoothing for visual output is
the modulator envelope's job — see `effects/modulator.py`. Keeping the analyser
raw means audio measurements stay as fast as the source delivers, and each
LED-control binding can pick its own attack/release without inheriting a
hidden, project-wide EMA.

Window vs. block size — the two are independent on purpose:
  - `source.blocksize` sets the hardware capture latency (PortAudio buffers one
    block before firing the callback; smaller block ⇒ faster transient response).
  - `fft_window` sets the FFT length, i.e. frequency resolution (`df = sr / N`).
    A wider window gives finer bass discrimination but its magnitude spectrum
    "averages over the whole window," so a transient ramps in over multiple
    updates instead of snapping to full energy on the frame it lands.
The default `blocksize=128, fft_window=512` is a sweet spot at 48 kHz: 2.67 ms
HW latency, 94 Hz bin resolution (fine for 3 broad bands), update every 2.67 ms.
"""

import logging
import threading
from typing import Any

import numpy as np

from .features import DEFAULT_BANDS, band_energies, peak, rms
from .normalizer import RollingNormalizer
from .source import AudioSource
from .state import AudioState

# Default rolling window for the per-feature auto-gain. 60 s is long enough
# that a single song section doesn't dominate (verses, breakdowns and drops
# all contribute), and short enough that moving the mic / changing the room
# resettles the dynamic range within a minute.
DEFAULT_NORMALIZE_WINDOW_S: float = 60.0

log = logging.getLogger(__name__)


class AudioAnalyser:
    """Wraps an `AudioSource` with feature extraction and rolling-window auto-gain.

    The source's callback runs on a private thread (PortAudio's RT thread for
    SoundDeviceSource). Inside the callback we update the FFT ring, compute
    raw features, write into the shared `AudioState`, and step the
    normalizers. All ops are vectorised numpy on small arrays — sub-millisecond.

    `AudioState` reads are lock-free from the asyncio render loop: the writer is
    a single thread writing scalars; torn reads at worst cause a one-frame
    visual blip, not worth a lock on the render hot path.
    """

    def __init__(
        self,
        source: AudioSource,
        fft_window: int = 512,
        bands: tuple[tuple[float, float], ...] = DEFAULT_BANDS,
        normalize_window_s: float = DEFAULT_NORMALIZE_WINDOW_S,
    ):
        info = source.info
        sr = int(info["samplerate"])
        bs = int(info["blocksize"])
        ch = int(info["channels"])
        if fft_window < bs:
            raise ValueError(
                f"fft_window ({fft_window}) must be >= source blocksize ({bs})"
            )
        self.source = source
        self.fft_window = int(fft_window)
        self.bands = bands
        self.normalize_window_s = float(normalize_window_s)
        self.state = AudioState(
            samplerate=sr,
            blocksize=bs,
            fft_window=self.fft_window,
            channels=ch,
        )
        # Ring buffer holding the most recent `fft_window` mono samples. Each
        # source block is shifted in at the tail; the analyser FFTs the whole
        # ring on every callback. In-place slice-assign shift is faster than
        # np.roll (which allocates a new array each call).
        self._ring = np.zeros(self.fft_window, dtype=np.float32)
        # One normalizer per feature. Update rate = block rate (samplerate /
        # blocksize); window length governs how much room history feeds the
        # 95th-percentile ceiling estimate.
        update_rate_hz = float(sr) / float(max(1, bs))
        self._normalizers: dict[str, RollingNormalizer] = {
            name: RollingNormalizer(
                window_s=self.normalize_window_s,
                update_rate_hz=update_rate_hz,
            )
            for name in ("rms", "peak", "low", "mid", "high")
        }
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.source.running

    def start(self) -> None:
        with self._lock:
            if self.source.running:
                return
            self.source.start(self._on_block)
            info = self.source.info
            self.state.device_name = str(info.get("device_name", ""))
            self.state.samplerate = int(info.get("samplerate", self.state.samplerate))
            self.state.blocksize = int(info.get("blocksize", self.state.blocksize))
            self.state.channels = int(info.get("channels", self.state.channels))
            self.state.fft_window = self.fft_window
            err = str(info.get("error", ""))
            if err:
                self.state.error = err
                self.state.enabled = False
                log.warning("analyser: source reported error: %s", err)
            else:
                self.state.error = ""
                self.state.enabled = True

    def stop(self) -> None:
        with self._lock:
            self.source.stop()
            self.state.enabled = False
            self.state.reset_levels()
            self._ring.fill(0.0)
            for n in self._normalizers.values():
                n.reset()

    def _on_block(self, mono: np.ndarray) -> None:
        n = mono.size
        fft_n = self._ring.size
        if n >= fft_n:
            # Block is at least as large as the FFT window — keep just the tail.
            self._ring[:] = mono[-fft_n:]
        else:
            # Slide the ring left by n samples and write the new block at the end.
            self._ring[: fft_n - n] = self._ring[n:]
            self._ring[fft_n - n :] = mono
        self._compute_features(self._ring)

    def _compute_features(self, win: np.ndarray) -> None:
        sr = self.state.samplerate
        rms_v = rms(win)
        peak_v = peak(win)
        low_v, mid_v, high_v = band_energies(win, sr, self.bands)
        s = self.state
        # Raw, instantaneous values — no EMA, no peak-hold. Modulator envelopes
        # downstream apply per-binding attack/release. Keeping the source state
        # raw is the whole point of the post-Phase-5 audio refactor: visuals
        # smooth, audio doesn't.
        s.rms = rms_v
        s.peak = peak_v
        s.low = low_v
        s.mid = mid_v
        s.high = high_v
        n = self._normalizers
        s.rms_norm = n["rms"].step(rms_v)
        s.peak_norm = n["peak"].step(peak_v)
        s.low_norm = n["low"].step(low_v)
        s.mid_norm = n["mid"].step(mid_v)
        s.high_norm = n["high"].step(high_v)
        s.block_count += 1

    @property
    def info(self) -> dict[str, Any]:
        """Pass-through of source info plus analyser-specific fields."""
        out = dict(self.source.info)
        out["fft_window"] = self.fft_window
        return out
