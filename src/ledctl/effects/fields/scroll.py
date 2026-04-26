"""`scroll` — a 1-D pattern that travels along an axis.

Replaces what used to be three separate effects: `wave` (shape=cosine),
`gradient` (shape=sawtooth), `chase` (shape=gauss with small width). The shape
is now a parameter, not a class.
"""

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from ...topology import Topology
from ..base import EffectParams
from ..registry import register
from ._shape import apply_shape
from .base import FieldEffect, FieldParams


class ScrollParams(FieldParams):
    """A 1-D pattern travelling along an axis.

    `shape` picks the pattern: cosine = wave, sawtooth = scrolling gradient,
    pulse = on/off bands, gauss = comet/chase. `cross_phase` shifts the pattern
    perpendicular to the travel axis — `(0, 0.15, 0)` makes the top row lead
    the bottom by ~0.3 cycles across a full y-span.
    """

    axis: Literal["x", "y", "z"] = Field(
        "x", description="Axis the pattern travels along"
    )
    speed: float = Field(
        0.3,
        description=(
            "Cycles per second; sign sets direction. axis=x: positive = "
            "stage-right, negative = stage-left."
        ),
    )
    wavelength: float = Field(
        1.0, gt=0.0,
        description="Cycles per full normalised span; 1.0 = one cycle end-to-end",
    )
    shape: Literal["cosine", "sawtooth", "pulse", "gauss"] = Field(
        "cosine",
        description="cosine = wave, sawtooth = gradient, pulse = bands, gauss = comet",
    )
    softness: float = Field(
        1.0, ge=0.0, le=1.0,
        description="cosine only: 0 = hard bands, 1 = fully smooth",
    )
    width: float = Field(
        0.15, gt=0.0, le=2.0,
        description="gauss only: peak width in cycles (smaller = sharper comet)",
    )
    cross_phase: tuple[float, float, float] = Field(
        (0.0, 0.0, 0.0),
        description=(
            "Per-axis phase offset added to the pattern u-coord, in cycles per "
            "unit of normalised position. e.g. (0, 0.15, 0) makes the top row "
            "lead the bottom by ~0.3 cycles across the full y-span."
        ),
    )


@register
class ScrollEffect(FieldEffect):
    name: ClassVar[str] = "scroll"
    Params: ClassVar[type[EffectParams]] = ScrollParams

    def __init__(self, params: ScrollParams, topology: Topology):
        super().__init__(params, topology)
        axis_idx = "xyz".index(params.axis)
        # Map [-1, 1] → [0, 1] along the chosen axis.
        self._u_axis = (topology.normalised_positions[:, axis_idx] + 1.0) * 0.5
        cp = np.asarray(params.cross_phase, dtype=np.float32)
        self._u_cross = topology.normalised_positions @ cp if np.any(cp) else None

    def _render_scalar(
        self, t: float, speed_override: float | None, out: np.ndarray
    ) -> None:
        p: ScrollParams = self.params  # type: ignore[assignment]
        speed = float(speed_override) if speed_override is not None else float(p.speed)
        u = self._u_axis / float(p.wavelength) - speed * t
        if self._u_cross is not None:
            u = u + self._u_cross
        phase = u - np.floor(u)
        apply_shape(phase, p.shape, p.softness, p.width, out)
