"""Fixed-timestep render loop driving Runtime → SplitTransport.

Per tick:
  1. Compute wall_t / dt / effective_t (master-speed-scaled).
  2. Build an AudioView (already-scaled by masters.audio_reactivity).
  3. Runtime.render → (live_buf, sim_buf) float32.
  4. Encode each via PixelBuffer (gamma + uint8) and ship via SplitTransport.

Frames drop rather than spiral on lag — same as v1.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Literal

import numpy as np

from .audio.state import AudioState
from .config import AppConfig
from .masters import MasterControls
from .pixelbuffer import PixelBuffer
from .surface import AudioView, EffectStore, Runtime
from .topology import Topology
from .transports.split import SplitTransport

log = logging.getLogger(__name__)


_CAL_COLOR: tuple[float, float, float] = (1.0, 0.0, 0.0)


@dataclass
class CalibrationState:
    mode: Literal["solo", "walk"]
    color: tuple[float, float, float] = _CAL_COLOR
    indices: tuple[int, ...] = ()
    step: int = 100
    interval: float = 1.0
    start_t: float = 0.0
    current: int = 0


class Engine:
    """Fixed-timestep async render loop."""

    def __init__(
        self,
        cfg: AppConfig,
        topology: Topology,
        transport: SplitTransport,
        runtime: Runtime,
        store: EffectStore,
        masters: MasterControls | None = None,
    ):
        self.cfg = cfg
        self.topology = topology
        self.transport = transport
        self.runtime = runtime
        self.store = store
        self.masters: MasterControls = masters or MasterControls()
        # Runtime keeps its own ref to masters so its master output stage stays in sync.
        self.runtime.masters = self.masters
        self.target_fps = cfg.project.target_fps
        self.gamma = cfg.output.gamma
        self.live_pb = PixelBuffer(topology.pixel_count)
        self.sim_pb = PixelBuffer(topology.pixel_count)
        self.fps: float = 0.0
        self.frame_count: int = 0
        self.dropped_frames: int = 0
        self.elapsed: float = 0.0
        self.effective_t: float = 0.0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.audio_state: AudioState | None = None
        self._audio_kick: asyncio.Event | None = None
        self._asyncio_loop: asyncio.AbstractEventLoop | None = None

    # ---- audio plumbing ---- #

    def attach_audio(self, state: AudioState | None) -> None:
        self.audio_state = state

    def kick_audio(self) -> None:
        loop = self._asyncio_loop
        ev = self._audio_kick
        if loop is None or ev is None:
            return
        loop.call_soon_threadsafe(ev.set)

    # ---- runtime fps ---- #

    # Snap values exposed in the operator UI. Keep the list in sync with the
    # `<input type="range">` mapping in main-desktop.js. Validating against
    # an explicit list (vs a free range) means an arbitrary PATCH /engine/fps
    # can't land the engine at a value the UI can't display.
    ALLOWED_TARGET_FPS: tuple[int, ...] = (24, 30, 40, 60, 90)

    def set_target_fps(self, fps: int) -> int:
        """Live-tunable LED leg rate. The render loop re-reads `target_fps`
        every tick, so the new value takes effect on the next iteration."""
        v = int(fps)
        if v not in self.ALLOWED_TARGET_FPS:
            raise ValueError(
                f"engine target_fps must be one of {list(self.ALLOWED_TARGET_FPS)}, got {fps}"
            )
        self.target_fps = v
        return self.target_fps

    # ---- masters ---- #

    def set_masters(self, **patch: object) -> MasterControls:
        self.masters = self.masters.merge(**patch)
        self.runtime.masters = self.masters
        return self.masters

    # ---- calibration ---- #

    def set_calibration_solo(self, indices: list[int]) -> CalibrationState:
        n = self.topology.pixel_count
        clean = tuple(sorted({int(i) for i in indices if 0 <= int(i) < n}))
        if not clean:
            raise ValueError(f"no valid global_index in {list(indices)} for {n} pixels")
        cal = CalibrationState(mode="solo", indices=clean, current=clean[0])
        self.runtime.calibration = cal
        return cal

    def set_calibration_walk(self, step: int, interval: float) -> CalibrationState:
        if step <= 0:
            raise ValueError(f"step must be > 0, got {step}")
        if interval <= 0:
            raise ValueError(f"interval must be > 0, got {interval}")
        cal = CalibrationState(
            mode="walk", step=int(step), interval=float(interval),
            start_t=self.elapsed, current=0,
        )
        self.runtime.calibration = cal
        return cal

    def clear_calibration(self) -> None:
        self.runtime.calibration = None

    def calibration_summary(self) -> dict | None:
        cal = self.runtime.calibration
        if cal is None:
            return None
        if cal.mode == "solo":
            return {"mode": "solo", "indices": list(cal.indices), "current": cal.current}
        return {
            "mode": "walk", "step": cal.step,
            "interval": cal.interval, "current": cal.current,
        }

    # ---- topology hot-swap ---- #

    def swap_topology(self, new_topology: Topology) -> None:
        self.topology = new_topology
        self.live_pb = PixelBuffer(new_topology.pixel_count)
        self.sim_pb = PixelBuffer(new_topology.pixel_count)
        self.runtime.swap_topology(new_topology)

    # ---- audio view (apply masters.audio_reactivity once per tick) ---- #

    def _build_audio_view(self) -> AudioView:
        s = self.audio_state
        if s is None:
            return AudioView(connected=False)
        gain = max(0.0, float(self.masters.audio_reactivity))
        # Latch beat as new-since-last-render delta.
        beats_seen = getattr(self, "_last_beats", 0)
        beats_now = int(getattr(s, "beat_count", 0))
        delta = max(0, beats_now - beats_seen)
        self._last_beats = beats_now
        # Beat is a per-frame intensity in [0, 1]:
        #   - 0.0 on frames with no fresh onset
        #   - on an onset, set to min(1.0, audio_reactivity) so beat-driven
        #     flashes scale linearly with the master, without ever exceeding 1.
        # This gives effects a clean "beat" multiplier they can compose with
        # their own per-effect intensity params (e.g. `flash = ctx.audio.beat
        # * p.kick_amount`), and 0 reactivity silences beat-driven content.
        beat_out = float(min(1.0, gain)) if delta > 0 else 0.0
        return AudioView(
            low=float(s.low) * gain,
            mid=float(s.mid) * gain,
            high=float(s.high) * gain,
            beat=beat_out,
            beats_since_start=beats_now,
            bpm=float(s.bpm) if s.bpm is not None else 120.0,
            connected=bool(s.connected),
        )

    # ---- main loop ---- #

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._asyncio_loop = asyncio.get_running_loop()
        self._audio_kick = asyncio.Event()
        self._last_beats = (
            int(self.audio_state.beat_count) if self.audio_state is not None else 0
        )
        self._task = asyncio.create_task(self._loop(), name="ledctl-engine")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        # `target_fps` is re-read each tick so the operator can adjust the LED
        # leg's rate live (slider in the UI / PATCH /engine/fps) without
        # restarting the loop.
        t0 = time.perf_counter()
        fps_window_start = t0
        fps_window_frames = 0
        last_wall = t0
        last_render = t0 - 1.0 / float(max(1, int(self.target_fps)))
        kick = self._audio_kick

        try:
            while not self._stop.is_set():
                period = 1.0 / float(max(1, int(self.target_fps)))
                kick_min_interval = period * 0.5
                deadline = last_render + period
                while True:
                    now = time.perf_counter()
                    remaining = deadline - now
                    if remaining <= 0:
                        break
                    if kick is None:
                        try:
                            await asyncio.wait_for(self._stop.wait(), timeout=remaining)
                            return
                        except TimeoutError:
                            break
                    wait_kick = asyncio.create_task(kick.wait())
                    wait_stop = asyncio.create_task(self._stop.wait())
                    done, pending = await asyncio.wait(
                        {wait_kick, wait_stop},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    if wait_stop in done:
                        return
                    if wait_kick in done:
                        kick.clear()
                        if time.perf_counter() - last_render >= kick_min_interval:
                            break
                        continue
                    break

                now = time.perf_counter()
                wall_t = now - t0
                dt_wall = wall_t - last_wall
                # Clamp dt at 2× target frame interval so a hiccup (DDP retransmit,
                # GC pause) doesn't tele-port stateful effects (comet heads,
                # ripple ages). Stateful effects integrate dt; spikes look like
                # jumps to the dance floor.
                dt_clamp = 2.0 * period
                if dt_wall > dt_clamp:
                    dt_wall = dt_clamp
                last_wall = wall_t
                self.elapsed = wall_t

                # Master speed scales BOTH the effective time-axis used by
                # effects (`ctx.t`) and the per-frame `ctx.dt` they integrate
                # against — without scaling dt too, effects that integrate
                # state by hand (`head += speed_param * ctx.dt`) ignore the
                # master speed entirely. wall_t stays unscaled so crossfades
                # complete in real-world seconds.
                speed = float(self.masters.speed)
                dt_scaled = dt_wall * speed
                self.effective_t += dt_scaled

                audio_view = self._build_audio_view()

                # The sim transport throttles to its own `target_fps` (UI
                # rate, default 24 Hz) independently of the engine tick. When
                # the UI frame isn't due — or no browser is connected, or sim
                # is paused — we skip the preview render AND the sim encode
                # so the Pi spends every cycle on the LIVE composition + DDP.
                sim_active = self.transport.sim.should_send_now()
                live_rgb, sim_rgb = self.runtime.render(
                    wall_t=wall_t,
                    dt=dt_scaled,
                    t_eff=self.effective_t,
                    audio=audio_view,
                    render_preview=sim_active,
                )

                # Encode + send.
                np.copyto(self.live_pb.rgb, live_rgb)
                live_bytes = self.live_pb.to_uint8(self.gamma)
                if not sim_active:
                    sim_bytes = None
                elif sim_rgb is live_rgb:
                    sim_bytes = live_bytes
                else:
                    np.copyto(self.sim_pb.rgb, sim_rgb)
                    sim_bytes = self.sim_pb.to_uint8(self.gamma)
                await self.transport.send(led_frame=live_bytes, sim_frame=sim_bytes)

                last_render = time.perf_counter()
                if kick is not None:
                    kick.clear()

                self.frame_count += 1
                fps_window_frames += 1
                if now - fps_window_start >= 1.0:
                    self.fps = fps_window_frames / (now - fps_window_start)
                    fps_window_start = now
                    fps_window_frames = 0
                if last_render - deadline > period:
                    self.dropped_frames += 1
        except Exception:
            log.exception("engine loop crashed")
            raise


__all__ = ["CalibrationState", "Engine"]
