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
        512, gt=0,
        description="Frames per callback block; smaller = lower latency, higher CPU",
    )
    channels: int = Field(1, ge=1, le=8, description="Capture channel count (mono = 1)")
    gain: float = Field(1.0, ge=0.0, description="Linear gain applied before features")
    smoothing: float = Field(
        0.4, ge=0.0, le=0.99,
        description="EMA factor on RMS/band energies; 0 = no smoothing, ~0.9 = sluggish",
    )


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: ProjectConfig
    server: ServerConfig = ServerConfig()
    controllers: dict[str, ControllerConfig]
    strips: list[StripConfig]
    transport: TransportConfig = TransportConfig()
    output: OutputConfig = OutputConfig()
    audio: AudioConfig = AudioConfig()

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
