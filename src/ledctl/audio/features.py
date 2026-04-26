import numpy as np

# Default analysis bands (Hz). Three broad bins is plenty for visual reactivity
# — finer resolution is the kind of thing aubio/librosa would replace later.
DEFAULT_BANDS: tuple[tuple[float, float], ...] = (
    (20.0, 250.0),     # low
    (250.0, 2000.0),   # mid
    (2000.0, 12000.0), # high
)

_WINDOWS: dict[int, np.ndarray] = {}


def _hann(n: int) -> np.ndarray:
    win = _WINDOWS.get(n)
    if win is None:
        win = np.hanning(n).astype(np.float32)
        _WINDOWS[n] = win
    return win


def rms(mono: np.ndarray) -> float:
    """Root-mean-square amplitude. `mono` is a 1-D float buffer in roughly [-1, 1]."""
    if mono.size == 0:
        return 0.0
    # Promote to float64 for the sum to avoid catastrophic cancellation on tiny
    # blocks of float32; the cost is negligible at our block sizes.
    return float(np.sqrt(np.mean(np.square(mono, dtype=np.float64))))


def peak(mono: np.ndarray) -> float:
    if mono.size == 0:
        return 0.0
    return float(np.max(np.abs(mono)))


def band_energies(
    mono: np.ndarray,
    samplerate: int,
    bands: tuple[tuple[float, float], ...] = DEFAULT_BANDS,
) -> tuple[float, ...]:
    """Sum FFT magnitudes per frequency band, normalised so each band's value
    is roughly in [0, 1] for typical line-level music."""
    n = mono.size
    if n == 0:
        return tuple(0.0 for _ in bands)
    spec = np.abs(np.fft.rfft(mono * _hann(n)))
    # Normalise: divide by half the bin count so a full-scale sine in one band
    # comes out near 1.0. Empirical, but stable across block sizes.
    spec = spec * (2.0 / n)
    freqs = np.fft.rfftfreq(n, 1.0 / samplerate)
    out: list[float] = []
    for lo, hi in bands:
        mask = (freqs >= lo) & (freqs < hi)
        if not mask.any():
            out.append(0.0)
        else:
            out.append(float(spec[mask].sum()))
    return tuple(out)
