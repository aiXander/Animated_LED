# CLAUDE.md

Guidance for Claude Code working in this repo. The hard plumbing (transport, topology,
audio bridge, auth, deploy) is **done and stable** — most future work is **UX / LLM
interface / effects**, so this file leans into those. Deep hardware/network detail lives in
sibling docs (`Hardware_setup.md`, `reach_the_pi.md`); only the operationally-critical bits
are kept here.

## What this is

A real-time controller for an **1800-LED festival install** (4 × 450 WS2815 strips on a metal
scaffolding rectangle, centre-fed via DDP into a Gledopto/WLED controller). The defining idea
of the current design ("surface v2", now on `main`): **the LLM authors a Python `Effect`
subclass per chat turn**, and the operator stacks effects Resolume-style into a *composition*
and crossfades them live. There is no typed primitive graph any more.

**Which device am I on?** Dev happens on the Mac (transport=simulator); production runs on the
Pi (transport=ddp/multi). Several Claude instances may run at once — always check terminal /
logs / hostname to know whether you're on the Pi or the Mac before acting.

## Commands

```bash
# Setup
uv venv --python 3.11
uv pip install -e ".[dev]"

# Run (Mac dev — simulator at http://127.0.0.1:8000, OpenAPI at /docs)
uv run ledctl run --config config/config.dev.yaml
uv run ledctl show-config --config config/config.dev.yaml   # inspect parsed config

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
(`agent.model`, currently `google/gemini-3.1-flash-lite-preview`).

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
5. on success: install into the selected preview layer + persist to `config/effects/<slug>/`

On failure at any stage the tool result is `{ok: false, error, details}` with a diagnostic
hint; the next turn's prompt embeds it under **LAST EFFECT ERROR** so the LLM self-corrects.
Up to `agent.retry_on_tool_error` (=2) auto-retries happen inside one chat turn before the
failure surfaces to the operator.

**Param auto-merge.** When a new layer installs, operator-tuned `param_values` for any `key`
that matches the prior layer's schema carry forward. So "regenerate but keep my colour" is
mechanical — the LLM is told this and picks new keys deliberately when a knob's meaning
changes.

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
ROLE → TOOL → PHYSICAL RIG → INSTALL (live from topology) → COORDINATE FRAMES →
AUDIO INPUT → EFFECT CONTRACT → RUNTIME API → PARAM SCHEMA → PERFORMANCE →
ANTI-PATTERNS → EXAMPLE EFFECTS → CURRENT PREVIEW LAYER → [LAST EFFECT ERROR]
```

- **CURRENT PREVIEW LAYER** shows *only the selected preview layer's* source + param schema +
  current values. Other preview layers and all of LIVE are deliberately hidden — showing them
  tempts the LLM to "preserve" surrounding state instead of authoring its own layer cleanly.
- **EXAMPLE EFFECTS** are loaded verbatim from `src/ledctl/surface/LLM_example_effects/*/effect.py`
  on every build (drop a new `effect.py` there → it shows up next turn). These are **separate**
  from the `surface/examples/` library seed (below) and live outside `config/effects/` so the
  operator can't delete them from the library UI and break the prompt.

**Token-budget reminder.** The prose blocks (`PHYSICAL_RIG`, `EFFECT_CONTRACT`,
`PERFORMANCE_RULES`, `ANTI_PATTERNS`), the example effects, and per-field `help` strings are
the dominant cost. Keep them tight when editing.

---

# Effects

A *layer* is one Python `Effect` subclass (LLM-authored, or hand-written and saved). Each is
its own module under `config/effects/<slug>/{effect.py, effect.yaml}` — real Python, diffable
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
| `ctx.pos` | `(N, 3)` float32 in `[-1, 1]` |
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
builtin), and constants `PI`, `TAU`, `LUT_SIZE` (=256), `PALETTE_NAMES`. Most math helpers
accept an `out=` for allocation-free writes.

**Sandbox** (`sandbox.py`): no `import`, `eval`, `exec`, `open`, `getattr/setattr`, `globals`,
`print`; AST rejects every dunder attribute except `__name__`; source ≤8 KB; exactly one
`Effect` subclass. Threat model is "LLM typo," not a malicious operator.

