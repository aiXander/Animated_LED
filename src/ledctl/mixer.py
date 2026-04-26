from dataclasses import dataclass
from typing import Literal

import numpy as np

from .effects.base import Effect

BlendMode = Literal["normal", "add", "screen", "multiply"]
BLEND_MODES: tuple[str, ...] = ("normal", "add", "screen", "multiply")


@dataclass
class Layer:
    """One entry in the Mixer stack.

    Layers render bottom-up: layer 0 renders first onto a black accumulator,
    each subsequent layer's output is blended into the accumulator using its
    `blend` mode and `opacity`.
    """

    effect: Effect
    blend: BlendMode = "normal"
    opacity: float = 1.0


def _blend_into(dst: np.ndarray, src: np.ndarray, mode: str, opacity: float) -> None:
    """Blend `src` into `dst` in-place. Both arrays are float32 (N, 3) in [0, 1]."""
    a = float(np.clip(opacity, 0.0, 1.0))
    if a == 0.0:
        return
    if mode == "normal":
        # dst = (1 - a) * dst + a * src
        dst *= 1.0 - a
        dst += src * a
    elif mode == "add":
        dst += src * a
    elif mode == "screen":
        # dst = 1 - (1 - dst) * (1 - a*src)  — symmetric brighten
        np.subtract(1.0, dst, out=dst)
        dst *= 1.0 - a * src
        np.subtract(1.0, dst, out=dst)
    elif mode == "multiply":
        # dst = dst * ((1 - a) + a * src) — opacity blends toward "no change"
        dst *= 1.0 - a * (1.0 - src)
    else:
        raise ValueError(f"unknown blend mode: {mode!r}")


class Mixer:
    """Stack of effect layers with crossfade-between-stacks support.

    The engine calls `render(t, out)` once per frame. Mutations to
    `layers` / `blackout` / `crossfade_to(...)` happen between frames in
    the same asyncio loop, so no locking is needed.
    """

    def __init__(self, n: int):
        self.n = n
        self.layers: list[Layer] = []
        self.blackout: bool = False
        self._scratch = np.zeros((n, 3), dtype=np.float32)
        self._buf_a = np.zeros((n, 3), dtype=np.float32)
        self._buf_b = np.zeros((n, 3), dtype=np.float32)
        self._from_layers: list[Layer] | None = None
        self._cf_start: float = 0.0
        self._cf_duration: float = 0.0

    @property
    def is_crossfading(self) -> bool:
        return self._from_layers is not None

    def crossfade_to(self, new_layers: list[Layer], duration: float, t: float) -> None:
        """Replace the active stack, optionally fading from old to new over `duration` seconds."""
        if duration <= 0.0 or not self.layers:
            self.layers = list(new_layers)
            self._from_layers = None
            return
        self._from_layers = list(self.layers)
        self.layers = list(new_layers)
        self._cf_start = t
        self._cf_duration = duration

    def render(self, t: float, out: np.ndarray) -> None:
        if self.blackout:
            out.fill(0.0)
            return
        if self._from_layers is not None:
            elapsed = t - self._cf_start
            if elapsed >= self._cf_duration:
                self._from_layers = None
            else:
                alpha = float(np.clip(elapsed / max(self._cf_duration, 1e-9), 0.0, 1.0))
                self._render_stack(self._from_layers, t, self._buf_a)
                self._render_stack(self.layers, t, self._buf_b)
                np.multiply(self._buf_a, 1.0 - alpha, out=out)
                out += self._buf_b * alpha
                np.clip(out, 0.0, 1.0, out=out)
                return
        self._render_stack(self.layers, t, out)

    def _render_stack(self, layers: list[Layer], t: float, out: np.ndarray) -> None:
        out.fill(0.0)
        if not layers:
            return
        for layer in layers:
            self._scratch.fill(0.0)
            layer.effect.render(t, self._scratch)
            _blend_into(out, self._scratch, layer.blend, layer.opacity)
        np.clip(out, 0.0, 1.0, out=out)
