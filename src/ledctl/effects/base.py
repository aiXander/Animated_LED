from abc import ABC, abstractmethod
from typing import ClassVar

import numpy as np
from pydantic import BaseModel

from ..topology import Topology


class EffectParams(BaseModel):
    """Base for effect parameters. Subclasses are surfaced through the API/MCP layer."""


class Effect(ABC):
    """An effect samples colour values into a working buffer at time t.

    Effects work in normalised spatial coords (`topology.normalised_positions`,
    each axis in [-1, 1]) so behaviour is independent of strip count or layout.
    """

    name: ClassVar[str] = "base"
    Params: ClassVar[type[EffectParams]] = EffectParams

    def __init__(self, params: EffectParams, topology: Topology):
        self.params = params
        self.topology = topology

    @abstractmethod
    def render(self, t: float, out: np.ndarray) -> None:
        """Write RGB float32 [0, 1] of shape (N, 3) into `out`."""
