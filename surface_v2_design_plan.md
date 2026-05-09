# Surface v2 — LLM-as-Author Design Plan

> **Goal.** Replace the typed `{kind, params}` primitive graph with **LLM-authored Python effects** that have direct access to a curated runtime API, plus an **auto-generated, ad-hoc operator control panel** the UI renders dynamically. Operator UI runs in two modes (Design / Live) so the operator can prototype with the LLM without disturbing what's actually playing on the LEDs. **Full replacement** — the existing `src/ledctl/surface/` is removed in this refactor (current work is committed; no need to maintain two pathways).

---

## 1. Executive summary

The current surface (`src/ledctl/surface/`) is a clean, well-typed compositional graph: ~30 primitive `kind`s, polymorphic combinators, strict per-path compile errors. It is excellent for **what it can express**. It is also a **glass ceiling**: any new spatial idea, particle behaviour, or orchestration the operator can describe in one sentence requires either (a) a new primitive class (a code change) or (b) a baroque tree the LLM cannot reliably author.

Example the user gave that v1 categorically cannot do:

> *"Create two comets going from left to right, the top one is leading the bottom one by 1/6th of the total distance. After a comet has passed, it leaves behind sparkles flickering in its color that gradually darken and fade out. The top comet is red, the bottom one deep blue. Both of them are pulsating in brightness between 0.6 and 1.0 max brightness based on the 'low' audio signal."*

This needs: two stateful particle objects, a side-aware spatial filter (`side_top` / `side_bottom`), a coupled "deposit sparkle on pass" mechanism with per-pixel fade, and audio-modulated brightness. None of that composes from existing primitives without authoring 3–4 new ones.

**The shift.** Stop curating a vocabulary. Give the LLM:

1. A clear **spatial model** (positions, named frames, strip layout, ASCII diagram of the rig).
2. A clear **audio model** (`low / mid / high / beat / bpm`, **already smoothed and auto-scaled upstream** — the LLM uses raw values).
3. A **render contract**: a Python `Effect` subclass with `init(ctx)` and `render(ctx) → (N, 3) float32` in `[0, 1]`.
4. A small, ergonomic **helper namespace** (numpy, palette/HSV/gauss/pulse helpers) and concrete documentation of every value in `ctx.*` (dtype, shape, range, example values).
5. A **param schema** the LLM emits *alongside* the code, which the operator UI renders as a dynamic mini control panel (sliders, color pickers, dropdowns, toggles).

The LLM writes short, vectorised numpy code (one Effect file = ~30–120 lines for most things). The UI gets dynamic controls per effect. The user can hand-tune via sliders without re-prompting.

Plus: the operator UI splits into **Design mode** (chat + preview-only render) and **Live mode** (sliders + render goes to LEDs). Means the LLM can stumble through three drafts of an effect without the dance floor noticing.

**Cost** of this freedom: a Python sandbox tuned for the Pi's hot path, a livecoding error path, and trusting the LLM to write fast vector code. **Benefit**: arbitrary expressiveness, instant prototyping by description, and the v1 layer of "vocabulary work" disappears.

---

## 2. Why v1 hits a wall (concrete failure modes)

Going through `surface/primitives/` and the user's example prompt:

- **Coupled stateful systems.** The v1 `comet` primitive owns its trail; nothing can attach a *second* effect to its passage (sparkles deposited at the head). Each new coupling = new primitive.
- **Per-instance asymmetric configuration.** "Top comet red, bottom comet blue, top leads by 1/6" requires two `comet` layer instances + a manual `side_top`/`side_bottom` spatial mask. v1 has no way to make one comet *aware* of another.
- **Compositional explosion.** What looks like one idea ("particles that leave fading sparkle trails") becomes a 7-deep node tree. The LLM authors it inconsistently across calls; small typos compile but produce visually wrong results that are hard to debug from a chat turn.
- **Param surface is fixed.** The operator can only nudge the 5 master controls. The LLM cannot expose "lead offset between comets" as a UI dial — it can only re-emit the whole stack with a new constant.
- **Token economy.** `surface/docs.py` regenerates the full primitive catalogue every system prompt. We're at ~7–8k tokens of catalogue today. Each new primitive grows that bill.

The pattern: every time the operator describes something genuinely new, **we** end up writing code, not the LLM. v2 inverts this.

---

## 3. The v2 contract

### 3.1 What the LLM emits per turn

The LLM has **one tool**: `write_effect`. Every turn, it emits the *complete* new effect — code + param schema. There is deliberately no `update_params` / "patch a default" path: a chat turn from the user is treated as a request for *script-level* change. If the user just wanted a different colour or a slower speed, they'd drag the slider themselves — they're sitting in front of the operator UI with every knob already exposed.

The tool call shape:

```jsonc
{
  "name": "twin_comets_with_sparkle_trails",
  "summary": "Two comets sweep left→right, top leads bottom by 1/6. Trails leave fading sparkles in the comet's colour.",
  "code": "<python source as a single string>",
  "params": [
    {
      "key": "leader_color", "label": "Top comet colour",
      "control": "color", "default": "#ff2020"
    },
    {
      "key": "follower_color", "label": "Bottom comet colour",
      "control": "color", "default": "#1040ff"
    },
    {
      "key": "lead_offset", "label": "Top–bottom lead",
      "control": "slider",
      "min": 0.0, "max": 0.5, "step": 0.01,
      "default": 0.1667
    },
    {
      "key": "speed", "label": "Comet speed (cycles/s)",
      "control": "slider",
      "min": 0.05, "max": 2.0, "step": 0.01,
      "default": 0.4
    },
    {
      "key": "sparkle_decay", "label": "Sparkle fade time (s)",
      "control": "slider",
      "min": 0.1, "max": 5.0, "step": 0.05,
      "default": 1.5
    },
    {
      "key": "audio_band", "label": "Brightness driver",
      "control": "select",
      "options": ["low", "mid", "high"],
      "default": "low"
    }
  ]
}
```

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
        self.x = ctx.frames.x                         # (N,) float32
        self.top = ctx.frames.side_top.astype(bool)   # (N,) bool
        self.bottom = ctx.frames.side_bottom.astype(bool)

        # ---- per-LED state buffers (preallocate; never alloc in render) ----
        self.sparkle_age = np.full(ctx.n, np.inf, dtype=np.float32)
        self.sparkle_rgb = np.zeros((ctx.n, 3), dtype=np.float32)

        # ---- comet state (scalars) ----
        self.head_top = 0.0       # x in [0, 1]
        self.head_bot = 0.0
        self.last_top_idx = -1
        self.last_bot_idx = -1

        # ---- output buffer (re-used every frame, no per-frame alloc) ----
        self.out = np.zeros((ctx.n, 3), dtype=np.float32)
        self._fade = np.empty(ctx.n, dtype=np.float32)   # scratch

    def render(self, ctx):
        p = ctx.params
        dt = ctx.dt

        # ---- advance heads (wrap at 1.0) ----
        self.head_top = (self.head_top + p.speed * dt) % 1.0
        self.head_bot = (self.head_top - p.lead_offset) % 1.0

        # ---- audio-driven brightness in [0.6, 1.0] ----
        # ctx.audio.bands is a dict; values are pre-smoothed in [0, 1].
        amp = 0.6 + 0.4 * ctx.audio.bands[p.audio_band]

        # ---- decay sparkles (exp fade with half-life = sparkle_decay) ----
        np.add(self.sparkle_age, dt, out=self.sparkle_age)
        np.divide(self.sparkle_age, max(p.sparkle_decay, 1e-3), out=self._fade)
        np.exp(np.negative(self._fade, out=self._fade), out=self._fade)
        np.multiply(self.sparkle_rgb, self._fade[:, None], out=self.out)

        # ---- draw comet heads as gaussians along x, masked top/bottom ----
        self._stamp(self.head_top, hex_to_rgb(p.leader_color), self.top, amp)
        self._stamp(self.head_bot, hex_to_rgb(p.follower_color), self.bottom, amp)

        # ---- deposit sparkles at the head's nearest LED on each side ----
        self._deposit(self.head_top, hex_to_rgb(p.leader_color), self.top, "top")
        self._deposit(self.head_bot, hex_to_rgb(p.follower_color), self.bottom, "bot")

        return self.out                             # (N, 3) float32 in [0, 1]

    def _stamp(self, head_x, color, mask, amp):
        d = np.abs(self.x - head_x)
        d = np.minimum(d, 1.0 - d)                  # wrap distance
        g = np.exp(-(d * d) * (1 / (2 * 0.03 * 0.03))) * amp
        self.out[mask] += g[mask, None] * color

    def _deposit(self, head_x, color, mask, side):
        idxs = np.where(mask)[0]
        i = idxs[np.argmin(np.abs(self.x[idxs] - head_x))]
        prev = self.last_top_idx if side == "top" else self.last_bot_idx
        if i != prev:
            j = idxs[(np.searchsorted(self.x[idxs], head_x) + rng.integers(-2, 3)) % idxs.size]
            self.sparkle_age[j] = 0.0
            self.sparkle_rgb[j] = color
            if side == "top":
                self.last_top_idx = i
            else:
                self.last_bot_idx = i
