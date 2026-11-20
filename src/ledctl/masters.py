"""Operator-owned master controls + the per-frame render context.

`MasterControls` is the small set of room-level knobs (brightness, speed,
audio_reactivity, saturation) the operator owns. They are deliberately
kept out of the surface DSL: the LLM never produces or alters them. The render
loop reads them through `RenderContext`; the REST surface in `api/server.py`
exposes `GET /masters` and `PATCH /masters` for the UI.

Bounds are the `BOUNDS` table below; `MastersConfig` and the REST request
model are built from it. The four master effects (`sparkle`, `glitch`,
`strobe`, `trails`; 0 = off) run in `surface/master_fx.py`.

`brightness > 1.0` activates an adaptive headroom mode in `Mixer._apply_master_output`:
the gain is derived from a rolling peak of the post-saturation stack so the recent
brightest pixel is pushed toward 1.0. `brightness ≤ 1.0` is exact linear gain — old
presets and existing UI behaviour stay identical. See `mixer.py` for the curve.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .audio.state import AudioState


# Field -> (lowest, highest). The one place a master's range is written.
BOUNDS: dict[str, tuple[float, float]] = {
    "brightness": (0.0, 2.0),
    "speed": (0.0, 3.0),
    "audio_reactivity": (0.0, 3.0),
    "saturation": (0.0, 1.0),
    "audio_swell": (0.0, 1.0),   # 0 = off; how far the LEDs dim when the room is quiet
    "sparkle": (0.0, 1.0),
    "glitch": (0.0, 1.0),
    "strobe": (0.0, 1.0),
    "trails": (0.0, 1.0),
}


@dataclass
class MasterControls:
    brightness: float = 1.0
    speed: float = 1.0
    audio_reactivity: float = 1.0
    saturation: float = 1.0
    audio_swell: float = 0.0
    sparkle: float = 0.0
    glitch: float = 0.0
    strobe: float = 0.0
    trails: float = 0.0

    def clamped(self) -> MasterControls:
        return MasterControls(**{
            k: min(hi, max(lo, float(getattr(self, k)))) for k, (lo, hi) in BOUNDS.items()
        })

    def merge(self, **patch: object) -> MasterControls:
        """Return a new MasterControls with `patch` applied and bounds enforced.

        Unknown keys raise — the REST layer already filters via pydantic, but
        this guards programmatic callers too.
        """
        unknown = set(patch) - set(BOUNDS)
        if unknown:
            raise ValueError(f"unknown master fields: {sorted(unknown)}")
        return replace(self, **patch).clamped()


@dataclass
class RenderContext:
    """Per-frame snapshot threaded through every primitive.

    `t` is *effective* time (master-speed-scaled). `wall_t` is raw monotonic
    time — the mixer's crossfade alpha uses wall_t so it keeps progressing
    under speed.
    `audio` is a possibly-pre-scaled view of the current AudioState (the
    low/mid/high fields are multiplied by `masters.audio_reactivity` once per
    tick, so individual primitives stay pure).
    """

    t: float = 0.0
    wall_t: float = 0.0
    audio: AudioState | None = None
    masters: MasterControls = field(default_factory=MasterControls)
