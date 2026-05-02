import asyncio
import contextlib
import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from .audio.state import AudioState
from .config import AppConfig
from .masters import MasterControls, RenderContext
from .mixer import BLEND_MODES, Layer, Mixer
from .pixelbuffer import PixelBuffer
from .surface import (
    Compiler,
    LayerSpec,
    NodeSpec,
    UpdateLedsSpec,
)
from .topology import Topology
from .transports.base import Transport

log = logging.getLogger(__name__)

# Default calibration colour: pure red at full brightness. Picked so it's
# unmistakeable against an off strip and never produced by an effect by accident.
_CAL_COLOR: tuple[float, float, float] = (1.0, 0.0, 0.0)


@dataclass
class CalibrationState:
    """Active calibration override.

    `solo` lights a fixed set of indices; `walk` advances through the strip,
    one index every `interval` seconds. Either mode bypasses the mixer.
    """

    mode: Literal["solo", "walk"]
    color: tuple[float, float, float] = _CAL_COLOR
    indices: tuple[int, ...] = ()           # solo
    step: int = 100                         # walk
    interval: float = 1.0                   # walk
    start_t: float = 0.0                    # walk
    current: int = 0                        # walk: index lit right now


def _validate_blend(blend: str) -> str:
    if blend not in BLEND_MODES:
        raise ValueError(f"unknown blend mode {blend!r}; must be one of {BLEND_MODES}")
    return blend


def _layer_from_spec(
    spec: dict[str, Any] | LayerSpec, topology: Topology
) -> Layer:
    """Validate + compile a single LayerSpec dict into a runtime Layer."""
    layer_spec = spec if isinstance(spec, LayerSpec) else LayerSpec.model_validate(spec)
    compiled = Compiler(topology).compile_layer(layer_spec)
    return Layer(
        node=compiled.node,
        spec_node=layer_spec.node.model_dump(),
        blend=_validate_blend(compiled.blend),
        opacity=float(compiled.opacity),
    )


