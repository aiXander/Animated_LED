"""Time-only primitives (one scalar per frame).

Constant, Lfo, AudioBand are leaves. Pulse, Clamp, RangeMap take a child
input — their output_kind matches the input (so a `pulse(scalar_field)` is
itself a scalar_field). Listed under "scalar_t" because that's the canonical
shape — the broadcasting is a convenience.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from ...masters import RenderContext
from ..registry import CompiledNode, OutputKind, Primitive, primitive
from ..shapes import clip_scalar

# --- constant ----------------------------------------------------------------


class _ConstantParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float = Field(0.0, description="Fixed scalar value")


class _CompiledConstant(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_t"

    def __init__(self, value: float):
        self._v = float(value)

    def render(self, ctx: RenderContext) -> float:
        return self._v


@primitive
class Constant(Primitive):
    kind = "constant"
    output_kind = "scalar_t"
    summary = "Fixed scalar value (also produced by bare numeric literals)."
    Params = _ConstantParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledConstant(params.value)


# --- lfo ---------------------------------------------------------------------


class _LfoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shape: Literal["sin", "saw", "triangle", "pulse"] = Field(
        "sin", description="Waveform"
    )
    period_s: float = Field(1.0, gt=0.0, description="Cycle duration in seconds")
    phase: float = Field(0.0, description="Phase offset in cycles [0, 1)")
    duty: float = Field(
        0.5, ge=0.0, le=1.0,
        description="High fraction for shape=pulse",
    )


class _CompiledLfo(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_t"

    def __init__(self, params: _LfoParams):
        self._shape = params.shape
        self._period = float(params.period_s)
        self._phase = float(params.phase)
        self._duty = float(params.duty)

    def render(self, ctx: RenderContext) -> float:
        phase = (ctx.t / self._period + self._phase) % 1.0
        s = self._shape
        if s == "sin":
            return 0.5 + 0.5 * math.sin(2.0 * math.pi * phase)
        if s == "saw":
            return phase
        if s == "triangle":
            return 1.0 - 2.0 * abs(phase - 0.5)
        return 1.0 if phase < self._duty else 0.0


@primitive
class Lfo(Primitive):
    kind = "lfo"
    output_kind = "scalar_t"
    summary = "Clock-driven oscillator. Reads ctx.t (master-speed-scaled)."
    Params = _LfoParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledLfo(params)


# --- audio_band --------------------------------------------------------------


class _AudioBandParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    band: Literal["low", "mid", "high"] = Field(
        ...,
        description=(
            "Which rolling-normalised frequency band to read: "
            "low (20–250 Hz, kick/sub), mid (250 Hz–2 kHz, vocals/snare body), "
            "high (2–12 kHz, hats/cymbals)"
        ),
    )


class _CompiledAudioBand(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_t"

    def __init__(self, band: str):
        self._field = band

    def render(self, ctx: RenderContext) -> float:
        if ctx.audio is None:
            return 0.0
        return float(getattr(ctx.audio, self._field, 0.0))


@primitive
class AudioBand(Primitive):
    kind = "audio_band"
    output_kind = "scalar_t"
    summary = (
        "Auto-scaled frequency band (low/mid/high) from the external audio "
        "server — already smoothed and ~[0, 1] under typical room loudness; "
        "may exceed 1 when masters.audio_reactivity > 1 (clip downstream if "
        "needed). Pick a band that matches the musical element you want; "
        "full-band loudness is intentionally not exposed. All attack/release "
        "and shaping happen upstream in the audio server's UI."
    )
    Params = _AudioBandParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledAudioBand(params.band)


# --- clamp -------------------------------------------------------------------


class _ClampParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Any = Field(..., description="A scalar_t/scalar_field node to clip")
    min: float = Field(0.0, description="Lower bound")
    max: float = Field(1.0, description="Upper bound")


class _CompiledClamp(CompiledNode):
    def __init__(self, child: CompiledNode, lo: float, hi: float):
        self._child = child
        self._lo = float(lo)
        self._hi = float(hi)
        self.output_kind = child.output_kind  # type: ignore[assignment]

    def render(self, ctx: RenderContext) -> Any:
        v = self._child.render(ctx)
        if isinstance(v, np.ndarray):
            return np.clip(v, self._lo, self._hi)
        return clip_scalar(float(v), self._lo, self._hi)


@primitive
class Clamp(Primitive):
    kind = "clamp"
    output_kind = None
    summary = "Clip an input scalar/field to [min, max]. Output kind matches input."
    Params = _ClampParams

    @classmethod
    def compile(cls, params, topology, compiler):
        from ..compiler import CompileError

        child = compiler.compile_child(
            params.input, expect="scalar_field", path="input"
        )
        if params.min > params.max:
            raise CompileError(
                f"clamp.min ({params.min}) > clamp.max ({params.max})"
            )
        return _CompiledClamp(child, params.min, params.max)


# --- range_map ---------------------------------------------------------------


class _RangeMapParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Any = Field(..., description="A scalar_t/scalar_field node")
    in_min: float = Field(0.0)
    in_max: float = Field(1.0)
    out_min: float = Field(0.0)
    out_max: float = Field(1.0)


class _CompiledRangeMap(CompiledNode):
    def __init__(self, child: CompiledNode, params: _RangeMapParams):
        self._child = child
        self.output_kind = child.output_kind  # type: ignore[assignment]
        self._in_lo = float(params.in_min)
        self._in_hi = float(params.in_max)
        self._out_lo = float(params.out_min)
        self._out_hi = float(params.out_max)

    def render(self, ctx: RenderContext) -> Any:
        v = self._child.render(ctx)
        denom = self._in_hi - self._in_lo
        t = 0.0 if denom == 0.0 else (v - self._in_lo) / denom
        return self._out_lo + (self._out_hi - self._out_lo) * t


@primitive
class RangeMap(Primitive):
    kind = "range_map"
    output_kind = None
    summary = "Linearly remap [in_min, in_max] → [out_min, out_max]."
    Params = _RangeMapParams

    @classmethod
    def compile(cls, params, topology, compiler):
        from ..compiler import CompileError

        child = compiler.compile_child(
            params.input, expect="scalar_field", path="input"
        )
        if params.in_min == params.in_max:
            raise CompileError("range_map: in_min must differ from in_max")
        return _CompiledRangeMap(child, params)


# --- pulse -------------------------------------------------------------------


class _PulseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: Any = Field(
        ...,
        description=(
            "Modulator (scalar_t / scalar_field). Usually `audio_band` for "
            "audio-reactive pulsing or `lfo` for shape-driven breathing. "
            "Inputs are clipped to [0, 1] before scaling."
        ),
    )
    floor: float = Field(
        0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Always-visible baseline in [0, 1]. Output = floor when `by` is "
            "0; output = 1.0 when `by` is at peak. floor=0 → full dynamic "
            "range (silence = dark); floor=0.6 → gentle pulse with the "
            "effect always visible; floor=0.9 → barely-there reactivity. "
            "Pick higher floors when you need the layer to stay legible "
            "during quiet passages."
        ),
    )


class _CompiledPulse(CompiledNode):
    def __init__(self, child: CompiledNode, floor: float):
        self._child = child
        self._floor = float(floor)
        self._span = 1.0 - self._floor
        self.output_kind = child.output_kind  # type: ignore[assignment]

    def render(self, ctx: RenderContext) -> Any:
        v = self._child.render(ctx)
        if isinstance(v, np.ndarray):
            return self._floor + self._span * np.clip(v, 0.0, 1.0)
        return self._floor + self._span * clip_scalar(float(v), 0.0, 1.0)


@primitive
class Pulse(Primitive):
    kind = "pulse"
    output_kind = None
    summary = (
        "Sugar for the canonical audio-reactive shape: `floor + (1-floor) * "
        "clip(by, 0, 1)`. Output is `floor` when `by` is 0 and 1.0 at peak — "
        "keeps the effect visible on silence while letting peaks reach "
        "100%. Two params instead of `range_map`'s five. Drop into "
        "`palette_lookup.brightness`, `sparkles.brightness`, or any other "
        "scalar slot you'd otherwise feed a raw modulator into."
    )
    Params = _PulseParams

    @classmethod
    def compile(cls, params, topology, compiler):
        child = compiler.compile_child(
            params.by, expect="scalar_field", path="by"
        )
        return _CompiledPulse(child, params.floor)


# --- step_select -------------------------------------------------------------


class _StepSelectParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: Any = Field(
        ...,
        description=(
            "An integer-ish scalar_t (e.g. `beat_index`). Floored, then "
            "wrapped mod len(values)."
        ),
    )
    values: list[float] = Field(
        ..., min_length=1,
        description="Fixed list to pick from. e.g. [1, -1] for direction flips.",
    )


class _CompiledStepSelect(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_t"

    def __init__(self, child: CompiledNode, values: list[float]):
        self._child = child
        self._values = [float(v) for v in values]
        self._n = len(self._values)

    def render(self, ctx: RenderContext) -> float:
        v = self._child.render(ctx)
        i = int(float(v))
        return self._values[i % self._n]


@primitive
class StepSelect(Primitive):
    kind = "step_select"
    output_kind = "scalar_t"
    summary = (
        "Pick element values[int(index) mod N]. The canonical way to express "
        "direction flips and multi-step value sequences synced to a counter "
        "(typically `beat_index`)."
    )
    Params = _StepSelectParams

    @classmethod
    def compile(cls, params, topology, compiler):
        child = compiler.compile_child(params.index, expect="scalar_t", path="index")
        return _CompiledStepSelect(child, params.values)


# --- audio_beat --------------------------------------------------------------


class _AudioBeatParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _CompiledAudioBeat(CompiledNode):
    """Rising-edge counter on `AudioState.beat_count`.

    Each render returns the number of new beats since the last render
    (typically 0 or 1 at 60 fps; can be ≥ 2 if the upstream onset detector
    fires twice between frames — that should never silently drop). Use as
    a binary multiplier (`> 0`) for one-shot triggers, or feed directly
    into a primitive that interprets it as a count (e.g. `ripple.trigger`).

    Returns 0 while the audio bridge isn't yet receiving `/audio/beat`
    packets — the rest of the visual stack keeps running unaffected."""

    output_kind: ClassVar[OutputKind] = "scalar_t"

    def __init__(self):
        self._last_count: int | None = None

    def render(self, ctx: RenderContext) -> float:
        if ctx.audio is None:
            return 0.0
        cur = int(getattr(ctx.audio, "beat_count", 0))
        if self._last_count is None or cur < self._last_count:
            # First read, or a wraparound — establish baseline silently.
            self._last_count = cur
            return 0.0
        delta = cur - self._last_count
        self._last_count = cur
        return float(delta)


@primitive
class AudioBeat(Primitive):
    kind = "audio_beat"
    output_kind = "scalar_t"
    summary = (
        "PRIMARY beat-sync primitive. Rising-edge trigger from the external "
        "onset detector (`/audio/beat`) — returns the number of new beats "
        "since the last render (0 / 1 typically, occasionally 2). Use this "
        "for ANY 'on the beat' / 'pulse to the rhythm' / 'flash on the kick' "
        "request. Common idioms: feed into `ripple.trigger`, into "
        "`beat_envelope` for flash decay, into `beat_index` for counting. "
        "Returns 0 while the upstream beat publisher isn't live. NEVER "
        "fake beat-sync with a hardcoded-period `lfo` or by thresholding "
        "`audio_band(low)` — both produce drift / mush."
    )
    Params = _AudioBeatParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledAudioBeat()


# --- beat_envelope -----------------------------------------------------------


class _BeatEnvelopeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decay_s: float = Field(
        0.15, gt=0.0, le=10.0,
        description=(
            "Tail length: seconds for the envelope to decay from peak to "
            "near-zero after each beat. Short (0.05–0.15) = sharp strobe / "
            "flash; medium (0.3–0.6) = smooth pump; long (1+) = slow swell. "
            "Honours master speed via ctx.t."
        ),
    )
    hold_s: float = Field(
        0.0, ge=0.0, le=2.0,
        description=(
            "Plateau at peak before decay starts. 0 (default) = instant peak "
            "→ decay. Useful for square-ish hits (hold_s=0.05) or sustained "
            "stabs (hold_s=0.3)."
        ),
    )
    shape: Literal["exp", "linear", "square"] = Field(
        "exp",
        description=(
            "Decay curve. exp = exponential (snappy, natural pulse); linear "
            "= straight ramp from 1 to 0; square = full-on for hold_s then "
            "instant off (hard strobe)."
        ),
    )


class _CompiledBeatEnvelope(CompiledNode):
    """Beat-triggered envelope on the live `/audio/beat` stream.

    Each new beat retriggers the envelope to 1.0; between beats it follows
    `shape` over `decay_s`. Tracks elapsed time via `ctx.t` so master speed
    + freeze interact correctly. Returns 0 while the audio bridge isn't
    yet receiving beats."""

    output_kind: ClassVar[OutputKind] = "scalar_t"

    def __init__(self, params: _BeatEnvelopeParams):
        self._decay = float(params.decay_s)
        self._hold = float(params.hold_s)
        self._shape = params.shape
        self._last_count: int | None = None
        # Large initial age = envelope is at 0 until the first beat lands.
        self._age: float = 1e9
        self._last_t: float | None = None

    def render(self, ctx: RenderContext) -> float:
        dt = 0.0 if self._last_t is None else max(0.0, ctx.t - self._last_t)
        self._last_t = ctx.t

        new_beats = 0
        if ctx.audio is not None:
            cur = int(getattr(ctx.audio, "beat_count", 0))
            if self._last_count is None or cur < self._last_count:
                self._last_count = cur
            else:
                new_beats = cur - self._last_count
                self._last_count = cur

        if new_beats > 0:
            self._age = 0.0
        else:
            self._age += dt

        age = self._age
        if age < self._hold:
            return 1.0
        tail = age - self._hold
        if self._shape == "square":
            return 0.0
        if self._shape == "linear":
            v = 1.0 - tail / self._decay
            return v if v > 0.0 else 0.0
        # exp: e^(-5*tail/decay) — at tail=decay value ≈ 0.0067 (~off).
        return math.exp(-5.0 * tail / self._decay)


@primitive
class BeatEnvelope(Primitive):
    kind = "beat_envelope"
    output_kind = "scalar_t"
    summary = (
        "Per-beat decay envelope: 1.0 on each `/audio/beat` trigger, fades "
        "over `decay_s`. The canonical shape for 'flash on the beat' / "
        "'pulse to the rhythm'. Drop into `palette_lookup.brightness`, "
        "`sparkles.brightness`, etc. Use SHORT decay_s (0.05–0.15) for "
        "strobe-like flashes; medium (0.3–0.6) for a musical pump."
    )
    Params = _BeatEnvelopeParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledBeatEnvelope(params)


# --- beat_index --------------------------------------------------------------


class _BeatIndexParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mod_n: int = Field(
        2, ge=1, le=256,
        description=(
            "Counter wraps mod N. 2 = alternates 0/1 every beat (canonical "
            "for direction flips); 4 = bar count; 8 = phrase position."
        ),
    )


class _CompiledBeatIndex(CompiledNode):
    """Live beat counter mod N — driven by `/audio/beat` rising edges."""

    output_kind: ClassVar[OutputKind] = "scalar_t"

    def __init__(self, mod_n: int):
        self._mod_n = int(mod_n)
        self._last_count: int | None = None
        self._counter: int = 0

    def render(self, ctx: RenderContext) -> float:
        if ctx.audio is None:
            return float(self._counter % self._mod_n)
        cur = int(getattr(ctx.audio, "beat_count", 0))
        if self._last_count is None or cur < self._last_count:
            self._last_count = cur
        else:
            self._counter += cur - self._last_count
            self._last_count = cur
        return float(self._counter % self._mod_n)


@primitive
class BeatIndex(Primitive):
    kind = "beat_index"
    output_kind = "scalar_t"
    summary = (
        "Beat-driven counter mod N. Increments on each `/audio/beat` edge "
        "— actual musical beats, not a hardcoded clock. Pair with "
        "`step_select` for 'every Nth beat' patterns: "
        "step_select(beat_index(mod_n=2), values=[1, -1]) flips direction "
        "every beat."
    )
    Params = _BeatIndexParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledBeatIndex(params.mod_n)
