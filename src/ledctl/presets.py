"""Operator-saved layer stacks loaded from YAML.

Preset YAML shape:

    crossfade_seconds: 1.5
    masters:
      brightness: 1.0
      speed: 1.0
      audio_reactivity: 1.0
      saturation: 1.0
      freeze: false
    layers:
      - blend: normal
        opacity: 1.0
        node:
          kind: palette_lookup
          params:
            scalar: { kind: wave, params: { axis: x, speed: 0.3 } }
            palette: fire

The `masters` block is part of the saved snapshot but is loaded *only when
explicitly requested* by the apply call (it's a separate operator decision —
the visual stack is the primary unit of recall, the room knobs are not).

`load_preset` validates the file against `surface.LayerSpec` so unknown keys
fail at load with the same structured error the agent sees from `update_leds`.
"""

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .mixer import BLEND_MODES
from .surface import LayerSpec

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,39}$")


class PresetMasters(BaseModel):
    """Snapshot of master controls saved alongside a preset.

    Bounds mirror `MasterControls.clamped()` so a hand-edited YAML can't
    smuggle out-of-range values past the UI sliders.
    """

    model_config = ConfigDict(extra="forbid")
    brightness: float = Field(1.0, ge=0.0, le=1.0)
    speed: float = Field(1.0, ge=0.0, le=3.0)
    audio_reactivity: float = Field(1.0, ge=0.0, le=3.0)
    audio_feature_cleaning: float = Field(1.0, ge=0.0, le=1.0)
    saturation: float = Field(1.0, ge=0.0, le=1.0)
    freeze: bool = False


class Preset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    crossfade_seconds: float = Field(0.0, ge=0.0)
    masters: PresetMasters = Field(default_factory=PresetMasters)
    layers: list[LayerSpec]


def validate_preset_name(name: str) -> str:
    """Reject names that would escape the presets dir or look ugly on disk."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"invalid preset name: {name!r} "
            "(letters/digits/_/-, must start with alphanumeric, max 40 chars)"
        )
    return name


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
                f"preset {name!r}: unknown blend mode {layer.blend!r}; "
                f"must be one of {BLEND_MODES}"
            )
    return preset


def list_presets(presets_dir: Path) -> list[str]:
    if not presets_dir.exists():
        return []
    return sorted(p.stem for p in presets_dir.glob("*.yaml") if p.is_file())


def save_preset(
    name: str,
    presets_dir: Path,
    crossfade_seconds: float,
    layers: list[dict[str, Any]],
    masters: dict[str, Any],
    overwrite: bool = True,
) -> Path:
    """Write `<presets_dir>/<name>.yaml` with the operator's current state.

    `layers` should be the dict form returned by `Engine.layer_state()` —
    each entry already carries `node`, `blend`, `opacity`. The Preset model
    re-validates everything before we touch disk so we never persist a stack
    the loader would refuse to read.
    """
    validate_preset_name(name)
    preset = Preset.model_validate(
        {
            "name": name,
            "crossfade_seconds": crossfade_seconds,
            "masters": masters,
            "layers": layers,
        }
    )
    path = presets_dir / f"{name}.yaml"
    if path.exists() and not overwrite:
        raise FileExistsError(f"preset already exists: {path}")
    presets_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "crossfade_seconds": preset.crossfade_seconds,
        "masters": preset.masters.model_dump(),
        "layers": [
            {
                "blend": layer.blend,
                "opacity": layer.opacity,
                "node": layer.node.model_dump(),
            }
            for layer in preset.layers
        ],
    }
    text = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return path
