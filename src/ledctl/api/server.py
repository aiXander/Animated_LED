import asyncio
import contextlib
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..audio import AudioCapture, list_input_devices
from ..config import AppConfig, AudioConfig, StripConfig
from ..effects.registry import list_effects
from ..engine import Engine
from ..mixer import BLEND_MODES
from ..presets import list_presets, load_preset
from ..topology import Topology
from ..transports.base import Transport
from ..transports.ddp import DDPTransport
from ..transports.multi import MultiTransport
from ..transports.simulator import SimulatorTransport

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
DEFAULT_PRESETS_DIR = Path(__file__).resolve().parents[3] / "config" / "presets"


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


# ---- request bodies ----


class PushEffectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    params: dict[str, Any] = Field(default_factory=dict)
    blend: str = "normal"
    opacity: float = Field(1.0, ge=0.0, le=1.0)


class PatchLayerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    params: dict[str, Any] | None = None
    blend: str | None = None
    opacity: float | None = Field(None, ge=0.0, le=1.0)


class ApplyPresetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    crossfade_seconds: float | None = Field(
        None, ge=0.0, description="Override the preset's own crossfade duration"
    )


class CalibrationSoloRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indices: list[int] = Field(
        ..., min_length=1, description="Global LED indices to light in red"
    )


class CalibrationWalkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: int = Field(100, gt=0, description="Increment between lit indices")
    interval: float = Field(1.0, gt=0.0, description="Seconds between advances")


class UpdateLayoutRequest(BaseModel):
    """Editor save: replace the strips list, keeping all other config sections."""

    model_config = ConfigDict(extra="forbid")
    strips: list[StripConfig] = Field(..., min_length=1)


class AudioSelectRequest(BaseModel):
    """`device` is an index, a (sub)string of the device name, or null for default."""

    model_config = ConfigDict(extra="forbid")
    device: str | int | None = Field(
        None, description="Device index, name (substring ok), or null for system default"
    )
    persist: bool = Field(
        True, description="If true and a config_path is known, write the new device to YAML"
    )


def _strips_to_yaml_dicts(strips: list[StripConfig]) -> list[dict[str, Any]]:
    """Plain-Python projection of strips for yaml.safe_dump (no pydantic types)."""
    out: list[dict[str, Any]] = []
    for s in strips:
        out.append(
            {
                "id": s.id,
                "controller": s.controller,
                "output": s.output,
                "pixel_offset": s.pixel_offset,
                "pixel_count": s.pixel_count,
                "leds_per_meter": s.leds_per_meter,
                "geometry": {
                    "type": s.geometry.type,
                    "start": list(s.geometry.start),
                    "end": list(s.geometry.end),
                },
                "reversed": s.reversed,
            }
        )
    return out


def _write_config_yaml(path: Path, cfg: AppConfig) -> None:
    """Atomically rewrite the config YAML, keeping a .bak of the prior version.

    Comments in the original file are lost — pyyaml has no round-trip preservation.
    Acceptable for v1 of the editor; revisit with ruamel.yaml if comments matter.
    """
    payload = yaml.safe_dump(
        _config_to_yaml_dict(cfg), sort_keys=False, default_flow_style=False
    )
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_bytes(path.read_bytes())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    tmp.replace(path)


def _config_to_yaml_dict(cfg: AppConfig) -> dict[str, Any]:
    """Serialise the full config back to a plain dict in stable section order."""
    return {
        "project": cfg.project.model_dump(),
        "server": cfg.server.model_dump(),
        "controllers": {k: v.model_dump() for k, v in cfg.controllers.items()},
        "strips": _strips_to_yaml_dicts(list(cfg.strips)),
        "transport": cfg.transport.model_dump(),
        "output": cfg.output.model_dump(),
        "audio": cfg.audio.model_dump(),
        "agent": cfg.agent.model_dump(),
    }


def _build_capture(audio_cfg: AudioConfig) -> AudioCapture:
    return AudioCapture(
        device=audio_cfg.device,
        samplerate=audio_cfg.samplerate,
        blocksize=audio_cfg.blocksize,
        fft_window=audio_cfg.fft_window,
        channels=audio_cfg.channels,
        gain=audio_cfg.gain,
    )


