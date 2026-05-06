import numpy as np


class PixelBuffer:
    """Float32 RGB working buffer in [0, 1].

    Effects render into `rgb`. The transport layer converts to uint8 right
    before send — keeps blending math clean and applies gamma in one place
    (here, not also in WLED).
    """

    __slots__ = ("n", "rgb", "_scratch", "_u8")

    def __init__(self, n: int):
        self.n = n
        self.rgb = np.zeros((n, 3), dtype=np.float32)
        # Pre-allocated scratch + uint8 frame buffers reused every tick to
        # avoid 3 fresh allocations per render at 60 Hz. The returned uint8
        # view is owned by us; the transport reads it synchronously before
        # we overwrite it next frame, so a single shared buffer is safe.
        self._scratch = np.zeros((n, 3), dtype=np.float32)
        self._u8 = np.zeros((n, 3), dtype=np.uint8)

    def clear(self) -> None:
        self.rgb.fill(0.0)

    def to_uint8(self, gamma: float = 1.0) -> np.ndarray:
        # Returns a view of an internally-reused buffer. Callers must consume
        # it (e.g. .tobytes() on the transport) before the next to_uint8 call.
        np.clip(self.rgb, 0.0, 1.0, out=self._scratch)
        if gamma != 1.0:
            np.power(self._scratch, gamma, out=self._scratch)
        np.multiply(self._scratch, 255.0, out=self._scratch)
        np.add(self._scratch, 0.5, out=self._scratch)
        np.copyto(self._u8, self._scratch, casting="unsafe")
        return self._u8
