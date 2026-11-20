"""MIDI control: a hardware controller (the Akai APC mini) plays the LEDs.

The controller is plugged into another machine. A sender there forwards its
raw messages over the `/ws/midi` WebSocket and lights the controller with what
comes back; ledctl owns the mapping (`surface.py`). `mount_midi` is the only
entry point, called from `create_app`.

`/ws/midi` (behind the same password cookie as every other route):
  sender -> ledctl   binary frame = one raw MIDI message (3 bytes);
                     text frame "sync" = send every light again
  ledctl -> sender   binary frame = [0x90, note, value]: set that light
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict

from .surface import FILENAME, MidiSurface

# How often a connected controller's lights are compared with the state.
LIGHTS_EVERY_S = 0.05


class BindingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # hardware id -> target id, or null to clear that control
    bindings: dict[str, str | None]


def mount_midi(app: FastAPI, *, engine: Any, runtime: Any, playlist: Any, store: Any,
               value_saver: Any, config_dir: Path,
               ws_auth: Callable[[WebSocket], Awaitable[bool]]) -> MidiSurface:
    def mark_dirty() -> None:
        app.state.state_dirty = True

    surface = MidiSurface(
        engine=engine, runtime=runtime, playlist=playlist, store=store,
        value_saver=value_saver, wzrd=app.state.wzrd,
        path=Path(config_dir) / FILENAME, mark_dirty=mark_dirty,
    )
    app.state.state_extras["midi"] = surface.state

    @app.get("/midi")
    async def get_midi() -> dict:
        return await asyncio.to_thread(surface.describe)

    @app.put("/midi/bindings")
    async def put_bindings(body: BindingsPatch) -> dict:
        """Bind or clear controls; the whole mapping is then saved to
        `config/midi.yaml`."""
        try:
            surface.patch_bindings(body.bindings)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        await asyncio.to_thread(surface.save)
        return await asyncio.to_thread(surface.describe)

    @app.post("/midi/reset")
    async def reset_midi() -> dict:
        """Back to the preset mapping; deletes `config/midi.yaml`."""
        await asyncio.to_thread(surface.reset)
        return await asyncio.to_thread(surface.describe)

    @app.websocket("/ws/midi")
    async def ws_midi(websocket: WebSocket) -> None:
        if not await ws_auth(websocket):
            return
        await websocket.accept()
        sent: dict[int, int] = {}      # what this sender's controller shows

        async def light() -> None:
            while True:
                for note, value in surface.lights().items():
                    if sent.get(note) != value:
                        await websocket.send_bytes(bytes((0x90, note, value)))
                        sent[note] = value
                await asyncio.sleep(LIGHTS_EVERY_S)

        surface.clients += 1
        lighter = asyncio.create_task(light(), name="ledctl-midi-lights")
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    surface.handle(msg["bytes"])
                elif msg.get("text") == "sync":
                    sent.clear()
        except WebSocketDisconnect:
            pass
        finally:
            lighter.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await lighter
            surface.clients -= 1
            if surface.clients <= 0:
                surface.released()

    return surface


__all__ = ["MidiSurface", "mount_midi"]
