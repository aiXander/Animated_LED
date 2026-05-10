"""FastAPI app + REST endpoints for surface v2 (Resolume-style layered).

Endpoints:
  - / and /m              static UI
  - /state                full snapshot incl. compositions, masters, audio, ddp
  - /topology             pixel layout for the simulator viz
  - /healthz              liveness probe
  - /effects              GET = on-disk library; POST {name}/save = save a stored effect
  - /effects/{name}       DELETE = remove
  - /effects/{name}/load_preview  POST — load saved → PREVIEW selected layer
  - /effects/{name}/load_live     POST — load saved → LIVE (crossfades)
  - /active                       GET — both compositions, current values, mode
  - /preview/params               PATCH — slider drag → ParamStore.update on selected layer
  - /live/params                  PATCH — slider drag on live's selected layer
  - /preview/select               POST {index} — pick which preview layer is "focused"
  - /live/select                  POST {index} — pick which live layer is "focused"
  - /preview/layer/blend          PATCH {index, blend?, opacity?, enabled?}
  - /live/layer/blend             PATCH {index, blend?, opacity?, enabled?}
  - /preview/layer/remove         POST {index}
  - /live/layer/remove            POST {index}
  - /preview/layer/reorder        POST {src, dst}
  - /live/layer/reorder           POST {src, dst}
  - /promote                      POST — crossfade live ← preview composition
  - /pull_live_to_preview         POST — copy live → preview (hard cut)
  - /mode                         POST {mode: design|live}
  - /blackout, /resume            blackout the live leg
  - /transport/pause, /resume     pause/resume DDP only
  - /sim/pause, /sim/resume       pause/resume sim broadcasts
  - /sim/fps                      PATCH {fps} — UI viz Hz (1..60)
  - /engine/fps                   PATCH {fps} — LED-leg engine Hz (1..240)
  - /calibration/solo|walk|stop   override LEDs with a solid pattern
  - /audio/state, /audio/ui       audio bridge state + UI URL
  - /masters                      GET/PATCH operator master row
  - /config                       GET (full config), PUT (rewrite strip layout)
  - /system/reboot                Pi reboot
  - /agent/*                      mounted by api/agent.py
"""

from __future__ import annotations

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
from ..playlist import (
    DEFAULT_PLAY_SECONDS,
    MIN_PLAY_SECONDS,
    Playlist,
    default_playlist_path,
)
from ..surface import (
    BLEND_MODES,
    EffectCompileError,
    EffectStore,
    Runtime,
)
from ..topology import Topology
from ..transports.ddp import DDPTransport
from ..transports.simulator import SimulatorTransport
from ..transports.split import SplitTransport
from .auth import attach_password_auth, is_websocket_authenticated

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
DEFAULT_EFFECTS_DIR = Path(__file__).resolve().parents[3] / "config" / "effects"


def _build_split_transport(cfg: AppConfig, sim: SimulatorTransport) -> SplitTransport:
    mode = cfg.transport.mode
    led: DDPTransport | None = None
    if mode in ("ddp", "multi"):
        if not cfg.controllers:
            raise ValueError(f"transport mode {mode!r} requires at least one controller")
        ctrl = next(iter(cfg.controllers.values()))
        led = DDPTransport(ctrl.host, ctrl.port)
    return SplitTransport(sim=sim, led=led)


# ---- request bodies ---- #


class CalibrationSoloRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indices: list[int] = Field(..., min_length=1)


class CalibrationWalkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: int = Field(100, gt=0)
    interval: float = Field(1.0, gt=0.0)


class UpdateLayoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strips: list[StripConfig] = Field(..., min_length=1)


class MastersPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brightness: float | None = Field(None, ge=0.0, le=2.0)
    speed: float | None = Field(None, ge=0.0, le=3.0)
    audio_reactivity: float | None = Field(None, ge=0.0, le=3.0)
    saturation: float | None = Field(None, ge=0.0, le=1.0)
    persist: bool = False


class ParamPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, Any] = Field(default_factory=dict)
    layer_index: int | None = Field(
        None, description="If omitted, applies to the slot's selected layer."
    )


class SelectLayerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int


class RemoveLayerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int


class ReorderLayerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    src: int
    dst: int


class LayerMetaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    blend: str | None = None
    opacity: float | None = Field(None, ge=0.0, le=1.0)
    enabled: bool | None = None


class ModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = Field(..., pattern="^(design|live)$")


class LoadEffectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layer_index: int | None = None
    blend: str = "normal"
    opacity: float = Field(1.0, ge=0.0, le=1.0)
    add_layer: bool = Field(False, description="Insert as a new layer instead of replacing.")


class SavePreviewRequest(BaseModel):
    """Operator-driven save of the currently-selected PREVIEW layer to disk."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,40}$")
    summary: str | None = Field(
        None, max_length=400,
        description="Override the in-memory layer's summary; otherwise reused.",
    )
    overwrite: bool = Field(
        True,
        description="When False, fail with 409 if a saved effect with this name exists.",
    )


class StarRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starred: bool


class RenameEffectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_name: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,40}$")


class PlaylistEntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,40}$")
    play_seconds: float = Field(DEFAULT_PLAY_SECONDS, ge=MIN_PLAY_SECONDS, le=3600.0)


class PlaylistPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[PlaylistEntryIn] = Field(default_factory=list)


# ---- yaml writers ---- #


def _strips_to_yaml_dicts(strips: list[StripConfig]) -> list[dict[str, Any]]:
    return [
        {
            "id": s.id, "controller": s.controller, "output": s.output,
            "pixel_offset": s.pixel_offset, "pixel_count": s.pixel_count,
            "leds_per_meter": s.leds_per_meter,
            "geometry": {
                "type": s.geometry.type,
                "start": list(s.geometry.start),
                "end": list(s.geometry.end),
            },
            "reversed": s.reversed,
        }
        for s in strips
    ]


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


def _write_config_yaml(path: Path, cfg: AppConfig) -> None:
    payload = yaml.safe_dump(_config_to_yaml_dict(cfg), sort_keys=False, default_flow_style=False)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_bytes(path.read_bytes())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    tmp.replace(path)


def _build_audio_bridge(cfg: AudioServerConfig) -> AudioBridge | None:
    if not cfg.enabled:
        return None
    return AudioBridge.from_config(cfg)


def _masters_from_config(cfg: AppConfig) -> MasterControls:
    m = cfg.masters
    return MasterControls(
        brightness=m.brightness, speed=m.speed,
        audio_reactivity=m.audio_reactivity, saturation=m.saturation,
    )


def _load_layer_from_store(store: EffectStore, name: str) -> dict[str, Any]:
    """Helper that returns kwargs ready for `runtime.install_layer(...)`."""
    stored = store.load(name)
    return {
        "name": stored.name,
        "summary": stored.summary,
        "source": stored.source,
        "param_schema": stored.param_schema,
        "param_values": stored.param_values,
    }


# ---- app factory ---- #


def create_app(
    cfg: AppConfig,
    presets_dir: Path | None = None,   # legacy; ignored
    config_path: Path | None = None,
    effects_dir: Path | None = None,
) -> FastAPI:
    topology = Topology.from_config(cfg)
    sim = SimulatorTransport(target_fps=float(cfg.transport.sim.fps))
    transport = _build_split_transport(cfg, sim)
    masters = _masters_from_config(cfg)
    runtime = Runtime(
        topology, masters,
        strict_params=bool(getattr(cfg.agent, "strict_params", False)),
    )
    runtime.crossfade_seconds = float(cfg.agent.default_crossfade_seconds)

    eff_dir = (effects_dir or DEFAULT_EFFECTS_DIR).resolve()
    store = EffectStore(eff_dir)
    store.install_examples_if_missing()
    playlist = Playlist.load(default_playlist_path(eff_dir.parent))

    # Boot defaults: try to load `pulse_mono` into both slots; if that fails
    # for any reason, fall back to a single black layer.
    def _safe_install(slot: str, name: str) -> None:
        try:
            kwargs = _load_layer_from_store(store, name)
            runtime.install_layer(slot, **kwargs, blend="normal", opacity=1.0)
        except Exception:
            log.exception("could not install %r into %s; slot stays empty", name, slot)

    if store.exists("pulse_mono"):
        _safe_install("live", "pulse_mono")
        _safe_install("preview", "pulse_mono")
    # Boot is a hard cut — no crossfade from black on first frame.
    runtime._cf = None

    engine = Engine(cfg, topology, transport, runtime, store, masters=masters)

    audio_bridge: AudioBridge | None = _build_audio_bridge(cfg.audio_server)
    if audio_bridge is not None:
        audio_bridge.listener.kick_callback = engine.kick_audio
        audio_bridge.start()
        engine.attach_audio(audio_bridge.state)
    else:
        engine.attach_audio(None)

    state_clients: set[WebSocket] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await engine.start()
        playlist.attach(runtime, store)
        broadcaster = asyncio.create_task(
            _state_broadcaster(state_clients, _full_state_payload, lambda: sim.target_fps),
            name="ledctl-state-broadcaster",
        )
        try:
            yield
        finally:
            playlist.stop()
            broadcaster.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await broadcaster
            await engine.stop()
            bridge: AudioBridge | None = app.state.audio_bridge
            if bridge is not None:
                bridge.stop()
            await transport.close()

    app = FastAPI(title="ledctl", version="2.0.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.runtime = runtime
    app.state.topology = topology
    app.state.simulator = sim
    app.state.config = cfg
    app.state.effects_dir = eff_dir
    app.state.effect_store = store
    app.state.config_path = config_path
    app.state.audio_bridge = audio_bridge
    app.state.playlist = playlist

    from .agent import install_agent_routes
    install_agent_routes(app, cfg.agent)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True, "fps": round(engine.fps, 2)}

    auth_password = (cfg.auth.password or "").strip() if cfg.auth.password else ""
    if auth_password:
        attach_password_auth(
            app, auth_password, cookie_max_age_days=cfg.auth.cookie_max_age_days
        )
        app.state.auth_password = auth_password
    else:
        app.state.auth_password = ""

    _NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}

    # ---- static ---- #

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers=_NO_CACHE_HEADERS)

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

    @app.get("/favicon.ico")
    async def favicon_ico() -> FileResponse:
        return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")

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

    @app.get("/sw.js")
    async def service_worker() -> FileResponse:
        return FileResponse(
            WEB_DIR / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    _LIB_DIR = (WEB_DIR / "lib").resolve()

    @app.get("/lib/{path:path}")
    async def lib_static(path: str) -> FileResponse:
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

    # ---- audio + masters payloads ---- #

    def _audio_state_payload() -> dict[str, Any]:
        bridge: AudioBridge | None = app.state.audio_bridge
        if bridge is None:
            return {
                "enabled": False, "connected": False, "device": "",
                "ui_url": app.state.config.audio_server.ui_url,
                "tailnet_ui_url": app.state.config.audio_server.tailnet_ui_url,
                "error": "audio_server.enabled is false",
                "low": 0.0, "mid": 0.0, "high": 0.0,
                "beat_count": 0, "bpm": None,
            }
        s = bridge.state
        supervisor_error = (
            bridge.supervisor.error if bridge.supervisor is not None else ""
        )
        return {
            "enabled": s.connected, "connected": s.connected, "device": s.device_name,
            "samplerate": s.samplerate, "blocksize": s.blocksize,
            "n_fft_bins": s.n_fft_bins,
            "bands": {
                "low": [s.low_lo, s.low_hi],
                "mid": [s.mid_lo, s.mid_hi],
                "high": [s.high_lo, s.high_hi],
            },
            "ui_url": bridge.ui_url,
            "tailnet_ui_url": app.state.config.audio_server.tailnet_ui_url,
            "error": s.error or supervisor_error,
            "low": round(s.low, 5), "mid": round(s.mid, 5), "high": round(s.high, 5),
            "beat_count": s.beat_count,
            "bpm": round(s.bpm, 2) if s.bpm is not None else None,
        }

    def _masters_payload() -> dict[str, Any]:
        return asdict(engine.masters)

    def _ddp_state_payload() -> dict[str, Any]:
        return transport.ddp_state()

    def _full_state_payload() -> dict:
        snap = runtime.snapshot()
        return {
            "fps": round(engine.fps, 2),
            "target_fps": engine.target_fps,
            "sim_fps": float(sim.target_fps),
            "frame_count": engine.frame_count,
            "dropped_frames": engine.dropped_frames,
            "elapsed": round(engine.elapsed, 3),
            "transport_mode": app.state.config.transport.mode,
            "sim_clients": sim.client_count,
            "blackout": runtime.blackout,
            "crossfading": snap["crossfading"],
            "calibration": engine.calibration_summary(),
            "gamma": engine.gamma,
            "audio": _audio_state_payload(),
            "masters": _masters_payload(),
            "ddp": _ddp_state_payload(),
            "sim_paused": bool(sim.paused),
            "mode": runtime.mode,
            "crossfade_seconds": runtime.crossfade_seconds,
            "live": snap["live"],
            "preview": snap["preview"],
            "playlist": playlist.state(),
        }

    @app.get("/state")
    async def state() -> dict:
        return _full_state_payload()

    @app.get("/topology")
    async def get_topology() -> dict:
        topo = engine.topology
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
                    "id": s.id, "controller": s.controller, "output": s.output,
                    "pixel_offset": s.pixel_offset, "pixel_count": s.pixel_count,
                    "leds_per_meter": s.leds_per_meter,
                    "start": list(s.geometry.start), "end": list(s.geometry.end),
                    "reversed": s.reversed,
                }
                for s in topo.strips
            ],
        }

    # ---- effects library ---- #

    @app.get("/effects")
    async def list_effects() -> dict:
        names = store.list()
        out = []
        for n in names:
            try:
                s = store.load(n)
                out.append({
                    "name": s.name,
                    "summary": s.summary,
                    "param_count": len(s.param_schema),
                    "starred": s.starred,
                    "updated_at": s.updated_at,
                })
            except Exception:
                continue
        return {"effects": out}

    @app.delete("/effects/{name}")
    async def delete_effect(name: str) -> dict:
        ok = store.delete(name)
        if not ok:
            raise HTTPException(status_code=404, detail=f"no effect {name!r}")
        return {"deleted": name}

    @app.post("/preview/save", status_code=201)
    async def save_preview(body: SavePreviewRequest) -> dict:
        """Persist the currently-selected preview layer under `body.name`.

        Operator-driven save (vs. agent-driven save in `apply_write_effect`).
        The on-disk param values pick up whatever the operator dragged the
        sliders to since the layer was loaded.
        """
        from ..surface.schema import WriteEffectArgs
        sel = runtime.preview.selected_layer()
        if sel is None:
            raise HTTPException(status_code=409, detail="preview is empty")
        if not body.overwrite and store.exists(body.name):
            raise HTTPException(
                status_code=409, detail=f"an effect named {body.name!r} already exists",
            )
        try:
            args = WriteEffectArgs(
                name=body.name,
                summary=body.summary if body.summary is not None else sel.summary,
                code=sel.source,
                params=sel.params.schema,
            )
        except ValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=e.errors(include_url=False, include_context=False),
            ) from e
        try:
            stored = store.save(args=args, param_values=sel.params.values())
        except (OSError, ValueError) as e:
            raise HTTPException(status_code=500, detail=f"save failed: {e}") from e
        # Rename the in-memory preview layer to match the saved slug. Without
        # this, subsequent /preview/params slider drags would call
        # store.save_values(<old name>, ...) and quietly overwrite a different
        # on-disk effect (or no-op on a name that's since been deleted).
        sel.name = stored.name
        sel.summary = stored.summary
        _wipe_agent_history()
        return {
            "saved": stored.name,
            "summary": stored.summary,
            "param_count": len(stored.param_schema),
        }

    @app.post("/effects/{name}/rename")
    async def rename_effect(name: str, body: RenameEffectRequest) -> dict:
        if not store.exists(name):
            raise HTTPException(status_code=404, detail=f"no effect {name!r}")
        if body.new_name != name and store.exists(body.new_name):
            raise HTTPException(
                status_code=409,
                detail=f"an effect named {body.new_name!r} already exists",
            )
        try:
            stored = store.rename(name, body.new_name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except (OSError, ValueError) as e:
            raise HTTPException(status_code=500, detail=f"rename failed: {e}") from e
        # Any layers currently loaded from the old name keep working but their
        # `name` attribute would still reference the old slug — subsequent
        # /params drags would call store.save_values(<old>) which silently
        # no-ops (yaml gone). Walk both compositions and update.
        for slot in ("preview", "live"):
            for layer in runtime.composition(slot).layers:
                if layer.name == name:
                    layer.name = stored.name
        _wipe_agent_history()
        return {
            "renamed": {"from": name, "to": stored.name},
            "summary": stored.summary,
        }

    @app.post("/effects/{name}/star")
    async def star_effect(name: str, body: StarRequest) -> dict:
        if not store.exists(name):
            raise HTTPException(status_code=404, detail=f"no effect {name!r}")
        # Round-trip the yaml — `save_values` only handles param_values, so
        # do a tiny manual write here.
        from yaml import safe_dump, safe_load
        yml = (store.root / name / "effect.yaml")
        meta = safe_load(yml.read_text()) or {}
        meta["starred"] = bool(body.starred)
        yml.write_text(safe_dump(meta, sort_keys=False, default_flow_style=False))
        return {"name": name, "starred": bool(body.starred)}

    def _wipe_agent_history() -> None:
        """Clear the LLM's rolling conversation buffer.

        Called when an operator action replaces preview SOURCE outside the
        agent's own write_effect path (library load, pull-live-to-preview,
        save, etc.). Without this wipe, the LLM's prior tool_call payloads
        in the deque reference source that's no longer in preview, which
        confuses follow-up turns ("the prompt says X is loaded but my last
        emit was Y"). Operator-visible `turns` (chat transcript) are
        preserved; only the model-visible message buffer clears.
        """
        store_obj = getattr(app.state, "agent_sessions", None)
        if store_obj is not None:
            store_obj.reset_all_buffers()

    @app.post("/effects/{name}/load_preview")
    async def load_preview(name: str, body: LoadEffectRequest | None = None) -> dict:
        body = body or LoadEffectRequest()
        try:
            kwargs = _load_layer_from_store(store, name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        try:
            runtime.install_layer(
                "preview", **kwargs,
                blend=body.blend, opacity=body.opacity,
                index=body.layer_index, replace=not body.add_layer,
            )
        except (EffectCompileError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        _wipe_agent_history()
        return {"loaded": "preview", "name": name, "snapshot": runtime.snapshot()}

    @app.post("/effects/{name}/load_live")
    async def load_live(name: str, body: LoadEffectRequest | None = None) -> dict:
        body = body or LoadEffectRequest()
        try:
            kwargs = _load_layer_from_store(store, name)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        try:
            runtime.install_layer(
                "live", **kwargs,
                blend=body.blend, opacity=body.opacity,
                index=body.layer_index, replace=not body.add_layer,
            )
        except (EffectCompileError, ValueError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {"loaded": "live", "name": name, "snapshot": runtime.snapshot()}

    # ---- mode + active snapshot ---- #

    @app.get("/active")
    async def active() -> dict:
        return {
            "mode": runtime.mode,
            "blackout": runtime.blackout,
            "crossfade_seconds": runtime.crossfade_seconds,
            "live": runtime.snapshot()["live"],
            "preview": runtime.snapshot()["preview"],
        }

    @app.post("/mode")
    async def set_mode(body: ModeRequest) -> dict:
        runtime.mode = body.mode
        return {"mode": runtime.mode}

    @app.post("/promote")
    async def promote() -> dict:
        try:
            runtime.promote()
        except EffectCompileError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        # Persist current preview values (post-tweak) on each layer.
        for layer in runtime.live.layers:
            with contextlib.suppress(Exception):
                store.save_values(layer.name, layer.params.values())
        return runtime.snapshot()

    @app.post("/pull_live_to_preview")
    async def pull_live_to_preview() -> dict:
        try:
            runtime.pull_live_to_preview()
        except EffectCompileError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        _wipe_agent_history()
        return runtime.snapshot()

    # ---- per-layer controls ---- #

    def _patch_params(slot: str, body: ParamPatchRequest) -> dict:
        comp = runtime.composition(slot)
        if not comp.layers:
            raise HTTPException(status_code=409, detail=f"{slot} has no layers")
        idx = body.layer_index if body.layer_index is not None else comp.selected
        if idx < 0 or idx >= len(comp.layers):
            raise HTTPException(status_code=404, detail=f"no layer at index {idx}")
        layer = comp.layers[idx]
        layer.params.update(body.values)
        # Best-effort persist tweaks on disk so a restart preserves them.
        with contextlib.suppress(Exception):
            store.save_values(layer.name, layer.params.values())
        return {"slot": slot, "index": idx, "values": layer.params.values()}

    @app.patch("/preview/params")
    async def patch_preview_params(body: ParamPatchRequest) -> dict:
        return _patch_params("preview", body)

    @app.patch("/live/params")
    async def patch_live_params(body: ParamPatchRequest) -> dict:
        return _patch_params("live", body)

    @app.post("/preview/select")
    async def select_preview(body: SelectLayerRequest) -> dict:
        return {"selected": runtime.select_layer("preview", body.index)}

    @app.post("/live/select")
    async def select_live(body: SelectLayerRequest) -> dict:
        return {"selected": runtime.select_layer("live", body.index)}

    def _patch_layer_meta(slot: str, body: LayerMetaRequest) -> dict:
        if body.blend is not None and body.blend not in BLEND_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown blend {body.blend!r}; must be one of {list(BLEND_MODES)}",
            )
        ok = runtime.patch_layer_meta(
            slot, body.index,
            blend=body.blend, opacity=body.opacity, enabled=body.enabled,
        )
        if not ok:
            raise HTTPException(status_code=404, detail=f"no layer at index {body.index}")
        return runtime.snapshot()

    @app.patch("/preview/layer/blend")
    async def patch_preview_layer(body: LayerMetaRequest) -> dict:
        return _patch_layer_meta("preview", body)

    @app.patch("/live/layer/blend")
    async def patch_live_layer(body: LayerMetaRequest) -> dict:
        return _patch_layer_meta("live", body)

    @app.post("/preview/layer/remove")
    async def remove_preview_layer(body: RemoveLayerRequest) -> dict:
        if not runtime.remove_layer("preview", body.index):
            raise HTTPException(status_code=404, detail=f"no layer at index {body.index}")
        return runtime.snapshot()

    @app.post("/live/layer/remove")
    async def remove_live_layer(body: RemoveLayerRequest) -> dict:
        if not runtime.remove_layer("live", body.index):
            raise HTTPException(status_code=404, detail=f"no layer at index {body.index}")
        return runtime.snapshot()

    @app.post("/preview/layer/reorder")
    async def reorder_preview_layer(body: ReorderLayerRequest) -> dict:
        if not runtime.reorder_layer("preview", body.src, body.dst):
            raise HTTPException(status_code=422, detail="bad src/dst")
        return runtime.snapshot()

    @app.post("/live/layer/reorder")
    async def reorder_live_layer(body: ReorderLayerRequest) -> dict:
        if not runtime.reorder_layer("live", body.src, body.dst):
            raise HTTPException(status_code=422, detail="bad src/dst")
        return runtime.snapshot()

    # ---- playlist ---- #

    @app.get("/playlist")
    async def get_playlist() -> dict:
        return playlist.state()

    @app.put("/playlist")
    async def put_playlist(body: PlaylistPutRequest) -> dict:
        for entry in body.entries:
            if not store.exists(entry.name):
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown effect {entry.name!r}; not in library",
                )
        playlist.replace_entries([e.model_dump() for e in body.entries])
        return playlist.state()

    @app.post("/playlist/start")
    async def start_playlist() -> dict:
        if not playlist.entries:
            raise HTTPException(status_code=409, detail="playlist is empty")
        try:
            playlist.start()
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return playlist.state()

    @app.post("/playlist/stop")
    async def stop_playlist() -> dict:
        playlist.stop()
        return playlist.state()

    # ---- masters ---- #

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
                    {**app.state.config.model_dump(), "masters": _masters_payload()}
                )
            except ValidationError as e:
                raise HTTPException(
                    status_code=422,
                    detail=e.errors(include_url=False, include_context=False),
                ) from e
            try:
                _write_config_yaml(Path(app.state.config_path), new_cfg)
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"could not write config: {e}") from e
            app.state.config = new_cfg
            saved_to = str(app.state.config_path)
        return {**_masters_payload(), "saved_to": saved_to}

    # ---- blackout ---- #

    @app.post("/blackout")
    async def post_blackout() -> dict:
        runtime.blackout = True
        return {"blackout": True}

    @app.post("/resume")
    async def post_resume() -> dict:
        runtime.blackout = False
        return {"blackout": False}

    # ---- DDP / sim transport control ---- #

    @app.get("/transport")
    async def get_transport() -> dict:
        return {"mode": app.state.config.transport.mode, "ddp": _ddp_state_payload()}

    @app.post("/transport/pause")
    async def post_transport_pause() -> dict:
        if transport.led is None:
            raise HTTPException(status_code=409, detail="no DDP transport in current mode")
        transport.led.paused = True
        return {"ddp": _ddp_state_payload()}

    @app.post("/transport/resume")
    async def post_transport_resume() -> dict:
        if transport.led is None:
            raise HTTPException(status_code=409, detail="no DDP transport in current mode")
        transport.led.paused = False
        return {"ddp": _ddp_state_payload()}

    @app.post("/sim/pause")
    async def post_sim_pause() -> dict:
        sim.paused = True
        return {"sim_paused": True}

    @app.post("/sim/resume")
    async def post_sim_resume() -> dict:
        sim.paused = False
        return {"sim_paused": False}

    @app.patch("/sim/fps")
    async def patch_sim_fps(payload: dict) -> dict:
        """Set the UI viz frame rate (Hz). Independent of the LED-leg engine
        rate. Lower = less Pi CPU spent on `tobytes()` + WebSocket sends +
        Tailscale/WireGuard encryption + /ws/state JSON snapshots."""
        try:
            new_fps = float(payload.get("fps"))
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail="missing/invalid 'fps'") from e
        try:
            sim.set_target_fps(new_fps)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"sim_fps": float(sim.target_fps)}

    @app.patch("/engine/fps")
    async def patch_engine_fps(payload: dict) -> dict:
        """Set the LED-leg engine tick rate (Hz). The render loop re-reads
        `target_fps` every tick so the change is live."""
        try:
            new_fps = int(payload.get("fps"))
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail="missing/invalid 'fps'") from e
        try:
            engine.set_target_fps(new_fps)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"target_fps": engine.target_fps}

    # ---- system ---- #

    @app.post("/system/reboot")
    async def post_system_reboot() -> dict:
        import shutil
        import subprocess
        if shutil.which("sudo") is None or shutil.which("reboot") is None:
            raise HTTPException(status_code=501, detail="reboot not available on this host")
        try:
            subprocess.Popen(
                ["sudo", "-n", "/bin/sh", "-c", "sleep 1 && /sbin/reboot"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"reboot failed: {e}") from e
        log.warning("system reboot requested via /system/reboot")
        return {"ok": True, "message": "rebooting in ~1s"}

    # ---- calibration ---- #

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

    # ---- audio bridge ---- #

    @app.get("/audio/state")
    async def get_audio_state() -> dict:
        return _audio_state_payload()

    @app.get("/audio/ui")
    async def get_audio_ui() -> dict:
        bridge: AudioBridge | None = app.state.audio_bridge
        cfg = app.state.config.audio_server
        url = bridge.ui_url if bridge is not None else cfg.ui_url
        return {
            "ui_url": url,
            "tailnet_ui_url": cfg.tailnet_ui_url,
            "enabled": cfg.enabled,
        }

    # ---- live config view + write-back ---- #

    @app.get("/config")
    async def get_config() -> dict:
        return _config_to_yaml_dict(app.state.config)

    @app.put("/config")
    async def put_config(body: UpdateLayoutRequest) -> dict:
        try:
            new_cfg = AppConfig.model_validate(
                {**app.state.config.model_dump(), "strips": [s.model_dump() for s in body.strips]}
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
                raise HTTPException(status_code=500, detail=f"could not write config: {e}") from e
        engine.swap_topology(new_topo)
        app.state.config = new_cfg
        return {
            "saved_to": str(path) if path is not None else None,
            "pixel_count": new_topo.pixel_count,
            "strips": [
                {
                    "id": s.id, "pixel_offset": s.pixel_offset,
                    "pixel_count": s.pixel_count,
                    "start": list(s.geometry.start), "end": list(s.geometry.end),
                    "reversed": s.reversed,
                }
                for s in new_topo.strips
            ],
        }

    # ---- websocket ---- #

    ws_path = cfg.transport.sim.ws_path

    async def _ws_auth_or_close(websocket: WebSocket) -> bool:
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
    fps_fn,
) -> None:
    """Push `_full_state_payload()` to every /ws/state client at the rate
    returned by `fps_fn` (the sim/UI FPS — re-read each tick so a slider drag
    takes effect immediately)."""
    next_tick = time.perf_counter()
    while True:
        period = 1.0 / max(1.0, float(fps_fn()))
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
