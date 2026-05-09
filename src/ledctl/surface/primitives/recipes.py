"""Compound recipes — primitives that compile to a sub-tree of leaves.

A recipe is a primitive whose `compile()` builds an inner `NodeSpec` and
hands it to the compiler — from the LLM's view it's one node with a small
number of params, but internally it expands to the same primitives the
agent could write by hand.

Recipes carry their own visible vocabulary (palette, period, etc.); the
expansion details live here, not in the prompt. Two for v1: `breathing`
and `strobe`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..registry import CompiledNode, OutputKind, Primitive, primitive


def _expand_recipe(
    expansion: dict, compiler, expect: OutputKind = "rgb_field"
) -> CompiledNode:
    """Compile a recipe's NodeSpec dict via the parent compiler.

    The expansion is a fully-formed NodeSpec dict — same shape the LLM
    would emit. We coerce + compile it through the existing pipeline so
    the type-checker, error paths, and schema all behave identically.
    """
    return compiler.compile_child(expansion, expect=expect, path="(recipe)")


# --- breathing ---------------------------------------------------------------


class _BreathingParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    palette: Any = Field(
        "warm",
        description="Palette / colour to breathe.",
    )
    period_s: float = Field(
        4.0, gt=0.0, le=120.0,
        description="Seconds per full breath in/out cycle.",
    )
    floor: float = Field(
        0.3, ge=0.0, le=1.0,
        description=(
            "Always-visible baseline brightness (0 = full black at trough, "
            "1 = no breath). Typical: 0.2–0.4 for a soft visible breath."
        ),
    )
    palette_pos: float = Field(
        0.5, ge=0.0, le=1.0,
        description="Where in the palette to sample the breathing colour.",
    )


@primitive
class Breathing(Primitive):
    kind = "breathing"
    output_kind = "rgb_field"
    summary = (
        "Smooth in/out brightness pulse on a single palette colour. "
        "Expands to palette_lookup(scalar=palette_pos, palette, "
        "brightness=pulse(lfo(sin), floor)). Use for low-energy ambient "
        "moments."
    )
    Params = _BreathingParams

    @classmethod
    def compile(cls, params, topology, compiler):
        expansion = {
            "kind": "palette_lookup",
            "params": {
                "scalar": {
                    "kind": "constant",
                    "params": {"value": float(params.palette_pos)},
                },
                "palette": params.palette,
                "brightness": {
                    "kind": "pulse",
                    "params": {
                        "by": {
                            "kind": "lfo",
                            "params": {
                                "shape": "sin",
                                "period_s": float(params.period_s),
                            },
                        },
                        "floor": float(params.floor),
                    },
                },
            },
        }
        return _expand_recipe(expansion, compiler)


# --- strobe ------------------------------------------------------------------


class _StrobeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decay_s: float = Field(
        0.08, gt=0.0, le=2.0,
        description=(
            "Flash duration: seconds for each beat-triggered flash to "
            "decay from peak to dark. Short (0.04–0.1) = sharp strobe; "
            "longer (0.2–0.4) = soft pulse-on-beat."
        ),
    )
    shape: Literal["exp", "linear", "square"] = Field(
        "exp",
        description=(
            "Decay curve. exp = snappy natural decay; linear = ramp; "
            "square = full-on for the hold then instant off (hardest "
            "strobe — pair with hold_s)."
        ),
    )
    hold_s: float = Field(
        0.0, ge=0.0, le=1.0,
        description=(
            "Plateau at peak before decay starts. 0 = instant peak → "
            "decay; 0.05 with shape=square = a crisp 50ms square flash."
        ),
    )
    palette: Any = Field(
        "white",
        description="Palette flash colour is sampled from.",
    )
    palette_pos: float = Field(
        0.5, ge=0.0, le=1.0,
        description="Where in the palette to sample the flash colour.",
    )


@primitive
class Strobe(Primitive):
    kind = "strobe"
    output_kind = "rgb_field"
    summary = (
        "Beat-driven strobe — flashes on each `/audio/beat` trigger from "
        "the audio server (real musical beats, NOT a hardcoded BPM clock). "
        "Expands to palette_lookup(scalar=pos, palette, "
        "brightness=beat_envelope(decay_s, hold_s, shape)). Goes silent "
        "when no audio is connected."
    )
    Params = _StrobeParams

    @classmethod
    def compile(cls, params, topology, compiler):
        expansion = {
            "kind": "palette_lookup",
            "params": {
                "scalar": {
                    "kind": "constant",
                    "params": {"value": float(params.palette_pos)},
                },
                "palette": params.palette,
                "brightness": {
                    "kind": "beat_envelope",
                    "params": {
                        "decay_s": float(params.decay_s),
                        "hold_s": float(params.hold_s),
                        "shape": params.shape,
                    },
                },
            },
        }
        return _expand_recipe(expansion, compiler)
