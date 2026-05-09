"""Pure colour / shape utilities used by the surface primitives.

No dependencies on the registry, the compiler, or any primitive — these are
the leaf-level functions that every other module reaches for.
"""

from __future__ import annotations

import numpy as np


def hex_to_rgb01(s: str) -> np.ndarray:
    """Parse a #rrggbb (or rrggbb) hex into a float32 RGB array in [0, 1]."""
    raw = s.lstrip("#")
    if len(raw) != 6:
        raise ValueError(f"hex colour must be 6 hex digits, got {s!r}")
    try:
        r = int(raw[0:2], 16)
        g = int(raw[2:4], 16)
        b = int(raw[4:6], 16)
    except ValueError as e:
        raise ValueError(f"hex colour must be 6 hex digits, got {s!r}") from e
    return np.array([r / 255.0, g / 255.0, b / 255.0], dtype=np.float32)


def hsv_to_rgb01(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Vectorised HSV->RGB. Hue in degrees (any sign / magnitude — taken mod 360);
    s, v in [0, 1]. Returns (N, 3) float32 in [0, 1].

    Used to bake `palette_hsv` (and the named `rainbow` palette) without the
    muddy / desaturated midpoints that RGB-space lerp gives between
    complementary colours.
    """
    h = np.mod(h.astype(np.float32, copy=False), 360.0)
    s = s.astype(np.float32, copy=False)
    v = v.astype(np.float32, copy=False)
    c = v * s
    h6 = h / 60.0
    x = c * (1.0 - np.abs(np.mod(h6, 2.0) - 1.0))
    zero = np.zeros_like(h)
    r_t = np.stack([c, x, zero, zero, x, c])
    g_t = np.stack([x, c, c, x, zero, zero])
    b_t = np.stack([zero, zero, x, c, c, x])
    seg = np.minimum(h6.astype(np.int32), 5)
    rows = np.arange(h.shape[0])
    m = v - c
    return np.stack(
        [r_t[seg, rows] + m, g_t[seg, rows] + m, b_t[seg, rows] + m],
        axis=1,
    ).astype(np.float32, copy=False)


def apply_shape(
    phase: np.ndarray,
    shape: str,
    softness: float,
    width: float,
    out: np.ndarray,
) -> None:
    """Evaluate `shape` on a fract-phase array and write into `out` (N,)."""
    if shape == "cosine":
        smooth = (np.cos(2.0 * np.pi * phase) + 1.0) * 0.5
        if softness >= 1.0:
            np.copyto(out, smooth.astype(np.float32, copy=False))
        elif softness <= 0.0:
            np.copyto(out, (smooth > 0.5).astype(np.float32))
        else:
            hard = (smooth > 0.5).astype(np.float32)
            np.copyto(
                out,
                (softness * smooth + (1.0 - softness) * hard).astype(
                    np.float32, copy=False
                ),
            )
    elif shape == "sawtooth":
        np.copyto(out, phase.astype(np.float32, copy=False))
    elif shape == "pulse":
        np.copyto(out, (phase < 0.5).astype(np.float32))
    elif shape == "gauss":
        d = phase - np.round(phase)
        np.copyto(
            out,
            np.exp(-(d * d) / max(width * width, 1e-9)).astype(
                np.float32, copy=False
            ),
        )
    else:
        raise ValueError(f"unknown shape {shape!r}")


def clip_scalar(v: float, lo: float, hi: float) -> float:
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


# Back-compat aliases (private names kept for any external importer that
# poked at internals — tests historically have).
_hsv_to_rgb01 = hsv_to_rgb01
_apply_shape = apply_shape
_clip_scalar = clip_scalar
