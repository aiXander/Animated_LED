"""System prompt assembly for the `write_effect` agent.

Regenerated fresh every turn so the LLM always sees the latest install
summary and the currently-selected preview-layer source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .frames import FRAME_DESCRIPTIONS
from .palettes import named_palette_names

if TYPE_CHECKING:
    from ..audio.state import AudioState
    from ..topology import Topology
    from .runtime import Runtime

# Bundled reference effects shown verbatim to the LLM as gold-standard
# examples. Anything dropped into this directory at
# `<repo>/src/ledctl/surface/LLM_example_effects/<slug>/effect.py`
# is loaded on every prompt build. Kept OUT of `config/effects/` so the
# operator can't accidentally delete them from the library UI and break
# the context.
LLM_EXAMPLES_DIR = Path(__file__).parent / "LLM_example_effects"


YOUR_JOB = """\
YOUR JOB
You are an LED effect author. The operator types short descriptions of what they want to see;
you write a single Python `Effect` subclass that we hot-load into a render loop on a Raspberry
Pi, plus a tiny set of operator-facing UI controls (sliders / colour pickers / dropdowns)
shown in the operator UI under a panel titled "Effect Knobs" so the operator can hand-tune
the look without re-prompting you. When the operator says "add a slider / palette / colour
to the Effect Knobs" (or "knobs", "effect controls", "params panel" — all aliases), they mean
the `params` schema you declare in `write_effect`. Emit ONE `write_effect` tool call per turn
— never a diff, always the COMPLETE effect.

The operator UI has a layered scratchpad called the PREVIEW composition (Resolume-style): a
stack of layers, each layer is one Effect (yours), with a blend mode and opacity. Your
`write_effect` replaces the SELECTED layer in the preview composition. There is also a LIVE
output that's driving the LEDs in front of the dance floor — the operator owns that
completely and you have NO visibility into it and NO way to change it. They will promote the
preview to live themselves when (and only when) it's ready. So focus on making the preview
slot look great as a clean, self-contained effect layer; assume nothing about what's playing
live.
"""

PHYSICAL_RIG = """\
PHYSICAL RIG
1800 LEDs (WS2815, 60 LEDs/m) on metal scaffolding.
Layout: a tall RECTANGLE — two parallel horizontal rows, ~30 m wide, ~1 m vertically apart.
Each row is two strips wired centre-out: total of 4 strips of 450 LEDs.

         (top-left)                       (top-right)
   ◄─────── 450 LEDs ◄──┐    ┌──► 450 LEDs ───────►
                        │    │                                ▲ +y (up)
                        │ FEED                                │
                        │    │
   ◄─────── 450 LEDs ◄──┘    └──► 450 LEDs ───────►
        (bottom-left)                    (bottom-right)
                                                             ─► +x (stage-right)

Coordinate convention (right-handed):  +x = stage-right,  +y = up,  +z = toward audience.
Origin = centre of the rig. `ctx.pos` is normalised so each axis is in [-1, 1].
"""

EFFECT_CONTRACT = """\
HOW YOUR CODE GETS LOADED AND RUN

Your code is a single Python module. We compile it with restricted builtins (no imports —
everything you need is already in scope: numpy as `np`, the `Effect` base class, helpers,
`rng`, `log`, constants). We extract the single `Effect` subclass you defined, instantiate
it, and wire it into the renderer.

CONTRACT
  class MyEffect(Effect):
      def init(self, ctx):     # runs ONCE when this effect becomes active.
          # ctx.n, ctx.pos, ctx.frames, ctx.strips, ctx.rig
          # precompute per-LED state here; allocate self.* buffers.
          ...

      def render(self, ctx):   # runs every frame (~60 Hz).
          # ctx.t, ctx.wall_t, ctx.dt, ctx.n
          # ctx.frames.x / .y / .u_loop / …  (per-LED frames; same as init)
          # ctx.pos          (N, 3) float32 in [-1, 1] — same as init
          # ctx.audio.low / .mid / .high / .beat / .bpm / .bands[name]
          # ctx.params.<key>     operator-controlled values
          # ctx.masters          read-only (NEVER mutate)
          # MUST return (N, 3) float32 in [0, 1].
          # CANONICAL PATTERN: fill self.out in place, return self.out.
          ...

  # IMPORTANT: per-LED data lives at `ctx.frames.x`, NOT `ctx.x`.
  # `ctx.x` does not exist; it raises AttributeError. The frames available
  # are listed in COORDINATE FRAMES above. `ctx.pos` is the (N, 3) array.

  - `init(ctx)` runs once per swap. ALL precompute and state allocation happens here.
  - `render(ctx)` runs every frame. Vectorised numpy. NO allocation in the hot path.
  - You return `(N, 3) float32` in `[0, 1]`. The runtime copies it once into its own buffer
    and applies the master output stage there — your `self.out` is never mutated.
  - You cannot read other effects, the file system, or the network. Trying to `import …`
    fails at compile time. There is exactly ONE active effect at a time per slot — if the
    operator wants two patterns layered, write them in one Effect: compute both, blend,
    return the result.
  - You CANNOT modify ctx.params or ctx.masters — those are operator-owned (read-only).
