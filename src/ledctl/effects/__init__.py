"""Public effect surface.

The pipeline is `field × palette × bindings`:
  - field generators (`scroll`, `radial`, `sparkle`, `noise`) produce a scalar
    field over the LED topology
  - a `palette` lifts that scalar to RGB
  - optional modulator `bindings` drive `brightness`, `speed`, or `hue_shift`
    from audio bands or LFOs, with per-slot smoothing applied automatically

Each generator is registered as an `Effect` so the existing engine + REST API
(`POST /effects/{name}`) works unchanged. The `params` schema for any
generator includes `palette` and `bindings`.
"""

from .base import Effect, EffectParams
from .fields import (
    FieldEffect,
    FieldParams,
    NoiseEffect,
    NoiseParams,
    RadialEffect,
    RadialParams,
    ScrollEffect,
    ScrollParams,
    SparkleEffect,
    SparkleParams,
)
from .modulator import Bindings, Envelope, ModulatorSpec, raw_value
from .palette import (
    LUT_SIZE,
    NAMED_PALETTES,
    PaletteSpec,
    PaletteStop,
    compile_lut,
    sample_lut,
)
from .registry import get_effect_class, list_effects, register

__all__ = [
    "Bindings",
    "Effect",
    "EffectParams",
    "Envelope",
    "FieldEffect",
    "FieldParams",
    "LUT_SIZE",
    "ModulatorSpec",
    "NAMED_PALETTES",
    "NoiseEffect",
    "NoiseParams",
    "PaletteSpec",
    "PaletteStop",
    "RadialEffect",
    "RadialParams",
    "ScrollEffect",
    "ScrollParams",
    "SparkleEffect",
    "SparkleParams",
    "compile_lut",
    "get_effect_class",
    "list_effects",
    "raw_value",
    "register",
    "sample_lut",
]
