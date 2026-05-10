from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Vec3 = tuple[float, float, float]


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    target_fps: int = Field(60, gt=0, le=240)


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = Field(8000, gt=0, lt=65536)


class AuthConfig(BaseModel):
    """Optional shared-password gate for the HTTP/WS surface.

    Off by default (no `password` set). When a password is configured the
    operator UI, REST endpoints, and websockets all require either:
      - a `ledctl_auth` cookie matching the password, or
      - a `?password=…` query param (cookie is then set automatically).

    The render loop and DDP transport are unaffected — auth only gates the
    public-facing API surface. On the Pi it's the difference between "anyone
    on venue WiFi can blackout the rig" and "only people with the share-code
    can". Comment the whole block out (or delete `password`) in dev configs.
    """

    model_config = ConfigDict(extra="forbid")
    password: str | None = Field(
        None,
        description=(
            "Shared password for the operator UI. Null/empty = open server. "
            "Plain string — no hashing v1; this isn't protecting secrets, "
            "it's keeping randoms off the LED panel during a gig."
        ),
    )
    cookie_max_age_days: int = Field(
        30, ge=1, le=365,
        description="How long the browser remembers the password after login.",
    )


class ControllerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["wled-ddp"]
    host: str
    port: int = 4048
    pixel_count: int = Field(..., gt=0)


class LineGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["line"]
    start: Vec3
    end: Vec3


class StripConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    controller: str
    output: int = Field(1, ge=1)
    pixel_offset: int = Field(..., ge=0)
    pixel_count: int = Field(..., gt=0)
    leds_per_meter: float = Field(30.0, gt=0)
    geometry: LineGeometry
    reversed: bool = False


class SimTransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ws_path: str = "/ws/frames"
    fps: int = Field(
        24, ge=1, le=60,
        description=(
            "Simulator-viz frame-stream rate (Hz). The engine still ticks at "
            "`project.target_fps` for the LED leg; this caps how often we "
            "encode + push a frame to the /ws/frames stream and how often "
            "/ws/state JSON snapshots fire. 24 Hz is plenty for visual judgment "
            "and meaningfully lowers Pi CPU when streaming over Tailscale "
            "(WireGuard encryption is per-packet)."
        ),
    )


class TransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["simulator", "ddp", "multi"] = "simulator"
    sim: SimTransportConfig = SimTransportConfig()


class OutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gamma: float = Field(
        2.2, ge=0.1, le=5.0,
        description=(
            "Gamma applied at the transport boundary; 1.0 disables. "
            "Set 1.0 if WLED is also gamma-correcting."
        ),
    )
    lut_size: int = Field(
        256, ge=16, le=8192,
        description=(
            "Entries in the baked palette LUT. Larger = smoother colour ramps "
            "at the cost of more memory per palette and a one-time bake cost. "
            "256 is enough for the 1800-LED install; bump to 1024 if visible "
            "stair-stepping appears on slow scalar gradients."
        ),
    )


