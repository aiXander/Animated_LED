from pathlib import Path

import numpy as np
import pytest

from ledctl.config import load_config
from ledctl.effects import PaletteSpec, ScrollEffect, ScrollParams
from ledctl.mixer import Layer, Mixer, _blend_into
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


@pytest.fixture
def topo() -> Topology:
    return Topology.from_config(load_config(DEV))


def _solid_layer(topo: Topology, color: str, blend: str = "normal", opacity: float = 1.0) -> Layer:
    """A static, single-colour layer built from `scroll` with a mono palette."""
    palette = PaletteSpec(name=f"mono_{color.lstrip('#')}")
    return Layer(
        effect=ScrollEffect(ScrollParams(speed=0.0, palette=palette), topo),
        blend=blend,
        opacity=opacity,
    )


def test_empty_stack_renders_black(topo: Topology):
    m = Mixer(topo.pixel_count)
    out = np.ones((topo.pixel_count, 3), dtype=np.float32)
    m.render(0.0, out)
    assert (out == 0.0).all()


def test_single_layer_normal(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ff8000"))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(0.0, out)
    assert np.allclose(out[0], [1.0, 128.0 / 255.0, 0.0], atol=1e-6)


def test_blackout_zeros_output(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    m.blackout = True
    out = np.ones((topo.pixel_count, 3), dtype=np.float32)
    m.render(0.0, out)
    assert (out == 0.0).all()


def test_add_blend_sums(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#400000"))            # base ≈0.251 R
    m.layers.append(_solid_layer(topo, "#400000", "add", 1.0)) # +0.251 R -> ≈0.502 R
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(0.0, out)
    assert np.allclose(out[:, 0], 2 * (64.0 / 255.0), atol=1e-3)
    assert np.allclose(out[:, 1:], 0.0, atol=1e-6)


def test_add_blend_clips_at_one(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    m.layers.append(_solid_layer(topo, "#ffffff", "add", 1.0))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(0.0, out)
    assert (out <= 1.0 + 1e-5).all()
    assert np.allclose(out, 1.0, atol=1e-6)


def test_multiply_darkens(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    m.layers.append(_solid_layer(topo, "#808080", "multiply", 1.0))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(0.0, out)
    # white * 0.5 ≈ 0.5
    assert np.allclose(out, 128.0 / 255.0, atol=2e-3)


def test_screen_brightens(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#404040"))
    m.layers.append(_solid_layer(topo, "#404040", "screen", 1.0))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(0.0, out)
    # screen(a, b) = 1 - (1-a)(1-b); for a=b=0.25: 1 - 0.75*0.75 = 0.4375
    assert np.allclose(out, 0.4375, atol=2e-3)


def test_normal_opacity_lerps(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#000000"))
    m.layers.append(_solid_layer(topo, "#ffffff", "normal", 0.5))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(0.0, out)
    assert np.allclose(out, 0.5, atol=2e-3)


def test_unknown_blend_mode_raises(topo: Topology):
    dst = np.zeros((4, 3), dtype=np.float32)
    src = np.ones((4, 3), dtype=np.float32)
    with pytest.raises(ValueError, match="unknown blend"):
        _blend_into(dst, src, "subtract", 1.0)


def test_crossfade_transitions(topo: Topology):
    m = Mixer(topo.pixel_count)
    # current = white
    m.layers.append(_solid_layer(topo, "#ffffff"))
    # crossfade to black over 1 second starting at t=0
    new_layers = [_solid_layer(topo, "#000000")]
    m.crossfade_to(new_layers, duration=1.0, t=0.0)
    assert m.is_crossfading

    out0 = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    out_mid = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    out_end = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(0.0, out0)        # alpha=0 -> white
    m.render(0.5, out_mid)     # alpha=0.5 -> mid grey
    m.render(1.5, out_end)     # past duration -> black

    assert np.allclose(out0, 1.0, atol=1e-3)
    assert np.allclose(out_mid, 0.5, atol=1e-2)
    assert np.allclose(out_end, 0.0, atol=1e-3)
    assert not m.is_crossfading


def test_crossfade_zero_duration_is_instant(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    m.crossfade_to([_solid_layer(topo, "#000000")], duration=0.0, t=0.0)
    assert not m.is_crossfading
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(0.0, out)
    assert np.allclose(out, 0.0, atol=1e-3)
