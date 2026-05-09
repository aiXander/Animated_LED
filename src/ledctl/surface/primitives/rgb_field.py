"""rgb_field primitives — the layer leaves that produce per-LED RGB."""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from ...masters import RenderContext
from ...topology import Topology
from .. import palettes as _palettes
from ..registry import CompiledNode, OutputKind, Primitive, primitive
from ..shapes import clip_scalar

# --- palette_lookup ----------------------------------------------------------


class _PaletteLookupParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scalar: Any = Field(
        ...,
        description="A scalar_field (or scalar_t broadcast) used to index the palette",
    )
    palette: Any = Field(
        ...,
        description="A palette node, or bare string for palette_named sugar",
    )
    brightness: Any = Field(
        1.0,
        description=(
            "Brightness multiplier in [0, 1]. Default 1.0 = full. For "
            "audio-reactive pulsing, use `pulse(by=audio_band(low|mid|high), "
            "floor=...)` so silence keeps a baseline and peaks reach 1.0. "
            "Avoid raw `audio_band` (silence = dark) and avoid static < 1 "
            "(use layer `opacity` for static dimming, master for global)."
        ),
    )
    hue_shift: Any = Field(
        0.0,
        description="Rotate the palette LUT by N cycles (scalar_t or scalar_field).",
    )


