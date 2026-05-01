"""Analyser-level tests: source decoupling, ring buffer, raw feature semantics."""

import math

import numpy as np

from ledctl.audio.analyser import AudioAnalyser
from ledctl.audio.source import AudioSource


class FakeSource(AudioSource):
    """Test double — drives the analyser's `_on_block` directly, no PortAudio."""

    def __init__(self, samplerate: int = 48000, blocksize: int = 128, channels: int = 1):
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._channels = channels
        self._on_block = None
        self._running = False

    @property
    def info(self) -> dict:
        return {
            "type": "fake",
            "device_name": "fake-source",
            "samplerate": self._samplerate,
            "blocksize": self._blocksize,
            "channels": self._channels,
            "error": "",
        }

    @property
    def running(self) -> bool:
        return self._running

    def start(self, on_block):
        self._on_block = on_block
        self._running = True

    def stop(self):
        self._on_block = None
        self._running = False

    def push(self, mono: np.ndarray) -> None:
        assert self._on_block is not None
        self._on_block(mono.astype(np.float32))


def _sine(freq: float, sr: int, n: int, amp: float = 1.0) -> np.ndarray:
    t = np.arange(n) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_analyser_writes_raw_features():
    """No EMA / no peak hold: a single block of a 1 kHz sine should give RMS
    ≈ amp / √2 immediately, not ramp up over multiple blocks."""
    src = FakeSource(samplerate=48000, blocksize=512)
    a = AudioAnalyser(src, fft_window=512)
    a.start()
    block = _sine(1000.0, 48000, 512, amp=0.5)
    src.push(block)
    assert a.state.rms == math.sqrt(0.25 / 2.0) or a.state.rms > 0.34
    # Tighter check.
    assert abs(a.state.rms - (0.5 / math.sqrt(2.0))) < 0.01
    assert abs(a.state.peak - 0.5) < 0.01


def test_analyser_features_drop_to_zero_on_silence_no_decay():
    """After loud → silence, raw features snap to 0 on the very next block.
    The old peak-hold would decay slowly; the new design defers that to
    modulator envelopes."""
    src = FakeSource(samplerate=48000, blocksize=512)
    a = AudioAnalyser(src, fft_window=512)
    a.start()
    src.push(_sine(1000.0, 48000, 512, amp=0.5))
    assert a.state.peak > 0.4
    src.push(np.zeros(512, dtype=np.float32))
    assert a.state.peak == 0.0
    assert a.state.rms == 0.0


def test_fft_window_can_exceed_blocksize():
    """fft_window=2048 with blocksize=128 — analyser accumulates 16 blocks
    before the ring is fully populated, then FFTs the full window each call."""
    src = FakeSource(samplerate=48000, blocksize=128)
    a = AudioAnalyser(src, fft_window=2048)
    a.start()
    # Push 16 blocks of zeros to "fill" the ring with silence baseline,
    # then 16 blocks of a 100 Hz tone to fully populate with signal.
    for _ in range(16):
        src.push(np.zeros(128, dtype=np.float32))
    sig = _sine(100.0, 48000, 16 * 128, amp=0.5)
    for i in range(16):
        src.push(sig[i * 128 : (i + 1) * 128])
    # After the ring is fully signal, low band should be hot.
    assert a.state.low > 0.05
    assert a.state.mid < 0.02
    assert a.state.high < 0.02


def test_fft_window_smaller_than_block_rejected():
    """Configuration sanity — analyser refuses fft_window < source blocksize."""
    src = FakeSource(samplerate=48000, blocksize=512)
    try:
        AudioAnalyser(src, fft_window=128)
    except ValueError as e:
        assert "fft_window" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_analyser_reset_on_stop():
    src = FakeSource(samplerate=48000, blocksize=512)
    a = AudioAnalyser(src, fft_window=512)
    a.start()
    src.push(_sine(1000.0, 48000, 512, amp=0.5))
    assert a.state.rms > 0
    a.stop()
    assert a.state.rms == 0.0
    assert a.state.peak == 0.0
    assert a.state.enabled is False


def test_legacy_smoothing_field_silently_dropped():
    """Old YAMLs with `audio.smoothing: 0.4` should still load — the field is
    no longer meaningful but we don't break existing configs."""
    from ledctl.config import AudioConfig

    cfg = AudioConfig.model_validate(
        {
            "enabled": True,
            "device": None,
            "samplerate": 48000,
            "blocksize": 128,
            "fft_window": 512,
            "channels": 1,
            "gain": 1.0,
            "smoothing": 0.4,
        }
    )
    # Field is gone from the model.
    assert not hasattr(cfg, "smoothing")
    assert cfg.blocksize == 128
    assert cfg.fft_window == 512


def test_fft_window_smaller_than_blocksize_in_config_rejected():
    from pydantic import ValidationError as PydValidationError

    from ledctl.config import AudioConfig

    try:
        AudioConfig(blocksize=512, fft_window=128)
    except PydValidationError as e:
        assert "fft_window" in str(e)
    else:
        raise AssertionError("expected ValidationError")