class AudioServerConfig(BaseModel):
    """External audio-feature server bridge.

    The LED controller no longer captures or analyses audio itself. Instead it
    auto-starts the
    [Realtime_PyAudio_FFT](https://github.com/Jaymon/Realtime_PyAudio_FFT)
    subprocess and listens for `/audio/lmh` + `/audio/meta` over OSC. Device
    selection, smoothing, band cutoffs etc. are all owned by that server's UI
    (default: http://127.0.0.1:8766) — there is no LED-side mic config.

    Fail modes are deliberately soft. If the subprocess fails to start, the
    OSC port is in use, or the bridge goes silent for `stale_after_s`, the
    LED render loop keeps running with `audio_band` returning 0 and a warning
    in the terminal. Re-enable reactivity by fixing the underlying issue
    (install the audio-server package, free the port, restart the LED server).
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        True,
        description=(
            "Master switch. When false the bridge isn't started; audio_band "
            "primitives return 0 and the operator UI shows 'audio off'."
        ),
    )
    autostart: bool = Field(
        True,
        description=(
            "Whether ledctl spawns the external audio-server as a subprocess "
            "on boot. Set False if you'd rather run it manually (handy when "
            "tuning bands via its UI on a different machine)."
        ),
    )
    command: list[str] = Field(
        default_factory=lambda: ["audio-server"],
        description=(
            "argv passed to subprocess.Popen for the audio-server. Default "
            "expects the `audio-server` console script from the "
            "Realtime_PyAudio_FFT package on PATH. Override with e.g. "
            "['python', '-m', 'server.main'] alongside `working_dir` if you "
            "run it from a checkout."
        ),
    )
    working_dir: str | None = Field(
        None,
        description=(
            "cwd for the audio-server subprocess. Optional. Useful when "
            "launching via `python -m server.main` from a clone. Relative "
            "paths are resolved against the directory containing the YAML "
            "config file (so '../../Realtime_PyAudio_FFT' from "
            "config/config.pi.yaml points at the sibling clone in the "
            "repo's parent directory)."
        ),
    )
    osc_listen_host: str = Field(
        "127.0.0.1",
        description=(
            "Local interface the OSC listener binds to. The audio-server "
            "ships `/audio/lmh` to this host:port; keep loopback unless "
            "you've moved the audio-server to another machine."
        ),
    )
    osc_listen_port: int = Field(
        9000, gt=0, lt=65536,
        description=(
            "UDP port for the OSC listener. Must match `osc.destinations` "
            "in the audio-server's config.yaml (default 9000)."
        ),
    )
    ui_url: str = Field(
        "http://127.0.0.1:8766",
        description=(
            "Where the audio-server's browser UI lives. The 'audio' link in "
            "the LED UI opens this in a new tab — pick devices, retune "
            "bands, save presets there."
        ),
    )
    tailnet_ui_url: str | None = Field(
        None,
        description=(
            "Optional override for the 'audio' link when the operator UI is "
            "loaded over a Tailscale tailnet (https + *.ts.net host). The "
            "configured `ui_url` (loopback) is unreachable from a remote "
            "browser; Tailscale Serve only exposes 443/8443/10000, so set "
            "this to e.g. `https://<host>.ts.net:10000/` if you've mounted "
            "the audio UI there. Leave unset to fall back to `ui_url`."
        ),
    )
    stale_after_s: float = Field(
        1.5,
        gt=0.0,
        le=60.0,
        description=(
            "Watchdog: if no `/audio/lmh` packet arrives for this many "
            "seconds the bridge is marked disconnected and the engine reverts "
            "to non-reactive output. The audio-server emits at audio block "
            "rate (~187 Hz @ 48k/256), so 1.5 s is well above the noise floor."
        ),
    )


class AgentConfig(BaseModel):
    """Language-driven control panel (Phase 6).

    A thin layer over OpenRouter (OpenAI-compatible). The render loop is
    independent — even with `enabled: true`, `/agent/chat` returning a 503 on
    a missing API key never affects what the LEDs are doing.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        True, description="Mount /agent/* and /chat. The render loop is unaffected either way."
    )
    provider: Literal["openrouter"] = Field(
        "openrouter", description="LLM provider; only OpenRouter for v1"
    )
    base_url: str = Field(
        "https://openrouter.ai/api/v1",
        description="OpenAI-compatible chat-completions endpoint",
    )
    model: str = Field(
        "anthropic/claude-sonnet-4-6",
        description="OpenRouter model id (e.g. 'anthropic/claude-sonnet-4-6')",
    )
    history_max_messages: int = Field(
        20,
        ge=2,
        le=200,
        description=(
            "Rolling buffer size, excluding the system prompt. Each user turn "
            "produces 3 messages (user / assistant tool-call / tool result), so "
            "20 covers ~6 turns of context."
        ),
    )
    request_timeout_seconds: float = Field(
        60.0, gt=0.0, description="Hard timeout on a single OpenRouter request"
    )
    rate_limit_per_minute: int = Field(
        30,
        ge=0,
        description="Per-session limit on /agent/chat (0 disables)",
    )
    default_crossfade_seconds: float = Field(
        1.0,
        ge=0.0,
        description="Used if the model omits `crossfade_seconds` in its tool call",
    )
    api_key_env: str = Field(
        "OPENROUTER_API_KEY",
        description="Env var the server reads the API key from (never YAML)",
    )
    debug_logging: bool = Field(
        False,
        description=(
            "Verbose per-turn logging of the OpenRouter request + response "
            "(model, message previews, tool args, raw response). Off by "
            "default; flip to True when diagnosing tool-call failures. "
            "Errors are always logged regardless of this flag."
        ),
    )
    retry_on_tool_error: int = Field(
        2,
        ge=0,
        le=5,
        description=(
            "On a failed `write_effect` tool call (validation/compile error), "
            "automatically re-prompt the LLM up to this many extra times. "
            "The failed tool result stays in the rolling buffer, so the model "
            "sees the structured error path on each retry and can self-correct. "
            "0 disables retries (the operator sees the first failure)."
        ),
    )
    strict_params: bool = Field(
        False,
        description=(
            "When True, an LLM-authored effect that tries to write `ctx.params.*` "
            "raises `TypeError` (loud failure). When False (v1 default), it logs a "
            "warning and silently no-ops so a sloppy assignment doesn't blackout "
            "the dance floor. Flip on once the prompt is reliable."
        ),
    )


