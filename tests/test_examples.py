"""Each bundled example: load + fence-test + render 60 frames cleanly."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

from ledctl.config import load_config
from ledctl.masters import MasterControls
from ledctl.surface import Runtime
from ledctl.surface.base import AudioView
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"
EXAMPLES_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "ledctl" / "surface" / "examples"
)


def _topo() -> Topology:
    return Topology.from_config(load_config(DEV))


def _bundled_examples() -> list[tuple[str, str, list[dict], dict]]:
    out = []
    for sub in sorted(EXAMPLES_DIR.iterdir()):
        if not sub.is_dir():
            continue
        py = sub / "effect.py"
        ym = sub / "effect.yaml"
        if not py.is_file() or not ym.is_file():
            continue
        meta = yaml.safe_load(ym.read_text())
        out.append(
            (sub.name, py.read_text(), meta.get("params") or [], meta.get("param_values") or {})
        )
    return out


def _audio(beat: int = 0, low: float = 0.5) -> AudioView:
    return AudioView(low=low, mid=0.3, high=0.2, beat=beat, beats_since_start=0,
                     bpm=120.0, connected=True)


def test_each_example_loads_and_renders_cleanly():
    examples = _bundled_examples()
    assert examples, "no bundled examples found"
    for name, src, schema, values in examples:
        topo = _topo()
        rt = Runtime(topo, MasterControls())
        rt.install_layer(
            "live", name=name, summary="bundled", source=src,
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
