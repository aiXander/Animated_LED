"""Per-turn system prompt assembly.

Regenerated *fresh* on every turn so the LLM always sees:
  1. install description (auto from Topology)
  2. current LED state (mixer stack, blackout, crossfade, fps)
  3. live audio reading
  4. operator masters (read-only — agent cannot change them)
  5. CONTROL SURFACE (auto from `surface.generate_docs()`)
  6. rubric

The catalogue, examples, and anti-patterns all live next to the primitives in
`surface.py` — there is no second source of truth here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import surface
from ..audio.features import DEFAULT_BANDS
from ..masters import MasterControls
from ..mixer import BLEND_MODES

if TYPE_CHECKING:
    from ..audio.state import AudioState
    from ..topology import Topology


def _summarise_install(topology: Topology) -> str:
    pmin = topology.bbox_min.tolist()
    pmax = topology.bbox_max.tolist()
    span_x = pmax[0] - pmin[0]
    span_y = pmax[1] - pmin[1]
    strip_lines = []
    for s in topology.strips:
        strip_lines.append(
            f"  - {s.id}: {s.pixel_count} LEDs from "
            f"{tuple(round(v, 2) for v in s.geometry.start)} to "
            f"{tuple(round(v, 2) for v in s.geometry.end)}"
            f"{' (reversed)' if s.reversed else ''}"
        )
    return (
        f"INSTALL\n"
        f"{topology.pixel_count} LEDs across {len(topology.strips)} strips, "
        f"spanning ~{span_x:.1f} m × {span_y:.1f} m. "
        f"Effects address LEDs in normalised coords (each axis in [-1, 1]); "
        f"+x = stage-right, +y = up, +z = toward audience.\n"
        + "\n".join(strip_lines)
    )


def _summarise_current_state(engine: Any, external_edit: bool = False) -> str:
    layer_state = engine.layer_state()
    payload: dict[str, Any] = {
        "blackout": bool(engine.mixer.blackout),
        "crossfading": bool(engine.mixer.is_crossfading),
        "fps": round(engine.fps, 1),
        "dropped_frames": engine.dropped_frames,
        "layers": layer_state,
    }
    header = (
        "CURRENT STATE (authoritative — operator can edit the stack via "
        "the UI between turns; if this differs from your last `update_leds` "
        "call, the operator removed/changed/reordered layers manually and "
        "you MUST rebuild from this, not from your prior tool call args)"
    )
    if external_edit:
        header += (
            "\n!! OPERATOR EDIT DETECTED: the layer stack below differs from "
            "what you returned in your most recent `update_leds` call. The "
            "operator manually edited the stack via the LAYERS UI panel "
            "since your last turn. Treat the layers below as ground truth "
            "and rebuild from them — do NOT re-add layers the operator removed."
        )
    return header + "\n" + json.dumps(payload, indent=2, default=str)


def _summarise_audio(audio_state: AudioState | None) -> str:
    if audio_state is None or not audio_state.enabled:
        return (
            "AUDIO\n"
            "Audio capture is OFF. audio_band primitives will return 0; "
            "prefer non-reactive specs (constant brightness, lfo modulators) "
            "until the operator enables capture at /audio."
        )
    bands = DEFAULT_BANDS  # ((20, 250), (250, 2000), (2000, 12000))
    low_lo, low_hi = bands[0]
    mid_lo, mid_hi = bands[1]
    hi_lo, hi_hi = bands[2]
    return (
        f"AUDIO (snapshot at request time — treat as 'the room a moment ago')\n"
        f"  device: {audio_state.device_name or 'default'}\n"
        f"  low  ({low_lo:.0f}–{low_hi:.0f} Hz): {audio_state.low:.3f} "
        f"(norm {audio_state.low_norm:.2f})\n"
        f"  mid  ({mid_lo:.0f}–{mid_hi:.0f} Hz): {audio_state.mid:.3f} "
        f"(norm {audio_state.mid_norm:.2f})\n"
        f"  high ({hi_lo:.0f}–{hi_hi:.0f} Hz): {audio_state.high:.3f} "
        f"(norm {audio_state.high_norm:.2f})\n"
        f"  audio_band reads the *_norm values (rolling-window auto-scaled to "
        f"~[0, 1]; multiplied by masters.audio_reactivity at the engine). "
        f"Pick the band that matches the musical element you want to track — "
        f"there is no full-band loudness primitive."
    )


def _summarise_masters(
    masters: MasterControls | None,
    crossfade_seconds: float | None = None,
) -> str:
    if masters is None and crossfade_seconds is None:
        return ""
    lines = ["OPERATOR MASTERS (read-only — set by sliders, persist across your changes):"]
    if masters is not None:
        lines.extend([
            f"  brightness:        {masters.brightness:.2f}   (final-output gain)",
            f"  speed:             {masters.speed:.2f}   (time multiplier on motion)",
            f"  audio_reactivity:  {masters.audio_reactivity:.2f}   "
            "(multiplier on every audio_band)",
            f"  saturation:        {masters.saturation:.2f}   (1 = full colour, 0 = greyscale)",
            f"  freeze:            {str(bool(masters.freeze)).lower()}   "
            "(true = effective time stops; envelope/audio still update)",
        ])
    if crossfade_seconds is not None:
        lines.append(
            f"  crossfade:         {crossfade_seconds:.2f}s   "
            "(duration of every fade between layer stacks — applies to your "
            "`update_leds` calls AND preset loads)"
        )
    lines.append(
        "You cannot modify these — they are sliders the operator owns. If a "
        "request can only be honoured by a master change ('make it brighter' "
        "while brightness < 1.0; 'less reactive' while audio_reactivity is "
        "high; 'slower transition' while crossfade is short), say so in your "
        "reply and tell the user which slider to move. Otherwise, design "
        "your spec assuming the masters stay where they are."
    )
    return "\n".join(lines)


def _summarise_blends_and_presets(presets: list[str]) -> str:
    presets_line = ", ".join(presets) if presets else "(none on disk)"
    return (
        "BLEND MODES\n"
        f"  {', '.join(BLEND_MODES)}. normal = layer cover, add = brighten, "
        "screen = soft brighten, multiply = darken/mask.\n"
        f"PRESETS (on disk; you can mention them but the tool always emits a full spec)\n"
        f"  {presets_line}"
    )


RUBRIC = (
    "RUBRIC\n"
    "- You have one tool: `update_leds`. Emit it once per turn (or not at all "
    "if the user is just chatting). The argument is the COMPLETE new layer "
    "stack — never a diff.\n"
    "- For 'more red, slower', re-emit the current stack with shifted colours "
    "and reduced wave speed.\n"
    "- The user can see the lights — keep your assistant text terse "
    "(<= 1 short sentence). No reciting the spec; the tool call already says it.\n"
    "- The control panel is for setting/adjusting the vibe at human typing "
    "speed. Don't try to drive an animation by spamming turns.\n"
    "- The audio reading is a snapshot from when the user pressed enter. "
    "Treat it as 'the room a moment ago.' audio_band keeps the visuals "
    "reactive in real time.\n"
    "- VALIDATION IS STRICT. Unknown primitives, unknown param keys, or "
    "kind mismatches (palette where scalar_field is expected, etc.) fail "
    "the tool call with a structured error including the full tree path. "
    "Re-emit a corrected full stack on the next turn — the previous valid "
    "stack is in CURRENT STATE so you can build from it.\n"
    "- The operator can edit the layer stack manually via the LAYERS UI "
    "panel (add/remove/reorder/patch). When that happens, CURRENT STATE "
    "will diverge from the `layers` argument you sent in your most recent "
    "`update_leds` call (and from the `layers` field in prior tool results "
    "in this conversation). ALWAYS rebuild from CURRENT STATE — do NOT "
    "reinstate layers the operator removed by copying your previous tool "
    "call's arguments. Your chat history is a record of what you did; "
    "CURRENT STATE is what's actually running.\n"
    "- If a tool result has `ok: false`, READ THE `details` field carefully "
    "before retrying — it tells you exactly which path/key the validator "
    "rejected.\n"
    "- If the user's request is ambiguous or whimsical, pick reasonable "
    "defaults instead of asking — they can correct on the next turn."
)


def build_system_prompt(
    *,
    topology: Topology,
    engine: Any,
    audio_state: AudioState | None,
    presets_dir: Path | None = None,
    masters: MasterControls | None = None,
    crossfade_seconds: float | None = None,
    external_edit: bool = False,
) -> str:
    """Assemble the per-turn system prompt. Pure function — easy to test."""
    presets: list[str] = []
    if presets_dir is not None and presets_dir.exists():
        presets = sorted(p.stem for p in presets_dir.glob("*.yaml") if p.is_file())

    sections: list[str] = [
        "You are the language-driven control panel for an audio-reactive LED "
        "festival install. You translate operator requests into one "
        "`update_leds` tool call describing the COMPLETE new layer stack as a "
        "tree of {kind, params} primitives. The render engine handles the "
        "crossfade. Validation is strict; the surface catalogue below is the "
        "authoritative spec.",
        _summarise_install(topology),
        _summarise_current_state(engine, external_edit=external_edit),
        _summarise_audio(audio_state),
    ]
    masters_block = _summarise_masters(masters, crossfade_seconds)
    if masters_block:
        sections.append(masters_block)
    sections.extend(
        [
            surface.generate_docs(
                topology=topology,
                audio_state=audio_state,
                engine_state=engine,
            ),
            _summarise_blends_and_presets(presets),
            RUBRIC,
        ]
    )
    return "\n\n".join(sections)
