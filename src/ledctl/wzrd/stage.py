"""The WZRD output hook: recolours the effect's output with the wall's colours
and returns the saturation and brightness gains that follow the wall.

Each frame the stage holds the *wall light*: one colour per LED, "what the
wall shines on this LED". Which part of the wall an LED sees is the `mapping`
setting; it depends on where the LED is (how far out along its leg), never on
the effect's own hue. Two amounts combine it with the effect: `colour` draws
the effect's pattern in the wall's colours, `wash` crossfades to the wall
light itself.

Mounted as `Runtime.output_hook`. `tick` runs once per frame; `recolour`,
`saturation` and `gain` run once per rendered output (LED leg, sim leg, a
crossfade's two sides) inside the master output stage (see
`Runtime._apply_master_output` for why the gain comes after the brightness
envelope).
"""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Any

import numpy as np

from .listener import WzrdListener
from .state import WzrdSettings, WzrdState

TABLE_SIZE = 256
_BLUR_RADIUS = 6              # table entries each side
_BAKE_INTERVAL_S = 0.05       # re-bake at most 20 times a second


def bake_table(weights: np.ndarray, colours: np.ndarray, white: float) -> np.ndarray:
    """(256, 3) wall palette, laid out along a line: entry 0 is the hub end.

    The bins lie along the line in hue order, each on a stretch as long as its
    weight. Stretches are filled with the bin colour, blurred (the two ends
    repeat their own colour, they do not wrap into each other) and rescaled so
    each entry's largest channel is 1; then `white` (the wall's colourless
    share) pulls every entry toward white, so a pastel wall gives pastel LEDs.
    """
    k = len(weights)
    total = float(weights.sum())
    w = weights / total if total >= 1e-4 else np.full(k, 1.0 / k, dtype=np.float32)
    edges = np.cumsum(w)
    edges[-1] = 1.0
    pos = (np.arange(TABLE_SIZE, dtype=np.float32) + 0.5) / TABLE_SIZE
    which = np.minimum(np.searchsorted(edges, pos, side="right"), k - 1)
    table = colours[which].astype(np.float32)
    kernel = np.hanning(2 * _BLUR_RADIUS + 3)[1:-1].astype(np.float32)
    kernel /= kernel.sum()
    padded = np.pad(table, ((_BLUR_RADIUS, _BLUR_RADIUS), (0, 0)), mode="edge")
    blurred = np.zeros_like(table)
    for i, kv in enumerate(kernel):
        blurred += kv * padded[i:i + TABLE_SIZE]
    blurred /= np.maximum(blurred.max(axis=1, keepdims=True), 1e-6)
    blurred += white * (1.0 - blurred)
    return blurred


