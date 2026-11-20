# CLAUDE.md

Guidance for Claude Code working in this repo. The hard plumbing (transport, topology,
audio bridge, auth, deploy) is **done and stable** — most future work is **UX / LLM
interface / effects**, so this file leans into those. Deep hardware/network detail lives in
sibling docs (`Hardware_setup.md`, `reach_the_pi.md`); only the operationally-critical bits
are kept here.

## What this is

A real-time controller for a **WS2815 LED install**: up to four 450-LED strips fed via DDP by
one Gledopto/WLED controller. The rig is a **hub rig**: the Gledopto at the origin and three or
four strips ("legs") running outward from it (the dev config holds four, 1800 LEDs; the Pi config three, as `leg_3` is not wired on site). It replaced a
rectangle of two 30 m rows; the prompt, the simulator and every library effect are on the hub
rig. The defining idea
of the current design ("surface v2", now on `main`): **the LLM authors a Python `Effect`
subclass per chat turn**, and the operator stacks effects Resolume-style into a *composition*
and crossfades them live. There is no typed primitive graph any more.

**Which device am I on?** Dev happens on the Mac (transport=simulator); production runs on the
Pi (transport=ddp/multi). Several Claude instances may run at once — always check terminal /
logs / hostname to know whether you're on the Pi or the Mac before acting.

## Documentation map

- `docs/reference/rig-geometry.md` — open it when touching the `strips` block of a config,
  `topology.py`, `surface/frames.py` or `ctx.rig`, when writing an effect for the hub rig, or
  when laying the legs out in a new room.
- `docs/reference/wzrd-input.md` — open it when touching `src/ledctl/wzrd/`, the audio bridge's
  network mode, the master output stage's hook, the audio swell, or when the LEDs do not follow
  WZRD's wall.
- `docs/reference/midi-input.md` — open it when touching `src/ledctl/midi/`, the master
  effects (`surface/master_fx.py`), the AKAI drawing in the UI, or when the controller's pads
  or faders do not act on the LEDs.
- `docs/reference/render-performance.md` — open it when the LEDs lag or drop frames, or before
  adding anything that runs on the event loop (a request handler, the engine tick, a
  WebSocket push, an OSC handler); it holds the "LEDs lag" runbook.
- `Mac_direct_setup.md` — open it before a gig to clear the Gledopto's Wi-Fi settings, or if
  the Pi isn't on site and the Mac has to drive the Gledopto over WLED's own hotspot.
- `docs/TODO/` — plans in flight.

## Commands

```bash
# Setup
uv venv --python 3.11
uv pip install -e ".[dev]"

# Run (Mac dev — simulator at http://127.0.0.1:8000, OpenAPI at /docs)
uv run ledctl run --config config/config.dev.yaml
uv run ledctl show-config --config config/config.dev.yaml   # inspect parsed config
# HTTP request logging is off by default; add --access-log to `run` when debugging the API

# Tests / lint
uv run pytest
uv run pytest tests/test_runtime.py            # single file
uv run ruff check src tests
```

`uv run` resolves the console script from the project venv (auto-syncing if deps drift), so
there's no need to activate or reference `.venv/bin` directly. If the venv's interpreter ever
goes stale (broken shebang after a Python upgrade), `rm -rf .venv && uv venv --python 3.11 &&
uv pip install -e ".[dev]"` rebuilds it.

`cli.py` auto-loads `.env` from the repo root for `OPENROUTER_API_KEY` — never put the key in
YAML. With no key, `/agent/chat` returns a clear 503 and the render loop is unaffected.

---

# The LLM interface (the part that matters most)

**One tool, one call per turn, always the complete effect — never a diff.** The whole agent
layer (`agent/`) is a thin OpenRouter wrapper, *not* a multi-tool loop. Model is set in YAML
(`agent.model`, currently `openai/gpt-6-luna`, with `agent.fallback_models`
(`z-ai/glm-5.3-flash`) sent as OpenRouter's `models` list so it answers when the primary is down; `agent.reasoning_effort` maps to
OpenRouter's unified reasoning param — `none` sends `{enabled: false}` (thinking off, notably
faster), `low/medium/high` send `{effort: ...}` (`medium` is the current setting), `null` omits it
entirely (provider default). Chat fetches abort client-side at `CHAT_TIMEOUT_MS` (90 s) in
both `main-*.js` — keep it above `agent.request_timeout_seconds` × the retry budget).

```
write_effect({ name, summary, code, params })
```

