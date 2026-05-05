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

Brightness master has two regimes:
  - `brightness ≤ 1.0`: pure linear gain + clip (legacy behaviour).
  - `brightness > 1.0`: adaptive headroom. The mixer tracks a rolling peak of
    the post-saturation stack (fast attack, slow release) and derives a gain
    that pushes that peak toward 1.0 — so a stack whose loudest pixel only
    reaches 0.7 can be lifted to use the full output range without harshly
    crushing peaks. brightness=1.0 → no extra gain, brightness=2.0 → full
    auto-fit. A final hard clip absorbs the small transients that can punch
    above the recent-peak estimate between frames.
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

# Adaptive-brightness peak envelope (engaged when masters.brightness > 1.0):
#   - fast attack (snap up immediately when a peak appears)
#   - exponential release with this halflife (don't pump on quiet frames)
#   - floor on the tracked peak so dark stacks don't ask for absurd gain
#   - hard cap on derived gain as a final safety
_PEAK_RELEASE_HALFLIFE = 0.5   # seconds
_PEAK_FLOOR = 0.1              # treat tracked peak below this as 0.1
_MAX_ADAPTIVE_GAIN = 6.0       # safety cap on the gain we will ever apply


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
        # Adaptive-brightness peak envelope. Updated every non-blackout frame
        # in _apply_master_output, regardless of slider position, so flipping
        # brightness above 1.0 doesn't have to wait for the envelope to warm up.
        self._recent_peak: float = 0.0
        self._last_peak_wall_t: float = -1.0  # sentinel: no prior frame yet

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

        # Update the rolling-peak envelope on the post-saturation stack. We do
        # this on every non-blackout frame so the headroom estimate is already
        # warm if the operator pushes brightness above 1.0.
        if not self.blackout and rgb.size:
            self._update_peak_envelope(float(np.max(rgb)), float(ctx.wall_t))

        if bright <= 1.0:
            if bright != 1.0:
                rgb *= bright
        else:
            # Adaptive headroom: target_peak interpolates from current recent
            # peak (at brightness=1) to 1.0 (at brightness=2). gain is the
            # uniform multiplier needed to lift recent_peak to target_peak.
            peak = max(self._recent_peak, _PEAK_FLOOR)
            target = peak + (bright - 1.0) * (1.0 - peak)
            gain = min(target / peak, _MAX_ADAPTIVE_GAIN)
            if gain != 1.0:
                rgb *= gain
        # Final clamp so subsequent gamma never sees out-of-range values; also
        # absorbs the small transients that punch past recent_peak between frames.
        np.clip(rgb, 0.0, 1.0, out=rgb)

    def _update_peak_envelope(self, current_peak: float, wall_t: float) -> None:
        """Fast-attack, slow-release max-follower used by the brightness master.

        Tracks the per-frame max of the post-saturation stack. Peaks snap up
        immediately so we never under-estimate headroom; quiet stretches decay
        toward the new floor with `_PEAK_RELEASE_HALFLIFE`, slow enough that
        a one-beat dip in audio doesn't pump the boost up and back.
        """
        last = self._last_peak_wall_t
        # Initialise / detect a non-monotonic wall_t (e.g. fresh process). When
        # the very first frame arrives, snap straight to current_peak instead
        # of sliding up from zero — the boost is correct from frame 1.
        if last < 0.0 or wall_t < last:
            self._recent_peak = current_peak
            self._last_peak_wall_t = wall_t
            return
        dt = wall_t - last
        self._last_peak_wall_t = wall_t
        if current_peak >= self._recent_peak:
            self._recent_peak = current_peak
        else:
            decay = 0.5 ** (dt / _PEAK_RELEASE_HALFLIFE)
            self._recent_peak *= decay
            if self._recent_peak < current_peak:
                self._recent_peak = current_peak
