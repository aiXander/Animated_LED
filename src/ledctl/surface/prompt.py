"""System prompt assembly for the `write_effect` agent.

Regenerated fresh every turn so the LLM always sees the latest install
summary and the source of the effect it is about to replace.

Structure (single canonical home per topic — no restatements):

  ROLE                — your job + scope of authorship
  TOOL                — write_effect JSON shape + carry-forward rules
  PHYSICAL RIG        — geometry + ASCII + 2D-plane caveat
  INSTALL             — per-strip layout (from topology, live)
  COORDINATE FRAMES   — ctx.frames.<name> reference
  AUDIO INPUT         — ctx.audio.* + the canonical beat-vs-bands rule
  EFFECT CONTRACT     — code skeleton + ctx surface + sandbox rules
  RUNTIME API         — helpers / constants pre-injected into the module
  PARAM SCHEMA        — 0–8 operator controls + per-effect vs master
  PERFORMANCE         — Pi budget, RULE 0 (no for loops), allocation, float32
  ANTI-PATTERNS       — concrete mistakes (no duplicates of rules above)
  EXAMPLE EFFECTS     — complete gold-standard write_effect payloads
  CURRENT EFFECT      — current effect source + param values
  LAST EFFECT ERROR?  — only when the previous turn failed
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .frames import FRAME_DESCRIPTIONS
from .palettes import named_palette_names

if TYPE_CHECKING:
    from ..audio.state import AudioState
    from ..topology import Topology
    from .runtime import Runtime

# Bundled reference effects shown to the LLM as gold-standard COMPLETE
# write_effect payloads: `<slug>/effect.py` (code) + `<slug>/effect.yaml`
# (name / summary / params sidecar, persistence format). Dropped at
# `<repo>/src/ledctl/surface/LLM_example_effects/<slug>/`. Kept OUT of
# `config/effects/` so the operator can't accidentally delete them from
# the library UI and break the context.
LLM_EXAMPLES_DIR = Path(__file__).parent / "LLM_example_effects"


ROLE = """\
ROLE
You author LED effects for a stage rig. Each turn → ONE `write_effect` tool call
with a COMPLETE Python `Effect` subclass + 0–8 operator controls. Never a diff.

`write_effect` REPLACES the current effect with the one you author. Write it clean and
self-contained — assume nothing about what was there before.

Operator vocabulary: "knobs" / "effect controls" / "params panel" = the `params` schema you
declare (renders under "Effect Knobs", directly below global "Masters"). Be terse in your
assistant text — the operator can SEE the lights; no recital needed.
"""

TOOL_BLOCK = """\
TOOL — `write_effect` (once per turn; omit if the user is just chatting)

  {
    "name":    "snake_case_name",     // <= 40 chars, [a-z][a-z0-9_]
    "summary": "one sentence",        // shown to operator in chat
    "code":    "<python source>",     // <= 8 KB; one Effect subclass
    "params":  [ {"key": ..., "control": ..., ...}, ... ]    // <= 8 entries
  }

  - Operator-tuned `param_values` carry forward for matching `key`s — design defaults around
    the values listed in CURRENT EFFECT. Pick new keys when a knob's meaning changes.
  - Per-param `help` strings render as hover tooltips — one sentence for non-obvious knobs.
  - ONE Effect per call. If a request needs several elements at once, blend them inside the one
    Effect (see EFFECT CONTRACT).
"""

PHYSICAL_RIG = """\
PHYSICAL RIG
1800 LEDs (WS2815, 60 LEDs/m) on metal scaffolding — a tall RECTANGLE: two parallel horizontal
rows, ~30 m wide, ~1 m apart. Each row is two strips wired centre-out (4 × 450 LEDs).

         (top-left)                       (top-right)
   ◄─────── 450 LEDs ◄──┐    ┌──► 450 LEDs ───────►
                        │ FEED │                              ▲ +y (up)
   ◄─────── 450 LEDs ◄──┘    └──► 450 LEDs ───────►
        (bottom-left)                    (bottom-right)       ─► +x (stage-right)

