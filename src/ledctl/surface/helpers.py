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


def hsv_to_rgb(h, s, v, out=None):
    """HSV → RGB. Scalars and arrays broadcast against each other; returns
    float32. Input shape `(...,)` → output `(..., 3)`. Scalar inputs return
    a `(3,)` array. If `out` is given, the result is written into it in place.

    Examples:
        hsv_to_rgb(0.33, 1.0, 1.0)               # → (3,) green
        hsv_to_rgb(np.array([0, 0.33, 0.66]), 1.0, 1.0)  # → (3, 3) RGB triples
        hsv_to_rgb(ctx.frames.x, 1.0, 1.0)       # → (N, 3) per-LED rainbow
        hsv_to_rgb(hue, 1.0, 1.0, out=self.out)  # → allocation-free per-LED fill
    """
    h = np.asarray(h, dtype=np.float32)
    s = np.asarray(s, dtype=np.float32)
    v = np.asarray(v, dtype=np.float32)
    shape = np.broadcast_shapes(h.shape, s.shape, v.shape)
    # Closed form:  channel = v*(1 - s) + v*s * clip(|k - 2| - 1, 0, 1)
    # with k = (h*6 + n) mod 6 and n = 5, 3, 1 for R, G, B. Computed on a
    # channel-major (3, ...) block so every step is one long contiguous pass
    # (broadcasting against a trailing axis of 3 would run 1800 loops of 3),
    # then copied into the (..., 3) result. No (N, 6) choice tables, no
    # gathers: the table version this replaces was the most expensive call in
    # most effects.
    h = np.broadcast_to(h, shape)
    h6 = h - np.floor(h)                      # wrap to [0, 1) without fmod
    h6 *= 6.0
    k = np.add.outer(_HSV_OFFSETS, h6)        # (3, ...): k - 2, before the wrap
    np.subtract(k, 6.0, out=k, where=k >= 4.0)
    np.abs(k, out=k)
    k -= 1.0
    np.clip(k, 0.0, 1.0, out=k)
    vs = v * s
    k *= vs
    k += v - vs
    if out is None:
        out = np.empty(shape + (3,), dtype=np.float32)
    out[...] = np.moveaxis(k, 0, -1)
    return out


# n - 2 for n = 5 (R), 3 (G), 1 (B): see hsv_to_rgb.
_HSV_OFFSETS = np.array([3.0, 1.0, -1.0], dtype=np.float32)


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


def pulse(x, width=0.5, out=None):
    """Cosine bump on [-width, +width], peak=1, else 0. Smooth shoulders."""
    x = np.asarray(x, dtype=np.float32)
    w = max(float(width), 1e-6)
    inside = np.abs(x) <= w
    res = np.where(inside, 0.5 * (1.0 + np.cos(np.pi * x / w)), 0.0)
    if out is None:
        return res.astype(np.float32, copy=False)
    np.copyto(out, res)
    return out


def tri(x, out=None):
    """Triangle wave with period 1, range [0, 1]. Peak at x=0.5."""
    x = np.mod(np.asarray(x, dtype=np.float32), 1.0)
    res = (1.0 - 2.0 * np.abs(x - 0.5)).astype(np.float32, copy=False)
    if out is None:
        return res
    np.copyto(out, res)
    return out


def wrap_dist(a, b, period=1.0, out=None):
    """Shortest signed distance from a to b on a circular axis of length `period`.

    Returns a value in [-period/2, +period/2]. Useful when comparing positions
    against a head moving along `u_loop` or any other wrapped coordinate.
    """
    p = float(period)
    a_arr = np.asarray(a, dtype=np.float32)
    b_arr = np.asarray(b, dtype=np.float32)
    d = np.mod(b_arr - a_arr + 0.5 * p, p) - 0.5 * p
    if out is None:
        return d.astype(np.float32, copy=False)
    np.copyto(out, d)
    return out


