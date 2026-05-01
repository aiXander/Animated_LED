# Refactor — A Single-File LED Control Surface

> Companion to the existing `implementation_roadmap.md`. This document specifies a
> structural refactor of the effect / palette / modulator layer that must be done
> **before** Phase 7 (mobile UI). The goal is to collapse the entire LED control
> surface — every primitive, every parameter, every named recipe — into a single
> Python module so that adding a new visual idea is a one-place change and the
> LLM's in-context API documentation is a derived artefact, not a hand-curated
> twin.

---

## 1. Why we're doing this

The current code already factored effects into `field × palette × bindings`,
which was a good first cut. But the seams have started to leak:

1. **Each "named effect" is its own opinionated bundle.** `scroll` packages an
   axis-travelling 1-D wave with a softness knob, a width knob (only for
   `gauss`), a cross-phase, and a shape selector. `radial` re-uses the same
   shape primitive but bolts on a different distance function. `noise` is its
   own world. They share concepts (a shape, a speed, a palette, bindings) but
   express them with subtly different schemas. A new "crossfade between two
   wave directions" idea has nowhere to live without a new effect class.

2. **The vocabulary is spread across ~6 files**:
   `effects/fields/{scroll,radial,sparkle,noise}.py` define the field
   generators, `effects/fields/_shape.py` defines the shape primitives,
   `effects/palette.py` defines named palettes + the LUT pipeline,
   `effects/modulator.py` defines audio/LFO sources + envelopes,
   `effects/registry.py` exposes the catalogue, `effects/fields/__init__.py`
   re-exports for import side-effects, `effects/__init__.py` re-exports again
   for callers. Adding a new effect requires touching 3–4 of those files plus
   the agent system prompt.

3. **The LLM's documentation is a parallel hand-written copy of the catalogue.**
   `agent/system_prompt.py` repeats the effect names (`scroll`, `radial`,
   `sparkle`, `noise`), the palette names (`rainbow`, `fire`, `ocean`, …),
   the source identifiers (`audio.rms`, `lfo.sin`, …), the binding-slot
   semantics (`brightness`, `speed`, `hue_shift`), examples, recipes, and
   anti-patterns. Every renamed param or new effect requires editing both the
   code and the prompt — and the prompt drifts.

4. **There are no real primitives the LLM can compose.** "Pulsate red" today
   has to be expressed as `scroll` (with a mono palette) + a `bindings.brightness`
   modulator, even though scroll's spatial pattern contributes nothing — it's
   a workaround that forced an anti-pattern entry. There's no way to say
   "a sine wave on x times an audio envelope on y" without writing a new
   effect class.

5. **Strict-extra schemas amplify the cost of misalignment.** Pydantic's
   `extra="forbid"` is the right call for the LLM round-trip, but it means
   every field naming choice is a hard contract: a renamed parameter is a
   breaking change for the prompt + presets + tests at once.

The refactor target: **one Python file that owns the entire control surface.**
Effects, primitives, palettes, modulators, blend rules, examples, recipes,
anti-patterns. The engine just calls a compiled callable; the agent just
imports `generate_docs()`. No second source of truth.

---

## 2. Design principles

1. **Composition over enumeration.** The catalogue is a small set of orthogonal
   primitives (a wave, a radial distance, a value-noise field, a sparkle
   stamper, an LFO, an audio band, an envelope, a palette LUT, …) and a few
   combinators (`mix`, `multiply`, `add`, `remap`, `clamp`). Today's "named
   effects" become recipes built out of those primitives — usable as defaults,
   never as the only path.

2. **One file, one truth.** `src/ledctl/surface.py` (working name) holds:
   - every primitive's Python implementation,
   - every primitive's pydantic schema,
   - every named palette,
   - every named recipe (what the old "effects" become — sugar, not types),
   - the blend modes,
   - the `compile(spec, topology) → render_fn` entry point,
   - the `generate_docs(audio_state, current_state, topology) → str` entry
     point that the agent's system prompt ingests verbatim,
   - the canonical examples + recipes + anti-patterns blocks.

3. **Specs are an AST, not a flat dict.** The `update_leds` tool argument
   becomes a tree of `{kind, params}` nodes. Any numeric param can be either a
   literal *or* another node (a modulator, an LFO, an audio band). That single
   recursion erases the "binding slots are special" rule: anything modulatable
   can be modulated by anything that produces a scalar, including future
   primitives we haven't thought of.

4. **Per-LED math stays vectorised numpy.** Compilation walks the AST once at
   layer-creation time, instantiates per-primitive state (lattices, RNGs,
   envelopes), and returns a closure that does pure numpy ops in the hot path.
   No interpretation cost per frame.

5. **Topology, audio state, and time are the only inputs.** Every primitive
   reads from the same trio. Topology is a constant for the lifetime of a
   layer (re-compiled on hot-swap), audio state is a shared mutable struct,
   `t` is the engine's monotonic clock.

6. **Documentation is generated, not authored.** The system prompt's
   EFFECTS / NESTED-TYPES / NAMED-PALETTES / BINDINGS / ANTI-PATTERNS /
   EXAMPLES sections all come from `generate_docs()`. The
   agent module wraps it with the install summary, current state, and audio
   snapshot only.

