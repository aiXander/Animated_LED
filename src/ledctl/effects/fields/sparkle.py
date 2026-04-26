"""`sparkle` — random per-LED impulses with exponential decay.

Stays its own field rather than a `scroll` shape because it carries temporal
state (which LEDs are currently lit) — you can't recreate that from a stateless
spatial function.
"""

from typing import ClassVar

import numpy as np
from pydantic import Field

from ...topology import Topology
from ..base import EffectParams
from ..registry import register
from .base import FieldEffect, FieldParams


class SparkleParams(FieldParams):
    """Random twinkles. Palette stop 0 = base colour, stop 1 = peak colour."""

    density: float = Field(
        0.3, ge=0.0, le=10.0,
        description="New sparkles per LED per second (0.05 ≈ each LED lights ~once every 20s)",
    )
    decay: float = Field(
        2.0, gt=0.0,
        description="Exponential decay rate per second; higher = shorter trails",
    )
    seed: int | None = Field(
        None, description="Optional RNG seed (None = unpredictable)"
    )


@register
class SparkleEffect(FieldEffect):
    name: ClassVar[str] = "sparkle"
    Params: ClassVar[type[EffectParams]] = SparkleParams

    def __init__(self, params: SparkleParams, topology: Topology):
        super().__init__(params, topology)
        self._brightness = np.zeros(topology.pixel_count, dtype=np.float32)
        self._rng = np.random.default_rng(params.seed)
        self._last_t: float | None = None

    def _render_scalar(
        self, t: float, speed_override: float | None, out: np.ndarray
    ) -> None:
        # speed_override is ignored: sparkle has no notion of speed. Density
        # could in principle be modulated, but that's a different slot — keep
        # the contract clean.
        p: SparkleParams = self.params  # type: ignore[assignment]
        dt = 0.0 if self._last_t is None else max(0.0, t - self._last_t)
        self._last_t = t
        if dt > 0.0:
            self._brightness *= float(np.exp(-p.decay * dt))
            expected = p.density * self.topology.pixel_count * dt
            n_new = int(self._rng.poisson(expected)) if expected > 0 else 0
            if n_new > 0:
                idxs = self._rng.integers(0, self.topology.pixel_count, n_new)
                self._brightness[idxs] = 1.0
        np.copyto(out, self._brightness)