```

That's the whole effect — ~50 lines of LLM-written Python. Note that nothing in the helper namespace was a special-cased primitive; everything came from numpy + a couple of helpers (`hex_to_rgb`, the named coordinate frames `ctx.frames.x` / `side_top` / `side_bottom`, the seeded `rng`). **No new primitive class was needed.**

### 3.3 Crucial properties

- **One file = one effect.** No multi-class compositions; no "primitive registration." Discoverable, swappable, persistable.
- **`init` runs once per swap.** All per-LED precompute, masks, RNGs, state buffers live there.
- **`render` is hot-path.** Vectorised numpy. Returns `(N, 3) float32 in [0, 1]`. Hard rule: no allocation in the hot path — preallocate everything in `init`, use `out=` kwargs.
- **Buffer ownership: the runtime never mutates the effect's returned array.** The runtime copies the returned buffer once into a runtime-owned `master_buf` and applies the master output stage there. Cost is one `(1800, 3) float32` memcpy (~22 KB, ~3 µs on the Pi) — negligible vs. per-frame budget, and it removes a whole class of "why does my effect drift over time?" bugs. Effects can therefore freely return `self.out` and trust that next frame's `render` sees their state untouched.
- **Params are first-class and live.** Sliders update `ctx.params.<key>` between frames, no recompile. The LLM never sees user param changes — only the operator does. Writes to `ctx.params` from inside the effect raise (not silent no-op) — silent failures are the opposite of helpful when the LLM is debugging from tracebacks.
- **State is `self.*`.** No globals, no persistence between effect swaps (deliberately — "blank slate" is the model the LLM understands).
- **`render` may raise.** We test-run the code first (init + ~30 synthetic render frames — see §9.1 fence test), catch any errors, log the traceback, and ship the error back to the LLM for the next try. LLM gets 2 consecutive retries by default (configurable in `config.yaml`).
- **`rng` seeding policy.** `rng = np.random.default_rng(seed)` where `seed = stable_hash(effect_name)`. Deterministic across reloads (so "twin_comets" always has the same sparkle texture); unique per effect. If the operator wants a re-roll, the LLM can expose a `seed` int_slider param and incorporate it.

---

## 4. Execution model — sandbox tuned for the Pi

### 4.1 Substrate decision

Python is the right substrate:

- The render loop is already Python + numpy in-process; staying in-process avoids per-frame IPC.
- Numpy is mandatory: at 1800 LEDs × 60 fps on a Pi 4/5, pure-Python `for` loops are ~10–30 ms per frame and miss budget on the first try; vectorised numpy hits sub-millisecond.
- Single trusted festival deployment — security threat model is "stop the LLM accidentally calling `os.system`," not "defend against malware." See §13.

### 4.2 Compile pipeline (one-time per effect)

```python
# surface/sandbox.py
import builtins, ast, types

_SAFE_BUILTINS = {n: getattr(builtins, n) for n in (
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
    "int", "isinstance", "len", "list", "map", "max", "min", "pow", "range",
    "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
    "zip", "type",
)}
# explicitly absent: __import__, open, exec, eval, compile, input,
# getattr/setattr/delattr, globals, locals, vars, breakpoint, exit, quit, print

_FORBIDDEN_NODES = (ast.Import, ast.ImportFrom)

def compile_effect(source: str, name: str) -> type[Effect]:
    if len(source) > MAX_SOURCE_BYTES:        # default 8 KB
        raise EffectCompileError(f"source too long ({len(source)} > {MAX_SOURCE_BYTES})")
    tree = ast.parse(source, filename=f"<llm:{name}>")
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            raise EffectCompileError(
                "imports are forbidden — the runtime API is already in the global namespace")
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

The AST scan and `compile()` happen **once**, at `write_effect` time. After that, the resulting code object runs at full CPython bytecode speed. There is **no per-frame sandbox overhead**: the restricted builtins live in the module's `__builtins__` dict, lookups are identical to a normal module, and `render(ctx)` is just a method call.

### 4.3 Performance model (the part that matters on the Pi)

The Pi 4 (and Pi 5) is the deployment target. At 1800 LEDs × 60 fps the budget is **16.6 ms/frame**, of which ~3 ms is reserved for DDP encode/transmit and another ~2 ms for mixer post-stage + simulator broadcast. **Per-effect render budget: ~5 ms**, 8 ms during a crossfade (two effects render concurrently).

What actually hits the budget on the Pi, in priority order:

1. **Loops in Python over `N=1800`** — death. Pure-Python 1800-iter loop is 1–3 ms by itself on a Pi 4. Make this fail loudly: the system prompt forbids it, the example effects model the right pattern, and we surface render-time p95 in the UI so violations are visible. We do not try to detect loops statically — the LLM follows examples reliably here.
2. **Per-frame allocation.** `np.zeros((N, 3))` triggers GC pressure (page-fault on first touch on the Pi). Rule: every per-LED array used by `render` is preallocated in `init`. The `Effect` base class provides `self.out` (the canonical `(N, 3) float32` output buffer) preallocated for the LLM, plus `self.scratch1d / scratch2d` if helpful.
3. **dtype slop.** Default numpy is float64; Pi's NEON SIMD prefers float32. Mixed-dtype ops promote to float64 silently. Mitigation: every public array on `ctx.frames`, `ctx.pos`, audio scalars, palette LUTs is **float32**. Helpers (`hex_to_rgb`, `palette_lerp`) return float32. The system prompt has a one-liner: *"everything is float32; mixing in fp64 literals is fine but if you build large temporaries, keep them fp32."*
4. **`out=` in-place ops.** `a + b * c` allocates twice; `np.multiply(b, c, out=tmp); np.add(a, tmp, out=tmp)` allocates zero. The flagship example demonstrates this pattern; the prompt teaches it as the second rule.
5. **Avoid Python-side fancy indexing where vectorised math works.** `arr[mask] += g[mask] * c` is fine (numpy handles it in C); but per-LED if-branches in a Python loop are not.

### 4.4 Watchdog (no hard kill — soft degrade)

Every render is timed with `perf_counter`. We track:

- Per-effect rolling **mean / p95 / p99** over the last 1 s.
- **Fast trip:** if p95 exceeds the budget for **0.5 s** straight (~30 frames), the runtime:
  1. Logs a warning with the offending effect name + p95.
  2. Surfaces a red badge in the operator UI ("`render p95 = 14 ms — slow`").
  3. Swaps that slot to the **safe-idle effect** (`pulse_mono` at 1 Hz, ~0.3 ms) and posts the slow effect's source + perf stats to the chat for the LLM to optimise.

The 0.5 s window is deliberately tight — 2 s of stutter is 120 frames of jank visible to the dance floor, and the festival rule (§3 of `user_design_spec.md`) is "never break the show." Per-effect FPS halving was considered and dropped: it adds a per-effect scheduling state machine to the single asyncio render loop and the binary "swap to idle" outcome is what we actually want during a set.

We do **not** try to interrupt mid-`render` — numpy holds the GIL through C and a hard-kill mid-call would corrupt the effect's `self.*` state. The watchdog is the pragmatic answer; the prompt's perf rules + examples are the prevention.

### 4.5 Why not a separate process / thread / interpreter?

- **Subprocess**: shipping `(N, 3) float32` over a pipe is ~22 KB / frame at 60 fps = 1.3 MB/s plus pickle overhead. On a Pi this is tens of ms of latency per frame for IPC alone. No.
- **Thread**: the GIL means we'd serialise anyway. Numpy releases the GIL inside C, but the asyncio loop needs the result *before* it can encode and ship it; gain is zero, complexity is non-zero.
- **Subinterpreter (PEP 684)**: still unstable in 3.11, no numpy-compatible model in 3.12. Revisit when 3.13's GIL story settles.
- **Direct in-process `exec`** is the right answer. Sandbox at compile time, run at full speed at frame time.

---

## 5. The runtime API the LLM is given

The namespace `build_runtime_namespace()` populates is the **entire** surface area the LLM author sees. Keep it small, useful, and stable.

### 5.1 Always available (top-level names)

