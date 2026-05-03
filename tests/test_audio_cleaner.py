"""FeatureCleaner tests — passthrough at strength=0, peak-follower + noise-gate
at strength=1, no rising-edge latency on lows/mids."""

from ledctl.audio.cleaner import FeatureCleaner


def test_strength_zero_is_passthrough():
    """At strength=0 the cleaner must not alter the bindings' inputs."""
    c = FeatureCleaner(update_rate_hz=375.0, strength=0.0)
    for x in (0.0, 0.05, 0.3, 0.8, 1.0):
        out = c.step(x, x, x)
        assert out == (x, x, x), f"x={x} → {out}"


def test_strength_one_passes_full_signal_through():
    """A loud sustained input should map to ~1.0 on every band: the peak
    follower locks at the input level, the gate maps that to full scale."""
    c = FeatureCleaner(update_rate_hz=375.0, strength=1.0)
    for _ in range(50):
        c.step(0.95, 0.95, 0.95)
    low, mid, high = c.step(0.95, 0.95, 0.95)
    # All three should saturate near 1.0 (gate stretches [floor, 1] → [0, 1]).
    assert low > 0.9, f"low={low}"
    assert mid > 0.9, f"mid={mid}"
    assert high > 0.9, f"high={high}"


def test_strength_one_smoothly_compresses_small_values():
    """No hard noise gate — small values are pulled toward zero by a power
    compression curve (y**p, p > 1). The curve is monotonic and continuous,
    so a value drifting around the noise level can't flicker on/off the way
    a hard threshold would. Peaks at y=1 still map to 1; mid-range values
    are noticeably attenuated.
    """
    c = FeatureCleaner(update_rate_hz=375.0, strength=1.0)
    # Warm up at zero so peak follower isn't carrying state from loud input.
    for _ in range(50):
        c.step(0.0, 0.0, 0.0)
    low, mid, high = c.step(0.2, 0.2, 0.2)
    # Each band should compress 0.2 measurably, with stronger compression
    # on highs (p_high > p_low). Outputs strictly less than the input.
    assert 0.0 < low < 0.2, f"low={low}"
    assert 0.0 < mid < 0.2, f"mid={mid}"
    assert 0.0 < high < 0.2, f"high={high}"
    # Highs compress more than lows by design.
    assert high < low, f"expected high < low (more compression), got {high} vs {low}"


def test_compression_is_monotonic_no_hard_threshold():
    """A small step in input must produce a small step in output — no
    discontinuity around any noise level. Sweeps inputs 0.0 → 0.5 in fine
    steps and asserts the output sequence is monotonically non-decreasing
    with no jumps that would indicate a threshold.
    """
    c = FeatureCleaner(update_rate_hz=375.0, strength=1.0)
    # Warm up at zero.
    for _ in range(50):
        c.step(0.0, 0.0, 0.0)
    # Sweep slowly upwards; reset the peak follower between samples by
    # hand-driving it down so each step's output is the rising-edge
    # (instant-follow) value of the compression curve at that input.
    outputs = []
    for v in [i / 100.0 for i in range(0, 51)]:
        c.reset()
        low, _mid, _high = c.step(v, 0.0, 0.0)
        outputs.append(low)
    # Monotonically non-decreasing.
    for i in range(1, len(outputs)):
        assert outputs[i] >= outputs[i - 1] - 1e-9, (
            f"non-monotonic at i={i}: {outputs[i-1]} → {outputs[i]}"
        )
    # No jump bigger than what a smooth curve would give. Largest expected
    # step on y**1.7 between 0.50 and 0.49 is ~0.020; require well under that.
    max_step = max(outputs[i] - outputs[i - 1] for i in range(1, len(outputs)))
    assert max_step < 0.03, f"suspicious jump suggesting threshold: {max_step}"


def test_release_decays_after_loud_input():
    """When loud input drops to silence, the peak follower decays exponentially
    rather than snapping — that's what gives kicks a clean 'ring out'."""
    c = FeatureCleaner(update_rate_hz=375.0, strength=1.0)
    # Lock the follower at 1.0.
    for _ in range(100):
        c.step(1.0, 1.0, 1.0)
    # Silence — output should decay, not snap to zero.
    low_decay = []
    for _ in range(30):
        low, _mid, _high = c.step(0.0, 0.0, 0.0)
        low_decay.append(low)
    # First post-silence sample is still elevated (released by ~0.997 per block).
    assert low_decay[0] > 0.9, f"first decay sample = {low_decay[0]}"
    # Decay is monotonic non-increasing.
    for i in range(1, len(low_decay)):
        assert low_decay[i] <= low_decay[i - 1] + 1e-6, (
            f"non-monotonic decay at {i}: {low_decay[i-1]} → {low_decay[i]}"
        )


def test_all_bands_zero_latency_on_rising_edge():
    """Every band must have NO latency on transients — kicks need to snap to
    full scale on the block they land, hats especially."""
    c = FeatureCleaner(update_rate_hz=375.0, strength=1.0)
    # Cold start at zero — first block of full input should jump to full out.
    low, mid, high = c.step(1.0, 1.0, 1.0)
    assert low > 0.99, f"low={low}"
    assert mid > 0.99, f"mid={mid}"
    assert high > 0.99, f"high={high}"


def test_bass_decays_between_150bpm_beats():
    """At 150 BPM (400 ms between beats) the bass envelope must have decayed
    to near-silent before the next hit lands — otherwise consecutive kicks
    visually merge into a sustained level."""
    update_rate = 375.0  # 48000 / 128
    c = FeatureCleaner(update_rate_hz=update_rate, strength=1.0)
    # Beat lands.
    c.step(1.0, 0.0, 0.0)
    # 400 ms of silence between beats.
    blocks_in_400ms = int(0.400 * update_rate)
    last_low = 1.0
    for _ in range(blocks_in_400ms):
        last_low, _mid, _high = c.step(0.0, 0.0, 0.0)
    # Should be well under 10% by next beat.
    assert last_low < 0.1, (
        f"bass envelope still at {last_low:.3f} after 400 ms — too much hold"
    )


def test_strength_clamped_to_unit_interval():
    """Out-of-range strength values must clamp inside step()."""
    c = FeatureCleaner(update_rate_hz=375.0)
    c.strength = 5.0
    out = c.step(1.0, 1.0, 1.0)
    # Should behave as strength=1.0 (full cleaning), not amplify past raw.
    assert all(0.0 <= v <= 1.0 for v in out)


def test_reset_clears_state():
    """After reset, the cleaner behaves like a freshly-constructed one."""
    c = FeatureCleaner(update_rate_hz=375.0, strength=1.0)
    for _ in range(50):
        c.step(1.0, 1.0, 1.0)
    c.reset()
    # First post-reset sample at zero should be zero (no peak-follower memory).
    low, mid, high = c.step(0.0, 0.0, 0.0)
    assert low == 0.0
    assert mid == 0.0
    assert high == 0.0
