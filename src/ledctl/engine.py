import asyncio
import contextlib
import logging
import time

from .config import AppConfig
from .effects.base import Effect
from .pixelbuffer import PixelBuffer
from .topology import Topology
from .transports.base import Transport

log = logging.getLogger(__name__)


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
        effect: Effect,
    ):
        self.cfg = cfg
        self.topology = topology
        self.transport = transport
        self.effect = effect
        self.buffer = PixelBuffer(topology.pixel_count)
        self.target_fps = cfg.project.target_fps
        self.fps: float = 0.0
        self.frame_count: int = 0
        self.dropped_frames: int = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

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

                self.buffer.clear()
                self.effect.render(t, self.buffer.rgb)
                await self.transport.send_frame(self.buffer.to_uint8())

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
