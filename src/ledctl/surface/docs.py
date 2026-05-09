"""Doc + JSON catalogue generation for the LLM prompt and operator UI."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from ..topology import Topology
from . import palettes as _palettes
from .frames import FRAME_DESCRIPTIONS
from .palettes import NAMED_PALETTES
from .registry import REGISTRY, Primitive

# Anchor recipes — small enough that the LLM picks up the pattern instantly,
# and they're the templates `EXAMPLE_TREES` / system prompt examples are
# built from.
EXAMPLE_TREES: dict[str, dict[str, Any]] = {
    "warm_drift": {
        "kind": "palette_lookup",
        "params": {
            "scalar": {"kind": "wave", "params": {"axis": "x", "speed": 0.12, "wavelength": 1.5}},
            "palette": {
                "kind": "palette_stops",
                "params": {
                    "stops": [
                        {"pos": 0.0, "color": "#ff2000"},
                        {"pos": 0.5, "color": "#ff8000"},
                        {"pos": 1.0, "color": "#ffd060"},
                    ]
                },
            },
            "brightness": {
                "kind": "pulse",
                "params": {
                    "by": {"kind": "audio_band", "params": {"band": "low"}},
                    "floor": 0.6,
                },
            },
        },
    },
    "fire_chase": {
        "kind": "palette_lookup",
        "params": {
            "scalar": {"kind": "wave", "params": {"axis": "x", "speed": 1.5, "wavelength": 0.5}},
            "palette": "fire",
            "brightness": {
                "kind": "pulse",
                "params": {
                    "by": {"kind": "audio_band", "params": {"band": "low"}},
                    "floor": 0.3,
                },
            },
        },
    },
    "pulse_red": {
        "kind": "palette_lookup",
        "params": {
            "scalar": {"kind": "constant", "params": {"value": 0.5}},
            "palette": "mono_ff0000",
            "brightness": {
                "kind": "pulse",
                "params": {
                    "by": {"kind": "lfo", "params": {"shape": "sin", "period_s": 0.8}},
                    "floor": 0.4,
                },
            },
        },
    },
    "sparkle_only": {
        "kind": "sparkles",
        "params": {"density": 0.04, "decay": 1.5, "seed": 7},
    },
    "axis_cross": {
        "kind": "palette_lookup",
        "params": {
            "scalar": {
                "kind": "mul",
                "params": {
                    "a": {"kind": "wave", "params": {"axis": "x", "speed": 0.4, "shape": "cosine"}},
                    "b": {"kind": "wave", "params": {"axis": "y", "speed": 0.3, "shape": "cosine"}},
                },
            },
            "palette": "rainbow",
        },
    },
    "rainbow_sparkles": {
        "kind": "sparkles",
        "params": {
            "palette": "rainbow",
            "density": 3.0,
            "decay": 2.0,
            "spread": 1.0,
            "palette_center": 0.5,
        },
    },
    "beat_strobe_white": {
        "kind": "strobe",
        "params": {
            "palette": "white",
            "decay_s": 0.08,
            "shape": "exp",
        },
    },
    "beat_pulse_red": {
        "kind": "palette_lookup",
        "params": {
            "scalar": {"kind": "constant", "params": {"value": 0.5}},
            "palette": "mono_ff0000",
            "brightness": {
                "kind": "beat_envelope",
                "params": {"decay_s": 0.4},
            },
        },
    },
    "snare_hit_sparkles": {
        "kind": "sparkles",
        "params": {
            "palette": "white",
            "density": 6.0,
            "decay": 2.5,
            "brightness": {
                "kind": "pulse",
                "params": {
                    "by": {"kind": "audio_band", "params": {"band": "high"}},
                    "floor": 0.0,
                },
            },
        },
    },
    "alternating_chase": {
        "kind": "comet",
        "params": {
            "axis": "u_loop",
            "speed": {
                "kind": "mul",
                "params": {
                    "a": 0.4,
                    "b": {
                        "kind": "step_select",
                        "params": {
                            "index": {"kind": "beat_index", "params": {"mod_n": 2}},
                            "values": [1.0, -1.0],
                        },
                    },
                },
            },
            "palette": "fire",
            "palette_pos": 0.7,
        },
    },
    # Headline pattern for "fireballs / comets shooting out from centre on
    # every beat". axial_dist=|x| means LEDs at the same |x| in each of the
    # 4 quadrants light up together — so a single comet head produces 4
    # simultaneous outward fronts (top-right, top-left, bottom-right,
    # bottom-left). `trigger: audio_beat()` is the optional modifier that
    # turns the continuous walker into a per-beat launcher.
    "beat_fireballs_outward": {
        "kind": "comet",
        "params": {
            "axis": "axial_dist",
            "spawn_position": 0.0,
            "speed": 2.0,
            "head_size": 0.05,
            "trail_length": 0.25,
            "trigger": {"kind": "audio_beat", "params": {}},
            "palette": "fire",
            "palette_pos": 0.6,
        },
    },
    # Concentric shockwaves around the centre point on every beat. Use
    # `ripple` (rings) when the user asks for circular shockwaves; use
    # `comet` (above) for linear/streaking fronts.
    "beat_concentric_rings": {
        "kind": "ripple",
        "params": {
            "axis": "radius",
            "rate": 0.0,
            "trigger": {"kind": "audio_beat", "params": {}},
            "speed": 1.0,
            "decay_s": 1.0,
            "palette": "ice",
            "palette_pos": 0.8,
        },
    },
    # Comet flying around the loop, relaunched from the top centre on each
    # beat — "every kick fires a fresh streak around the rig".
    "beat_loop_comet": {
        "kind": "comet",
        "params": {
            "axis": "u_loop",
            "spawn_position": 0.0,
            "speed": 1.5,
            "trail_length": 0.4,
            "trigger": {"kind": "audio_beat", "params": {}},
            "palette": "warm",
            "palette_pos": 0.5,
        },
    },
    "chromatic_drift": {
        "kind": "palette_lookup",
        "params": {
            "scalar": {"kind": "wave", "params": {"axis": "x", "speed": 0.2}},
            "palette": {
                "kind": "palette_hsv",
                "params": {"stops": [
                    {"pos": 0.0, "hue": 200.0},
                    {"pos": 1.0, "hue": 320.0},
                ]},
            },
        },
    },
}


ANTI_PATTERNS: list[str] = [
    "BEAT-SYNC RULE — for ANY 'pulse to the beat' / 'flash on the beat' / "
    "'follow the rhythm' / 'on every beat' / 'rhythmic strobe' request, the "
    "ONLY correct primitive is `audio_beat()` (or a wrapper like "
    "`beat_envelope` / `beat_index` / `strobe` / `ripple.trigger`). NEVER "
    "fake beat-sync with a hardcoded-period `lfo`, NEVER use a numeric BPM "
    "literal anywhere in the spec, NEVER threshold `audio_band(low)` to "
    "approximate kicks. There is no `bpm`, `bpm_clock`, `beat_count`, or "
    "`audio_bpm` primitive — actual musical beats come exclusively from "
    "`/audio/beat` via `audio_beat()` and friends. The bare `audio_band` "
    "(low/mid/high) primitive is for SMOOTH continuous reactivity, NOT for "
    "discrete beat triggering.",
    "AUDIO BAND ROUTING — `audio_band(band)` is for continuous loudness in a "
    "specific frequency range, not for the beat. Default to `audio_beat()` "
    "for rhythm-driven effects. Reach for `audio_band` only when the user's "
    "request explicitly names a frequency element — e.g. 'react to the bass' "
    "→ band=\"low\"; 'snare hits' / 'cymbals' / 'hi-hats' → band=\"high\"; "
    "'vocals' / 'mid-range energy' → band=\"mid\".",
    "There is no top-level `bindings` — modulation lives ON the parameter as "
    "a node. To modulate brightness, set `palette_lookup.brightness` (or "
    "`sparkles.brightness`) to a `pulse(by, floor)` node. There is no "
    "`bindings.brightness` block.",
    "Audio-reactive brightness wants `pulse(by=audio_band(...), floor=…)`, "
    "NOT a raw `audio_band`. Raw `audio_band` returns 0 on silence so the "
    "effect goes invisible whenever the music dips; `pulse` gives you a "
    "configurable floor (effect stays visible) while peaks still reach 1.0. "
    "Pick floor ≈ 0.5–0.7 for a clear pulse, ≈ 0.3 for dramatic dynamics, "
    "≈ 0.9 for very subtle reactivity, 0.0 only when the layer is *meant* "
    "to disappear on silence (e.g. an additive accent on top of another "
    "always-visible base layer).",
    "Static `brightness < 1` is wrong — that bakes a ceiling that prevents "
    "peaks reaching 100%. For static dimming, use the layer's `opacity`; "
    "for global dimming, the master brightness slider.",
    "`palette` is itself a node: bare strings (\"fire\") are sugar for "
    "`palette_named`. There is no `palette: \"red\"` — use `mono_ff0000`.",
    "`mix` is polymorphic; do not reach for a separate `palette_mix` — "
    "`mix(palette_a, palette_b, t)` is the palette crossfade.",
    "`mul(rgb_field, palette)` is rejected. Convert the palette to rgb_field "
    "first via `palette_lookup`.",
    "Discrete params (`axis`, `shape`, `band`, `direction`) are baked at compile "
    "time and cannot be modulated. Numeric params are `NumberOrNode` and accept "
    "either a literal or a scalar_t/scalar_field node.",
    "Audio is read via `audio_band` with band ∈ {low, mid, high}. Values come "
    "from the external audio-feature server, already smoothed and auto-scaled "
    "to ~[0, 1]. Pick the band that matches the musical element you want to "
    "track (low=kick, mid=vocals/snare, high=hats). All attack/release and "
    "smoothing live upstream — retune them in the audio server's UI, not here.",
    "`mix.t` is the lerp factor — it is a `scalar_t` (one number per frame), "
    "not a `scalar_field`. Feed it `lfo`, `audio_band`, or a literal 0..1 "
    "number; do NOT feed it `position`/`wave`/`noise` (those are per-LED).",
    "To split the install spatially (top half vs bottom half, etc.) use a "
    "`scalar_field` like `position` as the `palette_lookup.scalar` directly, or "
    "build it via `add`/`mul`/`mix` of two `scalar_field`s — `mix.t` cannot "
    "do per-LED splits because its blend factor is a single number.",
    "`wave.shape: cosine` *plateaus* color near peaks and troughs (its "
    "derivative is zero there), so dozens of adjacent LEDs end up the same "
    "colour and you get visible block artefacts on a smooth-palette sweep. "
    "Default is `sawtooth` — use it whenever you want flowing/scrolling colour. "
    "Pick `cosine` only when you actually want a breathing pulse on a mono "
    "palette (where the brightness up-down is the point and there's no "
    "colour gradient to band).",
]


def _kind_table_row(prim: type[Primitive]) -> str:
    pjson = json.dumps(
        _compact_params_schema(prim.Params), separators=(",", ":")
    )
    out_kind = prim.output_kind or "polymorphic"
    return f"  {prim.kind:18s} [{out_kind}]  {prim.summary}\n    params: {pjson}"


def _compact_params_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Compact JSON-schema-ish summary: per-field {type, default, enum, description}.

    Avoids the full pydantic dump (which inlines $defs and explodes the prompt
    with title/format/etc. fluff). The full schema is still available via
    `prim.Params.model_json_schema()` for the operator UI / GET /surface/primitives.
    """
    schema = model.model_json_schema()
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    out: dict[str, Any] = {}
    for name, field in props.items():
        entry: dict[str, Any] = {}
        if "type" in field:
            entry["type"] = field["type"]
        if "anyOf" in field:
            entry["any_of"] = [
                b.get("type", b.get("$ref", "any")) for b in field["anyOf"]
            ]
        if "enum" in field:
            entry["enum"] = field["enum"]
        if "default" in field:
            entry["default"] = field["default"]
        if name in required:
            entry["required"] = True
        if "description" in field:
            entry["desc"] = field["description"]
        out[name] = entry
    return out


