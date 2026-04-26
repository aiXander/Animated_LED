import logging
import threading
from typing import Any

import numpy as np

from .features import DEFAULT_BANDS, band_energies, peak, rms
from .normalizer import RollingNormalizer
from .state import AudioState

# Default rolling window for the per-feature auto-gain. 60 s is long enough
# that a single song section doesn't dominate (verses, breakdowns and drops
# all contribute), and short enough that moving the mic / changing the room
# resettles the dynamic range within a minute.
DEFAULT_NORMALIZE_WINDOW_S: float = 60.0

log = logging.getLogger(__name__)


def _import_sounddevice() -> Any:
    """Lazy import so machines without PortAudio don't crash at module load.

    Raises ImportError or OSError on systems where the binding can't initialise
    (no PortAudio shared lib). Callers must handle that.
    """
    import sounddevice as sd  # noqa: PLC0415
    return sd


def list_input_devices() -> list[dict[str, Any]]:
    """Enumerate available capture devices. Empty list if PortAudio isn't usable."""
    try:
        sd = _import_sounddevice()
    except (ImportError, OSError) as e:
        log.warning("sounddevice unavailable: %s", e)
        return []

    try:
        default_in = sd.default.device[0] if sd.default.device else -1
    except Exception:
        default_in = -1

    out: list[dict[str, Any]] = []
    for i, dev in enumerate(sd.query_devices()):
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        try:
            host_name = sd.query_hostapis(dev["hostapi"])["name"]
        except Exception:
            host_name = ""
        out.append(
            {
                "index": i,
                "name": dev["name"],
                "hostapi": host_name,
                "max_input_channels": int(dev["max_input_channels"]),
                "default_samplerate": float(dev.get("default_samplerate", 0.0)),
                "is_default": (i == default_in),
            }
        )
    return out


def resolve_device(spec: str | int | None) -> tuple[int | None, str]:
    """Resolve a device spec (None / int / name) to (index, display_name).

    Strings match by exact case-insensitive name first, then a substring fallback
    so the user can save a short token (e.g. "MacBook") and have it stick across
    reboots even if the OS appends a serial.
    """
    if spec is None:
        try:
            sd = _import_sounddevice()
            idx = sd.default.device[0] if sd.default.device else None
            if idx is None or idx < 0:
                return None, "system default"
            return int(idx), str(sd.query_devices(idx)["name"])
        except (ImportError, OSError, IndexError, KeyError):
            return None, "system default"

    sd = _import_sounddevice()
    devs = sd.query_devices()

    if isinstance(spec, int):
        if spec < 0 or spec >= len(devs):
            raise RuntimeError(f"audio device index {spec} out of range (0..{len(devs) - 1})")
        if int(devs[spec].get("max_input_channels", 0)) <= 0:
            raise RuntimeError(f"device {spec} ({devs[spec]['name']!r}) has no input channels")
        return spec, str(devs[spec]["name"])

    target = spec.strip().lower()
    for i, dev in enumerate(devs):
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        if dev["name"].lower() == target:
            return i, str(dev["name"])
    for i, dev in enumerate(devs):
        if int(dev.get("max_input_channels", 0)) <= 0:
            continue
        if target in dev["name"].lower():
            return i, str(dev["name"])
    raise RuntimeError(f"no input device matching {spec!r}")


class AudioCapture:
    """Thin wrapper around `sounddevice.InputStream` that maintains an `AudioState`.

    The PortAudio callback runs on a private thread; we compute features there
    (cheap numpy ops on a 512-frame block) and write into the shared state. The
    asyncio render loop reads scalar fields without locking.
    """

    def __init__(
        self,
        device: str | int | None = None,
        samplerate: int = 48000,
        blocksize: int = 512,
        channels: int = 1,
        gain: float = 1.0,
        smoothing: float = 0.4,
        bands: tuple[tuple[float, float], ...] = DEFAULT_BANDS,
        normalize_window_s: float = DEFAULT_NORMALIZE_WINDOW_S,
    ):
        self.device = device
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.channels = channels
        self.gain = gain
        self.smoothing = float(np.clip(smoothing, 0.0, 0.99))
        self.bands = bands
        self.normalize_window_s = float(normalize_window_s)
        self.state = AudioState(
            samplerate=samplerate, blocksize=blocksize, channels=channels
        )
        # One normalizer per feature. The callback fires at samplerate/blocksize
        # blocks per second — that's the sample rate the rolling buffer sees.
        update_rate_hz = float(samplerate) / float(max(1, blocksize))
        self._normalizers: dict[str, RollingNormalizer] = {
            name: RollingNormalizer(
                window_s=self.normalize_window_s,
                update_rate_hz=update_rate_hz,
            )
            for name in ("rms", "peak", "low", "mid", "high")
        }
        self._stream: Any = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            try:
                sd = _import_sounddevice()
            except (ImportError, OSError) as e:
                msg = f"sounddevice unavailable: {e}"
                self.state.error = msg
                self.state.enabled = False
                log.warning(msg)
                return
            try:
                idx, name = resolve_device(self.device)
                stream = sd.InputStream(
                    device=idx,
                    samplerate=self.samplerate,
                    blocksize=self.blocksize,
                    channels=self.channels,
                    dtype="float32",
                    callback=self._callback,
                )
                stream.start()
            except Exception as e:
                self.state.error = str(e)
                self.state.enabled = False
                log.warning("audio capture failed to start: %s", e)
                return
            self._stream = stream
            self.state.device_name = name
            self.state.enabled = True
            self.state.error = ""
            log.info(
                "audio capture started: %r @ %d Hz, block %d, %d ch",
                name, self.samplerate, self.blocksize, self.channels,
            )

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                log.warning("audio stop error: %s", e)
        self.state.enabled = False
        self.state.reset_levels()

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status:
            # xrun / dropouts; log at debug — they're frequent on laptops while
            # other apps grab the device, and not actionable from here.
            log.debug("audio status: %s", status)
        if indata.ndim > 1 and indata.shape[1] > 1:
            mono = indata.mean(axis=1)
        else:
            mono = indata[:, 0] if indata.ndim > 1 else indata
        if self.gain != 1.0:
            mono = mono * self.gain
        rms_v = rms(mono)
        peak_v = peak(mono)
        low_v, mid_v, high_v = band_energies(mono, self.samplerate, self.bands)
        a = self.smoothing
        s = self.state
        s.rms = a * s.rms + (1.0 - a) * rms_v
        # Peak hold: snap up, decay slowly. ~1 s to fall by 50% at 100 blocks/s.
        s.peak = max(peak_v, s.peak * 0.92)
        s.low = a * s.low + (1.0 - a) * low_v
        s.mid = a * s.mid + (1.0 - a) * mid_v
        s.high = a * s.high + (1.0 - a) * high_v
        # Feed the smoothed values into the rolling-window normalizers so
        # consumers (modulators) read auto-scaled [0, 1] regardless of mic
        # gain. Raw fields stay raw for the level-meter UI.
        n = self._normalizers
        s.rms_norm = n["rms"].step(s.rms)
        s.peak_norm = n["peak"].step(s.peak)
        s.low_norm = n["low"].step(s.low)
        s.mid_norm = n["mid"].step(s.mid)
        s.high_norm = n["high"].step(s.high)
        s.block_count += 1
