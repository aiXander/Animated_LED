# Surface v2 — LLM-as-Author Design Plan

> **Goal.** Replace the typed `{kind, params}` primitive graph with **LLM-authored Python effects** that have direct access to a curated runtime API, plus an **auto-generated, ad-hoc operator control panel** the UI renders dynamically. Prototype in a fully isolated `surface_v2/` package, gated by a single boolean toggle in config — v1 stays untouched and shippable for the festival.

---

## 1. Executive summary

The current surface (`src/ledctl/surface/`) is a clean, well-typed compositional graph: ~30 primitive `kind`s, polymorphic combinators, strict per-path compile errors. It is excellent for **what it can express**. It is also a **glass ceiling**: any new spatial idea, particle behaviour, or orchestration the operator can describe in one sentence requires either (a) a new primitive class (a code change) or (b) a baroque tree the LLM cannot reliably author.

Example the user gave that v1 categorically cannot do:

> *"Create two comets going from left to right, the top one is leading the bottom one by 1/6th of the total distance. After a comet has passed, it leaves behind sparkles flickering in its color that gradually darken and fade out. The top comet is red, the bottom one deep blue. Both of them are pulsating in brightness between 0.6 and 1.0 max brightness based on the 'low' audio signal."*

This needs: two stateful particle objects, a side-aware spatial filter (`side_top` / `side_bottom`), a coupled "deposit sparkle on pass" mechanism with per-pixel fade, and audio-modulated brightness. None of that composes from existing primitives without authoring 3–4 new ones.

**The shift.** Stop curating a vocabulary. Give the LLM:

1. A clear **spatial model** (positions, named frames, strip layout).
2. A clear **audio model** (the same `low / mid / high / beat / bpm` it already knows).
3. A **render contract**: a Python `Effect` subclass with `__init__(ctx)` and `render(ctx) → (N, 3) float32` in `[0, 1]`.
4. A small, ergonomic **helper namespace** (numpy, palette/HSV/gauss/pulse helpers, ...) and all existing params the LLM could use / reference in its code.
5. A **param schema** the LLM emits *alongside* the code, which the operator UI renders as a dynamic mini control panel (sliders, color pickers, dropdowns, toggles).

The LLM writes short, vectorised numpy code (one Effect file = ~30–120 lines for most things). The UI gets dynamic controls per effect. The user can hand-tune via sliders without re-prompting.

**Cost** of this freedom: a small Python sandbox, a livecoding error path, and trusting the LLM to write fast vector code. **Benefit**: arbitrary expressiveness, instant prototyping by description, and the v1 layer of "vocabulary work" disappears.

---

## 2. Why v1 hits a wall (concrete failure modes)

Going through `surface/primitives/` and the user's example prompt:

- **Coupled stateful systems.** The v1 `comet` primitive owns its trail; nothing can attach a *second* effect to its passage (sparkles deposited at the head). Each new coupling = new primitive.
- **Per-instance asymmetric configuration.** "Top comet red, bottom comet blue, top leads by 1/6" requires two `comet` layer instances + a manual `side_top`/`side_bottom` spatial mask. v1 has no way to make one comet *aware* of another.
- **Compositional explosion.** What looks like one idea ("particles that leave fading sparkle trails") becomes a 7-deep node tree. The LLM authors it inconsistently across calls; small typos compile but produce visually wrong results that are hard to debug from a chat turn.
- **Param surface is fixed.** The operator can only nudge the 5 master controls. The LLM cannot expose "lead offset between comets" as a UI dial — it can only re-emit the whole stack with a new constant.
- **Token economy.** `surface/docs.py` regenerates the full primitive catalogue every system prompt. We're at ~7–8k tokens of catalogue today (after the Phase G refactor). Each new primitive grows that bill.

The pattern: every time the operator describes something genuinely new, **we** end up writing code, not the LLM. v2 inverts this.

---

## 3. The v2 contract

### 3.1 What the LLM emits per turn

A single tool call, **`write_effect`**, with shape:

```jsonc
{
  "name": "twin_comets_with_sparkle_trails",
  "summary": "Two comets sweep left→right, top leads bottom by 1/6. Trails leave fading sparkles in the comet's colour.",
  "code": "<python source as a single string>",
  "params": [
    {
      "key": "leader_color",
      "label": "Top comet colour",
      "control": "color",
      "default": "#ff2020"
    },
    {
      "key": "follower_color",
      "label": "Bottom comet colour",
      "control": "color",
      "default": "#1040ff"
    },
    {
      "key": "lead_offset",
      "label": "Top–bottom lead",
      "control": "slider",
      "min": 0.0, "max": 0.5, "step": 0.01,
      "default": 0.1667
    },
    {
      "key": "speed",
      "label": "Comet speed",
      "control": "slider",
      "min": 0.05, "max": 2.0, "step": 0.01,
      "default": 0.4
    },
    {
      "key": "sparkle_decay",
      "label": "Sparkle fade time (s)",
      "control": "slider",
      "min": 0.1, "max": 5.0, "step": 0.05,
      "default": 1.5
    },
    {
      "key": "audio_band",
      "label": "Brightness driver",
      "control": "select",
      "options": ["low", "mid", "high"],
      "default": "low"
    }
  ]
}
```

A second tool, **`update_params`**, just patches the active effect's param values without regenerating code. The LLM is taught: if the user's request is satisfiable by changing existing param defaults, prefer `update_params` (instant, no compile, no crossfade); otherwise `write_effect`.

