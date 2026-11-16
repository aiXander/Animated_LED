"""Shared pieces of ledctl's two UDP/OSC listeners (audio features, WZRD input).

  - `AllowlistOSCUDPServer` receives OSC over UDP, drops packets from senders
    outside an allowlist before they are parsed, and calls one handler per
    address.
  - `LinkStats` tells the operator how healthy a packet stream is: packets per
    second and the largest gap between two packets, over the last second.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import threading
from collections.abc import Callable, Mapping
from time import monotonic

log = logging.getLogger(__name__)

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# An OSC handler: called as `handler(address, *args)`.
Handler = Callable[..., None]

# OSC type tag -> struct code, for the fixed-size argument types.
_FIXED = {ord("f"): "f", ord("i"): "i", ord("d"): "d", ord("h"): "q"}


def parse_allow_from(entries: list[str] | tuple[str, ...]) -> tuple[IPNetwork, ...]:
    """IPs or CIDR ranges -> networks. Raises ValueError on a bad entry."""
    return tuple(
        ipaddress.ip_network(e.strip(), strict=False) for e in entries if e.strip()
    )


class AllowlistOSCUDPServer:
    """OSC-over-UDP receiver for the flat numeric messages ledctl consumes.

    `handlers` maps an address to `handler(address, *args)`, or to None to
    accept and ignore that address without parsing it. `default_handler`, if
    given, is called as `default_handler(address)` for any other address.
    An empty `allow_from` accepts any sender.

    Why not python-osc's server: its per-packet path (a socketserver request
    object, a regex match of the address against every mapping, a generic
    argument parser) cost ~30 µs per packet on a laptop and several times that
    on the Pi, on a thread that shares the interpreter lock with the render
    loop. The audio server alone sends ~400 packets a second. Here a packet
    is one dict lookup and one `struct.unpack_from`. Bundles and unusual
    argument types still go through python-osc's parser.
    """

    def __init__(
        self,
        server_address: tuple[str, int],
        handlers: Mapping[str, Handler | None],
        allow_from: tuple[IPNetwork, ...] = (),
        default_handler: Callable[[str], None] | None = None,
    ):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.bind(server_address)
        except OSError:
            self._sock.close()
            raise
        # A short timeout so `shutdown()` is noticed without a wake-up packet.
        self._sock.settimeout(0.25)
        self.server_address = self._sock.getsockname()
        self._handlers: dict[bytes, tuple[str, Handler | None]] = {
            addr.encode("ascii"): (addr, fn) for addr, fn in handlers.items()
        }
        self._default = default_handler
        self.allow_from = tuple(allow_from)
        self.rejected: int = 0       # packets from senders off the allowlist
        self.malformed: int = 0      # packets that could not be parsed
        self.socket_errors: int = 0  # receive calls the OS refused
        self._verdicts: dict[str, bool] = {}
        self._structs: dict[bytes, struct.Struct | None] = {}
        self._stopping = False
        self._stopped = threading.Event()
        self._stopped.set()

    # ---- lifecycle (same names as socketserver, which this replaced) ----

    def serve_forever(self) -> None:
        self._stopped.clear()
        buf = bytearray(65536)
        recv = self._sock.recvfrom_into
        check = bool(self.allow_from)
        try:
            while not self._stopping:
                try:
                    n, peer = recv(buf)
                except TimeoutError:
                    continue
                except OSError as e:
                    if self._stopping:
                        break
                    # Keep listening: a dead listener means no audio or WZRD
                    # input until the service restarts.
                    self.socket_errors += 1
                    if self.socket_errors == 1:
                        log.warning("OSC socket error on %s: %s", self.server_address, e)
                    self._stopped.wait(0.05)   # never set while running: a plain pause
                    continue
                if check and not self._allowed(peer[0]):
                    self.rejected += 1
                    continue
                try:
                    self._dispatch(bytes(buf[:n]))
                except Exception:  # noqa: BLE001 - one bad packet must not stop the stream
                    self.malformed += 1
        finally:
            self._stopped.set()

    def shutdown(self) -> None:
        self._stopping = True
        self._stopped.wait(timeout=2.0)

    def server_close(self) -> None:
        self._sock.close()

    # ---- per packet ----

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

    def _dispatch(self, data: bytes) -> None:
        if data.startswith(b"#bundle"):
            self._dispatch_slow(data)
            return
        end = data.index(b"\0")
        entry = self._handlers.get(data[:end])
        if entry is None:
            if self._default is not None:
                self._default(data[:end].decode("ascii", "replace"))
            return
        address, handler = entry
        if handler is None:
            return
        i = (end + 4) & ~3                    # type tags start on a 4-byte boundary
        if i >= len(data) or data[i] != 0x2C:  # no ",tags" string: no arguments
            handler(address)
            return
        tags_end = data.index(b"\0", i)
        tags = data[i + 1:tags_end]
        try:
            unpacker = self._structs[tags]
        except KeyError:
            unpacker = self._structs[tags] = self._struct_for(tags)
        if unpacker is None:
            self._dispatch_slow(data)
            return
        handler(address, *unpacker.unpack_from(data, (tags_end + 4) & ~3))

    @staticmethod
    def _struct_for(tags: bytes) -> struct.Struct | None:
        """One Struct for a message whose arguments are all fixed-size numbers
        (every message ledctl consumes), else None = use the generic parser."""
        try:
            return struct.Struct(">" + "".join(_FIXED[t] for t in tags))
        except KeyError:
            return None

    def _dispatch_slow(self, data: bytes) -> None:
        """Bundles, strings, blobs: python-osc's parser handles everything."""
        from pythonosc.osc_packet import OscPacket  # noqa: PLC0415

        for timed in OscPacket(data).messages:
            msg = timed.message
            entry = self._handlers.get(msg.address.encode("ascii", "replace"))
            if entry is None:
                if self._default is not None:
                    self._default(msg.address)
            elif entry[1] is not None:
                entry[1](msg.address, *msg.params)


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