7. **Backward-compatible surface, fresh internals.** The REST API, the YAML
   preset format, the `update_leds` tool's outer shape (`{layers, blend,
   opacity, crossfade_seconds, blackout}`) all stay. What changes is what
   lives inside each layer's body.

---

## 3. The single file — `src/ledctl/surface.py`

### 3.1 Anatomy

```
surface.py
├── color utilities (hex_to_rgb01, gamma helpers — currently in effects/_color.py)
├── shape utilities (cosine / sawtooth / pulse / gauss → all just 1-D pure fns)
├── primitive registry  (REGISTRY: dict[str, Primitive])
├── primitives — each is one decorated function, ~10–40 lines:
│     │
│     ├── scalar fields (pure spatial, output: per-LED scalar in [0, 1])
│     │     wave              — 1-D travelling pattern (replaces scroll)
│     │     radial            — distance-from-point pattern
│     │     gradient          — static linear ramp on an axis
│     │     position          — raw normalised position component
│     │     constant          — the scalar 1.0 (or any literal)
│     │     noise2d           — value-noise lattice scrolled in time
│     │
│     ├── stateful fields (carry per-LED state; reset on re-compile)
│     │     sparkles          — Poisson stamp + exponential decay
│     │     trail             — fading echo of an input field (new — useful)
│     │
│     ├── modulators (pure-time scalars; no spatial dependence)
│     │     lfo               — sin / saw / triangle / pulse
│     │     audio_band        — rms / low / mid / high / peak
│     │     envelope          — asymmetric attack/release + gain + curve +
│     │                         floor/ceiling map. Self-contained mirror of
│     │                         the legacy ModulatorSpec so preset migration
│     │                         is one-to-one. clamp / range_map below are
│     │                         advanced-only — most trees never need them.
│     │     clamp / range_map — bounding + linear mapping (rare; envelope
│     │                         already covers the common floor/ceiling case)
│     │
│     ├── palettes (256-entry LUTs consumed by palette_lookup)
│     │     palette_named     — rainbow, fire, ice, sunset, ocean, warm, white, black, mono_<hex>
│     │     palette_stops     — gradient from explicit {pos, color} stops
│     │
│     ├── combinators (polymorphic; output kind resolved from inputs at compile)
│     │     mix(a, b, t)      — lerp; works on matching scalar_t / scalar_field / palette pairs
│     │     mul / add / screen / max / min
│     │     remap(input, fn)  — apply sin / abs / sqrt / pow / step
│     │     threshold(input, t)
│     │
│     └── colorisers (palette × scalar field → RGB)
│           palette_lookup(scalar, palette, brightness?, hue_shift?) — palette is a node, not a literal
│           solid(rgb)        — uniform colour (degenerate case of palette_lookup)
│
├── blend modes (normal / add / screen / multiply) — same as today
│
├── spec types (pydantic):
│     NodeSpec            — { kind: str, params: dict }   (recursive)
│     LayerSpec           — { node: NodeSpec, blend, opacity }
│     UpdateLedsSpec      — { layers: list[LayerSpec], crossfade_seconds, blackout }
│
├── compile(spec, topology) → render_fn
│        walks the tree, instantiates per-primitive state, returns a closure
│
├── named example trees (Python dict, surfaced through generate_docs):
│     EXAMPLE_TREES = {
│       "warm_drift":    NodeSpec(...),
│       "fire_chase":    NodeSpec(...),
│     }
│   Teaching artefacts only — the LLM sees them in the prompt's
│   EXAMPLES section and inlines / modifies them as needed. There is
│   no `recipe` primitive: the agent emits the complete tree every
│   turn, so a callable indirection earns nothing, and a shared name
│   without a path-based override system is more confusing than
│   useful. Operator-saved layer stacks stay where they are
│   (`config/presets/<name>.yaml`, loaded by `presets.py`).
│
├── examples / anti-patterns (lists of strings + structured tool calls)
│
└── generate_docs(*, topology, audio_state, engine_state) → str
        emits the prompt-ready CONTROL SURFACE block
```

That's the entire design surface. Adding "fluid noise reacting to bass" is one
new `@primitive` on a numpy function plus a docstring.

### 3.2 Primitive contract

A primitive is a Python function with a pydantic Params model and a kind tag.
It compiles into a small object that has:

```python
class CompiledNode:
    output_kind: Literal["scalar_field", "scalar_t", "rgb_field", "palette"]
    state: Any                 # whatever buffers, RNG, position arrays it caches
    def __call__(self, ctx, scratch_buffers) -> np.ndarray: ...
```

The four output kinds:

- `scalar_field` — per-LED scalar in [0, 1]. Spatially varying (e.g. `wave`,
  `radial`, `noise2d`, `sparkles`, `position`).
- `scalar_t`     — single scalar for the whole frame, no spatial dependence
  (e.g. `lfo`, `audio_band`, `envelope`, `constant`).
- `palette`      — 256-entry LUT. Built once at compile (`palette_named`,
  `palette_stops`) or rebuilt per-frame (`mix(palette_a, palette_b, scalar_t)`).
  Only `palette_lookup.palette` consumes this kind.
- `rgb_field`    — per-LED RGB in [0, 1]³. Always the layer's leaf node
  (e.g. `palette_lookup`, `solid`).

(There is no `rgb_t`. For uniform colour pulsing with audio, compose
`mul(solid(rgb=...), envelope(audio_band(...)))` — the `mul` combinator's
`rgb_field × scalar_t → rgb_field` rule (below) handles the broadcast.
`solid.rgb` itself stays a literal — vector-valued modulated params are
out of scope for v1.)

**Polymorphic combinators.** `mix`, `mul`, `add`, `screen`, `max`, `min`,
`remap`, and `threshold` resolve their `output_kind` from their inputs at
compile time. `mul(scalar_t, scalar_t) → scalar_t`;
`mul(scalar_t, scalar_field) → scalar_field`;
`mul(rgb_field, scalar_t) → rgb_field` (broadcast over RGB);
`mul(rgb_field, scalar_field) → rgb_field` (per-LED gain);
`mix(palette_a, palette_b, scalar_t) → palette`. Mismatched inputs (e.g.
`add(scalar_t, palette)`, or `mul(rgb_field, palette)`) raise at compile
with a clear error. The `t` argument of `mix` is always `scalar_t`
regardless of the kind being mixed.

**Compatibility rules (enforced at compile time):**

| param expects   | accepts                                            |
| --------------- | -------------------------------------------------- |
| `scalar_field`  | `scalar_field` directly, or `scalar_t` (broadcast) |
| `scalar_t`      | `scalar_t` only                                    |
| `palette`       | `palette` only                                     |
| `rgb_field`     | `rgb_field` only                                   |

For combinators that accept *either* an `rgb_field` or a `scalar_*` arg
(`mul`, `add`, `screen`, `max`, `min`), the rule is: if any input is
`rgb_field` the output is `rgb_field` and the other input must be a
scalar (broadcast over the RGB axis). Two `rgb_field` inputs are also
allowed and produce `rgb_field`. Mixing `palette` with `rgb_field` is
not — convert via `palette_lookup` first.

The asymmetry matters: a per-LED scalar can't drive a single time-varying
knob like `wave.speed` because the wave's analytical formula needs one number
per frame, not 1800. But a time-varying scalar can drive any per-LED slot
(every LED gets the same value that frame). This is what makes the headline
composition `palette_lookup(..., hue_shift=position("y"))` work — `hue_shift`
advertises `scalar_field` so per-LED variation is welcome there.

**Literal vs `NumberOrNode` convention.** Discrete params (enums like `axis`,
`shape`, `band`, `direction`) are fixed at spec time and cannot be modulated
— the compiler bakes them in. Numeric params (`speed`, `wavelength`,
`softness`, etc.) are typed `NumberOrNode = float | int | NodeSpec` and
accept either a literal or a node yielding a `scalar_t`. A handful of
params on `palette_lookup` (`brightness`, `hue_shift`) widen this further to
also accept `scalar_field` for the per-LED tricks above. Every primitive's
docstring spells out the kind each param accepts; `generate_docs()` mirrors
this in the catalogue.

A new primitive looks like this — note the docstring is what the LLM will see:

```python
@primitive(kind="scalar_field")
class Wave(Primitive):
    """1-D travelling pattern along an axis. Replaces scroll/wave/gradient/chase.

    Output: per-LED scalar in [0, 1] that the palette will look up.
    """
    class Params(BaseModel):
        axis: Literal["x", "y", "z"] = Field("x", description="...")
        wavelength: float = Field(1.0, gt=0.0, description="...")
        speed: NumberOrNode = Field(0.3, description="cycles/sec; sign sets direction")
        shape: Literal["cosine", "sawtooth", "pulse", "gauss"] = "cosine"
        softness: NumberOrNode = 1.0
        width: NumberOrNode = 0.15
        cross_phase: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def compile(self, params, topology, ctx) -> CompiledNode: ...