### 3.2 What the code looks like

The LLM writes **a single Python module** that defines exactly one `Effect` subclass. The class is plucked out by the loader (any subclass of `Effect` defined at module top level is taken). Lifecycle:

```python
# Everything the LLM-authored code can reference is in the namespace
# documented under §5 ("RUNTIME API"). No imports required.

class TwinCometsWithSparkleTrails(Effect):
    """Two comets sweep left→right; trails deposit fading sparkles."""

    def init(self, ctx):
        # ---- precompute spatial masks (cheap, runs once) ----
        # ctx.frames.x is per-LED in [0, 1]; signed_x in [-1, 1]
        self.x = ctx.frames.x                         # (N,)
        self.top = ctx.frames.side_top.astype(bool)   # (N,) bool
        self.bottom = ctx.frames.side_bottom.astype(bool)

        # ---- per-LED state buffers ----
        self.sparkle_age = np.full(ctx.n, np.inf, dtype=np.float32)
        self.sparkle_rgb = np.zeros((ctx.n, 3), dtype=np.float32)

        # ---- comet state ----
        self.head_top = 0.0       # x in [0, 1]
        self.head_bot = 0.0
        self.last_top_idx = -1
        self.last_bot_idx = -1

        # ---- output buffer (re-used every frame, no per-frame alloc) ----
        self.out = np.zeros((ctx.n, 3), dtype=np.float32)

    def render(self, ctx):
        p = ctx.params
        dt = ctx.dt

        # ---- advance heads (wrap at 1.0) ----
        self.head_top = (self.head_top + p.speed * dt) % 1.0
        self.head_bot = (self.head_top - p.lead_offset) % 1.0

        # ---- audio-driven brightness in [0.6, 1.0] ----
        band = ctx.audio.bands[p.audio_band]   # 0..1, smoothed upstream
        amp = 0.6 + 0.4 * band

        # ---- decay sparkles (exp fade with half-life = sparkle_decay) ----
        self.sparkle_age += dt
        fade = np.exp(-self.sparkle_age / max(p.sparkle_decay, 1e-3))
        self.out[:] = self.sparkle_rgb * fade[:, None]

        # ---- draw comet heads as gaussians along x, masked top/bottom ----
        self._stamp(self.out, self.head_top, hex_to_rgb(p.leader_color), self.top, amp)
        self._stamp(self.out, self.head_bot, hex_to_rgb(p.follower_color), self.bottom, amp)

        # ---- deposit sparkles at the head's nearest LED on each side ----
        self._deposit(self.head_top, hex_to_rgb(p.leader_color), self.top, "top")
        self._deposit(self.head_bot, hex_to_rgb(p.follower_color), self.bottom, "bot")

        return self.out                             # (N, 3) float32 in [0, 1]

    # ---- helpers (still inside the LLM's source) ----
    def _stamp(self, dst, head_x, color, mask, amp):
        d = np.abs(self.x - head_x)
        d = np.minimum(d, 1.0 - d)                  # wrap distance
        g = np.exp(-(d * d) / (2 * 0.03 * 0.03)) * amp
        dst[mask] += g[mask, None] * color

    def _deposit(self, head_x, color, mask, side):
        # Snap a sparkle pixel near the head once per ~1/30 s
        idxs = np.where(mask)[0]
        i = idxs[np.argmin(np.abs(self.x[idxs] - head_x))]
        prev = self.last_top_idx if side == "top" else self.last_bot_idx
        if i != prev:
            # fresh deposit: random nearby pixel for a sparkle feel
            j = idxs[(np.searchsorted(self.x[idxs], head_x) + np.random.randint(-2, 3)) % idxs.size]
            self.sparkle_age[j] = 0.0
            self.sparkle_rgb[j] = color
            if side == "top":
                self.last_top_idx = i
            else:
                self.last_bot_idx = i
```

That's the whole effect — ~50 lines of LLM-written Python. Note that nothing in the helper namespace was a special-cased primitive; everything came from numpy + a couple of helpers (`hex_to_rgb`, the named coordinate frames `ctx.frames.x` / `side_top` / `side_bottom`). **No new primitive class was needed.**

### 3.3 Crucial properties

- **One file = one effect.** No multi-class compositions; no "primitive registration." Discoverable, swappable, persistable.
- **`init` runs once per swap.** All per-LED precompute, masks, RNGs, state buffers live there.
- **`render` is hot-path.** Vectorised numpy. Returns `(N, 3) float32 in [0, 1]`. No allocation rule: encourage re-using `self.out`.
- **Params are first-class and live.** Sliders update `ctx.params.<key>` between frames, no recompile. The LLM never sees user param changes — only the operator does.
- **State is `self.*`.** No globals, no persistence between effect swaps (deliberately — "blank slate" is the model the LLM understands).
- **`render` may raise.** We 'test-run' the code first, catch any errors, log the traceback, and ship the error back to the LLM for the next try. LLM gets 2 consecutive tries by default (see config.yaml). 

---

## 4. Execution model

### 4.1 Sandboxed `exec`

Python is the right substrate (already in the hot path, numpy is essential, festival deployment is a single trusted box — security threat model is "stop the LLM accidentally calling `os.system`" not "defend against malware").