- `name` — snake_case, ≤40 chars. `summary` — one sentence (shown to operator in chat).
- `code` — one `Effect` subclass, ≤8 KB, no imports (runtime API is pre-injected).
- `params` — 0–8 operator controls (the "Effect Knobs" panel). See PARAM SCHEMA below.
- **No `blend` / `opacity`** — those are operator-owned and deliberately invisible to the LLM.
- **No discovery tools** (`list_*` / `get_*`) — everything the LLM needs is in the system
  prompt, regenerated fresh every turn.

**Where it lands.** `write_effect` replaces the **selected PREVIEW layer** (hard cut). The LIVE
LEDs are untouched until the operator clicks **Promote to live**. The LLM has *no visibility
into LIVE* and no way to change it — author the preview layer clean and self-contained.

**Compile pipeline** (`tool.py` → `runtime._compile_layer`), run before swap-in:
1. validate `params` schema (pydantic, `schema.py`)
2. AST scan + sandbox compile (`sandbox.py`)
3. `init(ctx)` against the real topology, **≤200 ms**
4. **30-frame fence test** (synthetic audio, `beat=1` every 6th frame) — wrong shape/dtype,
   NaN/Inf, or any exception rejects the install
5. on success: install into the selected preview layer. Only the operator's save
   (`POST /preview/save`) writes it to the library

On failure at any stage the tool result is `{ok: false, error, details}` with a diagnostic
hint; the next turn's prompt embeds it under **LAST EFFECT ERROR** so the LLM self-corrects.
Up to `agent.retry_on_tool_error` (=2) auto-retries happen inside one chat turn before the
failure surfaces to the operator.

**Param auto-merge.** When a new layer installs, operator-tuned `param_values` for any `key`
that matches the prior layer's schema carry forward. The LLM is told this and picks new keys
when a knob's meaning changes.

**Masters are read-only to the LLM.** The prompt does *not* surface master values as writable.
When a request can only be honoured by a master move ("brighter" while master brightness is
already 1.0, "less reactive"), the LLM is instructed to **tell the operator which slider to
move** rather than re-emit redundant code.

**Operator side-channel.** Any operator action that mutates preview source outside the agent's
own write_effect path (`load_preview`, `pull_live_to_preview`, `preview/save` rename) calls
`SessionStore.reset_all_buffers()` so the model's message deque doesn't reference stale source.
The operator-visible transcript is preserved; only the model-visible buffer clears.

## The system prompt (`surface/prompt.py`)

Rebuilt every turn by `build_system_prompt(...)`. Single canonical home per topic — no
restatements. Section order:

```
ROLE → TOOL → RIG (generated from the topology) → COORDINATE FRAMES → AUDIO INPUT →
EFFECT CONTRACT → RUNTIME API → PARAM SCHEMA → PERFORMANCE → ANTI-PATTERNS →
EXAMPLE EFFECTS → CURRENT EFFECT → [LAST EFFECT ERROR]
```

- **CURRENT EFFECT** shows *only the selected preview layer's* source + param schema +
  current values. Other preview layers and all of LIVE are deliberately hidden — showing them
  tempts the LLM to "preserve" surrounding state instead of authoring its own layer cleanly.
- **EXAMPLE EFFECTS** are rendered as **complete `write_effect` payloads** (name + summary +
  params + code) from `src/ledctl/surface/LLM_example_effects/<slug>/{effect.py, effect.yaml}`
  on every build. There are four, each a byte copy of its library effect: rings on the beat
  (`beat_rings`), particles that hop legs (`leg_comets`), a noise wash (`hub_ember`), a
  rotating beam (`leg_sweep`). They live outside `config/effects/` so the operator can't
  delete them from the library UI and break the prompt.
- **Size limit: 26,000 characters** for the built prompt, pinned by `tests/test_examples.py`.
  A fifth example does not fit.

The prose blocks (`RIG_RULES`, `EFFECT_CONTRACT`, `PERFORMANCE_RULES`, `ANTI_PATTERNS`) and
the examples dominate the cost.

---

# Effects

A *layer* is one Python `Effect` subclass (LLM-authored, or hand-written and saved). Each is
its own module under `config/effects/star/<slug>/{effect.py, effect.yaml}` — real Python, diffable
and SSH-editable on the Pi.

## The Effect contract

```python
class MyEffect(Effect):
    def init(self, ctx):     # ONCE on swap-in. Precompute per-LED arrays + state buffers.
        ...                  # self.out is preallocated by the base class as (N, 3) float32.

    def render(self, ctx):   # every frame (~60 Hz). Vectorised numpy only.
        ...                  # MUST return (N, 3) float32 in [0, 1].
                             # Canonical: fill self.out in place, `return self.out`.
```

