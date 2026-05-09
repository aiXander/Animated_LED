"""Helpers exposed in the runtime namespace for LLM-authored effects.

Everything here is pure numpy + small math primitives. The LLM imports
nothing — these names are injected into its module globals via
`build_runtime_namespace()` (see `runtime.py`).
"""

from __future__ import annotations

import logging
from functools import lru_cache

import numpy as np

from .palettes import LUT_SIZE, named_palette  # noqa: F401  re-exported

PI = float(np.pi)
TAU = float(2.0 * np.pi)

# Module-level logger so effects can `log.warning(...)` instead of `print`.
log = logging.getLogger("ledctl.effect")


@lru_cache(maxsize=2048)
def hex_to_rgb(hex_str: str) -> np.ndarray:
    """Convert '#rrggbb' to a (3,) float32 RGB in [0, 1]. Cached."""
    s = hex_str.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"hex colour must be #rrggbb, got {hex_str!r}")
    r = int(s[0:2], 16) / 255.0
    g = int(s[2:4], 16) / 255.0
    b = int(s[4:6], 16) / 255.0
    return np.array([r, g, b], dtype=np.float32)


def hsv_to_rgb(h, s, v):
    """HSV → RGB. Scalar or array. Returns float32, broadcasting like numpy."""
    h = np.asarray(h, dtype=np.float32)
    s = np.asarray(s, dtype=np.float32)
    v = np.asarray(v, dtype=np.float32)
    h6 = (np.mod(h, 1.0) * 6.0)
    i = np.floor(h6).astype(np.int32)
    f = h6 - i.astype(np.float32)
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    i_mod = i % 6
    # Stack and gather — works for scalars and arrays alike.
    r_choices = np.stack([v, q, p, p, t, v], axis=-1)
    g_choices = np.stack([t, v, v, q, p, p], axis=-1)
    b_choices = np.stack([p, p, t, v, v, q], axis=-1)
    if h6.ndim == 0:
        return np.array(
            [r_choices[i_mod], g_choices[i_mod], b_choices[i_mod]],
            dtype=np.float32,
        )
    out = np.stack(
        [
            np.take_along_axis(r_choices, i_mod[..., None], axis=-1)[..., 0],
            np.take_along_axis(g_choices, i_mod[..., None], axis=-1)[..., 0],
            np.take_along_axis(b_choices, i_mod[..., None], axis=-1)[..., 0],
        ],
        axis=-1,
    )
    return out.astype(np.float32, copy=False)


def lerp(a, b, t, out=None):
    """a*(1-t) + b*t with broadcasting. If `out` is given, write in place."""
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    t = np.asarray(t, dtype=np.float32)
    if out is None:
        return (a * (1.0 - t) + b * t).astype(np.float32, copy=False)
    np.subtract(b, a, out=out)
    out *= t
    out += a
    return out


def clip01(x, out=None):
    """np.clip(x, 0, 1). If `out` is None, returns a new array."""
    return np.clip(x, 0.0, 1.0, out=out)


def gauss(x, sigma, out=None):
    """Normalised gaussian profile with peak = 1 at x=0."""
    sigma = max(float(sigma), 1e-6)
    if out is None:
        return np.exp(-(np.asarray(x, dtype=np.float32) ** 2) / (2.0 * sigma * sigma))
    np.square(x, out=out)
    out *= -(1.0 / (2.0 * sigma * sigma))
    np.exp(out, out=out)
    return out


def pulse(x, width=0.5):
    """Cosine bump on [-width, +width], peak=1, else 0. Smooth shoulders."""
    x = np.asarray(x, dtype=np.float32)
    w = max(float(width), 1e-6)
    inside = np.abs(x) <= w
    out = np.where(inside, 0.5 * (1.0 + np.cos(np.pi * x / w)), 0.0)
    return out.astype(np.float32, copy=False)


def tri(x):
    """Triangle wave with period 1, range [0, 1]. Peak at x=0.5."""
    x = np.mod(np.asarray(x, dtype=np.float32), 1.0)
    return (1.0 - 2.0 * np.abs(x - 0.5)).astype(np.float32, copy=False)


def wrap_dist(a, b, period=1.0):
    """Shortest signed distance from a to b on a circular axis of length `period`.

    Returns a value in [-period/2, +period/2]. Useful when comparing positions
    against a head moving along `u_loop` or any other wrapped coordinate.
    """
    p = float(period)
    a_arr = np.asarray(a, dtype=np.float32)
    b_arr = np.asarray(b, dtype=np.float32)
    d = np.mod(b_arr - a_arr + 0.5 * p, p) - 0.5 * p
    return d.astype(np.float32, copy=False)


def palette_lerp(stops, t):
    """Sample a multi-stop palette at scalar/array t in [0, 1].

    `stops` is a list of (pos, "#rrggbb") or (pos, (r, g, b)) tuples in any
    order. Returns float32 of shape (..., 3).
    """
    pts = sorted(stops, key=lambda s: float(s[0]))
    xs = np.array([float(p[0]) for p in pts], dtype=np.float32)
    cols = []
    for _, c in pts:
        if isinstance(c, str):
            cols.append(hex_to_rgb(c))
        else:
            cols.append(np.asarray(c, dtype=np.float32))
    cols_arr = np.stack(cols, axis=0)
    t = np.asarray(t, dtype=np.float32)
    flat = t.ravel()
    r = np.interp(flat, xs, cols_arr[:, 0])
    g = np.interp(flat, xs, cols_arr[:, 1])
    b = np.interp(flat, xs, cols_arr[:, 2])
    out = np.stack([r, g, b], axis=-1).astype(np.float32, copy=False)
    return out.reshape(t.shape + (3,))
