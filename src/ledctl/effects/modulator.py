"""Modulators: bind a numeric param to a smoothed scalar source.

A modulator has two parts:
  - source — what produces the raw scalar each frame (audio band, LFO, const)
  - envelope — smooths the raw scalar (asymmetric attack/release one-pole),
               applies gain + a perceptual curve, clamps to [0, 1], and maps
               into [floor, ceiling]

The slot the modulator binds to (brightness / speed / hue_shift) defines the
default attack/release/floor/ceiling so the LLM doesn't have to specify them.
Override is always allowed.
"""

import math
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---- per-slot defaults ----
#
# Picked so the obvious binding "just works" without strobing or feeling sluggish:
#   brightness: snap up on a kick (30 ms attack), fade gracefully (500 ms release).
#   speed: smooth ramps both ways so tempo changes don't jolt.
#   hue_shift: very slow drift; quick changes look like a colour-bug.
# `floor` / `ceiling` here are the *output* range when the source maxes out;
# users typically only override them.
SLOT_DEFAULTS: dict[str, dict[str, float]] = {
    "brightness": {"attack_ms": 30.0, "release_ms": 500.0, "floor": 0.0, "ceiling": 1.0},
    "speed":      {"attack_ms": 200.0, "release_ms": 200.0, "floor": 0.0, "ceiling": 1.0},
    "hue_shift":  {"attack_ms": 200.0, "release_ms": 2000.0, "floor": 0.0, "ceiling": 1.0},
}

ModulatorSource = Literal[
    "const",
    "audio.rms", "audio.low", "audio.mid", "audio.high", "audio.peak",
    "lfo.sin", "lfo.saw", "lfo.triangle", "lfo.pulse",
]


class ModulatorSpec(BaseModel):
    """Source + envelope for a single binding slot."""

    model_config = ConfigDict(extra="forbid")

    source: ModulatorSource = Field(
        ...,
        description=(
            "Where the raw scalar comes from each frame. audio.* reads the live "
            "audio analyser; lfo.* is clock-driven; const is a fixed value."
        ),
    )
    value: float = Field(0.0, description="Used by source=const")
    period_s: float = Field(
        1.0, gt=0.0, description="Cycle duration in seconds for source=lfo.*"
    )
    phase: float = Field(0.0, description="LFO phase offset in cycles [0, 1)")
    duty: float = Field(
        0.5, ge=0.0, le=1.0, description="High fraction for source=lfo.pulse"
    )
    attack_ms: float | None = Field(
        None, ge=0.0,
        description="Smoothing time constant on rising values; slot default if null",
    )
    release_ms: float | None = Field(
        None, ge=0.0,
        description="Smoothing time constant on falling values; slot default if null",
    )
    floor: float | None = Field(
        None,
        description="Output value when the (normalised) source is at minimum; slot default if null",
    )
    ceiling: float | None = Field(
        None,
        description="Output value when the (normalised) source is at maximum; slot default if null",
    )
    gain: float = Field(
        1.0, ge=0.0,
        description=(
            "Multiplier applied to the raw source before clamping; "
            "raise it for quiet rooms"
        ),
    )
    curve: Literal["linear", "sqrt", "square"] = Field(
        "linear",
        description=(
            "Perceptual shaping after gain: sqrt = punchier on quiet input, "
            "square = lazier"
        ),
    )


@dataclass
class Envelope:
    """Stateful smoothing + mapping for a single Modulator binding."""

    spec: ModulatorSpec
    slot: str
    attack_ms: float = 0.0
    release_ms: float = 0.0
    floor: float = 0.0
    ceiling: float = 1.0
    _value: float = 0.0
    _last_t: float | None = None

    def __post_init__(self) -> None:
        defaults = SLOT_DEFAULTS[self.slot]
        s = self.spec
        attack = s.attack_ms if s.attack_ms is not None else defaults["attack_ms"]
        release = s.release_ms if s.release_ms is not None else defaults["release_ms"]
        self.attack_ms = float(attack)
        self.release_ms = float(release)
        self.floor = float(s.floor if s.floor is not None else defaults["floor"])
        self.ceiling = float(s.ceiling if s.ceiling is not None else defaults["ceiling"])

    def step(self, raw: float, t: float) -> float:
        # Asymmetric one-pole smoothing.
        if self._last_t is None or t < self._last_t:
            self._value = raw
        else:
            dt = t - self._last_t
            tau_ms = self.attack_ms if raw > self._value else self.release_ms
            tau_s = tau_ms / 1000.0
            if tau_s > 0.0 and dt > 0.0:
                k = math.exp(-dt / tau_s)
                self._value = self._value * k + raw * (1.0 - k)
            else:
                self._value = raw
        self._last_t = t

        # Gain → curve → clamp [0, 1] → map to [floor, ceiling].
        v = self._value * self.spec.gain
        if self.spec.curve == "sqrt":
            v = math.sqrt(v) if v > 0.0 else 0.0
        elif self.spec.curve == "square":
            v = v * v
        if v < 0.0:
            v = 0.0
        elif v > 1.0:
            v = 1.0
        return self.floor + (self.ceiling - self.floor) * v


def raw_value(spec: ModulatorSpec, t: float, audio_state) -> float:
    """Read the source's instantaneous raw scalar (un-smoothed, ~[0, 1])."""
    src = spec.source
    if src == "const":
        return float(spec.value)
    if src.startswith("audio."):
        if audio_state is None:
            return 0.0
        # `audio.rms` etc. resolve to the rolling-window-normalised value
        # (`rms_norm` etc.) so bindings see ~[0, 1] regardless of mic gain.
        # The raw `<band>` field stays available on AudioState for level-meter
        # display; modulators don't read it directly.
        band = src.split(".", 1)[1]
        return float(getattr(audio_state, f"{band}_norm", 0.0))
    if src.startswith("lfo."):
        kind = src.split(".", 1)[1]
        phase = (t / spec.period_s + spec.phase) % 1.0
        if kind == "sin":
            return 0.5 + 0.5 * math.sin(2.0 * math.pi * phase)
        if kind == "saw":
            return phase
        if kind == "triangle":
            return 1.0 - 2.0 * abs(phase - 0.5)
        if kind == "pulse":
            return 1.0 if phase < spec.duty else 0.0
    return 0.0


class Bindings(BaseModel):
    """Optional modulator bindings on a fixed slot list.

    Adding a new slot is a one-line schema + per-slot default change; the
    fields themselves only need to consume the value they care about (e.g.
    sparkle ignores `speed` because it has no notion of speed).
    """

    model_config = ConfigDict(extra="forbid")
    brightness: ModulatorSpec | None = Field(
        None,
        description=(
            "Multiplies final output. Default attack/release: 30 ms / 500 ms — "
            "fast on kicks, gentle release so it doesn't strobe."
        ),
    )
    speed: ModulatorSpec | None = Field(
        None,
        description=(
            "Replaces the field's static `speed` param. Smoothed slowly (200 ms "
            "both ways) so tempo changes don't jolt. Ignored by fields with no "
            "notion of speed (sparkle)."
        ),
    )
    hue_shift: ModulatorSpec | None = Field(
        None,
        description=(
            "Rotates the palette LUT by N cycles. Default release is slow "
            "(2 s) so colour drift looks deliberate."
        ),
    )