| name             | type             | purpose                                                    |
| ---------------- | ---------------- | ---------------------------------------------------------- |
| `np`             | module           | numpy. The workhorse.                                      |
| `Effect`         | class            | The base class to subclass. Provides `self.out` preallocated as `(N, 3) float32`. |
| `hex_to_rgb`     | `(str) → (3,) float32` | `'#ff8000'` → `np.array([1, 0.5, 0], float32)`. Cached.   |
| `hsv_to_rgb`     | `(h, s, v) → (3,) float32` or vectorised over arrays | colour conversion. |
| `lerp`           | `(a, b, t)`      | `a*(1-t) + b*t`, broadcasts; allocates only if `out` not given. |
| `clip01`         | `(x, out=None)`  | `np.clip(x, 0, 1, out=out)`.                               |
| `gauss`          | `(x, sigma, out=None)` | normalised gaussian profile (peak = 1).               |
| `pulse`          | `(x, width)`     | cosine bump in `[-width, +width]`, peak = 1, else 0.       |
| `tri`            | `(x)`            | triangle wave on [0, 1].                                   |
| `wrap_dist`      | `(a, b, period=1.0)` | shortest signed distance with wrap (great for `u_loop`). |
| `palette_lerp`   | `(stops, t)`     | sample a multi-stop palette at scalar/array t. Returns float32. |
| `named_palette`  | `(name) → (LUT_SIZE, 3) float32` | `'fire'`, `'rainbow'`, `'ocean'`, `'sunset'`, `'warm'`, `'ice'`, `'white'`, `'black'`. |
| `rng`            | `np.random.Generator` | seeded per-effect, deterministic across reloads.      |
| `log`            | logger           | `log.warning(...)` — never `print` (which is excluded from builtins). |

**Constants:** `PI`, `TAU`, `LUT_SIZE` (256).

Anything else the LLM wants — `sin`, `cos`, `exp`, `fft` — is `np.sin`, `np.cos`, `np.exp`, `np.fft`. We don't re-export numpy primitives; the LLM knows numpy.

**Removed vs. earlier draft:** no `audio_smooth` helper. Audio is already smoothed and auto-scaled upstream by the audio-server (`Realtime_PyAudio_FFT`). The LLM is told to use raw `ctx.audio.low / mid / high` directly.

### 5.2 The `EffectInitContext` (passed to `init`)

```python
@dataclass(frozen=True)
class EffectInitContext:
    n: int                                 # pixel count (e.g. 1800)
    pos: np.ndarray                        # (N, 3) float32 in [-1, 1]; +x stage-right, +y up, +z toward audience
    frames: FrameMap                       # named per-LED scalars (see §5.3)
    strips: list[StripInfo]                # per-strip metadata: id, pixel_count, geometry.start/.end
    rig: RigInfo                           # bbox_min, bbox_max, target_fps, span_x_m, span_y_m
```

`init` is also the right place to **declare derived per-LED arrays**: precompute distance-to-corner, rotated coords, anything spatial. It runs once per effect swap and never again, so cost is amortised across the lifetime of the effect.

### 5.3 The `FrameMap` (named coordinate frames)

Direct re-use of the existing `frames.py` content. Attribute access for ergonomics:

```python
ctx.frames.x              # (N,) float32 in [0, 1]   — left → right (stage-x)
ctx.frames.y              # (N,) float32 in [0, 1]   — bottom → top
ctx.frames.z              # (N,) float32 in [0, 1]   — back → audience
ctx.frames.signed_x       # (N,) float32 in [-1, 1]
ctx.frames.signed_y       # (N,) float32 in [-1, 1]
ctx.frames.u_loop         # (N,) float32 in [0, 1]   — clockwise around the rig from top-centre
ctx.frames.u_loop_signed  # (N,) float32 in [-0.5, +0.5]
ctx.frames.radius         # (N,) float32 in [0, 1]   — from rig centre
ctx.frames.angle          # (N,) float32 in [0, 1]   — atan2(y,x)/2π wrapped
ctx.frames.side_top       # (N,) float32 ∈ {0, 1}    — 1 on top row
ctx.frames.side_bottom    # (N,) float32 ∈ {0, 1}    — 1 on bottom row
ctx.frames.side_signed    # (N,) float32 ∈ {-1, +1}  — top = +1, bottom = -1
ctx.frames.axial_dist     # (N,) float32 in [0, 1]   — |x|, distance from centre column
ctx.frames.axial_signed   # (N,) float32 in [-1, 1]
ctx.frames.corner_dist    # (N,) float32 in [0, 1]   — to nearest corner
ctx.frames.strip_id       # (N,) int32               — 0..3 for the 4-strip rig
ctx.frames.chain_index    # (N,) float32 in [0, 1]   — local index along this strip from controller end
```

These arrays are the SAME object across renders — the LLM may take views and slices safely; mutation is forbidden by convention (the prompt says so; we do not write-protect).

### 5.4 The `EffectFrameContext` (passed to `render` every tick)

```python
@dataclass
class EffectFrameContext:
    t: float                          # effective time (master.speed-scaled, freezes on freeze)
    wall_t: float                     # raw wall clock, monotonic
    dt: float                         # seconds since previous render (~0.0167 at 60 fps)
    audio: AudioView
    params: ParamView                 # current operator-controlled values, attribute access
    masters: MastersView              # READ-ONLY snapshot (saturation/brightness/...). Diagnostic only.
```

**`AudioView`** — every value is **already smoothed and auto-scaled upstream by the audio-server**, then **pre-multiplied by `masters.audio_reactivity`** before reaching the effect. The LLM treats them as ready-to-use, doesn't see the master, and doesn't need to apply it. This keeps the operator's "audio reactivity" master a global, single-knob attenuator that works across every effect ever generated, without each effect having to opt in:

```python
class AudioView:
    low: float          # smoothed band energy in [0, 1] — typical range during a track: 0.1 – 0.9
                        # quiet room: 0.0 – 0.1; bass kick at peak: ~0.7 – 1.0
                        # cutoffs published live in /audio/meta (typical: 30 – 250 Hz)
    mid: float          # smoothed band energy in [0, 1] — typical range: 0.05 – 0.7
                        # carries vocals, snare body, melody  (typical 250 – 2000 Hz)
    high: float         # smoothed band energy in [0, 1] — typical range: 0.05 – 0.6
                        # carries hats, snare crack, sibilance (typical 2 – 8 kHz)
    bands: dict[str, float]      # {"low": <low>, "mid": <mid>, "high": <high>}
                                 # convenience for `ctx.audio.bands[ctx.params.audio_band]`
    beat: int           # number of new beats since last render (0 most frames; 1 on a kick;
                        # rarely 2 if two onsets fired in the same frame interval)
                        # use `ctx.audio.beat > 0` as a one-shot trigger
    beats_since_start: int   # monotonic onset counter
    bpm: float          # current tempo, falls back to 120.0 when disconnected
    connected: bool     # False when audio-server is silent / down. low/mid/high are 0.0 in this case
```

**`ParamView`** is a `SimpleNamespace`-like object whose attributes are the current values of every param the effect declared. **Writes raise `TypeError`** (the operator UI is the source of truth — silent no-ops would just hide LLM bugs). If an effect needs a clamped or derived value, compute it locally each frame.

### 5.5 What's deliberately **not** in the namespace

- File I/O, network, `subprocess`, anything that could touch the OS.
- Other effects' state (effects don't talk to each other — single active effect).
- The transport (effects compute pixels, they don't ship them).
- A way to mutate masters or params (read-only — operator-owned).
- `print` (use `log.info / log.warning`).

---

## 6. Param schema & the dynamic UI controls

The LLM's `params` array in the tool call is the contract for the auto-generated UI. The control vocabulary is small and stable:

| `control`     | additional fields                              | UI element                |
| ------------- | ---------------------------------------------- | ------------------------- |
| `slider`      | `min`, `max`, `step`, `default`, `unit?`       | range input + numeric box |
| `int_slider`  | `min`, `max`, `step?` (default 1), `default`   | integer range             |
| `color`       | `default` (hex)                                | colour picker             |
| `select`      | `options: [str, ...]`, `default`               | dropdown                  |
| `toggle`      | `default: bool`                                | switch                    |
| `palette`     | `default: str` (one of `named_palette` keys)   | named palette dropdown + preview swatch |

Common metadata on every param:
- `key`: snake_case identifier (must match attribute on `ctx.params`)
- `label`: human-friendly UI label
- `help?`: optional tooltip text

Validation happens on **two** sides:
- **LLM side** (compile-time): the schema is type-checked against a pydantic model; bad shapes are returned to the LLM as a structured error (similar to v1 compile errors).
- **Runtime side**: when the operator changes a value, it's clamped to the param's bounds before the next frame. `select` values must be in `options`.

**Live update path:**
```
slider drag  ──►  PATCH /effect/params  {key, value}
                            │
                            ▼
                  ParamStore.update(key, value)  (atomic on the asyncio loop)
                            │
                            ▼
                  next render() sees the new value
```

No recompile, no crossfade, no LLM round-trip. This is the headline UX win.

Soft cap: ≤8 params per effect. The LLM is taught to merge related knobs and avoid over-parameterising.

### 6.1 Param carry-forward across regenerations

When the LLM emits a new `write_effect` (replacing an effect the operator has been tuning via sliders), we **auto-merge by `key`**: for every param in the new schema whose `key` matches a param in the previous effect AND whose declared bounds/type accept the previous value, the previous value becomes the new effect's initial value (overriding the LLM's `default`). The LLM is told this in the system prompt — so:

- For knobs that are conceptually unchanged (`leader_color`, `speed`), reuse the same `key` → the operator's tweak survives the regeneration automatically.
- For knobs being renamed or re-bounded ("I'm renaming `comet_speed` → `sweep_speed` to match the new metaphor"), the LLM picks a new `key` and the slider resets to its declared default. This is correct: the meaning changed.
- For knobs being dropped or added, no merge happens (no key match).

