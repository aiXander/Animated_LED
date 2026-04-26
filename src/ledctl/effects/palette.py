"""Palettes: scalar in [0, 1] → RGB.

A `PaletteSpec` is either a named palette ("rainbow", "fire", "mono_<hex>", ...)
or a list of custom stops. The LLM-facing API accepts either form, including a
bare string shorthand: `"palette": "fire"` is the same as `"palette": {"name": "fire"}`.
We bake to a 256-entry LUT once at construction; per-frame lookup is one numpy
index op.
"""

from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._color import hex_to_rgb01

LUT_SIZE = 256


class PaletteStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pos: float = Field(..., ge=0.0, le=1.0, description="Stop position in [0, 1]")
    color: str = Field(..., description="Hex colour at this stop")

    @field_validator("color")
    @classmethod
    def _validate_hex(cls, v: str) -> str:
        hex_to_rgb01(v)
        return v


# Canonical named palettes. Stops are (pos, hex). Endpoints chosen so colours
# don't pop when a scrolling pattern wraps.
NAMED_PALETTES: dict[str, list[tuple[float, str]]] = {
    "rainbow": [
        (0.000, "#ff0000"),
        (0.167, "#ffff00"),
        (0.333, "#00ff00"),
        (0.500, "#00ffff"),
        (0.667, "#0000ff"),
        (0.833, "#ff00ff"),
        (1.000, "#ff0000"),
    ],
    "fire": [
        (0.00, "#000000"),
        (0.25, "#600000"),
        (0.50, "#ff3000"),
        (0.75, "#ffa000"),
        (1.00, "#ffff80"),
    ],
    "ice": [
        (0.0, "#000010"),
        (0.4, "#003080"),
        (0.7, "#00a0e0"),
        (1.0, "#ffffff"),
    ],
    "sunset": [
        (0.0, "#100030"),
        (0.4, "#c02060"),
        (0.7, "#ff7020"),
        (1.0, "#ffe080"),
    ],
    "ocean": [
        (0.0, "#001020"),
        (0.4, "#006080"),
        (0.7, "#20a0c0"),
        (1.0, "#c0f0ff"),
    ],
    "warm": [
        (0.0, "#ff3000"),
        (0.5, "#ffa000"),
        (1.0, "#ff5000"),
    ],
    "white": [(0.0, "#ffffff"), (1.0, "#ffffff")],
    "black": [(0.0, "#000000"), (1.0, "#000000")],
}


class PaletteSpec(BaseModel):
    """Named palette OR a list of custom stops; exactly one must be set.

    Bare-string shorthand: `"fire"` is normalised to `{"name": "fire"}`. Use
    `mono_<hex>` (e.g. `mono_ff7000`) for a single-colour palette without
    spelling out two stops.
    """

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(
        None,
        description=(
            "Named palette: rainbow, fire, ice, sunset, ocean, warm, white, "
            "black, or mono_<hex> for a single colour"
        ),
    )
    stops: list[PaletteStop] | None = Field(
        None, description="Custom palette as a sorted list of (pos, color) stops"
    )

    @model_validator(mode="before")
    @classmethod
    def _string_shorthand(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"name": v}
        return v

    @model_validator(mode="after")
    def _exactly_one(self) -> "PaletteSpec":
        if (self.name is None) == (self.stops is None):
            raise ValueError("PaletteSpec needs exactly one of `name` or `stops`")
        if self.stops is not None and len(self.stops) < 2:
            raise ValueError("custom palette needs at least 2 stops")
        return self

    @field_validator("name")
    @classmethod
    def _known_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v in NAMED_PALETTES:
            return v
        if v.startswith("mono_"):
            hex_to_rgb01(v[5:])  # raises if the hex is malformed
            return v
        raise ValueError(
            f"unknown palette {v!r}; choose one of {sorted(NAMED_PALETTES)} "
            f"or mono_<hex>"
        )


def compile_lut(spec: PaletteSpec) -> np.ndarray:
    """Bake a PaletteSpec into a (LUT_SIZE, 3) float32 LUT."""
    if spec.stops is not None:
        sorted_stops = sorted(spec.stops, key=lambda s: s.pos)
        positions = np.array([s.pos for s in sorted_stops], dtype=np.float32)
        colors = np.stack([hex_to_rgb01(s.color) for s in sorted_stops])
    elif spec.name is not None and spec.name.startswith("mono_"):
        rgb = hex_to_rgb01(spec.name[5:])
        positions = np.array([0.0, 1.0], dtype=np.float32)
        colors = np.stack([rgb, rgb])
    else:
        assert spec.name is not None
        named = NAMED_PALETTES[spec.name]
        positions = np.array([p for p, _ in named], dtype=np.float32)
        colors = np.stack([hex_to_rgb01(c) for _, c in named])

    x = np.linspace(0.0, 1.0, LUT_SIZE, dtype=np.float32)
    lut = np.empty((LUT_SIZE, 3), dtype=np.float32)
    for ch in range(3):
        lut[:, ch] = np.interp(x, positions, colors[:, ch])
    return lut


def sample_lut(lut: np.ndarray, t: np.ndarray, hue_shift: float = 0.0) -> np.ndarray:
    """Sample an LUT (256, 3) at scalar values `t` (N,), optionally rotated by
    `hue_shift` cycles (wraps mod 1 so the palette stays continuous)."""
    t = (t + hue_shift) % 1.0 if hue_shift != 0.0 else np.clip(t, 0.0, 1.0)
    idx = np.minimum(
        (t * (LUT_SIZE - 1) + 0.5).astype(np.int32),
        LUT_SIZE - 1,
    )
    return lut[idx]
