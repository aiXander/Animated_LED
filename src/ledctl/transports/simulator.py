from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

import numpy as np

from .base import Transport

if TYPE_CHECKING:
    from fastapi import WebSocket


class SimulatorTransport(Transport):
    """Broadcasts each frame as raw RGB bytes to all connected websocket clients.

    Frame-rate gating is independent of the engine tick rate. The engine ticks
    at `project.target_fps` (LED leg); this transport throttles the WS sends
    to `target_fps` so the UI viz can run at e.g. 24 Hz while the LEDs run at
    60 Hz. The Pi-side savings are real: each browser frame on a tailnet means
    `pixels.tobytes()` + WS framing + WireGuard encryption.
    """

    # Snap values exposed in the operator UI. Keep in sync with the slider
    # mapping in main-desktop.js — validating against this list means an
    # arbitrary PATCH /sim/fps can't land us at a value the UI can't display.
    ALLOWED_FPS: tuple[int, ...] = (12, 24, 30, 40, 60)

    def __init__(self, target_fps: float = 24.0) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        # When paused, send_frame is a no-op. Used by the operator UI to
        # offload the Pi: pausing the simulator stream stops every connected
        # browser viz from receiving frames so the render loop only feeds DDP.
        self.paused: bool = False
        # Independent UI frame rate. The engine's `should_send_now()` query
        # uses the same monotonic clock so render+encode work is also skipped
        # on dropped frames — not just the WS write.
        self.target_fps: float = float(target_fps)
        self._next_send_t: float = 0.0

    async def add_client(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove_client(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def set_target_fps(self, fps: float) -> float:
        v = int(round(float(fps)))
        if v not in self.ALLOWED_FPS:
            raise ValueError(
                f"sim fps must be one of {list(self.ALLOWED_FPS)}, got {fps}"
            )
        self.target_fps = float(v)
        # Force the next frame to send immediately so a slider drag feels live.
        self._next_send_t = 0.0
        return self.target_fps

    def should_send_now(self) -> bool:
        """Predicate the engine uses to short-circuit preview render + sim
        encode on frames we'd drop anyway. Does not advance internal state —
        the actual send call does that."""
        if self.paused or not self._clients:
            return False
        if self.target_fps <= 0.0:
            return False
        # 1 ms slack so a tick that arrives a hair early (scheduler jitter)
        # still counts as on-time and we don't accumulate cumulative drift.
        return time.monotonic() + 1e-3 >= self._next_send_t

    async def send_frame(self, pixels: np.ndarray) -> None:
        if self.paused or not self._clients:
            return
        if self.target_fps > 0.0:
            now = time.monotonic()
            if now + 1e-3 < self._next_send_t:
                return
            period = 1.0 / float(self.target_fps)
            # If we've fallen way behind (e.g. Pi was stalled), reset the
            # deadline relative to now rather than piling up missed slots —
            # otherwise we'd burst-send back-to-back frames to "catch up".
            if self._next_send_t == 0.0 or now > self._next_send_t + period:
                self._next_send_t = now + period
            else:
                self._next_send_t += period
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
