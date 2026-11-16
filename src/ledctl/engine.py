"""Fixed-timestep render loop driving Runtime → SplitTransport.

Per tick:
  1. Compute wall_t / dt / effective_t (master-speed-scaled).
  2. Build an AudioView (already-scaled by masters.audio_reactivity).
  3. Runtime.render → (live_buf, sim_buf) float32.
  4. Encode each via PixelBuffer (gamma + uint8) and ship via SplitTransport.

Frames drop rather than spiral on lag — same as v1.

A tick never awaits anything: DDP is a synchronous UDP send and the browser
preview is handed to its own sender, so the only thing that can delay a frame
is other code holding the event loop. `FrameStats` measures exactly that
(`late_ms`), next to the render and output cost, and `/state` reports it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
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


class FrameStats:
    """Timings of the last ~4 s of frames (at 60 fps), in milliseconds.

      render_ms  Runtime.render: effects, blending, master output stage
      out_ms     gamma/uint8 encode + DDP send + handing the preview to its sender
      late_ms    how long after its deadline a frame started = time something
                 else held the event loop. This is what a dropped frame looks like.
    """

    SIZE = 256
    # A frame starting this late is a visible hitch; counted and logged.
    STALL_MS = 50.0

    def __init__(self) -> None:
        self._render = np.zeros(self.SIZE, dtype=np.float32)
        self._out = np.zeros(self.SIZE, dtype=np.float32)
        self._late = np.zeros(self.SIZE, dtype=np.float32)
        self._i = 0
        self._n = 0
        self.stalls: int = 0             # frames that started > STALL_MS late, since boot
        self.worst_late_ms: float = 0.0  # since boot
        self._log_at: float = 0.0
        self._log_stalls: int = 0
        self._log_worst: float = 0.0

    def add(self, render_ms: float, out_ms: float, late_ms: float, now: float) -> None:
        i = self._i
        self._render[i] = render_ms
        self._out[i] = out_ms
        self._late[i] = late_ms
        self._i = (i + 1) % self.SIZE
        if self._n < self.SIZE:
            self._n += 1
        if late_ms > self.worst_late_ms:
            self.worst_late_ms = late_ms
        if late_ms > self.STALL_MS:
            self.stalls += 1
            self._log_stalls += 1
            self._log_worst = max(self._log_worst, late_ms)
        # At most one line every 10 s, so a bad night doesn't flood the journal.
        if self._log_stalls and now - self._log_at >= 10.0:
            log.warning(
                "render loop held up %d time(s) in the last 10 s (worst %.0f ms): "
                "something else ran on the event loop",
                self._log_stalls, self._log_worst,
            )
            self._log_at = now
            self._log_stalls = 0
            self._log_worst = 0.0

    def snapshot(self) -> dict[str, float | int]:
        n = self._n
        if n == 0:
            return {"frames": 0}

        def stat(buf: np.ndarray) -> tuple[float, float, float]:
            w = np.sort(buf[:n])
            return (
                round(float(w[n // 2]), 2),
                round(float(w[min(n - 1, int(0.95 * n))]), 2),
                round(float(w[-1]), 2),
            )

        r50, r95, rmax = stat(self._render)
        o50, o95, omax = stat(self._out)
        l50, l95, lmax = stat(self._late)
        return {
            "frames": n,
            "render_ms_p50": r50, "render_ms_p95": r95, "render_ms_max": rmax,
            "out_ms_p50": o50, "out_ms_p95": o95, "out_ms_max": omax,
            "late_ms_p50": l50, "late_ms_p95": l95, "late_ms_max": lmax,
            "stalls": self.stalls,
            "worst_late_ms": round(self.worst_late_ms, 1),
        }


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
        self.stats = FrameStats()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.audio_state: AudioState | None = None
        self._asyncio_loop: asyncio.AbstractEventLoop | None = None
        # The future the loop is sleeping on between frames (None while a
        # frame renders). An onset resolves it early.
        self._wake: asyncio.Future | None = None
        # Audio swell: a follower of /audio/level, and how much of the swell
        # is applied (fades out over a second when the audio feed dies).
        self._swell_level: float | None = None
        self._swell_presence: float = 0.0

    # ---- audio plumbing ---- #

    def attach_audio(self, state: AudioState | None) -> None:
        self.audio_state = state

    def kick_audio(self) -> None:
        """Thread-safe: ask for a frame now (an onset arrived)."""
        loop = self._asyncio_loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._on_kick)

    def _on_kick(self) -> None:
        # An onset that lands while a frame is rendering is not lost: the next
        # frame reads it from the beat counter.
        fut = self._wake
        if fut is not None and not fut.done():
            fut.set_result(True)

    async def _sleep(self, seconds: float) -> bool:
        """Sleep up to `seconds`. True = an onset woke us early."""
        loop = self._asyncio_loop
        fut = loop.create_future()
        self._wake = fut
        handle = loop.call_later(seconds, _resolve_timer, fut)
        try:
            return await fut
        finally:
            handle.cancel()
            self._wake = None

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

    # ---- audio swell (LED brightness follows loudness) ---- #

    def _update_swell(self, dt: float) -> float:
        """Advance the level follower and return the output gain for this tick.

            x    = clip((L - quiet) / (loud - quiet), 0, 1)
            gain = 1 - audio_swell * (1 - x)

        Dead audio must not black out the room, so the dim fades out over a
        second while the feed is stale. That is keyed on staleness
        (`connected`, cleared by the bridge watchdog), never on the value: a
        stalled server sends one last 0, and real silence is also 0 and must
        dim. Between that last 0 and the watchdog's timeout the lights dip
        partway; that is accepted.
        """
        s = self.audio_state
        cfg = self.cfg.audio_swell
        live = s is not None and s.connected and s.level is not None
        if live:
            level = float(s.level)
            if self._swell_level is None:
                self._swell_level = level
            else:
                tau = cfg.attack_s if level > self._swell_level else cfg.release_s
                a = 1.0 if tau <= 0.0 else 1.0 - math.exp(-dt / tau)
                self._swell_level += a * (level - self._swell_level)
        step = dt if live else -dt          # presence moves 0 <-> 1 over a second
        self._swell_presence = min(1.0, max(0.0, self._swell_presence + step))
        amount = float(self.masters.audio_swell) * self._swell_presence
        if amount <= 0.0 or self._swell_level is None:
            return 1.0
        x = (self._swell_level - cfg.quiet) / (cfg.loud - cfg.quiet)
        return 1.0 - amount * (1.0 - min(1.0, max(0.0, x)))

    # ---- main loop ---- #

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._asyncio_loop = asyncio.get_running_loop()
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
        # On the same clock as `wall_t` (seconds since t0), not the absolute
        # perf_counter: `last_wall = t0` made the first dt minus the host's
        # uptime, and started `effective_t` (ctx.t) there as well.
        last_wall = 0.0
        last_render = t0 - 1.0 / float(max(1, int(self.target_fps)))
        # The frame clock: each frame is due one period after the previous
        # frame was DUE, not after it finished, so render + send time does not
        # stretch the interval (it used to: 53 fps at a 60 fps target).
        deadline = t0
        runtime = self.runtime
        transport = self.transport
        stats = self.stats

        try:
            while not self._stop.is_set():
                period = 1.0 / float(max(1, int(self.target_fps)))
                kick_min_interval = period * 0.5
                kicked = False
                slept = False
                while True:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        break
                    slept = True
                    if (
                        await self._sleep(remaining)
                        and time.perf_counter() - last_render >= kick_min_interval
                    ):
                        kicked = True
                        break
                if not slept:
                    # Behind schedule: still give HTTP and WebSocket tasks a turn.
                    await asyncio.sleep(0)

                now = time.perf_counter()
                late = 0.0 if kicked else max(0.0, now - deadline)
                # An onset rendered early re-phases the clock from here; a frame
                # that is more than a period late does too, instead of bursting
                # to catch up (the slots it skipped count as dropped).
                if kicked or late <= period:
                    due = now if kicked else deadline
                else:
                    due = now
                    self.dropped_frames += int(late / period)
                deadline = due + period
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
                runtime.swell_gain = self._update_swell(dt_wall)

                # The sim transport throttles to its own `target_fps` (UI
                # rate, default 24 Hz) independently of the engine tick. When
                # the UI frame isn't due — or no browser is connected, or sim
                # is paused — we skip the preview render AND the sim encode
                # so the Pi spends every cycle on the LIVE composition + DDP.
                sim_active = transport.sim.should_send_now()
                live_rgb, sim_rgb = runtime.render(
                    wall_t=wall_t,
                    dt=dt_scaled,
                    t_eff=self.effective_t,
                    audio=audio_view,
                    render_preview=sim_active,
                )
                t_rendered = time.perf_counter()

                # Encode + send. Nothing here waits on a network peer.
                live_bytes = self.live_pb.encode(live_rgb, self.gamma)
                if not sim_active:
                    sim_bytes = None
                elif sim_rgb is live_rgb:
                    sim_bytes = live_bytes
                else:
                    sim_bytes = self.sim_pb.encode(sim_rgb, self.gamma)
                transport.send_now(led_frame=live_bytes, sim_frame=sim_bytes)

                last_render = time.perf_counter()
                stats.add(
                    (t_rendered - now) * 1000.0,
                    (last_render - t_rendered) * 1000.0,
                    late * 1000.0,
                    last_render,
                )

                self.frame_count += 1
                fps_window_frames += 1
                if now - fps_window_start >= 1.0:
                    self.fps = fps_window_frames / (now - fps_window_start)
                    fps_window_start = now
                    fps_window_frames = 0
                if last_render - due > period:
                    self.dropped_frames += 1
        except Exception:
            log.exception("engine loop crashed")
            raise


def _resolve_timer(fut: asyncio.Future) -> None:
    if not fut.done():
        fut.set_result(False)


__all__ = ["CalibrationState", "Engine", "FrameStats"]
