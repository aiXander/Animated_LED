"""Palette primitives — produce a 256-entry RGB LUT."""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...masters import RenderContext
from ..palettes import (
    NAMED_PALETTES,
    _lut_from_hsv_stops,
    _lut_from_named,
    _lut_from_stops,
)
from ..registry import CompiledNode, OutputKind, Primitive, primitive
from ..shapes import hex_to_rgb01

# --- palette_named -----------------------------------------------------------


class _PaletteNamedParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(
        ...,
        description=(
            "rainbow, fire, ice, sunset, ocean, warm, white, black, "
            "or mono_<hex> for a single colour"
        ),
    )

    @model_validator(mode="after")
    def _validate_name(self) -> _PaletteNamedParams:
        n = self.name
        if n in NAMED_PALETTES:
            return self
        if n.startswith("mono_"):
            hex_to_rgb01(n[5:])
            return self
        raise ValueError(
            f"unknown palette {n!r}; choose one of {sorted(NAMED_PALETTES)} "
            f"or mono_<hex>"
        )


class _CompiledPaletteNamed(CompiledNode):
    output_kind: ClassVar[OutputKind] = "palette"

    def __init__(self, name: str):
        self._lut = _lut_from_named(name)

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._lut


@primitive
class PaletteNamed(Primitive):
    kind = "palette_named"
    output_kind = "palette"
    summary = (
        "Named LUT (rainbow / fire / ice / sunset / ocean / warm / white / "
        "black / mono_<hex>). `rainbow` is HSV-baked at uniform brightness; "
        "the others encode brightness on purpose. Bare strings in node "
        "fields are sugar for this primitive."
    )
    Params = _PaletteNamedParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledPaletteNamed(params.name)


# --- palette_stops -----------------------------------------------------------


class _PaletteStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pos: float = Field(..., ge=0.0, le=1.0)
    color: str = Field(..., description="Hex colour at this stop")

    @model_validator(mode="after")
    def _check_color(self) -> _PaletteStop:
        hex_to_rgb01(self.color)
        return self


class _PaletteStopsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stops: list[_PaletteStop] = Field(..., min_length=2)


class _CompiledPaletteStops(CompiledNode):
    output_kind: ClassVar[OutputKind] = "palette"

    def __init__(self, params: _PaletteStopsParams):
        self._lut = _lut_from_stops([s.model_dump() for s in params.stops])

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._lut


@primitive
class PaletteStops(Primitive):
    kind = "palette_stops"
    output_kind = "palette"
    summary = "Custom palette from explicit (pos, color) stops (>= 2)."
    Params = _PaletteStopsParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledPaletteStops(params)


# --- palette_hsv -------------------------------------------------------------


class _PaletteHsvStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pos: float = Field(..., ge=0.0, le=1.0)
    hue: float = Field(
        ...,
        description=(
            "Hue in degrees: 0=red, 60=yellow, 120=green, 180=cyan, "
            "240=blue, 300=magenta. Values can exceed 360 (or go negative) "
            "for multi-cycle / direction-controlled sweeps; e.g. stops "
            "hue=0 and hue=360 walk the full chromatic circle the long way, "
            "hue=0 and hue=-180 go red->magenta->blue."
        ),
    )
    sat: float = Field(
        1.0, ge=0.0, le=1.0,
        description="Saturation (default 1 = pure colour, 0 = grey).",
    )
    val: float = Field(
        1.0, ge=0.0, le=1.0,
        description=(
            "HSV value (default 1 = max). Use < 1 only when you want this "
            "specific stop in the palette to be intrinsically darker than "
            "another. Layer opacity and the master brightness slider handle "
            "runtime dimming — there is no per-primitive brightness knob."
        ),
    )


class _PaletteHsvParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stops: list[_PaletteHsvStop] = Field(..., min_length=2)


class _CompiledPaletteHsv(CompiledNode):
    output_kind: ClassVar[OutputKind] = "palette"

    def __init__(self, params: _PaletteHsvParams):
        self._lut = _lut_from_hsv_stops([s.model_dump() for s in params.stops])

    def render(self, ctx: RenderContext) -> np.ndarray:
        return self._lut


@primitive
class PaletteHsv(Primitive):
    kind = "palette_hsv"
    output_kind = "palette"
    summary = (
        "Custom palette baked by HSV interpolation between hue stops. The "
        "LUT walks the chromatic surface so brightness stays uniform and "
        "complementary-colour midpoints stay saturated (no muddy/grey runs "
        "you'd get from RGB-space lerp). Prefer this over `palette_stops` "
        "whenever the palette is meant to encode colour at uniform brightness — "
        "the master slider and layer opacity handle dimming."
    )
    Params = _PaletteHsvParams

    @classmethod
    def compile(cls, params, topology, compiler):
        return _CompiledPaletteHsv(params)
