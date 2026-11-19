"""Named coordinate frames derived from a `Topology`.

A *frame* is a per-LED scalar (or per-LED vector), precomputed once at
topology-build time, that effects can address by name. The Cartesian
`x / y / z` are the originals — every other frame is a higher-level view
that lets the LLM say "from the hub outward" or "this leg" without
composing it from scratch.

The rig is a hub at the origin with K legs (one strip each) running outward.
`radius` (how far out) and `leg` / `leg_phase` / `angle` (which leg) are the
frames that describe it. The rest predate the hub rig and are kept computed
so older effects still compile; on a star most of them are arbitrary.
"""

from __future__ import annotations

import math

import numpy as np

# One-line description per frame. Every frame needs one (the sandbox lints
# `ctx.<name>` against these keys), but only PROMPT_FRAMES reach the LLM.
FRAME_DESCRIPTIONS: dict[str, str] = {
    "radius":        "0 at the hub → 1 at the farthest LED. THE main axis: rings, waves, comets",
    "chain_index":   "0 at the hub end of the LED's leg → 1 at its tip (= radius on equal legs)",
    "leg":           "int32 0..K-1: which leg the LED is on, ranked by ascending angle",
    "leg_phase":     "leg / K in [0, 1): spreads a hue or a phase offset evenly over the legs",
    "angle":         "the leg's direction as a turn in [0, 1): 0 = +x, CCW; one value per leg",
    "signed_x":      "Cartesian x in [-1, 1], hub at 0: for a 2-D field sampled along the legs",
    "signed_y":      "Cartesian y in [-1, 1], hub at 0",
    # Older frames, from the rectangle this rig replaced. Still computed so an
    # effect written for it compiles; on a star most are an arbitrary order.
    "x":             "Cartesian x in [0, 1]",
    "y":             "Cartesian y in [0, 1]",
    "z":             "Cartesian z in [0, 1]",
    "signed_z":      "Cartesian z in [-1, 1]",
    "u_loop":        "Clockwise arc position around the rectangle, [0, 1]",
    "u_loop_signed": "u_loop centred: [-0.5, +0.5]",
    "side_top":      "1.0 where y > 0",
    "side_bottom":   "1.0 where y < 0",
    "side_signed":   "sign of y",
    "axial_dist":    "|x| in [0, 1]",
    "axial_signed":  "x in [-1, 1]",
    "corner_dist":   "Distance to the nearest corner of the unit square, normalised",
    "strip_id":      "Integer per strip, in config order",
    "distance":      "√(x²+y²+z²) normalised to [0, 1] (see `radius`)",
}

# The frames the LLM prompt lists, in this order, and the runtime's error
# hints name. A new effect reads only these.
PROMPT_FRAMES: tuple[str, ...] = (
    "radius", "chain_index", "leg", "leg_phase", "angle", "signed_x", "signed_y",
)


def strip_direction(strip) -> float:
    """A strip's direction from the hub, as a turn fraction in [0, 1).

    Read from the strip's endpoint farther from the origin, never from LED
    positions: a leg's first LED sits on the origin, where atan2(0, 0) is 0.
    """
    x, y, _ = max(strip.start, strip.end, key=lambda p: p[0] ** 2 + p[1] ** 2)
    a = float(np.float32(math.atan2(y, x) / math.tau % 1.0))
    return a if a < 1.0 else 0.0  # float32 can round 0.99999999 up to 1.0


def rank_legs(strips: list) -> list:
    """The strips in leg order: ascending direction, ties in config order."""
    return sorted(strips, key=strip_direction)


