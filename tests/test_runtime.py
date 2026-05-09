"""Runtime — slots, layered composition, crossfade, master output stage, fence test."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ledctl.config import load_config
from ledctl.masters import MasterControls
from ledctl.surface import Runtime
from ledctl.surface.base import AudioView
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"

_PULSE = """\
class PulseMono(Effect):
    def init(self, ctx):
        self._scratch = np.zeros(ctx.n, dtype=np.float32)
    def render(self, ctx):
        col = hex_to_rgb(ctx.params.color)
        self.out[:] = col[None, :]
        return self.out
"""

_PARAMS_PULSE = [
    {"key": "color", "control": "color", "default": "#ff0000"},
]


def _topo() -> Topology:
    return Topology.from_config(load_config(DEV))


def _audio_zero() -> AudioView:
    return AudioView()


def test_install_into_live_then_preview():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer(
        "live", name="pulse_mono", summary="", source=_PULSE,
        param_schema=_PARAMS_PULSE,
    )
    rt.install_layer(
        "preview", name="pulse_mono", summary="", source=_PULSE,
        param_schema=_PARAMS_PULSE,
    )
    snap = rt.snapshot()
    assert snap["live"]["layers"][0]["name"] == "pulse_mono"
    assert snap["preview"]["layers"][0]["name"] == "pulse_mono"


def test_render_returns_two_buffers_in_design_mode():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt.install_layer("preview", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt._cf = None
    rt.mode = "design"
    live, sim = rt.render(wall_t=0.1, dt=1/60, t_eff=0.1, audio=_audio_zero())
    assert live.shape == (topo.pixel_count, 3)
    assert sim.shape == (topo.pixel_count, 3)
    # In design mode, sim and live are distinct buffers (content can match
    # when both slots run the same effect; the buffers themselves differ).
    assert sim is not live


def test_render_lives_share_buffer_in_live_mode():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt._cf = None
    rt.mode = "live"
    live, sim = rt.render(wall_t=0.1, dt=1/60, t_eff=0.1, audio=_audio_zero())
    assert sim is live  # zero-copy in live mode


def test_promote_starts_crossfade():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.crossfade_seconds = 1.0
    # live = red
    src_red = _PULSE
    src_blue = _PULSE.replace("hex_to_rgb(ctx.params.color)", "hex_to_rgb('#0000ff')")
    rt.install_layer("live", name="red", summary="", source=src_red,
                     param_schema=[{"key": "color", "control": "color", "default": "#ff0000"}])
    rt.install_layer("preview", name="blue", summary="", source=src_blue, param_schema=[])
    rt._cf = None
    rt.promote()
    assert rt._cf is not None
    assert rt._cf.duration == 1.0


def test_master_brightness_scales_output():
    topo = _topo()
    rt = Runtime(topo, MasterControls(brightness=0.5))
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt._cf = None
    live, _ = rt.render(wall_t=0.0, dt=1/60, t_eff=0.0, audio=_audio_zero())
    # Default red → R near 1.0 before brightness; after 0.5 brightness → ~0.5
    assert live[:, 0].max() <= 0.55
    assert live[:, 0].max() >= 0.45


def test_render_crash_does_not_kill_runtime():
    """Install a healthy effect, then swap its instance for one that always
    raises in render(). The runtime should catch, log, and zero the buffer."""
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt._cf = None
    layer = rt.live.layers[0]

    class ExplodingEffect(layer.instance.__class__):
        def render(self, ctx):
            raise ValueError("boom")

    new_inst = ExplodingEffect()
    new_inst._setup(rt.n)
    new_inst.init(rt._build_init_ctx())
    layer.instance = new_inst

    live, _ = rt.render(wall_t=0.0, dt=1/60, t_eff=0.0, audio=_audio_zero())
    assert live.shape == (topo.pixel_count, 3)
    # Output is zeros after crash + masters applied.
    assert np.all(live == 0.0)
    assert layer.consecutive_failures >= 1


def test_layer_blend_normal_replaces_below():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    # Add a second layer (replace=False)
    rt.install_layer("live", name="pulse_mono", summary="", source=_PULSE,
                     param_schema=[{"key": "color", "control": "color", "default": "#0000ff"}],
                     replace=False)
    rt._cf = None
    assert len(rt.live.layers) == 2
    # Set the second layer's color to blue
    rt.live.layers[1].params.update({"color": "#0000ff"})
    live, _ = rt.render(wall_t=0.0, dt=1/60, t_eff=0.0, audio=_audio_zero())
    # Top layer is normal opacity 1.0 — should fully cover red layer with blue.
    assert live[:, 2].max() > 0.9   # B channel high
    assert live[:, 0].max() < 0.1   # R channel low


def test_remove_and_reorder_layer():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer("live", name="a", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE)
    rt.install_layer("live", name="b", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE, replace=False)
    rt.install_layer("live", name="c", summary="", source=_PULSE,
                     param_schema=_PARAMS_PULSE, replace=False)
    assert [L.name for L in rt.live.layers] == ["a", "b", "c"]
    rt.reorder_layer("live", 0, 2)
    assert [L.name for L in rt.live.layers] == ["b", "c", "a"]
    rt.remove_layer("live", 1)
    assert [L.name for L in rt.live.layers] == ["b", "a"]
