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


def test_strength_one_gates_below_floor():
    """Input below every band's noise floor reads as exactly zero.

    Uses 0.02 — below all three floors (~0.05 / 0.04 / 0.03).
    """
    c = FeatureCleaner(update_rate_hz=375.0, strength=1.0)
    # Warm up at zero so peak follower isn't carrying state from loud input.
    for _ in range(50):
        c.step(0.0, 0.0, 0.0)
    low, mid, high = c.step(0.02, 0.02, 0.02)
    assert low == 0.0
    assert mid == 0.0
    assert high == 0.0


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
