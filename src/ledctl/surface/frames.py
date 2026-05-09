"""Named coordinate frames derived from a `Topology`.

A *frame* is a per-LED scalar (or per-LED vector), precomputed once at
topology-build time, that primitives can address by name. The Cartesian
`x / y / z` are the originals — every other frame is a higher-level view
that lets the LLM say "around the loop" or "from the centre outward"
without composing it from scratch.

The mapping is **rig-aware**: `u_loop` walks LEDs in clockwise chain order,
not raw Cartesian distance, so a `wave(axis=u_loop)` reads as motion around
the perimeter regardless of strip count, length, or `reversed` flags.

This module owns nothing stateful — it's pure derived data. Adding a new
frame is a one-line change here, plus a one-line entry in
`FRAME_DESCRIPTIONS` so the LLM knows it exists.
"""

from __future__ import annotations

import numpy as np

# Frame names exposed to the agent (and to the operator UI). The strings
# below are referenced from the system prompt and from `Wave / Gradient /
# Position / Frame` axis validation, so renaming one is a breaking change
# for any existing preset that addresses it.
#
# Each one-line description is what shows up in the prompt's FRAMES block —
# keep them tight (this is the dominant token cost).
FRAME_DESCRIPTIONS: dict[str, str] = {
    "x":             "Cartesian x in [0, 1] (left → right)",
    "y":             "Cartesian y in [0, 1] (bottom → top)",
    "z":             "Cartesian z in [0, 1] (back → front)",
    "signed_x":      "Cartesian x in [-1, 1]",
    "signed_y":      "Cartesian y in [-1, 1]",
    "signed_z":      "Cartesian z in [-1, 1]",
    "radius":        "Distance from centre column √(x²+y²), clipped to [0, 1]",
    "angle":         "Angle from centre, atan2(y,x)/2π wrapped to [0, 1]",
    "u_loop":        "Clockwise arc position around the rig, [0, 1] from top centre",
    "u_loop_signed": "u_loop centred at top: [-0.5, +0.5]",
    "side_top":      "1.0 on the top row, 0.0 on the bottom",
    "side_bottom":   "1.0 on the bottom row, 0.0 on the top",
    "side_signed":   "+1.0 top / -1.0 bottom",
    "axial_dist":    "|x| in [0, 1] — distance from centre column",
    "axial_signed": "x in [-1, 1] — symmetric around centre column",
    "corner_dist":   "Distance to nearest corner, normalised",
    "strip_id":      "Integer 0..K-1 per strip (operator-facing; see config)",
    "chain_index":   "Local index along the strip from the controller end, [0, 1]",
    "distance":      "√(x²+y²+z²) normalised to [0, 1] (legacy, see `radius`)",
}


def build_frames(
    *,
    normalised_positions: np.ndarray,
    leds: list,
    strips: list,
    pixel_count: int,
) -> dict[str, np.ndarray]:
    """Compute every named frame for a given topology.

    Returns a dict mapping frame name → ndarray. Most frames are float32 of
    shape (N,); `strip_id` is int32; vector frames (none yet) would be (N, k).
    """
    pos = normalised_positions  # (N, 3) in [-1, 1] per axis (subject to bbox)
    out: dict[str, np.ndarray] = {}

    # Cartesian, normalised to [0, 1] (the form existing primitives use).
    out["x"] = ((pos[:, 0] + 1.0) * 0.5).astype(np.float32)
    out["y"] = ((pos[:, 1] + 1.0) * 0.5).astype(np.float32)
    out["z"] = ((pos[:, 2] + 1.0) * 0.5).astype(np.float32)

    # Cartesian, signed [-1, 1].
    out["signed_x"] = pos[:, 0].astype(np.float32, copy=True)
    out["signed_y"] = pos[:, 1].astype(np.float32, copy=True)
    out["signed_z"] = pos[:, 2].astype(np.float32, copy=True)

    # Radial / angular around the centre column.
    r = np.sqrt(pos[:, 0] ** 2 + pos[:, 1] ** 2)
    r_max = max(float(r.max()), 1e-9)
    out["radius"] = np.clip(r / r_max, 0.0, 1.0).astype(np.float32)
    angle = np.arctan2(pos[:, 1], pos[:, 0]) / (2.0 * np.pi)
    out["angle"] = np.mod(angle, 1.0).astype(np.float32)

    # Top / bottom split (the rig is two parallel rows; the y sign decides).
    out["side_top"] = (pos[:, 1] > 0.0).astype(np.float32)
    out["side_bottom"] = (pos[:, 1] < 0.0).astype(np.float32)
    out["side_signed"] = np.sign(pos[:, 1]).astype(np.float32)

    # Axial (left-right vs centre column).
    out["axial_dist"] = np.clip(np.abs(pos[:, 0]), 0.0, 1.0).astype(np.float32)
    out["axial_signed"] = np.clip(pos[:, 0], -1.0, 1.0).astype(np.float32)

    # Distance to the nearest of the four normalised corners.
    corners = np.array(
        [(-1.0, -1.0), (-1.0, 1.0), (1.0, -1.0), (1.0, 1.0)], dtype=np.float32
    )
    diffs = pos[:, None, :2] - corners[None, :, :]
    cdist = np.sqrt((diffs ** 2).sum(axis=2)).min(axis=1)
    cdist = cdist / max(float(cdist.max()), 1e-9)
    out["corner_dist"] = cdist.astype(np.float32)

    # Same shape as the legacy `position(axis="distance")`, kept for the
    # back-compat alias. Computed from full (x, y, z) in [-1, 1].
    d3 = np.sqrt(np.sum(pos ** 2, axis=1))
    out["distance"] = (d3 / max(float(d3.max()), 1e-9)).astype(np.float32)

    # Strip id (integer rank by listing order in the config).
    strip_id = np.zeros(pixel_count, dtype=np.int32)
    for rank, strip in enumerate(strips):
        lo = strip.pixel_offset
        hi = lo + strip.pixel_count
        strip_id[lo:hi] = rank
    out["strip_id"] = strip_id

    # Chain index per LED, normalised inside its own strip.
    chain_index = np.zeros(pixel_count, dtype=np.float32)
    for strip in strips:
        n = strip.pixel_count
        if n <= 0:
            continue
        lo = strip.pixel_offset
        if n == 1:
            chain_index[lo] = 0.0
        else:
            chain_index[lo : lo + n] = np.linspace(
                0.0, 1.0, n, dtype=np.float32
            )
    out["chain_index"] = chain_index

    # u_loop: clockwise rank in the perimeter walk, normalised to [0, 1].
    out["u_loop"] = _compute_u_loop(strips=strips, pixel_count=pixel_count, pos=pos)
    out["u_loop_signed"] = (
        np.mod(out["u_loop"] + 0.5, 1.0) - 0.5
    ).astype(np.float32)

    return out


