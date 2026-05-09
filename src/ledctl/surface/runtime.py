"""Runtime — owns LIVE + PREVIEW compositions, mode, crossfade, masters.

Resolume-inspired layered model:

  * a *composition* is a list of *layers* (each layer = one Effect instance
    + blend + opacity + a name);
  * the runtime owns two compositions: LIVE (always rendered to LEDs) and
    PREVIEW (rendered to the simulator in design mode);
  * crossfade only ever applies on **promote** (swap PREVIEW → LIVE) — preview
    swaps are hard cuts because the operator is iterating;
  * each composition has a *selected layer* — that's what the chat panel and
    the param panel target. Default layer count is 1 so the simple case
    (single-effect per slot) is just "edit layer 0".

The Runtime is framework-free — it doesn't know about FastAPI, audio bridges,
or transports. The Engine wires it up.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from .base import (
    AudioView,
    Effect,
    EffectFrameContext,
    EffectInitContext,
    FrameMap,
    MastersView,
    ParamStore,
    ParamView,
    RigInfo,
)
from .helpers import (
    LUT_SIZE,
    PI,
    TAU,
    clip01,
    gauss,
    hex_to_rgb,
    hsv_to_rgb,
    lerp,
    log,
    named_palette,
    palette_lerp,
    pulse,
    tri,
    wrap_dist,
)
from .palettes import named_palette_names
from .sandbox import EffectCompileError, compile_effect

_log = logging.getLogger(__name__)


# Rec. 709 luminance — saturation master pulls toward greyscale.
_LUM = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

_PEAK_RELEASE_HALFLIFE = 0.5
_PEAK_FLOOR = 0.1
_MAX_ADAPTIVE_GAIN = 6.0


BlendMode = Literal["normal", "add", "screen", "multiply"]
BLEND_MODES: tuple[str, ...] = ("normal", "add", "screen", "multiply")


# ---------- runtime namespace builder ---------- #


def build_runtime_namespace(name: str) -> dict[str, Any]:
    """Names available to LLM-authored code. Seed `rng` from `name`."""
    seed = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")
    return {
        "np": np,
        "Effect": Effect,
        "hex_to_rgb": hex_to_rgb,
        "hsv_to_rgb": hsv_to_rgb,
        "lerp": lerp,
        "clip01": clip01,
        "gauss": gauss,
        "pulse": pulse,
        "tri": tri,
        "wrap_dist": wrap_dist,
        "palette_lerp": palette_lerp,
        "named_palette": named_palette,
        "rng": np.random.default_rng(seed),
        "log": log,
        "PI": PI,
        "TAU": TAU,
        "LUT_SIZE": LUT_SIZE,
        "PALETTE_NAMES": tuple(named_palette_names()),
    }


# ---------- composition entities ---------- #


_PERF_WINDOW_FRAMES = 60   # 1 s of render times at 60 fps
_BUDGET_TRIP_FRAMES = 30   # 0.5 s @ 60 fps of consecutive over-budget p95


class RenderStats:
    """Rolling per-layer render timing with mean / p95 / over-budget counter."""

    __slots__ = ("samples", "mean_ms", "p95_ms", "over_budget", "tripped")

    def __init__(self):
        # Fixed-size ring buffer of render times in milliseconds.
        self.samples: list[float] = []
        self.mean_ms: float = 0.0
        self.p95_ms: float = 0.0
        self.over_budget: int = 0
        self.tripped: bool = False

    def add(self, ms: float, budget_ms: float) -> None:
        self.samples.append(ms)
        if len(self.samples) > _PERF_WINDOW_FRAMES:
            self.samples.pop(0)
        if not self.samples:
            return
        self.mean_ms = sum(self.samples) / len(self.samples)
        # Cheap p95 — sort copy of the small window.
        s = sorted(self.samples)
        idx = max(0, int(0.95 * (len(s) - 1)))
        self.p95_ms = s[idx]
        if self.p95_ms > budget_ms:
            self.over_budget += 1
        else:
            self.over_budget = 0
        if self.over_budget >= _BUDGET_TRIP_FRAMES:
            self.tripped = True


@dataclass
class Layer:
    """One entry in a composition.

    `name` is the saved-effect identifier (also used as the on-disk slug).
    `summary` is a human-readable one-liner for chat / UI badges. `source` is
    the LLM-written source so the system prompt can show CURRENT EFFECTS.
    """

    name: str
    summary: str
    source: str
    instance: Effect
    params: ParamStore
    blend: BlendMode = "normal"
    opacity: float = 1.0
    enabled: bool = True
    consecutive_failures: int = 0
    perf: RenderStats = field(default_factory=RenderStats)


@dataclass
class Composition:
    """A list of layers + a selected index. Default = 1 layer."""

    layers: list[Layer] = field(default_factory=list)
    selected: int = 0

    def selected_layer(self) -> Layer | None:
        if not self.layers:
            return None
        i = max(0, min(self.selected, len(self.layers) - 1))
        return self.layers[i]

    def select(self, index: int) -> int:
        if not self.layers:
            self.selected = 0
            return 0
        self.selected = max(0, min(int(index), len(self.layers) - 1))
        return self.selected


@dataclass
class CrossfadeState:
    previous: Composition
    duration: float
    # Stamped on the first render call after the crossfade is created — the
    # render loop's `wall_t` is relative to engine start, so we can't sample
    # it from outside the loop. None = "not yet started; use this frame's wall_t".
    start_wall_t: float | None = None


# ---------- runtime ---------- #


class Runtime:
    def __init__(self, topology, masters, *, strict_params: bool = False):
        self.topology = topology
        self.masters = masters
        self.n = topology.pixel_count
        self.live = Composition()
        self.preview = Composition()
        self.mode: str = "live"
        self.blackout: bool = False
        self.crossfade_seconds: float = 1.0
        self.strict_params: bool = bool(strict_params)
        # Render the design-mode preview every other frame. The simulator
        # is a UI preview — 30 fps is plenty for visual judgment, and this
        # halves design-mode CPU during the trickiest scenario (crossfade +
        # preview running concurrently). Start the counter at -1 so the
        # first design-mode tick *does* render (we want immediate feedback
        # when the operator switches modes or after a write_effect swap).
        self.preview_half_rate: bool = True
        self._preview_tick: int = -1
        self._cf: CrossfadeState | None = None
        # Render scratch buffers — one accumulator per leg + one for crossfade.
        self._live_buf = np.zeros((self.n, 3), dtype=np.float32)
        self._preview_buf = np.zeros((self.n, 3), dtype=np.float32)
        self._cf_buf = np.zeros((self.n, 3), dtype=np.float32)
        self._layer_scratch = np.zeros((self.n, 3), dtype=np.float32)
        # Adaptive-brightness peak envelope.
        self._recent_peak: float = 0.0
        self._last_peak_wall_t: float = -1.0
        # Calibration override (set/cleared by the engine).
        self.calibration = None

    # ---- composition pickers ---- #

    def composition(self, slot: str) -> Composition:
        if slot == "live":
            return self.live
        if slot == "preview":
            return self.preview
        raise ValueError(f"unknown slot: {slot!r}")

    # ---- effect compilation / install ---- #

    INIT_BUDGET_MS = 200.0

    def _compile_layer(
        self,
        *,
        name: str,
        summary: str,
        source: str,
        param_schema: list[dict],
        param_values: dict[str, object] | None,
        blend: str,
        opacity: float,
        run_fence: bool,
    ) -> Layer:
        """Compile + init + (optionally) fence-test. Raises EffectCompileError."""
        ns = build_runtime_namespace(name)
        cls = compile_effect(source, name, ns)
        instance = cls()
        instance._setup(self.n)
        store = ParamStore(param_schema)
        if param_values:
            store.set_initial_values(param_values)
        init_ctx = self._build_init_ctx()
        # Init budget — common cause of overrun is an O(N²) precompute the LLM
        # didn't realise was quadratic; we'd rather catch it here than have the
        # operator see a hitch on promote.
        t0 = time.perf_counter()
        try:
            instance.init(init_ctx)
        except Exception as e:
            raise EffectCompileError(
                f"init() failed: {type(e).__name__}: {e}"
            ) from e
        init_ms = (time.perf_counter() - t0) * 1000.0
        if init_ms > self.INIT_BUDGET_MS:
            raise EffectCompileError(
                f"init() took {init_ms:.0f} ms (budget: {self.INIT_BUDGET_MS:.0f} ms). "
                f"Common cause: a per-pair O(N²) precompute over {self.n} LEDs. "
                f"Vectorise it or move the work into render()."
            )
        if instance.out is None:
            raise EffectCompileError(
                "Effect.out is None after init — base.__init__ + _setup not "
                "called (don't override __init__ unless you call super().__init__())"
            )
        layer = Layer(
            name=name, summary=summary, source=source,
            instance=instance, params=store,
            blend=_validate_blend(blend), opacity=_clip01(opacity),
        )
        if run_fence:
            self._fence_test(layer)
        return layer

    def install_layer(
        self,
        slot: str,
        *,
        name: str,
        summary: str,
        source: str,
        param_schema: list[dict],
        param_values: dict[str, object] | None = None,
        blend: str = "normal",
        opacity: float = 1.0,
        index: int | None = None,
        replace: bool = True,
    ) -> Layer:
        """Compile and place a layer into a slot's composition.

        If `replace=True` and `index` is None, replaces the *selected* layer
        (default behaviour for the chat agent). If `index` is given, replaces
        that index. If `replace=False`, inserts at `index` (or appends).

        Live mutations start a crossfade from the previous live composition.
        """
        comp = self.composition(slot)
        layer = self._compile_layer(
            name=name, summary=summary, source=source,
            param_schema=param_schema, param_values=param_values,
            blend=blend, opacity=opacity,
            run_fence=True,
        )
        if slot == "live":
            old_comp = _clone_composition(comp)
        if replace:
            target = comp.selected if index is None else int(index)
            if not comp.layers:
                comp.layers.append(layer)
                comp.selected = 0
            else:
                target = max(0, min(target, len(comp.layers) - 1))
                comp.layers[target] = layer
                comp.selected = target
        else:
            if index is None:
                insert_at = len(comp.layers)
            else:
                insert_at = max(0, min(int(index), len(comp.layers)))
            comp.layers.insert(insert_at, layer)
            comp.selected = insert_at
        if slot == "live":
            self._maybe_start_crossfade(old_comp)
        return layer

    def remove_layer(self, slot: str, index: int) -> bool:
        comp = self.composition(slot)
        if index < 0 or index >= len(comp.layers):
            return False
        if slot == "live":
            old_comp = _clone_composition(comp)
        del comp.layers[index]
        if comp.selected >= len(comp.layers):
            comp.selected = max(0, len(comp.layers) - 1)
        if slot == "live":
            self._maybe_start_crossfade(old_comp)
        return True

    def reorder_layer(self, slot: str, src: int, dst: int) -> bool:
        comp = self.composition(slot)
        if src < 0 or src >= len(comp.layers):
            return False
        if dst < 0 or dst >= len(comp.layers):
            return False
        if slot == "live":
            old_comp = _clone_composition(comp)
        layer = comp.layers.pop(src)
        comp.layers.insert(dst, layer)
        comp.selected = dst
        if slot == "live":
            self._maybe_start_crossfade(old_comp)
        return True

    def patch_layer_meta(
        self,
        slot: str,
        index: int,
        *,
        blend: str | None = None,
        opacity: float | None = None,
        enabled: bool | None = None,
    ) -> bool:
        comp = self.composition(slot)
        if index < 0 or index >= len(comp.layers):
            return False
        layer = comp.layers[index]
        if blend is not None:
            layer.blend = _validate_blend(blend)
        if opacity is not None:
            layer.opacity = _clip01(opacity)
        if enabled is not None:
            layer.enabled = bool(enabled)
        return True

    def select_layer(self, slot: str, index: int) -> int:
        return self.composition(slot).select(index)

    # ---- promote / pull ---- #

    def promote(self) -> None:
        """Crossfade LIVE ← PREVIEW (preserves preview)."""
        old_comp = _clone_composition(self.live)
        new_layers = []
        for src in self.preview.layers:
            new_layers.append(_clone_layer_for_live(src, self))
        self.live = Composition(layers=new_layers, selected=self.preview.selected)
        self._maybe_start_crossfade(old_comp)

    def pull_live_to_preview(self) -> None:
        """Copy LIVE → PREVIEW (overwrites preview, hard cut)."""
        new_layers = []
        for src in self.live.layers:
            new_layers.append(_clone_layer_for_live(src, self))
        self.preview = Composition(layers=new_layers, selected=self.live.selected)

    # ---- crossfade helper ---- #

    def _maybe_start_crossfade(self, prev: Composition) -> None:
        if self.crossfade_seconds <= 0.0 or not prev.layers:
            self._cf = None
            return
        # start_wall_t = None — stamped on the first render call below, so the
        # crossfade is in the same time domain as `ctx.wall_t`.
        self._cf = CrossfadeState(
            previous=prev,
            duration=float(self.crossfade_seconds),
            start_wall_t=None,
        )

    # ---- contexts ---- #

    def _build_init_ctx(self) -> EffectInitContext:
        bbox_min = tuple(float(x) for x in self.topology.bbox_min.tolist())
        bbox_max = tuple(float(x) for x in self.topology.bbox_max.tolist())
        rig = RigInfo(
            bbox_min=bbox_min,
            bbox_max=bbox_max,
            target_fps=60.0,
            span_x_m=float(bbox_max[0] - bbox_min[0]),
            span_y_m=float(bbox_max[1] - bbox_min[1]),
        )
        return EffectInitContext(
            n=self.n,
            pos=self.topology.normalised_positions.astype(np.float32, copy=False),
            frames=FrameMap(self.topology.derived),
            strips=list(self.topology.strips),
            rig=rig,
        )

    def _build_frame_ctx(
        self,
        layer: Layer,
        wall_t: float,
        dt: float,
        t_eff: float,
        audio: AudioView,
    ) -> EffectFrameContext:
        m = self.masters
        masters_view = MastersView(
            brightness=float(m.brightness),
            speed=float(m.speed),
            audio_reactivity=float(m.audio_reactivity),
            saturation=float(m.saturation),
            crossfade_seconds=float(self.crossfade_seconds),
        )
        return EffectFrameContext(
            t=float(t_eff),
            wall_t=float(wall_t),
            dt=float(dt),
            audio=audio,
            params=ParamView(layer.params, strict=self.strict_params),
            masters=masters_view,
            n=self.n,
            frames=FrameMap(self.topology.derived),
            pos=self.topology.normalised_positions,
        )

    # ---- fence test ---- #

    # 30 synthetic frames — catches NaN drift, off-by-one in deposit logic,
    # sparkle-pool overflow, scratch-buffer aliasing that 10 frames misses.
    def _fence_test(self, layer: Layer, frames: int = 30) -> None:
        wall_t = 0.0
        dt = 1.0 / 60.0
        for i in range(frames):
            audio = AudioView(
                low=0.4 + 0.4 * float(np.sin(i * 0.7)),
                mid=0.3, high=0.2,
                beat=1 if i % 6 == 0 else 0,
                beats_since_start=i // 6,
                bpm=120.0,
                connected=True,
            )
            ctx = self._build_frame_ctx(layer, wall_t, dt, wall_t, audio)
            try:
                rgb = layer.instance.render(ctx)
            except Exception as e:
                # Surface the traceback so the LLM sees which line failed —
                # generic NumPy errors like "all input arrays must have the
                # same shape" are useless without a line number.
                import traceback
                tb = traceback.format_exc()
                tb_lines = [
                    line for line in tb.splitlines()
                    if "<llm:" in line or "EffectError" in line
                    or line.strip().startswith("File ")
                ]
                tb_short = "\n".join(tb_lines) if tb_lines else tb
                hint = _diagnostic_hint(e)
                raise EffectCompileError(
                    f"render() crashed on synthetic frame {i}: "
                    f"{type(e).__name__}: {e}\n"
                    f"{hint}\n"
                    f"---\n{tb_short}"
                ) from e
            if not isinstance(rgb, np.ndarray):
                raise EffectCompileError(
                    f"render() returned {type(rgb).__name__}, expected ndarray"
                )
            if rgb.shape != (self.n, 3):
                raise EffectCompileError(
                    f"render() returned shape {rgb.shape}, expected ({self.n}, 3)"
                )
            if rgb.dtype != np.float32:
                raise EffectCompileError(
                    f"render() returned dtype {rgb.dtype}, expected float32"
                )
            if not np.isfinite(rgb).all():
                raise EffectCompileError(
                    f"render() produced NaN/Inf on synthetic frame {i}"
                )
            wall_t += dt

    # ---- composition rendering ---- #

    PER_LAYER_BUDGET_MS = 5.0
    CONSECUTIVE_FAIL_LIMIT = 3

    def _render_composition(
        self,
        comp: Composition,
        wall_t: float,
        dt: float,
        t_eff: float,
        audio: AudioView,
        *,
        out: np.ndarray,
    ) -> np.ndarray:
        """Render a composition into `out`. Walks layers bottom-up."""
        out.fill(0.0)
        for layer in comp.layers:
            if not layer.enabled or layer.opacity <= 0.0:
                continue
            ctx = self._build_frame_ctx(layer, wall_t, dt, t_eff, audio)
            t0 = time.perf_counter()
            try:
                rgb = layer.instance.render(ctx)
            except Exception:
                _log.exception("layer %r raised in render()", layer.name)
                layer.consecutive_failures += 1
                self._maybe_disable_failing_layer(layer)
                continue
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            layer.perf.add(elapsed_ms, self.PER_LAYER_BUDGET_MS)
            if layer.perf.tripped and layer.perf.over_budget != 0:
                _log.warning(
                    "layer %r tripped render budget (p95=%.1f ms); disabling",
                    layer.name, layer.perf.p95_ms,
                )
                layer.enabled = False
                layer.perf.tripped = False  # one-shot — operator can re-enable
                continue
            if not isinstance(rgb, np.ndarray) or rgb.shape != (self.n, 3):
                layer.consecutive_failures += 1
                self._maybe_disable_failing_layer(layer)
                continue
            if rgb.dtype != np.float32:
                rgb = rgb.astype(np.float32, copy=False)
            np.copyto(self._layer_scratch, rgb)
            np.clip(self._layer_scratch, 0.0, 1.0, out=self._layer_scratch)
            _blend_into(out, self._layer_scratch, layer.blend, float(layer.opacity))
            layer.consecutive_failures = 0
        np.clip(out, 0.0, 1.0, out=out)
        return out

    def _maybe_disable_failing_layer(self, layer: Layer) -> None:
        """3-strikes rule: a layer that crashes on consecutive frames is
        disabled rather than re-tried indefinitely. Operator can re-enable
        from the layer meta toggle."""
        if layer.consecutive_failures >= self.CONSECUTIVE_FAIL_LIMIT:
            _log.warning(
                "layer %r failed %d frames in a row; disabling",
                layer.name, layer.consecutive_failures,
            )
            layer.enabled = False

    # ---- per-frame entry point ---- #

    def render(
        self,
        *,
        wall_t: float,
        dt: float,
        t_eff: float,
        audio: AudioView,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Render LIVE + (when in design mode) PREVIEW; apply masters."""
        # ---- LIVE composition (with optional crossfade) ---- #
        if self._cf is not None:
            if self._cf.start_wall_t is None:
                self._cf.start_wall_t = wall_t
            elapsed = wall_t - self._cf.start_wall_t
            if elapsed >= self._cf.duration:
                self._cf = None
                self._render_composition(self.live, wall_t, dt, t_eff, audio,
                                         out=self._live_buf)
            else:
                alpha = float(np.clip(elapsed / max(self._cf.duration, 1e-9),
                                      0.0, 1.0))
                self._render_composition(self._cf.previous, wall_t, dt, t_eff, audio,
                                         out=self._cf_buf)
                self._render_composition(self.live, wall_t, dt, t_eff, audio,
                                         out=self._live_buf)
                np.multiply(self._cf_buf, 1.0 - alpha, out=self._cf_buf)
                self._cf_buf += self._live_buf * alpha
                np.clip(self._cf_buf, 0.0, 1.0, out=self._live_buf)
        else:
            self._render_composition(self.live, wall_t, dt, t_eff, audio,
                                     out=self._live_buf)

        if self.blackout:
            self._live_buf.fill(0.0)
        self._apply_master_output(self._live_buf, wall_t, update_envelope=True)
        if self.calibration is not None:
            self._apply_calibration(self._live_buf, wall_t)

        # ---- SIM leg ---- #
        if self.mode == "design":
            self._preview_tick += 1
            do_preview = (
                not self.preview_half_rate
                or (self._preview_tick & 1) == 0
            )
            if do_preview:
                self._render_composition(self.preview, wall_t, dt, t_eff, audio,
                                         out=self._preview_buf)
                if self.blackout:
                    self._preview_buf.fill(0.0)
                self._apply_master_output(self._preview_buf, wall_t,
                                          update_envelope=False)
                if self.calibration is not None:
                    self._apply_calibration(self._preview_buf, wall_t)
            # When skipping, return the previously-rendered buffer — the
            # WS frame stream emits the same bytes and the browser draws
            # an unchanged canvas for one frame. Visually indistinguishable.
            return self._live_buf, self._preview_buf
        return self._live_buf, self._live_buf

    # ---- master output stage ---- #

    def _apply_master_output(
        self, rgb: np.ndarray, wall_t: float, *, update_envelope: bool = True
    ) -> None:
        m = self.masters
        sat = float(m.saturation)
        bright = float(m.brightness)
        if sat != 1.0:
            grey = (rgb @ _LUM)[:, None]
            np.subtract(rgb, grey, out=rgb)
            rgb *= sat
            rgb += grey
        if update_envelope and not self.blackout and rgb.size:
            self._update_peak_envelope(float(np.max(rgb)), float(wall_t))
        if bright <= 1.0:
            if bright != 1.0:
                rgb *= bright
        else:
            peak = max(self._recent_peak, _PEAK_FLOOR)
            target = peak + (bright - 1.0) * (1.0 - peak)
            gain = min(target / peak, _MAX_ADAPTIVE_GAIN)
            if gain != 1.0:
                rgb *= gain
        np.clip(rgb, 0.0, 1.0, out=rgb)

    def _update_peak_envelope(self, current_peak: float, wall_t: float) -> None:
        last = self._last_peak_wall_t
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

    # ---- calibration override ---- #

    def _apply_calibration(self, rgb: np.ndarray, wall_t: float) -> None:
        cal = self.calibration
        if cal is None:
            return
        rgb.fill(0.0)
        n = self.n
        if cal.mode == "solo":
            for i in cal.indices:
                if 0 <= i < n:
                    rgb[i] = cal.color
            return
        steps = int(max(0.0, wall_t - cal.start_t) / cal.interval)
        idx = (steps * cal.step) % n
        cal.current = idx
        rgb[idx] = cal.color

    # ---- topology hot-swap ---- #

    def swap_topology(self, new_topology) -> None:
        self.topology = new_topology
        self.n = new_topology.pixel_count
        self._live_buf = np.zeros((self.n, 3), dtype=np.float32)
        self._preview_buf = np.zeros((self.n, 3), dtype=np.float32)
        self._cf_buf = np.zeros((self.n, 3), dtype=np.float32)
        self._layer_scratch = np.zeros((self.n, 3), dtype=np.float32)
        for slot in ("live", "preview"):
            comp = self.composition(slot)
            new_layers: list[Layer] = []
            for old in comp.layers:
                try:
                    new_layers.append(self._compile_layer(
                        name=old.name, summary=old.summary, source=old.source,
                        param_schema=old.params.schema,
                        param_values=old.params.values(),
                        blend=old.blend, opacity=old.opacity,
                        run_fence=False,
                    ))
                except EffectCompileError:
                    _log.exception("re-install of layer %r failed", old.name)
            comp.layers = new_layers
            if comp.selected >= len(comp.layers):
                comp.selected = max(0, len(comp.layers) - 1)
        self._cf = None  # discard any in-flight crossfade

    # ---- snapshots for /active and the system prompt ---- #

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "blackout": self.blackout,
            "crossfading": self._cf is not None,
            "crossfade_seconds": self.crossfade_seconds,
            "live": _comp_summary(self.live),
            "preview": _comp_summary(self.preview),
        }


