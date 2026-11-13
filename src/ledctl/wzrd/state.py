"""Operator settings for the WZRD input, and the smoothed picture of the wall.

`WzrdSettings` persists to `config/wzrd_input.yaml` (runtime state, gitignored),
written on every change. `WzrdState` turns the listener's raw packets into the
values the output stage uses; it is advanced once per frame on the render
thread. WZRD smooths nothing, so all smoothing lives here.
"""

from __future__ import annotations

import colorsys
import logging
import math
from pathlib import Path

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

# No /wzrd/colors packet for this long = signal lost (state is held).
SIGNAL_LOST_AFTER_S = 1.0
# Below this smoothed weight / level a quotient is noise: hold or use a default.
_EPS = 1e-4


class WzrdSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listen: bool = Field(
        False, description="Off = packets still show in the status; the output is untouched.")
    colour: float = Field(
        1.0, ge=0.0, le=1.0, description="How far the LED colours move to the wall's colours.")
    spread: float = Field(
        1.0, ge=0.0, le=1.0,
        description="How much of the wall's palette is laid out along the strips.")
    follow_level: float = Field(
        0.5, ge=0.0, le=1.0, description="How far the LEDs dim when the wall is dark.")
    level_full: float = Field(
        0.25, ge=0.02, le=1.0, description="The wall level that counts as fully lit.")
    couple_brightness: bool = Field(True, description="Multiply by WZRD's master brightness.")
    couple_saturation: bool = Field(True, description="Multiply by WZRD's master saturation.")
    colour_smoothing_s: float = Field(
        0.7, ge=0.0, le=5.0, description="Time constant of the colour table.")
    level_attack_s: float = Field(
        0.0, ge=0.0, le=1.0, description="0 = flash on the same frame the level jumps.")
    level_release_s: float = Field(
        0.25, ge=0.0, le=3.0, description="How fast the LEDs fall back after a flash.")

    @classmethod
    def load(cls, path: Path) -> WzrdSettings:
        if path.is_file():
            try:
                return cls.model_validate(yaml.safe_load(path.read_text()) or {})
            except (OSError, yaml.YAMLError, ValidationError):
                log.exception("wzrd: could not load %s; using defaults", path)
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(yaml.safe_dump(self.model_dump(), sort_keys=False))
        tmp.replace(path)


def _srgb(v: float) -> float:
    return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


def centre_colours(k: int) -> np.ndarray:
    """(k, 3): the colour of each hue bin's centre, largest channel 1, sRGB-encoded
    (what WZRD sends for an empty bin)."""
    out = np.empty((k, 3), dtype=np.float32)
    for i in range(k):
        out[i] = [_srgb(c) for c in colorsys.hsv_to_rgb(i / k, 1.0, 1.0)]
    return out


def _alpha(dt: float, tau: float) -> float:
    """Fraction of the way to the target after `dt` with time constant `tau`."""
    if tau <= 0.0:
        return 1.0
    return 1.0 - math.exp(-dt / tau)


class WzrdState:
    """The smoothed wall: bin weights and colours, white share, level, masters."""

    def __init__(self) -> None:
        self.has_packet: bool = False     # False until the first colours packet
        self.connected: bool = False
        self.age_s: float | None = None
        self.k: int = 0
        self.weights = np.zeros(0, dtype=np.float32)        # smoothed weight per bin
        self.premult = np.zeros((0, 3), dtype=np.float32)   # smoothed rgb x weight
        self.white: float = 0.0           # smoothed, weighted by the light it describes
        self._white_level: float = 0.0    # smoothed white x level
        self._level_slow: float = 0.0     # smoothed level (same time constant)
        self.level_raw: float = 0.0       # latest packet's level
        self.level: float = 0.0           # attack/release follower of level_raw
        self.brightness: float = 1.0      # WZRD masters, as received
        self.saturation: float = 1.0
        self.dirty: bool = False          # the colour table needs a re-bake
        self._last_wall_t: float | None = None

    def advance(self, wall_t: float, now: float, colors: tuple | None,
                masters: tuple | None, s: WzrdSettings) -> None:
        last = self._last_wall_t
        self._last_wall_t = wall_t
        dt = 0.0 if last is None or wall_t < last else min(wall_t - last, 0.25)

        if masters is not None:
            _, self.brightness, self.saturation = masters
        if colors is None:
            return
        arrival, level, white, bins = colors
        self.age_s = now - arrival
        self.connected = self.age_s <= SIGNAL_LOST_AFTER_S
        self.level_raw = level
        w = bins[:, 3]
        coloured = bool(w.any())

        if not self.has_packet or bins.shape[0] != self.k:
            # First packet (or a new bin count): start at the wall, no fade-in.
            self.has_packet = True
            self.k = bins.shape[0]
            self.weights = w.copy()
            self.premult = bins[:, :3] * w[:, None]
            self._white_level = white * level
            self._level_slow = level
            self.white = white
            self.level = level
            self.dirty = True
            return

        a = _alpha(dt, s.colour_smoothing_s)
        if a > 0.0:
            # Smoothing runs toward the latest packet on every frame, so a lost
            # signal settles on the last picture and holds it.
            if coloured:
                # Premultiplied, so an empty bin's centre hue and a faint bin's
                # noise carry no weight. An all-zero packet (black, grey or
                # white wall) leaves the colours where they are.
                dw = w - self.weights
                dp = bins[:, :3] * w[:, None] - self.premult
                if max(float(np.abs(dw).max()), float(np.abs(dp).max())) > 1e-5:
                    self.weights += a * dw
                    self.premult += a * dp
                    self.dirty = True
            self._white_level += a * (white * level - self._white_level)
            self._level_slow += a * (level - self._level_slow)
            if self._level_slow >= _EPS:
                new_white = min(1.0, self._white_level / self._level_slow)
                if abs(new_white - self.white) > 1e-4:
                    self.white = new_white
                    self.dirty = True

        tau = s.level_attack_s if level > self.level else s.level_release_s
        self.level += _alpha(dt, tau) * (level - self.level)

    def bin_colours(self) -> np.ndarray:
        """(k, 3) colour per bin: the smoothed quotient, or the bin's centre hue
        while its smoothed weight is too small to mean anything."""
        out = centre_colours(self.k)
        ok = self.weights >= _EPS
        out[ok] = self.premult[ok] / self.weights[ok, None]
        return out
