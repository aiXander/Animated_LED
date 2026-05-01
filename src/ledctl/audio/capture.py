"""Backwards-compatible orchestrator: source + analyser as a single object.

Pre-refactor this module owned both the PortAudio stream and the feature math.
That's now split into `source.SoundDeviceSource` (any input device) and
`analyser.AudioAnalyser` (FFT, bands, rolling-window normalizer). This wrapper
exists so callers (server.py, tests) keep one handle and don't have to wire
the two pieces themselves; new sources land in `source.py`, new feature math
lands in `analyser.py`.
"""

from typing import Any

from .analyser import DEFAULT_NORMALIZE_WINDOW_S, AudioAnalyser
from .features import DEFAULT_BANDS
from .source import SoundDeviceSource, list_input_devices, resolve_device
from .state import AudioState

__all__ = [
    "DEFAULT_NORMALIZE_WINDOW_S",
    "AudioCapture",
    "list_input_devices",
    "resolve_device",
]


class AudioCapture:
    """Convenience wrapper that pre-wires `SoundDeviceSource` + `AudioAnalyser`.

    Equivalent to constructing the two pieces separately and calling
    `analyser.start()` / `analyser.stop()`. Holds them as `self.source` and
    `self.analyser` for callers that want to reach in (e.g. picking a different
    source type later — swap `self.source` for a `NetworkAudioSource` and
    rebuild the analyser).
    """

    def __init__(
        self,
        device: str | int | None = None,
        samplerate: int = 48000,
        blocksize: int = 128,
        fft_window: int = 512,
        channels: int = 1,
        gain: float = 1.0,
        bands: tuple[tuple[float, float], ...] = DEFAULT_BANDS,
        normalize_window_s: float = DEFAULT_NORMALIZE_WINDOW_S,
    ):
        self.source = SoundDeviceSource(
            device=device,
            samplerate=samplerate,
            blocksize=blocksize,
            channels=channels,
            gain=gain,
        )
        self.analyser = AudioAnalyser(
            self.source,
            fft_window=fft_window,
            bands=bands,
            normalize_window_s=normalize_window_s,
        )

    @property
    def state(self) -> AudioState:
        return self.analyser.state

    @property
    def running(self) -> bool:
        return self.analyser.running

    @property
    def info(self) -> dict[str, Any]:
        return self.analyser.info

    def start(self) -> None:
        self.analyser.start()

    def stop(self) -> None:
        self.analyser.stop()
