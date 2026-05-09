"""Named palettes + LUT bake machinery.

The LUT size is mutable at boot (driven by `output.lut_size` in YAML); 256 is
the default. Bumping higher (e.g. 1024) is purely a one-time bake + memory
cost — useful if a smooth scalar walking the full palette shows visible
"stair" banding on the 1800-LED install.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .shapes import hex_to_rgb01, hsv_to_rgb01

LUT_SIZE = 256


def set_lut_size(n: int) -> None:
    """Override the palette LUT size. Must be called before any palettes bake."""
    global LUT_SIZE
    if n < 2:
        raise ValueError(f"lut_size must be >= 2, got {n}")
    LUT_SIZE = int(n)


# Tagged: each entry is {"interp": "rgb"|"hsv", "stops": ...}.
#   - "rgb" stops are (pos, "#rrggbb") tuples; intentionally vary brightness
#     (fire / ice / sunset / ocean go dark->bright on purpose).
#   - "hsv" stops are {pos, hue, sat?, val?} dicts; they bake via hue-space
#     interpolation so the LUT stays at uniform brightness with no muddy /
#     grey midpoints between complementary colours.
NAMED_PALETTES: dict[str, dict[str, Any]] = {
    "rainbow": {
        "interp": "hsv",
        "stops": [
            {"pos": 0.0, "hue": 0.0},
            {"pos": 1.0, "hue": 360.0},
        ],
    },
    "fire": {
        "interp": "rgb",
        "stops": [
            (0.00, "#000000"),
            (0.25, "#600000"),
            (0.50, "#ff3000"),
            (0.75, "#ffa000"),
            (1.00, "#ffff80"),
        ],
    },
    "ice": {
        "interp": "rgb",
        "stops": [
            (0.0, "#000010"),
            (0.4, "#003080"),
            (0.7, "#00a0e0"),
            (1.0, "#ffffff"),
        ],
    },
    "sunset": {
        "interp": "rgb",
        "stops": [
            (0.0, "#100030"),
            (0.4, "#c02060"),
            (0.7, "#ff7020"),
            (1.0, "#ffe080"),
        ],
    },
    "ocean": {
        "interp": "rgb",
        "stops": [
            (0.0, "#001020"),
            (0.4, "#006080"),
            (0.7, "#20a0c0"),
            (1.0, "#c0f0ff"),
        ],
    },
    "warm": {
        "interp": "rgb",
        "stops": [
            (0.0, "#ff3000"),
            (0.5, "#ffa000"),
            (1.0, "#ff5000"),
        ],
    },
    "white": {"interp": "rgb", "stops": [(0.0, "#ffffff"), (1.0, "#ffffff")]},
    "black": {"interp": "rgb", "stops": [(0.0, "#000000"), (1.0, "#000000")]},
}


def _bake_lut(positions: np.ndarray, colors: np.ndarray) -> np.ndarray:
    x = np.linspace(0.0, 1.0, LUT_SIZE, dtype=np.float32)
    lut = np.empty((LUT_SIZE, 3), dtype=np.float32)
    for ch in range(3):
        lut[:, ch] = np.interp(x, positions, colors[:, ch])
    return lut


def _lut_from_named(name: str) -> np.ndarray:
    if name.startswith("mono_"):
        rgb = hex_to_rgb01(name[5:])
        positions = np.array([0.0, 1.0], dtype=np.float32)
        colors = np.stack([rgb, rgb])
        return _bake_lut(positions, colors)
    if name not in NAMED_PALETTES:
        raise ValueError(
            f"unknown palette {name!r}; choose one of {sorted(NAMED_PALETTES)} "
            f"or mono_<hex>"
        )
    spec = NAMED_PALETTES[name]
    if spec["interp"] == "hsv":
        return _lut_from_hsv_stops(spec["stops"])
    stops = spec["stops"]
    positions = np.array([p for p, _ in stops], dtype=np.float32)
    colors = np.stack([hex_to_rgb01(c) for _, c in stops])
    return _bake_lut(positions, colors)


def _lut_from_stops(stops: list[dict[str, Any]]) -> np.ndarray:
    if len(stops) < 2:
        raise ValueError("palette_stops needs at least 2 stops")
    sorted_stops = sorted(stops, key=lambda s: s["pos"])
    positions = np.array([s["pos"] for s in sorted_stops], dtype=np.float32)
    colors = np.stack([hex_to_rgb01(s["color"]) for s in sorted_stops])
    return _bake_lut(positions, colors)


def _lut_from_hsv_stops(stops: list[dict[str, Any]]) -> np.ndarray:
    """Bake an RGB LUT (size = `LUT_SIZE`) from hue/sat/val stops via HSV-space lerp.

    Hue can take any signed value (interpreted mod 360 only at the final
    HSV->RGB step), so the user explicitly controls the path: stops at
    hue=0,360 walks the full chromatic circle red->...->red the long way;
    stops at hue=0,-180 goes red->magenta->blue (the other way).
    """
    if len(stops) < 2:
        raise ValueError("palette_hsv needs at least 2 stops")
    sorted_stops = sorted(stops, key=lambda s: s["pos"])
    positions = np.array([s["pos"] for s in sorted_stops], dtype=np.float32)
    hues = np.array([s["hue"] for s in sorted_stops], dtype=np.float32)
    sats = np.array(
        [s.get("sat", 1.0) for s in sorted_stops], dtype=np.float32
    )
    vals = np.array(
        [s.get("val", 1.0) for s in sorted_stops], dtype=np.float32
    )
    x = np.linspace(0.0, 1.0, LUT_SIZE, dtype=np.float32)
    h = np.interp(x, positions, hues).astype(np.float32, copy=False)
    s = np.interp(x, positions, sats).astype(np.float32, copy=False)
    v = np.interp(x, positions, vals).astype(np.float32, copy=False)
    return hsv_to_rgb01(h, s, v)
