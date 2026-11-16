"""Fan messages out to WebSocket clients without ever waiting on one.

The render loop and the state broadcaster both push to browsers that may sit
behind a slow link (the operator UI over a tailnet). Awaiting `ws.send_*` from
the producer makes the slowest client set the producer's pace: a congested
link used to stall the render loop, and with it the LEDs.

Here every client gets a mailbox and its own sender task. `publish()` drops the
message into each mailbox and returns at once. A client that cannot keep up
simply skips to the newest message when its previous send finishes.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any


class _Mailbox:
    __slots__ = ("ws", "pending", "wake", "task")

    def __init__(self, ws: Any):
        self.ws = ws
        # channel -> newest undelivered message. A newer message on the same
        # channel replaces the older one; messages go out oldest first.
        self.pending: dict[int, bytes | str] = {}
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None


class LatestFanout:
    def __init__(self) -> None:
        self._boxes: dict[Any, _Mailbox] = {}

    def __len__(self) -> int:
        return len(self._boxes)

    def __bool__(self) -> bool:
        return bool(self._boxes)

    def add(self, ws: Any) -> None:
        """Start delivering to `ws`. Must be called from the event loop."""
        if ws in self._boxes:
            return
        box = _Mailbox(ws)
        self._boxes[ws] = box
        box.task = asyncio.create_task(self._pump(box), name="ledctl-ws-sender")

    def discard(self, ws: Any) -> None:
        box = self._boxes.pop(ws, None)
        if box is not None and box.task is not None:
            box.task.cancel()

    def publish(self, message: bytes | str, channel: int = 0) -> None:
        """Queue `message` for every client. Never blocks, never awaits."""
        for box in self._boxes.values():
            # Re-insert at the end, so delivery order stays publish order: a
            # client never gets an older message after a newer one.
            box.pending.pop(channel, None)
            box.pending[channel] = message
            box.wake.set()

    async def close(self) -> None:
        boxes = list(self._boxes.values())
        self._boxes.clear()
        for box in boxes:
            if box.task is not None:
                box.task.cancel()
        for box in boxes:
            with contextlib.suppress(Exception):
                await box.ws.close()

    async def _pump(self, box: _Mailbox) -> None:
        ws = box.ws
        try:
            while True:
                await box.wake.wait()
                box.wake.clear()
                while box.pending:
                    channel = next(iter(box.pending))
                    message = box.pending.pop(channel)
                    if isinstance(message, str):
                        await ws.send_text(message)
                    else:
                        await ws.send_bytes(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The socket is gone (or broken): stop delivering to it, and close
            # it so the browser notices and reconnects instead of sitting on a
            # connection that will never carry another message.
            self._boxes.pop(ws, None)
            with contextlib.suppress(Exception):
                await ws.close()


__all__ = ["LatestFanout"]