```

`NumberOrNode` is the punchline: any number is literal-or-modulated, and
modulation is a property of the parameter, not of the parent primitive.
There's no second binding-slot system.

### 3.3 Modulation is explicit

If you want smoothing on an audio source, you write `envelope(audio_band(...))`
explicitly. The compiler does **not** auto-wrap raw audio nodes with a
hidden envelope, even if the host param has a "default attack/release" hint.
That keeps the rule simple: every primitive is exactly what its tree says it
is. The system prompt teaches the pattern with two anchor recipes
(`pulse_on_kick`, `slow_room_breathe`) so the LLM picks it up immediately.

The only smoothing the engine applies for you is in `envelope` itself, which
takes its `attack_ms` / `release_ms` from its own params (sane defaults:
60 ms attack, 250 ms release). If you write `audio_band("low")` without an
envelope, you get the raw rolling-normalised value — instantaneous, jagged.
That's a feature: peak-kick effects want exactly that.

`audio_band` always reads the rolling-normalised `*_norm` fields on
`AudioState` (never the raw scalars), matching today's binding behaviour.
This also means the operator's `audio_reactivity` master (§7.2) applies
uniformly to every audio reference in the tree.

**Time semantics under `freeze` / `speed`.** Every primitive that reads
time reads `ctx.t` (effective time) — *except* `envelope`, which uses
wall-clock dt for its attack/release smoothing. This is what makes the
§7.2 promise "a frozen pattern still breathes with the room" actually
hold: `freeze=true` halts wave / lfo / noise / sparkle decay, but an
`envelope(audio_band(...))` chain keeps tracking the room. Document this
in the `envelope` primitive's docstring so the LLM sees it too.

---

## 4. The expression schema

### 4.1 Tree shape

Every spec node is `{kind: <primitive name>, params: {...}}`. Params can be
literals or further nodes. A leaf field returns RGB; the engine just needs RGB
at the layer level.

```jsonc
// Old: scroll + bindings.brightness driven by audio.rms
{
  "effect": "scroll",
  "params": {
    "axis": "x", "speed": 0.3, "shape": "cosine",
    "palette": "fire",
    "bindings": {
      "brightness": {"source": "audio.rms", "floor": 0.5, "ceiling": 1.0}
    }
  }
}

// New: same effect as a tree
{
  "kind": "palette_lookup",
  "params": {
    "scalar": {
      "kind": "wave",
      "params": {"axis": "x", "speed": 0.3, "shape": "cosine"}
    },
    "palette": "fire",
    "brightness": {
      "kind": "envelope",
      "params": {
        "input": {"kind": "audio_band", "params": {"band": "rms"}},
        "floor": 0.5, "ceiling": 1.0
      }
    }
  }
}

// Use case the verbose form unlocks: yellow→red wave with the red end
// crossfading to purple on every kick. Two palette nodes, one envelope,
// one polymorphic mix.
{
  "kind": "palette_lookup",
  "params": {
    "scalar": {"kind": "wave", "params": {"axis": "x", "speed": 0.3}},
    "palette": {
      "kind": "mix",
      "params": {
        "a": {"kind": "palette_stops", "params": {
          "stops": [{"pos": 0, "color": "#ffd400"}, {"pos": 1, "color": "#ff2200"}]}},
        "b": {"kind": "palette_stops", "params": {
          "stops": [{"pos": 0, "color": "#ffd400"}, {"pos": 1, "color": "#8a00ff"}]}},
        "t": {"kind": "envelope", "params": {
          "input": {"kind": "audio_band", "params": {"band": "low"}},
          "attack_ms": 20, "release_ms": 400}}
      }
    }
  }
}
```

Slightly more verbose — but the verbosity is *uniform*: the LLM never has to
remember which slot lives inside `bindings` and which lives inside `params`,
or that `lfo.sin` is a magic string while `mono_ff0000` is a different magic
string. Everything is `{kind, params}` recursively, and the same `mix`
combinator handles scalar lerps, per-LED lerps, and palette lerps.

### 4.2 Sugar for common cases

Two pieces of sugar to keep specs readable:

1. **Bare literals where a node is expected.** `"speed": 0.3` is shorthand for
   `"speed": {"kind": "constant", "params": {"value": 0.3}}`. Parsed by a
   pydantic `BeforeValidator`.

2. **Bare strings for palettes.** `"palette": "fire"` is shorthand for
   `{"kind": "palette_named", "params": {"name": "fire"}}`. Same rule for
   `mono_ff0000` (the old shorthand survives, just as a registered primitive
   instead of a one-off in `palette.py`).

Named example trees in `EXAMPLE_TREES` (§3.1) are teaching material the LLM
sees in the prompt — they are inlined and edited by the agent, never
referenced as a primitive. If a real need for "named tree with overrides"
surfaces later, add it then with explicit dotted-path overrides — not before.

### 4.3 Layer-level shape stays familiar

```jsonc
{
  "layers": [
    {
      "node": {"kind": "palette_lookup", "params": {...}},
      "blend": "normal",
      "opacity": 1.0
    }
  ],
  "crossfade_seconds": 1.0,
  "blackout": false
}
```

`blend` and `opacity` stay at the layer level (the mixer owns them — they're
not effect-internal). `update_leds` accepts this exact shape; the
crossfade path's outer behaviour is unchanged from the operator's
viewpoint, but its internals shift: the mixer's signature changes from
`render(t, out)` to `render(ctx, out)`, alpha is computed against
`ctx.wall_t`, and ctx is forwarded to layer render closures so they read
the master-scaled `ctx.t`. See §7.2 for why.

---

## 5. The compiler

`compile(spec, topology) -> Callable[[float, np.ndarray], None]`:

1. Walk the tree recursively. For each node:
   - look up the primitive by `kind`,
   - validate `params` through its pydantic Params,
   - recurse into any nested NodeSpec params,
   - call `primitive.compile(params, topology, ctx)` to get a `CompiledNode`,
2. Type-check `output_kind` against the parent's expectation (combinators
   advertise the kind they want; mismatches raise a clear error early).
3. Return the root `CompiledNode`'s `__call__` wrapped in a layer-level
   adapter that writes the final RGB into the engine's pre-allocated buffer.

State is allocated **once at compile time** — per-primitive lattices, RNGs,
position caches, envelope state. The hot path is only pointwise numpy.

Compilation errors are structured (the same shape today's `update_leds`
returns when validation fails) so the LLM can self-correct on the next turn.

---

## 6. Mapping today's vocabulary to the new primitives

| today's effect / concept    | new primitives                                                                |
| --------------------------- | ----------------------------------------------------------------------------- |
| `scroll` (any shape)        | `palette_lookup(scalar=wave(...), palette=...)`                               |
| `radial`                    | `palette_lookup(scalar=radial(...), palette=...)`                             |
| `noise`                     | `palette_lookup(scalar=noise2d(...), palette=...)`                            |
| `sparkle`                   | `palette_lookup(scalar=sparkles(...), palette=...)`                           |
| audio-pulse layer (legacy)  | `mul(solid(rgb=...), envelope(audio_band("low")))` — using the `mul` combinator |
| `bindings.brightness`       | `palette_lookup(..., brightness=<modulator node>)`                            |
| `bindings.speed`            | `wave(speed=<modulator node>, ...)` — modulation lives on the field          |
| `bindings.hue_shift`        | `palette_lookup(..., hue_shift=<modulator node>)`                             |
| `audio.rms` / `audio.low` … | `audio_band(band="rms" \| "low" \| "mid" \| "high" \| "peak")`                |
| `lfo.sin` / `lfo.saw` …     | `lfo(shape="sin" \| "saw" \| "triangle" \| "pulse", period_s, phase, duty)`  |
| `palette: "fire"`           | `palette_named(name="fire")` (or string sugar)                                |
| `palette: {stops: [...]}`   | `palette_stops(stops=[...])`                                                  |

No expressive power is lost. `mix(wave_x, wave_y, lfo_sin)` is now a sentence
the LLM can write — that wasn't possible before.

---

## 7. Master controls — UI-direct, agent-blind

Some operator-room knobs don't belong in the spec at all. They're not
creative parameters; they're *room* parameters — the kind a sound engineer
reaches for when the lights need to come down for a speech, when the bass
hit feels weak in a hot room, or when the wave pattern looks too frantic on
this particular track. They must:

- **survive every spec change** — the LLM crossfading the layer stack must
  not wipe them,
- **be invisible to the agent** — `update_leds` should never produce or
  alter them, so the agent can't drift the room as a side effect of a
  creative request,
- **be a single set of sliders in the UI**, always present, always live,
- **stack on top** of whatever the spec is doing in a well-defined way.

This is a separate control surface from the surface DSL. The DSL describes
*what* to render; the masters describe *how loud, how fast, how reactive*
the install is right now.

### 7.1 The master set (v1)

A small, orthogonal set is much better than a deep panel. The user's three
asks plus two I'd add:

```python
@dataclass
class MasterControls:
    brightness: float = 1.0         # 0–1; final-output gain (rgb *= brightness)
    speed: float = 1.0              # 0–3; scales effective time for every primitive
    audio_reactivity: float = 1.0   # 0–3; multiplies every audio_band output
    saturation: float = 1.0         # 0–1; pulls the final RGB toward greyscale
    freeze: bool = False            # short-circuits speed→0 without mutating it
