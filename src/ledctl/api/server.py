from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from ..config import AppConfig
from ..effects.wave import WaveEffect, WaveParams
from ..engine import Engine
from ..topology import Topology
from ..transports.base import Transport
from ..transports.ddp import DDPTransport
from ..transports.multi import MultiTransport
from ..transports.simulator import SimulatorTransport

WEB_DIR = Path(__file__).resolve().parents[2] / "web"


def _build_transport(cfg: AppConfig, sim: SimulatorTransport) -> Transport:
    mode = cfg.transport.mode
    if mode == "simulator":
        return sim
    # Both ddp and multi need a controller endpoint.
    if not cfg.controllers:
        raise ValueError(f"transport mode {mode!r} requires at least one controller")
    ctrl = next(iter(cfg.controllers.values()))
    if mode == "ddp":
        return DDPTransport(ctrl.host, ctrl.port)
    if mode == "multi":
        return MultiTransport([sim, DDPTransport(ctrl.host, ctrl.port)])
    raise ValueError(f"unknown transport mode: {mode!r}")


def create_app(cfg: AppConfig) -> FastAPI:
    topology = Topology.from_config(cfg)
    sim = SimulatorTransport()
    transport = _build_transport(cfg, sim)
    effect = WaveEffect(WaveParams(), topology)
    engine = Engine(cfg, topology, transport, effect)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await engine.start()
        try:
            yield
        finally:
            await engine.stop()
            await transport.close()
            if transport is not sim:
                await sim.close()

    app = FastAPI(title="ledctl", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.topology = topology
    app.state.simulator = sim
    app.state.config = cfg

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/state")
    async def state() -> dict:
        return {
            "fps": round(engine.fps, 2),
            "target_fps": engine.target_fps,
            "frame_count": engine.frame_count,
            "dropped_frames": engine.dropped_frames,
            "transport_mode": cfg.transport.mode,
            "sim_clients": sim.client_count,
            "effect": effect.name,
        }

    @app.get("/topology")
    async def get_topology() -> dict:
        return {
            "pixel_count": topology.pixel_count,
            "bbox_min": topology.bbox_min.tolist(),
            "bbox_max": topology.bbox_max.tolist(),
            "leds": [
                {
                    "global_index": led.global_index,
                    "strip_id": led.strip_id,
                    "local_index": led.local_index,
                    "position": list(led.position),
                }
                for led in topology.leds
            ],
            "strips": [
                {
                    "id": s.id,
                    "controller": s.controller,
                    "output": s.output,
                    "pixel_offset": s.pixel_offset,
                    "pixel_count": s.pixel_count,
                    "start": list(s.geometry.start),
                    "end": list(s.geometry.end),
                    "reversed": s.reversed,
                }
                for s in topology.strips
            ],
        }

    ws_path = cfg.transport.sim.ws_path

    @app.websocket(ws_path)
    async def ws_frames(websocket: WebSocket) -> None:
        await websocket.accept()
        await sim.add_client(websocket)
        try:
            while True:
                # The simulator is server-push only. Block on receive so we
                # detect client disconnects via WebSocketDisconnect.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await sim.remove_client(websocket)

    return app