This way the common case ("regenerate but keep my colour and lead-offset") is mechanical and robust; the uncommon case (renamed/restructured params) is still the LLM's call. Falls back gracefully and saves prompt tokens vs. having the LLM hand-merge every time.

---

## 7. Two-mode operator UI: Design vs. Live

This is a first-class architectural concept, not a UI affordance bolted on later. The two modes affect what gets rendered, where it gets sent, and what controls the operator sees.

### 7.1 Mode semantics

| mode         | sim viz shows               | DDP (physical LEDs) shows  | controls available                              |
| ------------ | --------------------------- | -------------------------- | ----------------------------------------------- |
| **Design**   | the *preview* effect (LLM scratchpad) | the *live* effect (last promoted) | chat + LLM tools + preview's param panel + "Promote to live" button |
| **Live**     | mirrors DDP (the live effect)  | the live effect             | masters + live effect's param panel + "Switch to design" |

In **Design mode** the operator can prompt the LLM, watch it iterate, accept and reject drafts, fiddle with sliders on the *preview* effect — all without disturbing the dance floor, because DDP keeps shipping the last-promoted live effect.

In **Live mode** the operator is performing: param sliders, masters, blackout, freeze. No chat. The simulator viz mirrors the LEDs exactly so the operator can see what the room sees on screen.

