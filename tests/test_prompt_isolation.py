"""Pin the contract: the LLM sees PREVIEW only — never LIVE.

Live is the operator's domain. Leaking live source / params / layer count
into the system prompt would tempt the LLM to "preserve" what's playing
and clobber the operator's promote-time decisions.
"""

from __future__ import annotations

from pathlib import Path

from ledctl.config import load_config
from ledctl.masters import MasterControls
from ledctl.surface import Runtime, build_system_prompt
from ledctl.topology import Topology

DEV = Path(__file__).resolve().parents[1] / "config" / "config.dev.yaml"


def _topo() -> Topology:
    return Topology.from_config(load_config(DEV))


_LIVE_SRC = (
    "class LiveSecret(Effect):\n"
    "    def render(self, ctx):\n"
    "        # this string must NEVER appear in the LLM prompt\n"
    "        TOTALLY_UNIQUE_LIVE_SENTINEL = 42\n"
    "        self.out[:] = 0.0\n"
    "        return self.out\n"
)
_PREVIEW_SRC = (
    "class PreviewDraft(Effect):\n"
    "    def render(self, ctx):\n"
    "        UNIQUE_PREVIEW_SENTINEL = 7\n"
    "        col = hex_to_rgb(ctx.params.color)\n"
    "        self.out[:] = col[None, :]\n"
    "        return self.out\n"
)


def _build_runtime_with_distinct_live_and_preview() -> Runtime:
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer(
        "live", name="live_secret", summary="LIVE_SUMMARY_DO_NOT_LEAK",
        source=_LIVE_SRC, param_schema=[],
        param_values={},
    )
    rt.install_layer(
        "preview", name="preview_draft", summary="preview is what you author",
        source=_PREVIEW_SRC,
        param_schema=[{"key": "color", "control": "color", "default": "#22cc66"}],
    )
    rt._cf = None
    return rt


def test_prompt_does_not_leak_live_source():
    rt = _build_runtime_with_distinct_live_and_preview()
    prompt = build_system_prompt(
        topology=rt.topology,
        runtime=rt,
        audio_state=None,
        masters=rt.masters,
        crossfade_seconds=rt.crossfade_seconds,
    )
    assert "TOTALLY_UNIQUE_LIVE_SENTINEL" not in prompt
    assert "LIVE_SUMMARY_DO_NOT_LEAK" not in prompt
    assert "live_secret" not in prompt
    # Preview side is fully visible.
    assert "UNIQUE_PREVIEW_SENTINEL" in prompt
    assert "preview_draft" in prompt


def test_prompt_advertises_preview_only_role():
    rt = _build_runtime_with_distinct_live_and_preview()
    prompt = build_system_prompt(
        topology=rt.topology,
        runtime=rt,
        audio_state=None,
        masters=rt.masters,
        crossfade_seconds=rt.crossfade_seconds,
    )
    # Header for the section showing the LLM what it's working on.
    assert "CURRENT PREVIEW COMPOSITION" in prompt
    # The "no read, no write" stance must be stated.
    assert "NO visibility into it" in prompt or "no visibility" in prompt.lower()


def test_empty_preview_still_renders_cleanly():
    """Even when the preview is empty, the LLM is told that — instead of
    being shown nothing or (worse) the live composition as a fallback."""
    topo = _topo()
    rt = Runtime(topo, MasterControls())
    rt.install_layer(
        "live", name="live_only", summary="", source=_LIVE_SRC, param_schema=[],
    )
    rt._cf = None
    prompt = build_system_prompt(
        topology=rt.topology,
        runtime=rt,
        audio_state=None,
        masters=rt.masters,
        crossfade_seconds=rt.crossfade_seconds,
    )
    assert "preview composition is empty" in prompt
    assert "TOTALLY_UNIQUE_LIVE_SENTINEL" not in prompt
    assert "live_only" not in prompt
