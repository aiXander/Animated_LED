import numpy as np

# Gamma is applied through a lookup table: `np.power` on 5400 float32 values is
# one of the slowest steps of a frame on the Pi's ARM core, a table lookup one
# of the cheapest. 4096 input steps keep the result within one output code of
# the exact curve.
_GAMMA_LUT_STEPS = 4096


class PixelBuffer:
    """Float32 RGB working buffer in [0, 1].

    Effects render into `rgb`. The transport layer converts to uint8 right
    before send — keeps blending math clean and applies gamma in one place
    (here, not also in WLED).
    """

    __slots__ = ("n", "rgb", "_scratch", "_idx", "_u8", "_lut", "_lut_gamma")

    def __init__(self, n: int):
        self.n = n
        self.rgb = np.zeros((n, 3), dtype=np.float32)
        # Pre-allocated scratch + uint8 frame buffers reused every tick to
        # avoid fresh allocations per render at 60 Hz. The returned uint8
        # view is owned by us; the transport reads it synchronously before
        # we overwrite it next frame, so a single shared buffer is safe.
        self._scratch = np.zeros((n, 3), dtype=np.float32)
        self._idx = np.zeros((n, 3), dtype=np.intp)
        self._u8 = np.zeros((n, 3), dtype=np.uint8)
        self._lut: np.ndarray | None = None
        self._lut_gamma: float = 1.0

    def clear(self) -> None:
        self.rgb.fill(0.0)

    def to_uint8(self, gamma: float = 1.0) -> np.ndarray:
        """Encode this buffer's own `rgb`. See `encode`."""
        return self.encode(self.rgb, gamma)

    def encode(self, src: np.ndarray, gamma: float = 1.0) -> np.ndarray:
        """Clip `src` (N, 3 float32) to [0, 1], apply gamma, round to uint8.

        Returns a view of an internally-reused buffer. Callers must consume
        it (e.g. .tobytes() on the transport) before the next encode call.
        """
        scratch = self._scratch
        if gamma == 1.0:
            np.clip(src, 0.0, 1.0, out=scratch)
            np.multiply(scratch, 255.0, out=scratch)
            np.add(scratch, 0.5, out=scratch)
            np.copyto(self._u8, scratch, casting="unsafe")
            return self._u8
        if self._lut is None or self._lut_gamma != gamma:
            x = np.arange(_GAMMA_LUT_STEPS + 1, dtype=np.float64) / _GAMMA_LUT_STEPS
            self._lut = np.floor(np.power(x, gamma) * 255.0 + 0.5).astype(np.uint8)
            self._lut_gamma = gamma
        np.multiply(src, float(_GAMMA_LUT_STEPS), out=scratch)
        np.add(scratch, 0.5, out=scratch)
        np.clip(scratch, 0.0, float(_GAMMA_LUT_STEPS), out=scratch)
        np.copyto(self._idx, scratch, casting="unsafe")
        # mode="clip" also catches the index a NaN pixel casts to.
        np.take(self._lut, self._idx, out=self._u8, mode="clip")
        return self._u8
