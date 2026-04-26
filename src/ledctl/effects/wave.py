from typing import ClassVar, Literal

import numpy as np
from pydantic import Field, field_validator

from ..topology import Topology
from .base import Effect, EffectParams


def _hex_to_rgb01(s: str) -> np.ndarray:
    s = s.lstrip("#")
    if len(s) != 6:
        raise ValueError(f"hex colour must be 6 hex digits, got {s!r}")
    return np.array(
        [int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0, int(s[4:6], 16) / 255.0],
        dtype=np.float32,
    )


class WaveParams(EffectParams):
    """A travelling wave that morphs between two colours along one axis.

    Coordinates are normalised: `wavelength=1.0` means one full cycle spans the
    install end-to-end on the chosen axis.
    """

    color_a: str = Field("#ff7000", description="Hex colour at trough (e.g. #ff7000)")
    color_b: str = Field("#ff0030", description="Hex colour at peak (e.g. #ff0030)")
    wavelength: float = Field(
        1.0, gt=0.0,
        description="Cycles per full normalised span; 1.0 = one cycle end-to-end",
    )
    speed: float = Field(0.3, description="Cycles per second; negative reverses direction")
    direction: Literal["x", "y", "z"] = Field("x", description="Axis the wave travels along")
    softness: float = Field(
        1.0, ge=0.0, le=1.0,
        description="0=hard band edges, 1=fully smooth cosine",
    )

    @field_validator("color_a", "color_b")
    @classmethod
    def _validate_hex(cls, v: str) -> str:
        _hex_to_rgb01(v)
        return v


class WaveEffect(Effect):
    name: ClassVar[str] = "wave"
    Params: ClassVar[type[EffectParams]] = WaveParams

    def __init__(self, params: WaveParams, topology: Topology):
        super().__init__(params, topology)
        self._coords = topology.normalised_positions
        self._a = _hex_to_rgb01(params.color_a)
        self._b = _hex_to_rgb01(params.color_b)
        self._axis_idx = "xyz".index(params.direction)

    def render(self, t: float, out: np.ndarray) -> None:
        p: WaveParams = self.params  # type: ignore[assignment]
        axis = self._coords[:, self._axis_idx]
        # phase ∈ ℝ; cosine maps it to [-1, 1] then to [0, 1].
        phase = axis / p.wavelength - p.speed * t
        smooth = (np.cos(2.0 * np.pi * phase) + 1.0) * 0.5
        if p.softness >= 1.0:
            mix = smooth
        elif p.softness <= 0.0:
            mix = (smooth > 0.5).astype(np.float32)
        else:
            hard = (smooth > 0.5).astype(np.float32)
            mix = p.softness * smooth + (1.0 - p.softness) * hard
        # Linear interp in RGB. (Phase 2 will add gamma; keep it simple here.)
        np.multiply(self._a, (1.0 - mix)[:, None], out=out)
        out += self._b * mix[:, None]