```

Each domain maps to a single application point in the pipeline. No
primitive opts in or out — the application is mechanical and the primitives
stay pure.

### 7.2 Three application points

```
   [layer specs] ── compile ──► [compiled tree]
                                       │
   ┌───────────────── time stage ──────┼──────────────────┐
   │  effective_t += dt × speed        │                  │
   │  (engine accumulates, primitives  │                  │
   │   read ctx.t which IS effective_t)│                  │
   └────────────────────────────┬──────┘                  │
                                │                         │
   ┌──────────── audio stage ───┴───────┐                 │
   │  RenderContext exposes              │                 │
   │  ctx.audio.<band>_norm pre-scaled  │                 │
   │  by audio_reactivity once per tick │                 │
   └────────────────────────────────────┘                 │
                                │                         │
                                ▼                         │
                         [mixer.render]                   │
                                │                         │
   ┌─────────── output stage ───┴───────────────┐         │
   │  rgb = grey + (rgb - grey) * saturation    │         │
   │  rgb *= brightness                         │         │
   │  rgb → gamma → uint8 → transport            │         │
   └────────────────────────────────────────────┘         │
                                                          │
   freeze toggle: speed → 0 effectively, but a separate ──┘
   field so the user's last `speed` value is preserved
   when they un-freeze.
```

**Time stage.** The engine maintains a monotonically-advancing
`effective_t` alongside wall time. Each tick:
`effective_t += (wall_now - wall_last) * (0.0 if freeze else speed)`.
Every primitive that reads time reads `ctx.t` — which *is* effective_t —
*except* `envelope`, which reads wall-clock dt (see §3.3).
Setting `speed=0` (or `freeze=true`) freezes scrolls, LFOs, noise advection,
*and* sparkle decay (because sparkle's exponential uses dt from
effective_t too). Setting `speed=2` doubles every motion.

**Crossfades run on wall time, not effective time.** The mixer's
crossfade alpha is `(wall_now - cf_start) / cf_duration`, computed
against `ctx.wall_t`, not `ctx.t`. Crossfades are operator direction
("switch to peak preset over 1 s") — they should not be slowed by
`speed=0.5` or frozen by `freeze=true`. Concretely, `Mixer.render` takes
the full `RenderContext` (or wall_t + ctx) so it can use wall_t for
alpha while passing ctx through to the per-layer render closures, which
read ctx.t. This is a small change from today's `Mixer.render(t, out)`
(see `mixer.py`); call it out in the engine refactor.

Audio reactivity is independent of `speed` and `freeze` — `audio_band`
reads the current `AudioState` directly, not via `effective_t`, so a frozen
pattern still breathes with the room. This is deliberate: the operator
freezing motion shouldn't also kill the install's audio response.

**Audio stage.** The engine builds `ctx.audio` once per tick by copying the
shared `AudioState` and pre-multiplying the `*_norm` fields by
`max(0.0, masters.audio_reactivity)`. Primitives — `audio_band` and any
future audio reader — just read `ctx.audio.rms_norm` etc. and stay pure.
This means the master applies uniformly regardless of how many
`audio_band` references exist in the tree, and a future second audio
primitive doesn't have to re-implement the multiply. Raw (non-`_norm`)
fields are left untouched in case a primitive ever wants the unscaled
signal for diagnostics. LFO and `constant` modulators are unaffected.

With `audio_reactivity > 1` the `*_norm` values can exceed 1.0; we
deliberately do **not** clamp at the master site. The slider is for hot
rooms — pushing harder is the point — and any overshoot clips naturally at
the output stage (or earlier, at an explicit `envelope.ceiling` /
`palette_lookup.brightness`). Document this at the top of `audio_band`'s
docstring so the LLM doesn't assume a strict `[0, 1]` range.

**Output stage.** After the mixer writes its final RGB, two pointwise
ops run before gamma:
1. **Saturation pull**: `rgb = grey + (rgb - grey) * saturation`, where
   `grey = luminance(rgb)` (Rec. 709: 0.2126·R + 0.7152·G + 0.0722·B,
   broadcast to (N, 3)). `saturation = 0` collapses to greyscale,
   `saturation = 1` is a no-op.
2. **Brightness gain**: `rgb = clip(rgb * brightness, 0, 1)`. Plain
   multiplicative master — `brightness = 0.5` means "half as bright" with
   no surprises. (Earlier drafts proposed a P95-luminance ceiling; rejected
   for v1 because it makes a dim scene at `brightness=0.5` *unchanged*,
   which doesn't match the slider's mental model. If room-tone-aware
   brightness ever becomes a real ask, add it as a separate master, e.g.
   `headroom`.)

Order is moot mathematically for plain gain (saturation and scalar gain
commute), but we keep "saturation, then brightness" as a stable convention
so future non-commuting masters slot in cleanly. Gamma stays last
(post-master, pre-uint8) so the install's calibrated gamma curve sees the
master-scaled signal.

### 7.3 The render context

The compiler's `CompiledNode.__call__` signature changes from
`(t, audio_state, scratch)` to `(ctx, scratch)` where:

```python
@dataclass
class RenderContext:
    t: float                        # effective time (master-speed-scaled)
    wall_t: float                   # raw monotonic — load-bearing for the
                                    # mixer crossfade alpha (must not be
                                    # affected by speed/freeze) and for
                                    # envelope smoothing dt; otherwise
                                    # diagnostic-only inside primitives
    audio: AudioState | None
    masters: MasterControls
