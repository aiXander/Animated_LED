"""Named palettes baked to a fixed-size LUT.

Effect authors call `named_palette("fire") → (LUT_SIZE, 3) float32` and read
positions in [0, 1] via `palette_lerp(stops, t)`. The LUTs are baked once at
module load and shared across effects.
"""

from __future__ import annotations

import numpy as np

LUT_SIZE = 256


def _hex(c: str) -> tuple[float, float, float]:
    s = c.lstrip("#")
    if len(s) != 6:
        raise ValueError(f"hex colour must be #rrggbb, got {c!r}")
    r = int(s[0:2], 16) / 255.0
    g = int(s[2:4], 16) / 255.0
    b = int(s[4:6], 16) / 255.0
    return (r, g, b)


# Stops are (position, "#rrggbb"); positions must span [0, 1].
NAMED_STOPS: dict[str, list[tuple[float, str]]] = {
    "rainbow": [
        (0.00, "#ff0000"),
        (0.17, "#ff7700"),
        (0.33, "#ffff00"),
        (0.50, "#00ff00"),
        (0.67, "#00ffff"),
        (0.83, "#0000ff"),
        (1.00, "#ff00ff"),
    ],
    "fire": [
        (0.00, "#000000"),
        (0.20, "#330000"),
        (0.45, "#aa1a00"),
        (0.70, "#ff7700"),
        (0.90, "#ffdd44"),
        (1.00, "#ffffff"),
    ],
    "ice": [
        (0.00, "#000010"),
        (0.30, "#0a2a60"),
        (0.60, "#4488dd"),
        (0.85, "#bbeeff"),
        (1.00, "#ffffff"),
    ],
    "sunset": [
        (0.00, "#1a0033"),
        (0.30, "#7a1a4a"),
        (0.55, "#e64500"),
        (0.80, "#ffaa44"),
        (1.00, "#ffeebb"),
    ],
    "ocean": [
        (0.00, "#001020"),
        (0.40, "#003a55"),
        (0.70, "#1a8aaa"),
        (1.00, "#bbffee"),
    ],
    "warm": [
        (0.00, "#1a0a00"),
        (0.50, "#aa3300"),
        (1.00, "#ffcc44"),
    ],
    "cool": [
        (0.00, "#000a1a"),
        (0.45, "#143a6a"),
        (0.75, "#33aacc"),
        (1.00, "#ccf3ff"),
    ],
    "forest": [
        (0.00, "#020a02"),
        (0.30, "#0d3318"),
        (0.65, "#3d8a2e"),
        (0.90, "#cce066"),
        (1.00, "#fff7c2"),
    ],
    "lava": [
        (0.00, "#000000"),
        (0.25, "#220000"),
        (0.55, "#aa0a00"),
        (0.80, "#ff4400"),
        (1.00, "#ffe88a"),
    ],
    "neon": [
        (0.00, "#ff00aa"),
        (0.33, "#aa00ff"),
        (0.66, "#00ddff"),
        (1.00, "#22ff88"),
    ],
    "cyberpunk": [
        (0.00, "#0a0033"),
        (0.30, "#ff0099"),
        (0.65, "#00f0ff"),
        (1.00, "#fffba3"),
    ],
    "pastel": [
        (0.00, "#ffd4e1"),
        (0.30, "#fbf0aa"),
        (0.60, "#caf2c2"),
        (1.00, "#b8d8ff"),
    ],
    "candy": [
        (0.00, "#ff66cc"),
        (0.33, "#ffaa66"),
        (0.66, "#66e0ff"),
        (1.00, "#c2f48e"),
    ],
    "magma": [
        (0.00, "#000000"),
        (0.25, "#2a0a3a"),
        (0.55, "#9a2a5a"),
        (0.80, "#f06a3a"),
        (1.00, "#fcfdbf"),
    ],
    "viridis": [
        (0.00, "#440154"),
        (0.25, "#3b528b"),
        (0.50, "#21918c"),
        (0.75, "#5ec962"),
        (1.00, "#fde725"),
    ],
    "twilight": [
        (0.00, "#0d0b3a"),
        (0.30, "#5b2c6f"),
        (0.55, "#d05880"),
        (0.80, "#f5b08c"),
        (1.00, "#fff0e0"),
    ],
    "aurora": [
        (0.00, "#001a2a"),
        (0.30, "#0a4d4a"),
        (0.55, "#22cc88"),
        (0.80, "#88ddff"),
        (1.00, "#e6ccff"),
    ],
    "monochrome": [
        (0.00, "#000000"),
        (1.00, "#ffffff"),
    ],
    "ember": [
        (0.00, "#0a0000"),
        (0.40, "#5a0a00"),
        (0.75, "#cc3300"),
        (1.00, "#ffaa55"),
    ],
    "deepsea": [
        (0.00, "#000308"),
        (0.40, "#001a3a"),
        (0.75, "#0a4a88"),
        (1.00, "#5aaadd"),
    ],
    "white": [(0.00, "#ffffff"), (1.00, "#ffffff")],
    "black": [(0.00, "#000000"), (1.00, "#000000")],
}


def _bake_lut(stops: list[tuple[float, str]], size: int = LUT_SIZE) -> np.ndarray:
    if not stops:
        raise ValueError("palette must have at least one stop")
    pts = sorted(stops, key=lambda s: float(s[0]))
    xs = np.array([p[0] for p in pts], dtype=np.float32)
    cols = np.array([_hex(p[1]) for p in pts], dtype=np.float32)
    t = np.linspace(0.0, 1.0, size, dtype=np.float32)
    out = np.empty((size, 3), dtype=np.float32)
    out[:, 0] = np.interp(t, xs, cols[:, 0])
    out[:, 1] = np.interp(t, xs, cols[:, 1])
    out[:, 2] = np.interp(t, xs, cols[:, 2])
    return out


# Bake at module load — small, deterministic, callable from anywhere.
_BAKED: dict[str, np.ndarray] = {
    name: _bake_lut(stops) for name, stops in NAMED_STOPS.items()
}


def named_palette(name: str) -> np.ndarray:
    """Return the (LUT_SIZE, 3) float32 LUT for `name`. Read-only by convention."""
    if name not in _BAKED:
        raise ValueError(
            f"unknown palette {name!r}; available: {sorted(_BAKED.keys())}"
        )
    return _BAKED[name]


def named_palette_names() -> list[str]:
    return sorted(_BAKED.keys())


def bake_palette(stops: list[tuple[float, str]], size: int = LUT_SIZE) -> np.ndarray:
    """Bake a custom multi-stop palette to a LUT. Use sparingly — most effects
    should reference a named palette via `named_palette(name)`."""
    return _bake_lut(stops, size=size)