## Coordinate frames (`surface/frames.py`)

Effects address named per-LED scalars via `ctx.frames.<name>`; `build_system_prompt` emits the
full list with one-line descriptions from `FRAME_DESCRIPTIONS`. Cartesian (`x/y/z`,
`signed_x/y/z`), radial (`radius`, `angle`), row masks (`side_top/_bottom/_signed`), per-strip
(`strip_id`, `chain_index`), and the headline frame **`u_loop`** — a clockwise arc-position
around the rectangle in `[0, 1]` from top-centre. Motion along `u_loop` reads as motion *around
the rig* regardless of strip count or reversal.

**Geometry caveat the prompt hammers:** this is **not a 2D plane** — every LED has `y ∈ {+1, -1}`,
none in between. 2D particle physics that lets pixels roam `[-1, 1]²` mostly fades to black.
Pin `y` to ±1 and drive motion along `x`, or work 1D on `u_loop` / `chain_index` / `signed_x`.

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
No recompile, no LLM round-trip, no crossfade. Each patch also persists via
`EffectStore.save_values(...)` so tuning survives a restart.

## Per-layer safety nets (`runtime.py`)

- **Init budget** — `init(ctx)` >200 ms rejects the install (usually an O(N²) precompute).
- **Fence test** — 30 synthetic frames; any exception / wrong shape / non-float32 / NaN/Inf
  rejects. Hints mapped by `_diagnostic_hint` (`ctx.frames.x` vs `ctx.x`, etc.).
- **Per-frame isolation** — a `render()` that raises is logged, skipped that frame; **3
  consecutive failures** auto-disable the layer (operator can flip it back on).
- **Render watchdog** — `PER_LAYER_BUDGET_MS = 5.0`; p95 over budget for ~30 consecutive frames
  disables the layer ("worked then went black" ≈ over budget — vectorise harder).
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
- PREVIEW renders only in **design mode**, at half FPS by default (`preview_half_rate`).
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
(`config/effects/<name>/`); the playlist persists to `config/playlist.yaml` and survives
restarts. Out of scope (per operator): multiple playlists, jump-to-entry, shuffle.

API: `GET /playlist`, `PUT /playlist` (replace entries), `POST /playlist/start`,
`POST /playlist/stop`.

## Dual-mode UI

The landing page `/` hosts the operator UI: **Design / Live mode toggle**, simulator canvas,
per-slot composition decks (layers with blend/opacity/enabled), masters, chat panel, and the
Effect Knobs panel for the selected layer. In **design mode** the LED leg ships LIVE's
encoding while the sim leg shows PREVIEW; `transport/pause` blocks only the LED leg, `sim/pause`
only the sim leg.

**Two front-ends, one API.** The desktop console (`web/index.html` + `web/lib/main-desktop.js`)
is the wide two-column layout. Phones get a dedicated portrait UI at **`/m`**
(`web/index-mobile.html` + `web/lib/main-mobile.js`): sticky header (status / DESIGN-LIVE
segmented toggle / overflow menu), LED sim strip with a compact audio HUD, a context action bar
(Promote/Pull in design, LIVE status in live), and a **bottom tab bar** (Layers · Knobs · Chat ·
Output) over bottom-sheets for menu/library/playlist/colour/palette. `GET /` 307-redirects
phone-class User-Agents to `/m` (iPad excluded; `?view=desktop` forces desktop, `?view=mobile`
forces mobile); the PWA manifest `start_url` is `/m`. Both front-ends share the view-agnostic
modules `web/lib/{viz,state,util}.js` and hit the identical HTTP/WS API — **keep the two `main-*.js`
in behavioural sync** (debounce windows, optimistic deck updates, chat-epoch wipe) when changing
either. `web/audio-meter.js` is an unused legacy orphan.

## Persistence & examples — two distinct directories