# ---------- helpers ---------- #


def _validate_blend(blend: str) -> BlendMode:
    if blend not in BLEND_MODES:
        raise ValueError(f"unknown blend mode: {blend!r}")
    return blend  # type: ignore[return-value]


def _clip01(v) -> float:
    return float(max(0.0, min(1.0, float(v))))


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


def _layer_summary(layer: Layer) -> dict[str, Any]:
    return {
        "name": layer.name,
        "summary": layer.summary,
        "source": layer.source,
        "blend": layer.blend,
        "opacity": layer.opacity,
        "enabled": layer.enabled,
        "param_schema": layer.params.schema,
        "param_values": layer.params.values(),
        "perf": {
            "mean_ms": round(layer.perf.mean_ms, 3),
            "p95_ms": round(layer.perf.p95_ms, 3),
            "consecutive_failures": layer.consecutive_failures,
        },
    }


def _comp_summary(comp: Composition) -> dict[str, Any]:
    return {
        "selected": comp.selected,
        "layers": [_layer_summary(layer) for layer in comp.layers],
    }


def _clone_composition(comp: Composition) -> Composition:
    """Shallow clone for crossfade — shares Effect instances since the new
    composition replaces them anyway. Not safe for general aliasing."""
    return Composition(
        layers=list(comp.layers),
        selected=comp.selected,
    )