A **"Promote to live"** button in design mode crossfades the live slot from its current effect to the preview effect (over the operator's master crossfade duration). The preview slot keeps the same effect — promotion is a copy, not a move — so the operator can keep tweaking and re-promote as needed.

A **"Pull live to preview"** button in design mode loads the current live effect into the preview slot, so the operator can iterate on what's actually playing.

### 7.2 Engine / runtime data model

```python
class Runtime:
    live: ActiveEffect      # always rendered; goes to DDP (and to sim in live mode)
    preview: ActiveEffect   # active in design mode; goes to sim only
    mode: Literal["design", "live"]
    crossfade: CrossfadeState   # only ever applies to the LIVE slot

@dataclass
class ActiveEffect:
    name: str
    instance: Effect              # compiled, init'd
    params: ParamStore            # current values
    schema: list[ParamSpec]
    perf: RollingStats            # render p50/p95/p99 over last 2s
```

Every frame:

1. Render `live.instance` → `live_buffer (N, 3) float32`.
2. If a live crossfade is in progress, also render the previous live effect and lerp into `live_buffer`.
3. Apply master output stage (saturation pull → brightness gain → clip) to `live_buffer`.
4. If `mode == "live"`:
   - sim_buffer = live_buffer (no second render needed; identical content).
5. If `mode == "design"`:
   - Render `preview.instance` → `preview_buffer`.
   - Apply masters to `preview_buffer` too (same masters; the operator wants to see how it'll look live).
   - sim_buffer = preview_buffer.
6. Encode + transmit:
   - `ddp.send_frame(live_buffer.to_uint8(gamma))`
   - `simulator.broadcast(sim_buffer.to_uint8(gamma))`

Worst case (design mode + live crossfade): three renders per frame (preview, live, live-previous). At 5 ms/render budget that's 15 ms — still inside the 16.6 ms tick. Acceptable, and the watchdog catches violations (§4.4).

### 7.3 Transport routing

The current `MultiTransport` (sim + DDP) gets replaced by a **`SplitTransport`** with two methods:

```python
class SplitTransport:
    sim: SimulatorTransport
    led: DDPTransport | None       # None when running headless / dev sim-only

    async def send(self, *, sim_frame: bytes, led_frame: bytes | None) -> None:
        await self.sim.send_frame(sim_frame)
        if self.led is not None and led_frame is not None:
            await self.led.send_frame(led_frame)
```

Engine is the only caller; it picks `sim_frame` and `led_frame` per the mode rules above. `sim_frame is led_frame` (same bytes) in live mode and in dev-only setups — zero extra copy.

The existing `transport/pause` API (Pi-vs-Gledopto debug) still applies: pausing only blocks the `led` leg; the sim leg continues so the operator UI viz stays alive. No changes needed beyond pointing at the new `SplitTransport`.

### 7.4 UI sketch

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  [Design] [Live]                       brightness ▮▮▮▮▯  speed ▮▮▮▯▯  …     │
├─────────────────────────────────────┬────────────────────────────────────────┤
│                                     │  PARAMS — twin_comets_with_sparkles    │
│           SIMULATOR VIZ             │  ──────────────────────────────────    │
│                                     │  Top comet colour      ■ #ff2020       │
│       (preview effect when in       │  Bottom comet colour   ■ #1040ff       │
│        design; live effect when     │  Top–bottom lead       ▮▯▯▯▯ 0.166     │
│        in live)                     │  Comet speed           ▮▮▯▯▯ 0.40      │
│                                     │  Sparkle fade time     ▮▮▮▯▯ 1.5 s     │
│                                     │  Brightness driver     [low ▾]         │
│                                     │  ─────────────────────────────────     │
│                                     │  perf: 1.4 ms  •  audio: low=0.7       │
│                                     │  ┌────────────────────────────────┐    │
│                                     │  │ Promote to live ▶              │    │
│                                     │  └────────────────────────────────┘    │
├─────────────────────────────────────┴────────────────────────────────────────┤
│  CHAT (design mode only)                                                     │
│  > make the trail spiral around the rig instead of stay on side              │
│  ◀ Wrote new effect spiral_trails.py — preserved your colour + lead tweaks.  │
│  > add a soft white flicker on every kick                                    │
│  ◀ Wrote new effect spiral_trails_with_kick.py …                             │
│                                                                              │
│  Library: [pulse_mono] [audio_radial] [twin_comets_with_sparkles*] …         │
└──────────────────────────────────────────────────────────────────────────────┘
```

In Live mode: chat collapses, library list collapses, "Promote to live" disappears, "Switch to design" appears. The viz/params/masters layout is identical so muscle memory carries over.

### 7.5 Mode persistence

Mode persists across page reloads (local-storage). Restart of the server defaults to **Live** mode (so a kicked Pi doesn't come back up rendering a half-finished preview to nothing).

---

## 8. The system prompt for v2

Re-using the per-turn assembly pattern from `agent/system_prompt.py`. Sections, in order, with concrete content choices for the load-bearing ones:

### 8.1 Section list

1. **YOUR JOB.** One paragraph: you are an LED effect author. You write Python `Effect` classes that get hot-loaded into a render loop on a Raspberry Pi. The user describes what they want; you write the smallest Effect that delivers it. You also declare a small set of UI sliders so they can tune it without re-prompting you.
2. **PHYSICAL RIG** (§8.2 below).
3. **COORDINATE FRAMES** (§8.3 below).
4. **AUDIO INPUT** (§8.4 below).
5. **THE EFFECT CONTRACT** (§8.5 below).
6. **RUNTIME API.** Flat reference table of every name in §5.1, one-line each.
7. **PARAM SCHEMA.** The control vocabulary table (§6).
8. **PERFORMANCE RULES** (§8.6 below).
9. **EXAMPLE EFFECTS** (§8.7 below). Three handwritten reference effects.
10. **ANTI-PATTERNS.** Concrete things the LLM gets wrong (vectorise; preallocate; no imports; trust audio scaling; return shape; no print).
11. **CURRENT EFFECTS.** Source + current param values for both the `live` slot and the `preview` slot. Param values reflect any slider tweaks the operator has made since the effect was generated, so the LLM can see what the operator settled on and use those as the defaults of its next attempt. The LLM defaults to writing into `preview`.
12. **LAST EFFECT ERROR** (only present if the previous attempt failed). Full traceback + the offending source.
13. **TOOL.** `write_effect` schema (the only tool — every chat turn writes a new effect; the operator tunes within it via the UI sliders).

Token estimate: ~6–7k tokens (less than v1 because we trade the primitive catalogue for three reference examples + integration docs).

### 8.2 PHYSICAL RIG block (verbatim into prompt)

```
PHYSICAL RIG
1800 LEDs (WS2815, 60 LEDs/m) on metal scaffolding.
Layout: a tall RECTANGLE — two parallel horizontal rows, ~30 m wide, ~1 m
vertically apart. Each row is two strips wired centre-out: total of 4 strips.

         (top-left)                       (top-right)
   ◄─────── 450 LEDs ◄──┐    ┌──► 450 LEDs ───────►
                        │    │                                ▲ +y (up)
                        │    │                                │
                        │ FEED                                │
                        │    │
   ◄─────── 450 LEDs ◄──┘    └──► 450 LEDs ───────►
        (bottom-left)                    (bottom-right)
                                                             ─► +x (stage-right)

Quadrants are addressed via the named frames `side_top` / `side_bottom` /
`axial_signed` (see COORDINATE FRAMES). Logical pixel 0 of each strip is
its inner end (centre of the rig). The frames `u_loop` and `chain_index`
walk the rig clockwise so motion around the rectangle is one-line.

Coordinate convention (right-handed):
  +x = stage-right
  +y = up
  +z = toward the audience  (the rig is roughly planar in xy; z is small)
Origin = centre of the rig. `ctx.pos` is normalised so each axis is in [-1, 1].
```

### 8.3 COORDINATE FRAMES block

A table copied from §5.3 with one usage tip per frame, e.g.:

```
  side_top      bool-able mask of the top row.    Use:  mask = ctx.frames.side_top.astype(bool)
  u_loop        clockwise around the rig [0,1].   Use:  for "around the rectangle" motion
  signed_x      [-1,+1] left↔right.               Use:  for symmetric explode/collapse
  ...
```

Plus a tiny worked snippet:

```python
# To draw something only on the top row, masked-additively:
self.out[ctx.frames.side_top.astype(bool)] += my_per_led_rgb_top_array
```

### 8.4 AUDIO INPUT block (verbatim into prompt)

```
AUDIO INPUT
The audio-server (Realtime_PyAudio_FFT) captures from a USB mic, runs an FFT,
auto-scales each band's energy to ~[0, 1] via a long-window peak follower, and
publishes the result over OSC. By the time you see it, EVERYTHING IS PRE-
SMOOTHED AND SCALED. Use raw values; do not apply your own EMA/clamp/normalise.

  ctx.audio.low      float in [0, 1]  — band energy ≈ 30–250 Hz (kick, sub).
                                          quiet room ≈ 0.0–0.1
                                          steady groove ≈ 0.3–0.6
                                          peak kick ≈ 0.7–1.0
  ctx.audio.mid      float in [0, 1]  — ≈ 250–2000 Hz (vocals, snare body, melody).
                                          typical 0.05–0.7
  ctx.audio.high     float in [0, 1]  — ≈ 2–8 kHz (hats, sibilance, crack).
                                          typical 0.05–0.6
  ctx.audio.bands    dict {"low":…, "mid":…, "high":…}  — for param-driven band selection.

  ctx.audio.beat     int  — number of fresh onset events since the previous frame.
                            Almost always 0 or 1; occasionally 2. Use `> 0` as a
                            rising-edge trigger. NEVER threshold ctx.audio.low to
                            detect kicks — the upstream onset detector is far better.
  ctx.audio.beats_since_start  int monotonic counter.
  ctx.audio.bpm      float  — current tempo. Falls back to 120.0 when disconnected.
  ctx.audio.connected bool — False = audio-server silent. low/mid/high will be 0.0.

LIVE READING (snapshot at request time):
  device:    {device_name}
  low  ({low_lo:.0f}–{low_hi:.0f} Hz):  {low:.3f}
  mid  ({mid_lo:.0f}–{mid_hi:.0f} Hz):  {mid:.3f}
  high ({hi_lo:.0f}–{hi_hi:.0f} Hz):    {high:.3f}
  bpm:       {bpm:.1f}
```

(The live-snapshot block at the bottom is regenerated per-turn — same pattern as v1.)

### 8.5 THE EFFECT CONTRACT block (verbatim into prompt)

```
HOW YOUR CODE GETS LOADED AND RUN

Your code is a single Python module. We compile it with restricted builtins
(no imports — everything you need is already in scope: numpy as `np`, the
`Effect` base class, helpers, `rng`, `log`, constants). We extract the single
`Effect` subclass you defined, instantiate it, and wire it into the renderer:

    effect = YourEffectClass()
    effect.init(EffectInitContext(n=1800, pos=…, frames=…, strips=…, rig=…))
    # ... then 60 times per second, on the asyncio render loop:
    rgb = effect.render(EffectFrameContext(t=…, dt=…, audio=…, params=…, masters=…))
    # rgb must be a (1800, 3) float32 array with values in [0, 1].
    # We do NOT copy this — we read it directly. Returning `self.out` after
    # filling it in-place is the canonical pattern.

LIFECYCLE
  - `init(ctx)` runs ONCE per effect swap. Use it for ALL precomputation and
    state allocation: per-LED masks, distance lookups, output buffer, RNG-seeded
    arrays. The Pi appreciates it.
  - `render(ctx)` runs every frame. Vectorised numpy. No allocation.

WHAT YOU ARE NOT
  - You are not a layer in a stack. There is exactly one active effect at a
    time. If the user wants two patterns layered, write them in one Effect:
    compute both, blend, return the result.
  - You cannot read other effects, the transport, or the file system.
  - You cannot modify ctx.params or ctx.masters — those are operator-owned.
```

### 8.6 PERFORMANCE RULES block

```
PERFORMANCE — THIS RUNS ON A RASPBERRY PI

Target: render() < 5 ms per call at N=1800. Watchdog will swap your effect
out and report back to you if you blow the budget for >2 seconds.

  RULE 1: Vectorise. NEVER loop over `range(ctx.n)` in Python — use numpy.
  RULE 2: Don't allocate in render(). Preallocate `self.out` and any
          per-LED scratch buffers in `init`. Use `np.add(a, b, out=self.foo)`,
          `arr *= …`, `np.exp(x, out=…)` style.
  RULE 3: Stay in float32. ctx.frames.* and ctx.pos are float32; helpers
          return float32; build temporaries with `np.empty(N, dtype=np.float32)`.
          Mixing fp64 silently slows you down 2× on Pi.
  RULE 4: Do per-LED math in vector form. Even small Python branches on
          per-LED values will eat your budget.
```

### 8.7 EXAMPLE EFFECTS block

Four handwritten reference effects shipped in `surface/examples/` (each ~30–100 lines, fully written into the prompt verbatim):

- **`pulse_mono.py`** — simplest possible. Solid colour fills `self.out`, brightness pulses on `audio.low`. Demonstrates: `init` preallocation, `render` returning `self.out`, audio access, one slider param.
- **`audio_radial.py`** — palette-mapped `frames.radius` scrolled by `t`, brightness modulated by audio. Demonstrates: per-LED scalar field, `palette_lerp`, `audio.bands[...]` with a `select` param.
- **`palette_wash_with_kick_sparkles.py`** — *multi-component effect in one file.* A slow palette-wash background (`palette_lerp` along `u_loop`) plus per-frame stochastic kick-triggered sparkles overlaid additively. Demonstrates: how to write what v1 used to need a layer-stack for — compute A into one buffer, compute B into another, combine with `np.add(a, b, out=self.out)` + `clip01`. This is the canonical pattern any time the operator says "X plus Y" / "background X with Y on top."
- **`twin_comets_with_sparkles.py`** — the §3.2 example, fully written out. Demonstrates: state, side masks, particle deposit, audio modulation, hand-rolled gaussian stamping, `rng`.

These are simultaneously: the LLM's reference templates, the smoke-test fixtures (loaded by `tests/test_examples.py`), and the boot-time default (`pulse_mono`).

---

## 9. Tool call surface for the agent

One tool, server-validated. The LLM never touches param values — those are operator-owned via the dynamic UI. A new chat turn from the user is treated as a request for a script-level change.

### 9.1 `write_effect`

```jsonc
{
  "type": "function",
  "function": {
    "name": "write_effect",
    "description": "Replace the PREVIEW effect with a new Python Effect class plus an operator UI param schema. The operator promotes preview → live separately, and tunes individual values via the UI sliders. Always emit the COMPLETE effect — never a diff.",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "code", "params"],
      "properties": {
        "name": { "type": "string", "pattern": "^[a-z][a-z0-9_]{0,40}$" },
        "summary": { "type": "string" },
        "code": { "type": "string", "description": "A single Python module defining exactly one Effect subclass. Max 8 KB." },
        "params": { "type": "array", "items": { "$ref": "#/$defs/Param" } }
      }
    }
  }
}
```

Server flow on receipt:
1. Validate the param schema (pydantic).
2. AST-scan + sandbox-compile the code.
3. Instantiate the Effect, call `init(ctx)` once. **Reject if `init` takes >200 ms** — common cause is a per-pair (N×N) precompute the LLM didn't realise was quadratic; we'd rather catch it here than have the operator see a hitch on promote.
4. Fence-test: call `render(ctx)` for **30 synthetic frames** (~0.5 s of wall-time at `dt=1/60`) with a synthetic audio impulse train (a `beat=1` event every 6th frame, mid/high oscillating) and the param defaults. Check every frame is `(N, 3) float32 in [0, 1]` with no NaN/Inf. Time them; reject if any single render exceeds 50 ms or if mean exceeds 8 ms. Multi-frame fence-testing catches stateful bugs (NaN drift, off-by-one in deposit logic, sparkle pool overflow, scratch-buffer aliasing) that a 1-frame test misses.
5. Persist `code` + `params` + `param_values` (the defaults) per the layout in §10.
6. Swap into the **preview** slot (no crossfade — preview swap is hard, since the operator is iterating).
7. Tool result: `{ ok: true, name, params }` — or a structured error the LLM can read on the next turn.

### 9.2 Why no `update_params` tool?

Earlier drafts had a second tool to patch param defaults without regenerating code, with the LLM choosing between the two. We dropped it. The rationale:

- The operator already has every param in front of them as a live slider. If they want a different colour or speed, the fastest path is the slider, not a chat round-trip.
- A typed prompt almost always means "I want the *behaviour* to change," not "I want this one default tweaked." Funnelling these through `write_effect` is the right model.
- Fewer tool branches → simpler prompt, smaller chance of the LLM picking the wrong one.

If a chat turn really is just a re-colour, the LLM can write a near-identical effect with the new default in ~2 seconds and the operator gets a cleaner mental model: "every chat turn produces a new effect; the sliders tune within an effect."

---

## 10. Persistence

Each saved effect lives in its own folder under `config/effects/`, with the Python source as a real `.py` file (not a string baked into JSON) and the metadata in a sibling YAML:

```
config/effects/twin_comets_with_sparkle_trails/
  effect.py          ← Python source, real file (diffable, syntax-highlighted, SSH-editable on the Pi)
  effect.yaml        ← metadata + param schema + current operator values
