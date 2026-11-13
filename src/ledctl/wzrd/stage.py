"""The WZRD output hook: recolours the effect's output with the wall's colours
and returns the saturation and brightness gains that follow the wall.

Mounted as `Runtime.output_hook`. `tick` runs once per frame; `recolour`,
`saturation` and `gain` run once per rendered leg inside the master output
stage (see `Runtime._apply_master_output` for why the gain comes after the
brightness envelope).
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
    """(256, 3) hue -> wall colour lookup.

    The bins lie round a circle in hue order, each on an arc as wide as its
    weight, the first arc starting at -1/(2k) so a wall with equal weights maps
    every hue to itself. Arcs are filled with the bin colour, blurred circularly
    and rescaled so each entry's largest channel is 1; then `white` (the wall's
    colourless share) pulls every entry toward white, so a pastel wall gives
    pastel LEDs.
    """
    k = len(weights)
    total = float(weights.sum())
    w = weights / total if total >= 1e-4 else np.full(k, 1.0 / k, dtype=np.float32)
    edges = np.cumsum(w)
    edges[-1] = 1.0
    pos = (np.arange(TABLE_SIZE, dtype=np.float32) / TABLE_SIZE + 0.5 / k) % 1.0
    which = np.minimum(np.searchsorted(edges, pos, side="right"), k - 1)
    table = colours[which].astype(np.float32)
    kernel = np.hanning(2 * _BLUR_RADIUS + 3)[1:-1].astype(np.float32)
    kernel /= kernel.sum()
    blurred = np.zeros_like(table)
    for offset, kv in zip(range(-_BLUR_RADIUS, _BLUR_RADIUS + 1), kernel, strict=True):
        blurred += kv * np.roll(table, offset, axis=0)
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
        self.table = np.ones((TABLE_SIZE, 3), dtype=np.float32)
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

    # ---- the output hook ----

    def resize(self, topology: Any) -> None:
        n = topology.pixel_count
        self._u_table = (topology.derived["u_loop"] * TABLE_SIZE).astype(np.float32)
        self._v = np.zeros(n, dtype=np.float32)
        self._mn = np.zeros(n, dtype=np.float32)
        self._d = np.zeros(n, dtype=np.float32)
        self._dp = np.zeros(n, dtype=np.float32)
        self._h = np.zeros(n, dtype=np.float32)
        self._t = np.zeros(n, dtype=np.float32)
        self._mask = np.zeros(n, dtype=bool)
        self._idx = np.zeros(n, dtype=np.int32)
        self._c = np.zeros((n, 3), dtype=np.float32)

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
            self.table = bake_table(st.weights, st.bin_colours(), st.white)
            self._last_bake_wall_t = wall_t
            st.dirty = False
        lit = min(1.0, max(0.0, st.level / s.level_full))
        gain = 1.0 - s.follow_level * (1.0 - lit)
        if s.couple_brightness:
            gain *= st.brightness
        self._gain = gain

    def recolour(self, rgb: np.ndarray) -> None:
        """Move each LED's colour to the wall colour its hue (shifted along the
        strips by `spread`) looks up. In place. Keeps each pixel's largest and
        smallest channel, so black stays black and the effect's white stays white."""
        s = self.settings
        if not self._active() or s.colour <= 0.0:
            return
        v, mn, d, dp, h, t = self._v, self._mn, self._d, self._dp, self._h, self._t
        mask, idx, c = self._mask, self._idx, self._c
        r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
        np.max(rgb, axis=1, out=v)
        np.min(rgb, axis=1, out=mn)
        np.subtract(v, mn, out=d)
        np.maximum(d, 1e-6, out=dp)
        # Hue in sixths (0..6): blue-max, then green-max, then red-max on top.
        np.subtract(r, g, out=h)
        h /= dp
        h += 4.0
        np.subtract(b, r, out=t)
        t /= dp
        t += 2.0
        np.equal(v, g, out=mask)
        np.copyto(h, t, where=mask)
        np.subtract(g, b, out=t)
        t /= dp
        np.equal(v, r, out=mask)
        np.copyto(h, t, where=mask)
        h *= TABLE_SIZE / 6.0                       # now in table entries
        if s.spread > 0.0:
            np.multiply(self._u_table, s.spread, out=t)
            h += t
        np.floor(h, out=h)
        np.copyto(idx, h, casting="unsafe")
        idx &= TABLE_SIZE - 1                       # wraps negatives too
        np.take(self.table, idx, axis=0, out=c)
        c *= d[:, None]
        c += mn[:, None]                            # target = mn + d * wall colour
        c -= rgb
        c *= s.colour
        rgb += c

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