```

This is a one-time signature change paid during the surface refactor; once
in, every future master is a one-line addition to the dataclass and a
single read at its application point.

### 7.4 REST + WS API

```
GET   /masters              → current MasterControls
PATCH /masters              → partial update; pydantic-bounded
                               body: { brightness?, speed?, audio_reactivity?,
                                       saturation?, freeze?, persist?: bool }
                               persist=true writes the current values back
                               into config.yaml (atomic with .bak), mirroring
                               the /audio/select shape so the UI has one
                               persistence pattern across the operator surface.
```

Masters land in the `/state` payload and ride along the existing
`/ws/state` broadcast (already added in `api/server.py`; `/ws/frames`
stays pixels-only). The explicit-controls page subscribes to `/ws/state`
so slider changes from a second client reflect at ~60 Hz without polling. Bounds enforced
server-side: brightness ∈ [0, 1], speed ∈ [0, 3], audio_reactivity ∈ [0, 3],
saturation ∈ [0, 1].

### 7.5 The agent sees masters, but cannot change them

The agent's job is *creative direction*. Masters are *operator direction*.
The split: **the agent reads masters as read-only context, and does not
emit them via `update_leds`.** The system prompt regenerates each turn with
the current master values — same regen path as the audio snapshot — under
a `OPERATOR MASTERS (read-only)` block:

```
OPERATOR MASTERS (read-only — set by sliders, persist across your changes):
  brightness:        0.40   (final-output gain; user pulled this down)
  speed:             1.00   (time multiplier on motion)
  audio_reactivity:  1.00   (multiplier on every audio_band output)
  saturation:        1.00   (1 = full colour, 0 = greyscale)
  freeze:            false  (true = time stops, last frame held)

You cannot modify these — they are sliders the operator owns. If a
request can only be honoured by a master change ("make it brighter" while
brightness < 1.0; "less reactive" while audio_reactivity is high), say so
in your reply and tell the user which slider to move. Otherwise, design
your spec assuming the masters stay where they are.
```

This buys four things:

1. **The agent never undoes a master.** Masters are not in `update_leds`'s
   schema, so the LLM cannot put `brightness` back to 1.0 by accident.
2. **The agent isn't gaslit.** When the operator pulled brightness to 0.4
   and the user asks "make it brighter", the agent now knows why its spec
   alone won't fully satisfy the request and can surface that.
3. **The tool surface stays narrow.** `update_leds` keeps its single,
   well-tested shape. Master tweaks have a separate REST surface the UI
   binds directly. No tool-call round-trip just to drag a slider.
4. **The conversation stays focused.** "Less audio reactive" through chat
   gets a textual nudge ("pull the audio-reactivity slider down") rather
   than a wrestling match where the agent tries to express a master via
   the spec.

If a "set master" tool ever ships, it should be a *separate* tool with
its own session-level rate limit, not folded into `update_leds`. That
preserves the invariant that any single `update_leds` call is purely a
creative re-render with no operator-state side effects.

### 7.6 Persistence

Session-only by default — masters reset to `1.0 / 1.0 / 1.0 / 1.0 / false`
on engine restart. The UI's "save as defaults" action sends `PATCH /masters`
with `persist: true`; the server writes the current values into `config.yaml`
under a new `masters:` block (parallel to `audio:` and `agent:`, atomic with
`.bak`, same path the audio device picker already uses).

### 7.7 Future masters worth designing for, not building yet

These are sketches — keep them out of v1, but the dataclass + render
context shape above accommodates them as one-line additions:

- **Per-band reactivity** (`audio_low_reactivity`, `audio_mid_reactivity`,
  `audio_high_reactivity`) — three sliders if the operator wants to mute
  the bass response without touching mids/highs. Useful in a sub-heavy
  room where the kick is dominating everything.
- **Master hue rotation** (`hue_shift`, 0..1 cycles) — a constant offset
  added inside `palette_lookup`'s hue_shift pathway. Different vibe
  without retyping the spec.
- **Strobe lock** (`strobe_min_period_s`) — caps any LFO with
  `period_s < strobe_min_period_s` to that floor. Photosensitivity
  safety toggle.
- **Master colour-temperature trim** — warm/cool slider as a final RGB
  matrix. Useful when the install reads too cool against red stage
  lights.

The principle is the same: each future master is one field on
`MasterControls`, one read in the relevant primitive or stage, one slider
in the UI. No catalogue change, no agent-prompt change, no spec format
change.

---

## 8. How the LLM documentation gets generated

`surface.generate_docs(...)` returns a string built from the registry:

```
CONTROL SURFACE — primitives and how they compose.

KIND: scalar_field (per-LED scalar in [0, 1])
  wave           — 1-D travelling pattern. {axis, wavelength, speed, shape, ...}
                    schema: { ... pydantic JSON schema, $refs inlined ... }
  radial         — distance-from-point pattern. ...
  gradient       — static linear ramp. ...
  noise2d        — value-noise lattice scrolled in time. ...
  sparkles       — Poisson-stamped twinkles with exponential decay. ...
  position       — raw normalised position component. ...
  constant       — fixed scalar.

KIND: scalar_t (time-varying scalar, no spatial dep)
  lfo            — sin / saw / triangle / pulse oscillator. ...
  audio_band     — rms / low / mid / high / peak; auto-scaled to ~[0, 1]. ...
  envelope       — asymmetric attack/release wrapping any scalar. ...
  clamp          — clip to [floor, ceiling] then map to [out_min, out_max]. ...

KIND: combinator (composes children of any kind)
  mix(a, b, t)   — pointwise lerp. ...
  mul / add / screen / max / min ...
  remap(input, fn) — sin, abs, sqrt, pow, step. ...