The runtime copies the returned buffer into its own scratch before applying masters, so
returning `self.out` and trusting state survives across frames is safe.

**`ctx` surface (read-only — never mutate params/masters):**

| field | meaning |
| --- | --- |
| `ctx.n` / `ctx.t` / `ctx.wall_t` / `ctx.dt` | sizes & times (`dt` clamped at 2× frame interval on a hiccup) |
| `ctx.pos` | `(N, 3)` float32: metres / distance of the farthest LED, hub at 0 |
| `ctx.frames.<name>` | per-LED scalar arrays — see COORDINATE FRAMES |
| `ctx.audio.<...>` | audio bands / beat / bpm — see AUDIO CONTRACT |
| `ctx.params.<key>` | operator-controlled values, auto-updated between frames (writes are no-op + warning) |
| `ctx.masters` | frozen `MastersView` — diagnostic only |
| `ctx.strips` / `ctx.rig` | topology, init-time only |

`ctx.frames.x` — **not** `ctx.x` (raises AttributeError). This is the single most common LLM miss.

## Runtime namespace (in scope, no imports)

`np`, `Effect`, `hex_to_rgb`, `hsv_to_rgb`, `lerp`, `clip01`, `gauss`, `pulse`, `tri`,
`wrap_dist`, `palette_lerp(stops, t)`, `named_palette(name)`, `rng` (seeded by SHA-256 of the
effect name → deterministic across reloads), `log` (use instead of `print`, which isn't a
builtin), and constants `PI`, `TAU`, `LUT_SIZE` (=256), `PALETTE_NAMES`. Every array-returning
helper (`hsv_to_rgb`, `palette_lerp`, `lerp`, `clip01`, `gauss`, `pulse`, `tri`, `wrap_dist`)
accepts an `out=` for allocation-free writes; `hex_to_rgb` does not (cached scalar).

**Sandbox** (`sandbox.py`): no `import`, `eval`, `exec`, `open`, `getattr/setattr`, `globals`,
`print`; AST rejects every dunder attribute except `__name__`; source ≤8 KB; exactly one
`Effect` subclass. Threat model is "LLM typo," not a malicious operator.

## Coordinate frames (`surface/frames.py`)

Effects address named per-LED scalars via `ctx.frames.<name>`; the prompt lists the hub-rig frames
(`PROMPT_FRAMES`):
**`radius`** (0 at the hub, 1 at the farthest LED), `chain_index` (fraction along the LED's own
leg), `leg` / `leg_phase` / `angle` (which leg) and `signed_x` / `signed_y`; a new effect reads
only these. The older names (`u_loop`, `side_*`, `axial_*`, `x/y/z`, …) are still computed
and hidden from the LLM; on a star most are an arbitrary order. Definitions:
`docs/reference/rig-geometry.md`.

**Geometry caveat:** this is **not a 2D plane**. LEDs exist only on K thin legs, so a 2D field
is sampled along them and a free-roaming 2D particle lights almost nothing. Things travel
along a leg, hop between legs, or are rings (a function of `radius` alone).

## Audio contract (effect-side)

The audio bridge writes upstream OSC into `AudioState`; the engine packages it once per tick
into an `AudioView` and **pre-multiplies the bands by `masters.audio_reactivity`** — so the LLM
uses raw values:

| field | semantics |
| --- | --- |
| `ctx.audio.low / .mid / .high` | smoothed, auto-scaled band energies in ~`[0, 1]` |
| `ctx.audio.bands` | `{"low","mid","high"}` dict — handy for `select`-param band choice |
| `ctx.audio.beat` | float in `[0, 1]`, ~0 most frames, non-zero on a fresh onset |
| `ctx.audio.beats_since_start` | monotonic onset counter (not scaled by reactivity) |
| `ctx.audio.bpm` | tempo; falls back to `120.0` when disconnected |
| `ctx.audio.connected` | `False` = audio-server silent; bands will be `0.0` |

**The canonical rule (in ANTI-PATTERNS verbatim):** for *any* discrete/rhythmic event (kicks,
flashes, sparkle spawns, on-beat colour swaps) use `ctx.audio.beat` — the upstream onset
detector beats any band threshold. Never threshold `low/mid/high` to "detect a kick," and never
smooth/EMA/normalise the bands yourself (already done upstream). Continuous reactivity uses raw
`low/mid/high`. Effects must still look good silent: `amp = base + reactive * ctx.audio.low`.

## Params (the "Effect Knobs" panel)

0–8 controls declared alongside the code; each has its own pydantic model in `schema.py`:

