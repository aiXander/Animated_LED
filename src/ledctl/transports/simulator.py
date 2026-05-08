import asyncio
import contextlib
from typing import TYPE_CHECKING

import numpy as np

from .base import Transport

if TYPE_CHECKING:
    from fastapi import WebSocket


class SimulatorTransport(Transport):
    """Broadcasts each frame as raw RGB bytes to all connected websocket clients."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        # When paused, send_frame is a no-op. Used by the operator UI to
        # offload the Pi: pausing the simulator stream stops every connected
        # browser viz from receiving frames so the render loop only feeds DDP.
        self.paused: bool = False

    async def add_client(self, ws: "WebSocket") -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove_client(self, ws: "WebSocket") -> None:
        async with self._lock:
            self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def send_frame(self, pixels: np.ndarray) -> None:
        if self.paused or not self._clients:
            return
        data = pixels.tobytes()
        # Snapshot so we don't mutate during iteration.
        async with self._lock:
            targets = list(self._clients)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_bytes(data)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    async def close(self) -> None:
        async with self._lock:
            targets = list(self._clients)
            self._clients.clear()
        for ws in targets:
            with contextlib.suppress(Exception):
                await ws.close()
