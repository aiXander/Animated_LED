"""Audio sources — producers of mono float32 sample blocks.

A source's only job is to deliver `blocksize`-sample chunks to a callback as
soon as the OS hands them up. It owns nothing about FFTs, bands, or feature
state — that all lives in `analyser.py`. The split exists so a future source
(DJ booth over USB audio, network audio over RTP/AES67/NDI, file-based replay
for tests) can drop in without touching the analyser.

Today's only impl is `SoundDeviceSource` (PortAudio via `sounddevice`). When a
second source type appears, lift `AudioConfig.source` to a discriminated union;
until then, the top-level audio fields apply to whatever source is wired in.
"""

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# A source delivers mono float32 blocks to this callback. The callback runs on
# whatever thread the source uses internally (PortAudio's RT thread for
# SoundDeviceSource); it must be cheap and non-blocking.
BlockCallback = Callable[[np.ndarray], None]


class AudioSource(ABC):
    """Producer of mono float32 sample blocks.

    Implementations are responsible for:
      - opening their backing device/stream/socket on `start()`
      - down-mixing multi-channel input to mono before invoking the callback
      - applying the configured `gain` (one multiply, fine in the hot path)
      - exposing `info` so the analyser/UI can display what's actually running
    """

    @property
    @abstractmethod
    def info(self) -> dict[str, Any]:
        """Public-facing description: device_name, samplerate, blocksize, channels, error."""

    @property
    @abstractmethod
    def running(self) -> bool: ...

    @abstractmethod
    def start(self, on_block: BlockCallback) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


# ---- sounddevice / PortAudio ----


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


class SoundDeviceSource(AudioSource):
    """PortAudio capture via `sounddevice`. Mac built-in mic, USB interfaces,
    Pi I²S — anything the OS exposes as an input device.

    Down-mixes to mono and applies `gain` inside the PortAudio callback before
    handing the block to the registered analyser callback. Both ops are O(n)
    numpy and trivial at our blocksizes.
    """

    def __init__(
        self,
        device: str | int | None = None,
        samplerate: int = 48000,
        blocksize: int = 128,
        channels: int = 1,
        gain: float = 1.0,
    ):
        self.device = device
        self.samplerate = int(samplerate)
        self.blocksize = int(blocksize)
        self.channels = int(channels)
        self.gain = float(gain)
        self._stream: Any = None
        self._device_name: str = ""
        self._error: str = ""
        self._on_block: BlockCallback | None = None
        self._lock = threading.Lock()

    @property
    def info(self) -> dict[str, Any]:
        return {
            "type": "sounddevice",
            "device_name": self._device_name,
            "samplerate": self.samplerate,
            "blocksize": self.blocksize,
            "channels": self.channels,
            "error": self._error,
        }

    @property
    def running(self) -> bool:
        return self._stream is not None

    def start(self, on_block: BlockCallback) -> None:
        with self._lock:
            if self._stream is not None:
                return
            self._on_block = on_block
            try:
                sd = _import_sounddevice()
            except (ImportError, OSError) as e:
                self._error = f"sounddevice unavailable: {e}"
                log.warning(self._error)
                return
            try:
                idx, name = resolve_device(self.device)
                stream = sd.InputStream(
                    device=idx,
                    samplerate=self.samplerate,
                    blocksize=self.blocksize,
                    channels=self.channels,
                    dtype="float32",
                    callback=self._pa_callback,
                )
                stream.start()
            except Exception as e:
                self._error = str(e)
                log.warning("audio capture failed to start: %s", e)
                return
            self._stream = stream
            self._device_name = name
            self._error = ""
            log.info(
                "audio source started: %r @ %d Hz, block %d, %d ch",
                name, self.samplerate, self.blocksize, self.channels,
            )

    def stop(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
            self._on_block = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                log.warning("audio stop error: %s", e)

    def _pa_callback(
        self, indata: np.ndarray, frames: int, time_info: Any, status: Any
    ) -> None:
        if status:
            # xrun / dropouts; log at debug — frequent on laptops while other
            # apps grab the device, and not actionable from here.
            log.debug("audio status: %s", status)
        cb = self._on_block
        if cb is None:
            return
        if indata.ndim > 1 and indata.shape[1] > 1:
            mono = indata.mean(axis=1)
        else:
            mono = indata[:, 0] if indata.ndim > 1 else indata
        if self.gain != 1.0:
            mono = mono * self.gain
        cb(mono)
