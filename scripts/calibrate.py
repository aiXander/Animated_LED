"""Light one LED at a time in red to verify wiring matches `config.yaml`.

Usage:
    python scripts/calibrate.py --base-url http://127.0.0.1:8000 --step 100 --interval 1.5

The server walks the strip server-side; this script just kicks the walk off,
prints which LED is currently lit (and which strip it belongs to per the
running topology), and stops the walk on Ctrl+C. Pair it with the live view
in a browser — the same `current_index` shows up there too.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Any

import httpx


def _print_strip_index(topology: dict[str, Any], gid: int) -> str:
    for s in topology["strips"]:
        if s["pixel_offset"] <= gid < s["pixel_offset"] + s["pixel_count"]:
            local = gid - s["pixel_offset"]
            if s.get("reversed"):
                local = s["pixel_count"] - 1 - local
            return f"{s['id']} (local #{local})"
    return "<no strip>"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--step", type=int, default=100)
    p.add_argument("--interval", type=float, default=1.5)
    p.add_argument("--manual", action="store_true",
                   help="Step manually with the Enter key (overrides --interval).")
    args = p.parse_args(argv)

    client = httpx.Client(base_url=args.base_url, timeout=5.0)

    # Pull topology once for the strip lookup.
    topo = client.get("/topology").raise_for_status().json()
    n = topo["pixel_count"]
    print(f"calibrate: {n} pixels across {len(topo['strips'])} strips")

    if args.manual:
        try:
            for gid in range(0, n, args.step):
                client.post("/calibration/solo", json={"indices": [gid]}).raise_for_status()
                strip_label = _print_strip_index(topo, gid)
                input(f"  lit #{gid:>5}  {strip_label}    [enter for next, ctrl-c to stop] ")
        except (KeyboardInterrupt, EOFError):
            print("\nstopping.")
        finally:
            client.post("/calibration/stop")
            client.close()
        return 0

    # Auto-walk: ask the server to do it, then poll /state to label each step.
    client.post(
        "/calibration/walk", json={"step": args.step, "interval": args.interval}
    ).raise_for_status()

    def _stop(*_: object) -> None:
        client.post("/calibration/stop")
        client.close()
        print("\nstopped.")
        sys.exit(0)
    signal.signal(signal.SIGINT, _stop)

    last_idx = -1
    print(f"walking step={args.step} interval={args.interval}s — ctrl-c to stop")
    while True:
        cal = client.get("/state").json().get("calibration") or {}
        idx = cal.get("current")
        if idx is not None and idx != last_idx:
            last_idx = idx
            print(f"  lit #{idx:>5}  {_print_strip_index(topo, idx)}")
        # Sleep less than the walk interval so we don't miss steps.
        time.sleep(min(0.1, args.interval / 4))


if __name__ == "__main__":
    sys.exit(main())
