# /// script
# requires-python = ">=3.11"
# dependencies = ["mido", "python-rtmidi", "websockets"]
# ///
"""Forward a MIDI controller on this machine to ledctl, and light it back.

The stand-in for WZRD's LEDs mode: use it to play the LEDs from the Akai APC
mini when WZRD is not running (or to test ledctl's MIDI mapping on its own).
Do not run it while WZRD has the controller in another mode: both would act on
every press and fight over the pad lights.

    uv run scripts/midi_relay.py                                  # ledctl on this Mac
    uv run scripts/midi_relay.py --url ws://<pi>:8000/ws/midi     # ledctl on the Pi

The password, when ledctl has one, is read from LEDCTL_PASSWORD (never a flag,
so it stays out of the shell history and the process list).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os

import mido
import websockets


def find_port(names: list[str], device: str) -> str | None:
    return next((n for n in names if device.lower() in n.lower()), None)


async def relay(url: str, device: str) -> None:
    in_name = find_port(mido.get_input_names(), device)
    out_name = find_port(mido.get_output_names(), device)
    if in_name is None:
        raise SystemExit(f"no MIDI input matching {device!r}; found {mido.get_input_names()}")
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def on_message(msg: mido.Message) -> None:      # mido's own thread
        if msg.type in ("note_on", "note_off", "control_change"):
            loop.call_soon_threadsafe(queue.put_nowait, bytes(msg.bytes()))

    password = os.environ.get("LEDCTL_PASSWORD", "")
    headers = {"Cookie": f"ledctl_auth={password}"} if password else None
    inport = mido.open_input(in_name, callback=on_message)
    outport = mido.open_output(out_name) if out_name else None
    print(f"controller: in {in_name!r}, lights {out_name!r}")
    try:
        while True:
            try:
                async with websockets.connect(url, additional_headers=headers) as ws:
                    print(f"connected to {url}")

                    async def lights(ws=ws) -> None:
                        async for frame in ws:
                            if outport is not None and isinstance(frame, bytes):
                                outport.send(mido.Message.from_bytes(frame))

                    lighter = asyncio.create_task(lights())
                    try:
                        while not lighter.done():
                            get = asyncio.create_task(queue.get())
                            await asyncio.wait({get, lighter}, return_when=asyncio.FIRST_COMPLETED)
                            if get.done():
                                await ws.send(get.result())
                            else:
                                get.cancel()
                    finally:
                        lighter.cancel()
            except (OSError, websockets.WebSocketException) as e:
                print(f"not connected ({e}); retrying")
            await asyncio.sleep(1.0)
    finally:
        inport.close()
        if outport is not None:
            outport.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="ws://127.0.0.1:8000/ws/midi")
    ap.add_argument("--device", default="apc mini",
                    help="part of the controller's MIDI port name (default: 'apc mini')")
    args = ap.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(relay(args.url, args.device))


if __name__ == "__main__":
    main()