Right-handed coords: +x = stage-right, +y = up, +z = audience. Origin = rig centre.
`ctx.pos` is normalised so each axis is in [-1, 1].

NOT A 2D PLANE. Every LED has y ∈ {+1, -1} — none in between. 2D particle physics that lets
pixels roam in [-1, 1]² mostly fades to black. Either pin each particle's y to ±1 (drive
motion along x), or work 1D: `u_loop` walks the rectangle clockwise, `chain_index` walks each
strip, `signed_x` is the shared horizontal axis. `side_signed` masks rows (+1 top, -1 bottom).
"""

EFFECT_CONTRACT = """\
EFFECT CONTRACT
Compiled with restricted builtins — NO imports. `np`, `Effect`, helpers, `rng`, `log`, constants
are pre-injected. The single `Effect` subclass is extracted and instantiated.

  class MyEffect(Effect):
      def init(self, ctx):     # ONCE on swap-in. Precompute, allocate self.* buffers.
          ...
      def render(self, ctx):   # every frame (~60 Hz). Vectorised numpy only.
          ...                  # MUST return (N, 3) float32 in [0, 1].
                               # Canonical: fill self.out in place, `return self.out`.

ctx surface (READ-ONLY — never mutate params / masters):
  ctx.n / ctx.t / ctx.wall_t / ctx.dt       sizes & times (dt clamped on hiccup)
  ctx.pos                                   (N, 3) float32 in [-1, 1]
  ctx.frames.<name>                         per-LED scalars — see COORDINATE FRAMES
  ctx.audio.<...>                           see AUDIO INPUT
  ctx.params.<key>                          operator-controlled, auto-updated between frames
  ctx.masters                               diagnostic view (do NOT write)
  ctx.strips / ctx.rig                      topology, init-time only

  - `ctx.frames.x` (NOT `ctx.x` — raises AttributeError).
  - Runtime copies your return into its own buffer before masters, so `self.out` survives
    across frames — safe to carry state.
  - No filesystem, no network, no `import`. ONE Effect per call; for multi-element requests,
    blend inside one Effect.
"""

PERFORMANCE_RULES = """\
PERFORMANCE — Raspberry Pi target
render() < 5 ms at N=1800. WATCHDOG: p95 over 5 ms for ~30 consecutive frames (~0.5 s @ 60 fps)
→ the runtime DISABLES your effect (goes pitch BLACK). "Worked for a bit then went black" ≈ over
budget — vectorise harder.

  RULE 0 (ABSOLUTE) — NO Python `for` loop in render() over pixels or particles. NONE.
      Every per-pixel / per-particle calc = ONE vectorised numpy expression over
      (N,) / (P,) / (P, N) / (N, 3). Forbidden in render():
          for i in range(ctx.n)        | for px in self.particles
          [f(i) for i in range(ctx.n)] | map(f, per_led)
          np.vectorize                 | np.apply_along_axis
      Loops in init() are fine. Tools: broadcasting, boolean masks, np.where, np.einsum,
      np.add.at, the provided helpers.

  PARTICLES — broadcast across BOTH axes; accumulate with einsum. Python per-particle loops
  are fine at P≤8, marginal at P=16, blow the budget at P≥32. Canonical (P, N) pattern:
      dx       = led_x[None, :] - parts_x[:, None]              # (P, N)
      falloff  = np.maximum(0.0, 1.0 - np.abs(dx) / radius)      # (P, N)
      colors   = hsv_to_rgb(parts_hue, 0.85, 1.0)                # (P, 3)
      np.einsum('pn,pc->nc', falloff, colors, out=self.out)      # (N, 3) accumulate
  Memory at P=100, N=1800: 720 KB float32 — fine.

  - NO allocation in render(). Preallocate `self.out` + scratch in init. Use the `out=`
    parameter on numpy ufuncs and helpers (`gauss` / `lerp` / `clip01` all accept it).
  - Stay in float32. ctx arrays are float32; `np.empty(shape, dtype=np.float32)` for scratch.
    Mixing fp64 silently halves Pi throughput.
  - No per-LED Python branches (`if some_led_x > 0`). Use `np.where`, mask multiply, `np.clip`.