class MastersConfig(BaseModel):
    """Operator-owned room knobs (refactor §7).

    Persistence shim for the operator UI's `PATCH /masters` with
    `persist=true`. The render-time copy lives on `Engine.masters` and is
    seeded from this block on boot.
    """

    model_config = ConfigDict(extra="forbid")
    brightness: float = Field(1.0, ge=0.0, le=2.0)
    speed: float = Field(1.0, ge=0.0, le=3.0)
    audio_reactivity: float = Field(1.0, ge=0.0, le=3.0)
    saturation: float = Field(1.0, ge=0.0, le=1.0)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: ProjectConfig
    server: ServerConfig = ServerConfig()
    auth: AuthConfig = AuthConfig()
    controllers: dict[str, ControllerConfig]
    strips: list[StripConfig]
    transport: TransportConfig = TransportConfig()
    output: OutputConfig = OutputConfig()
    audio_server: AudioServerConfig = AudioServerConfig()
    agent: AgentConfig = AgentConfig()
    masters: MastersConfig = MastersConfig()

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_audio_block(cls, data: object) -> object:
        # The Phase-5 `audio:` block (device, samplerate, blocksize, fft_window)
        # was ripped out when capture moved to the external audio-feature
        # server. Drop it silently so older YAMLs in the wild still load —
        # `extra="forbid"` would otherwise reject the unknown key. The new
        # `audio_server:` block is unrelated and lives next to it.
        if isinstance(data, dict) and "audio" in data:
            data = {k: v for k, v in data.items() if k != "audio"}
        return data

    @model_validator(mode="after")
    def _check_strip_layout(self) -> "AppConfig":
        seen_ids: set[str] = set()
        for s in self.strips:
            if s.id in seen_ids:
                raise ValueError(f"duplicate strip id: {s.id}")
            seen_ids.add(s.id)
            if s.controller not in self.controllers:
                raise ValueError(
                    f"strip {s.id!r} references unknown controller {s.controller!r}"
                )
        # Detect overlapping pixel ranges within a controller.
        per_ctrl: dict[str, list[tuple[int, int, str]]] = {}
        for s in self.strips:
            per_ctrl.setdefault(s.controller, []).append(
                (s.pixel_offset, s.pixel_offset + s.pixel_count, s.id)
            )
        for ctrl, ranges in per_ctrl.items():
            ranges.sort()
            for i in range(1, len(ranges)):
                prev_end = ranges[i - 1][1]
                cur_start = ranges[i][0]
                if cur_start < prev_end:
                    raise ValueError(
                        f"controller {ctrl!r}: pixel range overlap between "
                        f"{ranges[i - 1][2]!r} and {ranges[i][2]!r}"
                    )
            total = self.controllers[ctrl].pixel_count
            max_end = max(r[1] for r in ranges)
            if max_end > total:
                raise ValueError(
                    f"controller {ctrl!r}: strips reach pixel {max_end} "
                    f"but pixel_count is {total}"
                )
        return self


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    text = config_path.read_text()
    data = yaml.safe_load(text)
    cfg = AppConfig.model_validate(data)
    # Resolve a relative audio_server.working_dir against the config file's
    # directory so YAML can stay portable (e.g. "../Realtime_PyAudio_FFT"
    # works on any host that mirrors the repo layout, with no hardcoded
    # absolute paths).
    wd = cfg.audio_server.working_dir
    if wd:
        wd_path = Path(wd)
        if not wd_path.is_absolute():
            wd_path = (config_path.parent / wd_path).resolve()
        cfg.audio_server.working_dir = str(wd_path)
    return cfg