- `src/ledctl/surface/examples/` — bundled **library seed**: `pulse_mono`, `audio_radial`,
  `palette_wash_with_kick_sparkles`, `twin_comets_with_sparkles`. Copied into `config/effects/`
  on first boot (`install_examples_if_missing()`). On boot the runtime loads `pulse_mono` into
  both slots; if that fails the slot stays empty (implicit black layer).
- `src/ledctl/surface/LLM_example_effects/` — **prompt** gold-standard sources shown verbatim
  to the LLM (`fluid_strobe_nebula`, `rainbow_comet`, `twin_comets_with_sparkles`). Never copied
  to the library; can't be deleted from the UI.

On disk each saved effect is `config/effects/<slug>/effect.py` + `effect.yaml` (`{name, summary,
source: 'agent'|'user', created_at, updated_at, params, param_values, starred}`). The v1
`config/presets/*.yaml` files are **no longer loaded** — left on disk for reference only.

---

# Architecture (the stable plumbing)

**Render loop** (`engine.py`): fixed-timestep async loop at `target_fps`. Each tick computes
`effective_t += dt × speed` (`dt` clamped), builds an `AudioView` (bands pre-multiplied by
`masters.audio_reactivity`), calls `Runtime.render(...)` → `(live_rgb, sim_rgb)`, encodes to
uint8 (gamma + clip), and ships via `SplitTransport.send(led_frame=…, sim_frame=…)`. Frames
drop rather than spiral on lag. The loop sleeps until the next deadline OR an audio-kick event
(`engine.kick_audio()`), gated by `kick_min_interval`.

**Key modules:**
- `topology.py` — spatial model of all 1800 LEDs; normalised positions in `[-1, 1]` per axis
  from `config` strip geometries; derives the named `ctx.frames.*` arrays.
- `masters.py` — operator-owned `MasterControls` (`brightness ∈ [0,2]`, `speed`,
  `audio_reactivity`, `saturation`); surfaced to effects read-only as `ctx.masters`.
- `surface/` — the v2 runtime (see above). Base classes in `base.py`; sandbox in `sandbox.py`;
  param schema in `schema.py`; tool handler in `tool.py`; prompt in `prompt.py`; on-disk CRUD
  in `persistence.py`.
- `pixelbuffer.py` — float32 working buffer in `[0, 1]`; gamma applied once here, not in WLED.
- `transports/` — `simulator` (WebSocket), `ddp` (UDP to WLED, 480 px/packet, PUSH on final
  packet only), and `split.py` (`SplitTransport` = one sim leg + 0-or-1 DDP leg).
