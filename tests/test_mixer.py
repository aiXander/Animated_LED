from pathlib import Path

import numpy as np
import pytest

from ledctl.config import load_config
from ledctl.masters import MasterControls, RenderContext
from ledctl.mixer import Layer, Mixer, _blend_into
from ledctl.surface import Compiler, LayerSpec, NodeSpec
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


@pytest.fixture
def topo() -> Topology:
    return Topology.from_config(load_config(DEV))


def _solid_layer(
    topo: Topology, color: str, blend: str = "normal", opacity: float = 1.0
) -> Layer:
    """A static, single-colour layer built via the surface (palette_lookup
    on a constant scalar with a mono palette)."""
    pal = f"mono_{color.lstrip('#')}"
    spec = LayerSpec(
        node=NodeSpec(
            kind="palette_lookup",
            params={
                "scalar": {"kind": "constant", "params": {"value": 0.0}},
                "palette": {"kind": "palette_named", "params": {"name": pal}},
            },
        ),
        blend=blend,
        opacity=opacity,
    )
    compiled = Compiler(topo).compile_layer(spec)
    return Layer(
        node=compiled.node,
        spec_node=spec.node.model_dump(),
        blend=compiled.blend,
        opacity=compiled.opacity,
    )


def _ctx() -> RenderContext:
    return RenderContext(t=0.0, wall_t=0.0, audio=None, masters=MasterControls())


def test_empty_stack_renders_black(topo: Topology):
    m = Mixer(topo.pixel_count)
    out = np.ones((topo.pixel_count, 3), dtype=np.float32)
    m.render(_ctx(), out)
    assert (out == 0.0).all()


def test_single_layer_normal(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ff8000"))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(_ctx(), out)
    assert np.allclose(out[0], [1.0, 128.0 / 255.0, 0.0], atol=1e-6)


def test_blackout_zeros_output(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    m.blackout = True
    out = np.ones((topo.pixel_count, 3), dtype=np.float32)
    m.render(_ctx(), out)
    assert (out == 0.0).all()


def test_add_blend_sums(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#400000"))
    m.layers.append(_solid_layer(topo, "#400000", "add", 1.0))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(_ctx(), out)
    assert np.allclose(out[:, 0], 2 * (64.0 / 255.0), atol=1e-3)
    assert np.allclose(out[:, 1:], 0.0, atol=1e-6)


def test_add_blend_clips_at_one(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    m.layers.append(_solid_layer(topo, "#ffffff", "add", 1.0))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(_ctx(), out)
    assert (out <= 1.0 + 1e-5).all()
    assert np.allclose(out, 1.0, atol=1e-6)


def test_multiply_darkens(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    m.layers.append(_solid_layer(topo, "#808080", "multiply", 1.0))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(_ctx(), out)
    assert np.allclose(out, 128.0 / 255.0, atol=2e-3)


def test_screen_brightens(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#404040"))
    m.layers.append(_solid_layer(topo, "#404040", "screen", 1.0))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(_ctx(), out)
    assert np.allclose(out, 0.4375, atol=2e-3)


def test_normal_opacity_lerps(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#000000"))
    m.layers.append(_solid_layer(topo, "#ffffff", "normal", 0.5))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(_ctx(), out)
    assert np.allclose(out, 0.5, atol=2e-3)


def test_unknown_blend_mode_raises():
    dst = np.zeros((4, 3), dtype=np.float32)
    src = np.ones((4, 3), dtype=np.float32)
    with pytest.raises(ValueError, match="unknown blend"):
        _blend_into(dst, src, "subtract", 1.0)


def test_crossfade_transitions(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    new_layers = [_solid_layer(topo, "#000000")]
    m.crossfade_to(new_layers, duration=1.0, wall_t=0.0)
    assert m.is_crossfading

    out0 = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    out_mid = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    out_end = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(RenderContext(t=0.0, wall_t=0.0, masters=MasterControls()), out0)
    m.render(RenderContext(t=0.5, wall_t=0.5, masters=MasterControls()), out_mid)
    m.render(RenderContext(t=1.5, wall_t=1.5, masters=MasterControls()), out_end)

    assert np.allclose(out0, 1.0, atol=1e-3)
    assert np.allclose(out_mid, 0.5, atol=1e-2)
    assert np.allclose(out_end, 0.0, atol=1e-3)
    assert not m.is_crossfading


def test_crossfade_zero_duration_is_instant(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    m.crossfade_to([_solid_layer(topo, "#000000")], duration=0.0, wall_t=0.0)
    assert not m.is_crossfading
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(_ctx(), out)
    assert np.allclose(out, 0.0, atol=1e-3)


# ---- master output stage ----


def test_master_brightness_dims_uniformly(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(
        RenderContext(masters=MasterControls(brightness=0.5)),
        out,
    )
    assert np.allclose(out, 0.5, atol=1e-5)


def test_master_saturation_collapses_to_grey(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ff0000"))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(
        RenderContext(masters=MasterControls(saturation=0.0)),
        out,
    )
    # Pure red → luminance 0.2126 → all channels equal
    expected = 0.2126
    assert np.allclose(out[0], [expected, expected, expected], atol=1e-3)


def test_master_brightness_zero_blacks_out(topo: Topology):
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    m.render(
        RenderContext(masters=MasterControls(brightness=0.0)),
        out,
    )
    assert (out == 0.0).all()


def test_master_speed_does_not_affect_crossfade_alpha(topo: Topology):
    """speed=0 should freeze pattern motion but the crossfade still runs on
    wall_t. (The mixer test here directly drives wall_t.)"""
    m = Mixer(topo.pixel_count)
    m.layers.append(_solid_layer(topo, "#ffffff"))
    m.crossfade_to([_solid_layer(topo, "#000000")], duration=1.0, wall_t=0.0)
    out = np.zeros((topo.pixel_count, 3), dtype=np.float32)
    # ctx.t = 0 (frozen) but wall_t = 0.5 → crossfade alpha = 0.5.
    m.render(
        RenderContext(t=0.0, wall_t=0.5, masters=MasterControls(freeze=True)),
        out,
    )
    assert np.allclose(out, 0.5, atol=1e-2)