def build_frames(
    *,
    normalised_positions: np.ndarray,
    leds: list,
    strips: list,
    pixel_count: int,
) -> dict[str, np.ndarray]:
    """Compute every named frame for a given topology.

    Returns a dict mapping frame name → ndarray. Most frames are float32 of
    shape (N,); `strip_id` and `leg` are int32.
    """
    pos = normalised_positions  # (N, 3): metres / distance of the farthest LED
    out: dict[str, np.ndarray] = {}

    out["x"] = ((pos[:, 0] + 1.0) * 0.5).astype(np.float32)
    out["y"] = ((pos[:, 1] + 1.0) * 0.5).astype(np.float32)
    out["z"] = ((pos[:, 2] + 1.0) * 0.5).astype(np.float32)

    out["signed_x"] = pos[:, 0].astype(np.float32, copy=True)
    out["signed_y"] = pos[:, 1].astype(np.float32, copy=True)
    out["signed_z"] = pos[:, 2].astype(np.float32, copy=True)

    r = np.sqrt(pos[:, 0] ** 2 + pos[:, 1] ** 2)
    r_max = max(float(r.max()), 1e-9)
    out["radius"] = np.clip(r / r_max, 0.0, 1.0).astype(np.float32)

    # One value per leg, written to every LED of its strip. Pixels in a gap of
    # the pixel range (no strip) keep leg 0 and angle 0.
    legs = rank_legs(strips)
    leg = np.zeros(pixel_count, dtype=np.int32)
    angle = np.zeros(pixel_count, dtype=np.float32)
    for rank, strip in enumerate(legs):
        lo = strip.pixel_offset
        hi = lo + strip.pixel_count
        leg[lo:hi] = rank
        angle[lo:hi] = strip_direction(strip)
    out["angle"] = angle
    out["leg"] = leg
    out["leg_phase"] = (leg / max(len(legs), 1)).astype(np.float32)

    out["side_top"] = (pos[:, 1] > 0.0).astype(np.float32)
    out["side_bottom"] = (pos[:, 1] < 0.0).astype(np.float32)
    out["side_signed"] = np.sign(pos[:, 1]).astype(np.float32)

    out["axial_dist"] = np.clip(np.abs(pos[:, 0]), 0.0, 1.0).astype(np.float32)
    out["axial_signed"] = np.clip(pos[:, 0], -1.0, 1.0).astype(np.float32)

    corners = np.array(
        [(-1.0, -1.0), (-1.0, 1.0), (1.0, -1.0), (1.0, 1.0)], dtype=np.float32
    )
    diffs = pos[:, None, :2] - corners[None, :, :]
    cdist = np.sqrt((diffs ** 2).sum(axis=2)).min(axis=1)
    cdist = cdist / max(float(cdist.max()), 1e-9)
    out["corner_dist"] = cdist.astype(np.float32)

    d3 = np.sqrt(np.sum(pos ** 2, axis=1))
    out["distance"] = (d3 / max(float(d3.max()), 1e-9)).astype(np.float32)

    strip_id = np.zeros(pixel_count, dtype=np.int32)
    for rank, strip in enumerate(strips):
        lo = strip.pixel_offset
        hi = lo + strip.pixel_count
        strip_id[lo:hi] = rank
    out["strip_id"] = strip_id

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

    Walk strips by quadrant: top-right (forward) → bottom-right (reversed)
    → bottom-left (forward) → top-left (reversed). Strips that don't fit
    drop into a fallback bucket appended at the end.
    """
    buckets: dict[int, list[tuple]] = {0: [], 1: [], 2: [], 3: [], 4: []}
    for strip in strips:
        start = np.asarray(strip.start, dtype=np.float32)
        end = np.asarray(strip.end, dtype=np.float32)
        y_avg = 0.5 * (start[1] + end[1])
        dx = end[0] - start[0]
        is_centre_fed = abs(start[0]) < 1e-3 * (abs(end[0]) + 1e-6)
        if not is_centre_fed:
            buckets[4].append((strip, False))
            continue
        if y_avg >= 0.0 and dx > 0:
            buckets[0].append((strip, False))
        elif y_avg < 0.0 and dx > 0:
            buckets[1].append((strip, True))
        elif y_avg < 0.0 and dx < 0:
            buckets[2].append((strip, False))
        elif y_avg >= 0.0 and dx < 0:
            buckets[3].append((strip, True))
        else:
            buckets[4].append((strip, False))

    for slot in buckets:
        buckets[slot].sort(key=lambda sd: float(sd[0].end[0]))

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
        local = np.arange(n, dtype=np.int64)
        if reverse:
            local = local[::-1]
        rank[lo + local] = next_rank + np.arange(n, dtype=np.int64)
        next_rank += n

    if pixel_count <= 1:
        return np.zeros(pixel_count, dtype=np.float32)
    u = (rank.astype(np.float32) / float(pixel_count - 1))
    return u