```

`effect.yaml`:

```yaml
name: twin_comets_with_sparkle_trails
summary: "Two comets sweep left→right..."
source: agent          # or 'user' for hand-written
created_at: 2026-05-09T...
updated_at: 2026-05-09T...
params:                # schema (see §6)
  - key: leader_color
    label: "Top comet colour"
    control: color
    default: "#ff2020"
  - ...
param_values:          # operator's current values (auto-merged into next regen — see §6.1)
  leader_color: "#ff70a0"
  lead_offset: 0.083
  ...
```

The split matters for three reasons: `git diff` on the source file is readable; SSH-into-the-Pi-and-tweak-an-effect is one editor open away; and the disk-watcher hot-reload (open Q #4) becomes a one-liner with `watchdog` since `.py` and `.yaml` save events are well-defined. Bundled examples live under `src/ledctl/surface/examples/<slug>/effect.py` with the same shape.

REST endpoints:

| method | path                          | purpose                                       |
| ------ | ----------------------------- | --------------------------------------------- |
| GET    | `/effects`                    | list saved effects                            |
| POST   | `/effects/{name}/load_preview`| load a saved effect into the PREVIEW slot     |
| POST   | `/effects/{name}/load_live`   | load a saved effect into the LIVE slot (with crossfade) |
| POST   | `/effects/{name}/save`        | save the active preview under `name`          |
| DELETE | `/effects/{name}`             | remove from disk                              |
| GET    | `/active`                     | both slots: name, code, params, values, mode  |
| PATCH  | `/preview/params`             | set one or more preview param values          |
| PATCH  | `/live/params`                | set one or more live param values             |
| POST   | `/promote`                    | crossfade live ← preview (preview unchanged)  |
| POST   | `/pull_live_to_preview`       | copy live → preview (preview overwritten)     |
| POST   | `/mode`                       | set mode = "design" \| "live"                 |

State persisted across restarts: `mode`, `live.name + values`, `preview.name + values`. On boot we re-instantiate from disk; if a saved effect's source no longer compiles (e.g. helper API changed), we fall back to `pulse_mono` for that slot and surface a warning in the UI.

---

## 11. Engine integration (full overwrite, no toggle)

The existing `surface/` package is removed. Engine, mixer, agent, API, and presets are reworked to talk to the new runtime directly. The migration path is destructive — we lean on the recent commits as the rollback boundary.

### 11.1 What gets deleted

```
src/ledctl/surface/                         — entire package
src/ledctl/mixer.py                          — replaced by runtime.py (post-stage moves over)
src/ledctl/agent/tool.py                     — replaced by surface/tool.py (write_effect / update_params)
src/ledctl/agent/system_prompt.py            — replaced by surface/prompt.py
config/presets/                              — kept on disk for reference, not loaded
src/ledctl/presets.py                        — replaced by surface/persistence.py
tests/test_surface_*.py                      — deleted (or moved to /tests/legacy_v1/ for diff inspection, .gitignored before commit)
tests/test_mixer.py                          — rewritten against the new runtime
```

### 11.2 What gets kept verbatim

- `topology.py` (incl. `frames.py` content moved to `surface/frames.py` underneath the new package).
- `audio/*` (state, bridge, supervisor — entirely audio-server-facing).
- `masters.py` — operator-owned controls, render-loop concept, unchanged.
- `pixelbuffer.py` — gamma encode + uint8 cast.
- `transports/{base,ddp,simulator}.py` — but `multi.py` is replaced by `split.py` (§7.3).
- `config.py` — gains a few fields (`engine.preview_default`, etc.); no removals.
- `api/auth.py` — auth gate untouched.
- `cli.py` — boot path unchanged.

### 11.3 New folder layout

```
src/ledctl/
  surface/                       ← THE new package (replacing the old one of the same name)
    __init__.py                  — public API: Runtime, Effect, build_runtime_namespace
    base.py                      — Effect base class, EffectInitContext, EffectFrameContext, AudioView, FrameMap, ParamView
    frames.py                    — moved from old surface/, unchanged content
    sandbox.py                   — AST scan + safe exec + class extraction
    helpers.py                   — hex_to_rgb, hsv_to_rgb, gauss, pulse, lerp, clip01, palette_lerp, named_palette
    palettes.py                  — NAMED_PALETTES, LUT baking (lifted from old surface/)
    runtime.py                   — Runtime: live/preview slots, crossfade, param store, master output, watchdog
    schema.py                    — pydantic models for tool calls + Param spec
    persistence.py               — load/save effect JSONs from config/effects/
    prompt.py                    — build_system_prompt(...): assembles §8 prompt
    tool.py                      — apply_write_effect, apply_update_params handlers
    examples/
      pulse_mono/effect.py + effect.yaml
      audio_radial/effect.py + effect.yaml
      palette_wash_with_kick_sparkles/effect.py + effect.yaml
      twin_comets_with_sparkles/effect.py + effect.yaml
      __init__.py                — list of bundled examples
  transports/
    split.py                     — NEW: SimulatorTransport + DDPTransport with separate sim/led frames
    multi.py                     — DELETED
  engine.py                      — slimmed: owns Runtime, calls render → SplitTransport
  api/
    server.py                    — routes rebuilt against the new model
    agent.py                     — points at surface/prompt + surface/tool
config/
  effects/<slug>/{effect.py, effect.yaml}   — NEW: persisted effects (source + metadata sidecar)
  presets/                       — left in place for reference; no longer loaded
src/web/
  index.html                     — rebuilt as the dual-mode shell
  lib/app.js                     — chat + dynamic param panel + viz + mode toggle
tests/
  test_sandbox.py                — AST scan, builtins, error reporting
  test_runtime.py                — preview/live slots, crossfade, watchdog, split transport
  test_examples.py               — load each example, fence-test, render 60 frames
  test_persistence.py
  test_prompt.py                 — sanity-checks the assembled system prompt
```

### 11.4 Engine rewrite (sketch)

```python
class Engine:
    def __init__(self, cfg, topology, transport: SplitTransport, masters=None):
        self.cfg = cfg
        self.topology = topology
        self.transport = transport
        self.target_fps = cfg.project.target_fps
        self.gamma = cfg.output.gamma
        self.runtime = Runtime(topology, masters=masters or MasterControls())
        self.runtime.load_default()    # pulse_mono into both slots on boot

    async def _loop(self):
        # ... (timing identical to today; period gating + audio kick) ...
        ctx_live, ctx_preview = self.runtime.build_contexts(wall_t, dt, audio_view, masters)

        live_buf = self.runtime.render_live(ctx_live)         # (N, 3) float32 in [0, 1]
        if self.runtime.mode == "design":
            preview_buf = self.runtime.render_preview(ctx_preview)
            sim_buf = preview_buf
        else:
            sim_buf = live_buf

        led_bytes = self.runtime.encode(live_buf, self.gamma)
        sim_bytes = led_bytes if sim_buf is live_buf else self.runtime.encode(sim_buf, self.gamma)
        await self.transport.send(led_frame=led_bytes, sim_frame=sim_bytes)
```

`Runtime` owns everything that used to live in `Mixer` (master output stage, crossfade, blackout) plus the new preview slot, watchdog, and per-effect param/perf stores. Calibration overrides hook into `Runtime.encode` — same place Mixer applied them.

---

## 12. Crossfade between effects

Crossfade only ever applies to the **live** slot — preview swaps are hard cuts because the operator is actively iterating and a crossfade would make "did my fix work?" feedback ambiguous.

```python
def render_live(self, ctx) -> np.ndarray:
    if self._cf is None:
        return self._apply_masters(self.live.instance.render(ctx))
    elapsed = ctx.wall_t - self._cf.start
    if elapsed >= self._cf.duration:
        self._cf = None
        return self._apply_masters(self.live.instance.render(ctx))
    alpha = clip01(elapsed / self._cf.duration)
    a = self._cf.previous.render(ctx)
    b = self.live.instance.render(ctx)
    np.multiply(a, 1.0 - alpha, out=self._cf_scratch)
    self._cf_scratch += b * alpha
    np.clip(self._cf_scratch, 0.0, 1.0, out=self._cf_scratch)
    return self._apply_masters(self._cf_scratch)
```

Alpha uses `wall_t` so freeze/speed don't slow the crossfade — same v1 contract.

---

## 13. Error handling

| failure                                           | what happens                                                                                       |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `write_effect` → SyntaxError / AST-disallowed     | tool result `{ok: false, error: "compile_failed", traceback}` — LLM self-corrects on next turn     |
| `write_effect` → fence test crashes (`init` or first `render`) | same, with the specific exception. Preview slot unchanged.                              |
| live `render` raises after going active           | catch, log traceback, blank that frame. After 3 consecutive failures, swap that slot to `pulse_mono` (safe idle) and surface the error in the chat for the next LLM turn. |
| live `render` returns wrong shape / dtype         | same as above. Shape check costs ~50 ns / frame and runs on first render only after a swap.        |
| watchdog: p95 over budget for 2 s                 | warn → halve target FPS for that effect → if still over, swap to `pulse_mono`, post perf report to chat |

All error tracebacks are surfaced to the LLM on the next turn under a **`LAST EFFECT ERROR`** section, so the LLM can fix without the operator having to re-prompt.

LLM gets up to **2 automatic consecutive retries** (configurable in `config.yaml`) before the chat asks the operator whether to keep trying.

---

## 14. Security notes

We're not building a public sandbox. Threat model:

- **Goal:** the LLM's typo `import os; os.system("rm -rf /")` cannot run.
- **Goal:** a runaway `while True: pass` cannot wedge the render loop.
- **Non-goal:** defending against a malicious operator who has already chat access.

Mitigations:

- **AST reject** of `import` and dunder access (§4.2).
- **Stripped builtins** — no `eval/exec/open/__import__/print/getattr/setattr/...`.
- **Source size cap** (8 KB).
- **Render budget watchdog** (§4.4). We do *not* try to interrupt mid-render — numpy holds the GIL through C and a hard kill mid-call would corrupt state. The pragmatic answer is the budget warning + automatic swap to safe idle.

Good enough for the festival rig. If we ever serve this to untrusted users, swap `exec` for a real sandbox (subinterpreter / RestrictedPython / Pyodide-in-process). Out of scope.

---

## 15. Implementation plan (suggested phasing)

### Phase 0 — destructive cut (½ day)
- [ ] Confirm working tree is clean (it is).
- [ ] Branch `surface-v2-rewrite`.
- [ ] Delete the v1 surface package, mixer, agent tool/prompt, presets.py, related tests.
- [ ] Move `frames.py` content into the new `surface/frames.py`.
- [ ] Stub the new `surface/` package so the tree compiles.
- [ ] Engine boots into a hardcoded "all-black" effect; tests are red but project runs.

### Phase 1 — runtime + sandbox (1 day)
- [ ] `Effect` base class (with preallocated `self.out`), `EffectInitContext`, `EffectFrameContext`, `FrameMap`, `AudioView`, `ParamView`, `MastersView`.
- [ ] `helpers.py` — full §5.1 surface, with tests for each helper (focused on float32 correctness and `out=` paths).
- [ ] `sandbox.py` — AST scan, restricted builtins, `compile_effect()`. Tests: imports rejected, dunder access rejected, source-size cap, normal numpy code accepted.
- [ ] `runtime.py` — live/preview slots, mode switch, crossfade, watchdog scaffold, ParamStore. Master output stage (saturation pull → adaptive brightness → clip), copied from the old mixer.
- [ ] `transports/split.py` — replace MultiTransport.

### Phase 2 — example effects + smoke harness (½ day)
- [ ] `examples/pulse_mono.py`, `audio_radial.py`, `twin_comets_with_sparkles.py`. These double as **acceptance tests** for the runtime API.
- [ ] `tests/test_examples.py`: load each example, instantiate against a synthetic 1800-LED topology, render 60 frames with synthetic audio, assert no exceptions + bounded RGB + render time under 5 ms (skip the timing assertion in CI on x86; only assert it on the Pi run).

### Phase 3 — REST + persistence (½ day)
- [ ] All endpoints from §10.
- [ ] `persistence.py` — load on boot, save on `write_effect`, fall back to `pulse_mono` if a saved source no longer loads.
- [ ] Boot defaults: `pulse_mono` in both slots; mode = "live".

### Phase 4 — agent integration (1 day)
- [ ] `prompt.py` — assemble §8 prompt.
- [ ] `tool.py` — `write_effect` handler (the only tool).
- [ ] `api/agent.py` — wired to the new tools/prompt.
- [ ] **Acceptance:** the user's flagship "twin comets with sparkle trails" prompt one-shots cleanly. If it doesn't, the prompt is wrong — iterate until it does.

### Phase 5 — operator UI (1 day)
- [ ] `index.html` rebuilt as the dual-mode shell.
- [ ] `lib/app.js`: mode toggle, dynamic param panel (slider/color/select/toggle/palette), chat (visible only in design), live-code viewer, saved-effects list, masters row, preview/live indicators, "Promote to live" / "Pull live to preview" buttons.
- [ ] Reuse simulator canvas + WebSocket frame stream verbatim.

### Phase 6 — polish (rolling)
- [ ] Render budget watchdog wired to UI badge.
- [ ] "Star" effects for quick-recall in the library.
- [ ] LLM-side: monitor whether the LLM is faithfully carrying forward the operator's param tweaks from the previous effect's `param_values` into the new effect's defaults; tighten the prompt if it isn't.
- [ ] Pi field-test sweep: measure per-example render time on the rig, tune budget if needed.

**Total: ~4–5 dev days** to a usable system the user can drive on the rig.

---

## 16. Open questions (worth deciding before code)

1. **Single-effect vs. layer stack.** v2 is single-effect per slot for prototype clarity. The LLM is good at writing a unified effect; layer stacks were a v1 affordance because primitives were limited. Revisit if a real prompt fails because of this.
2. **Effects calling each other.** Could we let the LLM `apply_named_effect("pulse_mono")` from inside its own `render`? Tempting (sub-effects), but breaks the "one file, one Effect" mental model. Skip for v2.
3. **Crossfade duration.** Use the existing master crossfade slider (already operator-owned). The LLM never picks the duration. Same v1 contract.
4. **Hot-reload from disk.** Should we watch `config/effects/*.json` and auto-load? Useful for developer tinkering on the Pi via SSH. **Yes** behind a config flag, off by default.
5. **Behaviour on render crash.** Blank frame for the failing render, then 3-strikes swap to `pulse_mono`. Configurable to "1 Hz dim red breathing" if blank is too jarring.
6. **Code-size cap.** 8 KB / ~200 lines per effect. Reject above with a structured error to keep the agent disciplined.
7. **Mobile UI.** Out of scope. Dual-mode panel is desktop-first; phone-friendly version comes later.
8. **Preview crossfade.** Hard-cut on preview swap (so the operator sees the new code immediately). Crossfade only on live promote / preset load on the live slot.
9. **What happens to the old `config/presets/`?** Leave on disk for reference; not loaded by the new runtime. We can write a one-shot migration tool that takes a v1 preset and asks the LLM to translate it to a v2 Effect, but that's a separate project.
10. **Carrying operator tweaks forward.** Resolved: **auto-merge by `key`** (see §6.1). Mechanical merge for matching keys whose previous value fits the new bounds; the LLM is told this in the prompt and chooses keys deliberately (reuse for "same knob, new effect"; rename for "this knob means something different now"). Hybrid keeps the common case bulletproof without losing the LLM's ability to restructure params freely.

---

## 17. The "north star" worked example, end to end

The user's flagship prompt walked through the new system:

> *"Create two comets going from left to right, the top one is leading the bottom one by 1/6th of the total distance. After a comet has passed, it leaves behind sparkles flickering in its color that gradually darken and fade out. The top comet is red, the bottom one deep blue. Both of them are pulsating in brightness between 0.6 and 1.0 max brightness based on the 'low' audio signal."*

### Operator
- Switches to **Design mode** (single click).
- Types into chat. Hits send.
- LEDs continue showing whatever the current `live` effect is — dance floor undisturbed.

### LLM round-trip
- Reads system prompt: knows the rig is a 1800-LED rectangle, `frames.side_top` masks the top row, `audio.low` is pre-smoothed in `[0, 1]`, `Effect` contract, `rng`, `hex_to_rgb`, `wrap_dist`, the worked example template.
- Emits one `write_effect` tool call with code ≈ §3.2 above + a six-param schema.

### Server
- Param schema validated.
- AST scan: clean.
- Sandbox compile: ok.
- `init(synthetic_ctx)` runs in <1 ms (precompute masks).
- Fence-test `render(synthetic_ctx)`: returns `(1800, 3) float32`, all in `[0, 1]`, takes ~1.4 ms. Pass.
- Save to `config/effects/twin_comets_with_sparkle_trails.json`.
- Hard-swap into the **preview slot** — operator sees it instantly in the simulator.
- Tool result `{ok: true, name, params}`.

### UI (design mode)
- Param panel updates: 6 controls appear (two colour pickers, four sliders, one dropdown).
- Chat shows the LLM's `summary` + a "View source" disclosure.
- Simulator viz shows the new effect, audio-reactive — but the LEDs still show the previous live effect.

### Iteration in design mode
- Operator drags `lead_offset` from 0.166 → 0.083 → comets get tighter on screen. No LLM call.
- Operator drags `leader_color` to a brighter pink in the UI colour picker. No LLM call.
- Operator: "now make the trail particles spiral around the rig instead of stay on side" → script-level change. LLM emits `write_effect` with new code (using `frames.u_loop` + `wrap_dist`). The system prompt's CURRENT EFFECTS block already shows it the operator's tweaked values (`lead_offset = 0.083`, `leader_color = "#ff70a0"`), so the new effect's defaults preserve the operator's tuning.

### Promote
- Operator clicks **"Promote to live"**. The live slot crossfades from the previous effect to `twin_comets_with_sparkle_trails` over the master crossfade duration.
- Operator can stay in design mode (sim still showing preview, which is the same effect now) or switch to **Live mode** (sim now mirrors LEDs; sliders only, chat hidden) for the rest of the set.

This is the UX we're building toward.

---

## 18. What gets removed from v1, and why that's fine

In the spirit of "no half-finished implementations" — the things v2 explicitly drops:

- **The typed primitive graph.** Replaced by Python code. The compile-time safety we lose is replaced by (a) AST sanity-checks, (b) fence-test on `init` + first `render`, (c) live watchdog with auto-rollback. The expressivity gained is large; the safety lost is recoverable.
- **Layer stacks + blend modes.** One effect per slot. Compose inside the effect.
- **Cross-effect palette / scalar reuse.** Each effect re-derives what it needs from `frames` + `helpers`.
- **`primitives_json` REST catalogue.** No primitives. Catalogue is the system prompt + `examples/`.
- **Persisted layer-stack presets** (`config/presets/*.yaml`). Replaced by per-effect JSONs (`config/effects/*.json`). Old YAML files stay on disk for reference but the new runtime doesn't load them.

Things kept verbatim:

- Topology + named coordinate frames.
- Audio bridge + `AudioState` semantics + the audio-server subprocess.
- Master controls (brightness/saturation/speed/freeze/audio_reactivity), incl. adaptive headroom.
- Crossfade math (only applied to the live slot now).
- Calibration overrides.
- DDP / simulator transports + the `transport/pause` API.
- Auth gate.

The split is clean: v2 changes **what gets rendered and how the operator interacts with it**, not **how it's shipped**.

---

## 19. Decision summary

| question                                  | answer                                                          |
| ----------------------------------------- | --------------------------------------------------------------- |
| Substrate for LLM-authored code           | Python with sandboxed `exec`, AST-scanned, restricted builtins  |
| Effect shape                              | One `Effect` subclass per file: `init(ctx)` + `render(ctx) → (N,3) float32` |
| Spatial vocabulary                        | Named frames (`x / u_loop / radius / side_top / signed_x / ...`) via `ctx.frames.*`, all float32, plus `ctx.pos` |
| Audio vocabulary                          | `low / mid / high / beat / bpm` via `ctx.audio.*` — pre-smoothed and pre-scaled upstream; LLM uses raw values |
| Sandbox cost on the Pi                    | One-time at compile; zero per-frame overhead. Per-frame perf comes from numpy + preallocated buffers + float32. |
| Param schema                              | Six control types declared per-effect by the LLM                |
| Operator modes                            | **Design** (chat + preview-only render) and **Live** (sliders + render to LEDs). Sim and DDP can show different streams in design mode. |
| Iteration                                 | One LLM tool: `write_effect` (full new effect, into preview). Operator tunes within an effect via the UI sliders — never via the chat. Param values auto-merge by `key` across regenerations (§6.1) so colour/speed tweaks survive automatically; the LLM picks new keys when a knob's meaning changes. |
| State                                     | Per-effect via `self.*`. No globals, no inter-effect state. Runtime never mutates the effect's returned buffer (one memcpy into `master_buf` before masters apply). |
| Crossfade & error recovery                | Crossfade only on live promote. Render errors → blank frame, 3-strikes swap to `pulse_mono`, error reported to LLM. Watchdog trips after **0.5 s** of p95-over-budget (not 2 s) to keep stutter off the dance floor. |
| Storage                                   | `config/effects/<slug>/{effect.py, effect.yaml}` — Python source as a real file, metadata + param schema + values in a sibling YAML. Diffable, SSH-editable, ready for disk-watch hot-reload. |
| Migration path                            | Full replacement — old surface package and mixer deleted in this refactor. |
| Estimated build time                      | ~4–5 dev days to a usable system on the rig                     |

The bet: **a ~700-line runtime + a great system prompt outperforms a 2,500-line typed primitive graph**, because the LLM is the right tool to author the long tail of effects, and we should give it the room — and the right amount of context about the Pi, the rig, and the signals — to actually do that.

---

## 20. Future work — nice to have, deferred to v3

These came up during v2 design but don't need to land in the first cut. Each is a self-contained follow-up; none of them are blockers for getting the user on a rig with a working LLM-authored effect loop.

### 20.1 Performance / runtime

- **Design-mode preview at half-rate.** During a live crossfade in design mode the worst case is three renders per tick (live, live-previous, preview). At ~5 ms/render budget that's 15 ms of 16.6 ms — uncomfortably close on a Pi 4. v3: render the preview every other frame (the simulator viz is a UI preview, not on stage; 30 fps is plenty for visual judgment). Halves design-mode CPU during crossfades.
- **Adaptive render-budget per effect.** Today the budget is a single number (5 ms). v3: track the live effect's actual mean and dynamically grant the preview slot whatever's left. Lets a cheap live effect host a more expensive preview without false-positive watchdog trips.
- **Per-effect `dt` clamping or substepping.** If the asyncio loop hiccups (e.g. DDP packet retransmit), `dt` could jump to ~50 ms and stateful effects (comets, ripples) would tele-port. v3: cap `dt` at e.g. 2× target, or substep stateful integrators.

### 20.2 Authoring / LLM ergonomics

- **`update_params` tool (LLM-driven preset shifts).** v2 deliberately has only `write_effect`; the operator tunes via sliders. v3 might introduce a second tool that lets the LLM emit *just* a new `param_values` dict for the current effect (e.g. "warmer, slower" → re-colour without rewriting code). Cheaper, faster, but adds prompt complexity. Revisit when the operator complains about the round-trip cost of full regenerations for tiny tweaks.
- **Sub-effects / effect composition.** v2 is one Effect per slot. v3 could expose `apply_named_effect("pulse_mono", out=…)` so an Effect can render an existing effect into a buffer and modulate it (e.g. "tint and blur this saved effect"). Tempting but breaks the "one file, one Effect" mental model. Only worth it if real prompts repeatedly want this.
- **Hot-reload from disk.** v2 ships behind a config flag (open Q #4). v3: turn on by default with a debounce + fence-test before swap, so editing `config/effects/<slug>/effect.py` over SSH on the Pi just works. The new file layout (real `.py`) makes this trivial to implement.
- **Library-side effect "stars" / quick-recall.** Phase 6 polish flagged it. v3: a starred-effects rail in the operator UI for one-tap recall mid-set, with thumbnail previews rendered offline.
- **v1 preset migration tool.** A one-shot "translate this v1 layer-stack YAML into a v2 Effect" prompt + scaffold. Useful only if the v1 presets contain looks the operator misses; defer until that complaint actually surfaces.

### 20.3 Operator UX

- **Mobile / tablet operator UI.** Out of scope for v2 (desktop-first dual-mode shell). v3: phone-friendly Live mode (sliders + masters + promote-from-library, no chat) since the operator is often physically away from a laptop (`user_design_spec.md` §9).
- **Hands-free / MIDI / OSC param control.** Hardware fader / knob → param key. Same `PATCH /params` plumbing under the hood.
- **"Surprise me" / generative riffs.** `user_design_spec.md` §10 — let the LLM volunteer a riff on the current effect ("twin comets but in spiral form") on operator request. Just another `write_effect` invocation with a different prompt template; UI affordance only.

### 20.4 Reliability / safety

- **Stronger sandbox (subinterpreter or RestrictedPython).** v2's threat model is "LLM typo," not "malicious input" (§14). v3 only matters if we ever expose the chat to untrusted users (livestream guests, etc.) — at which point swap `exec` for a real isolation boundary.
- **State-snapshot on crash.** When the watchdog swaps an effect to `pulse_mono`, currently we lose its `self.*` state. v3: pickle a snapshot for post-mortem so the LLM can reason about which buffer was misbehaving.
- **Render-time CPU profiling fed back to the LLM.** v2 reports p95. v3: capture per-call profile data ("85% of time in `np.exp` over 1800 elements") and surface it in the next system prompt so the LLM can target the actual hotspot.