```python
# surface_v2/sandbox.py
import builtins, ast, types

_SAFE_BUILTINS = {n: getattr(builtins, n) for n in (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
    "int", "isinstance", "len", "list", "map", "max", "min", "pow", "range",
    "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
    "zip", "type", "print",  # print routed to logger
)}
# explicitly absent: __import__, open, exec, eval, compile, input, getattr/setattr/delattr,
#   globals, locals, vars, breakpoint, exit, quit

_FORBIDDEN_NODES = (ast.Import, ast.ImportFrom)  # AST-level reject

def compile_effect(source: str, name: str) -> type[Effect]:
    tree = ast.parse(source, filename=f"<llm:{name}>")
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            raise EffectCompileError("imports are forbidden — the runtime API "
                                     "is already in the global namespace")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise EffectCompileError(f"dunder attribute access disallowed: {node.attr}")
    code = compile(tree, f"<llm:{name}>", "exec")
    mod_globals = build_runtime_namespace()        # see §5
    mod_globals["__builtins__"] = _SAFE_BUILTINS
    mod = types.ModuleType(f"effect_{name}")
    mod.__dict__.update(mod_globals)
    exec(code, mod.__dict__)
    classes = [v for v in mod.__dict__.values()
               if isinstance(v, type) and issubclass(v, Effect) and v is not Effect]
    if len(classes) != 1:
        raise EffectCompileError(f"expected exactly one Effect subclass, got {len(classes)}")
    return classes[0]
```

This is a **bag-check, not a vault**. It catches the LLM's reflex `import os` mistakes and prevents accidental file/network calls. Anyone who actually wanted to escape this could, but they'd need physical access to the prompt pane on the festival rig — out of scope.

### 4.2 Lifecycle

```
operator chats ──► LLM emits write_effect ──► sandbox compile ──► instantiate ──►
                                                                     │
                                                                     ▼
                                                         init(EffectInitContext)
                                                                     │
                              ┌──────────────────────────────────────┤
                              ▼                                      │
                   per-frame: render(EffectFrameContext)              │
                              │                                      │
                              └──── crash ──► swap to error pulse ◄──┘
                                              dispatch to LLM
```

Crossfade between two `Effect` instances is a 1:1 reuse of the existing `Mixer.crossfade_to` mechanism — render both, lerp, ship to transport.

### 4.3 Performance contract

The LLM is told in the system prompt:
- **Budget:** `render` should complete in ≤5 ms for N=1800. Measure: we time every render and surface a rolling p95 alongside the catalogue. If the budget is missed for >2 s, the supervisor degrades gracefully (drops to half the target FPS, surfaces a warning to the operator UI).
- **Idiom:** all per-LED work via numpy vector ops. Loops over `range(N)` are forbidden by convention (we don't enforce, but the prompt teaches the pattern hard).
- **Output:** must be `(N, 3)` float32 (the loader checks shape and dtype on first render and raises with a clear message if wrong; LLM sees it and corrects).

---

## 5. The runtime API the LLM is given

The namespace `build_runtime_namespace()` populates is the **entire** surface area the LLM author sees. Keep it small, useful, and stable.

### 5.1 Always available (top-level names)

| name             | type             | purpose                                                    |
| ---------------- | ---------------- | ---------------------------------------------------------- |
| `np`             | module           | numpy. The workhorse.                                      |
| `Effect`         | class            | The base class to subclass.                                |
| `hex_to_rgb`     | `(str) → (3,)`   | `'#ff8000'` → `np.array([1, 0.5, 0], float32)`.            |
| `hsv_to_rgb`     | `(h, s, v) → (3,)` or vectorised over arrays | colour conversion. |
| `lerp`           | `(a, b, t)`      | `a*(1-t) + b*t`, broadcasts.                               |
| `clip01`         | `(x)`            | `np.clip(x, 0, 1)`.                                        |
| `gauss`          | `(x, sigma)`     | normalised gaussian profile (peak = 1).                    |
| `pulse`          | `(x, width)`     | cosine bump in `[-width, +width]`, peak = 1, else 0.       |
| `tri`            | `(x)`            | triangle wave on [0, 1].                                   |
| `wrap_dist`      | `(a, b, period)` | shortest signed distance with wrap (great for `u_loop`).   |
| `palette_lerp`   | `(stops, t)`     | sample a multi-stop palette at scalar/array t.             |
| `named_palette`  | `(name) → (LUT_SIZE, 3)` | look up `'fire'`, `'rainbow'`, etc.                |
| `audio_smooth`   | `(state, key, halflife_s)` | pull a smoothed band value from a per-effect smoother. Helps avoid harsh visual jitter when the LLM wants softer reactivity than the upstream gives. |
| `rng`            | `np.random.Generator` | seeded per-effect, deterministic across reloads.      |
| `log`            | logger           | `log.warning(...)` — never `print` to stdout.              |

**Constants:** `PI`, `TAU`, `LUT_SIZE`.

Anything else the LLM wants — sin, cos, exp, FFT — is `np.sin`, `np.cos`, `np.exp`, `np.fft`. We don't re-export numpy primitives; the LLM knows numpy.

### 5.2 The `EffectInitContext` (passed to `init`)

```python
@dataclass
class EffectInitContext:
    n: int                                 # pixel count, e.g. 1800
    pos: np.ndarray                        # (N, 3) float32 in [-1, 1]
    frames: FrameMap                       # named per-LED scalars (see §5.3)
    strips: list[StripInfo]                # per-strip metadata
    config: dict                           # rig-level info (bbox, target_fps)
```

`init` is also the right place to **declare derived per-LED arrays**: precompute distance-to-corner, rotated coords, anything spatial. It runs once per effect swap and never again, so cost is amortised.

### 5.3 The `FrameMap` (named coordinate frames)

Direct re-use of v1's `frames.py` (don't reinvent). Attribute access for ergonomics:

```python
ctx.frames.x              # per-LED [0, 1]  (left → right)
ctx.frames.y              # per-LED [0, 1]  (bottom → top)
ctx.frames.signed_x       # per-LED [-1, 1]
ctx.frames.signed_y       # per-LED [-1, 1]
ctx.frames.u_loop         # per-LED [0, 1]  clockwise around the rig
ctx.frames.u_loop_signed  # per-LED [-0.5, +0.5]
ctx.frames.radius         # per-LED [0, 1]  from rig centre
ctx.frames.angle          # per-LED [0, 1]  atan2(y,x)/2π wrapped
ctx.frames.side_top       # per-LED 1.0 / 0.0  (top row mask)
ctx.frames.side_bottom    # per-LED 1.0 / 0.0
ctx.frames.side_signed    # per-LED +1 top, -1 bottom
ctx.frames.axial_dist     # per-LED [0, 1]  |x|
ctx.frames.axial_signed   # per-LED [-1, 1]
ctx.frames.corner_dist    # per-LED [0, 1]
ctx.frames.strip_id       # per-LED int32
ctx.frames.chain_index    # per-LED [0, 1] within its own strip
```

These are the same arrays v1 already builds in `topology.derived`. Surface v2's `FrameMap` is a thin attribute-access wrapper around that dict — no recomputation.

### 5.4 The `EffectFrameContext` (passed to `render` every tick)

```python
@dataclass
class EffectFrameContext:
    t: float                          # effective time (master.speed-scaled, freezes on freeze)
    wall_t: float                     # raw wall clock, monotonic
    dt: float                         # seconds since previous render
    audio: AudioView                  # see below
    params: ParamView                 # current operator-controlled values, attribute access
    masters: MasterControls           # READ-ONLY snapshot (saturation/brightness/etc)
```

`AudioView`:

```python
class AudioView:
    low: float                                 # smoothed [0, 1]
    mid: float                                 # smoothed [0, 1]
    high: float                                # smoothed [0, 1]
    bands: dict[str, float]                    # {"low": 0.7, "mid": 0.2, "high": 0.4}
    beat: int                                  # number of new beats since last render (0 / 1 typically)
    beats_since_start: int                     # monotonic counter
    bpm: float                                 # current tempo, falls back to 120 when disconnected
    connected: bool
```

`ParamView` is a `SimpleNamespace`-like object whose attributes are the current values of every param the effect declared. Mutating it does nothing (read-only proxy); the operator UI is the source of truth.

### 5.5 What's deliberately **not** in the namespace

