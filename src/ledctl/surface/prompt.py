"""System prompt assembly for the `write_effect` agent.

Regenerated fresh every turn so the LLM always sees the latest install summary,
audio snapshot, master values, and currently-active effect source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .frames import FRAME_DESCRIPTIONS
from .palettes import named_palette_names

if TYPE_CHECKING:
    from ..audio.state import AudioState
    from ..masters import MasterControls
    from ..topology import Topology
    from .runtime import Runtime


YOUR_JOB = """\
YOUR JOB
You are an LED effect author. The operator types short descriptions of what they want to see;
you write a single Python `Effect` subclass that we hot-load into a render loop on a Raspberry
Pi, plus a tiny set of operator-facing UI controls (sliders / colour pickers / dropdowns) so
the operator can hand-tune the look without re-prompting you. Emit ONE `write_effect` tool
call per turn — never a diff, always the COMPLETE effect.

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
  - `if ctx.audio.low > 0.6: ...` to detect kicks → use `ctx.audio.beat > 0` (rising-edge).
  - thresholding `audio_band` to detect onsets → upstream onset detector is far better.
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

EXAMPLE_BASIC = '''\
# EXAMPLE 1 — pulse_mono.  The simplest effect: solid colour, breathes on bass.
class PulseMono(Effect):
    """Solid colour fills the rig; brightness pulses on audio.low."""

    def init(self, ctx):
        self.amp = np.zeros(ctx.n, dtype=np.float32)   # scratch buffer

    def render(self, ctx):
        p = ctx.params
        col = hex_to_rgb(p.color)
        amp = p.floor + (1.0 - p.floor) * float(ctx.audio.low)
        self.out[:] = col[None, :]
        self.out *= amp
        return self.out
'''