| `control` | extra fields | UI |
| --- | --- | --- |
| `slider` | `min, max, step?, default, unit?` | range + numeric box |
| `int_slider` | `min, max, step?, default` | integer range |
| `color` | `default` (hex) | colour picker |
| `select` | `options: [str], default` | dropdown |
| `toggle` | `default: bool` | switch |
| `palette` | `default` (a `named_palette` key) | palette dropdown + swatch |

Optional `help` string per param → hover tooltip. Slider drag → `PATCH /{slot}/params` →
`ParamStore.update` (atomic on the loop, clamped) → next render's `ctx.params.<key>` sees it.
No recompile, no LLM round-trip, no crossfade. Values also reach `effect.yaml` in batches
(`DeferredValueSaver`: one write per effect, 10 s after the first unsaved change, off the event
loop; flushed on shutdown and before any effect is loaded or rewritten) so tuning survives a
restart. A file whose values did not change is not rewritten.

## Per-layer safety nets (`runtime.py`)

- **Init budget** — `init(ctx)` >200 ms rejects the install (usually an O(N²) precompute).
- **Fence test** — 30 synthetic frames; any exception / wrong shape / non-float32 / NaN/Inf
  rejects. Hints mapped by `_diagnostic_hint` (`ctx.frames.x` vs `ctx.x`, etc.).
- **Per-frame isolation** — a `render()` that raises is logged, skipped that frame; **3
  consecutive failures** auto-disable the layer (operator can flip it back on).
- **Render watchdog** — `PER_LAYER_BUDGET_MS = 5.0` of the render thread's CPU time; p95 over
  budget for ~30 consecutive frames disables the layer ("worked then went black" ≈ over
  budget — vectorise harder).
- **`dt` clamp** — engine clamps `dt` at 2× the target frame interval so a hiccup doesn't
  teleport stateful effects.

**Performance RULE 0 (absolute):** no Python `for` loop in `render()` over pixels or particles.
Every per-pixel/per-particle calc is one vectorised numpy expression (broadcasting, masks,
`np.where`, `np.einsum`, `np.add.at`). Loops in `init()` are fine. Preallocate; stay float32.

---

# Compositions, playlist & operator UX

## Compositions, blend modes, crossfade

`Runtime.live` and `Runtime.preview` are each a `Composition`: a `list[Layer]` + a `selected`
index that the chat/param panels target. A `Layer` carries `{name, summary, source, instance,
params, blend, opacity, enabled, consecutive_failures, perf}`. Blend modes (`BLEND_MODES`):
`normal`, `add`, `screen`, `multiply`. Layers render bottom-up into a per-slot accumulator
(`_blend_into`), which then runs the master output stage.

