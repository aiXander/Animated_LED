from pathlib import Path

import numpy as np

from ledctl.config import load_config
from ledctl.effects.wave import WaveEffect, WaveParams
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


def test_wave_renders_within_unit_range():
    cfg = load_config(DEV)
    topo = Topology.from_config(cfg)
    eff = WaveEffect(WaveParams(), topo)
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    eff.render(0.0, out)
    assert out.shape == (topo.pixel_count, 3)
    assert (out >= 0.0).all() and (out <= 1.0 + 1e-5).all()


def test_wave_changes_over_time():
    cfg = load_config(DEV)
    topo = Topology.from_config(cfg)
    eff = WaveEffect(WaveParams(speed=1.0), topo)
    a = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    b = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    eff.render(0.0, a)
    eff.render(0.25, b)
    assert not np.allclose(a, b), "wave should travel between frames"