- `audio/` — thin bridge to the external [Realtime_PyAudio_FFT](https://github.com/Jaymon/Realtime_PyAudio_FFT)
  server (spawned as a subprocess); `state.py` holds `AudioState`, `bridge.py` is the OSC
  listener + supervisor. Soft-fail everywhere: no audio → bands 0, bpm fallback, render keeps
  going. **The LED controller never captures audio itself** — pick device / tune bands at the
  audio-server's own UI (`http://127.0.0.1:8766`, the 'audio' link in the operator UI).
- `agent/` — `session.py` (in-memory `SessionStore`, `history_max`-capped buffer, rate limit,
  `reset_all_buffers()`) + `client.py` (lazy OpenAI-compatible OpenRouter wrapper).
- `api/server.py` — FastAPI app + the dual-mode operator UI. `api/auth.py` — optional
  shared-password gate (off in dev). `api/agent.py` — `POST /agent/chat` etc.

**Config** (`config.py`, pydantic): duplicate strip IDs, overlapping pixel ranges, and
over-capacity are caught at startup. `config/config.dev.yaml` (Mac, simulator, no auth) vs
`config/config.pi.yaml` (Pi, ddp/multi, `auth.password: kaailed`). Swap production vs dev by
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
- **Agent:** `POST /agent/chat`, `GET/DELETE /agent/sessions/{id}`, `GET/PATCH /agent/config`.
- **WebSockets:** `/ws/frames` (sim canvas), `/ws/state` (UI panels) — both honour `?password=`.

---

# Hardware & on-site operations

Physical build detail (gear, power, soldering, WS2815 backup-data line) is in
`Hardware_setup.md`. Phone/network access routes are in `reach_the_pi.md`. The essentials:

**Topology.** Pi (`10.0.0.1`) ↔ Gledopto (`10.0.0.2`) over a 15 m ethernet cable; **DDP frames
flow over ethernet only, never WiFi.** Keep WiFi off on the Gledopto (its "Static IP" is shared
between ethernet and WiFi). The Pi joins the phone hotspot ("Xander's Pixel" / `xanderwifi`) for
browser UIs, Tailscale, and SSH only. The Gledopto runs **WLED-MoonModules** (AudioReactive
fork), and must be **GRB** colour order for WS2815.

**Services on the Pi** (all enabled, autostart on reboot): `ledctl.service` (`:8000`, DDP +
spawns audio-server), `wled-proxy` (`:8080` → WLED UI), audio-server subprocess (`:8765` WS +
`:8766` UI), `tailscaled`. Browser URLs: ledctl `:8000`, WLED `:8080`, audio FFT `:8766` on the
LAN; via tailnet `xanderpi.tail182af2.ts.net` ports 8443 / 443 / 10000 respectively. `gledopto-reboot.service`
fires `curl http://10.0.0.2/reset` 8 s after ledctl starts so WLED enters realtime override
cleanly on every boot (works around the WLED-MM realtime-intake wedge).

**Strip mapping** (verified 1:1 with the operator UI via `/calibration/solo`; don't edit either
side without re-running the calibration sequence). All four WLED outputs: WS281x, RGB, length
450, Reversed OFF. ledctl strips all `reversed: false`, all wired controller-at-centre
(logical pixel 0 = centre):

| ledctl `id` | `pixel_offset` | physical quadrant |
| --- | --- | --- |
| top_right | 0 | top-right |
| bottom_right | 450 | bottom-right |
| bottom_left | 900 | bottom-left |
| top_left | 1350 | top-left |

**DDP debug recipe** when LEDs show WLED's own preset instead of ledctl content:
1. `curl 'localhost:8000/transport?password=kaailed'` — confirm `frames_sent` is climbing.
2. `sudo tcpdump -i any -n 'host 10.0.0.2 and udp port 4048' -c 20` — confirm packets leave.
3. `ping -c 3 10.0.0.2` + `arp -n | grep 10.0.0.2` — must resolve a MAC, not "(incomplete)".
4. Check WLED Info page for a "Realtime: DDP, IP: 10.0.0.1" line. **Absence with packets
   arriving = wedged realtime intake → REBOOT THE GLEDOPTO before chasing config** (full power
   cycle has fixed this on-site with no settings changed). Only if a reboot doesn't help:
   WLED-MM **Sync → Receive UDP Realtime** ON, LED **Length** 1800, a Segment 0 spanning
   0–1799, PC Mode OFF.

The `/transport/{pause,resume}` flag stops DDP while keeping the simulator live; after WLED's
~2.5 s realtime timeout the Gledopto resumes its own preset — instant A/B between "Pi drives"
and "Gledopto drives" with no restart.

**Coordinate convention.** Right-handed: `+x` = stage-right, `+y` = up, `+z` = toward audience.
Origin = centre of scaffolding. `ctx.pos` normalised so each axis ∈ `[-1, 1]`;
`ctx.frames.x/y/z` are the same data rescaled to `[0, 1]`.

## Deploy artefacts

- `deploy/ledctl.service` — systemd unit (`Restart=always`, `Nice=-5`, `audio` group,
  `ProtectSystem=full`). The template ships `User=pi` / `/home/pi/...` paths; on the actual Pi
  patch to `User=xander` and the real layout (`/home/xander/audio_LED/Animated_LED/`).
- `deploy/{gledopto-reboot,wled-proxy}.service` + `wled-proxy.socket`.
- `.env.example` → copy to `.env` (gitignored) for the OpenRouter key.
- Pi gotchas: `audio_server.command` must be an **absolute path** (systemd has no user PATH);
  the venv needs `python-osc` (`uv pip install python-osc`); the audio-server's own
  `configs/main.yaml` needs `ws.host: 0.0.0.0` for tailnet reach.