`Runtime.render(*, wall_t, dt, t_eff, audio) → (live_buf, sim_buf)`:
- LIVE always renders. A crossfade renders the previous composition too and blends by
  `alpha = elapsed/duration` on `wall_t` (so the speed master doesn't slow promotes).
- PREVIEW renders only in **design mode**, at half FPS by default (`preview_half_rate`); a
  skipped tick's time and beat carry to its next render.
- **Master output stage** per leg: saturation pull toward Rec.709 luminance → brightness gain
  (≤1 multiplicative; >1 uses an adaptive headroom envelope tracking recent peak, 0.5 s release
  half-life) → clip `[0, 1]`. Calibration override runs *after* this stage.

Crossfades fire automatically on any LIVE mutation (`install_layer("live")`, remove, reorder,
`promote()`); duration is operator-owned `runtime.crossfade_seconds` (seeded from
`agent.default_crossfade_seconds`). PREVIEW swaps are deliberate **hard cuts** — the operator is
iterating and "did my fix work?" needs unambiguous feedback.

## Playlist (`playlist.py`) — single canonical, auto-looping

A single playlist drives the LIVE composition unattended. When started, an asyncio task walks
the entries in order, each playing `play_seconds` (min 5 s, default 120 s) before crossfading
to the next, **looping forever**; only `stop()` exits. Per-iteration errors (missing/bad
effect) are logged and skipped, never killing the task. Effects come from the on-disk library
(`config/effects/star/<name>/`); the playlist persists to `config/playlist.yaml` and survives
restarts. Out of scope (per operator): multiple playlists, jump-to-entry, shuffle.

API: `GET /playlist`, `PUT /playlist` (replace entries), `POST /playlist/start`,
`POST /playlist/stop`.

## Dual-mode UI

The landing page `/` hosts the operator UI: **Design / Live mode toggle**, simulator canvas,
per-slot composition decks (layers with blend/opacity/enabled), masters, chat panel, and the
Effect Knobs panel for the selected layer. In **design mode** the LED leg ships LIVE's
encoding while the sim leg shows PREVIEW; `transport/pause` blocks only the LED leg, `sim/pause`
only the sim leg. The simulator draws the legs to scale in a near-square box (a strip-like
layout such as the old rectangle keeps a stretched band view); tapping an LED solos it.

**Two front-ends, one API.** The desktop console (`web/index.html` + `web/lib/main-desktop.js`)
is the wide two-column layout. Phones get a dedicated portrait UI at **`/m`**
(`web/index-mobile.html` + `web/lib/main-mobile.js`): sticky header (status / DESIGN-LIVE
segmented toggle / overflow menu), LED sim with a compact audio HUD, a context action bar
(Promote/Pull in design, LIVE status in live), and a **bottom tab bar** (Layers · Knobs · Chat ·
Output) over bottom-sheets for menu/library/playlist/colour/palette. `GET /` 307-redirects
phone-class User-Agents to `/m` (iPad excluded; `?view=desktop` forces desktop, `?view=mobile`
forces mobile); the PWA manifest `start_url` is `/m`. Both front-ends share the view-agnostic
modules `web/lib/{viz,state,util,wzrd,midi}.js` and hit the identical HTTP/WS API — **keep the two `main-*.js`
in behavioural sync** (debounce windows, optimistic deck updates, chat-epoch wipe, merging
`/ws/state` messages) when changing either. `web/audio-meter.js` is an unused legacy orphan.

## Persistence & examples — three distinct directories

- `config/effects/star/` — **the library, tracked in git and shared by every device** (Mac, Pi):
  effect code, knob schema and the saved knob values, which are the shared defaults. Everything
  the operator or the LLM does in the UI (tuning a knob, save, rename, delete, star) ends up
  in files here and shows up in `git status`. Those changes are the operator's tuning: commit
  them when asked to share them, otherwise leave them alone, and never revert them to tidy
  the tree. Move library files with plain `mv` / `cp -R` and ledctl stopped, never `git mv`
  (it stages). Stop ledctl before committing a tuning session. Rewriting an effect replaces
  its saved values on every device: carry a tuned value over where the knob's `key` keeps its
  meaning. `tests/test_examples.py` installs and renders every effect on three legs and on
  four. Beside it, `config/effects/double-line/` is a frozen copy of twelve effects as they
  were on the rectangle: never loaded or edited.
- `src/ledctl/surface/examples/` — bundled **seed**: `pulse_mono`, `audio_radial`,
  `palette_wash_with_kick_sparkles`, `twin_comets_with_sparkles`, and the calibration aid
  `leg_direction` (soft balls rolling hub → tip on every leg). On every start, any seed
  missing from the library is copied in (`install_examples_if_missing()`; it never
  overwrites), so an empty library still boots and deleting one of them does not stick. A
  changed seed is written to both folders, byte-identical. A new effect goes into the
  library, not here.
- **Boot state.** LIVE comes back to what it showed before the restart: `live_state.py`
  rewrites `config/live.yaml` (gitignored runtime state: LIVE's layers as library names +
  blend/opacity/enabled, and whether the playlist was running) within ~2 s of any change,
  and boot restores it, resuming the playlist if it was running. With no file, or no
  restorable layer (an unsaved agent effect), LIVE boots `pulse_mono`; PREVIEW always does.
  Masters come from the config's `masters:` block; `PATCH /masters` with `persist: true`
  writes them but re-dumps the whole YAML, dropping its comments.
- `src/ledctl/surface/LLM_example_effects/` — the **prompt's** four examples (above).

`effect.yaml` holds `{name, summary, source: 'agent'|'user'|'bundled', created_at, updated_at,
params, param_values, starred}`. The v1 `config/presets/*.yaml` files are no longer loaded.

---

# Architecture (the stable plumbing)

**Render loop** (`engine.py`): fixed-timestep async loop at `target_fps`. Each tick computes
`effective_t += dt × speed` (`dt` clamped), builds an `AudioView` (bands pre-multiplied by
`masters.audio_reactivity`), calls `Runtime.render(...)` → `(live_rgb, sim_rgb)`, encodes to
uint8 (gamma + clip), and ships via `SplitTransport.send_now(led_frame=…, sim_frame=…)`: DDP
first, then the browser preview is handed to a per-client mailbox. **The tick never awaits a
network peer, and request handlers keep file I/O, YAML and effect compiles off the event loop**
(see `docs/reference/render-performance.md`). Frames drop rather than spiral on lag. The loop
sleeps until the next deadline OR an audio-kick event (`engine.kick_audio()`, fired by onset
packets only), gated by `kick_min_interval`. Frame timings and Pi health (temperature, CPU
clock, governor, throttling) are the `perf` key of `/state`, shown in the fps pill (desktop)
and the Output tab (mobile).

**Key modules:**
- `topology.py` — spatial model of every LED from the config's strip geometries (`line` or
  `spoke`); positions normalised about the origin with one scale; derives the named
  `ctx.frames.*` arrays. See `docs/reference/rig-geometry.md`.