"""

ANTI_PATTERNS = """\
ANTI-PATTERNS — concrete mistakes

  - `self.out[mask] = colour * factor` for overlapping shapes → assignment OVERWRITES (last
    wins, no additive brightness). For sparkles / particles / blobs, accumulate additively:
        self.out += contribution            # then clip01(self.out) at the end
  - 2D particle physics with y free in [-1, 1] → every LED has y ∈ {+1, -1}; particles drifting
    toward y=0 light NOTHING. Pin y to ±1 or run 1D on `u_loop` / `signed_x`.
  - Iterative physics (repulsion / springs / flocking) without DAMPING → energy accumulates,
    particles slam corners. Always include drag:
        self.vel *= max(0.0, 1.0 - p.damping * ctx.dt)
    and `np.clip` velocity.
  - Smoothing audio yourself (EMA, peak-follow, normalise) → already smoothed and auto-scaled
    upstream; another smoother kills responsiveness.
  - Thresholding `ctx.audio.low > 0.6` to detect kicks → use `ctx.audio.beat` (see AUDIO INPUT).
  - `print(...)` → not in builtins. Use `log.warning(...)`.
  - Returning float64 → cast to float32, or fill `self.out` (already float32) in place.
  - Dunder access (`__class__`, `__globals__`, ...) → reserved.

SHAPE GOTCHAS — "all input arrays must have the same shape"
  - `np.stack([per_led_array, scalar])` mixes ndim → use `np.full_like(per_led_array, 0.5)`.
  - `np.concatenate` on (N,) and (3,) → broadcast or reshape first.
  - `hsv_to_rgb(per_led_h, scalar_s, scalar_v)` already returns (N, 3) — no manual broadcasting.
  - `palette_lerp(stops, t_array)` returns `t.shape + (3,)`.
  - When stuck, `np.broadcast_arrays(a, b, c)` returns views at a common shape.