def generate_docs(
    *,
    topology: Topology | None = None,
    audio_state: Any | None = None,
    engine_state: Any | None = None,
) -> str:
    """Build the prompt-ready CONTROL SURFACE block.

    `topology` / `audio_state` / `engine_state` are accepted for symmetry with
    callers that pass them through (and so future masters / palettes can read
    them), but the v1 docs are self-contained from the registry.
    """
    by_kind: dict[str, list[type[Primitive]]] = {
        "scalar_field": [],
        "scalar_t": [],
        "palette": [],
        "rgb_field": [],
        "polymorphic": [],
    }
    for prim in REGISTRY.values():
        key = prim.output_kind or "polymorphic"
        by_kind.setdefault(key, []).append(prim)

    sections: list[str] = []
    sections.append(
        "CONTROL SURFACE — primitives compose into a tree. Every node is "
        "{kind, params}. Numeric params accept either a literal or a node; "
        "discrete params (axis, shape, band, …) are literal-only. Bare numbers "
        "are sugar for `constant`; bare palette strings are sugar for "
        "`palette_named`. Strict validation: unknown keys fail with a "
        "structured error you can read on the next turn."
    )
    sections.append(
        "OUTPUT KINDS\n"
        "  scalar_field — per-LED scalar [0, 1] (spatial)\n"
        "  scalar_t     — single scalar/frame (time-only)\n"
        f"  palette      — {_palettes.LUT_SIZE}-entry RGB LUT\n"
        "  rgb_field    — per-LED RGB; the layer leaf"
    )

    # Named coordinate frames the agent addresses by name (in `wave.axis`,
    # `gradient.axis`, `position.axis`, `frame.axis`). Precomputed at
    # topology build time, immutable thereafter.
    sections.append(
        "FRAMES (named coordinate axes — pass as `axis: \"<name>\"`)\n"
        + "\n".join(
            f"  {name:14s}  {desc}" for name, desc in FRAME_DESCRIPTIONS.items()
        )
    )

    for header_kind, label in [
        ("scalar_field", "KIND: scalar_field"),
        ("scalar_t", "KIND: scalar_t"),
        ("palette", "KIND: palette"),
        ("rgb_field", "KIND: rgb_field"),
        ("polymorphic", "KIND: polymorphic combinators"),
    ]:
        prims = sorted(by_kind.get(header_kind, []), key=lambda p: p.kind)
        if not prims:
            continue
        rows = [_kind_table_row(p) for p in prims]
        sections.append(label + "\n" + "\n".join(rows))

    sections.append(
        "AUDIO ROUTING (READ THIS FIRST FOR EVERY REACTIVE REQUEST)\n"
        "  • 'beat / rhythm / pulse to the music / flash on the beat / drop'\n"
        "      → use `audio_beat()` (or `beat_envelope`, `beat_index`, "
        "`strobe`, or any primitive's `trigger:` slot).\n"
        "      `audio_beat()` fires on actual musical onsets from the "
        "external detector. Hardcoded-BPM clocks and `audio_band` thresholds\n"
        "      DRIFT and produce mushy results — they are not available.\n"
        "  • Beat-triggered EMISSION is an OPTIONAL MODIFIER, not a "
        "primitive switch. Pick the visual primitive based on what the "
        "user wants to see (linear walker → `comet`, rings → `ripple`, "
        "evenly-spaced dots → `chase_dots`); then add `trigger: "
        "audio_beat()` to turn 'continuous motion' into 'launches on every "
        "beat'. Comet and ripple both expose a `trigger:` slot for exactly "
        "this — same audio source, same idiom, different visual.\n"
        "  • Common templates (see EXAMPLES for full specs):\n"
        "      - 'fireballs shooting out from centre on the beat (top + "
        "bottom)' → `comet(axis=\"axial_dist\", spawn_position=0, "
        "trigger=audio_beat(), speed≈2.0, palette=\"fire\", "
        "palette_pos≈0.6)`. axial_dist mirrors to 4 quadrants → 4 "
        "simultaneous outward fronts per beat.\n"
        "      - 'concentric shockwaves / rings expanding from the centre "
        "on the beat' → `ripple(axis=\"radius\", rate=0, "
        "trigger=audio_beat(), …)`.\n"
        "      - 'comets flying around the rig launched on every beat' → "
        "`comet(axis=\"u_loop\", trigger=audio_beat(), …)`.\n"
        "  • 'react to bass / kick / sub / low end'   → "
        "`audio_band(band=\"low\")` (continuous, e.g. via `pulse`).\n"
        "  • 'react to vocals / mid-range / snare body'  → "
        "`audio_band(band=\"mid\")`.\n"
        "  • 'snare hits / hi-hats / cymbals / sparkle / shimmer / high end' "
        " → `audio_band(band=\"high\")`.\n"
        "  • Generic 'react to the music' with no specifics → default to "
        "`audio_beat()`-driven motion (it's the headline reactive primitive).\n"
        "  • There is NO bpm / tempo / clock primitive. If you ever feel "
        "tempted to write a numeric bpm literal, stop — use `audio_beat()`."
    )
    sections.append(
        "BLEND MODES (layer-level): normal, add, screen, multiply"
    )
    sections.append(
        "NAMED PALETTES (use as `palette: \"<name>\"` or via palette_named):\n"
        "  " + ", ".join(sorted(NAMED_PALETTES)) + ", mono_<hex>\n"
        "  IMPORTANT — palette_pos brightness gradient: most named palettes "
        "are dark→bright (`fire`/`ice`/`sunset`/`ocean`/`sparks` start "
        "near-black at pos 0). Pos 0.0–0.2 = essentially OFF. Pos 0.5–0.8 = "
        "the saturated, vibrant range. Pos 0.8–1.0 = highlights / wash-out. "
        "For 'red fireballs' on `fire` use palette_pos ~0.4–0.6 (rich red); "
        "0.1 will render as black on the rig. For pure single colours use "
        "`mono_<hex>` (e.g. `mono_ff0000` for pure red) — palette_pos is "
        "irrelevant there. `rainbow` and `warm` are uniformly bright across "
        "their range; the others trade off."
    )

    examples_block = "EXAMPLES\n"
    for name, tree in EXAMPLE_TREES.items():
        examples_block += f"  {name}: " + json.dumps(tree, separators=(",", ":")) + "\n"
    sections.append(examples_block.rstrip())

    sections.append(
        "ANTI-PATTERNS (the model keeps reaching for these — don't)\n"
        + "\n".join(f"  - {ap}" for ap in ANTI_PATTERNS)
    )

    return "\n\n".join(sections)


def primitives_json() -> dict[str, Any]:
    """JSON catalogue for `GET /surface/primitives`.

    Returns full pydantic model schemas, *without* shrinking — the operator
    UI builds form fields from these and doesn't share the agent's token
    budget.
    """
    out: dict[str, Any] = {}
    for kind, prim in REGISTRY.items():
        out[kind] = {
            "kind": kind,
            "output_kind": prim.output_kind or "polymorphic",
            "summary": prim.summary,
            "params_schema": prim.Params.model_json_schema(),
        }
    return out
