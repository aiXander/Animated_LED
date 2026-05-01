"""Layer stack with blend modes, crossfade, and the master output stage.

A `Layer` is a compiled surface tree (`render_fn`) plus a blend mode and an
opacity. The mixer walks the stack, blends each layer's RGB into an
accumulator, optionally crossfades from a previous stack, then applies the
master output stage (saturation pull → brightness gain). Gamma still happens
last in `PixelBuffer.to_uint8(...)`.

Crossfade alpha is computed against `ctx.wall_t` — operator direction
("switch to peak preset over 1 s") must not be slowed by `speed=0.5` or
frozen by `freeze=true`. Per-layer rendering uses `ctx.t` (the
master-speed-scaled effective time).
"""

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from .masters import RenderContext
from .surface import CompiledNode

BlendMode = Literal["normal", "add", "screen", "multiply"]
BLEND_MODES: tuple[str, ...] = ("normal", "add", "screen", "multiply")

# Rec. 709 luminance weights — used by the saturation master.
_LUM = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


@dataclass
class Layer:
    """One entry in the Mixer stack.

    Layers render bottom-up: layer 0 onto black, each subsequent layer blends
    into the accumulator using its `blend` and `opacity`.

    `node` is a compiled surface tree (`CompiledNode` with output_kind=rgb_field).
    `spec_node` is the source `NodeSpec` it was compiled from, kept so the
    REST `/state` payload and PATCH `/layers/{i}` can round-trip the spec
    without going through the surface again.
    """

    node: CompiledNode
    spec_node: dict = field(default_factory=dict)
    blend: BlendMode = "normal"
    opacity: float = 1.0


def _blend_into(dst: np.ndarray, src: np.ndarray, mode: str, opacity: float) -> None:
    """Blend `src` into `dst` in-place. Both float32 (N, 3) in [0, 1]."""
    a = float(np.clip(opacity, 0.0, 1.0))
    if a == 0.0:
        return
    if mode == "normal":
        dst *= 1.0 - a
        dst += src * a
    elif mode == "add":
        dst += src * a
    elif mode == "screen":
        np.subtract(1.0, dst, out=dst)
        dst *= 1.0 - a * src
        np.subtract(1.0, dst, out=dst)
    elif mode == "multiply":
        dst *= 1.0 - a * (1.0 - src)
    else:
        raise ValueError(f"unknown blend mode: {mode!r}")


class Mixer:
    """Stack of compiled layers with crossfade-between-stacks support.

    The engine calls `render(ctx, out)` once per frame. Mutations to
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

    def crossfade_to(
        self, new_layers: list[Layer], duration: float, wall_t: float
    ) -> None:
        """Replace the active stack, optionally fading from old to new over
        `duration` seconds (measured in wall-clock time)."""
        if duration <= 0.0 or not self.layers:
            self.layers = list(new_layers)
            self._from_layers = None
            return
        self._from_layers = list(self.layers)
        self.layers = list(new_layers)
        self._cf_start = wall_t
        self._cf_duration = duration

    def render(self, ctx: RenderContext, out: np.ndarray) -> None:
        if self.blackout:
            out.fill(0.0)
            self._apply_master_output(out, ctx)
            return
        if self._from_layers is not None:
            elapsed = ctx.wall_t - self._cf_start
            if elapsed >= self._cf_duration:
                self._from_layers = None
            else:
                alpha = float(
                    np.clip(elapsed / max(self._cf_duration, 1e-9), 0.0, 1.0)
                )
                self._render_stack(self._from_layers, ctx, self._buf_a)
                self._render_stack(self.layers, ctx, self._buf_b)
                np.multiply(self._buf_a, 1.0 - alpha, out=out)
                out += self._buf_b * alpha
                np.clip(out, 0.0, 1.0, out=out)
                self._apply_master_output(out, ctx)
                return
        self._render_stack(self.layers, ctx, out)
        self._apply_master_output(out, ctx)

    def _render_stack(
        self, layers: list[Layer], ctx: RenderContext, out: np.ndarray
    ) -> None:
        out.fill(0.0)
        if not layers:
            return
        for layer in layers:
            rgb = layer.node.render(ctx)
            _blend_into(out, rgb, layer.blend, layer.opacity)
        np.clip(out, 0.0, 1.0, out=out)

    # ---- master output stage ----

    def _apply_master_output(self, rgb: np.ndarray, ctx: RenderContext) -> None:
        masters = ctx.masters
        sat = float(masters.saturation)
        bright = float(masters.brightness)
        if sat != 1.0:
            # Pull toward greyscale: rgb = grey + (rgb - grey) * sat
            grey = (rgb @ _LUM)[:, None]
            np.subtract(rgb, grey, out=rgb)
            rgb *= sat
            rgb += grey
        if bright != 1.0:
            rgb *= bright
        # Final clamp so subsequent gamma never sees out-of-range values.
        np.clip(rgb, 0.0, 1.0, out=rgb)
