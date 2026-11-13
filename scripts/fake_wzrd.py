#!/usr/bin/env python3
"""Stand-in for WZRD's LED link: sends the two C-LED messages over OSC/UDP.

    uv run python scripts/fake_wzrd.py                    # 127.0.0.1:9100, 60 Hz
    uv run python scripts/fake_wzrd.py --flash            # level jumps every 0.5 s
    uv run python scripts/fake_wzrd.py --brightness 0     # WZRD's BLACKOUT
    uv run python scripts/fake_wzrd.py --host <pi> --colours 4 --white 0.3

Each frame is two datagrams sent back to back, as WZRD does:

    /wzrd/colors   level, white, then 12 x (r, g, b, weight)
    /wzrd/masters  brightness, saturation

The fake wall is `--colours` (2..4) hues spaced evenly round the hue circle,
with unequal shares, rotating once every `--period` seconds. A hue between two
bin centres splits its share over both, so the palette glides instead of
stepping, and two to eight bins carry weight at any moment.
"""

from __future__ import annotations

import argparse
import colorsys
import time

from pythonosc.udp_client import SimpleUDPClient

N_BINS = 12
# Shares of the coloured light per wall colour (normalised to the count used).
_SHARES = (0.5, 0.3, 0.15, 0.05)


def _srgb(v: float) -> float:
    """Linear light -> sRGB encoding (C-LED sends bin colours sRGB-encoded)."""
    return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


def _hue_colour(hue: float) -> tuple[float, float, float]:
    """Full-saturation colour at a linear-light hue, largest channel 1, sRGB."""
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, 1.0, 1.0)
    return _srgb(r), _srgb(g), _srgb(b)


def colors_message(phase: float, n_colours: int, level: float, white: float) -> list[float]:
    """The 50 floats of `/wzrd/colors` for a wall rotated by `phase` (0..1)."""
    shares = _SHARES[:n_colours]
    total = sum(shares)
    weights = [0.0] * N_BINS
    colours = [_hue_colour(i / N_BINS) for i in range(N_BINS)]  # empty bin = centre hue
    for j, share in enumerate(shares):
        hue = (phase + j / n_colours) % 1.0
        pos = hue * N_BINS
        lo = int(pos) % N_BINS
        hi = (lo + 1) % N_BINS
        frac = pos - int(pos)
        for b, w in ((lo, 1.0 - frac), (hi, frac)):
            if w > 0.0:
                weights[b] += w * share / total
                colours[b] = _hue_colour(hue)
    out = [float(level), float(white)]
    for (r, g, b), w in zip(colours, weights, strict=True):
        out += [r, g, b, w]
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9100)
    p.add_argument("--rate", type=float, default=60.0, help="frames per second (max 100)")
    p.add_argument("--colours", type=int, default=2, choices=(2, 3, 4),
                   help="how many wall colours")
    p.add_argument("--period", type=float, default=30.0,
                   help="seconds for the palette to rotate once round the hue circle")
    p.add_argument("--level", type=float, default=0.25, help="the wall's level, 0..1")
    p.add_argument("--white", type=float, default=0.0,
                   help="share of the light that is colourless, 0..1")
    p.add_argument("--flash", action="store_true",
                   help="jump level to 1 for 80 ms every half second")
    p.add_argument("--brightness", type=float, default=1.0,
                   help="WZRD master brightness, 0..2 (0 = BLACKOUT)")
    p.add_argument("--saturation", type=float, default=1.0,
                   help="WZRD master saturation, 0..2")
    p.add_argument("--seconds", type=float, default=0.0, help="stop after this long (0 = run on)")
    args = p.parse_args()

    client = SimpleUDPClient(args.host, args.port)
    period = max(1.0 / args.rate, 0.010)   # C-LED: never two frames closer than 10 ms
    masters = [float(args.brightness), float(args.saturation)]
    print(f"fake WZRD -> {args.host}:{args.port} at {1 / period:.0f} Hz "
          f"({args.colours} colours{', flashing' if args.flash else ''}); Ctrl-C stops")

    t0 = time.perf_counter()
    next_at = t0
    sent = 0
    try:
        while True:
            now = time.perf_counter()
            t = now - t0
            if args.seconds and t >= args.seconds:
                break
            level = args.level
            if args.flash and (t % 0.5) < 0.08:
                level = 1.0
            client.send_message(
                "/wzrd/colors",
                colors_message(t / args.period, args.colours, level, args.white),
            )
            client.send_message("/wzrd/masters", masters)
            sent += 1
            # Next frame on the clock, but never under 10 ms after this one.
            next_at = max(next_at + period, now + 0.010)
            # Sleep most of the wait, then spin the last 2 ms: time.sleep alone
            # overshoots by milliseconds and the rate would read 57, not 60.
            delay = next_at - time.perf_counter()
            if delay > 0.002:
                time.sleep(delay - 0.002)
            while time.perf_counter() < next_at:
                pass
    except KeyboardInterrupt:
        pass
    print(f"sent {sent} frames in {time.perf_counter() - t0:.1f} s")


if __name__ == "__main__":
    main()
