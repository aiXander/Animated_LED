"""Analyser-level tests: source decoupling, ring buffer, raw feature semantics."""

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
    """No EMA / no peak hold: a single block of a 1 kHz sine should land
    energy in the mid band immediately, not ramp up over multiple blocks."""
    src = FakeSource(samplerate=48000, blocksize=512)
    a = AudioAnalyser(src, fft_window=512)
    a.start()
    block = _sine(1000.0, 48000, 512, amp=0.5)
    src.push(block)
    # 1 kHz sits in the mid band (250 Hz–2 kHz); low/high should stay quiet.
    assert a.state.mid > 0.05
    assert a.state.low < 0.02
    assert a.state.high < 0.02


def test_analyser_features_drop_to_zero_on_silence_no_decay():
    """After loud → silence, raw features snap to 0 on the very next block.
    The old peak-hold would decay slowly; the new design defers that to
    modulator envelopes."""
    src = FakeSource(samplerate=48000, blocksize=512)
    a = AudioAnalyser(src, fft_window=512)
    a.start()
    src.push(_sine(1000.0, 48000, 512, amp=0.5))
    assert a.state.mid > 0.05
    src.push(np.zeros(512, dtype=np.float32))
    assert a.state.low == 0.0
    assert a.state.mid == 0.0
    assert a.state.high == 0.0


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


def test_per_band_normalization_independent():
    """Each band has its own RollingNormalizer — a quiet band must auto-scale
    to ~[0, 1] of its own dynamic range, not get drowned out by a louder band.

    Drives a low+high mix where the low band's raw energy is much larger than
    the high band's. After enough blocks both `low_norm` and `high_norm`
    should saturate near 1.0, proving each ceiling tracks its own band.
    """
    sr = 48000
    bs = 512
    # Short normalize window so we hit a stable percentile in <1 s of test time.
    # Update rate at this blocksize is sr/bs ≈ 94 Hz, so 1 s ≈ 94 blocks.
    src = FakeSource(samplerate=sr, blocksize=bs)
    a = AudioAnalyser(src, fft_window=bs, normalize_window_s=1.0)
    a.start()
    # Loud bass (100 Hz, amp 0.7) + much quieter hat (6 kHz, amp 0.05).
    bass = _sine(100.0, sr, bs, amp=0.7)
    hat = _sine(6000.0, sr, bs, amp=0.05)
    mix = (bass + hat).astype(np.float32)
    for _ in range(120):
        src.push(mix)
    # Raw energies confirm the bands are at very different absolute levels.
    assert a.state.low > 10.0 * a.state.high, (
        f"expected low ≫ high; got low={a.state.low:.4f} high={a.state.high:.4f}"
    )
    # But after the rolling-window auto-gain, both bands span ~[0, 1] of their
    # own range — the quiet hat is just as visible to a binding as the loud kick.
    assert 0.85 <= a.state.low_norm <= 1.0, f"low_norm={a.state.low_norm}"
    assert 0.85 <= a.state.high_norm <= 1.0, f"high_norm={a.state.high_norm}"


def test_cleaning_strength_zero_is_bit_exact_passthrough():
    """Critical contract: at audio_feature_cleaning=0, the analyser's *_norm
    outputs must be exactly the rolling-normalizer values — no peak-follower
    decay, no noise-gate dead zone, no transient one-block artifacts. This
    proves the operator can fully disable the cleaner via the master slider.

    Strategy: run a known-impulse signal through the analyser at strength=0
    and check that *_norm drops to 0 the instant the raw signal does (no
    decay tail). This is the simplest assertion that excludes both the peak
    follower (which would hold) and the noise gate (which would still report 0
    but for a different reason — we cover that with a positive-quiet-input
    check too).
    """
    sr = 48000
    bs = 512
    src = FakeSource(samplerate=sr, blocksize=bs)
    a = AudioAnalyser(src, fft_window=bs)
    a.cleaning_strength = 0.0
    a.start()
    # Loud signal — drives normalizer ceiling up so *_norm reaches ~1.
    for _ in range(20):
        src.push(_sine(1000.0, sr, bs, amp=0.8))
    assert a.state.mid_norm > 0.5, (
        f"sanity: loud mid should be reactive, got mid_norm={a.state.mid_norm}"
    )
    # Single block of silence. With cleaner off, mid_norm must be exactly 0
    # because the rolling-normalizer's input is exactly 0. With the cleaner
    # on, the peak follower would hold at ~1 and decay over many blocks.
    src.push(np.zeros(bs, dtype=np.float32))
    assert a.state.mid_norm == 0.0, (
        f"strength=0 should produce zero hold/decay; mid_norm={a.state.mid_norm}"
    )
    assert a.state.low_norm == 0.0
    assert a.state.high_norm == 0.0
    # And a small but non-zero raw signal should NOT be gated — the noise
    # floor only fires at strength>0.
    quiet = _sine(1000.0, sr, bs, amp=0.02).astype(np.float32)
    src.push(quiet)
    # After 20 loud blocks, the rolling normalizer's ceiling is high, so this
    # quiet block normalises to a small positive value. Cleaner-off => that
    # small value reaches mid_norm verbatim. Cleaner-on (strength=1) would
    # squash it to 0 via the noise gate.
    assert a.state.mid_norm > 0.0, (
        f"strength=0 should not gate quiet signals; mid_norm={a.state.mid_norm}"
    )


def test_cleaning_strength_one_decays_from_loud_to_silent():
    """Mirror of the above: at strength=1, after loud→silent, the *_norm
    output decays smoothly rather than snapping to 0 — confirming the cleaner
    is in fact active when the master slider says so."""
    sr = 48000
    bs = 512
    src = FakeSource(samplerate=sr, blocksize=bs)
    a = AudioAnalyser(src, fft_window=bs)
    a.cleaning_strength = 1.0
    a.start()
    for _ in range(20):
        src.push(_sine(1000.0, sr, bs, amp=0.8))
    src.push(np.zeros(bs, dtype=np.float32))
    # With τ_mid = 45 ms and dt = bs/sr ≈ 10.7 ms, decay-per-block ≈ 0.79.
    # Output is well above 0 but below the loud peak — the signature of an
    # active envelope follower.
    assert 0.1 < a.state.mid_norm < 1.0, (
        f"strength=1 should produce decay, not snap-to-zero; "
        f"mid_norm={a.state.mid_norm}"
    )


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
    assert a.state.mid > 0
    a.stop()
    assert a.state.low == 0.0
    assert a.state.mid == 0.0
    assert a.state.high == 0.0
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
