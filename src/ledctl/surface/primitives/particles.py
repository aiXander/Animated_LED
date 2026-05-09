"""Stateful particle / transient rgb_field primitives.

Each leaf here owns its own per-frame integration (RNG seeds, ripple pools,
fade buffers). Layers using these are stateful — `freeze` halts state
advance because they read `ctx.t`, not wall-clock time.

The three primitives:
  - `comet`:      one head with an exponential trail.
  - `chase_dots`: M evenly-spaced dots scrolling along an axis (stateless).
  - `ripple`:     Poisson-rate-emitted concentric rings; `rate` is a scalar_t
                  so `audio_band` modulates emission directly.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from ...masters import RenderContext
from ...topology import Topology
from .. import palettes as _palettes
from ..registry import CompiledNode, OutputKind, Primitive, primitive
from .scalar_field import _resolve_axis_or_index

# --- comet -------------------------------------------------------------------


class _CometParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: str = Field(
        "u_loop",
        description=(
            "Frame the comet flies along. Use u_loop for around-the-loop "
            "motion, signed_x for left↔right, etc. (see FRAMES)."
        ),
    )
    speed: Any = Field(
        0.3,
        description=(
            "Cycles per second along the axis (scalar_t). Sign sets "
            "direction; modulate via mul(speed, step_select(...)) to flip "
            "direction on the beat."
        ),
    )
    head_size: float = Field(
        0.04, gt=0.0, le=1.0,
        description=(
            "Gaussian sigma of the head in normalised axis units. ~0.02 = "
            "tight pinpoint, ~0.1 = soft glow."
        ),
    )
    trail_length: float = Field(
        0.3, ge=0.0, le=2.0,
        description=(
            "Decay length of the trail behind the head (axis units). 0 = "
            "head only; 0.3 ≈ trail visible across a third of the axis."
        ),
    )
    trail_sparseness: float = Field(
        0.0, ge=0.0, le=1.0,
        description=(
            "0 = solid trail (clean comet); 1 = highly broken-up "
            "(meteor-like). Stochastic per-pixel multiplier on the trail "
            "intensity. Seeded for determinism."
        ),
    )
    trigger: Any = Field(
        0.0,
        description=(
            "OPTIONAL beat-trigger modifier (scalar_t). On each rising edge "
            "(>0) the head jumps back to `spawn_position` and walks LINEARLY "
            "outward (axis treated as non-circular — head exits cleanly "
            "after 1.0 axis units, so non-circular axes like `axial_dist` "
            "and `radius` work correctly). Drive with `audio_beat()` for "
            "'launch / shoot / fire on the beat' behaviour. Pick `speed` "
            "so the head covers the desired distance within one beat "
            "(beat_period × speed = travel per beat; ~2.0 is a good "
            "default at 120 BPM for a full sweep). Default 0 = untriggered "
            "= continuous looping head with circular wrap (the legacy "
            "behaviour, ideal for `u_loop` chasers). Mirrors `ripple.trigger`."
        ),
    )
    spawn_position: float = Field(
        0.0, ge=0.0, le=1.0,
        description=(
            "Axis position the head returns to on each `trigger` rising "
            "edge. 0 (default) = start of axis — for `axial_dist` or "
            "`radius` this is the rig centre, so the comet shoots outward "
            "from the middle on each beat. Ignored when trigger is 0."
        ),
    )
    palette: Any = Field(
        "white",
        description="Palette the comet samples its colour from.",
    )
    palette_pos: Any = Field(
        0.5,
        description="Sample position [0, 1] inside the palette (scalar_t).",
    )
    brightness: Any = Field(
        1.0,
        description="Brightness multiplier in [0, 1] (scalar_field/scalar_t).",
    )
    seed: int = Field(
        0,
        description=(
            "RNG seed for `trail_sparseness`. Multiple comet layers should "
            "use distinct seeds so their grain doesn't lock together."
        ),
    )


class _CompiledComet(CompiledNode):
    output_kind: ClassVar[OutputKind] = "rgb_field"

    def __init__(
        self,
        topology: Topology,
        params: _CometParams,
        speed_node: CompiledNode,
        trigger_node: CompiledNode,
        palette_node: CompiledNode,
        palette_pos_node: CompiledNode,
        brightness_node: CompiledNode,
    ):
        self._n = topology.pixel_count
        self._u = _resolve_axis_or_index(topology, params.axis).astype(
            np.float32, copy=True
        )
        self._head_size = float(params.head_size)
        self._trail_length = float(params.trail_length)
        self._trail_sparseness = float(params.trail_sparseness)
        self._speed = speed_node
        self._trigger = trigger_node
        self._spawn_position = float(params.spawn_position)
        # ctx.t at most-recent rising edge of `trigger`. None = no trigger
        # has fired yet, so the head walks from t=0 (legacy continuous mode).
        self._last_trigger_t: float | None = None
        self._palette = palette_node
        self._palette_pos = palette_pos_node
        self._brightness = brightness_node
        rng = np.random.default_rng(int(params.seed))
        self._noise = rng.random(self._n, dtype=np.float32)
        self._intensity = np.empty(self._n, dtype=np.float32)
        self._out = np.zeros((self._n, 3), dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        speed = float(self._speed.render(ctx))
        # Detect a rising edge on the optional beat-trigger and reset the
        # head to spawn_position. `audio_beat()` returns the count of new
        # beats per frame, so `> 0` is the rising edge.
        trig = float(self._trigger.render(ctx))
        if trig > 0.0:
            self._last_trigger_t = ctx.t
        # Two modes:
        #  - No trigger ever fired (legacy continuous mode): head walks
        #    `(speed * ctx.t) % 1.0` and the axis is treated as CIRCULAR,
        #    so a comet on `u_loop` chases endlessly around the rig.
        #  - After a trigger fires (beat-triggered mode): head walks
        #    LINEARLY from `spawn_position` outward and exits the axis
        #    after travelling 1.0 units. axial_dist / radius (non-circular
        #    axes) only render correctly here — wrap would put the corners
        #    right next to the centre.
        if self._last_trigger_t is None:
            head = (speed * ctx.t) % 1.0
            offset = np.mod(self._u - head + 0.5, 1.0) - 0.5
        else:
            phase_t = max(0.0, ctx.t - self._last_trigger_t)
            head = self._spawn_position + speed * phase_t
            offset = self._u - head

        # Head: gaussian centred at 0 in offset-space.
        sigma = max(self._head_size, 1e-4)
        head_intensity = np.exp(-(offset * offset) / (sigma * sigma)).astype(
            np.float32, copy=False
        )

        # Trail: exponential falloff *behind* the head (sign of speed picks
        # which direction is "behind"). dist_behind > 0 → behind the head.
        sign = 1.0 if speed >= 0 else -1.0
        dist_behind = -sign * offset
        if self._trail_length > 0.0:
            trail_intensity = np.where(
                dist_behind > 0,
                np.exp(-dist_behind / max(self._trail_length, 1e-4)),
                0.0,
            ).astype(np.float32, copy=False)
            if self._trail_sparseness > 0.0:
                # Per-pixel stochastic cuts on the trail (head stays clean).
                cut = 1.0 - self._trail_sparseness * self._noise
                trail_intensity = trail_intensity * cut
        else:
            trail_intensity = np.zeros(self._n, dtype=np.float32)

        np.maximum(head_intensity, trail_intensity, out=self._intensity)

        # Sample palette at palette_pos, modulate by brightness.
        lut = self._palette.render(ctx)
        lut_size = _palettes.LUT_SIZE
        pos = float(self._palette_pos.render(ctx))
        idx = max(0, min(lut_size - 1, int(pos * (lut_size - 1) + 0.5)))
        rgb = lut[idx]

        bright = self._brightness.render(ctx)
        if isinstance(bright, np.ndarray):
            eff = self._intensity * bright.astype(np.float32, copy=False)
        else:
            eff = self._intensity * float(bright)
        self._out[:] = rgb[None, :] * eff[:, None]
        return self._out


@primitive
class Comet(Primitive):
    kind = "comet"
    output_kind = "rgb_field"
    summary = (
        "One comet flying along an axis with a fading trail. Default = "
        "continuous looping walk; pass `trigger: audio_beat()` for "
        "'shoot/fire/launch ON THE BEAT' — the head jumps back to "
        "`spawn_position` on each beat and travels outward again. "
        "head_size = head sigma, trail_length = decay distance, "
        "trail_sparseness = grain (0 solid, 1 meteor). Use `u_loop` for "
        "around-the-loop motion or `axial_dist` for centre→corners "
        "fireballs (4 mirrored fronts on the rectangular rig)."
    )
    Params = _CometParams

    @classmethod
    def compile(cls, params, topology, compiler):
        speed = compiler.compile_child(params.speed, expect="scalar_t", path="speed")
        trigger = compiler.compile_child(
            params.trigger, expect="scalar_t", path="trigger"
        )
        palette = compiler.compile_child(
            params.palette, expect="palette", path="palette"
        )
        ppos = compiler.compile_child(
            params.palette_pos, expect="scalar_t", path="palette_pos"
        )
        brightness = compiler.compile_child(
            params.brightness, expect="scalar_field", path="brightness"
        )
        return _CompiledComet(
            topology, params, speed, trigger, palette, ppos, brightness
        )


# --- chase_dots --------------------------------------------------------------


class _ChaseDotsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: str = Field(
        "u_loop",
        description="Frame the dots travel along (see FRAMES).",
    )
    count: int = Field(
        4, ge=1, le=64,
        description="How many evenly-spaced dots.",
    )
    width: float = Field(
        0.03, gt=0.0, le=0.5,
        description="Gaussian sigma of each dot in normalised axis units.",
    )
    speed: Any = Field(
        0.5,
        description=(
            "Cycles per second of the dot train (scalar_t). Sign sets direction."
        ),
    )
    palette: Any = Field(
        "white",
        description="Palette each dot samples its colour from.",
    )
    palette_pos: Any = Field(
        0.5,
        description=(
            "Sample position [0, 1] inside the palette (scalar_t). Use a "
            "constant for a single colour, an `lfo`/`audio_band` for a "
            "colour walk."
        ),
    )
    palette_spread: float = Field(
        0.0, ge=0.0, le=1.0,
        description=(
            "Spread the dots across this width of the palette around "
            "`palette_pos`. 0 = all dots same colour; 1 = full rainbow "
            "across the dots."
        ),
    )
    brightness: Any = Field(
        1.0,
        description="Brightness multiplier (scalar_field/scalar_t).",
    )


class _CompiledChaseDots(CompiledNode):
    output_kind: ClassVar[OutputKind] = "rgb_field"

    def __init__(
        self,
        topology: Topology,
        params: _ChaseDotsParams,
        speed_node: CompiledNode,
        palette_node: CompiledNode,
        palette_pos_node: CompiledNode,
        brightness_node: CompiledNode,
    ):
        self._n = topology.pixel_count
        self._u = _resolve_axis_or_index(topology, params.axis).astype(
            np.float32, copy=True
        )
        self._count = int(params.count)
        self._sigma = max(float(params.width), 1e-4)
        self._spread = float(params.palette_spread)
        self._speed = speed_node
        self._palette = palette_node
        self._palette_pos = palette_pos_node
        self._brightness = brightness_node
        # Per-dot phase offsets (evenly distributed across the cycle).
        self._dot_offsets = np.arange(self._count, dtype=np.float32) / self._count
        # Per-dot palette offsets across the spread window.
        if self._count > 1 and self._spread > 0.0:
            self._dot_palette_offsets = (
                np.linspace(-0.5, 0.5, self._count, dtype=np.float32) * self._spread
            )
        else:
            self._dot_palette_offsets = np.zeros(self._count, dtype=np.float32)
        self._out = np.zeros((self._n, 3), dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        speed = float(self._speed.render(ctx))
        head = (speed * ctx.t) % 1.0
        # Per-dot positions in [0, 1].
        positions = (head + self._dot_offsets) % 1.0  # (count,)

        # Per-LED, per-dot signed offset on the circular axis.
        u = self._u[:, None]                                    # (n, 1)
        p = positions[None, :]                                  # (1, count)
        offset = np.mod(u - p + 0.5, 1.0) - 0.5                 # (n, count)
        # Gaussian profile.
        intensity = np.exp(-(offset * offset) / (self._sigma * self._sigma))

        lut = self._palette.render(ctx)
        lut_size = _palettes.LUT_SIZE
        base_pos = float(self._palette_pos.render(ctx))
        sample_pos = np.mod(base_pos + self._dot_palette_offsets, 1.0)
        idx = np.minimum(
            (sample_pos * (lut_size - 1) + 0.5).astype(np.int32), lut_size - 1
        )
        dot_rgb = lut[idx]                                       # (count, 3)

        # Sum of (intensity[:, k] * dot_rgb[k]) across k.
        rgb = intensity @ dot_rgb                                # (n, 3)
        np.clip(rgb, 0.0, 1.0, out=rgb)

        bright = self._brightness.render(ctx)
        if isinstance(bright, np.ndarray):
            self._out[:] = rgb * bright.astype(np.float32, copy=False)[:, None]
        else:
            b = float(bright)
            if b == 1.0:
                self._out[:] = rgb
            else:
                self._out[:] = rgb * b
        return self._out


@primitive
class ChaseDots(Primitive):
    kind = "chase_dots"
    output_kind = "rgb_field"
    summary = (
        "M evenly-spaced dots scrolling along an axis. Crisp WLED-style "
        "chase — distinct from `comet` (no trail). Use palette_spread > 0 "
        "to colour each dot a different palette index."
    )
    Params = _ChaseDotsParams

    @classmethod
    def compile(cls, params, topology, compiler):
        speed = compiler.compile_child(params.speed, expect="scalar_t", path="speed")
        palette = compiler.compile_child(
            params.palette, expect="palette", path="palette"
        )
        ppos = compiler.compile_child(
            params.palette_pos, expect="scalar_t", path="palette_pos"
        )
        brightness = compiler.compile_child(
            params.brightness, expect="scalar_field", path="brightness"
        )
        return _CompiledChaseDots(
            topology, params, speed, palette, ppos, brightness
        )


# --- ripple ------------------------------------------------------------------


_RIPPLE_POOL = 12  # max concurrent ripples per layer


class _RippleParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: str = Field(
        "radius",
        description=(
            "Distance frame each ripple expands along. `radius` (default) "
            "= rings around the centre; `axial_dist` = mirrored bars from "
            "centre column; `u_loop` = ripples chasing around the loop."
        ),
    )
    rate: Any = Field(
        2.0,
        description=(
            "Continuous Poisson rate of new ripples per second (scalar_t). "
            "Modulate with `audio_band(low)` for energy-driven rate "
            "spikes — raw signal in its full dynamic range, no manual "
            "thresholding."
        ),
    )
    trigger: Any = Field(
        0.0,
        description=(
            "Deterministic per-event emitter (scalar_t). Each frame, "
            "`int(trigger)` ripples are emitted in addition to the "
            "Poisson process driven by `rate`. The intended source is "
            "`audio_beat()` (rising-edge from the upstream onset detector) "
            "for kick-locked ripples; default 0 disables. **Never** "
            "manually threshold `audio_band(low)` here — the audio server "
            "publishes a clean beat trigger for exactly this purpose."
        ),
    )
    trigger_head_start_s: float = Field(
        0.12, ge=0.0, le=2.0,
        description=(
            "Age (seconds) that trigger-spawned ripples start at, so a "
            "kick-locked ring is already mid-expansion on the first frame "
            "and pops cleanly on the beat instead of fading in from "
            "radius=0. Tune relative to `speed`: head_start × speed = "
            "initial radius. Has no effect on Poisson `rate` emissions, "
            "which keep their natural fade-in. Set to 0 to disable."
        ),
    )
    speed: float = Field(
        0.6, gt=0.0, le=10.0,
        description="Outward propagation speed in normalised axis units / sec.",
    )
    width: float = Field(
        0.05, gt=0.0, le=1.0,
        description="Ring thickness (gaussian sigma in axis units).",
    )
    decay_s: float = Field(
        1.5, gt=0.0, le=20.0,
        description=(
            "Each ripple's lifetime: brightness fades exponentially over "
            "this many seconds, then the slot frees."
        ),
    )
    palette: Any = Field(
        "ice",
        description="Palette each ripple samples its colour from.",
    )
    palette_pos: Any = Field(
        0.6,
        description="Sample position inside the palette (scalar_t).",
    )
    brightness: Any = Field(
        1.0,
        description="Brightness multiplier (scalar_field/scalar_t).",
    )
    seed: int | None = Field(
        None,
        description="RNG seed for the Poisson emission. None = unpredictable.",
    )


class _CompiledRipple(CompiledNode):
    output_kind: ClassVar[OutputKind] = "rgb_field"

    def __init__(
        self,
        topology: Topology,
        params: _RippleParams,
        rate_node: CompiledNode,
        trigger_node: CompiledNode,
        palette_node: CompiledNode,
        palette_pos_node: CompiledNode,
        brightness_node: CompiledNode,
    ):
        self._n = topology.pixel_count
        self._u = _resolve_axis_or_index(topology, params.axis).astype(
            np.float32, copy=True
        )
        self._rate = rate_node
        self._trigger = trigger_node
        self._trigger_head_start = float(params.trigger_head_start_s)
        self._speed = float(params.speed)
        self._sigma = max(float(params.width), 1e-4)
        self._decay = float(params.decay_s)
        self._palette = palette_node
        self._palette_pos = palette_pos_node
        self._brightness = brightness_node
        # Pre-allocated pool of ripple slots; -inf birth = unused.
        self._birth = np.full(_RIPPLE_POOL, -np.inf, dtype=np.float32)
        self._rng = np.random.default_rng(params.seed)
        self._last_t: float | None = None
        self._intensity = np.empty(self._n, dtype=np.float32)
        self._out = np.zeros((self._n, 3), dtype=np.float32)

    def _emit_one(self, birth: float) -> None:
        """Replace the oldest pool slot with a fresh ripple born at `birth`.

        `birth` < `ctx.t` head-starts the ripple: on the next render its age
        is already `ctx.t - birth`, so it appears mid-expansion. Used by the
        trigger path so a kick-locked ring is visible immediately.
        """
        slot = int(np.argmin(self._birth))
        self._birth[slot] = birth

    def render(self, ctx: RenderContext) -> np.ndarray:
        dt = (
            0.0
            if self._last_t is None or ctx.t < self._last_t
            else ctx.t - self._last_t
        )
        self._last_t = ctx.t

        # Deterministic trigger: emit `int(trigger)` ripples regardless of dt.
        # Driven by `audio_beat()` for kick-locked rings; default 0 = disabled.
        # Birth is offset into the past so the first rendered frame already
        # shows the ring at a visible radius (no near-invisible fade-in).
        trig = max(0, int(float(self._trigger.render(ctx))))
        if trig > 0:
            trig_birth = ctx.t - self._trigger_head_start
            for _ in range(trig):
                self._emit_one(trig_birth)

        if dt > 0.0:
            rate = max(0.0, float(self._rate.render(ctx)))
            expected = rate * dt
            if expected > 0.0:
                n_new = int(self._rng.poisson(expected))
                for _ in range(n_new):
                    self._emit_one(ctx.t)

        # Render every active ripple additively.
        out = self._intensity
        out.fill(0.0)
        for slot in range(_RIPPLE_POOL):
            birth = float(self._birth[slot])
            if not np.isfinite(birth):
                continue
            # Clamp tiny negatives from float32 (birth) vs python float (ctx.t)
            # rounding — a ripple emitted on the same frame must still render.
            age = max(0.0, ctx.t - birth)
            if age > self._decay * 5.0:
                continue  # decayed past visibility — free the slot conceptually
            radius = age * self._speed
            d = self._u - radius
            ring = np.exp(-(d * d) / (self._sigma * self._sigma))
            # Brightness envelope: exponential decay over `decay_s`.
            env = float(np.exp(-age / self._decay))
            out += ring.astype(np.float32, copy=False) * env

        np.clip(out, 0.0, 1.0, out=out)

        # Colour mapping.
        lut = self._palette.render(ctx)
        lut_size = _palettes.LUT_SIZE
        pos = float(self._palette_pos.render(ctx))
        idx = max(0, min(lut_size - 1, int(pos * (lut_size - 1) + 0.5)))
        rgb = lut[idx]

        bright = self._brightness.render(ctx)
        if isinstance(bright, np.ndarray):
            eff = out * bright.astype(np.float32, copy=False)
        else:
            eff = out * float(bright)
        self._out[:] = rgb[None, :] * eff[:, None]
        return self._out


@primitive
class Ripple(Primitive):
    kind = "ripple"
    output_kind = "rgb_field"
    summary = (
        "Concentric rings expanding outward along a distance frame "
        "(`radius` for centre rings, `axial_dist` for mirrored bars, "
        "`u_loop` for around-the-loop). Two emission paths: continuous "
        "Poisson `rate` (modulate with `audio_band` for energy-driven "
        "density) and per-event `trigger` (drive with `audio_beat()` for "
        "kick-locked rings). Up to 12 concurrent ripples per layer. "
        "Stateful, uses ctx.t."
    )
    Params = _RippleParams

    @classmethod
    def compile(cls, params, topology, compiler):
        rate = compiler.compile_child(params.rate, expect="scalar_t", path="rate")
        trigger = compiler.compile_child(
            params.trigger, expect="scalar_t", path="trigger"
        )
        palette = compiler.compile_child(
            params.palette, expect="palette", path="palette"
        )
        ppos = compiler.compile_child(
            params.palette_pos, expect="scalar_t", path="palette_pos"
        )
        brightness = compiler.compile_child(
            params.brightness, expect="scalar_field", path="brightness"
        )
        return _CompiledRipple(
            topology, params, rate, trigger, palette, ppos, brightness
        )
