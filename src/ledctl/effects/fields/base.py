"""Field-effect base class.

A `FieldEffect` decomposes a render into three composable steps:
  1. produce a scalar field f(x, y, z, t) ∈ [0, 1] over the LED topology
  2. look up RGB through a palette LUT (with optional hue rotation)
  3. multiply by an optional brightness modulator

Subclasses only implement `_render_scalar` — palette + modulator handling is
shared, so a new generator is "what scalar field, in what spatial pattern,
over time." That's the surface the LLM-facing API exposes.
"""

from abc import abstractmethod
from typing import ClassVar

import numpy as np
from pydantic import Field

from ...topology import Topology
from ..base import Effect, EffectParams
from ..modulator import Bindings, Envelope, raw_value
from ..palette import PaletteSpec, compile_lut, sample_lut


class FieldParams(EffectParams):
    """Common params shared by every field generator."""

    palette: PaletteSpec = Field(
        default_factory=lambda: PaletteSpec(name="white"),
        description=(
            "Either a named palette string (rainbow, fire, ice, sunset, ocean, "
            "warm, white, mono_<hex>) or a custom list of stops."
        ),
    )
    bindings: Bindings = Field(
        default_factory=Bindings,
        description="Optional modulator bindings on brightness, speed, hue_shift.",
    )


class FieldEffect(Effect):
    """Abstract base — subclasses implement `_render_scalar`."""

    name: ClassVar[str] = "_field_base"
    Params: ClassVar[type[FieldParams]] = FieldParams

    def __init__(self, params: FieldParams, topology: Topology):
        super().__init__(params, topology)
        self._lut = compile_lut(params.palette)
        self._scalar = np.empty(topology.pixel_count, dtype=np.float32)
        self._env_brightness = (
            Envelope(spec=params.bindings.brightness, slot="brightness")
            if params.bindings.brightness is not None
            else None
        )
        self._env_speed = (
            Envelope(spec=params.bindings.speed, slot="speed")
            if params.bindings.speed is not None
            else None
        )
        self._env_hue = (
            Envelope(spec=params.bindings.hue_shift, slot="hue_shift")
            if params.bindings.hue_shift is not None
            else None
        )

    @abstractmethod
    def _render_scalar(
        self, t: float, speed_override: float | None, out: np.ndarray
    ) -> None:
        """Write per-LED scalar field into `out` (shape (N,), values in [0, 1]).

        `speed_override` is the resolved value of the `speed` modulator slot
        (None if not bound). Subclasses with no notion of speed may ignore it.
        """

    def _resolved(self, env: Envelope | None, t: float) -> float | None:
        if env is None:
            return None
        return env.step(raw_value(env.spec, t, self.topology.audio_state), t)

    def render(self, t: float, out: np.ndarray) -> None:
        speed_override = self._resolved(self._env_speed, t)
        self._render_scalar(t, speed_override, self._scalar)
        hue = self._resolved(self._env_hue, t) or 0.0
        rgb = sample_lut(self._lut, self._scalar, hue)
        brightness = self._resolved(self._env_brightness, t)
        if brightness is None:
            out[:] = rgb
        else:
            out[:] = rgb * brightness
