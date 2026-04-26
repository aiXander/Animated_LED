import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from .audio.state import AudioState
from .config import AppConfig
from .effects.base import Effect, EffectParams
from .effects.registry import get_effect_class
from .mixer import BLEND_MODES, Layer, Mixer
from .pixelbuffer import PixelBuffer
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
    one index every `interval` seconds, useful during physical install to
    confirm wiring matches `config.yaml`. Either mode bypasses the mixer.
    """

    mode: Literal["solo", "walk"]
    color: tuple[float, float, float] = _CAL_COLOR
    indices: tuple[int, ...] = ()           # solo
    step: int = 100                         # walk
    interval: float = 1.0                   # walk
    start_t: float = 0.0                    # walk
    current: int = 0                        # walk: index lit right now


def _build_effect(name: str, params: dict[str, Any], topology: Topology) -> Effect:
    cls = get_effect_class(name)
    params_obj: EffectParams = cls.Params(**(params or {}))
    return cls(params_obj, topology)


def _validate_blend(blend: str) -> str:
    if blend not in BLEND_MODES:
        raise ValueError(f"unknown blend mode {blend!r}; must be one of {BLEND_MODES}")
    return blend


class Engine:
    """Fixed-timestep render loop.

    Targets `target_fps` using `time.perf_counter`. If the encode/transport
    falls behind, drop the schedule forward rather than spiral-of-death
    catching up — better to skip a frame than queue them.
    """

    def __init__(
        self,
        cfg: AppConfig,
        topology: Topology,
        transport: Transport,
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
        self.elapsed: float = 0.0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.calibration: CalibrationState | None = None
        self.audio_state: AudioState | None = None

    def attach_audio(self, state: AudioState | None) -> None:
        """Make an AudioState visible to audio-reactive effects via topology.

        Stored on the engine so it survives `swap_topology` (the new topology
        inherits the same reference).
        """
        self.audio_state = state
        self.topology.audio_state = state

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
        name: str,
        params: dict[str, Any] | None = None,
        blend: str = "normal",
        opacity: float = 1.0,
    ) -> int:
        _validate_blend(blend)
        effect = _build_effect(name, params or {}, self.topology)
        self.mixer.layers.append(Layer(effect=effect, blend=blend, opacity=float(opacity)))
        return len(self.mixer.layers) - 1

    def update_layer(
        self,
        i: int,
        params: dict[str, Any] | None = None,
        blend: str | None = None,
        opacity: float | None = None,
    ) -> None:
        layer = self.mixer.layers[i]
        if blend is not None:
            layer.blend = _validate_blend(blend)
        if opacity is not None:
            layer.opacity = float(opacity)
        if params is not None:
            cls = type(layer.effect)
            current = layer.effect.params.model_dump()
            merged = {**current, **params}
            new_params = cls.Params(**merged)
            layer.effect = cls(new_params, self.topology)

    def remove_layer(self, i: int) -> None:
        self.mixer.layers.pop(i)

    def crossfade_to(
        self,
        specs: list[dict[str, Any]],
        duration: float,
    ) -> None:
        new_layers: list[Layer] = []
        for spec in specs:
            effect = _build_effect(spec["effect"], spec.get("params") or {}, self.topology)
            blend = _validate_blend(spec.get("blend", "normal"))
            opacity = float(spec.get("opacity", 1.0))
            new_layers.append(Layer(effect=effect, blend=blend, opacity=opacity))
        self.mixer.crossfade_to(new_layers, duration, self.elapsed)

    def layer_state(self) -> list[dict[str, Any]]:
        return [
            {
                "effect": layer.effect.name,
                "blend": layer.blend,
                "opacity": layer.opacity,
                "params": layer.effect.params.model_dump(),
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

        Effects close over per-LED arrays (e.g. normalised positions), so they
        must be re-instantiated against the new topology — even if pixel_count
        is unchanged. Layer specs (effect name, params, blend, opacity) survive.
        """
        specs = self.layer_state()
        n_old = self.topology.pixel_count
        self.topology = new_topology
        # Carry the live audio reference across swaps so audio-reactive effects
        # don't go silent after a layout edit.
        self.topology.audio_state = self.audio_state
        if new_topology.pixel_count != n_old:
            self.buffer = PixelBuffer(new_topology.pixel_count)
            self.mixer = Mixer(new_topology.pixel_count)
        else:
            # Keep the mixer (its scratch buffers are still the right size) but
            # drop layers — they're bound to the old topology.
            self.mixer.layers.clear()
        for spec in specs:
            self.push_layer(spec["effect"], spec["params"], spec["blend"], spec["opacity"])
        # Calibration state references global indices; clear if any are now out of range.
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
        # walk: one index lit, advancing every `interval` seconds.
        steps = int(max(0.0, t - cal.start_t) / cal.interval)
        idx = (steps * cal.step) % n
        cal.current = idx
        rgb[idx] = cal.color

    # ---- main loop ----

    async def _loop(self) -> None:
        period = 1.0 / float(self.target_fps)
        t0 = time.perf_counter()
        next_tick = t0
        fps_window_start = t0
        fps_window_frames = 0

        try:
            while not self._stop.is_set():
                now = time.perf_counter()
                t = now - t0
                self.elapsed = t

                self.buffer.clear()
                self.mixer.render(t, self.buffer.rgb)
                # Calibration override (if active) replaces the rendered frame
                # with a single-LED-red pattern. Applied after mixer so it wins.
                if self.calibration is not None:
                    self._apply_calibration(t)
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
                    # Fell behind; resync schedule and count it.
                    self.dropped_frames += 1
                    next_tick = time.perf_counter()
                    # Yield so we don't starve other tasks during a long stall.
                    await asyncio.sleep(0)
        except Exception:
            log.exception("engine loop crashed")
            raise
