"""Operator-owned master controls + the per-frame render context.

`MasterControls` is the small set of room-level knobs (brightness, speed,
audio_reactivity, saturation, freeze) the operator owns. They are deliberately
kept out of the surface DSL: the LLM never produces or alters them. The render
loop reads them through `RenderContext`; the REST surface in `api/server.py`
exposes `GET /masters` and `PATCH /masters` for the UI.

Bounds (enforced in `clamped()` and at the REST layer):
  - brightness ∈ [0, 1]
  - speed ∈ [0, 3]
  - audio_reactivity ∈ [0, 3]
  - saturation ∈ [0, 1]
  - freeze: bool

A frozen pattern still breathes with the room, by design: `freeze` zeroes
`effective_t` accumulation but `audio_band` reads `AudioState` directly,
and the audio server keeps publishing smoothed band energies regardless.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .audio.state import AudioState


@dataclass
class MasterControls:
    brightness: float = 1.0
    speed: float = 1.0
    audio_reactivity: float = 1.0
    saturation: float = 1.0
    freeze: bool = False

    def clamped(self) -> MasterControls:
        return MasterControls(
            brightness=_clip(self.brightness, 0.0, 1.0),
            speed=_clip(self.speed, 0.0, 3.0),
            audio_reactivity=_clip(self.audio_reactivity, 0.0, 3.0),
            saturation=_clip(self.saturation, 0.0, 1.0),
            freeze=bool(self.freeze),
        )

    def merge(self, **patch: object) -> MasterControls:
        """Return a new MasterControls with `patch` applied and bounds enforced.

        Unknown keys raise — the REST layer already filters via pydantic, but
        this guards programmatic callers too.
        """
        valid = {
            "brightness",
            "speed",
            "audio_reactivity",
            "saturation",
            "freeze",
        }
        unknown = set(patch) - valid
        if unknown:
            raise ValueError(f"unknown master fields: {sorted(unknown)}")
        out = replace(self, **patch)
        return out.clamped()


def _clip(v: float, lo: float, hi: float) -> float:
    f = float(v)
    if f < lo:
        return lo
    if f > hi:
        return hi
    return f


@dataclass
class RenderContext:
    """Per-frame snapshot threaded through every primitive.

    `t` is *effective* time (master-speed-scaled, frozen if `freeze`). `wall_t`
    is raw monotonic time — the mixer's crossfade alpha uses wall_t so it
    keeps progressing under speed/freeze.
    `audio` is a possibly-pre-scaled view of the current AudioState (the
    low/mid/high fields are multiplied by `masters.audio_reactivity` once per
    tick, so individual primitives stay pure).
    """

    t: float = 0.0
    wall_t: float = 0.0
    audio: AudioState | None = None
    masters: MasterControls = field(default_factory=MasterControls)