EXAMPLE_ADVANCED = '''\
# EXAMPLE 2 — twin_comets_with_sparkles.  Two stateful particles + side masks +
# sparkle deposit + audio modulation — the user's "north star" prompt, written
# the way the runtime expects it.
class TwinCometsWithSparkles(Effect):
    """Two comets sweep along the rig. Top is red; bottom is blue. Each leaves
    sparkles in its colour that fade out behind it."""

    def init(self, ctx):
        self.x = ctx.frames.x                              # (N,) float32 in [0, 1]
        self.top = ctx.frames.side_top.astype(bool)
        self.bot = ctx.frames.side_bottom.astype(bool)
        self.sparkle_age = np.full(ctx.n, np.inf, dtype=np.float32)
        self.sparkle_rgb = np.zeros((ctx.n, 3), dtype=np.float32)
        self.head_top = 0.0
        self.head_bot = 0.0
        self.last_top = -1
        self.last_bot = -1
        self._fade = np.empty(ctx.n, dtype=np.float32)
        self._d = np.empty(ctx.n, dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        dt = ctx.dt
        self.head_top = (self.head_top + p.speed * dt) % 1.0
        self.head_bot = (self.head_top - p.lead_offset) % 1.0

        amp = 0.6 + 0.4 * float(ctx.audio.bands[p.audio_band])

        # sparkle decay (exp, half-life ~ p.sparkle_decay)
        np.add(self.sparkle_age, dt, out=self.sparkle_age)
        np.divide(self.sparkle_age, max(p.sparkle_decay, 1e-3), out=self._fade)
        np.exp(np.negative(self._fade, out=self._fade), out=self._fade)
        np.multiply(self.sparkle_rgb, self._fade[:, None], out=self.out)

        self._stamp(self.head_top, hex_to_rgb(p.leader_color), self.top, amp)
        self._stamp(self.head_bot, hex_to_rgb(p.follower_color), self.bot, amp)

        self._deposit(self.head_top, hex_to_rgb(p.leader_color), self.top, "top")
        self._deposit(self.head_bot, hex_to_rgb(p.follower_color), self.bot, "bot")

        return self.out

    def _stamp(self, head_x, color, mask, amp):
        np.subtract(self.x, head_x, out=self._d)
        np.abs(self._d, out=self._d)
        np.minimum(self._d, 1.0 - self._d, out=self._d)
        g = np.exp(-(self._d * self._d) * (1.0 / (2.0 * 0.03 * 0.03))) * amp
        self.out[mask] += g[mask, None] * color

    def _deposit(self, head_x, color, mask, side):
        idxs = np.where(mask)[0]
        if idxs.size == 0:
            return
        i = idxs[np.argmin(np.abs(self.x[idxs] - head_x))]
        prev = self.last_top if side == "top" else self.last_bot
        if i != prev:
            self.sparkle_age[i] = 0.0
            self.sparkle_rgb[i] = color
            if side == "top":
                self.last_top = int(i)
            else:
                self.last_bot = int(i)
'''


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
    if audio_state is None or not audio_state.enabled:
        return (
            "AUDIO INPUT\n"
            "Audio-server is disconnected. ctx.audio.low/mid/high will be 0.0 and\n"
            "ctx.audio.beat will be 0. Build effects that still look good silent\n"
            "(e.g. drive brightness from a slider with audio mixed on top via\n"
            "`amp = base + reactive * ctx.audio.low`)."
        )
    bands = (
        (audio_state.low_lo, audio_state.low_hi),
        (audio_state.mid_lo, audio_state.mid_hi),
        (audio_state.high_lo, audio_state.high_hi),
    )
    return (
        "AUDIO INPUT\n"
        "The audio-server (Realtime_PyAudio_FFT) captures from a mic, runs an FFT,\n"
        "auto-scales each band's energy to ~[0, 1] via a long-window peak follower,\n"
        "and publishes the result over OSC. EVERYTHING IS PRE-SMOOTHED AND SCALED —\n"
        "use raw values; do not apply your own EMA/clamp/normalise.\n\n"
        f"  ctx.audio.low      float in [0, 1]  ({bands[0][0]:.0f}–{bands[0][1]:.0f} Hz, kick/sub)\n"
        f"  ctx.audio.mid      float in [0, 1]  ({bands[1][0]:.0f}–{bands[1][1]:.0f} Hz, vocals/snare)\n"
        f"  ctx.audio.high     float in [0, 1]  ({bands[2][0]:.0f}–{bands[2][1]:.0f} Hz, hats/sibilance)\n"
        "  ctx.audio.bands    dict {'low','mid','high'} — convenient for select-param-driven band\n"
        "  ctx.audio.beat     int — number of fresh onsets since previous frame\n"
        "                          (almost always 0 or 1; use `> 0` as a rising-edge trigger).\n"
        "                          NEVER threshold ctx.audio.low to detect kicks.\n"
        "  ctx.audio.beats_since_start  int monotonic onset counter\n"
        "  ctx.audio.bpm      float — current tempo. Falls back to 120.0 when disconnected.\n"
        "  ctx.audio.connected  bool — False = audio off; low/mid/high will be 0.0.\n\n"
        f"LIVE READING (snapshot at request time):\n"
        f"  device:    {audio_state.device_name or 'default'}\n"
        f"  low:  {audio_state.low:.3f}    mid:  {audio_state.mid:.3f}    high: {audio_state.high:.3f}\n"
        f"  bpm:       {audio_state.bpm if audio_state.bpm is not None else 120.0:.1f}"
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
        "  palette_lerp(stops, t)    multi-stop palette sample at t (scalar or array)\n"
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
        "Every param is a dict with `key` (snake_case), `label`, `control`, plus control-specific fields:\n"
        "  - slider:      {min, max, step?, default, unit?}    — float\n"
        "  - int_slider:  {min, max, step?, default}           — integer\n"
        "  - color:       {default: '#rrggbb'}                  — colour picker\n"
        "  - select:      {options: [str, ...], default}        — dropdown\n"
        "  - toggle:      {default: bool}                       — switch\n"
        "  - palette:     {default: name}                       — named palette dropdown\n\n"
        "Read values via `ctx.params.<key>`. The operator drags sliders → ctx.params updates between\n"
        "frames; you do nothing special. Only declare what's worth tuning by hand. The system carries\n"
        "the operator's slider tweaks across regenerations when you reuse the same `key`.\n"
    )