def _compute_u_loop(
    *,
    strips: list,
    pixel_count: int,
    pos: np.ndarray,
) -> np.ndarray:
    """Build a clockwise chain-order coordinate.

    Strategy: classify each strip by (y_sign of its LEDs, x sign of its
    outer-end). Walk them clockwise starting at top centre:

        1. top + extends-right     → forward
        2. bottom + extends-right  → reversed (right edge → centre)
        3. bottom + extends-left   → forward (centre → left edge)
        4. top + extends-left      → reversed (left edge → centre)

    Strips that don't fit (e.g. vertical risers, or no centre-fed pattern)
    fall back to "append in config order, forward direction" so a custom
    rig still produces a defined `u_loop` instead of an exception.
    """
    # Bucket into the four canonical quadrant slots.
    buckets: dict[int, list[tuple]] = {0: [], 1: [], 2: [], 3: [], 4: []}
    # Slot 4 is the fallback bucket for non-centre-fed strips.
    for strip in strips:
        start = np.asarray(strip.geometry.start, dtype=np.float32)
        end = np.asarray(strip.geometry.end, dtype=np.float32)
        y_avg = 0.5 * (start[1] + end[1])
        dx = end[0] - start[0]
        # Centre-fed = start near x=0; if not, drop into the fallback bucket.
        is_centre_fed = abs(start[0]) < 1e-3 * (abs(end[0]) + 1e-6)
        if not is_centre_fed:
            buckets[4].append((strip, False))
            continue
        if y_avg >= 0.0 and dx > 0:
            buckets[0].append((strip, False))   # top, right-extending
        elif y_avg < 0.0 and dx > 0:
            buckets[1].append((strip, True))    # bottom, right-extending → reversed
        elif y_avg < 0.0 and dx < 0:
            buckets[2].append((strip, False))   # bottom, left-extending
        elif y_avg >= 0.0 and dx < 0:
            buckets[3].append((strip, True))    # top, left-extending → reversed
        else:
            buckets[4].append((strip, False))

    # Within each bucket, sort by extend distance so multi-strip layouts
    # (e.g. two stacked top-right strips) walk inner-then-outer.
    for slot in buckets:
        buckets[slot].sort(key=lambda sd: float(np.asarray(sd[0].geometry.end[0])))

    walk_order: list[tuple] = (
        buckets[0] + buckets[1] + buckets[2] + buckets[3] + buckets[4]
    )

    rank = np.zeros(pixel_count, dtype=np.int64)
    next_rank = 0
    for strip, reverse in walk_order:
        n = strip.pixel_count
        if n <= 0:
            continue
        lo = strip.pixel_offset
        # Map strip-local LEDs into the walk in the requested direction.
        local = np.arange(n, dtype=np.int64)
        if reverse:
            local = local[::-1]
        rank[lo + local] = next_rank + np.arange(n, dtype=np.int64)
        next_rank += n

    if pixel_count <= 1:
        return np.zeros(pixel_count, dtype=np.float32)
    u = (rank.astype(np.float32) / float(pixel_count - 1))
    return u
