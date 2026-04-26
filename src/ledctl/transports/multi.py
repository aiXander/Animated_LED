import asyncio
import contextlib

import numpy as np

from .base import Transport


class MultiTransport(Transport):
    """Fans a frame out to multiple transports concurrently."""

    def __init__(self, transports: list[Transport]):
        self._transports = list(transports)

    async def send_frame(self, pixels: np.ndarray) -> None:
        if not self._transports:
            return
        await asyncio.gather(
            *(t.send_frame(pixels) for t in self._transports),
            return_exceptions=False,
        )

    async def close(self) -> None:
        for t in self._transports:
            with contextlib.suppress(Exception):
                await t.close()
