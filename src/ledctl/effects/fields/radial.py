"""`radial` — a distance-from-point pattern. Rings expanding out, or pulses in."""

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from ...topology import Topology
from ..base import EffectParams
from ..registry import register
from ._shape import apply_shape
from .base import FieldEffect, FieldParams


class RadialParams(FieldParams):
    """Rings centred on a point in normalised coords."""

    center: tuple[float, float, float] = Field(
        (0.0, 0.0, 0.0),
        description="Centre in normalised coords [-1, 1]; (0,0,0) = middle of the install",
    )
    speed: float = Field(
        0.3,
        description="Cycles per second; positive = rings travel outward, negative = inward",
    )
    wavelength: float = Field(
        0.5, gt=0.0,
        description="Cycles per unit normalised distance from centre",
    )
    shape: Literal["cosine", "sawtooth", "pulse", "gauss"] = Field("cosine")
    softness: float = Field(1.0, ge=0.0, le=1.0)
    width: float = Field(0.15, gt=0.0, le=2.0)


@register
class RadialEffect(FieldEffect):
    name: ClassVar[str] = "radial"
    Params: ClassVar[type[EffectParams]] = RadialParams

    def __init__(self, params: RadialParams, topology: Topology):
        super().__init__(params, topology)
        c = np.asarray(params.center, dtype=np.float32)
        diff = topology.normalised_positions - c
        self._dist = np.sqrt(np.sum(diff * diff, axis=1)).astype(np.float32)

    def _render_scalar(
        self, t: float, speed_override: float | None, out: np.ndarray
    ) -> None:
        p: RadialParams = self.params  # type: ignore[assignment]
        speed = float(speed_override) if speed_override is not None else float(p.speed)
        u = self._dist / float(p.wavelength) - speed * t
        phase = u - np.floor(u)
        apply_shape(phase, p.shape, p.softness, p.width, out)