def _clone_layer_for_live(src: Layer, runtime: Runtime) -> Layer:
    """Promote / pull: clone a layer so the destination looks IDENTICAL to
    the source at the moment of the swap.

    Deepcopy the running Effect instance (head positions, sparkle ages,
    palette LUTs, all `self.*` state) into a fresh Layer wrapper with its
    own ParamStore. Live and preview don't share mutable references after
    this — each evolves independently from the same starting state.

    A fresh recompile + init would reset stateful effects to t=0, so the
    operator would see the comets jump back to the start on every promote.
    That's what the deepcopy avoids.

    If deepcopy fails (LLM emitted an effect with non-copyable state — a
    socket, lock, generator), fall back to recompile + reinit so promote
    still works; the visible state reset is the lesser evil.
    """
    try:
        new_instance = copy.deepcopy(src.instance)
        new_params = ParamStore(src.params.schema)
        new_params.set_initial_values(src.params.values())
        return Layer(
            name=src.name, summary=src.summary, source=src.source,
            instance=new_instance, params=new_params,
            blend=src.blend, opacity=src.opacity,
            enabled=src.enabled,
        )
    except Exception:
        _log.exception(
            "deepcopy of layer %r failed; falling back to recompile (state will reset)",
            src.name,
        )
        return runtime._compile_layer(
            name=src.name, summary=src.summary, source=src.source,
            param_schema=src.params.schema,
            param_values=src.params.values(),
            blend=src.blend, opacity=src.opacity,
            run_fence=False,
        )


