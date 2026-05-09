"""Headline end-to-end test:

  1. The LLM emits broken code → write_effect returns ok=false with traceback.
  2. The next turn's system prompt includes the LAST EFFECT ERROR block.
  3. The LLM emits a fixed version → write_effect succeeds, preview slot installed.
  4. The simulator (sim leg) renders the preview successfully.
  5. The operator "promotes" → live composition crossfades to the new effect.

This is the user's "make my imagination the bottleneck" pipeline.
"""

from __future__ import annotations

from pathlib import Path

from ledctl.config import load_config
from ledctl.masters import MasterControls
from ledctl.surface import (
    EffectStore,
    Runtime,
    apply_write_effect,
    build_system_prompt,
    write_effect_tool_schema,
)
from ledctl.surface.base import AudioView
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


def _topo() -> Topology:
    return Topology.from_config(load_config(DEV))


def _audio() -> AudioView:
    return AudioView(low=0.5, mid=0.3, high=0.2, beat=0, beats_since_start=0,
                     bpm=120.0, connected=True)


def _bootstrap(tmp_path: Path) -> tuple[Runtime, EffectStore]:
    """Set up runtime + effects store with pulse_mono in both slots."""
    store = EffectStore(tmp_path)
    store.install_examples_if_missing()

    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.crossfade_seconds = 0.5

    # Boot both slots with pulse_mono
    for slot in ("live", "preview"):
        stored = store.load("pulse_mono")
        rt.install_layer(
            slot, name=stored.name, summary=stored.summary, source=stored.source,
            param_schema=stored.param_schema, param_values=stored.param_values,
        )
    rt._cf = None  # boot is hard-cut
    return rt, store


def test_full_pipeline_broken_then_fixed_then_promoted(tmp_path: Path):
    rt, store = _bootstrap(tmp_path)
    topo = rt.topology

    # ---- TURN 1: LLM emits broken code (uses an undefined name) ---- #
    broken_args = {
        "name": "broken_v1",
        "summary": "an effect that doesn't exist",
        "code": (
            "class Broken(Effect):\n"
            "    def init(self, ctx):\n"
            "        self.x = ctx.frames.x\n"
            "    def render(self, ctx):\n"
            "        # references an undefined helper — should NameError\n"
            "        self.out[:] = some_undefined_helper(ctx.t)\n"
            "        return self.out\n"
        ),
        "params": [],
    }
    result1 = apply_write_effect(broken_args, runtime=rt, store=store)
    assert result1["ok"] is False
    assert result1["error"] in ("compile_failed", "tool_argument_validation_failed")
    # Preview must NOT have been swapped — still pulse_mono.
    assert rt.preview.layers[0].name == "pulse_mono"
    assert rt.live.layers[0].name == "pulse_mono"

    # ---- TURN 2: assemble system prompt that surfaces the error ---- #
    last_error = {"error": result1["error"], "details": result1["details"]}
    prompt = build_system_prompt(
        topology=topo,
        runtime=rt,
        audio_state=None,
        masters=rt.masters,
        crossfade_seconds=rt.crossfade_seconds,
        last_error=last_error,
    )
    assert "LAST EFFECT ERROR" in prompt
    assert "compile_failed" in prompt or "tool_argument_validation_failed" in prompt
    # The prompt should also expose the runtime API so the LLM knows what's in scope.
    assert "RUNTIME API" in prompt
    # And the current preview source so the LLM can see what it's replacing.
    assert "SELECTED LAYER SOURCE" in prompt

    # ---- TURN 3: LLM emits a corrected version (uses real helpers) ---- #
    # NOTE: param keys are deliberately distinct from pulse_mono's so the
    # auto-merge doesn't pull pulse_mono's red colour into the new effect.
    # That carry-forward behaviour is exercised separately below.
    fixed_args = {
        "name": "fixed_solid",
        "summary": "Solid green that breathes on bass.",
        "code": (
            "class FixedSolid(Effect):\n"
            "    def init(self, ctx):\n"
            "        self._scratch = np.zeros(ctx.n, dtype=np.float32)\n"
            "    def render(self, ctx):\n"
            "        col = hex_to_rgb(ctx.params.tint)\n"
            "        floor = float(ctx.params.bass_floor)\n"
            "        amp = floor + (1.0 - floor) * float(ctx.audio.low)\n"
            "        self.out[:] = col[None, :]\n"
            "        self.out *= amp\n"
            "        return self.out\n"
        ),
        "params": [
            {"key": "tint", "control": "color", "default": "#22cc66"},
            {"key": "bass_floor", "control": "slider",
             "min": 0.0, "max": 1.0, "step": 0.01, "default": 0.4},
        ],
    }
    result2 = apply_write_effect(fixed_args, runtime=rt, store=store)
    assert result2["ok"] is True, result2
    assert result2["applied"] == "preview"
    # Preview slot has been swapped.
    assert rt.preview.layers[0].name == "fixed_solid"
    # Live slot UNDISTURBED — still pulse_mono.
    assert rt.live.layers[0].name == "pulse_mono"

    # ---- TURN 4: simulator (preview leg) renders cleanly ---- #
    rt.mode = "design"
    live_buf, sim_buf = rt.render(
        wall_t=0.05, dt=1/60, t_eff=0.05, audio=_audio(),
    )
    assert live_buf.shape == (topo.pixel_count, 3)
    assert sim_buf.shape == (topo.pixel_count, 3)
    # In design mode they are different buffers.
    assert sim_buf is not live_buf
    # The preview shows green; the live still shows pulse_mono's default colour (red-ish).
    # G channel max of preview should exceed G channel max of live by a meaningful margin.
    assert sim_buf[:, 1].max() > live_buf[:, 1].max() + 0.1

    # ---- TURN 5: promote → live crossfades to the new effect ---- #
    rt.promote()
    assert rt._cf is not None  # crossfade is in progress
    # After the crossfade fully elapses, live should match the new effect.
    rt.render(wall_t=0.05, dt=1/60, t_eff=0.05, audio=_audio())
    rt.render(wall_t=10.0, dt=1/60, t_eff=10.0, audio=_audio())
    # Crossfade now done.
    assert rt._cf is None
    # New live composition has the fixed_solid layer at top.
    assert rt.live.layers[0].name == "fixed_solid"
    live_buf, _ = rt.render(wall_t=11.0, dt=1/60, t_eff=11.0, audio=_audio())
    # Green channel dominant in live now.
    assert live_buf[:, 1].max() > live_buf[:, 0].max()


