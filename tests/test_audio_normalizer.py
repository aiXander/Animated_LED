import numpy as np
import pytest

from ledctl.audio.normalizer import RollingNormalizer


def _drain(norm: RollingNormalizer, value: float, n: int) -> float:
    """Push `value` through the normalizer `n` times, returning the last output."""
    out = 0.0
    for _ in range(n):
        out = norm.step(value)
    return out


def test_silence_floored_to_min_ceiling():
    """Pure silence stays normalised to 0 — no division-by-zero, no noise blow-up."""
    n = RollingNormalizer(window_s=1.0, update_rate_hz=100.0, min_ceiling=0.02)
    for _ in range(200):
        out = n.step(0.0)
    assert out == 0.0
    assert n.ceiling >= 0.02


def test_steady_loud_input_normalises_to_one():
    """A steady level over the full window maps to ~1.0 (95th percentile == that level)."""
    n = RollingNormalizer(window_s=1.0, update_rate_hz=100.0)
    out = _drain(n, 0.5, 200)
    assert out == pytest.approx(1.0, abs=0.05)
    assert n.ceiling == pytest.approx(0.5, abs=0.05)


def test_room_loudness_inferred_regardless_of_absolute_level():
    """The whole point: a quiet mic and a loud mic both end up spanning [0, 1]."""
    quiet = RollingNormalizer(window_s=1.0, update_rate_hz=100.0)
    loud = RollingNormalizer(window_s=1.0, update_rate_hz=100.0)
    for _ in range(200):
        # "Quiet" mic peaks at 0.05, "loud" mic peaks at 0.7. Both should
        # eventually map their own peak to ~1.0.
        last_quiet = quiet.step(0.05)
        last_loud = loud.step(0.7)
    assert last_quiet == pytest.approx(1.0, abs=0.05)
    assert last_loud == pytest.approx(1.0, abs=0.05)


def test_sudden_spike_does_not_clip_after_snap_up():
    """A live value above the current ceiling triggers an immediate snap-up so
    the normalised output stays in [0, 1] instead of clipping at >1."""
    n = RollingNormalizer(window_s=1.0, update_rate_hz=100.0)
    # Establish a steady ceiling around 0.1.
    _drain(n, 0.1, 200)
    assert n.ceiling == pytest.approx(0.1, abs=0.02)
    # Now a sudden much-louder transient.
    out = n.step(0.8)
    assert out <= 1.0
    assert out == pytest.approx(1.0, abs=1e-6)
    assert n.ceiling >= 0.8


def test_brief_spike_does_not_dominate_long_window():
    """A single transient shouldn't lock the ceiling high for the whole window —
    that's why we use a percentile, not max."""
    n = RollingNormalizer(window_s=2.0, update_rate_hz=100.0)
    _drain(n, 0.1, 200)
    n.step(1.0)  # one-off spike
    # Continue at the steady level for a while.
    _drain(n, 0.1, 200)
    # 95th-percentile should be back near 0.1; one outlier in 200 samples = 0.5%.
    assert n.ceiling < 0.3, f"ceiling stuck at {n.ceiling}"


def test_ceiling_decays_when_room_goes_quiet():
    """As loud samples scroll out of the window, the ceiling drops and
    sensitivity returns."""
    n = RollingNormalizer(window_s=1.0, update_rate_hz=100.0)
    _drain(n, 0.5, 200)  # fill window with loud
    assert n.ceiling == pytest.approx(0.5, abs=0.05)
    _drain(n, 0.05, 200)  # full window of quiet
    assert n.ceiling == pytest.approx(0.05, abs=0.02)


def test_window_size_param_changes_buffer():
    a = RollingNormalizer(window_s=1.0, update_rate_hz=100.0)
    b = RollingNormalizer(window_s=10.0, update_rate_hz=100.0)
    assert a.buffer_size == 100
    assert b.buffer_size == 1000


def test_invalid_inputs_dont_poison_buffer():
    n = RollingNormalizer(window_s=1.0, update_rate_hz=100.0)
    n.step(float("nan"))
    n.step(float("inf"))
    n.step(-0.5)
    # Buffer should still hold finite, non-negative values.
    assert np.isfinite(n._buf).all()
    assert (n._buf >= 0.0).all()


def test_reset_clears_state():
    n = RollingNormalizer(window_s=1.0, update_rate_hz=100.0, min_ceiling=0.02)
    _drain(n, 0.7, 200)
    assert n.ceiling > 0.5
    n.reset()
    assert n.ceiling == pytest.approx(0.02)
    assert n.filled == 0


def test_validation_rejects_bad_params():
    with pytest.raises(ValueError):
        RollingNormalizer(window_s=0.0)
    with pytest.raises(ValueError):
        RollingNormalizer(update_rate_hz=0.0)
    with pytest.raises(ValueError):
        RollingNormalizer(percentile=0.0)
    with pytest.raises(ValueError):
        RollingNormalizer(percentile=101.0)