# Backwards-friendly alias for tests / docs that referenced ActiveEffect.
ActiveEffect = Layer


def _diagnostic_hint(exc: Exception) -> str:
    """Map a fence-test exception to an actionable hint for the LLM. The hint
    is appended to the EffectCompileError message and shows up under
    LAST EFFECT ERROR in the next system prompt, so the LLM can self-correct
    without burning a retry."""
    msg = str(exc)
    name = type(exc).__name__
    if name == "AttributeError":
        # Most common: ctx.x / ctx.y / ctx.u_loop instead of ctx.frames.<name>
        if "'EffectFrameContext'" in msg or "'EffectInitContext'" in msg:
            return (
                "Hint: the context doesn't have that attribute directly. "
                "Per-LED frames live at `ctx.frames.<name>` (e.g. `ctx.frames.x`, "
                "`ctx.frames.u_loop`, `ctx.frames.radius`). The 3D position array "
                "is `ctx.pos` (shape (N, 3)). The audio bands are at "
                "`ctx.audio.low / .mid / .high`. See COORDINATE FRAMES + RUNTIME "
                "API in the system prompt for the full list."
            )
        if "unknown frame" in msg:
            # FrameMap.__getattr__ raises with this prefix.
            return (
                "Hint: that frame doesn't exist. Available names are listed in "
                "COORDINATE FRAMES (x/y/z, signed_x/y/z, u_loop, radius, angle, "
                "side_top, side_bottom, axial_dist, axial_signed, corner_dist, "
                "strip_id, chain_index, distance)."
            )
        if "unknown param" in msg:
            # ParamView.__getattr__ raises with this prefix when an effect
            # reaches for a key it didn't declare in its schema.
            return (
                "Hint: that param key isn't in your declared schema. Either add "
                "it under `params` in your write_effect call, or read it from a "
                "constant. Use `ctx.params.<key>` (NOT `ctx.params['<key>']`)."
            )
        if "'AudioView'" in msg:
            return (
                "Hint: AudioView exposes low/mid/high/beat/beats_since_start/bpm/"
                "connected and a `bands` dict — nothing else. No `volume`, `peak`, "
                "or `freq` fields."
            )
    if name == "ValueError" and "shape" in msg:
        return (
            "Hint: shape mismatch. Common causes:\n"
            "  - hsv_to_rgb(per_led_h, scalar_s, scalar_v) is supported and broadcasts;\n"
            "  - np.stack / np.concatenate on arrays of different ndim — broadcast first;\n"
            "  - per-LED ops mixing (N,) and scalar — usually fine; mixing (N,) and (3,) is not."
        )
    if name == "TypeError" and "params are read-only" in msg:
        return (
            "Hint: you tried to write to ctx.params.* — those are operator-owned. "
            "Read them with `ctx.params.<key>`; if you need a derived value, compute "
            "it into a local."
        )
    if name == "KeyError":
        return (
            "Hint: KeyError usually means a frame name typo or a param key that's "
            "not in your declared schema. Frames at `ctx.frames`; params via "
            "`ctx.params.<key>` (NOT `ctx.params['<key>']`)."
        )
    return (
        "Hint: traceback below shows which line in your code raised. Common pitfalls:\n"
        "  - per-LED data lives at `ctx.frames.<name>` (not `ctx.<name>`);\n"
        "  - audio bands at `ctx.audio.low/.mid/.high`;\n"
        "  - return must be (N, 3) float32 in [0, 1] — fill `self.out` in place."
    )