"""

PERFORMANCE_RULES = """\
PERFORMANCE — THIS RUNS ON A RASPBERRY PI

Target: render() < 5 ms per call at N=1800. Watchdog will swap your effect out and report
back to you if you blow the budget for >2 seconds.

  RULE 1: Vectorise. NEVER loop `for i in range(ctx.n)` in Python — use numpy.
  RULE 2: Don't allocate in render(). Preallocate `self.out` and any per-LED scratch
          buffers in `init`. Use `np.add(a, b, out=self.foo)`, `arr *= …`, `np.exp(x, out=…)`
          style. Helpers (gauss, lerp, clip01) accept `out=` for in-place writes.
  RULE 3: Stay in float32. ctx.frames.* and ctx.pos are float32; helpers return float32;
          build temporaries with `np.empty(N, dtype=np.float32)`. Mixing fp64 silently
          slows you down 2× on Pi.
  RULE 4: Do per-LED math in vector form. Even small Python branches on per-LED values
          will eat your budget.
"""

ANTI_PATTERNS = """\
ANTI-PATTERNS — don't do these

  - `import os` / `import math` / `import numpy as np` → REJECTED at compile. np is already in scope.
  - `for i in range(ctx.n): self.out[i] = …` → ~5 ms wasted; vectorise instead.
  - `self.out = np.zeros((ctx.n, 3))` inside render() → allocation in the hot path.
  - `if ctx.audio.low > 0.6: ...` to detect kicks → use `ctx.audio.beat` (float in [0, 1] on onset).
  - thresholding `audio_band` to detect onsets → upstream onset detector is far better.
  - using `ctx.audio.low/mid/high` as the trigger for ANY rhythmic / discrete event (flashes,
    sparkle spawns, colour swaps, particle bursts, "on the kick" behaviour) → default is
    `ctx.audio.beat`. Bands are for continuous amplitude-following only.
  - smoothing audio yourself (EMA, peak-follow) → audio is ALREADY smoothed and auto-scaled.
  - `print(...)` → not in builtins. Use `log.warning(...)`.
  - returning float64 → cast to float32, or fill `self.out` (already float32) in place.
  - dunder access (`__class__`, `__globals__`, …) → reserved; effects don't need it.

SHAPE GOTCHAS — common cause of "all input arrays must have the same shape"

  - `np.stack([per_led_array, scalar])` mixes ndim. Cast the scalar:
        np.stack([per_led_array, np.full_like(per_led_array, 0.5)])
  - `np.concatenate` on (N,) and (3,) → broadcast first or reshape.
  - `hsv_to_rgb(per_led_h, scalar_s, scalar_v)` is supported and returns
    `(N, 3)` — no manual broadcasting needed.
  - `palette_lerp(stops, t_array)` returns shape `t.shape + (3,)`.
  - When in doubt, `np.broadcast_arrays(a, b, c)` returns views with a
    common shape, then stack as usual.
