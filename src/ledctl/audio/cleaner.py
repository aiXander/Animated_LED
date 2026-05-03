"""Per-band post-processor that turns jittery normalised features into
musically-shaped envelopes.

Live music through the FFT pipeline has two annoying failure modes for visual
modulation:
  - hi-hat / cymbal bands jitter on a per-block basis: single blocks spike
    above neighbours by 20-30%, producing visible random flicker that doesn't
    sync to anything in the music.
  - the bass band sustains around 30-50% of its rolling ceiling between hits
    on a steady track, so a "kick" looks like a tiny bump on top of a noisy
    floor instead of a clean discrete pulse.

Both go away with two cheap ops applied per band:

  1. Asymmetric peak follower with exponential release.
       y[t] = max(x[t], y[t-1] * release)
     Rising edges pass through immediately on every band — zero added latency
     on any transient. Falling edges decay with a per-band time constant
     short enough that periodic content up to ~150 BPM (400 ms period)
     decays to near-silent between hits.

  2. Soft noise gate. (y - floor) / (1 - floor), clamped to [0, 1]. The
     mic-noise floor is trimmed; gate is intentionally low so musical
     dynamics survive — easier to dial up than to recover lost detail.

`strength` ∈ [0, 1] linearly blends raw input against the fully-shaped
output, per band. At strength=0 the cleaner is a passthrough; at strength=1
the bindings see the full musical envelope. The strength is read fresh each
block, so the operator slider feels live.

State is three floats — runs at audio block rate, sub-microsecond per call.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class _BandShape:
    """Tuning knobs per band. Defaults err on the side of keeping musical
    dynamics intact — easier to dial up than to recover lost detail.

    `release_tau_s` is the *e-fold* time of the exponential decay on falling
    edges. Constraint: must be short enough that 150 BPM (400 ms beat-to-beat)
    bass decays cleanly between hits. With τ=80 ms, exp(-400/80) ≈ 0.007 →
    fully decayed.

    `noise_floor` is in normalised units (post rolling-window auto-gain), so
    0.05 means "trim the bottom 5% of the recent-loudness ceiling." Set low
    so bass mid-decay doesn't get squashed.

    `attack_alpha` blends the previous output back in on rising edges; 0 means
    instant follow (zero latency). All bands use 0 — highs especially must
    stay transparent on rising edges.
    """

    release_tau_s: float
    noise_floor: float
    attack_alpha: float


# Index order: low, mid, high. Matches AudioState's *_norm fields.
_BAND_SHAPES: tuple[_BandShape, _BandShape, _BandShape] = (
    _BandShape(release_tau_s=0.080, noise_floor=0.05, attack_alpha=0.0),
    _BandShape(release_tau_s=0.045, noise_floor=0.04, attack_alpha=0.0),
    _BandShape(release_tau_s=0.025, noise_floor=0.03, attack_alpha=0.0),
)


class FeatureCleaner:
    """Stateful per-band envelope shaper.

    Construct once per analyser; call `step(low, mid, high)` once per audio
    block with the rolling-window-normalised values. Returns the cleaned
    triple. `strength` is a public attribute — the engine writes it once per
    render tick from `masters.audio_feature_cleaning`. CPython float assignment
    is atomic, so no lock is needed despite the writer/reader living on
    different threads.
    """

    __slots__ = ("_release", "_floor", "_attack", "_y0", "_y1", "_y2", "strength")

    def __init__(self, update_rate_hz: float, strength: float = 1.0):
        if update_rate_hz <= 0.0:
            raise ValueError(f"update_rate_hz must be > 0, got {update_rate_hz}")
        dt = 1.0 / float(update_rate_hz)
        # Pre-bake per-block release coefficients from each band's e-fold tau.
        # Keeping them as plain tuples (not numpy) — three multiplies per block
        # is faster as scalar ops than allocating tiny arrays.
        self._release: tuple[float, float, float] = tuple(
            math.exp(-dt / s.release_tau_s) for s in _BAND_SHAPES
        )  # type: ignore[assignment]
        self._floor: tuple[float, float, float] = tuple(
            s.noise_floor for s in _BAND_SHAPES
        )  # type: ignore[assignment]
        self._attack: tuple[float, float, float] = tuple(
            s.attack_alpha for s in _BAND_SHAPES
        )  # type: ignore[assignment]
        self._y0: float = 0.0
        self._y1: float = 0.0
        self._y2: float = 0.0
        self.strength: float = float(strength)

    def reset(self) -> None:
        self._y0 = 0.0
        self._y1 = 0.0
        self._y2 = 0.0

    def step(
        self, low: float, mid: float, high: float
    ) -> tuple[float, float, float]:
        s = self.strength
        # Snapshot to a local — strength can be written from another thread
        # mid-call, and we want a single coherent blend for this block.
        if s <= 0.0:
            # Bypass — keep state warm so toggling on doesn't snap from zero.
            self._y0 = low
            self._y1 = mid
            self._y2 = high
            return low, mid, high
        if s > 1.0:
            s = 1.0

        return (
            self._step_band(0, low, s),
            self._step_band(1, mid, s),
            self._step_band(2, high, s),
        )

    def _step_band(self, i: int, x: float, s: float) -> float:
        # Manual indexing on tuples — measurably faster than getattr / dict
        # lookup at this call rate. The three bands could be unrolled, but
        # the loop is already cache-resident.
        if i == 0:
            y = self._y0
        elif i == 1:
            y = self._y1
        else:
            y = self._y2

        if x >= y:
            a = self._attack[i]
            y = x if a <= 0.0 else (1.0 - a) * x + a * y
        else:
            y = y * self._release[i]

        if i == 0:
            self._y0 = y
        elif i == 1:
            self._y1 = y
        else:
            self._y2 = y

        floor = self._floor[i]
        if y <= floor:
            cleaned = 0.0
        else:
            cleaned = (y - floor) / (1.0 - floor)
            if cleaned > 1.0:
                cleaned = 1.0

        # Lerp raw → cleaned.
        return (1.0 - s) * x + s * cleaned
