"""Split transport: separate sim leg + LED leg.

In live mode the engine ships the same bytes to both legs (zero extra copy
since we pass the same uint8 frame). In design mode the LED leg gets the
LIVE buffer's encoding while the sim leg gets the PREVIEW.

`transport/pause` only blocks the LED leg — sim keeps streaming so the
operator UI viz stays alive when the operator wants to A/B WLED's own preset.
"""

from __future__ import annotations

import contextlib
from typing import Any

import numpy as np

from .base import Transport
from .ddp import DDPTransport
from .simulator import SimulatorTransport


class SplitTransport(Transport):
    """Owns one SimulatorTransport + zero-or-one DDPTransport."""

    def __init__(
        self,
        sim: SimulatorTransport,
        led: DDPTransport | None,
    ) -> None:
        self.sim = sim
        self.led = led

    async def send_frame(self, pixels: np.ndarray) -> None:
        """Compatibility hook — fans the same frame to both legs."""
        await self.send(led_frame=pixels, sim_frame=pixels)

    async def send(
        self,
        *,
        led_frame: np.ndarray | None,
        sim_frame: np.ndarray | None,
    ) -> None:
        # Sim first — cheap and the operator stares at it. Then DDP.
        if sim_frame is not None:
            await self.sim.send_frame(sim_frame)
        if self.led is not None and led_frame is not None:
            await self.led.send_frame(led_frame)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.sim.close()
        if self.led is not None:
            with contextlib.suppress(Exception):
                await self.led.close()

    # --- helpers for callers (engine, REST handlers) --- #

    @property
    def has_led(self) -> bool:
        return self.led is not None

    def ddp_state(self) -> dict[str, Any]:
        if self.led is None:
            return {"available": False, "paused": False, "frames_sent": 0, "packets_sent": 0}
        return {
            "available": True,
            "paused": bool(self.led.paused),
            "host": self.led.host,
            "port": self.led.port,
            "frames_sent": self.led.frames_sent,
            "packets_sent": self.led.packets_sent,
        }
