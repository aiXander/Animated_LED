"""What the LIVE LEDs were showing, so a restart or a power cut mid-gig comes back to it.

`config/live.yaml` (runtime state, gitignored) holds the LIVE composition as
library effect names plus each layer's blend / opacity / enabled, and whether the
playlist was running. `watch()` rewrites it within a couple of seconds of any
change, off the event loop; boot reads it with `load()`. Knob values are not in
here: they already live in each effect's `effect.yaml`.

A layer whose effect is not in the library (an agent effect promoted to LIVE
without a save) cannot be restored and is skipped on boot.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import yamlio
from .surface.persistence import write_atomic

log = logging.getLogger(__name__)

FILENAME = "live.yaml"


def snapshot(runtime, playlist) -> dict[str, Any]:
    return {
        "layers": [
            {"name": layer.name, "blend": layer.blend, "opacity": float(layer.opacity),
             "enabled": bool(layer.enabled)}
            for layer in runtime.live.layers
        ],
        "selected": runtime.live.selected,
        "playlist_running": bool(playlist.running),
    }


def load(path: Path) -> dict[str, Any] | None:
    try:
        data = yamlio.load(path.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("could not read %s; booting the default effect", path)
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, state: dict[str, Any]) -> None:
    write_atomic(path, yamlio.dump(state))


async def watch(path: Path, get_state: Callable[[], dict[str, Any]],
                interval: float = 2.0) -> None:
    """Poll the LIVE state; write it when it differs from what is on disk."""
    last = load(path)
    while True:
        await asyncio.sleep(interval)
        state = get_state()
        if state == last:
            continue
        try:
            await asyncio.to_thread(_write, path, state)
            last = state
        except OSError:
            log.exception("could not write %s", path)