class WzrdStage:
    def __init__(self, topology: Any, listener: WzrdListener, settings: WzrdSettings,
                 settings_path: Path | None = None):
        self.listener = listener
        self.settings = settings
        self.settings_path = settings_path
        self.state = WzrdState()
        self._set_table(np.ones((TABLE_SIZE, 3), dtype=np.float32))
        self._last_bake_wall_t: float = -1.0
        self._gain: float = 1.0
        self.resize(topology)

    # ---- settings ----

    def update_settings(self, patch: dict[str, Any]) -> WzrdSettings:
        """Validate, apply and persist a partial settings change. Raises
        pydantic's ValidationError on an unknown key or an out-of-range value."""
        new = WzrdSettings.model_validate({**self.settings.model_dump(), **patch})
        self.settings = new
        if self.settings_path is not None:
            new.save(self.settings_path)
        return new

    def _set_table(self, table: np.ndarray) -> None:
        self.table = table
        # Channel-major copy (3, 256) for the per-LED read, which works on
        # (3, N) blocks so each numpy step is one long pass.
        self._table_t = np.ascontiguousarray(table.T)
        self._wall_stale = True

    # ---- the output hook ----

    def resize(self, topology: Any) -> None:
        n = topology.pixel_count
        # `palette` mapping: the table read at how far out the LED sits, so
        # every leg shows the same hub-to-tip run of colours.
        radius = topology.derived["radius"]
        self._idx = np.minimum((radius * TABLE_SIZE).astype(np.int32), TABLE_SIZE - 1)
        self._v = np.zeros(n, dtype=np.float32)
        self._mn = np.zeros(n, dtype=np.float32)
        self._c = np.zeros((3, n), dtype=np.float32)
        self._rgb_t = np.zeros((3, n), dtype=np.float32)
        # The wall light, (3, N). In the `palette` mapping every entry's
        # largest channel is 1, so it is also its own tint (the wall's colour
        # at full strength); a picture mapping will need the two apart.
        self._wall = np.ones((3, n), dtype=np.float32)
        self._wall_stale = True

    def _active(self) -> bool:
        return self.settings.listen and self.state.has_packet

    def tick(self, wall_t: float) -> None:
        s = self.settings
        st = self.state
        st.advance(wall_t, monotonic(), self.listener.latest_colors,
                   self.listener.latest_masters, s)
        if not st.has_packet:
            return
        if st.dirty and (wall_t - self._last_bake_wall_t >= _BAKE_INTERVAL_S
                         or wall_t < self._last_bake_wall_t):
            self._set_table(bake_table(st.weights, st.bin_colours(), st.white))
            self._last_bake_wall_t = wall_t
            st.dirty = False
        if self._wall_stale:
            # Built here, once per frame at most, not once per rendered output.
            np.take(self._table_t, self._idx, axis=1, out=self._wall, mode="clip")
            self._wall_stale = False
        lit = min(1.0, max(0.0, (st.level - s.wall_dark) / (s.wall_bright - s.wall_dark)))
        gain = s.led_floor + (1.0 - s.led_floor) * lit
        if s.couple_brightness:
            gain *= st.brightness
        self._gain = gain

    def recolour(self, rgb: np.ndarray) -> None:
        """Combine the effect with the wall light, in place:

            E'  = E  + colour * (max(E) * wall - E)     # effect keeps its brightness
            out = E' + wash   * (wall - E')             # crossfade to the wall itself

        With `tint_white` off the first line keeps each pixel's smallest
        channel (`min(E) + (max(E) - min(E)) * wall`), so the effect's white
        and grey stay colourless."""
        s = self.settings
        if not self._active() or (s.colour <= 0.0 and s.wash <= 0.0):
            return
        v, mn, c, wall = self._v, self._mn, self._c, self._wall
        # Work on a channel-major (3, N) copy: contiguous rows instead of
        # stride-3 columns, so every step below is one long vector pass.
        rgb_t = self._rgb_t
        np.copyto(rgb_t, rgb.T)
        if s.colour > 0.0:
            r, g, b = rgb_t[0], rgb_t[1], rgb_t[2]
            # Pairwise, not `np.max(rgb, axis=1)`: a reduction over an axis of 3
            # runs N tiny loops and was the slowest step here.
            np.maximum(r, g, out=v)
            np.maximum(v, b, out=v)
            if s.tint_white:
                np.multiply(wall, v, out=c)
            else:
                np.minimum(r, g, out=mn)
                np.minimum(mn, b, out=mn)
                v -= mn
                np.multiply(wall, v, out=c)
                c += mn
            if s.colour < 1.0:
                c -= rgb_t
                c *= s.colour
                c += rgb_t
        else:
            np.copyto(c, rgb_t)
        if s.wash > 0.0:
            c *= 1.0 - s.wash
            np.multiply(wall, s.wash, out=rgb_t)
            c += rgb_t
        np.copyto(rgb.T, c)

    def saturation(self) -> float:
        if self._active() and self.settings.couple_saturation:
            return self.state.saturation
        return 1.0

    def gain(self) -> float:
        return self._gain if self._active() else 1.0

    # ---- API payload ----

    def payload(self) -> dict[str, Any]:
        st = self.state
        colors = self.listener.latest_colors
        age = monotonic() - colors[0] if colors is not None else None
        return {
            "settings": self.settings.model_dump(),
            "status": {
                # The mapping in use. A picture mapping needs WZRD's picture,
                # which no WZRD sends yet, so every mapping shows `palette`.
                "mapping_active": "palette",
                "connected": age is not None and age <= 1.0,
                "age_s": round(age, 2) if age is not None else None,
                "level": round(st.level_raw, 4),
                "white": round(st.white, 4),
                "brightness": round(st.brightness, 4),
                "saturation": round(st.saturation, 4),
                "gain": round(self.gain(), 4),
                **self.listener.link.snapshot(),
                "dropped": self.listener.dropped,
                "error": self.listener.error,
            },
        }