def palette_lerp(stops, t, out=None):
    """Sample a multi-stop palette at scalar/array t in [0, 1].

    Accepts any of:
      - a baked LUT — ndarray of shape (M, 3) such as `named_palette(name)`.
        Positions are assumed evenly spaced on [0, 1].
      - [(pos, "#rrggbb"), ...]    or  [(pos, (r, g, b)), ...]  — (pos, colour) pairs.
      - [(pos, r, g, b), ...]                                   — flat 4-tuples.
      - ["#rrggbb", "#rrggbb", ...] or [(r, g, b), ...]         — bare colours,
        positions distributed evenly on [0, 1].

    Returns float32 of shape `t.shape + (3,)`. If `out` is given, the result
    is written into it in place.
    """
    # LUT fast path: (M, 3) ndarray. Catches `palette_lerp(named_palette('fire'), …)`.
    # Evenly spaced stops need no search: index straight into the table and
    # blend the two neighbours.
    if isinstance(stops, np.ndarray) and stops.ndim == 2 and stops.shape[1] == 3 \
            and stops.shape[0] >= 2:
        lut = stops.astype(np.float32, copy=False)
        last = lut.shape[0] - 1
        x = np.clip(np.asarray(t, dtype=np.float32) * last, 0.0, float(last))
        i0 = np.minimum(x.astype(np.intp), last - 1)
        frac = (x - i0)[..., None]
        lo = lut[i0]
        # A fresh array: for a scalar `t`, `lut[i0]` is a VIEW of the shared
        # palette table, so nothing below may write into `lo` or `lut`.
        res = lut[i0 + 1] - lo
        res *= frac
        res += lo
        if out is None:
            return res
        np.copyto(out, res)
        return out
    if isinstance(stops, np.ndarray) and stops.ndim == 2 and stops.shape[1] == 3:
        xs = np.linspace(0.0, 1.0, stops.shape[0], dtype=np.float32)
        cols_arr = stops.astype(np.float32, copy=False)
    else:
        xs_list, cols = _parse_palette_stops(stops)
        order = np.argsort(np.asarray(xs_list, dtype=np.float32))
        xs = np.asarray(xs_list, dtype=np.float32)[order]
        cols_arr = np.stack([cols[i] for i in order], axis=0)
    t = np.asarray(t, dtype=np.float32)
    flat = t.ravel()
    r = np.interp(flat, xs, cols_arr[:, 0])
    g = np.interp(flat, xs, cols_arr[:, 1])
    b = np.interp(flat, xs, cols_arr[:, 2])
    res = np.stack([r, g, b], axis=-1).astype(np.float32, copy=False)
    res = res.reshape(t.shape + (3,))
    if out is None:
        return res
    np.copyto(out, res)
    return out


_NUMERIC = (int, float, np.floating, np.integer)


def _to_rgb_triplet(c) -> np.ndarray:
    """Normalise '#rrggbb', (r, g, b), [r, g, b], or ndarray-of-3 to (3,) float32."""
    if isinstance(c, str):
        return hex_to_rgb(c)
    arr = np.asarray(c, dtype=np.float32)
    if arr.shape != (3,):
        raise ValueError(
            f"palette_lerp: colour must be '#rrggbb' or a 3-element (r, g, b); "
            f"got shape {arr.shape}"
        )
    return arr


def _parse_palette_stops(stops):
    """Return (xs_list, [rgb (3,) f32, ...]) from a flexible stops sequence.

    Raises ValueError with an LLM-actionable message on any malformed input —
    the fence-test then surfaces that under LAST EFFECT ERROR.
    """
    try:
        seq = list(stops)
    except TypeError as e:
        raise ValueError(
            f"palette_lerp: `stops` must be a sequence or (M, 3) ndarray; "
            f"got {type(stops).__name__}"
        ) from e
    if len(seq) < 1:
        raise ValueError("palette_lerp: `stops` is empty — give at least 1 colour.")

    first = seq[0]
    # Bare hex list: ['#ff0000', '#00ff00', ...] — distribute evenly.
    if isinstance(first, str):
        cols = [_to_rgb_triplet(c) for c in seq]
        xs = list(np.linspace(0.0, 1.0, max(len(cols), 2), dtype=np.float32)[: len(cols)])
        return xs, cols
    if not isinstance(first, (list, tuple, np.ndarray)):
        raise ValueError(
            f"palette_lerp: stop {first!r} (type {type(first).__name__}) not understood — "
            "use '#rrggbb', (r, g, b), (pos, colour), or (pos, r, g, b)."
        )
    first_len = len(first)
    # Bare (r, g, b) list — all numeric, length 3, no per-stop position.
    if first_len == 3 and all(isinstance(v, _NUMERIC) for v in first):
        cols = [_to_rgb_triplet(c) for c in seq]
        xs = list(np.linspace(0.0, 1.0, max(len(cols), 2), dtype=np.float32)[: len(cols)])
        return xs, cols
    # (pos, colour) tuples — standard 2-tuple form.
    if first_len == 2:
        xs, cols = [], []
        for s in seq:
            if len(s) != 2:
                raise ValueError(
                    f"palette_lerp: mixed stop lengths — expected (pos, colour) 2-tuples, "
                    f"got {s!r}."
                )
            xs.append(float(s[0]))
            cols.append(_to_rgb_triplet(s[1]))
        return xs, cols
    # (pos, r, g, b) flat 4-tuples — common LLM shorthand.
    if first_len == 4 and all(isinstance(v, _NUMERIC) for v in first):
        xs, cols = [], []
        for s in seq:
            if len(s) != 4:
                raise ValueError(
                    f"palette_lerp: mixed stop lengths — expected (pos, r, g, b) 4-tuples, "
                    f"got {s!r}."
                )
            xs.append(float(s[0]))
            cols.append(_to_rgb_triplet(tuple(s[1:])))
        return xs, cols
    raise ValueError(
        f"palette_lerp: stop {first!r} has length {first_len}; expected one of: "
        "'#rrggbb', (r, g, b), (pos, colour), or (pos, r, g, b)."
    )