def test_write_effect_keeps_preview_on_compile_error(tmp_path: Path):
    rt, store = _bootstrap(tmp_path)
    # Broken: imports
    args = {
        "name": "x",
        "summary": "",
        "code": "import os\nclass X(Effect):\n    def render(self, ctx): return self.out\n",
        "params": [],
    }
    result = apply_write_effect(args, runtime=rt, store=store)
    assert result["ok"] is False
    # Preview unchanged.
    assert rt.preview.layers[0].name == "pulse_mono"


def test_param_carry_forward_across_regeneration(tmp_path: Path):
    """When the LLM emits a new effect that reuses a `key`, the operator's
    current slider value should survive."""
    rt, store = _bootstrap(tmp_path)

    # Operator drags the floor slider.
    sel = rt.preview.selected_layer()
    assert sel is not None
    sel.params.update({"floor": 0.85})

    # New effect that reuses `floor`.
    args = {
        "name": "another",
        "summary": "",
        "code": (
            "class Another(Effect):\n"
            "    def render(self, ctx):\n"
            "        amp = float(ctx.params.floor)\n"
            "        self.out[:] = amp\n"
            "        return self.out\n"
        ),
        "params": [
            {"key": "floor", "control": "slider", "min": 0, "max": 1,
             "step": 0.01, "default": 0.4},
        ],
    }
    result = apply_write_effect(args, runtime=rt, store=store)
    assert result["ok"] is True
    # Operator's tweak survived: 0.85 not 0.4.
    new_layer = rt.preview.selected_layer()
    assert new_layer is not None
    assert abs(float(new_layer.params.get("floor")) - 0.85) < 1e-6


def test_write_effect_tool_schema_well_formed():
    schema = write_effect_tool_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "write_effect"
    params = schema["function"]["parameters"]
    assert "name" in params["properties"]
    assert "code" in params["properties"]
    assert "params" in params["properties"]
