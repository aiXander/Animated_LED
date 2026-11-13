"""UDP/OSC listener for WZRD's LED link (contract C-LED version 2).

    /wzrd/colors   level, white, then k x (r, g, b, weight), one per hue bin
    /wzrd/masters  brightness, saturation

Runs `AllowlistOSCUDPServer` on its own daemon thread, like the audio bridge.
A handler validates and clamps one packet and stores it by a single reference
assignment, so the render thread never reads half an update. It does no other
maths and never wakes the render loop.
"""

from __future__ import annotations

import logging
import threading
from time import monotonic
from typing import Any

import numpy as np

from ..osc import AllowlistOSCUDPServer, IPNetwork, LinkStats

log = logging.getLogger(__name__)

COLORS_ADDRESS = "/wzrd/colors"
MASTERS_ADDRESS = "/wzrd/masters"


def _finite_floats(args: tuple[Any, ...]) -> np.ndarray | None:
    """OSC arguments as float32, or None when any is non-numeric or not finite."""
    for a in args:
        if isinstance(a, bool) or not isinstance(a, (int, float)):
            return None
    arr = np.asarray(args, dtype=np.float32)
    if not np.all(np.isfinite(arr)):
        return None
    return arr


def parse_colors(args: tuple[Any, ...]) -> tuple[float, float, np.ndarray] | None:
    """`/wzrd/colors` arguments -> (level, white, bins (k, 4)), all in 0..1.

    The bin count comes from the argument count, `(n - 2) / 4`. None = drop.
    """
    n = len(args)
    if n < 6 or (n - 2) % 4 != 0:
        return None
    arr = _finite_floats(args)
    if arr is None:
        return None
    np.clip(arr, 0.0, 1.0, out=arr)
    return float(arr[0]), float(arr[1]), arr[2:].reshape(-1, 4)


def parse_masters(args: tuple[Any, ...]) -> tuple[float, float] | None:
    """`/wzrd/masters` arguments -> (brightness, saturation), both in 0..2."""
    if len(args) != 2:
        return None
    arr = _finite_floats(args)
    if arr is None:
        return None
    np.clip(arr, 0.0, 2.0, out=arr)
    return float(arr[0]), float(arr[1])


class WzrdListener:
    def __init__(self, host: str = "0.0.0.0", port: int = 9100,
                 allow_from: tuple[IPNetwork, ...] = ()):
        self.host = host
        self.port = int(port)
        self.allow_from = tuple(allow_from)
        # Latest valid packets: (arrival_time, level, white, bins) and
        # (arrival_time, brightness, saturation). Replaced whole, never mutated.
        self.latest_colors: tuple[float, float, float, np.ndarray] | None = None
        self.latest_masters: tuple[float, float, float] | None = None
        self.invalid: int = 0            # packets that failed validation
        self.link = LinkStats()          # counts /wzrd/colors only
        self.error: str = ""
        self._server: AllowlistOSCUDPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def dropped(self) -> int:
        """Packets refused: failed validation, or a sender off the allowlist."""
        srv = self._server
        return self.invalid + (srv.rejected if srv is not None else 0)

    def start(self) -> None:
        if self._server is not None:
            return
        from pythonosc import dispatcher  # noqa: PLC0415

        d = dispatcher.Dispatcher()
        d.map(COLORS_ADDRESS, self._on_colors)
        d.map(MASTERS_ADDRESS, self._on_masters)
        d.set_default_handler(self._on_other)
        try:
            self._server = AllowlistOSCUDPServer(
                (self.host, self.port), d, allow_from=self.allow_from,
            )
        except OSError as e:
            self.error = f"could not bind {self.host}:{self.port}: {e}"
            log.warning("WZRD listener %s — WZRD input disabled", self.error)
            return
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="ledctl-wzrd-listener", daemon=True,
        )
        self._thread.start()
        log.info("WZRD listener bound %s:%d", self.host, self.port)

    def stop(self) -> None:
        srv = self._server
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception as e:  # noqa: BLE001
                log.debug("WZRD listener shutdown: %s", e)
        self._server = None
        self._thread = None

    # ---- OSC handlers (listener thread) ----

    def _on_colors(self, _addr: str, *args: Any) -> None:
        parsed = parse_colors(args)
        if parsed is None:
            self.invalid += 1
            return
        now = monotonic()
        self.latest_colors = (now, *parsed)
        self.link.mark(now)

    def _on_masters(self, _addr: str, *args: Any) -> None:
        parsed = parse_masters(args)
        if parsed is None:
            self.invalid += 1
            return
        self.latest_masters = (monotonic(), *parsed)

    def _on_other(self, _addr: str, *_args: Any) -> None:
        self.invalid += 1
