from abc import ABC, abstractmethod

import numpy as np


class Transport(ABC):
    """Pluggable frame sink. Receives an (N, 3) uint8 RGB array per frame."""

    @abstractmethod
    async def send_frame(self, pixels: np.ndarray) -> None: ...

    async def close(self) -> None:
        """Release any underlying resources. Default: no-op."""
        return None
