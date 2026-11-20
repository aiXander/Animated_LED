"""Master effects: sparkle, glitch, strobe and trails on top of whatever plays.

Four operator masters (0 = off, 1 = full) that run at the end of the master
output stage, after the brightness step, so they sit on the picture the room
actually sees. One `MasterFx` per rendered output (LIVE, PREVIEW) because
trails, sparks and a running glitch are state.

With every amount at 0 and no flash, `apply` returns before touching the
buffer: the output is exactly what it was without this module.

Order: trails -> glitch -> sparkle -> flash -> strobe. Trails smear the effect
only (a smeared glitch reads as mush); the strobe gates everything.
"""

from __future__ import annotations

import math

import numpy as np

# Sparkle: how long one spark takes to fade to 1/e, and sparks per LED per
# second at full amount (their product is the mean spark level, about 0.25).
_SPARK_TAU_S = 0.12
_SPARK_RATE = 2.0
# A beat adds this many seconds' worth of sparks at once, scaled by its strength.
_SPARK_BEAT_BURST_S = 0.15
# Glitch: events per second at full amount, and how long one lasts.
_GLITCH_RATE = 9.0
_GLITCH_MIN_S, _GLITCH_MAX_S = 0.04, 0.20
# Strobe: flashes per second at the lowest and the full amount, and the lit
# share of each cycle. The top stays under the rates that are hard to look at.
_STROBE_MIN_HZ, _STROBE_MAX_HZ = 2.0, 11.0
_STROBE_DUTY = 0.4
# Trails: fade time constant at the lowest and the full amount.
_TRAIL_MIN_S, _TRAIL_MAX_S = 0.05, 2.5


class MasterFx:
    def __init__(self, n: int, seed: int = 0):
        self.n = int(n)
        self._rng = np.random.default_rng(seed)
        self._spark = np.zeros(self.n, dtype=np.float32)
        self._spark_live = False               # any spark still visible
        self._trail = np.zeros((self.n, 3), dtype=np.float32)
        self._trail_live = False
        self._strobe_phase = 0.0
        # A running glitch: seconds left, and what it does to which LEDs.
        self._glitch_left = 0.0
        self._hold = np.zeros((self.n, 3), dtype=np.float32)
        self._hold_idx = np.zeros(0, dtype=np.intp)
        self._drop_idx = np.zeros(0, dtype=np.intp)
        self._shift_idx = np.zeros(0, dtype=np.intp)
        self._shift_src = np.zeros(0, dtype=np.intp)
        self._swap_idx = np.zeros(0, dtype=np.intp)

    def apply(self, rgb: np.ndarray, masters, dt: float, beat: float,
              level: float = 1.0, flash: bool = False) -> None:
        """Run the effects on `rgb` (N, 3) in place. `level` is how bright
        added light may be (the brightness master, capped at 1), so sparks and
        the flash dim with the room."""
        sparkle = float(masters.sparkle)
        glitch = float(masters.glitch)
        strobe = float(masters.strobe)
        trails = float(masters.trails)
        if not (sparkle or glitch or strobe or trails or flash
                or self._spark_live or self._trail_live or self._glitch_left > 0.0):
            return
        dt = max(0.0, float(dt))

        if trails > 0.0:
            tau = _TRAIL_MIN_S + trails * trails * (_TRAIL_MAX_S - _TRAIL_MIN_S)
            self._trail *= math.exp(-dt / tau)
            np.maximum(rgb, self._trail, out=rgb)
            np.copyto(self._trail, rgb)
            self._trail_live = True
        elif self._trail_live:
            self._trail.fill(0.0)
            self._trail_live = False

        self._glitch(rgb, glitch, dt, beat)

        if sparkle > 0.0 or self._spark_live:
            self._spark *= math.exp(-dt / _SPARK_TAU_S)
            if sparkle > 0.0:
                per_s = sparkle * sparkle * _SPARK_RATE * self.n
                count = self._rng.poisson(per_s * (dt + _SPARK_BEAT_BURST_S * float(beat)))
                if count:
                    self._spark[self._rng.integers(0, self.n, size=min(count, self.n))] = 1.0
                self._spark_live = True
            elif float(self._spark.max(initial=0.0)) < 0.004:
                self._spark.fill(0.0)
                self._spark_live = False
            if self._spark_live:
                rgb += (self._spark * level)[:, None]

        if flash:
            np.maximum(rgb, level, out=rgb)

        if strobe > 0.0:
            hz = _STROBE_MIN_HZ + strobe * (_STROBE_MAX_HZ - _STROBE_MIN_HZ)
            self._strobe_phase = (self._strobe_phase + dt * hz) % 1.0
            if self._strobe_phase >= _STROBE_DUTY:
                # Dark part of the cycle; the depth reaches full at a quarter.
                rgb *= 1.0 - min(1.0, strobe * 4.0)

    # ---- glitch ----

    def _glitch(self, rgb: np.ndarray, amount: float, dt: float, beat: float) -> None:
        if self._glitch_left <= 0.0:
            if amount <= 0.0:
                return
            # Random starts, plus most beats once the amount is up.
            chance = amount * amount * _GLITCH_RATE * dt + amount * float(beat)
            if self._rng.random() >= chance:
                return
            self._start_glitch(rgb, amount)
        self._glitch_left -= dt
        if self._hold_idx.size:
            rgb[self._hold_idx] = self._hold[self._hold_idx]
        if self._shift_idx.size:
            rgb[self._shift_idx] = rgb[self._shift_src]
        if self._swap_idx.size:
            rgb[self._swap_idx] = rgb[self._swap_idx][:, (1, 2, 0)]
        if self._drop_idx.size:
            rgb[self._drop_idx] = 0.0

    def _start_glitch(self, rgb: np.ndarray, amount: float) -> None:
        """Pick a few runs of LEDs and one fault for each: freeze on this
        frame, show a neighbouring stretch, rotate the colour channels, or go
        dark. Loops over runs (at most 8), never over LEDs."""
        rng = self._rng
        n = self.n
        self._glitch_left = float(rng.uniform(_GLITCH_MIN_S, _GLITCH_MAX_S))
        np.copyto(self._hold, rgb)
        picked: dict[str, list[np.ndarray]] = {"hold": [], "shift": [], "swap": [], "drop": []}
        shift_src: list[np.ndarray] = []
        longest = max(8, int(n * (0.02 + 0.10 * amount)))
        for _ in range(1 + int(rng.integers(0, 2 + int(amount * 6)))):
            length = int(rng.integers(4, longest + 1))
            start = int(rng.integers(0, n))
            idx = np.arange(start, start + length) % n
            kind = ("hold", "shift", "swap", "drop")[int(rng.integers(0, 4))]
            picked[kind].append(idx)
            if kind == "shift":
                shift_src.append((idx + int(rng.integers(5, 60))) % n)
        def cat(parts: list[np.ndarray]) -> np.ndarray:
            return np.concatenate(parts) if parts else np.zeros(0, dtype=np.intp)
        self._hold_idx = cat(picked["hold"])
        self._shift_idx = cat(picked["shift"])
        self._shift_src = cat(shift_src)
        self._swap_idx = cat(picked["swap"])
        self._drop_idx = cat(picked["drop"])
