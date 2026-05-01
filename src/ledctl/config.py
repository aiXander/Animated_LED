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


class AudioConfig(BaseModel):
    """Audio capture settings (Phase 5).

    `device` is the input source the install listens to. Save by name (string)
    rather than index — indices reshuffle between sessions and machines, names
    don't (within a host). The /audio web route is the canonical way to pick it.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(True, description="Whether to start the capture stream on boot")
    device: str | int | None = Field(
        None,
        description=(
            "Input device — name (preferred), index, or null for the system default. "
            "Edit interactively at /audio."
        ),
    )
    samplerate: int = Field(48000, gt=0, description="Capture sample rate in Hz")
    blocksize: int = Field(
        128, gt=0,
        description=(
            "Frames per PortAudio callback. Sets the HW capture latency floor; "
            "smaller = lower latency, slightly more callbacks/sec. 128 @ 48 kHz "
            "≈ 2.67 ms is comfortable on M-series Macs."
        ),
    )
    fft_window: int = Field(
        512, gt=0,
        description=(
            "FFT length over the most-recent samples; sets frequency resolution. "
            "Must be >= blocksize. Larger windows give finer bins but slower "
            "transient response (a kick ramps in over multiple updates)."
        ),
    )
    channels: int = Field(1, ge=1, le=8, description="Capture channel count (mono = 1)")
    gain: float = Field(1.0, ge=0.0, description="Linear gain applied before features")

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_fields(cls, data: object) -> object:
        # `smoothing` was a callback-side EMA factor in the original Phase-5
        # implementation. After splitting source/analyser, all temporal smoothing
        # moved into the per-binding modulator envelopes (effects/modulator.py),
        # so a global EMA on the audio state is no longer wanted. Silently drop
        # it on read so existing YAMLs keep loading; `extra="forbid"` would
        # otherwise reject the unknown key.
        if isinstance(data, dict) and "smoothing" in data:
            data = {k: v for k, v in data.items() if k != "smoothing"}
        return data

    @model_validator(mode="after")
    def _check_window_vs_block(self) -> "AudioConfig":
        if self.fft_window < self.blocksize:
            raise ValueError(
                f"audio.fft_window ({self.fft_window}) must be >= "
                f"audio.blocksize ({self.blocksize})"
            )
        return self


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
        1,
        ge=0,
        le=5,
        description=(
            "On a failed `update_leds` tool call (validation/compile error), "
            "automatically re-prompt the LLM up to this many extra times. "
            "The failed tool result stays in the rolling buffer, so the model "
            "sees the structured error path on each retry and can self-correct. "
            "0 disables retries (the operator sees the first failure)."
        ),
    )


class MastersConfig(BaseModel):
    """Operator-owned room knobs (refactor §7).

    Persistence shim for the operator UI's `PATCH /masters` with
    `persist=true`. The render-time copy lives on `Engine.masters` and is
    seeded from this block on boot.
    """

    model_config = ConfigDict(extra="forbid")
    brightness: float = Field(1.0, ge=0.0, le=1.0)
    speed: float = Field(1.0, ge=0.0, le=3.0)
    audio_reactivity: float = Field(1.0, ge=0.0, le=3.0)
    saturation: float = Field(1.0, ge=0.0, le=1.0)
    freeze: bool = False


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: ProjectConfig
    server: ServerConfig = ServerConfig()
    controllers: dict[str, ControllerConfig]
    strips: list[StripConfig]
    transport: TransportConfig = TransportConfig()
    output: OutputConfig = OutputConfig()
    audio: AudioConfig = AudioConfig()
    agent: AgentConfig = AgentConfig()
    masters: MastersConfig = MastersConfig()

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
    text = Path(path).read_text()
    data = yaml.safe_load(text)
    return AppConfig.model_validate(data)