- File I/O, network, `subprocess`, anything that could touch the OS.
- Other effects' state (effects don't talk to each other — single active effect).
- The transport (effects compute pixels, they don't ship them).
- A way to mutate masters (read-only — same rule v1 has).

---

## 6. Param schema & the dynamic UI

The LLM's `params` array in the tool call is the contract for the auto-generated UI. The control vocabulary is small and stable:

| `control`     | additional fields                              | UI element                |
| ------------- | ---------------------------------------------- | ------------------------- |
| `slider`      | `min`, `max`, `step`, `default`, `unit?`       | range input + numeric box |
| `int_slider`  | `min`, `max`, `step?` (default 1), `default`   | integer range             |
| `color`       | `default` (hex)                                | colour picker             |
| `select`      | `options: [str, ...]`, `default`               | dropdown                  |
| `toggle`      | `default: bool`                                | switch                    |
| `palette`     | `default: str`                                 | named palette dropdown + preview swatch (re-uses v1's `NAMED_PALETTES` list) |

Common metadata on every param:
- `key`: snake_case identifier (must match attribute on `ctx.params`)
- `label`: human-friendly UI label
- `help?`: optional tooltip text

Validation happens on **two** sides:
- **LLM side** (compile-time): the schema is type-checked against a pydantic model; bad shapes are returned to the LLM as a structured error like the v1 compile errors.
- **Runtime side**: when the operator changes a value, it's clamped to the param's bounds before the next frame. `select` values must be in `options`.

**Live update path:**
```
slider drag  ──►  PATCH /v2/effect/params  {key, value}
                            │
                            ▼
                  ParamView.update(key, value)  (atomic on the asyncio loop)
                            │
                            ▼
                  next render() sees the new value
```

No recompile, no crossfade, no LLM round-trip. This is the headline UX win.

---

## 7. The system prompt for v2

Re-using the per-turn assembly pattern from `agent/system_prompt.py`. Sections, in order:

1. **INSTALL.** Topology summary (1800 LEDs, 4 strips, geometry), with a small ASCII rig diagram showing top-right / bottom-right / bottom-left / top-left labels.
2. **COORDINATE FRAMES.** Names + one-liners (re-use `FRAME_DESCRIPTIONS`). The most important section. Includes a worked example: "to address only the top row, mask with `ctx.frames.side_top.astype(bool)`."
3. **AUDIO.** Same content as today: device, band cutoffs, current values; how `low/mid/high/beat/bpm` are smoothed and scaled.
4. **MASTERS** (read-only).
5. **RENDER CONTRACT.** The `Effect` class signature, the two contexts, the return-shape rule, the no-import rule, the perf budget. ~30 lines.
6. **RUNTIME API.** A flat reference table of every helper in the namespace (§5.1) with a one-line signature each. ~25 lines.
7. **PARAM SCHEMA.** The control vocabulary table (§6). ~15 lines.
8. **EXAMPLE EFFECTS.** Three handwritten reference effects shipped in `surface_v2/examples/`:
    - **`pulse_mono.py`** — simplest possible: solid colour, brightness pulses on `audio.low`.
    - **`audio_radial.py`** — palette-mapped radius with audio-driven brightness; demonstrates `frames.radius` + `palette_lerp`.
    - **`twin_comets_with_sparkles.py`** — the one from §3.2 above, fully written out. Demonstrates state, side masks, particle deposit, audio modulation.
9. **ANTI-PATTERNS.** Concrete things the LLM gets wrong:
    - "Don't loop over `range(N)`. Vectorise."
    - "Don't allocate inside `render`. Pre-allocate `self.out` in `init`."
    - "Don't `import` anything — the namespace is given."
    - "Don't normalise audio yourself; `ctx.audio.low` is already in `[0, 1]`."
    - "Always return `(N, 3) float32` in `[0, 1]`. The loader rejects everything else."
    - "Don't read masters except for diagnostics — the operator owns those."
10. **CURRENT EFFECT.** Source of the active effect + its current param values. The LLM can choose to patch params (`update_params`) instead of regenerating.
11. **TOOLS.** `write_effect` and `update_params` schemas.

Token estimate: ~5–6k tokens of system prompt (less than v1, because we trade the primitive catalogue for three reference examples and one runtime-API table).

---

## 8. Tool call surface for the agent

Two tools, both server-validated.

### 8.1 `write_effect`

```jsonc
{
  "type": "function",
  "function": {
    "name": "write_effect",
    "description": "Replace the active LED effect with a new Python Effect class plus an operator UI param schema. Always emit the COMPLETE effect — never a diff.",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "code", "params"],
      "properties": {
        "name": { "type": "string", "pattern": "^[a-z][a-z0-9_]{0,40}$" },
        "summary": { "type": "string" },
        "code": { "type": "string", "description": "A single Python module defining exactly one Effect subclass." },
        "params": { "type": "array", "items": { "$ref": "#/$defs/Param" } }
      }
    }
  }
}
```

Server flow on receipt:
1. Validate the param schema (pydantic).
2. AST-scan + sandbox-compile the code.
3. Instantiate the Effect, call `init(ctx)` once.
4. Fence-test: call `render(ctx)` once with synthetic context (t=0, dt=1/60, zeroed audio, defaults). Check `(N, 3) float32 in [0, 1]`.
5. Persist `code` + `params` + `param_values` (the defaults) to `config/v2_effects/<name>.json`.
6. Crossfade the engine to the new effect (using master crossfade duration).
7. Tool result: `{ ok: true, name, params }` — or a structured error the LLM can read.

### 8.2 `update_params`

```jsonc
{
  "type": "function",
  "function": {
    "name": "update_params",
    "description": "Patch the active effect's parameter values without changing its code. Use this when the user's request is satisfiable by tuning existing knobs.",
    "parameters": {
      "type": "object",
      "required": ["values"],
      "properties": {
        "values": { "type": "object", "additionalProperties": true }
      }
    }
  }
}
```

Server flow: validate each key against the active effect's schema; clamp to bounds; apply atomically; broadcast updated values to all open UIs over the existing `/ws/state` channel.

### 8.3 Choosing between the two

Hard rule in the system prompt: *"If every change the user requested can be done by adjusting an existing param's default, call `update_params`. Otherwise call `write_effect` (a complete new effect). Never both in the same turn."*

This makes iteration cheap. "Make the top one orange instead of red" → one OSC packet, no Python compile, no crossfade flash.

---

## 9. Persistence

`config/v2_effects/<slug>.json` per saved effect:

```jsonc
{
  "name": "twin_comets_with_sparkle_trails",
  "summary": "...",
  "code": "<source>",
  "params": [ ... ],            // schema
  "param_values": { ... },      // current operator values
  "created_at": "2026-05-09T...",
  "updated_at": "2026-05-09T...",
  "source": "agent" | "user"    // hand-written examples land in examples/ instead
}
```

REST endpoints (mirroring v1 presets):

| method | path                         | purpose                                |
| ------ | ---------------------------- | -------------------------------------- |
| GET    | `/v2/effects`                | list saved effects                     |
| POST   | `/v2/effects/{name}/apply`   | crossfade to a saved effect            |
| POST   | `/v2/effects/{name}/save`    | save the active effect under `name`    |
| DELETE | `/v2/effects/{name}`         | remove from disk                       |
| GET    | `/v2/active`                 | current effect: name, code, params, values |
| PATCH  | `/v2/active/params`          | set one or more param values           |

---

## 10. Engine integration & the toggle

A single config field, fully back-compat:

```yaml
# config/config.dev.yaml
engine:
  surface_version: "v1"   # default — current behaviour, untouched
```

```yaml
# config/config.v2.yaml (or set on the Pi after testing)
engine:
  surface_version: "v2"
```

Wiring inside `engine.py`:

```python
class Engine:
    def __init__(self, cfg, topology, transport, masters=None):
        ...
        if cfg.engine.surface_version == "v2":
            from .surface_v2.runtime import V2Runtime
            self._v2 = V2Runtime(topology)
            self._render_path = self._render_v2
        else:
            self._v2 = None
            self._render_path = self._render_v1   # current code, unchanged
```

The render loop's body becomes:

```python
self.buffer.clear()
self._render_path(ctx)        # v1 → mixer.render; v2 → v2.render_into
if self.calibration is not None:
    self._apply_calibration(wall_t)
await self.transport.send_frame(self.buffer.to_uint8(self.gamma))
```

`V2Runtime` owns:
- The active `Effect` instance.
- The previous `Effect` instance during a crossfade.
- Wall-clock crossfade math (re-using the same alpha curve).
- Param store, audio smoother, rng seeding.
- Compile + fence-test pipeline (called from the `write_effect` handler).
- The post-render master output stage (saturation pull → brightness gain → clip), copied verbatim from `Mixer._apply_master_output`. The brightness master's adaptive headroom and the saturation pull are not v1-specific concepts — they're operator features and v2 must preserve them.

**v2 deliberately has no concept of "layer stack" / blend modes.** One effect at a time. If we ever want layering back, the LLM can build it inside one `Effect` (it's just numpy compositing). This keeps the prototype focused.

**v1 is untouched.** Every line under `src/ledctl/surface/`, every test under `tests/test_surface_*.py`, every preset under `config/presets/` keeps working when `surface_version: "v1"`.

---

## 11. UI integration

A new page, `/v2`, served alongside the existing `/` (which stays on v1). Flipping the config flag changes which page the operator opens; both work in dev simultaneously when the Pi is on v1 and the Mac is on v2.

The v2 UI has three regions:

1. **Live viz** (top): re-use the existing simulator canvas/WebSocket frame stream verbatim. No changes needed to `audio-meter.js` / `main-desktop.js` — the frame topic is the same.
2. **Dynamic param panel** (left or right rail): rendered from the active effect's param schema. One control per param. Every change debounces to ~16 ms then `PATCH`es. Values reset to defaults via a per-param click; "reset all" button per effect.
3. **Chat + active-code panel** (bottom or right): the existing chat UI on top, plus a collapsed view of the active effect's source (so the operator can read what the LLM wrote). A small "Save as…" / "Saved effects" library list above the chat.

Plus the existing master row stays exactly as it is — masters are common to both engines.

The dynamic panel rendering is small (one component per `control` type, ~150 lines of vanilla JS). No framework required; keep parity with the rest of the project's hand-rolled web UI style.

---

## 12. Crossfade between effects

The mixer's crossfade math is engine-agnostic:

```python
def _render_crossfade_v2(out, prev, curr, ctx, alpha):
    a = prev.render(ctx)
    b = curr.render(ctx)
    np.multiply(a, 1.0 - alpha, out=out)
    out += b * alpha
    np.clip(out, 0.0, 1.0, out=out)
```

`alpha` uses `ctx.wall_t` — same as v1, so freeze/speed don't slow the crossfade. Once `alpha >= 1.0` the previous Effect is dropped.

The new Effect's `init` runs at `write_effect` time (during the fence test), so by the time the crossfade starts, there's no first-frame stall.

---

## 13. Error handling

Three failure modes, each with a defined recovery path:

| failure                                           | what happens                                                                                       |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `write_effect` → SyntaxError / AST-disallowed     | tool result returns `{ok: false, error: "compile_failed", traceback: "..."}` — LLM self-corrects   |
| `write_effect` → fence test crashes (`init` or first `render`) | same, but with the specific exception. Active effect is unchanged.                       |
| live `render` raises after going active           | catch, log traceback, blank output for this frame. After 3 consecutive failures, swap back to the previous effect (or to a built-in "safe idle"). Surface the error in the chat for the next LLM turn. |
| live `render` returns wrong shape / dtype         | same as above. The shape check costs ~50 ns per frame.                                             |

All error tracebacks are surfaced to the LLM in the next turn's system prompt under a **`LAST EFFECT ERROR`** section, so the LLM can fix without the operator having to re-prompt.

---

## 14. Security notes

We're not building a public sandbox. Threat model:

- **Goal:** the LLM's typo `import os; os.system("rm -rf /")` cannot run.
- **Goal:** a runaway `while True: pass` cannot wedge the render loop.
- **Non-goal:** defending against a malicious operator who has already chat access.

Mitigations:

- **AST reject** of `import` and dunder access (§4.1).
- **Stripped builtins** (no `eval/exec/open/__import__/...`).
- **Render budget watchdog**: every `render` runs in the asyncio loop directly (we don't move to a thread — overhead would dominate). We measure wallclock and emit a warning + degrade FPS if the rolling p95 exceeds budget. We do *not* try to interrupt mid-render — numpy holds the GIL through C and a hard kill would corrupt state. The pragmatic answer is the budget warning + manual swap to the safe idle effect.
- **No `subprocess`, no `socket`** — they're not in the namespace and `import` is gone.

Good enough for the festival rig. If we ever serve this to untrusted users, swap `exec` for a real sandbox (subinterpreter / Pyodide-in-process / RestrictedPython). Out of scope for v2.

---

## 15. Folder layout

Strictly isolated; v1 imports nothing from v2 and vice-versa (except `topology` and `audio` which are shared infrastructure):

```
src/ledctl/
  surface/               # v1, untouched
  surface_v2/            # NEW
    __init__.py          — public API: V2Runtime, Effect, build_runtime_namespace
    base.py              — Effect base class, EffectInitContext, EffectFrameContext, AudioView, FrameMap, ParamView
    sandbox.py           — AST scan + safe exec + class extraction
    helpers.py           — hex_to_rgb, hsv_to_rgb, gauss, pulse, lerp, clip01, palette_lerp, named_palette, audio_smooth, ...
    runtime.py           — V2Runtime: holds active+previous effects, crossfade, param store, render-into-buffer, brightness/saturation post-stage
    schema.py            — pydantic models for tool call (write_effect, update_params) + Param
    persistence.py       — load/save effect JSONs from config/v2_effects/
    prompt.py            — build_system_prompt_v2(...): assembles the §7 prompt
    tool.py              — apply_write_effect(...), apply_update_params(...) — the tool handlers
    examples/
      pulse_mono.py
      audio_radial.py
      twin_comets_with_sparkles.py
      README.md           — these are the LLM's reference templates AND smoke tests
  api/
    server.py            — adds /v2/* routes when surface_version == "v2"
    agent.py             — branches on surface_version to pick the right tool/prompt
config/
  v2_effects/            — persisted LLM-generated effects live here
src/web/
  v2.html                — operator page for v2 mode
  lib/v2-app.js          — ~600 lines of vanilla JS: chat + dynamic param panel + viz reuse
tests/
  test_surface_v2_sandbox.py
  test_surface_v2_runtime.py
  test_surface_v2_examples.py    — load each example, fence-test, render 60 frames, snapshot RGB stats
```

---

## 16. Implementation plan (suggested phasing)

### Phase 0 — scaffolding (½ day)
- [ ] Create `surface_v2/` skeleton with empty modules + the toggle field (`engine.surface_version`).
- [ ] Wire engine branching so v1 still runs identically when flag is absent.
- [ ] Smoke test: tests pass, dev server still works on v1.

### Phase 1 — runtime + sandbox (1 day)
- [ ] `Effect` base class, `EffectInitContext`, `EffectFrameContext`, `FrameMap` (wraps `topology.derived`), `AudioView`, `ParamView`.
- [ ] `helpers.py` — full §5.1 surface, with tests for each helper.
- [ ] `sandbox.py` — AST scan, restricted builtins, `compile_effect()`. Tests: imports rejected, dunder access rejected, normal numpy code accepted.
- [ ] `runtime.py` — `V2Runtime.swap_to(EffectClass)`, `render_into(buffer, ctx)`, crossfade. Audio smoother per-effect. Master output stage (copied from mixer).

### Phase 2 — example effects + smoke harness (½ day)
- [ ] `examples/pulse_mono.py`, `audio_radial.py`, `twin_comets_with_sparkles.py`. These double as **acceptance tests** for the runtime API.
- [ ] `tests/test_surface_v2_examples.py`: load each example, instantiate against a synthetic 1800-LED topology, render 60 frames with synthetic audio, assert no exceptions + bounded RGB.

### Phase 3 — REST + persistence (½ day)
- [ ] `/v2/active` (GET), `/v2/active/params` (PATCH), `/v2/effects` (GET/POST/DELETE/apply).
- [ ] `persistence.py` — load on boot, save on `write_effect`.
- [ ] Default effect on boot: `pulse_mono` from the examples directory.

### Phase 4 — agent integration (1 day)
- [ ] `prompt.py` — assemble the §7 system prompt; reuse `topology` summary + audio summary helpers from v1.
- [ ] `tool.py` — `write_effect` and `update_params` handlers.
- [ ] `api/agent.py` — branch on `surface_version` to pick the v2 tool set + prompt.
- [ ] Iterate on the prompt with the real LLM until the user's flagship "twin comets" prompt one-shots cleanly. **This is the acceptance bar for Phase 4** — if it doesn't one-shot, the prompt is wrong.

### Phase 5 — operator UI (1 day)
- [ ] `src/web/v2.html` + `lib/v2-app.js`: dynamic param panel (slider/color/select/toggle/palette renderers), chat, active-code viewer, saved-effects list.
- [ ] Reuse simulator canvas + WebSocket frame stream verbatim.
- [ ] Wire the master row.

### Phase 6 — polish (rolling)
- [ ] Render budget watchdog + p95 readout in UI.
- [ ] "Pin" / "Star" effects for quick-recall.
- [ ] LLM-side: teach it to prefer `update_params` when possible (anti-pattern in the prompt: "regenerating code when only a default changed").
- [ ] Move v2 default effect to Pi config when stable; keep flag.

**Total: ~3.5–4.5 dev days to a usable prototype the user can drive on the rig.**

---

## 17. Open questions (worth deciding before code)

1. **Single-effect vs. layer stack.** v2 starts single-effect for prototype clarity. Do we ever want stacking back? My take: **no** — the LLM is good at writing a unified effect; layer stacks were a v1 affordance because primitives were limited.

2. **Effects calling each other.** Could we let the LLM `apply_named_effect("pulse_mono")` from inside its own `render`? Tempting (sub-effects), but breaks the "one file, one Effect" mental model. Skip for v2. If the user wants composition, the LLM writes both pieces in one effect.

3. **Crossfade duration.** Use the existing master crossfade slider (already operator-owned). The LLM never picks the duration. Same v1 contract.

4. **Audio smoothing inside effects.** Provide `audio_smooth(ctx.audio, "low", halflife_s=0.1)` so the LLM can locally smooth without touching upstream config? **Yes** — give it the helper, but the prompt guides it to use raw bands by default (the upstream is already auto-scaled).

5. **Hot-reload from disk.** Should we watch `config/v2_effects/*.json` and auto-apply on change? Useful for developer tinkering on the Pi. **Yes** but behind a config flag, off by default.

6. **`update_params` vs. `update_param`.** The schema lets the LLM patch many at once — good (single tool call for "make it warmer and slower"). Keep plural.

7. **Error pulse vs. blackout on render crash.** I prefer **error pulse** (1 Hz dim red breathing) — the operator sees something is wrong; blackout looks like a power fail. Configurable.

8. **Token budget for `code`.** The flagship `twin_comets_with_sparkles` is ~50 lines; the LLM will be tempted to write more. Cap at 6 KB of code per effect (~150 lines), reject above with a structured error to keep the agent disciplined.

9. **Naming.** `surface_v2` is fine for the prototype folder; rename later (`effects/`, `vj/`, etc.) once we know the right name.

10. **Mobile UI.** Out of scope for v2. The dynamic param panel is desktop-first; a phone-friendly version comes after Phase 7 of the master roadmap.

---

## 18. The "north star" worked example

To make sure the design is sound, walk the user's flagship prompt end-to-end:

> *"Create two comets going from left to right, the top one is leading the bottom one by 1/6th of the total distance. After a comet has passed, it leaves behind sparkles flickering in its color that gradually darken and fade out. The top comet is red, the bottom one deep blue. Both of them are pulsating in brightness between 0.6 and 1.0 max brightness based on the 'low' audio signal."*

### Operator action
Types into the v2 chat. Hits send.

### LLM round-trip
- Reads system prompt: knows about `frames.side_top`, `frames.x`, `audio.low`, the `Effect` contract, the param schema, the worked `twin_comets_with_sparkles` example.
- Emits one `write_effect` tool call with code ≈ §3.2 above + a six-param schema.

### Server
- AST scan: clean.
- Sandbox compile: ok.
- `init(synthetic_ctx)` runs in <1 ms (precompute masks).
- Fence-test `render(synthetic_ctx)`: returns `(1800, 3) float32`, all in `[0, 1]`. Pass.
- Save to `config/v2_effects/twin_comets_with_sparkle_trails.json`.
- Crossfade engine to new effect over operator's master crossfade duration (e.g. 1 s).
- Tool result `{ok: true, name, params}`.

### UI
- Param panel updates: 6 controls appear (two colour pickers, four sliders, one dropdown).
- Chat shows the LLM's `summary` + a "View source" disclosure.
- LEDs show the new effect, audio-reactive.

### Iteration
Operator drags `lead_offset` from 0.166 → 0.083 → comets get tighter. No LLM call.
Operator: "make the leader a brighter pink" → LLM emits `update_params({"leader_color": "#ff70a0"})`. Single round-trip, no compile.
Operator: "now make the trail particles spiral around the rig instead of stay on side" → fundamental change. LLM emits `write_effect` with new code (using `frames.u_loop` + `wrap_dist`).

This is the UX we're building toward.

---

## 19. What v2 deliberately gives up vs. v1

In the spirit of "no half-finished implementations" — the things v2 does **not** carry over from v1, and why that's fine:

- **Layer stack + blend modes.** One effect at a time. Compose inside the effect.
- **Type-checked compositional graph.** Replaced by "the loader runs the code; if it crashes, you see the traceback." Pythonic, less safe, much more expressive.
- **Cross-effect palette / scalar reuse.** No shared compile-time vocabulary. Each effect re-derives what it needs from `frames` + `helpers`.
- **`primitives_json` REST catalogue.** v2 doesn't have primitives. The "catalogue" is the system prompt + the `examples/` directory.
- **Persisted layer-stack presets.** Replaced by persisted whole-effect JSONs. v1 presets stay readable in v1 mode.

Things v2 keeps verbatim:

- Topology + named coordinate frames.
- Audio bridge + AudioState semantics.
- Master controls (brightness/saturation/speed/freeze/audio_reactivity), including adaptive headroom.
- Crossfade math and the operator's crossfade slider.
- Calibration overrides.
- Transport layer.
- DDP-pause / blackout semantics.

The split is clean: v2 changes **what gets rendered**, not **how it's shipped**.

---

## 20. Decision summary

| question                                  | answer                                                          |
| ----------------------------------------- | --------------------------------------------------------------- |
| Substrate for LLM-authored code           | Python with sandboxed `exec`, AST-scanned, restricted builtins  |
| Effect shape                              | One `Effect` subclass per file: `init(ctx)` + `render(ctx) → (N,3) float32` |
| Spatial vocabulary                        | Re-use v1's named frames (`x / u_loop / radius / side_top / ...`) via `ctx.frames.*` |
| Audio vocabulary                          | Same as v1 (`low / mid / high / beat / bpm / connected`) via `ctx.audio.*` |
| Param schema                              | Six control types (`slider / int_slider / color / select / toggle / palette`), declared per-effect by the LLM |
| Operator UI                               | Dynamic panel auto-rendered from the schema; PATCH-on-change, no recompile |
| Iteration                                 | Two LLM tools: `write_effect` (full new effect), `update_params` (patch defaults). Prefer the latter when possible. |
| State                                     | Per-effect via `self.*`. No globals, no inter-effect state.     |
| Crossfade & error recovery                | Reuse v1 mixer crossfade; render errors → 1 s error pulse, then revert + report to LLM |
| v1 compatibility                          | Single config flag `engine.surface_version: v1 | v2`. v1 untouched, all current tests/presets keep working. |
| Estimated build time                      | ~3.5–4.5 dev days to a usable prototype                          |

The bet: **a 600-line runtime + a great system prompt outperforms a 2,500-line typed primitive graph**, because the LLM is the right tool to author the long tail of effects, and we should give it room to actually do that.