def create_app(
    cfg: AppConfig,
    presets_dir: Path | None = None,
    config_path: Path | None = None,
) -> FastAPI:
    topology = Topology.from_config(cfg)
    sim = SimulatorTransport()
    transport = _build_transport(cfg, sim)
    engine = Engine(cfg, topology, transport)
    presets_path = presets_dir or DEFAULT_PRESETS_DIR

    # Default startup: a single `scroll` layer.
    # — orange/yellow/red palette, scrolling stage-right (positive x speed)
    # — top row leads the bottom row by 0.075 cycles via cross_phase
    # — brightness bound to audio RMS, swinging in [0.5, 1.0]. The audio
    #   source is auto-scaled by the rolling-window normalizer in capture,
    #   so the natural dynamic range of the room maps to the full [0, 1]
    #   binding input — no per-install gain tuning needed.
    # The whole show is one API call; configure further over the API.
    engine.push_layer(
        "scroll",
        {
            "axis": "x",
            "speed": 0.15,
            "wavelength": 1.5,
            "shape": "cosine",
            "softness": 1.0,
            "cross_phase": [0.0, 0.075, 0.0],
            "palette": {
                "stops": [
                    {"pos": 0.0, "color": "#ff2000"},
                    {"pos": 0.4, "color": "#ff8000"},
                    {"pos": 0.7, "color": "#ffdd00"},
                    {"pos": 1.0, "color": "#ffffff"},
                ],
            },
            "bindings": {
                "brightness": {
                    "source": "audio.rms",
                    "floor": 0.5,
                    "ceiling": 1.0,
                },
            },
        },
    )

    audio: AudioCapture = _build_capture(cfg.audio)
    if cfg.audio.enabled:
        audio.start()
    engine.attach_audio(audio.state)

    # State websocket clients. Mutated only from the event loop, so a plain
    # set + lock-free copy is fine.
    state_clients: set[WebSocket] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await engine.start()
        broadcaster = asyncio.create_task(
            _state_broadcaster(state_clients, _full_state_payload, engine.target_fps),
            name="ledctl-state-broadcaster",
        )
        try:
            yield
        finally:
            broadcaster.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await broadcaster
            await engine.stop()
            cap: AudioCapture | None = app.state.audio
            if cap is not None:
                cap.stop()
            await transport.close()
            if transport is not sim:
                await sim.close()

    app = FastAPI(title="ledctl", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.topology = topology
    app.state.simulator = sim
    app.state.config = cfg
    app.state.presets_dir = presets_path
    app.state.config_path = config_path
    app.state.audio = audio

    # Phase 6 — language-driven control panel. Always installed (state +
    # routes), but `/agent/chat` no-ops with 503 if `agent.enabled` is false
    # or the API key env is unset. Render loop is unaffected either way.
    from .agent import install_agent_routes

    install_agent_routes(app, cfg.agent, presets_path)

    def current_topology() -> Topology:
        # Topology can be hot-swapped via PUT /config; always read it off the engine.
        return engine.topology

    # ---- static / topology ----

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/editor")
    async def editor() -> FileResponse:
        return FileResponse(WEB_DIR / "editor.html")

    @app.get("/audio")
    async def audio_page() -> FileResponse:
        return FileResponse(WEB_DIR / "audio.html")

    def _audio_state_payload() -> dict[str, Any]:
        cap: AudioCapture | None = app.state.audio
        if cap is None:
            return {"enabled": False, "error": "audio not initialised"}
        s = cap.state
        return {
            "enabled": s.enabled,
            "device": s.device_name,
            "configured_device": app.state.config.audio.device,
            "samplerate": s.samplerate,
            "blocksize": s.blocksize,
            "fft_window": s.fft_window,
            "channels": s.channels,
            "block_count": s.block_count,
            "error": s.error,
            "rms": round(s.rms, 5),
            "peak": round(s.peak, 5),
            "low": round(s.low, 5),
            "mid": round(s.mid, 5),
            "high": round(s.high, 5),
        }

    def _full_state_payload() -> dict:
        return {
            "fps": round(engine.fps, 2),
            "target_fps": engine.target_fps,
            "frame_count": engine.frame_count,
            "dropped_frames": engine.dropped_frames,
            "elapsed": round(engine.elapsed, 3),
            "transport_mode": app.state.config.transport.mode,
            "sim_clients": sim.client_count,
            "blackout": engine.mixer.blackout,
            "crossfading": engine.mixer.is_crossfading,
            "calibration": engine.calibration_summary(),
            "layers": engine.layer_state(),
            "gamma": engine.gamma,
            "audio": _audio_state_payload(),
        }

    @app.get("/state")
    async def state() -> dict:
        return _full_state_payload()

    @app.get("/topology")
    async def get_topology() -> dict:
        topo = current_topology()
        return {
            "pixel_count": topo.pixel_count,
            "bbox_min": topo.bbox_min.tolist(),
            "bbox_max": topo.bbox_max.tolist(),
            "leds": [
                {
                    "global_index": led.global_index,
                    "strip_id": led.strip_id,
                    "local_index": led.local_index,
                    "position": list(led.position),
                }
                for led in topo.leds
            ],
            "strips": [
                {
                    "id": s.id,
                    "controller": s.controller,
                    "output": s.output,
                    "pixel_offset": s.pixel_offset,
                    "pixel_count": s.pixel_count,
                    "leds_per_meter": s.leds_per_meter,
                    "start": list(s.geometry.start),
                    "end": list(s.geometry.end),
                    "reversed": s.reversed,
                }
                for s in topo.strips
            ],
        }

    # ---- effects ----

    @app.get("/effects")
    async def get_effects() -> dict:
        return {
            name: {
                "name": name,
                "params_schema": cls.Params.model_json_schema(),
            }
            for name, cls in list_effects().items()
        }

    @app.post("/effects/{name}", status_code=201)
    async def post_effect(name: str, body: PushEffectRequest | None = None) -> dict:
        if name not in list_effects():
            raise HTTPException(status_code=404, detail=f"unknown effect: {name!r}")
        body = body or PushEffectRequest()
        if body.blend not in BLEND_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown blend {body.blend!r}; must be one of {list(BLEND_MODES)}",
            )
        try:
            i = engine.push_layer(name, body.params, body.blend, body.opacity)
        except ValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=e.errors(include_url=False, include_context=False),
            ) from e
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {"layer_index": i, "layers": engine.layer_state()}

    # ---- layers ----

    @app.patch("/layer/{i}")
    async def patch_layer(i: int, body: PatchLayerRequest) -> dict:
        if i < 0 or i >= len(engine.mixer.layers):
            raise HTTPException(status_code=404, detail=f"no layer at index {i}")
        if body.blend is not None and body.blend not in BLEND_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown blend {body.blend!r}; must be one of {list(BLEND_MODES)}",
            )
        try:
            engine.update_layer(i, body.params, body.blend, body.opacity)
        except ValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=e.errors(include_url=False, include_context=False),
            ) from e
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {"layer_index": i, "layers": engine.layer_state()}

    @app.delete("/layer/{i}")
    async def delete_layer(i: int) -> dict:
        if i < 0 or i >= len(engine.mixer.layers):
            raise HTTPException(status_code=404, detail=f"no layer at index {i}")
        engine.remove_layer(i)
        return {"layers": engine.layer_state()}

    # ---- blackout ----

    @app.post("/blackout")
    async def post_blackout() -> dict:
        engine.mixer.blackout = True
        return {"blackout": True}

    @app.post("/resume")
    async def post_resume() -> dict:
        engine.mixer.blackout = False
        return {"blackout": False}

    # ---- presets ----

    @app.get("/presets")
    async def get_presets() -> dict:
        return {"presets": list_presets(presets_path)}

    @app.post("/presets/{name}")
    async def post_preset(name: str, body: ApplyPresetRequest | None = None) -> dict:
        try:
            preset = load_preset(name, presets_path)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except (ValidationError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        body = body or ApplyPresetRequest()
        duration = (
            body.crossfade_seconds
            if body.crossfade_seconds is not None
            else preset.crossfade_seconds
        )
        specs = [
            {
                "effect": layer.effect,
                "params": layer.params,
                "blend": layer.blend,
                "opacity": layer.opacity,
            }
            for layer in preset.layers
        ]
        try:
            engine.crossfade_to(specs, duration)
        except ValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=e.errors(include_url=False, include_context=False),
            ) from e
        except (TypeError, ValueError, KeyError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {
            "applied": preset.name or name,
            "crossfade_seconds": duration,
            "layers": engine.layer_state(),
        }

    # ---- calibration (Phase 4) ----

    @app.post("/calibration/solo")
    async def post_calibration_solo(body: CalibrationSoloRequest) -> dict:
        try:
            cal = engine.set_calibration_solo(body.indices)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {"calibration": engine.calibration_summary(), "lit": list(cal.indices)}

    @app.post("/calibration/walk")
    async def post_calibration_walk(body: CalibrationWalkRequest | None = None) -> dict:
        body = body or CalibrationWalkRequest()
        try:
            engine.set_calibration_walk(body.step, body.interval)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {"calibration": engine.calibration_summary()}

    @app.post("/calibration/stop")
    async def post_calibration_stop() -> dict:
        engine.clear_calibration()
        return {"calibration": None}

    # ---- audio (Phase 5) ----

    @app.get("/audio/devices")
    async def get_audio_devices() -> dict:
        return {"devices": list_input_devices()}

    @app.get("/audio/state")
    async def get_audio_state() -> dict:
        return _audio_state_payload()

    @app.post("/audio/select")
    async def post_audio_select(body: AudioSelectRequest) -> dict:
        # Build a fresh capture against the new device. If it fails to start,
        # restart the previous one so the install isn't left without audio.
        prev: AudioCapture = app.state.audio
        prev.stop()
        new_cap = AudioCapture(
            device=body.device,
            samplerate=app.state.config.audio.samplerate,
            blocksize=app.state.config.audio.blocksize,
            fft_window=app.state.config.audio.fft_window,
            channels=app.state.config.audio.channels,
            gain=app.state.config.audio.gain,
        )
        new_cap.start()
        if not new_cap.state.enabled:
            err = new_cap.state.error or "audio capture failed to start"
            # Restore the previous device so the system isn't left silent.
            prev.start()
            engine.attach_audio(prev.state)
            raise HTTPException(status_code=422, detail=err)
        app.state.audio = new_cap
        engine.attach_audio(new_cap.state)

        saved_to: str | None = None
        if body.persist and app.state.config_path is not None:
            try:
                new_cfg = AppConfig.model_validate(
                    {
                        **app.state.config.model_dump(),
                        "audio": {
                            **app.state.config.audio.model_dump(),
                            "device": body.device,
                            "enabled": True,
                        },
                    }
                )
            except ValidationError as e:
                raise HTTPException(
                    status_code=422,
                    detail=e.errors(include_url=False, include_context=False),
                ) from e
            try:
                _write_config_yaml(Path(app.state.config_path), new_cfg)
            except OSError as e:
                raise HTTPException(
                    status_code=500, detail=f"could not write config: {e}"
                ) from e
            app.state.config = new_cfg
            saved_to = str(app.state.config_path)
        return {
            "device": new_cap.state.device_name,
            "configured_device": body.device,
            "samplerate": new_cap.state.samplerate,
            "saved_to": saved_to,
        }

    # ---- live config view + write-back (Phase 4 editor) ----

    @app.get("/config")
    async def get_config() -> dict:
        return _config_to_yaml_dict(app.state.config)

    @app.put("/config")
    async def put_config(body: UpdateLayoutRequest) -> dict:
        # Build a candidate AppConfig with the new strips, keeping all other sections.
        # AppConfig validators run on construction (overlap detection, controller refs,
        # capacity check), so a bad layout is rejected before we touch disk.
        try:
            new_cfg = AppConfig.model_validate(
                {
                    **app.state.config.model_dump(),
                    "strips": [s.model_dump() for s in body.strips],
                }
            )
            new_topo = Topology.from_config(new_cfg)
        except ValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=e.errors(include_url=False, include_context=False),
            ) from e
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

        # Persist to disk before mutating the engine, so a write failure leaves
        # the running config consistent with what's on disk.
        path = app.state.config_path
        if path is not None:
            try:
                _write_config_yaml(Path(path), new_cfg)
            except OSError as e:
                raise HTTPException(
                    status_code=500, detail=f"could not write config: {e}"
                ) from e

        engine.swap_topology(new_topo)
        app.state.config = new_cfg
        return {
            "saved_to": str(path) if path is not None else None,
            "pixel_count": new_topo.pixel_count,
            "strips": [
                {
                    "id": s.id,
                    "pixel_offset": s.pixel_offset,
                    "pixel_count": s.pixel_count,
                    "start": list(s.geometry.start),
                    "end": list(s.geometry.end),
                    "reversed": s.reversed,
                }
                for s in new_topo.strips
            ],
        }

    # ---- websocket ----

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

    @app.websocket("/ws/state")
    async def ws_state(websocket: WebSocket) -> None:
        # Server-push stream of /state at engine.target_fps. Lets the landing
        # page update the engine + audio panel at 60fps without an HTTP poll
        # storm or rebuilding the chat / layer DOM.
        await websocket.accept()
        state_clients.add(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            state_clients.discard(websocket)

    return app


async def _state_broadcaster(
    clients: set[WebSocket],
    payload_fn,
    target_fps: float,
) -> None:
    """Push a fresh state JSON to every connected client at `target_fps`.

    Skips serialisation entirely when nobody is listening so the engine isn't
    paying for a payload no one reads. Drops clients on send error.

    Deadline-based scheduling — same pattern as `Engine._loop` — so payload
    serialisation and per-client sends don't drift the rate below target. A
    flat `await asyncio.sleep(period)` per cycle would have the audio meter
    visibly lag the LED viz on a loaded event loop.
    """
    period = 1.0 / max(1.0, float(target_fps))
    next_tick = time.perf_counter()
    while True:
        next_tick += period
        sleep = next_tick - time.perf_counter()
        if sleep > 0:
            await asyncio.sleep(sleep)
        else:
            # Fell behind — resync to "now" so we don't burst-send a backlog.
            next_tick = time.perf_counter()
        if not clients:
            continue
        try:
            text = json.dumps(payload_fn(), separators=(",", ":"))
        except Exception:
            log.exception("state broadcaster payload failed")
            continue
        dead: list[WebSocket] = []
        for ws in list(clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)