KIND: rgb_field (LED-output RGB)
  palette_lookup(scalar, palette, brightness?, hue_shift?) — the most common leaf.
  solid(rgb) — uniform colour.

NAMED PALETTES
  rainbow, fire, ice, sunset, ocean, warm, white, black, mono_<hex>

BLEND MODES
  normal, add, screen, multiply

EXAMPLES
  [ today's anchor set expressed in the new tree form, plus the named
    EXAMPLE_TREES (warm_drift, fire_chase, sparkle_only, peak_kick, …)
    inlined in full, plus three composition showcases: palette swap on
    the kick (mix of two palette_stops gated by envelope(audio_band("low"))),
    uniform colour pulsing with audio (palette_lookup with
    scalar=audio_band("rms") — the scalar_t broadcasts across all LEDs,
    no spatial primitive needed), and an axis cross (mul of wave(axis=x)
    and wave(axis=y) gated by audio.high). ]

ANTI-PATTERNS
  - There is no top-level `bindings` — modulation lives on the parameter as a node.
  - `palette` is itself a node; bare strings ("fire") are shorthand for `palette_named`.
  - `mix` is polymorphic; don't reach for a separate `palette_mix` — it doesn't exist.
  - ...
```

The agent module's `system_prompt.py` becomes:

```python
def build_system_prompt(*, topology, engine, audio_state, presets_dir=None):
    return "\n\n".join([
        ROLE_PARAGRAPH,                                  # static
        _summarise_install(topology),                    # auto from Topology
        _summarise_current_state(engine),                # auto from Engine
        _summarise_audio(audio_state),                   # auto from AudioState
        surface.generate_docs(...),                      # the whole catalogue
        RUBRIC,                                          # static
    ])
