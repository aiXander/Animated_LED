"""Diagnostic-hint regression tests + the relaxed render-context contract.

The LLM's most common failure modes:
  - `ctx.x` instead of `ctx.frames.x`
  - shape mismatch in hsv_to_rgb / stack
  - writing to ctx.params

The fence-test error message must point at the fix concretely so the
auto-retry loop converges within its 2-attempt budget. Tests below pin
those messages so a future refactor can't silently regress them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ledctl.config import load_config
from ledctl.masters import MasterControls
from ledctl.surface import EffectCompileError, Runtime
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


def _topo() -> Topology:
    return Topology.from_config(load_config(DEV))


# ---- ctx.frames + ctx.pos work in render (no need to cache from init) ---- #


def test_ctx_frames_works_in_render():
    """An effect that uses `ctx.frames.x` directly in render() must work —
    the LLM keeps reaching for this idiom."""
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    src = (
        "class HueByX(Effect):\n"
        "    def render(self, ctx):\n"
        "        # NO caching from init — uses ctx.frames in render directly.\n"
        "        rgb = hsv_to_rgb(ctx.frames.x, 1.0, 1.0)\n"
        "        self.out[:] = rgb\n"
        "        return self.out\n"
    )
    rt.install_layer(
        "preview", name="hue_by_x", summary="", source=src, param_schema=[],
    )
    assert rt.preview.layers[0].name == "hue_by_x"


def test_ctx_pos_works_in_render():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    src = (
        "class FromPos(Effect):\n"
        "    def render(self, ctx):\n"
        "        # ctx.pos shape is (N, 3) — same as init.\n"
        "        x = ctx.pos[:, 0]\n"
        "        self.out[:, 0] = (x + 1.0) * 0.5\n"
        "        return self.out\n"
    )
    rt.install_layer(
        "preview", name="from_pos", summary="", source=src, param_schema=[],
    )


# ---- diagnostic hints ---- #


def test_hint_for_attribute_on_frame_context():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    src = (
        "class Bad(Effect):\n"
        "    def render(self, ctx):\n"
        "        # Wrong: ctx.x — should be ctx.frames.x.\n"
        "        x = ctx.x\n"
        "        return self.out\n"
    )
    with pytest.raises(EffectCompileError) as exc:
        rt.install_layer(
            "preview", name="bad_ctx", summary="", source=src, param_schema=[],
        )
    msg = str(exc.value)
    # The hint must point at the right idiom.
    assert "ctx.frames" in msg, msg
    assert "AttributeError" in msg


def test_hint_for_unknown_frame_name():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    src = (
        "class Bad(Effect):\n"
        "    def render(self, ctx):\n"
        "        x = ctx.frames.banana\n"   # not a real frame
        "        return self.out\n"
    )
    with pytest.raises(EffectCompileError) as exc:
        rt.install_layer(
            "preview", name="bad_frame", summary="", source=src, param_schema=[],
        )
    msg = str(exc.value)
    assert "COORDINATE FRAMES" in msg or "available" in msg.lower()


def test_hint_for_audio_typo():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    src = (
        "class Bad(Effect):\n"
        "    def render(self, ctx):\n"
        "        v = ctx.audio.volume\n"   # AudioView has no `volume`
        "        return self.out\n"
    )
    with pytest.raises(EffectCompileError) as exc:
        rt.install_layer(
            "preview", name="bad_audio", summary="", source=src, param_schema=[],
        )
    msg = str(exc.value)
    assert "AudioView" in msg or "low/mid/high" in msg


def test_hint_for_strict_param_write():
    topo = _topo()
    rt = Runtime(topo, MasterControls(), strict_params=True)
    src = (
        "class Bad(Effect):\n"
        "    def render(self, ctx):\n"
        "        ctx.params.color = '#000000'\n"
        "        return self.out\n"
    )
    with pytest.raises(EffectCompileError) as exc:
        rt.install_layer(
            "preview", name="bad_param_write", summary="", source=src,
            param_schema=[
                {"key": "color", "control": "color", "default": "#ff0000"},
            ],
        )
    msg = str(exc.value)
    assert "operator-owned" in msg or "read-only" in msg


def test_traceback_contains_llm_filename():
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    src = (
        "class Boom(Effect):\n"
        "    def render(self, ctx):\n"
        "        return ctx.frames.x  # wrong shape — not (N, 3)\n"
    )
    with pytest.raises(EffectCompileError) as exc:
        rt.install_layer(
            "preview", name="wrong_shape", summary="", source=src, param_schema=[],
        )
    msg = str(exc.value)
    # The shape-check should fire (returning (N,) instead of (N, 3)).
    assert "shape" in msg.lower()
