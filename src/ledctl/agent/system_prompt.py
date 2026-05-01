"""Build the per-turn system prompt.

Regenerated *fresh* on every user turn so the LLM always sees the install's
*current* state — not what it intended last turn, what the engine actually
ended up with after validation/clamping. Auto-injected sections:

  1. install description (from Topology)
  2. current LED state (mixer stack, blackout, crossfade, fps)
  3. live audio reading (snapshotted at request time)
  4. control surface — *full JSON schemas* for every effect (palette + bindings
     inlined), so the model has the same authoritative spec the validator does
  5. nested-type reference (PaletteSpec, ModulatorSpec, Bindings)
  6. recipes for common requests + anti-patterns
  7. examples
  8. rubric

This is the dominant token cost of Phase 6 — keep the per-field descriptions
tight, but never abbreviate the *structure* (the LLM keeps inventing nested
keys when the structure is implicit).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..audio.features import DEFAULT_BANDS
from ..effects.modulator import Bindings, ModulatorSpec
from ..effects.palette import PaletteSpec
from ..effects.registry import list_effects
from ..mixer import BLEND_MODES

if TYPE_CHECKING:
    from ..audio.state import AudioState
    from ..topology import Topology


# Hand-curated examples spanning the design space. Kept short on purpose —
# the model needs anchors, not novels.
EXAMPLES: list[dict[str, Any]] = [
    {
        "user": "warm slow ambient drift, amber to red, top row leading",
        "tool": {
            "name": "update_leds",
            "arguments": {
                "layers": [
                    {
                        "effect": "scroll",
                        "params": {
                            "axis": "x",
                            "speed": 0.12,
                            "wavelength": 1.5,
                            "shape": "cosine",
                            "softness": 1.0,
                            "cross_phase": [0.0, 0.075, 0.0],
                            "palette": {
                                "stops": [
                                    {"pos": 0.0, "color": "#ff2000"},
                                    {"pos": 0.5, "color": "#ff8000"},
                                    {"pos": 1.0, "color": "#ffd060"},
                                ]
                            },
                            "bindings": {
                                "brightness": {
                                    "source": "audio.rms",
                                    "floor": 0.5,
                                    "ceiling": 1.0,
                                }
                            },
                        },
                    }
                ],
                "crossfade_seconds": 2.0,
            },
        },
    },
    {
        "user": "make it pulsate red",
        "tool": {
            "name": "update_leds",
            "arguments": {
                "layers": [
                    {
                        "effect": "scroll",
                        "params": {
                            "palette": "mono_ff0000",
                            "bindings": {
                                "brightness": {
                                    "source": "lfo.sin",
                                    "period_s": 0.8,
                                    "floor": 0.15,
                                    "ceiling": 1.0,
                                }
                            },
                        },
                    }
                ],
                "crossfade_seconds": 0.5,
            },
        },
    },
    {
        "user": "peak hour, fast fire chase, kick-reactive",
        "tool": {
            "name": "update_leds",
            "arguments": {
                "layers": [
                    {
                        "effect": "scroll",
                        "params": {
                            "axis": "x",
                            "speed": 1.5,
                            "wavelength": 0.5,
                            "shape": "cosine",
                            "palette": "fire",
                            "bindings": {
                                "brightness": {
                                    "source": "audio.low",
                                    "floor": 0.3,
                                    "ceiling": 1.0,
                                    "gain": 4.0,
                                    "release_ms": 300,
                                }
                            },
                        },
                    },
                    {
                        "effect": "sparkle",
                        "blend": "screen",
                        "opacity": 0.7,
                        "params": {
                            "density": 0.4,
                            "decay": 2.5,
                            "palette": "mono_ffffff",
                            "bindings": {
                                "brightness": {
                                    "source": "audio.high",
                                    "gain": 6.0,
                                }
                            },
                        },
                    },
                ],
                "crossfade_seconds": 1.0,
            },
        },
    },
    {
        "user": "go dark for a sec",
        "tool": {
            "name": "update_leds",
            "arguments": {"blackout": True},
        },
    },
]


# Recipes — common operator phrasings → which knob to turn. Token-cheap but
# closes the gap between "what the operator says" and "which schema field".
RECIPES: list[tuple[str, str]] = [
    (
        "pulsating / breathing / flashing colour",
        "single layer with `palette: \"mono_<hex>\"` + "
        "`bindings.brightness = {source: \"lfo.sin\", period_s: 0.6–1.5, "
        "floor: 0.1, ceiling: 1.0}`. Do NOT change `speed` — that scrolls "
        "the pattern; with a mono palette there's nothing to scroll.",
    ),
    (
        "reactive to bass / kick / drums",
        "`bindings.brightness = {source: \"audio.low\", gain: 3.0–5.0, "
        "release_ms: 200–400}` on the main layer.",
    ),
    (
        "reactive to vocals / leads",
        "`bindings.brightness = {source: \"audio.mid\", gain: 2.5}`.",
    ),
    (
        "reactive to hi-hats / cymbals",
        "`bindings.brightness = {source: \"audio.high\", gain: 5.0}` — "
        "usually on a sparkle or accent layer.",
    ),
    (
        "rainbow waving / flowing",
        "`effect: scroll, palette: \"rainbow\", axis: \"x\", "
        "speed: ±0.1–0.4, wavelength: 1.0–2.0`. Sign of `speed` sets "
        "direction (positive = stage-right).",
    ),
    (
        "different colour on top vs. bottom row",
        "`effect: scroll, axis: \"x\"`, multi-stop palette, "
        "`cross_phase: [0, 0.5, 0]` (the y-component shifts the wave per "
        "unit y so the top row reads a different palette index than the "
        "bottom).",
    ),
    (
        "rings / pulses radiating from centre",
        "`effect: radial, center: [0,0,0], wavelength: 0.3–0.6`, multi-"
        "colour palette so the rings are visible.",
    ),
    (
        "twinkling / sparkles / stars",
        "`effect: sparkle` (NOT `noise` with a white palette — that's a "
        "milky haze). For coloured sparkles, use a mono_<hex> palette.",
    ),
    (
        "soft moving texture / clouds / haze",
        "`effect: noise, scale: 0.2–0.5, speed: 0.05–0.2, octaves: 2–3` "
        "with a coloured palette (rainbow, ocean, sunset...).",
    ),
    (
        "two effects mixed",
        "Stack two layers. Bottom layer is the base wash (`blend: normal`); "
        "top layer is the accent (`blend: screen` or `add`, opacity 0.4–0.8).",
    ),
    (
        "go dark / blackout",
        "`{\"blackout\": true}` — `layers` is ignored when blackout is set.",
    ),
]


# Anti-patterns the LLM keeps reaching for. Listed explicitly because the
# model otherwise infers them from the param names.
ANTI_PATTERNS: list[str] = [
    "mono_<hex> palette + non-trivial scroll/radial shape = flat colour. The "
    "shape modulates a SCALAR which the palette LUT turns into RGB; a single-"
    "colour LUT collapses every scalar to the same RGB. To make a single hue "
    "*pulse*, drive `bindings.brightness`, not `speed`.",
    "Adding a `noise` layer with `palette: \"white\"` and `blend: \"add\"` "
    "washes everything to grey/white. For sparkles use `effect: sparkle`. For "
    "tinted noise, pick a coloured palette.",
    "There is no `scroll_phase` or `phase_offset` field. The cross-axis phase "
    "knob on `scroll` is `cross_phase` (a 3-tuple of cycles per unit "
    "normalised position).",
    "There is no top-level `bindings` on a layer — `bindings` lives INSIDE "
    "`params` (next to `palette`, `speed`, etc.).",
    "There is no `palette: \"red\"` named palette. Single-colour shorthand is "
    "`mono_<hex>`, e.g. `mono_ff0000`.",
]


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


def _summarise_current_state(engine: Any) -> str:
    layer_state = engine.layer_state()
    payload: dict[str, Any] = {
        "blackout": bool(engine.mixer.blackout),
        "crossfading": bool(engine.mixer.is_crossfading),
        "fps": round(engine.fps, 1),
        "dropped_frames": engine.dropped_frames,
        "layers": layer_state,
    }
    return "CURRENT STATE\n" + json.dumps(payload, indent=2, default=str)


def _summarise_audio(audio_state: AudioState | None) -> str:
    if audio_state is None or not audio_state.enabled:
        return (
            "AUDIO\n"
            "Audio capture is OFF. Audio bindings will read 0; prefer "
            "non-reactive specs (fixed brightness or LFO bindings) until "
            "the operator enables capture at /audio."
        )
    bands = DEFAULT_BANDS  # ((20, 250), (250, 2000), (2000, 12000))
    low_lo, low_hi = bands[0]
    mid_lo, mid_hi = bands[1]
    hi_lo, hi_hi = bands[2]
    return (
        f"AUDIO (snapshot at request time — treat as 'the room a moment ago')\n"
        f"  device: {audio_state.device_name or 'default'}\n"
        f"  rms (full-band): {audio_state.rms:.3f} (norm {audio_state.rms_norm:.2f})\n"
        f"  peak (full-band): {audio_state.peak:.3f} (norm {audio_state.peak_norm:.2f})\n"
        f"  low  ({low_lo:.0f}–{low_hi:.0f} Hz): {audio_state.low:.3f} "
        f"(norm {audio_state.low_norm:.2f})\n"
        f"  mid  ({mid_lo:.0f}–{mid_hi:.0f} Hz): {audio_state.mid:.3f} "
        f"(norm {audio_state.mid_norm:.2f})\n"
        f"  high ({hi_lo:.0f}–{hi_hi:.0f} Hz): {audio_state.high:.3f} "
        f"(norm {audio_state.high_norm:.2f})\n"
        f"  Bindings consume the *_norm values (rolling-window auto-scaled to "
        f"~[0, 1])."
    )


# ---- schema serialisation ---------------------------------------------------
#
# We dump every effect's full pydantic JSON Schema into the prompt with $defs
# resolved. The validator (pydantic via `cls.Params(**...)`) is the source of
# truth — by handing the model the same schema, we close the gap between
# "what the LLM thinks the API is" and "what the validator accepts".


# Map pydantic $defs names → short symbolic placeholders we'll resolve once
# in NESTED TYPES. Without this, every effect schema re-inlines the full
# PaletteSpec + Bindings + ModulatorSpec, blowing the prompt up by ~10× with
# duplicated content.
_PLACEHOLDER_DEFS: dict[str, str] = {
    "PaletteSpec": "<PaletteSpec; see NESTED TYPES>",
    "Bindings": "<Bindings; see NESTED TYPES>",
    "ModulatorSpec": "<ModulatorSpec; see NESTED TYPES>",
    "PaletteStop": "<PaletteStop; see NESTED TYPES>",
}


def _resolve_refs(
    schema: dict[str, Any], *, placeholder: bool = False
) -> dict[str, Any]:
    """Inline `$ref`s against `$defs` and strip Pydantic-internal keys.

    With `placeholder=True`, refs to known nested types (PaletteSpec, Bindings,
    ModulatorSpec) collapse to a short string pointer at NESTED TYPES instead
    of re-inlining the whole schema. Use this for per-effect schemas; use the
    full inline form for the standalone NESTED TYPES block.

    Drops `title` to save tokens — pydantic emits a `title` for every field
    and they duplicate the field name.
    """
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if ref.startswith("#/$defs/"):
                    name = ref[len("#/$defs/"):]
                    if placeholder and name in _PLACEHOLDER_DEFS:
                        return _PLACEHOLDER_DEFS[name]
                    target = defs.get(name)
                    if target is not None:
                        return walk(target)
                return {}
            return {
                k: walk(v)
                for k, v in node.items()
                if k not in {"$defs", "$schema", "title"}
            }
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


def _summarise_effects() -> str:
    """Dump every effect's full Params JSON Schema, $refs inlined.

    The schema is the same one the validator uses. `palette` and `bindings`
    are inlined so a single block per effect tells the model everything it
    needs to construct a valid `params` payload.
    """
    out = [
        "EFFECTS — full JSON Schema for each effect's `params` payload. "
        "`additionalProperties: false` is enforced server-side; unknown keys "
        "fail the tool call. `palette` and `bindings` are placeholders here; "
        "their full schema is in NESTED TYPES below."
    ]
    for name, cls in sorted(list_effects().items()):
        schema = _resolve_refs(cls.Params.model_json_schema(), placeholder=True)
        doc = (cls.Params.__doc__ or "").strip().splitlines()
        headline = doc[0] if doc else name
        out.append(f"\n  {name}: {headline}")
        out.append(
            "  schema: " + json.dumps(schema, separators=(",", ":"), default=str)
        )
    return "\n".join(out)


def _summarise_nested_types() -> str:
    """Standalone reference for PaletteSpec / ModulatorSpec / Bindings.

    Repeated from the per-effect schemas so the model can recognise the type
    by name when reading a partial spec, instead of reverse-engineering it
    every turn.
    """
    palette = _resolve_refs(PaletteSpec.model_json_schema())
    modulator = _resolve_refs(ModulatorSpec.model_json_schema())
    bindings = _resolve_refs(Bindings.model_json_schema())
    return (
        "NESTED TYPES (used by every effect's `palette` and `bindings` fields)\n"
        "  PaletteSpec: " + json.dumps(palette, separators=(",", ":"), default=str) + "\n"
        "  ModulatorSpec: " + json.dumps(modulator, separators=(",", ":"), default=str) + "\n"
        "  Bindings: " + json.dumps(bindings, separators=(",", ":"), default=str) + "\n"
        "  PaletteSpec also accepts a bare-string shorthand: \"fire\" ≡ "
        "{\"name\": \"fire\"}. Use `mono_<hex>` (e.g. `mono_ff7000`) for a "
        "single colour."
    )


def _summarise_palettes() -> str:
    from ..effects.palette import NAMED_PALETTES

    return (
        "NAMED PALETTES (use as `palette: \"<name>\"` or `palette: {\"name\": "
        "\"<name>\"}`)\n"
        "  " + ", ".join(sorted(NAMED_PALETTES)) + ", mono_<hex>\n"
        "  Custom: `{\"stops\": [{\"pos\": 0.0, \"color\": \"#ff2000\"}, "
        "{\"pos\": 1.0, \"color\": \"#ffd060\"}]}` (>=2 stops, sorted by pos)."
    )


def _summarise_bindings_overview() -> str:
    return (
        "BINDINGS — modulators on each effect's `params.bindings.{brightness, "
        "speed, hue_shift}` slot.\n"
        "  - `brightness`: multiplies output. Default attack/release: 30 ms / "
        "500 ms (snap on, gentle off). Use this for pulsation, audio reactivity, "
        "fades.\n"
        "  - `speed`: replaces the field's static `speed`. Smoothed slowly "
        "(200/200 ms) so tempo changes don't jolt. Sparkle ignores it.\n"
        "  - `hue_shift`: rotates palette LUT in cycles. Slow release (2 s) so "
        "colour drift looks deliberate.\n"
        "  Source values: const (use `value`), audio.{rms,peak,low,mid,high}, "
        "lfo.{sin,saw,triangle,pulse} (set `period_s`, `phase`, and `duty` for "
        "pulse). `audio.*` reads the auto-scaled norm (~[0, 1]).\n"
        "  Override knobs: `floor`, `ceiling`, `gain`, `attack_ms`, "
        "`release_ms`, `curve` (linear|sqrt|square)."
    )


def _summarise_blends_and_presets(presets: list[str]) -> str:
    presets_line = ", ".join(presets) if presets else "(none on disk)"
    return (
        "BLEND MODES\n"
        f"  {', '.join(BLEND_MODES)}. normal = layer cover, add = brighten, "
        "screen = soft brighten, multiply = darken/mask.\n"
        f"PRESETS (on disk; you can mention them but the tool always emits a full spec)\n"
        f"  {presets_line}"
    )


def _recipes_block() -> str:
    lines = ["RECIPES (operator phrasing → which knob to turn)"]
    for phrase, recipe in RECIPES:
        lines.append(f"  - {phrase}: {recipe}")
    return "\n".join(lines)


def _anti_patterns_block() -> str:
    lines = ["ANTI-PATTERNS (the model keeps reaching for these — don't)"]
    for ap in ANTI_PATTERNS:
        lines.append(f"  - {ap}")
    return "\n".join(lines)


def _examples_block() -> str:
    rendered = []
    for ex in EXAMPLES:
        rendered.append(
            f"  user: {ex['user']}\n"
            f"  tool: {json.dumps(ex['tool'], separators=(',', ': '))}"
        )
    return "EXAMPLES\n" + "\n\n".join(rendered)


RUBRIC = (
    "RUBRIC\n"
    "- You have one tool: `update_leds`. Emit it once per turn (or not at all "
    "if the user is just chatting). The argument is the COMPLETE new layer "
    "stack — never a diff.\n"
    "- For 'more red, slower', re-emit the current stack with shifted colours "
    "and reduced speed.\n"
    "- Pick a `crossfade_seconds` that fits: snappy ~0.3, normal ~1–1.5, slow "
    "drift 3–5.\n"
    "- The user can see the lights — keep your assistant text terse "
    "(<= 1 short sentence). No reciting the spec; the tool call already says it.\n"
    "- The control panel is for setting/adjusting the vibe at human typing "
    "speed. Don't try to drive an animation by spamming turns.\n"
    "- The audio reading is a snapshot from when the user pressed enter. "
    "Treat it as 'the room a moment ago.' Bindings (audio.* sources) keep "
    "the visuals reactive in real time.\n"
    "- VALIDATION IS STRICT. Unknown effect names, unknown param keys, or "
    "wrong nested shape (e.g. `bindings` outside `params`, or "
    "`scroll_phase` instead of `cross_phase`) will fail the tool call with a "
    "structured error. Re-emit a corrected full stack on the next turn — the "
    "previous (valid) stack is in CURRENT STATE so you can build from it.\n"
    "- If a tool result has `ok: false`, READ THE `details` field carefully "
    "before retrying — it tells you exactly which key/value the validator "
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
) -> str:
    """Assemble the per-turn system prompt. Pure function — easy to test."""
    presets: list[str] = []
    if presets_dir is not None and presets_dir.exists():
        presets = sorted(p.stem for p in presets_dir.glob("*.yaml") if p.is_file())

    sections = [
        "You are the language-driven control panel for an audio-reactive LED "
        "festival install. You translate operator requests into one "
        "`update_leds` tool call that describes the COMPLETE new state of the "
        "lights. The render engine handles the crossfade. The full schema for "
        "every effect is included below — the validator rejects unknown keys, "
        "so stick to the documented fields.",
        _summarise_install(topology),
        _summarise_current_state(engine),
        _summarise_audio(audio_state),
        _summarise_effects(),
        _summarise_nested_types(),
        _summarise_palettes(),
        _summarise_bindings_overview(),
        _summarise_blends_and_presets(presets),
        _recipes_block(),
        _anti_patterns_block(),
        _examples_block(),
        RUBRIC,
    ]
    return "\n\n".join(sections)