class _CompiledPaletteLookup(CompiledNode):
    output_kind: ClassVar[OutputKind] = "rgb_field"

    def __init__(
        self,
        topology: Topology,
        scalar_node: CompiledNode,
        palette_node: CompiledNode,
        brightness_node: CompiledNode,
        hue_shift_node: CompiledNode,
    ):
        self._n = topology.pixel_count
        self._scalar = scalar_node
        self._palette = palette_node
        self._brightness = brightness_node
        self._hue_shift = hue_shift_node
        self._out = np.zeros((self._n, 3), dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        lut = self._palette.render(ctx)
        s = self._scalar.render(ctx)
        if isinstance(s, float):
            s = np.full(self._n, s, dtype=np.float32)
        hue = self._hue_shift.render(ctx)
        if isinstance(hue, np.ndarray):
            t = (s + hue) % 1.0
        elif float(hue) != 0.0:
            t = (s + float(hue)) % 1.0
        else:
            t = np.clip(s, 0.0, 1.0)
        lut_size = _palettes.LUT_SIZE
        idx = np.minimum(
            (t * (lut_size - 1) + 0.5).astype(np.int32),
            lut_size - 1,
        )
        rgb = lut[idx]
        bright = self._brightness.render(ctx)
        if isinstance(bright, np.ndarray):
            self._out[:] = rgb * bright[:, None]
        else:
            b = float(bright)
            if b == 1.0:
                self._out[:] = rgb
            else:
                self._out[:] = rgb * b
        return self._out


@primitive
class PaletteLookup(Primitive):
    kind = "palette_lookup"
    output_kind = "rgb_field"
    summary = (
        "Sample a palette LUT with a scalar field. For audio reactivity wrap "
        "the modulator in `pulse(by, floor)` and pass that to `brightness` — "
        "static dimming is owned by layer `opacity` and the master slider."
    )
    Params = _PaletteLookupParams

    @classmethod
    def compile(cls, params, topology, compiler):
        scalar = compiler.compile_child(params.scalar, expect="scalar_field", path="scalar")
        palette = compiler.compile_child(params.palette, expect="palette", path="palette")
        brightness = compiler.compile_child(
            params.brightness, expect="scalar_field", path="brightness"
        )
        hue_shift = compiler.compile_child(
            params.hue_shift, expect="scalar_field", path="hue_shift"
        )
        return _CompiledPaletteLookup(topology, scalar, palette, brightness, hue_shift)


# --- solid -------------------------------------------------------------------


class _SolidParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rgb: tuple[float, float, float] = Field(
        ..., description="Uniform colour as (r, g, b) in [0, 1]"
    )


class _CompiledSolid(CompiledNode):
    output_kind: ClassVar[OutputKind] = "rgb_field"

    def __init__(self, topology: Topology, rgb: tuple[float, float, float]):
        self._out = np.tile(
            np.asarray(rgb, dtype=np.float32), (topology.pixel_count, 1)
        )

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._out


@primitive
class Solid(Primitive):
    kind = "solid"
    output_kind = "rgb_field"
    summary = "Uniform colour. Cheaper than palette_lookup for plain washes."
    Params = _SolidParams

    @classmethod
    def compile(cls, params, topology, compiler):
        rgb = tuple(clip_scalar(float(v), 0.0, 1.0) for v in params.rgb)
        return _CompiledSolid(topology, rgb)  # type: ignore[arg-type]


# --- sparkles ----------------------------------------------------------------


class _SparklesParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    palette: Any = Field(
        "mono_ffffff",
        description=(
            "Palette each stamp samples its colour from (palette node or "
            "bare string sugar). Default white = classic sparkle."
        ),
    )
    density: Any = Field(
        0.3,
        description=(
            "New sparkles per LED per second (scalar_t). Higher = busier "
            "grain; combine with `decay` to set steady-state coverage."
        ),
    )
    decay: Any = Field(
        2.0,
        description=(
            "Exponential brightness decay per second (scalar_t). Higher = "
            "shorter pixels; 0 keeps every stamp lit."
        ),
    )
    spread: Any = Field(
        0.0,
        description=(
            "Palette window width in [0, 1] each stamp samples from "
            "(wraps mod 1). 0 = single colour at `palette_center`, "
            "1 = full palette. (scalar_t)"
        ),
    )
    palette_center: Any = Field(
        0.5,
        description="Centre of the palette window in [0, 1] (scalar_t).",
    )
    brightness: Any = Field(
        1.0,
        description=(
            "Brightness multiplier in [0, 1]. Default 1.0 = full. For "
            "audio-reactive sparkles, use `pulse(by=audio_band(...), "
            "floor=...)` so silent passages still twinkle and peaks pop. "
            "Avoid raw `audio_band` (silence = invisible) and avoid static "
            "< 1 (use layer `opacity` for static dimming)."
        ),
    )
    seed: int | None = Field(None, description="RNG seed; None = unpredictable.")


class _CompiledSparkles(CompiledNode):
    output_kind: ClassVar[OutputKind] = "rgb_field"

    def __init__(
        self,
        topology: Topology,
        palette_node: CompiledNode,
        density_node: CompiledNode,
        decay_node: CompiledNode,
        spread_node: CompiledNode,
        center_node: CompiledNode,
        brightness_node: CompiledNode,
        seed: int | None,
    ):
        self._n = topology.pixel_count
        self._palette = palette_node
        self._density = density_node
        self._decay = decay_node
        self._spread = spread_node
        self._center = center_node
        self._brightness = brightness_node
        self._rng = np.random.default_rng(seed)
        self._intensity = np.zeros(self._n, dtype=np.float32)
        self._palette_idx = np.zeros(self._n, dtype=np.float32)
        self._last_t: float | None = None
        self._out = np.zeros((self._n, 3), dtype=np.float32)

    def render(self, ctx: RenderContext) -> np.ndarray:
        dt = (
            0.0
            if self._last_t is None or ctx.t < self._last_t
            else ctx.t - self._last_t
        )
        self._last_t = ctx.t
        density = max(0.0, float(self._density.render(ctx)))
        decay = max(0.0, float(self._decay.render(ctx)))
        if dt > 0.0:
            self._intensity *= float(np.exp(-decay * dt))
            expected = density * self._n * dt
            if expected > 0.0:
                n_new = int(self._rng.poisson(expected))
                if n_new > 0:
                    spread = clip_scalar(
                        float(self._spread.render(ctx)), 0.0, 1.0
                    )
                    center = float(self._center.render(ctx))
                    half = spread * 0.5
                    samples = self._rng.uniform(-half, half, n_new) + center
                    samples = np.mod(samples, 1.0).astype(np.float32)
                    idxs = self._rng.integers(0, self._n, n_new)
                    self._intensity[idxs] = 1.0
                    self._palette_idx[idxs] = samples

        lut = self._palette.render(ctx)
        lut_size = _palettes.LUT_SIZE
        idx = np.minimum(
            (np.clip(self._palette_idx, 0.0, 1.0) * (lut_size - 1) + 0.5).astype(
                np.int32
            ),
            lut_size - 1,
        )
        rgb = lut[idx]
        bright = self._brightness.render(ctx)
        if isinstance(bright, np.ndarray):
            bright_eff = self._intensity * bright.astype(np.float32, copy=False)
        else:
            bright_eff = self._intensity * float(bright)
        self._out[:] = rgb * bright_eff[:, None]
        return self._out


@primitive
class Sparkles(Primitive):
    kind = "sparkles"
    output_kind = "rgb_field"
    summary = (
        "Poisson-stamped twinkles with exponential decay. Layer leaf — each "
        "stamp samples a colour from the palette window (default white). "
        "Stack via blend modes (`add` / `screen` to overlay a base layer). "
        "Stateful, uses ctx.t (so freeze halts decay too). For audio-reactive "
        "twinkle, set `brightness` to `pulse(audio_band(...), floor)`."
    )
    Params = _SparklesParams

    @classmethod
    def compile(cls, params, topology, compiler):
        palette = compiler.compile_child(
            params.palette, expect="palette", path="palette"
        )
        density = compiler.compile_child(
            params.density, expect="scalar_t", path="density"
        )
        decay = compiler.compile_child(
            params.decay, expect="scalar_t", path="decay"
        )
        spread = compiler.compile_child(
            params.spread, expect="scalar_t", path="spread"
        )
        center = compiler.compile_child(
            params.palette_center, expect="scalar_t", path="palette_center"
        )
        brightness = compiler.compile_child(
            params.brightness, expect="scalar_field", path="brightness"
        )
        return _CompiledSparkles(
            topology, palette, density, decay, spread, center, brightness,
            params.seed,
        )
