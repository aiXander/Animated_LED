"""Shared pieces of ledctl's two UDP/OSC listeners (audio features, WZRD input).

  - `AllowlistOSCUDPServer` drops packets from senders outside an allowlist
    before they are parsed or dispatched.
  - `LinkStats` tells the operator how healthy a packet stream is: packets per
    second and the largest gap between two packets, over the last second.
"""

from __future__ import annotations

import ipaddress
from time import monotonic
from typing import Any

from pythonosc.osc_server import BlockingOSCUDPServer

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_allow_from(entries: list[str] | tuple[str, ...]) -> tuple[IPNetwork, ...]:
    """IPs or CIDR ranges -> networks. Raises ValueError on a bad entry."""
    return tuple(
        ipaddress.ip_network(e.strip(), strict=False) for e in entries if e.strip()
    )


class AllowlistOSCUDPServer(BlockingOSCUDPServer):
    """`BlockingOSCUDPServer` that only accepts senders on an allowlist.

    An empty list accepts any sender. `verify_request` is the `socketserver`
    hook that runs before dispatch, so a rejected packet costs one dict lookup.
    """

    def __init__(self, server_address: tuple[str, int], dispatcher: Any,
                 allow_from: tuple[IPNetwork, ...] = ()):
        super().__init__(server_address, dispatcher)
        self.allow_from = tuple(allow_from)
        self.rejected: int = 0
        self._verdicts: dict[str, bool] = {}

    def _allowed(self, host: str) -> bool:
        verdict = self._verdicts.get(host)
        if verdict is None:
            try:
                addr = ipaddress.ip_address(host)
                verdict = any(addr in net for net in self.allow_from)
            except ValueError:
                verdict = False
            if len(self._verdicts) < 1024:   # a spoofing flood must not grow it forever
                self._verdicts[host] = verdict
        return verdict

    def verify_request(self, request: Any, client_address: Any) -> bool:
        if self.allow_from and not self._allowed(client_address[0]):
            self.rejected += 1
            return False
        return super().verify_request(request, client_address)


class LinkStats:
    """Packets per second and largest inter-packet gap over the last second.

    `mark()` runs on the listener thread for every counted packet; `snapshot()`
    is read from HTTP handlers. Plain attribute writes, no lock: a torn read is
    one stale number in a status line.
    """

    def __init__(self) -> None:
        self._window_start: float = 0.0
        self._count: int = 0
        self._max_gap: float = 0.0
        self._last_at: float = 0.0
        self.packets_per_s: float = 0.0
        self.max_gap_ms: float | None = None

    def mark(self, now: float | None = None) -> None:
        now = monotonic() if now is None else now
        if self._last_at == 0.0 or now - self._last_at > 5.0:
            # First packet, or the stream is back after a break: new window.
            self._window_start = now
            self._count = 0
            self._max_gap = 0.0
        else:
            gap = now - self._last_at
            if gap > self._max_gap:
                self._max_gap = gap
        self._last_at = now
        self._count += 1
        elapsed = now - self._window_start
        if elapsed >= 1.0:
            self.packets_per_s = (self._count - 1) / elapsed
            self.max_gap_ms = self._max_gap * 1000.0
            self._window_start = now
            self._count = 1
            self._max_gap = 0.0

    def snapshot(self, now: float | None = None) -> dict[str, float | None]:
        now = monotonic() if now is None else now
        if self._last_at == 0.0:
            return {"packets_per_s": 0.0, "max_gap_ms": None}
        silent = now - self._last_at
        if silent > 1.0:
            # Nothing for over a second: the gap that matters is the current one.
            return {"packets_per_s": 0.0, "max_gap_ms": round(silent * 1000.0, 1)}
        gap = self.max_gap_ms
        return {
            "packets_per_s": round(self.packets_per_s, 1),
            "max_gap_ms": round(gap, 1) if gap is not None else None,
        }


__all__ = ["AllowlistOSCUDPServer", "IPNetwork", "LinkStats", "parse_allow_from"]
