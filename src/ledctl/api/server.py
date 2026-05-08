import asyncio
import contextlib
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..audio import AudioBridge
from ..config import AppConfig, AudioServerConfig, StripConfig
from ..engine import Engine
from ..masters import MasterControls
from ..mixer import BLEND_MODES
from ..presets import list_presets, load_preset, save_preset, validate_preset_name
from ..surface import primitives_json
from ..topology import Topology
from ..transports.base import Transport
from ..transports.ddp import DDPTransport
from ..transports.multi import MultiTransport
from ..transports.simulator import SimulatorTransport
from .auth import attach_password_auth, is_websocket_authenticated

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
DEFAULT_PRESETS_DIR = Path(__file__).resolve().parents[3] / "config" / "presets"


def _build_transport(cfg: AppConfig, sim: SimulatorTransport) -> Transport:
    mode = cfg.transport.mode
    if mode == "simulator":
        return sim
    if not cfg.controllers:
        raise ValueError(f"transport mode {mode!r} requires at least one controller")
    ctrl = next(iter(cfg.controllers.values()))
    if mode == "ddp":
        return DDPTransport(ctrl.host, ctrl.port)
    if mode == "multi":
        return MultiTransport([sim, DDPTransport(ctrl.host, ctrl.port)])
    raise ValueError(f"unknown transport mode: {mode!r}")


# ---- request bodies ----


class PushLayerRequest(BaseModel):
    """Body for POST /layers — append a single compiled layer to the stack."""

    model_config = ConfigDict(extra="forbid")
    node: dict[str, Any] = Field(
        ..., description="Surface tree {kind, params}; leaf must be rgb_field"
    )
    blend: str = "normal"
    opacity: float = Field(1.0, ge=0.0, le=1.0)


class PatchLayerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: dict[str, Any] | None = None
    blend: str | None = None
    opacity: float | None = Field(None, ge=0.0, le=1.0)


class ApplyPresetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    crossfade_seconds: float | None = Field(
        None, ge=0.0, description="Override the preset's own crossfade duration"
    )
    apply_masters: bool = Field(
        False,
        description=(
            "If true, also push the preset's masters block into MasterControls. "
            "Off by default — the visual stack and the room knobs are independent."
        ),
    )


class SavePresetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(
        ...,
        min_length=1,
        max_length=40,
        description="Preset name; letters/digits/_/-, max 40 chars.",
    )
    overwrite: bool = Field(
        True, description="If false, fail with 409 when a preset of this name exists."
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
    model_config = ConfigDict(extra="forbid")
    strips: list[StripConfig] = Field(..., min_length=1)


class MastersPatchRequest(BaseModel):
    """Partial update for the operator master row.

    With `persist=true` and a known config_path the post-merge values are
    written into the `masters:` block of config.yaml (atomic with `.bak`).
    """

    model_config = ConfigDict(extra="forbid")
    brightness: float | None = Field(None, ge=0.0, le=2.0)
    speed: float | None = Field(None, ge=0.0, le=3.0)
    audio_reactivity: float | None = Field(None, ge=0.0, le=3.0)
    saturation: float | None = Field(None, ge=0.0, le=1.0)
    freeze: bool | None = None
    persist: bool = False


def _strips_to_yaml_dicts(strips: list[StripConfig]) -> list[dict[str, Any]]:
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
    return {
        "project": cfg.project.model_dump(),
        "server": cfg.server.model_dump(),
        "controllers": {k: v.model_dump() for k, v in cfg.controllers.items()},
        "strips": _strips_to_yaml_dicts(list(cfg.strips)),
        "transport": cfg.transport.model_dump(),
        "output": cfg.output.model_dump(),
        "audio_server": cfg.audio_server.model_dump(),
        "agent": cfg.agent.model_dump(),
        "masters": cfg.masters.model_dump(),
    }


def _build_audio_bridge(cfg: AudioServerConfig) -> AudioBridge | None:
    if not cfg.enabled:
        return None
    return AudioBridge.from_config(cfg)


def _masters_from_config(cfg: AppConfig) -> MasterControls:
    m = cfg.masters
    return MasterControls(
        brightness=m.brightness,
        speed=m.speed,
        audio_reactivity=m.audio_reactivity,
        saturation=m.saturation,
        freeze=m.freeze,
    )


def create_app(
    cfg: AppConfig,
    presets_dir: Path | None = None,
    config_path: Path | None = None,
) -> FastAPI:
    topology = Topology.from_config(cfg)
    sim = SimulatorTransport()
    transport = _build_transport(cfg, sim)
    engine = Engine(cfg, topology, transport, masters=_masters_from_config(cfg))
    presets_path = presets_dir or DEFAULT_PRESETS_DIR

    # Default startup: load the `default` preset from disk so the show is
    # editable as YAML rather than buried in code.
    _default_preset = load_preset("default", presets_path)
    for _layer in _default_preset.layers:
        engine.push_layer(
            _layer.node,
            blend=_layer.blend,
            opacity=_layer.opacity,
        )

    audio_bridge: AudioBridge | None = _build_audio_bridge(cfg.audio_server)
    if audio_bridge is not None:
        # Wire the kick callback BEFORE start() so the very first packet
        # already wakes the render loop. The listener fires this from its
        # OSC server thread; engine.kick_audio() is internally thread-safe
        # via call_soon_threadsafe.
        audio_bridge.listener.kick_callback = engine.kick_audio
        audio_bridge.start()
        engine.attach_audio(audio_bridge.state)
    else:
        engine.attach_audio(None)

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
            bridge: AudioBridge | None = app.state.audio_bridge
            if bridge is not None:
                bridge.stop()
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
    app.state.audio_bridge = audio_bridge

    from .agent import install_agent_routes

    install_agent_routes(app, cfg.agent, presets_path)

    # Always-public liveness probe — handy for systemd watchdogs and Phase 9
    # reliability work. Returns 200 even when auth is enabled.
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True, "fps": round(engine.fps, 2)}

    # Shared-password gate. Off in dev (no `auth.password`), on for the Pi.
    # Attach AFTER routes/state so the middleware sees them, and so the login
    # page can read `app.state.auth_password`.
    auth_password = (cfg.auth.password or "").strip() if cfg.auth.password else ""
    if auth_password:
        attach_password_auth(
            app, auth_password, cookie_max_age_days=cfg.auth.cookie_max_age_days
        )
        app.state.auth_password = auth_password
    else:
        app.state.auth_password = ""

    def current_topology() -> Topology:
        return engine.topology

    # ---- static / topology ----

    _NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers=_NO_CACHE_HEADERS)

    @app.get("/m")
    async def mobile() -> FileResponse:
        return FileResponse(WEB_DIR / "mobile.html", headers=_NO_CACHE_HEADERS)

    @app.get("/editor")
    async def editor() -> FileResponse:
        return FileResponse(WEB_DIR / "editor.html", headers=_NO_CACHE_HEADERS)

    @app.get("/audio-meter.js")
    async def audio_meter_js() -> FileResponse:
        return FileResponse(
            WEB_DIR / "audio-meter.js",
            media_type="application/javascript",
            headers=_NO_CACHE_HEADERS,
        )

    @app.get("/favicon.svg")
    async def favicon_svg() -> FileResponse:
        return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")

    # /favicon.ico is what most browsers ask for by default; serve the SVG.
    @app.get("/favicon.ico")
    async def favicon_ico() -> FileResponse:
        return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")

    # ---- PWA: manifest, raster icons, service worker ----
    # The mobile UI is installable as a home-screen app (Add to Home Screen on
    # iOS Safari, "Install app" prompt on Android Chrome). The SW is minimal —
    # it exists only to satisfy Chrome's installability heuristic.
    @app.get("/manifest.webmanifest")
    async def manifest() -> FileResponse:
        return FileResponse(
            WEB_DIR / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    @app.get("/icon-192.png")
    async def icon_192() -> FileResponse:
        return FileResponse(WEB_DIR / "icon-192.png", media_type="image/png")

    @app.get("/icon-512.png")
    async def icon_512() -> FileResponse:
        return FileResponse(WEB_DIR / "icon-512.png", media_type="image/png")

    @app.get("/apple-touch-icon.png")
    async def apple_touch_icon() -> FileResponse:
        return FileResponse(WEB_DIR / "apple-touch-icon.png", media_type="image/png")

    # Service worker MUST be served from the site root (its scope is the
    # directory it's served from); /sw.js gives it scope "/" — broad enough
    # to cover both / and /m.
    @app.get("/sw.js")
    async def service_worker() -> FileResponse:
        return FileResponse(
            WEB_DIR / "sw.js",
            media_type="application/javascript",
            headers={
                # Browsers cap SW cache at 24h anyway; explicit no-cache means
                # operators can ship updates without users uninstalling.
                "Cache-Control": "no-cache",
                "Service-Worker-Allowed": "/",
            },
        )

    # Shared ES modules under src/web/lib/ — bootstrap script + per-feature
    # modules used by both index.html and mobile.html.
    _LIB_DIR = (WEB_DIR / "lib").resolve()

    @app.get("/lib/{path:path}")
    async def lib_static(path: str) -> FileResponse:
        # Resolve and verify the candidate stays inside _LIB_DIR — guards
        # against `..` traversal (FastAPI's path converter passes raw text).
        candidate = (_LIB_DIR / path).resolve()
        try:
            candidate.relative_to(_LIB_DIR)
        except ValueError as e:
            raise HTTPException(status_code=404, detail="not found") from e
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(
            candidate,
            media_type="application/javascript",
            headers=_NO_CACHE_HEADERS,
        )

    def _audio_state_payload() -> dict[str, Any]:
        bridge: AudioBridge | None = app.state.audio_bridge
        if bridge is None:
            return {
                "enabled": False,
                "connected": False,
                "device": "",
                "ui_url": app.state.config.audio_server.ui_url,
                "tailnet_ui_url": app.state.config.audio_server.tailnet_ui_url,
                "error": "audio_server.enabled is false",
                "low": 0.0,
                "mid": 0.0,
                "high": 0.0,
            }
        s = bridge.state
        supervisor_error = (
            bridge.supervisor.error if bridge.supervisor is not None else ""
        )
        return {
            "enabled": s.connected,
            "connected": s.connected,
            "device": s.device_name,
            "samplerate": s.samplerate,
            "blocksize": s.blocksize,
            "n_fft_bins": s.n_fft_bins,
            "bands": {
                "low": [s.low_lo, s.low_hi],
                "mid": [s.mid_lo, s.mid_hi],
                "high": [s.high_lo, s.high_hi],
            },
            "ui_url": bridge.ui_url,
            "tailnet_ui_url": app.state.config.audio_server.tailnet_ui_url,
            "error": s.error or supervisor_error,
            "low": round(s.low, 5),
            "mid": round(s.mid, 5),
            "high": round(s.high, 5),
        }

    def _masters_payload() -> dict[str, Any]:
        return asdict(engine.masters)

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
            "masters": _masters_payload(),
            "ddp": _ddp_state_payload(),
            "sim_paused": bool(sim.paused),
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

    # ---- surface primitives catalogue ----

    @app.get("/surface/primitives")
    async def get_surface_primitives() -> dict:
        return primitives_json()

    # ---- layers ----

    def _wipe_agent_history() -> None:
        """Clear LLM message buffers when the operator mutates the layer stack.

        The LLM's prior `update_leds` tool-call args are stored verbatim in
        each session's rolling buffer. Once the operator changes the stack
        (delete/add/patch a layer, or apply a preset), those args reference
        layers that no longer exist or no longer match — and the model
        pattern-matches against them and undoes the operator's edit on the
        next turn. Wiping `messages` makes the LLM start fresh: it sees the
        current layer stack via CURRENT STATE in the system prompt and the
        new user prompt, with no stale conversational context to pull from.
        Operator-visible `turns` and the session id are kept, so the chat
        panel transcript is unaffected.
        """
        store = getattr(app.state, "agent_sessions", None)
        if store is not None:
            store.reset_all_buffers()

    @app.post("/layers", status_code=201)
    async def post_layer(body: PushLayerRequest) -> dict:
        if body.blend not in BLEND_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown blend {body.blend!r}; must be one of {list(BLEND_MODES)}",
            )
        try:
            i = engine.push_layer(body.node, body.blend, body.opacity)
        except ValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=e.errors(include_url=False, include_context=False),
            ) from e
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        _wipe_agent_history()
        return {"layer_index": i, "layers": engine.layer_state()}

    @app.patch("/layers/{i}")
    async def patch_layer(i: int, body: PatchLayerRequest) -> dict:
        if i < 0 or i >= len(engine.mixer.layers):
            raise HTTPException(status_code=404, detail=f"no layer at index {i}")
        if body.blend is not None and body.blend not in BLEND_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown blend {body.blend!r}; must be one of {list(BLEND_MODES)}",
            )
        try:
            engine.update_layer(i, body.node, body.blend, body.opacity)
        except ValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=e.errors(include_url=False, include_context=False),
            ) from e
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        _wipe_agent_history()
        return {"layer_index": i, "layers": engine.layer_state()}

    @app.delete("/layers/{i}")
    async def delete_layer(i: int) -> dict:
        if i < 0 or i >= len(engine.mixer.layers):
            raise HTTPException(status_code=404, detail=f"no layer at index {i}")
        engine.remove_layer(i)
        _wipe_agent_history()
        return {"layers": engine.layer_state()}

    # ---- masters ----

    @app.get("/masters")
    async def get_masters() -> dict:
        return _masters_payload()

    @app.patch("/masters")
    async def patch_masters(body: MastersPatchRequest) -> dict:
        patch = {
            k: v
            for k, v in body.model_dump(exclude_none=True).items()
            if k != "persist"
        }
        try:
            engine.set_masters(**patch)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

        saved_to: str | None = None
        if body.persist and app.state.config_path is not None:
            try:
                new_cfg = AppConfig.model_validate(
                    {
                        **app.state.config.model_dump(),
                        "masters": _masters_payload(),
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
        return {**_masters_payload(), "saved_to": saved_to}

    # ---- blackout ----

    @app.post("/blackout")
    async def post_blackout() -> dict:
        engine.mixer.blackout = True
        return {"blackout": True}

    @app.post("/resume")
    async def post_resume() -> dict:
        engine.mixer.blackout = False
        return {"blackout": False}

    # ---- DDP control (Pi vs Gledopto) ----
    # Pausing the DDP transport stops realtime frames to WLED. WLED's own
    # realtime override expires after ~2.5 s, after which the Gledopto's
    # active preset/effect takes over the LEDs again. The simulator leg of
    # MultiTransport keeps streaming so the operator UI viz stays live.

    def _ddp_transports() -> list[DDPTransport]:
        t = transport
        if isinstance(t, DDPTransport):
            return [t]
        if isinstance(t, MultiTransport):
            return [c for c in t._transports if isinstance(c, DDPTransport)]
        return []

    def _ddp_state_payload() -> dict[str, Any]:
        ddps = _ddp_transports()
        if not ddps:
            return {"available": False, "paused": False, "frames_sent": 0, "packets_sent": 0}
        d = ddps[0]
        return {
            "available": True,
            "paused": bool(d.paused),
            "host": d.host,
            "port": d.port,
            "frames_sent": d.frames_sent,
            "packets_sent": d.packets_sent,
        }

    @app.get("/transport")
    async def get_transport() -> dict:
        return {"mode": app.state.config.transport.mode, "ddp": _ddp_state_payload()}

    @app.post("/transport/pause")
    async def post_transport_pause() -> dict:
        ddps = _ddp_transports()
        if not ddps:
            raise HTTPException(status_code=409, detail="no DDP transport in current mode")
        for d in ddps:
            d.paused = True
        return {"ddp": _ddp_state_payload()}

    @app.post("/transport/resume")
    async def post_transport_resume() -> dict:
        ddps = _ddp_transports()
        if not ddps:
            raise HTTPException(status_code=409, detail="no DDP transport in current mode")
        for d in ddps:
            d.paused = False
        return {"ddp": _ddp_state_payload()}

    # Pausing the simulator stream stops broadcasting frames to browser
    # viz clients. The Pi was being overloaded driving DDP + the simulator
    # WebSocket simultaneously; this lets the operator drop the sim leg
    # on demand without touching DDP.
    @app.post("/sim/pause")
    async def post_sim_pause() -> dict:
        sim.paused = True
        return {"sim_paused": True}

    @app.post("/sim/resume")
    async def post_sim_resume() -> dict:
        sim.paused = False
        return {"sim_paused": False}

    # ---- system ----

    @app.post("/system/reboot")
    async def post_system_reboot() -> dict:
        """Reboot the host machine (Pi). Requires passwordless sudo for /sbin/reboot.

        Schedules `sudo /sbin/reboot` ~1s in the future so the HTTP response
        can return cleanly before the system goes down.
        """
        import shutil
        import subprocess

        if shutil.which("sudo") is None or shutil.which("reboot") is None:
            raise HTTPException(status_code=501, detail="reboot not available on this host")
        try:
            subprocess.Popen(
                ["sudo", "-n", "/bin/sh", "-c", "sleep 1 && /sbin/reboot"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:  # pragma: no cover
            raise HTTPException(status_code=500, detail=f"reboot failed: {e}") from e
        log.warning("system reboot requested via /system/reboot")
        return {"ok": True, "message": "rebooting in ~1s"}

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
        # Operator's master crossfade slider (agent.default_crossfade_seconds)
        # is the single source of truth for transition speed. The duration
        # baked into the preset YAML is preserved for archival reasons but
        # ignored at apply-time. An explicit `body.crossfade_seconds` still
        # overrides for direct API/automation callers.
        duration = (
            body.crossfade_seconds
            if body.crossfade_seconds is not None
            else float(app.state.config.agent.default_crossfade_seconds)
        )
        try:
            engine.crossfade_to(preset.layers, duration)
        except ValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=e.errors(include_url=False, include_context=False),
            ) from e
        except (TypeError, ValueError, KeyError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        _wipe_agent_history()
        if body.apply_masters:
            try:
                engine.set_masters(**preset.masters.model_dump())
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
        return {
            "applied": preset.name or name,
            "crossfade_seconds": duration,
            "applied_masters": body.apply_masters,
            "layers": engine.layer_state(),
            "masters": _masters_payload(),
        }

    @app.post("/presets", status_code=201)
    async def create_preset(body: SavePresetRequest) -> dict:
        try:
            validate_preset_name(body.name)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        layers = engine.layer_state()
        masters = _masters_payload()
        # Crossfade duration baked into the preset = whatever the operator has
        # currently dialed in on the agent's default-crossfade slider. That's
        # the closest single-source-of-truth for "how fast should this preset
        # come in next time" without adding a second knob to the save dialog.
        duration = float(app.state.config.agent.default_crossfade_seconds)
        try:
            path = save_preset(
                name=body.name,
                presets_dir=presets_path,
                crossfade_seconds=duration,
                layers=layers,
                masters=masters,
                overwrite=body.overwrite,
            )
        except FileExistsError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except (ValidationError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(
                status_code=500, detail=f"could not write preset: {e}"
            ) from e
        return {
            "name": body.name,
            "saved_to": str(path),
            "crossfade_seconds": duration,
            "layers": layers,
            "masters": masters,
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

    # ---- audio (external feature server bridge) ----

    @app.get("/audio/state")
    async def get_audio_state() -> dict:
        return _audio_state_payload()

    @app.get("/audio/ui")
    async def get_audio_ui() -> dict:
        """Return where to open the audio-server's browser UI.

        The 'audio' link in the operator UI hits this and redirects the user.
        Centralised so the URL is sourced from config rather than hard-coded
        on the client.
        """
        bridge: AudioBridge | None = app.state.audio_bridge
        cfg = app.state.config.audio_server
        url = bridge.ui_url if bridge is not None else cfg.ui_url
        return {
            "ui_url": url,
            "tailnet_ui_url": cfg.tailnet_ui_url,
            "enabled": cfg.enabled,
        }

    # ---- live config view + write-back (Phase 4 editor) ----

    @app.get("/config")
    async def get_config() -> dict:
        return _config_to_yaml_dict(app.state.config)

    @app.put("/config")
    async def put_config(body: UpdateLayoutRequest) -> dict:
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

    async def _ws_auth_or_close(websocket: WebSocket) -> bool:
        """Reject the upgrade if auth is on and the cookie doesn't match.

        Starlette's HTTP middleware doesn't run on WS upgrades, so we check
        the same cookie here. Closing pre-accept gives the browser a clean
        4401 without the server ever entering the message loop.
        """
        pw: str = app.state.auth_password
        if pw and not is_websocket_authenticated(websocket, pw):
            await websocket.close(code=4401)
            return False
        return True

    @app.websocket(ws_path)
    async def ws_frames(websocket: WebSocket) -> None:
        if not await _ws_auth_or_close(websocket):
            return
        await websocket.accept()
        await sim.add_client(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await sim.remove_client(websocket)

    @app.websocket("/ws/state")
    async def ws_state(websocket: WebSocket) -> None:
        if not await _ws_auth_or_close(websocket):
            return
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
    """Push a fresh state JSON to every connected client at `target_fps`."""
    period = 1.0 / max(1.0, float(target_fps))
    next_tick = time.perf_counter()
    while True:
        next_tick += period
        sleep = next_tick - time.perf_counter()
        if sleep > 0:
            await asyncio.sleep(sleep)
        else:
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
