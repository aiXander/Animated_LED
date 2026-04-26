from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .mixer import BLEND_MODES


class PresetLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effect: str
    params: dict[str, Any] = Field(default_factory=dict)
    blend: str = "normal"
    opacity: float = Field(1.0, ge=0.0, le=1.0)


class Preset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    crossfade_seconds: float = Field(0.0, ge=0.0)
    layers: list[PresetLayer]


def load_preset(name: str, presets_dir: Path) -> Preset:
    """Load `<presets_dir>/<name>.yaml` into a validated Preset."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"invalid preset name: {name!r}")
    path = presets_dir / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"preset not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if "name" not in data:
        data["name"] = name
    preset = Preset.model_validate(data)
    for layer in preset.layers:
        if layer.blend not in BLEND_MODES:
            raise ValueError(
                f"preset {name!r}: unknown blend mode {layer.blend!r}; must be one of {BLEND_MODES}"
            )
    return preset


def list_presets(presets_dir: Path) -> list[str]:
    if not presets_dir.exists():
        return []
    return sorted(p.stem for p in presets_dir.glob("*.yaml") if p.is_file())
