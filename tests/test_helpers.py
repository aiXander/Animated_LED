"""Helper-function regression tests — these are the surface area the LLM
calls into, so a shape bug here causes every effect to fail at fence-test."""

from __future__ import annotations

import numpy as np
import pytest

from ledctl.surface.helpers import (
    clip01,
    gauss,
    hex_to_rgb,
    hsv_to_rgb,
    lerp,
    palette_lerp,
    pulse,
    tri,
    wrap_dist,
)

# ---- hsv_to_rgb shape behaviour (the most-failed helper in the LLM) ---- #


def test_hsv_to_rgb_scalar():
    out = hsv_to_rgb(0.0, 1.0, 1.0)
    assert out.shape == (3,)
    assert out.dtype == np.float32
    np.testing.assert_allclose(out, [1.0, 0.0, 0.0], atol=1e-3)


def test_hsv_to_rgb_array_h_scalar_sv():
    """The exact shape mismatch the LLM kept hitting:
    `hsv_to_rgb(per_led_hue_array, 1.0, 1.0)` returns (N, 3)."""
    h = np.linspace(0.0, 1.0, 7, dtype=np.float32)
    out = hsv_to_rgb(h, 1.0, 1.0)
    assert out.shape == (7, 3)
    assert out.dtype == np.float32
    # h=0 → red, h=1/3 → green, h=2/3 → blue.
    np.testing.assert_allclose(out[0], [1.0, 0.0, 0.0], atol=1e-3)


def test_hsv_to_rgb_array_h_scalar_v_array_s():
    h = np.array([0.0, 0.33, 0.66], dtype=np.float32)
    s = np.array([0.5, 1.0, 0.8], dtype=np.float32)
    out = hsv_to_rgb(h, s, 1.0)
    assert out.shape == (3, 3)


def test_hsv_to_rgb_2d_h():
    h = np.zeros((4, 5), dtype=np.float32)
    out = hsv_to_rgb(h, 1.0, 1.0)
    assert out.shape == (4, 5, 3)


# ---- other broadcasting helpers ---- #


def test_lerp_broadcasts():
    a = np.zeros(8, dtype=np.float32)
    b = np.ones(8, dtype=np.float32)
    out = lerp(a, b, 0.5)
    assert out.shape == (8,)
    np.testing.assert_allclose(out, 0.5)


def test_clip01():
    out = clip01(np.array([-0.5, 0.3, 1.7]))
    np.testing.assert_allclose(out, [0.0, 0.3, 1.0])


def test_gauss_peak_one_at_zero():
    out = gauss(np.array([0.0]), 0.1)
    np.testing.assert_allclose(out, [1.0], atol=1e-5)


def test_pulse_zero_outside_window():
    x = np.array([-1.0, 0.0, 1.0])
    out = pulse(x, width=0.5)
    assert out[0] == 0.0
    assert out[2] == 0.0
    assert out[1] > 0.99


def test_tri_period():
    np.testing.assert_allclose(tri(0.0), 0.0)
    np.testing.assert_allclose(tri(0.5), 1.0)


def test_wrap_dist_array():
    a = np.array([0.0, 0.0])
    b = np.array([0.1, 0.9])
    d = wrap_dist(a, b)
    np.testing.assert_allclose(d, [0.1, -0.1], atol=1e-5)


def test_palette_lerp_array_t():
    stops = [(0.0, "#000000"), (1.0, "#ffffff")]
    out = palette_lerp(stops, np.array([0.0, 0.5, 1.0]))
    assert out.shape == (3, 3)
    np.testing.assert_allclose(out[2], [1.0, 1.0, 1.0])


def test_hex_to_rgb_3_digit():
    np.testing.assert_allclose(hex_to_rgb("#f00"), [1.0, 0.0, 0.0])


def test_hex_to_rgb_bad():
    with pytest.raises(ValueError):
        hex_to_rgb("#abcd")


# ---- fence-test now surfaces the offending line ---- #


def test_fence_traceback_pinpoints_failing_line():
    """When an effect crashes inside `render`, the operator-visible error
    must include the LLM source filename so the next prompt can fix it."""
    from pathlib import Path

    from ledctl.config import load_config
    from ledctl.masters import MasterControls
    from ledctl.surface import EffectCompileError, Runtime
    from ledctl.topology import Topology

    DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"
    topo = Topology.from_config(load_config(DEV))
    rt = Runtime(topo, MasterControls())
    src = (
        "class Boom(Effect):\n"
        "    def init(self, ctx):\n"
        "        self.h = ctx.frames.x\n"
        "    def render(self, ctx):\n"
        "        # The historical bug: scalar s/v with array h.\n"
        "        rgb = hsv_to_rgb(self.h, 1.0, 1.0)\n"
        "        # Now hsv_to_rgb broadcasts properly, so make this fail another\n"
        "        # way — concatenating arrays of different ndim:\n"
        "        np.stack([self.h, np.array(0.5)])\n"
        "        return self.out\n"
    )
    with pytest.raises(EffectCompileError) as exc:
        rt.install_layer(
            "preview", name="boom_shape", summary="", source=src,
            param_schema=[],
        )
    msg = str(exc.value)
    assert "render() crashed" in msg
    # Filename of the LLM's source is in the traceback.
    assert "<llm:boom_shape>" in msg


def test_fence_no_longer_fails_on_hsv_broadcast():
    """The original failure case must now succeed end-to-end."""
    from pathlib import Path

    from ledctl.config import load_config
    from ledctl.masters import MasterControls
    from ledctl.surface import Runtime
    from ledctl.topology import Topology

    DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"
    topo = Topology.from_config(load_config(DEV))
    rt = Runtime(topo, MasterControls())
    src = (
        "class HueRamp(Effect):\n"
        "    def init(self, ctx):\n"
        "        self.h = ctx.frames.x\n"
        "    def render(self, ctx):\n"
        "        rgb = hsv_to_rgb(self.h, 1.0, 1.0)\n"
        "        self.out[:] = rgb\n"
        "        return self.out\n"
    )
    rt.install_layer(
        "preview", name="hue_ramp", summary="", source=src, param_schema=[],
    )
    assert rt.preview.layers[0].name == "hue_ramp"
