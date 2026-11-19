"""Each effect that ships in git (the seed folder, the live library and the
examples the LLM prompt shows): install through the real compile pipeline, then
render 60 frames cleanly, on a three-leg and on a four-leg rig. And the prompt
itself describes the rig it is built for."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import yaml

from ledctl.api.server import DEFAULT_EFFECTS_DIR
from ledctl.config import AppConfig
from ledctl.masters import MasterControls
from ledctl.surface import Runtime, build_system_prompt
from ledctl.surface.base import AudioView
from ledctl.surface.prompt import LLM_EXAMPLES_DIR
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"
EXAMPLES_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "ledctl" / "surface" / "examples"
)


def _topo(n_legs: int) -> Topology:
    """The dev rig with its legs replaced by `n_legs` evenly spread spokes."""
    raw = yaml.safe_load(DEV.read_text())
    leg = raw["strips"][0]
    raw["strips"] = [
        {**leg, "id": f"leg_{i + 1}", "output": i + 1, "pixel_offset": i * leg["pixel_count"],
         "geometry": {"type": "spoke", "angle_deg": 90 + i * 360 / n_legs}}
        for i in range(n_legs)
    ]
    return Topology.from_config(AppConfig.model_validate(raw))


def _bundled_examples() -> list[tuple[str, str, list[dict], dict]]:
    """The seed folder, the live library (`config/effects/star/`, what every
    device pulls) and the prompt's examples (what the LLM copies from)."""
    out = []
    for root in (EXAMPLES_DIR, DEFAULT_EFFECTS_DIR, LLM_EXAMPLES_DIR):
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            py = sub / "effect.py"
            ym = sub / "effect.yaml"
            if not py.is_file() or not ym.is_file():
                continue
            meta = yaml.safe_load(ym.read_text())
            out.append((
                f"{root.name}/{sub.name}", py.read_text(),
                meta.get("params") or [], meta.get("param_values") or {},
            ))
    return out


def _audio(beat: int = 0, low: float = 0.5) -> AudioView:
    return AudioView(low=low, mid=0.3, high=0.2, beat=beat, beats_since_start=0,
                     bpm=120.0, connected=True)


@pytest.mark.parametrize("n_legs", [3, 4])
def test_each_example_loads_and_renders_cleanly(n_legs):
    examples = _bundled_examples()
    assert examples, "no bundled examples found"
    topo = _topo(n_legs)
    assert topo.pixel_count == n_legs * 450
    for name, src, schema, values in examples:
        rt = Runtime(topo, MasterControls())
        rt.install_layer(
            "live", name=name.split("/")[-1], summary="bundled", source=src,
            param_schema=schema, param_values=values,
        )
        rt._cf = None
        for i in range(60):
            audio = _audio(beat=1 if i % 8 == 0 else 0,
                           low=0.4 + 0.4 * float(np.sin(i * 0.3)))
            live, _ = rt.render(
                wall_t=i * 1/60, dt=1/60, t_eff=i * 1/60, audio=audio,
            )
            assert live.shape == (topo.pixel_count, 3), name
            assert live.dtype == np.float32, name
            assert np.isfinite(live).all(), name
            assert live.min() >= -1e-6 and live.max() <= 1.0 + 1e-6, name


@pytest.mark.parametrize("n_legs", [3, 4])
def test_prompt_describes_the_hub_rig_it_is_built_for(n_legs):
    """The LLM's picture of the rig is generated from the topology: the right
    number of legs, no word left from the rectangle this rig replaced (in the
    prose or in an example), and inside the size limit."""
    prompt = build_system_prompt(topology=_topo(n_legs), runtime=None)
    assert f"on {n_legs} legs" in prompt
    assert f"leg {n_legs - 1}:" in prompt and f"leg {n_legs}:" not in prompt
    for word in ("rectangle", " row", "u_loop", "side_", "axial_", "corner_dist"):
        assert word not in prompt.lower(), word
    assert len(prompt) <= 26_000
