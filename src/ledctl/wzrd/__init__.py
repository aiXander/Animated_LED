"""WZRD input: the LEDs take their colours, level and masters from the wall
that WZRD (a projection tool on another machine) is playing.

Self-contained: `mount_wzrd` is the only entry point, called from `create_app`
when `wzrd_input.enabled` is true. Deleting this package and that call leaves
ledctl working unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .api import install_wzrd_routes
from .listener import WzrdListener
from .stage import WzrdStage
from .state import WzrdSettings

SETTINGS_FILENAME = "wzrd_input.yaml"


def mount_wzrd(app: FastAPI, cfg: Any, runtime: Any, topology: Any,
               config_dir: Path) -> WzrdStage:
    """Build the listener and the output hook, start listening, install the
    routes and add the `wzrd` key to `/state`. The caller stops
    `stage.listener` on shutdown."""
    wcfg = cfg.wzrd_input
    loopback = wcfg.listen_host in ("127.0.0.1", "localhost")
    listener = WzrdListener(
        host=wcfg.listen_host,
        port=wcfg.listen_port,
        allow_from=() if loopback else cfg.audio_server.resolved_allow_from(),
    )
    path = Path(config_dir) / SETTINGS_FILENAME
    stage = WzrdStage(topology, listener, WzrdSettings.load(path), settings_path=path)
    runtime.output_hook = stage
    listener.start()
    install_wzrd_routes(app, stage)
    app.state.state_extras["wzrd"] = stage.payload
    return stage


__all__ = ["WzrdListener", "WzrdSettings", "WzrdStage", "mount_wzrd"]
