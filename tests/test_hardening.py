"""v1.1 hardening tests: dunder reject, dt clamp, init budget, strict params,
30-frame fence catches more bugs, auto-retry."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from ledctl.agent.client import CompletionResult
from ledctl.api.server import create_app
from ledctl.config import load_config
from ledctl.masters import MasterControls
from ledctl.surface import Runtime
from ledctl.surface.base import AudioView
from ledctl.surface.runtime import build_runtime_namespace
from ledctl.surface.sandbox import EffectCompileError, compile_effect
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


def _topo() -> Topology:
    return Topology.from_config(load_config(DEV))


def _ns(name: str = "x") -> dict:
    return build_runtime_namespace(name)


# ---- 1. Dunder rejection ---- #


def test_sandbox_rejects_dunder_class():
    src = (
        "class X(Effect):\n"
        "    def render(self, ctx):\n"
        "        x = self.__class__\n"
        "        return self.out\n"
    )
    with pytest.raises(EffectCompileError, match="dunder attribute access"):
        compile_effect(src, "x", _ns())


def test_sandbox_rejects_dunder_globals():
    src = (
        "class X(Effect):\n"
        "    def render(self, ctx):\n"
        "        return self.render.__globals__['os']\n"
    )
    with pytest.raises(EffectCompileError, match="dunder attribute access"):
        compile_effect(src, "x", _ns())


def test_sandbox_allows_name_dunder():
    # __name__ is the one allowance; some helpers read it.
    src = (
        "class X(Effect):\n"
        "    def render(self, ctx):\n"
        "        _n = __name__\n"
        "        return self.out\n"
    )
    cls = compile_effect(src, "x", _ns())
    assert cls is not None


# ---- 2. Init budget ---- #


def test_install_rejects_slow_init():
    """Init that does enough numpy work to overshoot a tiny budget."""
    src = (
        "class Slow(Effect):\n"
        "    def init(self, ctx):\n"
        "        # Force enough numpy work to overshoot a 0.1 ms budget\n"
        "        # without depending on a Pi.\n"
        "        x = ctx.frames.x\n"
        "        for _ in range(50):\n"
        "            d = x[:, None] - x[None, :]\n"
        "            self.junk = (d * d).sum()\n"
        "    def render(self, ctx):\n"
        "        return self.out\n"
    )
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    # Tiny budget so even a few ms of numpy work trips it.
    rt.INIT_BUDGET_MS = 0.1
    with pytest.raises(EffectCompileError, match=r"init\(\) took"):
        rt.install_layer(
            "preview", name="slow", summary="", source=src, param_schema=[],
        )


# ---- 3. Strict params ---- #


def test_strict_params_raises_on_write():
    src = (
        "class Bad(Effect):\n"
        "    def render(self, ctx):\n"
        "        ctx.params.color = '#000000'\n"
        "        return self.out\n"
    )
    topo = _topo()
    rt = Runtime(topo, MasterControls(), strict_params=True)
    # Strict mode → fence-test render call raises TypeError, surfaced as
    # EffectCompileError("render() crashed on synthetic frame …").
    with pytest.raises(EffectCompileError, match="render"):
        rt.install_layer(
            "preview", name="bad", summary="", source=src,
            param_schema=[
                {"key": "color", "control": "color", "default": "#ff0000"},
            ],
        )


def test_soft_params_silently_warns_on_write():
    """v1 default: writes are no-op'd with a log warning, never crash."""
    src = (
        "class SloppyButOk(Effect):\n"
        "    def render(self, ctx):\n"
        "        ctx.params.color = '#000000'\n"   # ignored
        "        col = hex_to_rgb(ctx.params.color)\n"
        "        self.out[:] = col[None, :]\n"
        "        return self.out\n"
    )
    topo = _topo()
    rt = Runtime(topo, MasterControls(), strict_params=False)
    rt.install_layer(
        "preview", name="sloppy", summary="", source=src,
        param_schema=[{"key": "color", "control": "color", "default": "#ff0000"}],
    )
    # render passes; the assignment was ignored.
    assert rt.preview.layers[0].name == "sloppy"


