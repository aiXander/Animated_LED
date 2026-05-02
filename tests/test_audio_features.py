import numpy as np

from ledctl.audio.features import band_energies


def _sine(freq: float, sr: int, n: int, amp: float = 1.0) -> np.ndarray:
    t = np.arange(n) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_band_energies_isolate_band():
    sr = 48000
    n = 4096
    # 100 Hz → low band (20–250 Hz); mid/high should stay near zero.
    low, mid, high = band_energies(_sine(100.0, sr, n, amp=0.5), sr)
    assert low > 0.05, f"expected low band > 0.05, got {low}"
    assert mid < 0.01, f"expected mid band ≈ 0, got {mid}"
    assert high < 0.01, f"expected high band ≈ 0, got {high}"

    # 6 kHz → high band.
    low, mid, high = band_energies(_sine(6000.0, sr, n, amp=0.5), sr)
    assert high > 0.05
    assert low < 0.01
    assert mid < 0.01


def test_band_energies_empty_input():
    assert band_energies(np.zeros(0, dtype=np.float32), 48000) == (0.0, 0.0, 0.0)


def test_band_energies_handle_short_block():
    # Smallest realistic block size shouldn't crash.
    x = _sine(440.0, 48000, 64)
    bands = band_energies(x, 48000)
    assert len(bands) == 3