def _masters_block(masters: MasterControls | None) -> str:
    if masters is None:
        return ""
    return (
        "OPERATOR MASTERS (read-only; available via ctx.masters but NOT mutable)\n"
        f"  brightness:        {masters.brightness:.2f}\n"
        f"  speed:             {masters.speed:.2f}   (multiplies ctx.t advancement)\n"
        f"  audio_reactivity:  {masters.audio_reactivity:.2f}   (already pre-applied to ctx.audio.*)\n"
        f"  saturation:        {masters.saturation:.2f}\n"
        "If a request can only be honoured by a master change ('make it brighter' while brightness=1.0;\n"
        "'less reactive' while audio_reactivity is high) — TELL THE OPERATOR which slider to move\n"
        "instead of writing a redundant effect. Otherwise design assuming the masters stay where they are."
    )


def _current_effects_block(runtime: Runtime | None) -> str:
    """Render ONLY the PREVIEW composition — the LLM has no visibility into LIVE.

    LIVE is operator-controlled exclusively (no read, no write). Showing live
    source / param values would tempt the LLM to "preserve" what's playing
    and confuse it about which layer it's authoring.
    """
    if runtime is None:
        return ""
    snap = runtime.snapshot()
    out = ["CURRENT PREVIEW COMPOSITION (your scratchpad — what you're authoring)"]
    out.append(f"  crossfade on promote: {snap['crossfade_seconds']:.2f}s")
    preview = snap.get("preview") or {}
    layers = preview.get("layers") or []
    sel = preview.get("selected", 0)
    if not layers:
        out.append("  preview composition is empty — your `write_effect` will create the first layer")
        return "\n".join(out)
    out.append(f"  layers: {len(layers)}, selected: #{sel}")
    for i, layer in enumerate(layers):
        marker = "▶" if i == sel else " "
        out.append(
            f"  {marker} #{i}  {layer['name']}  "
            f"[{layer['blend']} @ opacity={layer['opacity']:.2f}"
            f"{' DISABLED' if not layer.get('enabled', True) else ''}]"
        )
        out.append(f"      summary: {layer['summary']}")
        out.append(f"      param_values: {json.dumps(layer['param_values'])}")
    sel_layer = layers[min(sel, len(layers) - 1)]
    out.append(
        "\n  SELECTED LAYER SOURCE (your `write_effect` will REPLACE this — "
        "build on it or rewrite it as the user requested):"
    )
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
    "params":  [ { "key": ..., "control": ..., ... }, ... ]   // <= 8 entries
  }

What changes vs. a chat reply:
  - The effect goes into the PREVIEW slot (the operator sees it instantly in the simulator).
  - DDP / live LEDs are unaffected until the operator clicks "Promote to live".
  - Drag-tuned slider values for matching `key`s are carried forward into your next attempt
    via `param_values` in CURRENT EFFECTS — design defaults around those when sensible.

Be terse in your assistant text. The operator can SEE the lights; they don't need a recital.
"""


def build_system_prompt(
    *,
    topology: Topology,
    runtime: Runtime | None,
    audio_state: AudioState | None,
    masters: MasterControls | None,
    crossfade_seconds: float | None = None,
    presets_dir: Path | None = None,  # legacy; ignored
    last_error: dict[str, Any] | None = None,
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
        "EXAMPLE EFFECTS — these are what good output looks like\n\n" + EXAMPLE_BASIC + "\n\n" + EXAMPLE_ADVANCED,
    ]
    masters_block = _masters_block(masters)
    if masters_block:
        sections.append(masters_block)
    cur = _current_effects_block(runtime)
    if cur:
        sections.append(cur)
    err = _last_error_block(last_error)
    if err:
        sections.append(err)
    sections.append(TOOL_BLOCK)
    return "\n\n".join(sections)