# ---- 4. 30-frame fence ---- #


def test_fence_test_30_frames_catches_drift():
    """An effect that NaNs after the 11th frame would slip through a 10-frame
    fence but not a 30-frame one."""
    src = (
        "class NaNAt15(Effect):\n"
        "    def init(self, ctx):\n"
        "        self.tick = 0\n"
        "    def render(self, ctx):\n"
        "        self.tick += 1\n"
        "        if self.tick > 15:\n"
        "            self.out[:] = float('nan')\n"
        "        return self.out\n"
    )
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    with pytest.raises(EffectCompileError, match="NaN/Inf"):
        rt.install_layer(
            "preview", name="nanafter15", summary="", source=src, param_schema=[],
        )


# ---- 5. Watchdog disables a slow layer ---- #


def test_3_consecutive_failures_disables_layer():
    """A layer that crashes on three frames in a row gets disabled."""
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    # Install a healthy effect
    src = (
        "class Pulse(Effect):\n"
        "    def render(self, ctx):\n"
        "        self.out[:] = 0.0\n"
        "        return self.out\n"
    )
    rt.install_layer("live", name="pulse", summary="", source=src, param_schema=[])
    rt._cf = None
    layer = rt.live.layers[0]
    # Swap its instance for a thrower.

    class Thrower(layer.instance.__class__):
        def render(self, ctx):
            raise ValueError("boom")

    new_inst = Thrower()
    new_inst._setup(rt.n)
    new_inst.init(rt._build_init_ctx())
    layer.instance = new_inst

    audio = AudioView()
    for _ in range(4):
        rt.render(wall_t=0.0, dt=1/60, t_eff=0.0, audio=audio)
    assert layer.enabled is False


# ---- 6. dt clamping in engine — verified via runtime call (no engine boot) ---- #


def test_dt_clamp_is_in_engine_loop_only():
    """Smoke test: the engine code clamps dt at 2× period before passing
    into Runtime.render. Here we just confirm Runtime accepts a large dt
    without explosion (it doesn't try to clamp internally — that's the
    engine's job). This documents the contract."""
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    src = (
        "class Pulse(Effect):\n"
        "    def render(self, ctx):\n"
        "        self.out[:] = 0.0\n"
        "        return self.out\n"
    )
    rt.install_layer("live", name="x", summary="", source=src, param_schema=[])
    rt._cf = None
    live, _ = rt.render(wall_t=0.0, dt=10.0, t_eff=0.0, audio=AudioView())
    assert live.shape == (topo.pixel_count, 3)
    assert np.isfinite(live).all()


# ---- 7. Auto-retry surfaces final failure after exhausting budget ---- #


@pytest.fixture
def client(tmp_path):
    cfg = load_config(DEV)
    app = create_app(cfg, effects_dir=tmp_path)
    with TestClient(app) as c:
        yield c


def _broken_completion() -> CompletionResult:
    import json
    raw_args = {
        "name": "bad",
        "summary": "",
        "code": "import os\nclass X(Effect):\n    def render(self, ctx): return self.out\n",
        "params": [],
    }
    return CompletionResult(
        text="",
        tool_calls=[{"id": "c1", "name": "write_effect", "arguments": raw_args}],
        raw_message={
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "write_effect", "arguments": json.dumps(raw_args)},
            }],
        },
        finish_reason="tool_calls",
        model="mock",
    )


def test_auto_retry_surfaces_failure_count(client: TestClient):
    """When the LLM keeps emitting broken code, retries_used reflects the
    exhausted budget."""
    fake = _broken_completion()
    with patch("ledctl.agent.client.AgentClient.complete", return_value=fake):
        r = client.post("/agent/chat", json={"message": "make a bad effect"})
    assert r.status_code == 200
    body = r.json()
    assert body["tool_result"]["ok"] is False
    # Default config: retry_on_tool_error = 2 → 1 initial + 2 retries = 3 attempts
    assert body["retries_used"] == 2
