import numpy as np


class PixelBuffer:
    """Float32 RGB working buffer in [0, 1].

    Effects render into `rgb`. The transport layer converts to uint8 right
    before send — keeps blending math clean and gamma-correctable later.
    """

    __slots__ = ("n", "rgb")

    def __init__(self, n: int):
        self.n = n
        self.rgb = np.zeros((n, 3), dtype=np.float32)

    def clear(self) -> None:
        self.rgb.fill(0.0)

    def to_uint8(self) -> np.ndarray:
        return (np.clip(self.rgb, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
