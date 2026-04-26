from pathlib import Path

import numpy as np

from ledctl.config import load_config
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


def test_topology_from_dev_config():
    cfg = load_config(DEV)
    topo = Topology.from_config(cfg)
    assert topo.pixel_count == 1800
    assert len(topo.leds) == 1800
    assert topo.positions.shape == (1800, 3)
    # x spans 30m, y spans 1m (top 0.5 / bottom -0.5).
    span_x = topo.bbox_max[0] - topo.bbox_min[0]
    span_y = topo.bbox_max[1] - topo.bbox_min[1]
    assert abs(span_x - 30.0) < 1e-3
    assert abs(span_y - 1.0) < 1e-3
    # Normalised must stay in [-1, 1] on every axis.
    assert (topo.normalised_positions <= 1.0 + 1e-5).all()
    assert (topo.normalised_positions >= -1.0 - 1e-5).all()


def test_first_led_of_each_strip_at_chain_start():
    cfg = load_config(DEV)
    topo = Topology.from_config(cfg)
    by_strip = {s.id: s for s in cfg.strips}
    # local_index 0 corresponds to the strip's `start` (chain head, near Gledopto).
    for strip in cfg.strips:
        first = topo.leds[strip.pixel_offset]
        assert first.strip_id == strip.id
        assert first.local_index == 0
        assert np.allclose(first.position, by_strip[strip.id].geometry.start, atol=1e-3)
        last = topo.leds[strip.pixel_offset + strip.pixel_count - 1]
        assert last.local_index == strip.pixel_count - 1
        assert np.allclose(last.position, by_strip[strip.id].geometry.end, atol=1e-3)


def test_reversed_flag_flips_chain_to_space_mapping():
    cfg = load_config(DEV)
    # Mutate a strip in-memory to test the flag without touching the YAML.
    cfg.strips[0].reversed = True
    topo = Topology.from_config(cfg)
    first = topo.leds[cfg.strips[0].pixel_offset]
    # With reversed=True, local_index 0 now sits at `end`, not `start`.
    assert np.allclose(first.position, cfg.strips[0].geometry.end, atol=1e-3)
