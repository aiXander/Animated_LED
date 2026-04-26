"""`noise` — 2D value noise sampled at LED positions, scrolled over time.

Uses a precomputed lattice of random values; per frame we offset positions and
bilinearly interpolate. Multiple octaves can be summed for a richer texture.
Cheaper and more deterministic than perlin/simplex, and visually fine on
1-D-ish layouts (where most LEDs share a y-coord and the noise reads as a
rolling 1-D pattern).
"""

from typing import ClassVar

import numpy as np
from pydantic import Field

from ...topology import Topology
from ..base import EffectParams
from ..registry import register
from .base import FieldEffect, FieldParams

_LATTICE_SIZE = 64


class NoiseParams(FieldParams):
    """2D value noise scrolled over time."""

    speed: float = Field(
        0.2,
        description="Field flow speed in lattice units per second; sign sets direction",
    )
    scale: float = Field(
        0.5, gt=0.0,
        description="Spatial scale: smaller = larger blobs, larger = busier texture",
    )
    octaves: int = Field(
        1, ge=1, le=4,
        description="Octaves of noise summed (each at 2× scale, half amplitude)",
    )
    seed: int | None = Field(
        None, description="RNG seed for the lattice (None = 0)"
    )


@register
class NoiseEffect(FieldEffect):
    name: ClassVar[str] = "noise"
    Params: ClassVar[type[EffectParams]] = NoiseParams

    def __init__(self, params: NoiseParams, topology: Topology):
        super().__init__(params, topology)
        rng = np.random.default_rng(params.seed if params.seed is not None else 0)
        self._lattice = rng.random(
            (_LATTICE_SIZE, _LATTICE_SIZE), dtype=np.float32
        )
        # Cache LED positions (only x and y — typical layouts are roughly 2D).
        self._x = topology.normalised_positions[:, 0].astype(np.float32, copy=False)
        self._y = topology.normalised_positions[:, 1].astype(np.float32, copy=False)

    def _render_scalar(
        self, t: float, speed_override: float | None, out: np.ndarray
    ) -> None:
        p: NoiseParams = self.params  # type: ignore[assignment]
        speed = float(speed_override) if speed_override is not None else float(p.speed)
        N = _LATTICE_SIZE
        out.fill(0.0)
        amp = 1.0
        total_amp = 0.0
        for octave in range(p.octaves):
            scale = float(p.scale) * (2 ** octave)
            # Time offsets along x and y differ slightly so the field flows
            # rather than scrolling rigidly along a diagonal.
            ox = (self._x * scale * N + speed * t * N) % N
            oy = (self._y * scale * N + speed * t * 0.7 * N) % N
            x0 = np.floor(ox).astype(np.int32)
            y0 = np.floor(oy).astype(np.int32)
            x1 = (x0 + 1) % N
            y1 = (y0 + 1) % N
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