```

The hand-written EFFECTS / NESTED-TYPES / ANTI-PATTERNS / EXAMPLES blocks
all disappear from `system_prompt.py`. They live next to the primitives
that mention them.

---

## 9. What lives outside `surface.py`

These do **not** move into surface.py — they don't touch the catalogue:

- `engine.py` — the render loop, calibration, topology hot-swap. Gains
  ownership of `MasterControls`, accumulates `effective_t` per tick, and
  builds the `RenderContext` (including the audio-reactivity-scaled audio
  view) before passing it to the mixer.
- `mixer.py` — layer stack, blend logic, crossfade. Becomes thinner: a
  `Layer` is `{render_fn, blend, opacity}`, no `Effect` reference. After
  blending, applies the saturation pull and the brightness gain (master
  output stage) before handing off to PixelBuffer.
- `pixelbuffer.py` — float→uint8 + gamma. Unchanged (gamma still last,
  after the master output stage).
- `transports/` — DDP / simulator / multi. Unchanged.
- `audio/` — capture, analyser, normaliser, state. Unchanged.
- `topology.py` — spatial model. Unchanged.
- `api/server.py` — REST endpoints rename to match the new shape. The
  `{name}` segment is dropped because there are no named effects anymore,
  every layer is a tree of primitives:
    - `POST /effects/{name}` → `POST /layers`  (body `{node, blend?, opacity?}`)
    - `PATCH /layer/{i}` → `PATCH /layers/{i}` (body `{node?, blend?, opacity?}`)
    - `DELETE /layer/{i}` → `DELETE /layers/{i}`
    - `GET /effects` → `GET /surface/primitives` (returns
      `surface.generate_docs(format="json")`)
  `POST /presets/{name}` keeps its path — presets stay as operator-saved
  layer stacks. New: `GET /masters` and `PATCH /masters` for the operator
  slider row; `/state` and `/ws/state` payloads gain a `masters` block.
- `agent/system_prompt.py` — collapses to the ~30 lines shown above, plus
  a small `_summarise_masters(masters)` block that emits the read-only
  master values for the agent (see §7.5).
- `agent/tool.py` — the `update_leds` tool's argument schema is now
  `UpdateLedsSpec` from `surface.py`. The validation / clamp / pre-flight
  logic stays, just imports flip. The schema deliberately has no master
  fields; an attempt by the LLM to nest `masters: {...}` is rejected by
  pydantic `extra="forbid"` and flows back as a structured error.
- `presets.py` — loads tree-shape YAML only. The four shipped presets are
  hand-translated in commit 2 (§10); no shim, no dual-format support.

That's a *narrow* surface for a refactor. Everything spatial / temporal /
audio-related is untouched.

---

## 10. Migration plan — one branch, three commits, single PR

No feature flag. With one developer and no live users, parallel-running two
`update_leds` schemas is overhead with no real safety win — and it forces
the agent's tool definition + system prompt to know which mode it's in,
which is annoying to keep coherent across tests. Instead: a long-lived
branch (`refactor/surface`) with a strict parity test suite, three logically
separate commits for review hygiene, and a single cutover PR.

The parity guarantee is provided by **golden-frame tests**: for each of the
four shipped presets, render N seeded frames with the legacy path on
`main`, save them as fixtures, and assert the new path renders the same
frames within a small per-channel epsilon (≤ 1/255 for analytical fields,
≤ 2/255 for `noise2d` — FP-accumulation order shifts there are unavoidable
under refactor and don't matter visually). Same fixture pinned audio + LFO
state. If the parity tests pass, the cutover is safe.

**Prerequisite (lands first inside commit 1):** a `ScriptedAudioState`
fixture in `tests/conftest.py` that yields canned `AudioState` snapshots
per tick from a small in-memory script. Today's `SoundDeviceSource` is
non-deterministic and machine-dependent, so fixtures need a deterministic
audio feed. We feed the fixture into the parity tests by stubbing
`AudioCapture.state` (or directly building a `RenderContext` with the
scripted state in unit tests), not by adding a new `AudioSource` subclass:
`AudioSource` emits PCM blocks via callback (`audio/source.py`), not
`AudioState` snapshots, so a "scripted source" would have to also
re-implement the analyser to produce comparable values. A scripted state
is one layer up and exactly what the parity tests need. A real
file-replay `AudioSource` for analyser/source integration is still a
worthwhile follow-up, but it is *not* a prerequisite for this refactor
and lives on the roadmap, not here.

### Commit 1 — green-field `surface.py`

1. Create `src/ledctl/surface.py` with the registry, the primitives that
   can express today's catalogue (`wave`, `radial`, `noise2d`, `sparkles`,
   `lfo`, `audio_band`, `envelope`, `palette_named`, `palette_stops`,
   `palette_lookup`, plus the combinators), the spec types, the compiler,
   and `generate_docs()`.
2. Add `tests/test_surface_parity.py` — generate the legacy fixtures from
   today's presets first, then assert the new compiler reproduces them.
3. Nothing else in the repo changes. The new file is dead code at this
   commit and the suite should still be green.

### Commit 2 — port presets, prompt, tool

4. Translate the four shipped presets (`default`, `chill`, `peak`, `cooldown`)
   to tree-form YAML by hand. Each old `effect: X` maps to a tree of 2–4
   primitives. Implicit defaults from today's binding system must become
   explicit during translation, or the parity fixtures won't match:
   - any `bindings.*.gain: G` and `curve: ...` become explicit `gain` /
     `curve` params on the wrapping `envelope` node (envelope mirrors
     the legacy ModulatorSpec field-for-field — see §3.1 — so this is
     mechanical),
   - audio-driven bindings that relied on the *per-slot* envelope
     defaults (see `effects/modulator.py:SLOT_DEFAULTS`) must spell
     them out: `brightness` slots become
     `envelope(attack_ms=30, release_ms=500)`, `speed` slots become
     `envelope(attack_ms=200, release_ms=200)`, `hue_shift` slots
     become `envelope(attack_ms=200, release_ms=2000)`. The new
     `envelope` primitive's own defaults (60 ms attack, 250 ms release
     per §3.3) are for greenfield use; they do *not* match any single
     legacy slot, so trusting them during migration would silently
     drift the look of every preset.
   Audit `config/presets/*.yaml` for `gain:`, `curve:`, `floor:`,
   `ceiling:`, and audio-source bindings before freezing the golden
   frames.
5. Regenerate `agent/system_prompt.py` to call `surface.generate_docs()`
   plus a small `_summarise_masters(masters)` helper. Move `EXAMPLES`,
   `ANTI_PATTERNS` from `system_prompt.py` into `surface.py` (where
   they belong).
6. Update `agent/tool.py` to validate against `UpdateLedsSpec`.
7. Update `engine.py` / `mixer.py` to call `surface.compile()` and run the
   master output stage (saturation + brightness gain).
8. Add `MasterControls` plumbing (§7) — `RenderContext`, REST endpoints,
   `/state` payload extension.
9. Rewrite the tests that exercise the old shape (agent, tool, presets,
   mixer-master). Behaviour they test is preserved; only assertions reshape.
10. Run the full suite — parity from commit 1 plus the rewritten tests.
    Smoke-test on the simulator.

### Commit 3 — delete the old vocabulary

11. Delete `src/ledctl/effects/{base,registry,palette,modulator,_color}.py`
    and `src/ledctl/effects/fields/`. Move what's worth keeping
    (e.g. `_color.hex_to_rgb01`) into `surface.py`.
12. Update `README.md` — the "Effects, layers, and presets" and
    "Switching effect modes on the fly" sections need new curl examples,
    plus a `Master controls` block.

Each commit is reviewable in isolation. The PR ships only after all three
land and the simulator smoke test (every preset, every master combination)
is clean.

---

## 11. What this enables — concrete wins

### For the operator UI (Phase 7 prerequisite work)

The new explicit-controls page (the immediate motivation for this refactor)
queries `GET /surface/primitives`, gets back the full catalogue with JSON
Schemas, and renders one form section per primitive. Adding a new
primitive in `surface.py` instantly populates a new section in the UI with no
client-side change. Sliders, dropdowns, and color pickers map to the same
JSON Schema annotations the LLM reads.

### For the LLM

A request like *"a sine wave on the x-axis times an LFO on y, gated by
audio.high"* becomes literally one node tree. Today that requires inventing
a new effect. The model improves at compositional asks because the surface
*is* compositional.

### For new visual ideas

- "Plasma": `mix(wave(axis=x), wave(axis=y), lfo(period=4))` →
  `palette_lookup(rainbow)`. Two existing primitives, no new code.
- "Fluid": one new `@primitive` for an advection step plus the existing
  `palette_lookup`. Three lines elsewhere.
- "Hue offset top-vs-bottom row": already expressible as
  `palette_lookup(scalar, palette, hue_shift=position("y"))`.

### For testing

Each primitive is a pure function over numpy arrays. They get unit tests in
isolation (already true), but combinator tests gain massive leverage —
"`mul(x, y)` produces the pointwise product" replaces "`audio_pulse` honours
floor/ceiling/sensitivity" plus the same shape for every other audio-bound
effect.

---

## 12. Risks & open questions

1. **Token budget for `generate_docs()`.** Inlining JSON schemas for every
   primitive plus combinators plus recipes plus examples could push the
   system prompt past today's ~2k baseline. Mitigations baked in from the
   start: (a) one-line description budget per param (already in
   `CLAUDE.md`); (b) `generate_docs()` emits a *compact* form — `kind |
   accepted-input-kinds | param table` — not raw `model_json_schema()`
   dumps; (c) the full pydantic schemas are still available via
   `GET /surface/primitives` for the operator UI, which doesn't share the
   agent's token budget. Measure after commit 2 and trim further if
   needed — a layer-count cap is the obvious lever if the current-state
   block balloons, but don't preemptively add it.
2. **Compile-time validation locality.** With the recursive
   `{kind, params: dict}` shape, pydantic only validates the outer
   envelope at JSON-parse time. Each primitive's `Params` is re-validated
   when the compiler walks the tree. Practically: errors land at compile
   instead of parse, but the error path becomes a path through the tree
   (`layers[0].node.params.scalar.params.shape`) which is *more* useful to
   the LLM, not less. Make sure `tool.py`'s structured-error formatter
   includes the full path and (where applicable) the `extra_forbidden →
   valid keys` hint that today's path already produces.
3. **Stateful primitives (sparkles, trail) need careful state lifecycle.**
   On a layer crossfade, the *new* compiled tree gets a fresh state. On
   `update_layer` (PATCH), we recompile the whole tree — simpler, matches
   today's behaviour, and means a slider drag against the operator UI can
   visibly reset sparkle state at high frequency. Crossfades hide single
   resets; rapid PATCH drags don't. **Mitigation:** the operator UI should
   debounce its slider PATCHes (e.g. 100 ms) rather than firing on every
   pixel of mouse motion. Worth an explicit Phase 7 note when that UI
   lands.
4. **Type checking the AST.** A naive structural validation lets a user pass
   an `rgb_field` where a `scalar_field` is expected (e.g.
   `wave(speed=palette_named("fire"))`). The compiler must reject this
   with a clear "`wave.speed` expects scalar_t; got rgb_field" error.
   Use the `output_kind` tag mentioned in §3.2 plus the compatibility
   table.
5. **`compile_lut` and the LUT cache.** Today every named palette compiles
   to a 256-entry LUT once per effect instance. In the new shape,
   `palette_named("fire")` is a primitive whose compiled state is the LUT.
   If two layers use the same named palette they each have their own LUT
   today — not worth caching globally; the cost is microseconds. Note the
   non-optimisation rather than building it.
6. **Performance.** No regression expected — the hot-path numpy ops are the
   same. The compile step does an extra dict walk and pydantic validation
   per *layer change*, not per frame. Measure once after commit 2 against
   `engine.fps` on the dev simulator at 1800 LEDs.
7. **Auto-generated prose docs are tempting but worse than schema dumps.**
   The schema is what the validator reads; the prose is paraphrase. Keep
   prose as an opt-in, never the source.
8. **Master visibility leaks operator state into the agent prompt.** With
   §7.5 the masters block is part of the system prompt and regenerates
   each turn — same path as the audio snapshot. That means each
   slider-drag during an active conversation invalidates the prompt cache
   on the next turn. Acceptable: the masters block is small (5 floats)
   and dragging a slider mid-chat is rare. If it ever becomes a hot path,
   move masters to the rolling-buffer tail rather than the system prompt.

---

## 13. Concrete file deltas

What this refactor adds / removes / edits:

```
+ src/ledctl/surface.py                              (new — ~600–800 lines)
+ src/ledctl/masters.py                              (new — ~80 lines: dataclass, REST handlers, persistence)
+ tests/test_surface_primitives.py                    (new — per-primitive)
+ tests/test_surface_compile.py                       (new — AST → render)
+ tests/test_surface_docs.py                          (new — generated docs are stable)
+ tests/test_masters.py                               (new — speed/audio/output stages, agent-blindness)

~ tests/conftest.py                                   (add ScriptedAudioState fixture for deterministic parity / unit tests)
~ src/ledctl/agent/system_prompt.py                  (collapse to ~80 lines)
~ src/ledctl/agent/tool.py                           (UpdateLedsSpec from surface)
~ src/ledctl/api/server.py                           (POST /effects/{name} → POST /layers; PATCH/DELETE /layer/{i} → /layers/{i}; GET /effects → GET /surface/primitives)
~ src/ledctl/engine.py                               (Layer holds render_fn, not Effect; effective_t accumulator; RenderContext build)
~ src/ledctl/mixer.py                                (Layer dataclass thinner; output-stage saturation+brightness)
~ src/ledctl/presets.py                              (load tree-form YAML — preset = layer stack of NodeSpecs)
~ config/presets/{default,chill,peak,cooldown}.yaml  (rewritten in tree form)
~ tests/test_effects.py                              (rewritten as tests/test_surface_primitives.py)
~ tests/test_agent.py                                (assertions reshape)
~ README.md                                          (effects + curl sections)

– src/ledctl/effects/base.py                         (folded into surface.py)
– src/ledctl/effects/registry.py                     (folded into surface.py)
– src/ledctl/effects/palette.py                      (folded into surface.py)
– src/ledctl/effects/modulator.py                    (folded into surface.py)
– src/ledctl/effects/_color.py                       (folded into surface.py)
– src/ledctl/effects/fields/                         (folded into surface.py)
– src/ledctl/effects/__init__.py                     (replaced by surface.py)
```

Net file count drops; the lines-of-code total is roughly flat (~1100 → ~1100)
but lives in one well-organised module.

---

## 14. The order of work

The seven steps below squash into the three commits described in §10 at
PR time (steps 1–4 → commit 1, steps 5–6 → commit 2, step 7 → commit 3).
Commit 1 stays pure dead-code: the new file compiles and passes parity
against in-memory tree specs constructed in the test, but nothing else
in the repo (presets, API, agent, engine) is touched. Preset YAML
translation, agent/tool/API cutover, and the master-controls plumbing
all land together in commit 2.

1. **Skeleton.** Branch off main as `refactor/surface`. Add a
   `ScriptedAudioState` fixture in `tests/conftest.py` so audio
   fixtures are deterministic (see §10). Generate golden-frame fixtures
   from the four shipped presets on `main` (60 deterministic frames
   each, pinned audio + LFO state via the scripted state). Create
   `src/ledctl/surface.py` with the registry, the spec types
   (`NodeSpec`, `LayerSpec`, `UpdateLedsSpec`), and the compiler
   scaffold (validates + walks but only one primitive: `solid`). The
   parity-test harness exists, the new file is dead code on the branch,
   the legacy suite is still green.
2. **Scalar fields and palettes.** `wave`, `radial`, `gradient`,
   `noise2d`, `position`, `constant`, `palette_named`, `palette_stops`,
   `palette_lookup`. Port the existing math; copy the unit tests over
   with shape adjustments.
3. **Modulators and combinators.** `lfo`, `audio_band`, `envelope`,
   `clamp`, `range_map`, `mix` (polymorphic — also handles palette lerp),
   `mul`, `add`, `screen`, `remap`, `threshold`. Port the existing
   modulator/envelope logic.
4. **Stateful primitives and example trees (still dead code).**
   `sparkles`, `trail`, and the `EXAMPLE_TREES` dict (named NodeSpecs
   that `generate_docs()` inlines into the prompt). Express each
   shipped preset (`default`, `chill`, `peak`, `cooldown`) as an
   in-memory `NodeSpec` inside the parity test (no YAML rewrites yet)
   and assert the new compiler matches the legacy golden frames.
   Commit 1 closes here.
5. **Preset / docs / agent cutover (commit 2 begins).** Rewrite the
   four preset YAMLs in tree form (using exactly the trees from step 4,
   including the explicit per-slot envelope defaults from §10).
   Implement `generate_docs()` (compact form: kind |
   accepted-input-kinds | one-line param table). Rewire
   `system_prompt.py` and add `_summarise_masters()`. Rewrite `tool.py`
   schema to `UpdateLedsSpec`. Update `presets.py` to load tree-form
   YAML. Update `api/server.py` endpoints (`POST /layers`, etc.).
   Wire the engine + mixer to `surface.compile()` and switch
   `Mixer.render(ctx, out)` (alpha on `ctx.wall_t`, layers read
   `ctx.t` — see §4.3 / §7.2). Delete the legacy `Effect`-instantiation
   path's *call sites*; the `effects/` files themselves stay until
   commit 3 to keep this commit's diff legible. Update tests. Re-run
   the full suite plus parity tests.
6. **Masters.** Add `MasterControls`, `RenderContext`, `effective_t`
   accumulation in `engine.py`, output-stage application (saturation pull
   + brightness gain) in `mixer.py`, audio-stage pre-scaling baked into
   `RenderContext.audio` in `engine.py`, `GET/PATCH /masters` (with the
   `persist` flag mirroring `/audio/select`) + `/state` block in
   `server.py`, optional `masters:` config block + persistence shim.
   Extend `system_prompt.build_system_prompt(...)` to ingest the current
   `MasterControls` and emit the read-only block (§7.5). End state: a
   curl-driven `PATCH /masters` slows the wave, dims the room, damps
   audio reactivity — and the next `update_leds` call leaves all five
   fields untouched, while the agent's reply acknowledges the master
   state when relevant. Commit 2 closes here.
7. **Delete the old vocabulary (commit 3).** Remove `effects/`
   directory contents folded into `surface.py`. Update README. Tag a
   commit so the diff is reviewable.

After this lands, **Phase 7 (mobile / explicit-controls UI)** can build
against `GET /surface/primitives` directly: every primitive's JSON Schema
becomes a control panel section automatically, and a new primitive ships to
the UI by being added to `surface.py`. Same for any future LLM agent
upgrades — there's no second copy of the vocabulary to keep in sync.
