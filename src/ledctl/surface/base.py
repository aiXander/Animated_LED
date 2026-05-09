"""Effect base class + per-frame / per-init contexts.

The LLM authors a single `Effect` subclass per file. The runtime picks it up
via `compile_effect()` (`sandbox.py`), instantiates it, and drives:

    e = MyEffect()
    e.init(EffectInitContext(...))
    while True:
        rgb = e.render(EffectFrameContext(...))   # (N, 3) float32 in [0, 1]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from ..config import StripConfig


# ---------- per-LED named coordinate frames ---------- #


class FrameMap:
    """Attribute-access view over a `topology.derived` dict.

    `ctx.frames.x`, `ctx.frames.u_loop`, etc. — same arrays the topology
    precomputed. Frames are immutable by convention; effects must not mutate
    them in place.
    """

    __slots__ = ("_d",)

    def __init__(self, derived: dict[str, np.ndarray]):
        self._d = derived

    def __getattr__(self, name: str) -> np.ndarray:
        try:
            return self._d[name]
        except KeyError as e:
            raise AttributeError(
                f"unknown frame {name!r}; available: {sorted(self._d.keys())}"
            ) from e

    def __dir__(self) -> list[str]:
        return list(self._d.keys())

    def keys(self) -> list[str]:
        return list(self._d.keys())


# ---------- audio view (already pre-scaled by masters.audio_reactivity) ---------- #


@dataclass
class AudioView:
    """Per-frame audio snapshot.

    Every value is already smoothed/auto-scaled upstream (audio-server) AND
    multiplied by `masters.audio_reactivity` — the LLM uses raw values.
    """

    low: float = 0.0
    mid: float = 0.0
    high: float = 0.0
    beat: int = 0                # new onsets since previous render (0 most frames)
    beats_since_start: int = 0   # monotonic counter
    bpm: float = 120.0
    connected: bool = False

    @property
    def bands(self) -> dict[str, float]:
        return {"low": self.low, "mid": self.mid, "high": self.high}


# ---------- read-only view of operator masters (diagnostic) ---------- #


@dataclass(frozen=True)
class MastersView:
    brightness: float = 1.0
    speed: float = 1.0
    audio_reactivity: float = 1.0
    saturation: float = 1.0
    freeze: bool = False
    crossfade_seconds: float = 1.0


# ---------- live, mutable param values ---------- #


class ParamView:
    """Attribute-access wrapper around a ParamStore for the current effect.

    Reads return the latest operator-set value. Writes are SOFT in v1: they
    become a `log.warning` and a no-op so a sloppy LLM-emitted assignment
    doesn't crash render in front of the dance floor. The strict-raise
    behaviour ships in v1.1.
    """

    __slots__ = ("_store", "_strict")

    def __init__(self, store: ParamStore, strict: bool = False):
        # bypass __setattr__
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_strict", bool(strict))

    def __getattr__(self, name: str):
        try:
            return self._store.get(name)
        except KeyError as e:
            raise AttributeError(
                f"unknown param {name!r}; declared params: {self._store.keys()}"
            ) from e

    def __setattr__(self, name: str, value) -> None:
        if self._strict:
            raise TypeError(
                f"params are read-only on the effect side; tried to set {name!r}"
            )
        # Soft mode: warn + ignore so a typo in render() doesn't blackout.
        from .helpers import log
        log.warning(
            "effect tried to write ctx.params.%s = %r — ignored (params are operator-owned)",
            name, value,
        )

    def keys(self) -> list[str]:
        return self._store.keys()


# ---------- contexts threaded through init / render ---------- #


@dataclass
class RigInfo:
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    target_fps: float
    span_x_m: float
    span_y_m: float


@dataclass
class EffectInitContext:
    """Passed to `Effect.init` once per swap. Frozen-feel; do not mutate."""

    n: int
    pos: np.ndarray              # (N, 3) float32 in [-1, 1]
    frames: FrameMap
    strips: list[StripConfig]
    rig: RigInfo


@dataclass
class EffectFrameContext:
    """Passed to `Effect.render` every tick."""

    t: float
    wall_t: float
    dt: float
    audio: AudioView
    params: ParamView
    masters: MastersView
    n: int


# ---------- Effect base ---------- #


class Effect:
    """Base class for all LLM-authored effects.

    Subclasses implement `init(ctx)` and `render(ctx)`. The runtime owns
    instance lifetime (one instance per swap; init runs once; render runs
    per frame).

    `self.out` is preallocated in the base init for ergonomics — most effects
    fill it in place and `return self.out`.
    """

    def __init__(self) -> None:
        # Buffers populated in `_setup`; assigned None so `init` can detect a
        # subclass that calls super().init too late.
        self.out: np.ndarray | None = None
        self._n: int = 0

    def _setup(self, n: int) -> None:
        """Internal: runtime calls this before user `init`."""
        self.out = np.zeros((n, 3), dtype=np.float32)
        self._n = int(n)

    # --- override hooks --- #

    def init(self, ctx: EffectInitContext) -> None:
        """Override to precompute per-LED arrays / state buffers."""

    def render(self, ctx: EffectFrameContext) -> np.ndarray:
        """Override. Must return (N, 3) float32 in [0, 1]. Default = black."""
        if self.out is None:
            raise RuntimeError("Effect.render called before init()")
        self.out.fill(0.0)
        return self.out


# ---------- ParamStore (lives next to ParamView; importable from runtime.py) ---------- #


class ParamStore:
    """Mutable, type-validated bag of param values + their schema.

    Hot-path readers go through ParamView. Slider drags PATCH /preview/params
    (or /live/params), which calls `update(...)` here; the next frame's
    `render` sees the new value via the view's __getattr__.
    """

    def __init__(self, schema: list[dict] | None = None):
        self._schema: list[dict] = list(schema or [])
        self._by_key: dict[str, dict] = {}
        self._values: dict[str, object] = {}
        for spec in self._schema:
            key = spec["key"]
            self._by_key[key] = spec
            self._values[key] = spec.get("default")

    @property
    def schema(self) -> list[dict]:
        return list(self._schema)

    def values(self) -> dict[str, object]:
        return dict(self._values)

    def keys(self) -> list[str]:
        return list(self._values.keys())

    def get(self, key: str):
        if key not in self._values:
            raise KeyError(key)
        return self._values[key]

    def set_initial_values(self, values: dict[str, object]) -> None:
        """Initialise values directly (used on load-from-disk)."""
        for k, v in values.items():
            if k in self._values:
                self._values[k] = self._coerce(k, v)

    def update(self, patch: dict[str, object]) -> dict[str, object]:
        """Apply a partial slider patch with bounds clamping. Returns new values."""
        for k, v in patch.items():
            if k not in self._by_key:
                continue  # ignore unknown keys silently — UI may lag schema
            self._values[k] = self._coerce(k, v)
        return dict(self._values)

    def _coerce(self, key: str, raw: object):
        spec = self._by_key[key]
        ctrl = spec.get("control")
        if ctrl == "slider":
            v = float(raw)
            if "min" in spec:
                v = max(float(spec["min"]), v)
            if "max" in spec:
                v = min(float(spec["max"]), v)
            return v
        if ctrl == "int_slider":
            v = int(raw)
            if "min" in spec:
                v = max(int(spec["min"]), v)
            if "max" in spec:
                v = min(int(spec["max"]), v)
            return v
        if ctrl == "color":
            s = str(raw).strip()
            if not s.startswith("#"):
                s = "#" + s
            if len(s) not in (4, 7):
                raise ValueError(f"bad colour for {key}: {raw!r}")
            return s.lower()
        if ctrl == "select":
            opts = spec.get("options") or []
            v = str(raw)
            if opts and v not in opts:
                # fall back to default rather than crash
                v = str(spec.get("default", opts[0] if opts else v))
            return v
        if ctrl == "toggle":
            return bool(raw)
        if ctrl == "palette":
            return str(raw)
        # Unknown control type: pass through.
        return raw