class Engine:
    """Fixed-timestep render loop.

    Targets `target_fps` using `time.perf_counter`. If the encode/transport
    falls behind, drop the schedule forward rather than spiral-of-death
    catching up — better to skip a frame than queue them.

    `effective_t` is master-speed-scaled monotonic time; primitives read
    `ctx.t` (which is effective_t). Crossfade alpha and `envelope` smoothing
    use `ctx.wall_t` (raw monotonic) so operator direction never gets slowed
    by `masters.speed` and a frozen pattern still breathes with the room.
    """

    def __init__(
        self,
        cfg: AppConfig,
        topology: Topology,
        transport: Transport,
        masters: MasterControls | None = None,
    ):
        self.cfg = cfg
        self.topology = topology
        self.transport = transport
        self.buffer = PixelBuffer(topology.pixel_count)
        self.mixer = Mixer(topology.pixel_count)
        self.target_fps = cfg.project.target_fps
        self.gamma = cfg.output.gamma
        self.fps: float = 0.0
        self.frame_count: int = 0
        self.dropped_frames: int = 0
        self.elapsed: float = 0.0          # wall-clock since start
        self.effective_t: float = 0.0      # master-speed-scaled time
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.calibration: CalibrationState | None = None
        self.audio_state: AudioState | None = None
        self.masters: MasterControls = masters or MasterControls()

    def attach_audio(self, state: AudioState | None) -> None:
        """Make an AudioState visible to audio_band primitives via ctx."""
        self.audio_state = state
        self.topology.audio_state = state

    def set_masters(self, **patch: object) -> MasterControls:
        """Apply a partial master update; returns the post-clamp result."""
        self.masters = self.masters.merge(**patch)
        return self.masters

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="ledctl-engine")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ---- layer mutation API (called from FastAPI handlers) ----

    def push_layer(
        self,
        node: dict[str, Any] | NodeSpec,
        blend: str = "normal",
        opacity: float = 1.0,
    ) -> int:
        """Append a new compiled layer to the stack and return its index."""
        spec_dict = {
            "node": node.model_dump() if isinstance(node, NodeSpec) else node,
            "blend": blend,
            "opacity": float(opacity),
        }
        self.mixer.layers.append(_layer_from_spec(spec_dict, self.topology))
        return len(self.mixer.layers) - 1

    def update_layer(
        self,
        i: int,
        node: dict[str, Any] | NodeSpec | None = None,
        blend: str | None = None,
        opacity: float | None = None,
    ) -> None:
        """Patch a layer in place. `node` recompiles the tree (resets state)."""
        layer = self.mixer.layers[i]
        if blend is not None:
            layer.blend = _validate_blend(blend)
        if opacity is not None:
            layer.opacity = float(opacity)
        if node is not None:
            new_node = (
                node.model_dump() if isinstance(node, NodeSpec) else node
            )
            spec_dict = {
                "node": new_node,
                "blend": layer.blend,
                "opacity": layer.opacity,
            }
            new_layer = _layer_from_spec(spec_dict, self.topology)
            layer.node = new_layer.node
            layer.spec_node = new_layer.spec_node

    def remove_layer(self, i: int) -> None:
        self.mixer.layers.pop(i)

    def crossfade_to(
        self,
        layer_specs: list[dict[str, Any]] | list[LayerSpec],
        duration: float,
    ) -> None:
        """Replace the layer stack and crossfade from the previous one."""
        new_layers = [_layer_from_spec(spec, self.topology) for spec in layer_specs]
        self.mixer.crossfade_to(new_layers, duration, self.elapsed)

    def layer_state(self) -> list[dict[str, Any]]:
        return [
            {
                "node": dict(layer.spec_node),
                "blend": layer.blend,
                "opacity": layer.opacity,
            }
            for layer in self.mixer.layers
        ]

    # ---- calibration override ----

    def set_calibration_solo(self, indices: list[int]) -> CalibrationState:
        n = self.topology.pixel_count
        clean = tuple(sorted({int(i) for i in indices if 0 <= int(i) < n}))
        if not clean:
            raise ValueError(f"no valid global_index in {list(indices)} for {n} pixels")
        self.calibration = CalibrationState(mode="solo", indices=clean, current=clean[0])
        return self.calibration

    def set_calibration_walk(self, step: int, interval: float) -> CalibrationState:
        if step <= 0:
            raise ValueError(f"step must be > 0, got {step}")
        if interval <= 0:
            raise ValueError(f"interval must be > 0, got {interval}")
        self.calibration = CalibrationState(
            mode="walk",
            step=int(step),
            interval=float(interval),
            start_t=self.elapsed,
            current=0,
        )
        return self.calibration

    def clear_calibration(self) -> None:
        self.calibration = None

    def calibration_summary(self) -> dict[str, Any] | None:
        cal = self.calibration
        if cal is None:
            return None
        if cal.mode == "solo":
            return {"mode": "solo", "indices": list(cal.indices), "current": cal.current}
        return {
            "mode": "walk",
            "step": cal.step,
            "interval": cal.interval,
            "current": cal.current,
        }

    # ---- topology hot-swap (Phase 4 editor) ----

    def swap_topology(self, new_topology: Topology) -> None:
        """Replace topology and rebuild dependent state, preserving the layer stack.

        Layer trees close over per-LED arrays (e.g. normalised positions), so
        they must be recompiled against the new topology — even if pixel_count
        is unchanged. Layer specs (node tree, blend, opacity) survive.
        """
        specs = self.layer_state()
        n_old = self.topology.pixel_count
        self.topology = new_topology
        self.topology.audio_state = self.audio_state
        if new_topology.pixel_count != n_old:
            self.buffer = PixelBuffer(new_topology.pixel_count)
            self.mixer = Mixer(new_topology.pixel_count)
        else:
            self.mixer.layers.clear()
        for spec in specs:
            self.push_layer(spec["node"], blend=spec["blend"], opacity=spec["opacity"])
        cal = self.calibration
        if cal is not None and cal.mode == "solo":
            valid = tuple(i for i in cal.indices if i < new_topology.pixel_count)
            if not valid:
                self.calibration = None
            elif valid != cal.indices:
                self.calibration = CalibrationState(
                    mode="solo", indices=valid, current=valid[0]
                )

    def _apply_calibration(self, t: float) -> None:
        cal = self.calibration
        if cal is None:
            return
        rgb = self.buffer.rgb
        rgb.fill(0.0)
        n = self.topology.pixel_count
        if cal.mode == "solo":
            for i in cal.indices:
                rgb[i] = cal.color
            return
        steps = int(max(0.0, t - cal.start_t) / cal.interval)
        idx = (steps * cal.step) % n
        cal.current = idx
        rgb[idx] = cal.color

    # ---- per-frame audio scaling ----

    def _build_audio_view(self) -> AudioState | None:
        """Apply masters.audio_reactivity to the *_norm fields once per tick.

        Raw fields are passed through unchanged. The doc behind this
        (refactor §7.2 audio stage) is that the master applies uniformly
        regardless of how many `audio_band` references the tree holds, and a
        future second audio primitive doesn't have to re-implement the
        multiply.
        """
        s = self.audio_state
        if s is None:
            return None
        gain = max(0.0, float(self.masters.audio_reactivity))
        if gain == 1.0:
            return s
        return dataclasses.replace(
            s,
            low_norm=s.low_norm * gain,
            mid_norm=s.mid_norm * gain,
            high_norm=s.high_norm * gain,
        )

    # ---- main loop ----

    async def _loop(self) -> None:
        period = 1.0 / float(self.target_fps)
        t0 = time.perf_counter()
        next_tick = t0
        fps_window_start = t0
        fps_window_frames = 0
        last_wall = t0

        try:
            while not self._stop.is_set():
                now = time.perf_counter()
                wall_t = now - t0
                dt_wall = wall_t - last_wall
                last_wall = wall_t
                self.elapsed = wall_t

                # Advance effective_t by dt × speed; freeze short-circuits to 0.
                speed = 0.0 if self.masters.freeze else float(self.masters.speed)
                self.effective_t += dt_wall * speed

                ctx = RenderContext(
                    t=self.effective_t,
                    wall_t=wall_t,
                    audio=self._build_audio_view(),
                    masters=self.masters,
                )

                self.buffer.clear()
                self.mixer.render(ctx, self.buffer.rgb)
                if self.calibration is not None:
                    self._apply_calibration(wall_t)
                await self.transport.send_frame(self.buffer.to_uint8(self.gamma))

                self.frame_count += 1
                fps_window_frames += 1
                if now - fps_window_start >= 1.0:
                    self.fps = fps_window_frames / (now - fps_window_start)
                    fps_window_start = now
                    fps_window_frames = 0

                next_tick += period
                sleep = next_tick - time.perf_counter()
                if sleep > 0:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=sleep)
                        return  # stop requested
                    except TimeoutError:
                        pass
                else:
                    self.dropped_frames += 1
                    next_tick = time.perf_counter()
                    await asyncio.sleep(0)
        except Exception:
            log.exception("engine loop crashed")
            raise


# Re-export so callers keep `from ledctl.engine import UpdateLedsSpec` ergonomic.
__all__ = ["CalibrationState", "Engine", "UpdateLedsSpec"]