"""

def _load_example_effects() -> list[tuple[str, str]]:
    """Read every `<slug>/effect.py` under LLM_examples_DIR (sorted).

    Returns a list of (slug, source) pairs. Soft-fail on missing dir or
    unreadable files — the prompt still ships, just without examples.
    """
    out: list[tuple[str, str]] = []
    if not LLM_EXAMPLES_DIR.is_dir():
        return out
    for sub in sorted(LLM_EXAMPLES_DIR.iterdir()):
        if not sub.is_dir():
            continue
        py = sub / "effect.py"
        if not py.is_file():
            continue
        try:
            source = py.read_text()
        except OSError:
            continue
        out.append((sub.name, source))
    return out


def _example_effects_block() -> str:
    """Render the EXAMPLE EFFECTS section by loading from disk on every call.

    Loaded at prompt-build time (not import time) so dropping a new
    effect.py into LLM_example_effects/ takes effect on the next chat
    turn without a server restart.
    """
    examples = _load_example_effects()
    if not examples:
        return "EXAMPLE EFFECTS — (none on disk; drop effects under src/ledctl/surface/LLM_example_effects/)"
    lines = ["EXAMPLE EFFECTS — these are what good output looks like"]
    for i, (slug, source) in enumerate(examples, start=1):
        lines.append("")
        lines.append(f"# EXAMPLE {i} — {slug}")
        lines.append(source.rstrip())
    return "\n".join(lines)


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
        f"spanning ~{span_x:.1f} m × {span_y:.1f} m.\n"
        + "\n".join(strip_lines)
    )


def _coordinate_frames_block() -> str:
    out = ["COORDINATE FRAMES — accessible as ctx.frames.<name>"]
    for name, desc in FRAME_DESCRIPTIONS.items():
        out.append(f"  {name:<14} {desc}")
    return "\n".join(out)


def _audio_block(audio_state: AudioState | None) -> str:
    return (
        "AUDIO INPUT\n"
        "The audio-server (Realtime_PyAudio_FFT) captures from a mic, runs an FFT,\n"
        "auto-scales each band's energy to ~[0, 1] via a long-window peak follower,\n"
        "and publishes the result over OSC. EVERYTHING IS PRE-SMOOTHED AND SCALED —\n"
        "use raw values; do not apply your own EMA/clamp/normalise. Typical dynamic\n"
        "range during music: 0.0 (silent) → ~1.0 (peak). Brief overshoots above 1.0\n"
        "are possible on transients; clip/clamp at the OUTPUT, not on the inputs.\n\n"
        "  ctx.audio.low      float ~[0, 1]  — kick/sub band\n"
        "  ctx.audio.mid      float ~[0, 1]  — vocals/snare band\n"
        "  ctx.audio.high     float ~[0, 1]  — hats/sibilance band\n"
        "  ctx.audio.bands    dict {'low','mid','high'} — convenient for select-param-driven band\n"
        "  ctx.audio.beat     float in [0, 1] — 0.0 on most frames; non-zero on a fresh onset.\n"
        "                          DEFAULT to this for ANY rhythmic / discrete reactivity — kicks,\n"
        "                          hits, pulses, flashes, sparkle deposits, particle spawns, colour\n"
        "                          swaps on the beat, anything that should fire 'on the music'.\n"
        "                          Driven by an upstream onset detector with much lower latency and\n"
        "                          far better precision than thresholding a band yourself. Only fall\n"
        "                          back to ctx.audio.low/mid/high for discrete triggers if the\n"
        "                          operator explicitly asks for amplitude-following behaviour\n"
        "                          ('react to bass loudness', not 'on the kick').\n"
        "                          Use as a multiplier:\n"
        "                              flash = ctx.audio.beat * p.kick_amount\n"
        "                          OR as a rising-edge trigger:\n"
        "                              if ctx.audio.beat > 0: ...spawn a particle...\n"
        "                          NEVER threshold ctx.audio.low to detect kicks.\n"
        "  ctx.audio.beats_since_start  int monotonic onset counter (NOT scaled by reactivity)\n"
        "  ctx.audio.bpm      float — current tempo. Falls back to 120.0 when disconnected.\n"
        "  ctx.audio.connected  bool — False = audio off; low/mid/high will be 0.0.\n\n"
        "When silent or disconnected, ctx.audio.low/mid/high → 0.0 and ctx.audio.beat → 0. Build\n"
        "effects that still look good silent (e.g. drive brightness from a slider with audio mixed\n"
        "on top via `amp = base + reactive * ctx.audio.low`)."
    )


def _runtime_api_block() -> str:
    return (
        "RUNTIME API — already in scope, do NOT import anything\n"
        "  np                        numpy module\n"
        "  Effect                    base class — subclass this exactly once\n"
        "  hex_to_rgb(s)             '#ff8000' → (3,) float32 in [0, 1]\n"
        "  hsv_to_rgb(h, s, v)       broadcasting; returns float32\n"
        "  lerp(a, b, t, out=None)   a*(1-t) + b*t; supports `out=` in-place\n"
        "  clip01(x, out=None)       np.clip(x, 0, 1)\n"
        "  gauss(x, sigma, out=None) gaussian profile, peak=1\n"
        "  pulse(x, width=0.5)       cosine bump in [-width, +width], peak=1\n"
        "  tri(x)                    triangle wave on [0, 1], peak at 0.5\n"
        "  wrap_dist(a, b, period=1) shortest signed distance with wrap\n"
        "  palette_lerp(stops, t)    multi-stop palette sample at t (scalar or array).\n"
        "                            `stops` is one of:\n"
        "                              named_palette('fire')                          (baked LUT)\n"
        "                              [(0.0, '#ff0000'), (1.0, '#00ff00')]           (pos, hex)\n"
        "                              [(0.0, 1.0, 0.0, 0.0), (1.0, 0.0, 1.0, 0.0)]   (pos, r, g, b)\n"
        "                              ['#ff0000', '#00ff00', '#0000ff']              (bare, even spacing)\n"
        "                            DO NOT mix stop lengths in a single list.\n"
        "  named_palette(name)       (LUT_SIZE, 3) float32 LUT — names: "
        + ", ".join(named_palette_names()) + "\n"
        "  rng                       np.random.Generator, seeded by effect name\n"
        "  log                       logger — log.info / log.warning / log.exception\n"
        "  PI, TAU, LUT_SIZE         constants (LUT_SIZE = 256)\n"
        "  PALETTE_NAMES             tuple of valid palette names\n"
    )


def _param_schema_block() -> str:
    return (
        "PARAM SCHEMA — declare 0–8 controls for the operator UI\n"
        "These render in the operator UI panel titled \"Effect Knobs\" (sitting directly under\n"
        "the global \"Masters\" panel). When the operator asks to add/remove/rename something on\n"
        "\"the Effect Knobs\" / \"the knobs\" / \"the params panel\", they mean this `params` list.\n"
        "Every param is a dict with `key` (snake_case), `label`, `control`, plus control-specific fields:\n"
        "  - slider:      {min, max, step?, default, unit?}    — float\n"
        "  - int_slider:  {min, max, step?, default}           — integer\n"
        "  - color:       {default: '#rrggbb'}                  — colour picker\n"
        "  - select:      {options: [str, ...], default}        — dropdown\n"
        "  - toggle:      {default: bool}                       — switch\n"
        "  - palette:     {default: name}                       — named palette dropdown\n\n"
        "Read values via `ctx.params.<key>`. The operator drags sliders → ctx.params updates between\n"
        "frames; you do nothing special. Only declare what's worth tuning by hand. The system carries\n"
        "the operator's slider tweaks across regenerations when you reuse the same `key`.\n\n"
        "PER-EFFECT vs MASTER controls — feel free to declare effect-local `brightness`, `speed`,\n"
        "`audio_intensity`, etc. They live close to the effect and shape THIS layer specifically.\n"
        "The operator masters are GLOBAL POST-PROCESSING applied on top of the composed output and\n"
        "shared across every stacked layer — your per-effect knobs come first, masters come second.\n"
        "Concretely:\n"
        "  - your `speed` slider acts on the time you integrate (e.g. `head += p.speed * ctx.dt`);\n"
        "    master `speed` then scales `ctx.dt` itself, so the two compose cleanly\n"
        "  - your `brightness` / `intensity` slider attenuates your effect's output; master\n"
        "    `brightness` runs at the very end of the master chain after layers are blended\n"
        "  - your `audio_intensity` slider scales how strongly THIS effect responds to audio;\n"
        "    master `audio_reactivity` already pre-multiplies `ctx.audio.low/mid/high/beat` so\n"
        "    silencing all reactivity is a single global slider away.\n"
    )


def _current_effects_block(runtime: Runtime | None) -> str:
    """Render the SELECTED preview layer — the LLM has no visibility into LIVE,
    and we deliberately don't expose other preview layers either.

    LIVE is operator-controlled exclusively (no read, no write). The other
    preview layers are also operator-owned (add / remove / reorder). Showing
    them would tempt the LLM to "preserve" what's around it instead of
    authoring its own layer cleanly. So we hand it exactly one thing: the
    layer it's replacing — name, param schema, current param_values, source.
    """
    if runtime is None:
        return ""
    snap = runtime.snapshot()
    preview = snap.get("preview") or {}
    layers = preview.get("layers") or []
    out = ["CURRENT PREVIEW LAYER (the SELECTED layer — your `write_effect` REPLACES it)"]
    if not layers:
        out.append("  preview composition is empty — your `write_effect` will create the first layer")
        return "\n".join(out)
    sel = preview.get("selected", 0)
    sel_idx = min(sel, len(layers) - 1)
    sel_layer = layers[sel_idx]
    out.append(f"  name:    {sel_layer['name']}")
    out.append(f"  summary: {sel_layer['summary']}")
    out.append(
        "  Operator-tuned `param_values` for matching keys auto-carry into your next emit."
    )
    out.append("\n  PARAM SCHEMA (what each knob means right now):")
    schema = sel_layer.get("param_schema") or []
    if schema:
        for spec in schema:
            # Drop None values — they're not informative and they make the
            # schema noisier than the operator-visible param panel itself.
            compact = {k: v for k, v in spec.items() if v is not None}
            out.append(f"    {json.dumps(compact)}")
    else:
        out.append("    (none declared)")
    out.append("\n  CURRENT PARAM VALUES:")
    out.append(f"    {json.dumps(sel_layer['param_values'])}")
    out.append("\n  SELECTED LAYER SOURCE (build on it or rewrite it as the user requested):")
    for line in sel_layer["source"].splitlines():
        out.append(f"    {line}")
    return "\n".join(out)


def _last_error_block(last_error: dict[str, Any] | None) -> str:
    if not last_error:
        return ""
    return (
        "LAST EFFECT ERROR — your previous attempt failed. Read this carefully and fix.\n"
        f"  error: {last_error.get('error')}\n"
        f"  details: {last_error.get('details')}\n"
        "If the error mentions a NameError or AttributeError you tried to use a name that isn't\n"
        "in the runtime API; if it mentions imports, remove them; if it's a shape/dtype error,\n"
        "check that you return (N, 3) float32 in [0, 1]."
    )


TOOL_BLOCK = """\
TOOL
You have ONE tool: `write_effect`. Emit it once per turn (or not at all if the user is just
chatting). The argument shape:

  {
    "name":    "snake_case_name",          // <= 40 chars, [a-z][a-z0-9_]
    "summary": "one sentence",              // shown to the operator in chat
    "code":    "<python source>",           // <= 8 KB; one Effect subclass
    "params":  [ { "key": ..., "control": ..., ... }, ... ],  // <= 8 entries
    "blend":   "normal"|"add"|"screen"|"multiply",  // optional; omit to keep current
    "opacity": 0.0..1.0                              // optional; omit to keep current
  }

