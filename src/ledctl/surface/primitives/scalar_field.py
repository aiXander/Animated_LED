"""Spatial primitives — per-LED scalar fields in [0, 1]."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from ...masters import RenderContext
from ...topology import Topology
from ..registry import CompiledNode, OutputKind, Primitive, primitive
from ..shapes import apply_shape


def _resolve_axis_or_index(topology: Topology, axis: str) -> np.ndarray:
    """Look up a named frame on `topology.derived`.

    Falls back to deriving x/y/z/distance from `normalised_positions` if a
    caller has built a Topology by hand without populating `derived` (some
    older tests do this). Raises CompileError on an unknown name.
    """
    from ..compiler import CompileError

    derived = getattr(topology, "derived", {}) or {}
    if axis in derived:
        return derived[axis]
    if axis in ("x", "y", "z"):
        idx = "xyz".index(axis)
        return ((topology.normalised_positions[:, idx] + 1.0) * 0.5).astype(
            np.float32, copy=True
        )
    if axis == "distance":
        d = np.sqrt(np.sum(topology.normalised_positions ** 2, axis=1))
        return (d / max(float(d.max()), 1e-9)).astype(np.float32, copy=True)
    raise CompileError(
        f"unknown axis {axis!r}; "
        f"choose one of {sorted(derived) if derived else ['x', 'y', 'z']}"
    )


# --- wave --------------------------------------------------------------------


class _WaveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: str = Field(
        "x",
        description=(
            "Frame the pattern travels along. Cartesian: x, y, z. Rig-aware: "
            "u_loop (clockwise around the loop), radius, angle, axial_dist, "
            "side_top, side_bottom, chain_index. See FRAMES."
        ),
    )
    wavelength: float = Field(
        1.0, gt=0.0,
        description="Cycles per full normalised span; 1.0 = one cycle end-to-end",
    )
    speed: Any = Field(
        0.3,
        description="Cycles/sec (scalar_t). Sign sets direction.",
    )
    shape: Literal["cosine", "sawtooth", "pulse", "gauss"] = Field(
        "sawtooth",
        description=(
            "How phase sweeps [0,1] each cycle. "
            "sawtooth = continuous linear flow — DEFAULT, smoothest color across "
            "LEDs, use for flowing/scrolling palettes (esp. cyclic ones like rainbow). "
            "cosine = up/down pulse — color *plateaus* at peaks and troughs because "
            "its derivative is zero there, so use this for breathing brightness with "
            "mono palettes, NOT for smooth color sweeps. "
            "pulse = hard on/off bands. "
            "gauss = single comet pulse per cycle."
        ),
    )
    softness: float = Field(
        1.0, ge=0.0, le=1.0,
        description="cosine only: 0 = hard bands, 1 = fully smooth",
    )
    width: float = Field(
        0.15, gt=0.0, le=2.0,
        description="gauss only: peak width in cycles",
    )
    cross_phase: tuple[float, float, float] = Field(
        (0.0, 0.0, 0.0),
        description=(
            "Per-axis phase offset in cycles per unit normalised position. "
            "(0, 0.15, 0) makes the top row lead the bottom by ~0.3 cycles."
        ),
    )


class _CompiledWave(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(
        self,
        params: _WaveParams,
        topology: Topology,
        speed_node: CompiledNode,
    ):
        self._u_axis = _resolve_axis_or_index(topology, params.axis).astype(
            np.float32, copy=True
        )
        self._wavelength = float(params.wavelength)
        self._shape = params.shape
        self._softness = float(params.softness)
        self._width = float(params.width)
        cp = np.asarray(params.cross_phase, dtype=np.float32)
        self._u_cross: np.ndarray | None = (
            topology.normalised_positions @ cp if np.any(cp) else None
        )
        self._speed = speed_node
        self._scratch = np.empty(topology.pixel_count, dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        speed = float(self._speed.render(ctx))
        u = self._u_axis / self._wavelength - speed * ctx.t
        if self._u_cross is not None:
            u = u + self._u_cross
        phase = u - np.floor(u)
        apply_shape(phase, self._shape, self._softness, self._width, self._scratch)
        return self._scratch


@primitive
class Wave(Primitive):
    kind = "wave"
    output_kind = "scalar_field"
    summary = "1-D travelling pattern along an axis (replaces scroll/wave/gradient/chase)."
    Params = _WaveParams

    @classmethod
    def compile(cls, params, topology, compiler):
        speed = compiler.compile_child(params.speed, expect="scalar_t", path="speed")
        return _CompiledWave(params, topology, speed)


# --- radial ------------------------------------------------------------------


class _RadialParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    center: tuple[float, float, float] = Field(
        (0.0, 0.0, 0.0),
        description="Centre in normalised coords [-1, 1]",
    )
    speed: Any = Field(
        0.3,
        description="Cycles/sec (scalar_t); positive = rings travel outward",
    )
    wavelength: float = Field(
        0.5, gt=0.0,
        description="Cycles per unit normalised distance from centre",
    )
    shape: Literal["cosine", "sawtooth", "pulse", "gauss"] = Field("cosine")
    softness: float = Field(1.0, ge=0.0, le=1.0)
    width: float = Field(0.15, gt=0.0, le=2.0)


class _CompiledRadial(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(
        self,
        params: _RadialParams,
        topology: Topology,
        speed_node: CompiledNode,
    ):
        c = np.asarray(params.center, dtype=np.float32)
        diff = topology.normalised_positions - c
        self._dist = np.sqrt(np.sum(diff * diff, axis=1)).astype(np.float32)
        self._wavelength = float(params.wavelength)
        self._shape = params.shape
        self._softness = float(params.softness)
        self._width = float(params.width)
        self._speed = speed_node
        self._scratch = np.empty(topology.pixel_count, dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        speed = float(self._speed.render(ctx))
        u = self._dist / self._wavelength - speed * ctx.t
        phase = u - np.floor(u)
        apply_shape(phase, self._shape, self._softness, self._width, self._scratch)
        return self._scratch


@primitive
class Radial(Primitive):
    kind = "radial"
    output_kind = "scalar_field"
    summary = "Distance-from-point pattern. Rings expanding out, or pulses in."
    Params = _RadialParams

    @classmethod
    def compile(cls, params, topology, compiler):
        speed = compiler.compile_child(params.speed, expect="scalar_t", path="speed")
        return _CompiledRadial(params, topology, speed)


# --- gradient ----------------------------------------------------------------


class _GradientParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: str = Field(
        "x",
        description=(
            "Frame to ramp along (any registered name; see FRAMES). "
            "Default: Cartesian x."
        ),
    )
    invert: bool = False


class _CompiledGradient(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(self, params: _GradientParams, topology: Topology):
        ramp = _resolve_axis_or_index(topology, params.axis)
        if params.invert:
            ramp = 1.0 - ramp
        self._ramp = ramp.astype(np.float32, copy=True)

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._ramp


@primitive
class Gradient(Primitive):
    kind = "gradient"
    output_kind = "scalar_field"
    summary = "Static linear ramp 0→1 along an axis."
    Params = _GradientParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledGradient(params, topology)


# --- position ----------------------------------------------------------------


class _PositionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: str = Field(
        "x",
        description=(
            "Frame to surface as a [0, 1] field. Legacy x/y/z/distance still "
            "work; the full FRAMES set (u_loop, radius, angle, axial_dist, …) "
            "is also accepted. Prefer `frame` for new code."
        ),
    )


class _CompiledPosition(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(self, params: _PositionParams, topology: Topology):
        self._field = _resolve_axis_or_index(topology, params.axis).astype(
            np.float32, copy=True
        )

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._field


@primitive
class Position(Primitive):
    kind = "position"
    output_kind = "scalar_field"
    summary = (
        "Raw normalised position / frame component as a [0, 1] field "
        "(legacy alias for `frame`)."
    )
    Params = _PositionParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledPosition(params, topology)


# --- frame -------------------------------------------------------------------


class _FrameParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: str = Field(
        "x",
        description=(
            "Name of a registered coordinate frame (see FRAMES). The big "
            "wins for the rectangular rig: u_loop = arc-length around the "
            "loop, axial_dist = |x|, side_top / side_bottom = top/bottom "
            "row mask. Cartesian x / y / z still work as legacy."
        ),
    )


@primitive
class Frame(Primitive):
    kind = "frame"
    output_kind = "scalar_field"
    summary = (
        "Per-LED scalar from a named coordinate frame. The control-surface "
        "vocabulary for spatial layout — wrap any frame in primitives that "
        "take an `axis` (wave / gradient / radial) when you need explicit "
        "control over how the rig is addressed."
    )
    Params = _FrameParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledPosition(
            _PositionParams(axis=params.axis), topology
        )


# --- noise -------------------------------------------------------------------


_NOISE_LATTICE = 64


class _NoiseParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed: Any = Field(
        0.2,
        description="Field flow speed in lattice units per second (scalar_t)",
    )
    scale: Any = Field(
        0.5,
        description="Spatial scale; smaller = larger blobs (scalar_t)",
    )
    octaves: int = Field(1, ge=1, le=4, description="Octaves summed")
    seed: int = Field(0, description="Lattice RNG seed")


class _CompiledNoise(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(
        self,
        params: _NoiseParams,
        topology: Topology,
        speed_node: CompiledNode,
        scale_node: CompiledNode,
    ):
        rng = np.random.default_rng(params.seed)
        self._lattice = rng.random(
            (_NOISE_LATTICE, _NOISE_LATTICE), dtype=np.float32
        )
        self._octaves = int(params.octaves)
        self._x = topology.normalised_positions[:, 0].astype(np.float32, copy=True)
        self._y = topology.normalised_positions[:, 1].astype(np.float32, copy=True)
        self._speed = speed_node
        self._scale = scale_node
        self._scratch = np.empty(topology.pixel_count, dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        speed = float(self._speed.render(ctx))
        base_scale = float(self._scale.render(ctx))
        n = _NOISE_LATTICE
        out = self._scratch
        out.fill(0.0)
        amp = 1.0
        total_amp = 0.0
        for octave in range(self._octaves):
            scale = base_scale * (2 ** octave)
            ox = (self._x * scale * n + speed * ctx.t * n) % n
            oy = (self._y * scale * n + speed * ctx.t * 0.7 * n) % n
            x0 = np.floor(ox).astype(np.int32)
            y0 = np.floor(oy).astype(np.int32)
            x1 = (x0 + 1) % n
            y1 = (y0 + 1) % n
            fx = (ox - x0).astype(np.float32)
            fy = (oy - y0).astype(np.float32)
            v00 = self._lattice[y0, x0]
            v10 = self._lattice[y0, x1]
            v01 = self._lattice[y1, x0]
            v11 = self._lattice[y1, x1]
            v0 = v00 * (1.0 - fx) + v10 * fx
            v1 = v01 * (1.0 - fx) + v11 * fx
            out += amp * (v0 * (1.0 - fy) + v1 * fy)
            total_amp += amp
            amp *= 0.5
        if total_amp > 0.0:
            out /= total_amp
        return out


@primitive
class Noise(Primitive):
    kind = "noise"
    output_kind = "scalar_field"
    summary = (
        "Smooth 2D value-noise field flowing in time. Use as a scalar_field "
        "for blobby washes or to drive palette_lookup. Distinct from "
        "`sparkles` (discrete stamp grain)."
    )
    Params = _NoiseParams

    @classmethod
    def compile(cls, params, topology, compiler):
        speed = compiler.compile_child(params.speed, expect="scalar_t", path="speed")
        scale = compiler.compile_child(params.scale, expect="scalar_t", path="scale")
        return _CompiledNoise(params, topology, speed, scale)


# --- trail -------------------------------------------------------------------


class _TrailParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Any = Field(..., description="A scalar_field node to leave a trail behind")
    decay: Any = Field(
        2.0,
        description="Exponential decay per second of the trail brightness",
    )


class _CompiledTrail(CompiledNode):
    output_kind: ClassVar[OutputKind] = "scalar_field"

    def __init__(
        self,
        topology: Topology,
        child: CompiledNode,
        decay_node: CompiledNode,
    ):
        self._child = child
        self._decay = decay_node
        self._buf = np.zeros(topology.pixel_count, dtype=np.float32)
        self._last_t: float | None = None

    def render(self, ctx: RenderContext) -> np.ndarray:
        new = self._child.render(ctx)
        if isinstance(new, float):
            new = np.full(self._buf.shape, float(new), dtype=np.float32)
        dt = (
            0.0
            if self._last_t is None or ctx.t < self._last_t
            else ctx.t - self._last_t
        )
        self._last_t = ctx.t
        decay = max(0.0, float(self._decay.render(ctx)))
        if dt > 0.0:
            self._buf *= float(np.exp(-decay * dt))
        np.maximum(self._buf, new, out=self._buf)
        return self._buf


@primitive
class Trail(Primitive):
    kind = "trail"
    output_kind = "scalar_field"
    summary = "Fading echo of an input scalar_field. Stateful, uses ctx.t."
    Params = _TrailParams

    @classmethod
    def compile(cls, params, topology, compiler):
        child = compiler.compile_child(params.input, expect="scalar_field", path="input")
        decay = compiler.compile_child(params.decay, expect="scalar_t", path="decay")
        return _CompiledTrail(topology, child, decay)