"""


def _load_example_effects() -> list[dict[str, Any]]:
    """Read every example under LLM_EXAMPLES_DIR (sorted): `effect.py` is the
    code; the `effect.yaml` sidecar supplies name / summary / params so each
    example renders as a COMPLETE write_effect payload.

    Soft-fail on missing dir, unreadable files, or a bad sidecar — the prompt
    still ships, degraded rather than broken.
    """
    out: list[dict[str, Any]] = []
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
        meta: dict[str, Any] = {}
        sidecar = sub / "effect.yaml"
        if sidecar.is_file():
            try:
                loaded = yaml.safe_load(sidecar.read_text())
                if isinstance(loaded, dict):
                    meta = loaded
            except (OSError, yaml.YAMLError):
                pass
        out.append(
            {
                "name": meta.get("name") or sub.name,
                "summary": meta.get("summary") or "",
                "params": meta.get("params") or [],
                "code": source,
            }
        )
    return out


def _example_effects_block() -> str:
    """Render the EXAMPLE EFFECTS section by loading from disk on every call.

    Loaded at prompt-build time (not import time) so dropping a new example
    dir into LLM_example_effects/ takes effect on the next chat turn.
    """
    examples = _load_example_effects()
    if not examples:
        return "EXAMPLE EFFECTS — (none on disk; drop effects under src/ledctl/surface/LLM_example_effects/)"
    lines = [
        "EXAMPLE EFFECTS — gold-standard COMPLETE `write_effect` payloads.",
        "Mirror this shape every time: name + summary + params + code. Note the range of",
        "archetypes (orbiting comet, stateful sparkles, plasma field, noise flicker, beat",
        "envelope) — start from the one closest to the request instead of forcing everything",
        "into a particle/comet mould.",
    ]
    for i, ex in enumerate(examples, start=1):
        lines.append("")
        lines.append(f"# EXAMPLE {i} — {ex['name']}")
        lines.append(f"summary: {ex['summary']}")
        lines.append("params:")
        if ex["params"]:
            for spec in ex["params"]:
                compact = {k: v for k, v in spec.items() if v is not None}
                lines.append(f"  {json.dumps(compact)}")
        else:
            lines.append("  []")
        lines.append("code:")
        lines.append(ex["code"].rstrip())
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
        "Mic → upstream FFT → auto-scaled to ~[0, 1] (long-window peak follower) → OSC.\n"
        "PRE-SMOOTHED AND PRE-SCALED — do NOT EMA / clamp / normalise yourself. Brief overshoots\n"
        "above 1.0 on transients; clip at OUTPUT, not input. Master `audio_reactivity` already\n"
        "pre-multiplies the bands below.\n\n"
        "  ctx.audio.low / .mid / .high  float ~[0, 1] — kick/sub | vocals/snare | hats/sibilance\n"
        "  ctx.audio.bands               dict {'low','mid','high'} — for select-param band choice\n"
        "  ctx.audio.beat                float in [0, 1]. 0 most frames; non-zero on a fresh onset.\n"
        "                                *** DEFAULT TRIGGER for ALL discrete / rhythmic events:\n"
        "                                kicks, flashes, sparkle spawns, particle bursts, on-beat\n"
        "                                colour swaps — anything 'on the music'. Upstream onset\n"
        "                                detector beats any band threshold (latency + precision).\n"
        "                                Use as multiplier (`ctx.audio.beat * p.kick_amount`) or\n"
        "                                rising-edge trigger (`if ctx.audio.beat > 0: spawn(...)`).\n"
        "                                Only use low/mid/high for discrete events if the operator\n"
        "                                explicitly asks for amplitude-following.\n"
        "  ctx.audio.beats_since_start   int monotonic counter (NOT scaled by reactivity)\n"
        "  ctx.audio.bpm                 float — tempo; 120.0 fallback when disconnected\n"
        "  ctx.audio.connected           bool — False = silent; bands will be 0.0\n\n"
        "Effects must still look good silent: `amp = base + reactive * ctx.audio.low`."
    )


def _runtime_api_block() -> str:
    return (
        "RUNTIME API — in scope (no imports needed)\n"
        "  np                        numpy module\n"
        "  Effect                    base class — subclass exactly once\n"
        "  hex_to_rgb(s)             '#ff8000' → (3,) float32 in [0, 1]\n"
        "  hsv_to_rgb(h, s, v)       broadcasting; returns float32\n"
        "  lerp(a, b, t, out=None)   a*(1-t) + b*t\n"
        "  clip01(x, out=None)       np.clip(x, 0, 1)\n"
        "  gauss(x, sigma, out=None) gaussian, peak=1\n"
        "  pulse(x, width=0.5)       cosine bump on [-width, +width], peak=1\n"
        "  tri(x)                    triangle wave on [0, 1], peak at 0.5\n"
        "  wrap_dist(a, b, period=1) shortest signed distance with wrap\n"
        "  palette_lerp(stops, t)    multi-stop palette sample. `stops` is ONE of:\n"
        "                              named_palette('fire')                       (baked LUT)\n"
        "                              [(0.0,'#ff0000'), (1.0,'#00ff00')]          (pos, hex)\n"
        "                              [(0.0,1,0,0), (1.0,0,1,0)]                  (pos, r, g, b)\n"
        "                              ['#ff0000','#00ff00','#0000ff']             (bare, even)\n"
        "                            Don't mix stop lengths in one list.\n"
        "  named_palette(name)       (LUT_SIZE, 3) float32 LUT — names: "
        + ", ".join(named_palette_names()) + "\n"
        "  rng                       np.random.Generator, seeded by effect name\n"
        "  log                       logger — log.info / log.warning / log.exception\n"
        "  PI, TAU, LUT_SIZE (=256), PALETTE_NAMES   constants\n"
    )


def _param_schema_block() -> str:
    return (
        "PARAM SCHEMA — 0–8 operator controls (renders as the \"Effect Knobs\" panel)\n"
        "Each: `{key (snake_case), label, control, ...control-specific..., help?}`\n"
        "  slider      {min, max, step?, default, unit?}     float\n"
        "  int_slider  {min, max, step?, default}            integer\n"
        "  color       {default: '#rrggbb'}\n"
        "  select      {options: [str, ...], default}        dropdown\n"
        "  toggle      {default: bool}\n"
        "  palette     {default: name}                       named palette dropdown\n\n"
        "Read via `ctx.params.<key>` (auto-updates between frames). Declare only what's worth\n"
        "hand-tuning. Operator tweaks for matching `key`s carry across regenerations — pick new\n"
        "keys deliberately when a knob's meaning changes.\n\n"
        "PER-EFFECT vs MASTER. Effect-local `brightness` / `speed` / `audio_intensity` shape THIS\n"
        "effect; masters are GLOBAL post-processing on the final output. They compose cleanly\n"
        "(your `speed` scales integrated time; master `speed` scales `ctx.dt`. Your `brightness`\n"
        "attenuates this effect; master `brightness` runs on the final output. `audio_reactivity`\n"
        "already pre-multiplies `ctx.audio.*`). If the request can only be honoured by a MASTER\n"
        "change (\"brighter\" with master brightness already 1.0, \"less reactive\" with reactivity\n"
        "high), TELL the operator which slider to move instead of re-emitting code.\n"
    )


def _current_effects_block(runtime: Runtime | None) -> str:
    """Render the SELECTED preview layer — the LLM has no visibility into LIVE,
    and we deliberately don't expose other preview layers either.

    Showing the rest of the stack would tempt the LLM to "preserve" what's
    around it instead of authoring its own layer cleanly. So we hand it
    exactly one thing: the layer it's replacing.
    """
    if runtime is None:
        return ""
    snap = runtime.snapshot()
    preview = snap.get("preview") or {}
    layers = preview.get("layers") or []
    out = ["CURRENT EFFECT (your `write_effect` REPLACES it)"]
    if not layers:
        out.append("  no effect yet — your `write_effect` will create one")
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
            # Drop None values — they make the schema noisier than the
            # operator-visible param panel itself.
            compact = {k: v for k, v in spec.items() if v is not None}
            out.append(f"    {json.dumps(compact)}")
    else:
        out.append("    (none declared)")
    out.append("\n  CURRENT PARAM VALUES:")
    out.append(f"    {json.dumps(sel_layer['param_values'])}")
    out.append("\n  CURRENT EFFECT SOURCE (build on it or rewrite it as the user requested):")
    for line in sel_layer["source"].splitlines():
        out.append(f"    {line}")
    return "\n".join(out)


def _last_error_block(last_error: dict[str, Any] | None) -> str:
    if not last_error:
        return ""
    return (
        "LAST EFFECT ERROR — your previous attempt failed. Read carefully and fix.\n"
        f"  error: {last_error.get('error')}\n"
        f"  details: {last_error.get('details')}\n"
        "Common causes: NameError / AttributeError → you used a name that isn't in the RUNTIME API;\n"
        "ImportError → remove the import (everything is in scope); shape/dtype error → check you\n"
        "return (N, 3) float32 in [0, 1]."
    )


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
        ROLE,
        TOOL_BLOCK,
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
    return "\n\n".join(sections)
