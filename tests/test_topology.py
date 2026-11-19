from pathlib import Path

import numpy as np
import pytest

from ledctl.api.server import _write_config_yaml
from ledctl.config import AppConfig, load_config
from ledctl.surface.frames import FRAME_DESCRIPTIONS
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


def test_topology_from_dev_config():
    cfg = load_config(DEV)
    topo = Topology.from_config(cfg)
    n = sum(s.pixel_count for s in cfg.strips)
    assert topo.pixel_count == n
    assert len(topo.leds) == n
    assert topo.positions.shape == (n, 3)
    # One uniform scale about the origin: nothing lies farther out than 1.
    dist = np.linalg.norm(topo.normalised_positions, axis=1)
    assert abs(float(dist.max()) - 1.0) < 1e-5


def test_first_led_of_each_strip_at_chain_start():
    cfg = load_config(DEV)
    topo = Topology.from_config(cfg)
    by_strip = {s.id: s for s in cfg.strips}
    # local_index 0 corresponds to the strip's `start` (chain head, near Gledopto).
    for strip in cfg.strips:
        first = topo.leds[strip.pixel_offset]
        assert first.strip_id == strip.id
        assert first.local_index == 0
        assert np.allclose(first.position, by_strip[strip.id].start, atol=1e-3)
        last = topo.leds[strip.pixel_offset + strip.pixel_count - 1]
        assert last.local_index == strip.pixel_count - 1
        assert np.allclose(last.position, by_strip[strip.id].end, atol=1e-3)


def test_reversed_flag_flips_chain_to_space_mapping():
    cfg = load_config(DEV)
    # Mutate a strip in-memory to test the flag without touching the YAML.
    cfg.strips[0].reversed = True
    topo = Topology.from_config(cfg)
    first = topo.leds[cfg.strips[0].pixel_offset]
    # With reversed=True, local_index 0 now sits at `end`, not `start`.
    assert np.allclose(first.position, cfg.strips[0].end, atol=1e-3)


# Frames that predate the hub rig. Effects written for the rectangle read them,
# so they must keep computing on any layout.
LEGACY_FRAMES = (
    "x", "y", "z", "signed_x", "signed_y", "signed_z", "radius", "angle",
    "u_loop", "u_loop_signed", "side_top", "side_bottom", "side_signed",
    "axial_dist", "axial_signed", "corner_dist", "strip_id", "chain_index",
    "distance",
)


@pytest.mark.parametrize("angles_deg", [(330, 90, 210), (270, 0, 180, 90)])
def test_hub_rig_geometry(angles_deg, tmp_path):
    """A hub with K legs, given out of angular order, the first leg half length."""
    cfg = load_config(DEV)
    k = len(angles_deg)
    strips = [
        {
            "id": f"leg_{i + 1}", "controller": "gledopto_main",
            "pixel_offset": i * 450, "pixel_count": 225 if i == 0 else 450,
            "geometry": {"type": "spoke", "angle_deg": a},
        }
        for i, a in enumerate(angles_deg)
    ]
    cfg = AppConfig.model_validate({**cfg.model_dump(), "strips": strips})
    topo = Topology.from_config(cfg)
    f = topo.derived

    by_direction = sorted(range(k), key=lambda i: angles_deg[i])
    for i, strip in enumerate(cfg.strips):
        lo, hi = strip.pixel_offset, strip.pixel_offset + strip.pixel_count
        assert f["radius"][lo] == 0.0                       # first LED on the hub
        tip = 0.5 if i == 0 else 1.0                        # longest legs reach 1
        assert f["radius"][hi - 1] == pytest.approx(tip, abs=2e-3)
        # One direction per leg, its first LED included (atan2(0, 0) would be 0).
        assert np.all(f["angle"][lo:hi] == f["angle"][lo])
        assert f["angle"][lo] == pytest.approx(angles_deg[i] / 360.0, abs=1e-6)
        rank = by_direction.index(i)
        assert np.all(f["leg"][lo:hi] == rank)
        assert f["leg_phase"][lo] == pytest.approx(rank / k)

    for name in LEGACY_FRAMES:
        assert f[name].shape == (topo.pixel_count,), name
        assert np.isfinite(f[name]).all(), name
    assert set(f) == set(FRAME_DESCRIPTIONS)

    # A layout saved by PUT /config must come back as the same spokes.
    path = tmp_path / "config.yaml"
    _write_config_yaml(path, cfg)
    assert load_config(path).strips == cfg.strips