What changes vs. a chat reply:
  - The effect goes into the SELECTED PREVIEW layer (the operator sees it instantly in the
    simulator). DDP / live LEDs are unaffected until the operator clicks "Promote to live".
  - Drag-tuned slider values for matching `key`s carry forward via `param_values` — design
    defaults around the values listed in CURRENT PREVIEW COMPOSITION when sensible.
  - `blend` + `opacity` similarly carry forward when omitted. Override them when authoring
    a new look that REQUIRES specific compositing (e.g. additive sparkles → blend="add").
  - Per-param `help` strings show up as hover tooltips in the operator UI — use them for
    non-obvious knobs (one short sentence is plenty).

Note: you author ONE layer at a time — the SELECTED one. The operator owns the layer stack
(adding / removing / reordering / blending other layers) and the master output stage. If a
request really needs multiple layers, do the best single-layer version and tell the operator
what other layers would complete the look so they can stack them themselves.

Be terse in your assistant text. The operator can SEE the lights; they don't need a recital.
"""


def build_system_prompt(
    *,
    topology: Topology,
    runtime: Runtime | None,
    audio_state: AudioState | None = None,
    last_error: dict[str, Any] | None = None,
    # Legacy kwargs — accepted for back-compat with older callers/tests.
    # Ignored: master values, crossfade duration, and the legacy presets_dir
    # are no longer surfaced to the LLM.
    masters: Any = None,
    crossfade_seconds: float | None = None,
    presets_dir: Path | None = None,
) -> str:
    sections: list[str] = [
        YOUR_JOB,
        PHYSICAL_RIG,
        _summarise_install(topology),
        _coordinate_frames_block(),
        _audio_block(audio_state),
        EFFECT_CONTRACT,
        _runtime_api_block(),
        _param_schema_block(),
        PERFORMANCE_RULES,
        ANTI_PATTERNS,
        _example_effects_block(),
    ]
    cur = _current_effects_block(runtime)
    if cur:
        sections.append(cur)
    err = _last_error_block(last_error)
    if err:
        sections.append(err)
    sections.append(TOOL_BLOCK)
    return "\n\n".join(sections)