- `masters.py` — operator-owned `MasterControls` (`brightness ∈ [0,2]`, `speed`,
  `audio_reactivity`, `saturation`, `audio_swell` = brightness follows loudness, 0 = off, and
  the master effects `sparkle`, `glitch`, `strobe`, `trails`, 0 = off; ranges in `BOUNDS`);
  the first five are surfaced to effects read-only as `ctx.masters`.
- `surface/` — the v2 runtime (see above). Base classes in `base.py`; sandbox in `sandbox.py`;
  param schema in `schema.py`; tool handler in `tool.py`; prompt in `prompt.py`; on-disk CRUD
  in `persistence.py`.
- `pixelbuffer.py` — float32 working buffer in `[0, 1]`; gamma applied once here, not in WLED.
- `transports/` — `simulator` (WebSocket), `ddp` (UDP to WLED, 480 px/packet, PUSH on final
  packet only), and `split.py` (`SplitTransport` = one sim leg + 0-or-1 DDP leg).
- `fanout.py` — `LatestFanout`, the per-client mailbox behind both WebSockets: a slow browser
  skips to the newest message instead of holding up the sender.
- `audio/` — thin bridge to the external [Realtime_PyAudio_FFT](https://github.com/Jaymon/Realtime_PyAudio_FFT)
  server (spawned as a subprocess); `state.py` holds `AudioState`, `bridge.py` is the OSC
  listener + supervisor. Soft-fail everywhere: no audio → bands 0, bpm fallback, render keeps
  going. **The LED controller never captures audio itself** — pick device / tune bands at the
  audio-server's own UI (`http://127.0.0.1:8766`, the 'audio' link in the operator UI).
- `wzrd/` — optional WZRD input (wall colours, level and masters from a projection tool, applied
  through `Runtime.output_hook`); `osc.py` holds what its listener shares with the audio one.
  See `docs/reference/wzrd-input.md`.
- `midi/` — MIDI control: a sender on the Mac (WZRD's LEDs mode, or `scripts/midi_relay.py`)
  forwards the Akai APC mini's raw messages over `/ws/midi`; ledctl maps them to effects,
  masters and actions and sends the pad lights back. See `docs/reference/midi-input.md`.
- `agent/` — `session.py` (in-memory `SessionStore`, `history_max`-capped buffer, rate limit,
  `reset_all_buffers()`) + `client.py` (lazy OpenAI-compatible OpenRouter wrapper).
- `api/server.py` — FastAPI app + the dual-mode operator UI. `api/auth.py` — optional
  shared-password gate (off in dev). `api/agent.py` — `POST /agent/chat` etc.

**Config** (`config.py`, pydantic): duplicate strip IDs, overlapping pixel ranges, and
over-capacity are caught at startup. `config/config.dev.yaml` (Mac, simulator, no auth) vs
`config/config.pi.yaml` (Pi, ddp/multi, `auth.password_env: LEDCTL_PASSWORD`; the value lives in the gitignored
`.env`, never in a tracked file: this repo is public). Swap production vs dev by
changing `transport.mode`; everything above the transport layer is identical.

## API surface (operator endpoints)

- **Library:** `GET /effects`, `POST /effects/{name}/{load_preview,load_live,star,rename}`,
  `DELETE /effects/{name}`, `POST /preview/save`, `GET /palettes`.
- **Composition (per slot ∈ {preview, live}):** `PATCH /{slot}/params`, `POST /{slot}/select`,
  `POST /{slot}/layer/{remove,reorder}`, `PATCH /{slot}/layer/blend`.
- **Live control:** `POST /promote` (crossfade live ← preview), `POST /pull_live_to_preview`
  (hard cut), `POST /mode`, `GET /active`.
- **Playlist:** `GET/PUT /playlist`, `POST /playlist/{start,stop}`.
- **Output:** `GET/PATCH /masters` (PATCH supports `persist: true` write-back to YAML),
  `POST /{blackout,resume}`, `GET /transport` + `POST /transport/{pause,resume}` (DDP only),
  `POST /sim/{pause,resume}`, `PATCH /sim/fps`, `PATCH /engine/fps`, `POST /system/reboot`.
- **Diagnostics/setup:** `GET /state` (full snapshot), `GET /topology`, `GET /healthz`,
  `GET/PUT /config` (PUT hot-swaps topology), `POST /calibration/{solo,walk,stop}`,
  `GET /audio/{state,ui}`.
- **WZRD input:** `GET/PATCH /wzrd` (settings + link status; also the `wzrd` key in `/state`).
- **MIDI control:** `GET /midi`, `PUT /midi/bindings`, `POST /midi/reset`, WebSocket `/ws/midi`
  (also the `midi` key in `/state`).
- **Agent:** `POST /agent/chat`, `GET/DELETE /agent/sessions/{id}`, `GET/PATCH /agent/config`.
- **WebSockets:** `/ws/frames` (sim canvas), `/ws/state` (UI panels) — both honour `?password=`.
  `/ws/state` sends the compositions (`live`, `preview`) only on connect, after a mutating
  request and every 0.5 s; the pushes in between omit them, so a client **merges** each message
  into its state rather than replacing it.

---

# Hardware & on-site operations

**⚠️ Pending, do at the next on-site setup (the Pi wasn't available to do it on 2026-11-16):**
the Gledopto's WLED Wi-Fi settings were changed (a phone hotspot was entered) while trying to
drive it from the Mac. As soon as the Pi is back on the ethernet cable, clear them and verify DDP
per the undo checklist in `Mac_direct_setup.md`, then delete this line.

**Updating the Pi.** The Pi's repo talks to GitHub over HTTPS with a personal access token
limited to this repo (git's `store` helper, `~/.git-credentials`, mode 600), so plain
`git pull` and `git push` work. Pull with ledctl stopped, as for any library change, then
refresh dependencies:
```bash
git pull --ff-only
~/.local/bin/uv pip install -q -e .
```
The Pi's `.env` holds `LEDCTL_PASSWORD` and `OPENROUTER_API_KEY`; without the password ledctl
refuses to start.

Physical build detail (gear, power, soldering, WS2815 backup-data line) is in
`Hardware_setup.md`. Phone/network access routes are in `reach_the_pi.md`. The essentials:

**Topology.** Pi (`10.0.0.1`) ↔ Gledopto (`10.0.0.2`) over a 15 m ethernet cable; **DDP frames
flow over ethernet only, never WiFi.** Keep WiFi off on the Gledopto (its "Static IP" is shared
between ethernet and WiFi). The Pi joins the phone hotspot ("Xander's Pixel") for
browser UIs, Tailscale, and SSH only. The Gledopto runs **WLED-MoonModules** (AudioReactive
fork), and must be **GRB** colour order for WS2815.

**Services on the Pi** (all enabled, autostart on reboot): `ledctl.service` (`:8000`, DDP +
spawns audio-server), `wled-proxy` (`:8080` → WLED UI), audio-server subprocess (`:8765` WS +
`:8766` UI), `tailscaled`. Browser URLs: ledctl `:8000`, WLED `:8080`, audio FFT `:8766` on the
LAN; via tailnet `****` ports 8443 / 443 / 10000 respectively. `gledopto-reboot.service`
fires `curl http://10.0.0.2/reset` 8 s after ledctl starts so WLED enters realtime override
cleanly on every boot (works around the WLED-MM realtime-intake wedge).

**Remote resilience (unattended installs only).** `deploy/net-watchdog.{sh,service}` reboots
the Pi after 10 minutes without internet and meanwhile re-nudges NetworkManager onto WiFi;
install steps, the site-specific `WIFI_CONN` and the infinite-retry autoconnect settings are in
`reach_the_pi.md` §6. **Keep it stopped at an attended gig** on a phone hotspot or any uplink
that may drop, or it reboots the Pi mid-show. Before leaving a Pi unattended, confirm the
site's WiFi is a stored network and survives a reboot: the Pi knows only the phone hotspot
plus whatever was added on site. Not done, as far as the repo records: the hardware watchdog
(`RuntimeWatchdogSec` in `/etc/systemd/system.conf`, for a fully frozen kernel) and a
phone-controllable smart plug for a remote power-cycle.

**Strip mapping.** All four WLED outputs: WS281x, RGB, length 450, Reversed OFF; the Gledopto
is not reconfigured per layout. In ledctl each strip is one leg, `reversed: false`, logical
pixel 0 at the hub: `leg_1` at `pixel_offset` 0, `leg_2` at 450, `leg_3` at 900, `leg_4` at 1350.
The dev config enables all four; `config.pi.yaml` comments out `leg_3` (output 3 is not wired
on site; a strip on an output ledctl does not drive keeps showing WLED's own preset). **The
target setup for the gig is four legs:** wire output 3, uncomment `leg_3`, restart ledctl. No
effect needs rewriting: every library effect reads `ctx.rig.n_legs` / `ctx.frames.leg`, and
`tests/test_examples.py` renders each one on three and four legs. The
four `angle_deg` values (90/180/270/0) are **placeholders, not yet
calibrated against a real floor**: which physical leg each pixel range is has to be found on
site with `/calibration/solo` (runbook in `docs/reference/rig-geometry.md`).

**DDP debug recipe** when LEDs show WLED's own preset instead of ledctl content:
0. `ip -br addr show eth0` — must read `UP` with `10.0.0.1/24`. `NO-CARRIER`/`DOWN` (and no
   address) = the ethernet cable is unplugged or dead; nothing downstream matters. **Check this
   first: `frames_sent` climbs happily into a dead interface, so step 1 does *not* prove a link.**
   Re-seating the cable is enough — NetworkManager reattaches `10.0.0.1` with no reboot — but the
   Gledopto will still need a manual `curl http://10.0.0.2/reset`, because `gledopto-reboot.service`
   already fired 8 s after ledctl started, i.e. while the link was still down.
   `UP` with **no address** (carrier present, `nmcli dev status` shows eth0 `disconnected`) =
   the static profile is missing or inactive and NetworkManager's DHCP fallback "Wired
   connection 1" keeps timing out. The profile is `gledopto-eth` (recreated 2026-11-19):
   `sudo nmcli con up gledopto-eth`, or if `nmcli con show` lacks it:
   `sudo nmcli con add type ethernet ifname eth0 con-name gledopto-eth ipv4.method manual
   ipv4.addresses 10.0.0.1/24 ipv4.never-default yes ipv6.method disabled
   connection.autoconnect-priority 100`. Then `curl http://10.0.0.2/reset`.
1. `curl 'localhost:8000/transport?password=CHANGE_ME'` — confirm `frames_sent` is climbing.
2. `sudo tcpdump -i any -n 'host 10.0.0.2 and udp port 4048' -c 20` — confirm packets leave.
3. `ping -c 3 10.0.0.2` (`arp` isn't installed on the Pi; use `ip neigh` if you need the MAC).
4. `curl -s http://10.0.0.2/json/info` — want `"live": true, "lip": "10.0.0.1", "lm": "DDP"`
   (same thing the WLED Info page shows as "Realtime: DDP, IP: 10.0.0.1"). **Absence with packets
   arriving = wedged realtime intake → REBOOT THE GLEDOPTO before chasing config** (full power
   cycle has fixed this on-site with no settings changed). Only if a reboot doesn't help:
   WLED-MM **Sync → Receive UDP Realtime** ON, LED **Length** 1800, a Segment 0 spanning
   0–1799, PC Mode OFF.

The `/transport/{pause,resume}` flag stops DDP while keeping the simulator live; after WLED's
~2.5 s realtime timeout the Gledopto resumes its own preset — instant A/B between "Pi drives"
and "Gledopto drives" with no restart.

**Coordinate convention.** Origin = the Gledopto (the hub). x-y = the plane the legs lie in,
which the simulator draws (a floor rig is seen from above; point `+y` at the projection wall);
z = height off that plane. `ctx.pos` is metres divided by the distance of the farthest LED, one
scale for all axes; `ctx.frames.x/y/z` are the same data rescaled to `[0, 1]`.

## Deploy artefacts

- `deploy/ledctl.service` — systemd unit **template** (`Restart=always`, `Nice=-10`, `audio`
  group, `ProtectSystem=full`; real-time scheduling is in the file commented out, untested on
  the Pi). It ships `User=pi` / `/home/pi/...` paths; the copy installed on the Pi at
  `/etc/systemd/system/ledctl.service` is patched to `User=xander` and the real layout
  (`/home/xander/audio_LED/Animated_LED/`), so apply later template changes to that copy by
  hand rather than overwriting it.
- `deploy/{gledopto-reboot,wled-proxy}.service` + `wled-proxy.socket`.
- `deploy/cpu-performance.service` — pins the CPU governor to `performance` at boot (the
  default `ondemand` clocks the Pi down between frames).
- `.env.example` → copy to `.env` (gitignored) for the OpenRouter key.
- Pi gotchas: `audio_server.command` must be an **absolute path** (systemd has no user PATH);
  the venv needs `python-osc` (`uv pip install python-osc`); the audio-server's own
  `configs/main.yaml` needs `ws.host: 0.0.0.0` for tailnet reach.
