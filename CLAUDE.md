# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code / hardware setup in this repository.

## Current hardware setup:
We are on-site, there is no fixed local wifi network yet (never has been active) so I'm using my phone's hotspot.
network name: "Xander's Pixel", password: xanderwifi

Both the Pi and the Gledopto know this network (added in settings).

My Pi (hardcoded to 10.0.0.1) is connected to gledopto (hardcoded to 10.0.0.2) via a 15m ethernet cable.
I am using my macbook to SSH into the Pi since I don't have an external keyboard / screen.
My macbook is thus my command centre.

I will use several Claude Code instances for dev / debugging so always try to see from terminal / logs / ... on which device you currently are (either the Pi or the macbook).

All the LEDs have been physically connected (3 x 150 LEDs on all 4 quadrants).

## Network access cheatsheet (current state, 2026-05-08)

**Topology.** Gledopto/WLED is reachable only through the Pi, on the **ethernet-only** static IP `10.0.0.2` (the WLED "Static IP" field is shared between ethernet and WiFi — keep WiFi off on the Gledopto so the ethernet leg the DDP render path depends on stays intact). The Pi joins the phone hotspot for everything else (browser UIs, Tailscale, SSH).

**Service map on the Pi.** All four services are `enabled` and autostart on every reboot:

| Service                 | Listens on             | Talks to                                  |
| ----------------------- | ---------------------- | ----------------------------------------- |
| `ledctl.service`        | `0.0.0.0:8000`         | DDP→Gledopto on ethernet; spawns audio-server |
| `wled-proxy.{socket,service}` | `0.0.0.0:8080`   | reverse-proxies to `10.0.0.2:80` (WLED UI) |
| `audio-server` (subprocess of ledctl) | `0.0.0.0:8765` (WS) + `0.0.0.0:8766` (HTTP UI) + sends OSC to `127.0.0.1:9000` | USB mic via ALSA |
| `tailscaled.service`    | WireGuard              | tailnet (`xanderpi.tail182af2.ts.net`)    |

**Browser URLs.**

| What                | LAN (hotspot)                                      | Tailnet (anywhere)                                  |
| ------------------- | -------------------------------------------------- | --------------------------------------------------- |
| ledctl operator UI  | `http://xanderpi.local:8000` / `http://<pi-ip>:8000` | `https://xanderpi.tail182af2.ts.net:8443/`          |
| WLED UI             | `http://xanderpi.local:8080` / `http://<pi-ip>:8080` | `https://xanderpi.tail182af2.ts.net/` (port 443)    |
| Audio-server FFT UI | `http://xanderpi.local:8766` / `http://<pi-ip>:8766` | `https://xanderpi.tail182af2.ts.net:10000/`         |

`xanderpi.local` (mDNS) only resolves in Safari/Firefox/`curl` on the Mac. Chrome bypasses macOS mDNS — use the IP or the tailnet URL there. Hotspot IPs are dynamic; re-resolve via `dscacheutil -q host -a name XanderPi.local` from the Mac.

**Tailscale serve.** Same Tailscale account (`mlpaperchannel@`) on Mac + Pi. Three HTTPS mounts active (`tailscale serve` only allows ports 443 / 8443 / 10000 — that's why ports are remapped on the tailnet side): WLED→443, ledctl→8443, audio-UI→10000. Configs persist in `/var/lib/tailscale/`, so `tailscale serve` resumes automatically after reboot.

**Pi access from the Mac.**
- Passwordless: Mac's `~/.ssh/id_ed25519.pub` is in the Pi's `~/.ssh/authorized_keys` — `ssh xander@XanderPi.local` just works.
- Pixel hotspot client isolation: if `.local` stops resolving and devices can't see each other, restart the hotspot or switch its security to **WPA2-Personal** with **2.4 GHz / Extend compatibility ON**. The ESP32 in the Gledopto is 2.4 GHz only and won't even list a 5 GHz-only hotspot in its scan.

**Festival-day rule.** DDP frames flow Pi → Gledopto over the ethernet cable, never over WiFi. WiFi / Tailscale is browser UI + SSH only.

**Clean-Pi install notes (for future re-deploys).**
- Repo lives at `/home/xander/audio_LED/Animated_LED/`, with `Realtime_PyAudio_FFT` as a sibling at `/home/xander/audio_LED/Realtime_PyAudio_FFT/`. The canonical `deploy/ledctl.service` template uses `User=pi` and `/home/pi/animated_LED/` paths — patch `User=xander` and the three paths to the actual layout above before installing under `/etc/systemd/system/`.
- `config.pi.yaml` `audio_server.command` must be the **absolute path** to the `audio-server` binary (`/home/xander/.local/bin/audio-server`) — systemd doesn't inherit the user shell's PATH, so a bare `"audio-server"` won't resolve under the unit.
- ledctl's venv at `/home/xander/audio_LED/Animated_LED/.venv` needs `python-osc` for the OSC listener to bind. Install with `cd <repo> && /home/xander/.local/bin/uv pip install python-osc` (the venv has no `pip` of its own; PEP 668 blocks system `pip3`).
- Audio-server's `Realtime_PyAudio_FFT/configs/main.yaml` must have `ws.host: 0.0.0.0` so the FFT UI / WebSocket are reachable from the tailnet. Leave the OSC destination on `127.0.0.1` — both processes are on the same Pi.

## Commands

```bash
# Setup
uv venv --python 3.11
uv pip install -e ".[dev]"

# Run server
cd /Users/xandersteenbrugge/Documents/Projects/animated_LED
.venv/bin/ledctl run --config config/config.dev.yaml
# Simulator at http://127.0.0.1:8000, OpenAPI at http://127.0.0.1:8000/docs

# Inspect parsed config
.venv/bin/ledctl show-config --config config/config.dev.yaml

# Tests
.venv/bin/pytest
.venv/bin/pytest -v tests/test_runtime.py            # single file
.venv/bin/pytest tests/test_examples.py::test_pulse_mono_runs  # single test

# Lint
.venv/bin/ruff check src tests
```

## Architecture

The system is a real-time LED controller for an 1800-LED festival install (4 × 450 WS2815 strips on metal scaffolding, centre-fed via DDP into a Gledopto/WLED controller).

The big shift in **surface v2 (the current branch)** is that effects are no longer composed from a typed primitive graph; the LLM authors a Python `Effect` subclass per chat turn and the operator stacks them Resolume-style into a *composition*. See `surface_v2_design_plan.md` for the long-form rationale and `surface_v2_phased_plan.md` for the v1/v1.1 cuts. The summary below describes the code as it actually exists today.

**Render loop** (`engine.py`): fixed-timestep async loop at `target_fps`. Each tick — compute `wall_t / dt / effective_t` (`effective_t += dt × speed`; `dt` is clamped at 2× the target frame interval so a hiccup doesn't tele-port stateful effects), build an `AudioView` (each band already pre-multiplied by `masters.audio_reactivity` here in the engine; the LLM uses raw `ctx.audio.low/mid/high`), call `Runtime.render(...)` → `(live_rgb, sim_rgb)` float32, copy each into a `PixelBuffer`, encode to uint8 (gamma + clip), and ship via `SplitTransport.send(led_frame=…, sim_frame=…)`. Frames drop rather than spiral on lag — same as v1. The render loop sleeps until either the next deadline OR an audio-kick event (`engine.kick_audio()` from the OSC listener) — gated by a `kick_min_interval` so beats can't unpace the loop.

**Key modules:**
- `topology.py` — Spatial model of all 1800 LEDs. Normalised positions (each axis in [-1, 1]) derived from `config.yaml` strip geometries. Effects address LEDs through `ctx.pos` (the (N, 3) float32 array) and the named frames in `ctx.frames.*`.
- `masters.py` — Operator-owned `MasterControls` (`brightness`, `speed`, `audio_reactivity`, `saturation`). Deliberately invisible to LLM-authored effects as writes — surfaced as `ctx.masters` (a frozen `MastersView`, read-only). `audio_reactivity` is the single global attenuator applied once in `engine._build_audio_view()` before the bands reach the effect. `brightness` ∈ [0, 2]; values >1 use an adaptive headroom envelope (recent peak + release half-life) so "louder than 1" doesn't just clip immediately.
- `surface/` — The whole new runtime; see "Surface v2" below.
- `pixelbuffer.py` — Float32 working buffer in [0, 1]. Gamma (default 2.2) applied once here, not in WLED.
- `transports/` — Pluggable output. `simulator` (WebSocket to browser), `ddp` (UDP to WLED, 480 px/packet, PUSH on the final packet only), and `split.py` — `SplitTransport` owns one sim leg + zero-or-one DDP leg and exposes `send(*, led_frame, sim_frame)`. In live mode the engine passes the same uint8 bytes to both legs; in design mode the LED leg gets the LIVE composition's encoding while the sim leg gets the PREVIEW. `transport/pause` only blocks the LED leg; `sim/pause` only blocks the sim leg. (`multi.py` from v1 was deleted — this is its replacement.)
- `audio/` — Thin bridge to the external [Realtime_PyAudio_FFT](https://github.com/Jaymon/Realtime_PyAudio_FFT) audio-feature server. The LED controller does **not** capture or analyse audio itself — it spawns the audio-server as a subprocess and consumes its OSC feed:
  - `state.py` — `AudioState` holds the latest `low/mid/high` band energies (already auto-scaled to ~[0, 1] on the audio server side), plus device name, samplerate, blocksize, band cutoffs, and a `connected` flag. Single writer (the OSC listener thread), many lock-free readers.
  - `bridge.py` — `OscFeatureListener` (UDP socket on port 9000 by default; `/audio/lmh`, `/audio/meta`, `/audio/beat`, `/audio/bpm` handlers; staleness watchdog gated on `lmh` only), `AudioServerSupervisor` (launches the `audio-server` console script as a subprocess, pipes its stdout into the LED logger, terminates cleanly on shutdown), and `AudioBridge` that owns both. Failures are deliberately soft: if the binary is missing, port is taken, or packets stop arriving, the LED render loop keeps going with `ctx.audio.low/mid/high → 0`, `ctx.audio.beat → 0`, `ctx.audio.bpm → fallback`, and a warning in the terminal.
  - The audio-server's own browser UI (default `http://127.0.0.1:8766`) is the canonical place to pick the input device, retune band cutoffs, and save presets. The 'audio' link in the LED operator UI opens that URL in a new tab.
- `agent/` — Thin layer over OpenRouter, NOT a multi-tool agent loop. **The single tool is now `write_effect`** (see `surface/tool.py` below):
  - `session.py` — in-memory `SessionStore` + `ChatSession` with `history_max`-capped rolling buffer (heals dangling `tool` messages after trim), per-session rolling-window rate limit, and `reset_all_buffers()` (called when an operator action replaces preview source outside the agent's own write_effect path — library load, pull-live-to-preview — so the LLM's prior `write_effect` payloads in the deque don't reference source that's no longer in preview). Sessions wipe on restart.
  - `client.py` — thin OpenAI-compatible wrapper aimed at OpenRouter. Imports `openai` lazily; `MissingApiKey` raised at first call.
- `api/server.py` — FastAPI app. Operator endpoints, organised by what they touch:
  - **Library:** `GET /effects`, `POST /effects/{name}/load_preview`, `POST /effects/{name}/load_live`, `POST /effects/{name}/star`, `DELETE /effects/{name}`, `POST /preview/save`.
  - **Per-slot composition:** `POST /promote` (crossfade live ← preview), `POST /pull_live_to_preview` (hard cut), `POST /mode` (`design`|`live`), `GET /active`.
  - **Per-layer (slot ∈ {preview, live}):** `PATCH /{slot}/params`, `POST /{slot}/select`, `POST /{slot}/layer/remove`, `POST /{slot}/layer/reorder`, `PATCH /{slot}/layer/blend` (blend / opacity / enabled toggle).
  - **Output controls:** `GET /masters`, `PATCH /masters` (with optional `persist: true` write-back into the active YAML), `POST /blackout` + `/resume`, `POST /transport/pause` + `/resume` (DDP only), `POST /sim/pause` + `/resume`, `POST /system/reboot`.
  - **Diagnostics + setup:** `GET /state` (full snapshot incl. compositions, masters, audio, ddp), `GET /topology`, `GET /healthz`, `GET/PUT /config` (PUT rewrites the strip layout and hot-swaps the topology), `POST /calibration/{solo,walk,stop}`, `GET /audio/state` + `/audio/ui` (read-only — device picking lives on the external audio-server).
  - **WebSockets:** `/ws/frames` (frame broadcast — sim canvas), `/ws/state` (state broadcast — operator UI panels). Both honour the auth gate via `?password=` on the upgrade URL.
  - The landing page `/` hosts the dual-mode operator UI (mode toggle + simulator + composition decks + masters + chat).
- `api/auth.py` — optional shared-password gate for the entire HTTP/WS surface. Activated by setting `auth.password` in YAML; off by default for dev. Sets a `ledctl_auth` cookie via `/login` (form post) or `?password=…` query, gates HTTP via Starlette middleware, and rejects WS upgrades pre-accept with close code 4401 if the cookie is missing/wrong. `/login`, `/logout`, `/healthz` are always public. Render loop and DDP transport are unaffected — auth only protects the public-facing API surface.
- `api/agent.py` — `POST /agent/chat` (synchronous LLM round-trip via `asyncio.to_thread`, supports up to `agent.retry_on_tool_error` automatic retries with `LAST EFFECT ERROR` injected into the next system prompt), `GET/DELETE /agent/sessions/{id}`, `GET/PATCH /agent/config` (read-only on key; PATCH currently only adjusts `default_crossfade_seconds`, mirrored onto the live runtime). 503 on disabled/missing key, 429 on rate-limit hit, 502 on LLM failure.

**Config validation** (`config.py` Pydantic schemas): duplicate strip IDs, overlapping pixel ranges, and over-capacity caught at startup.

**Transport swap:** change `config.transport.mode` between `simulator` (dev) and `ddp`/`multi` (production). All code above the transport layer is identical.

## Surface v2 — LLM-as-author Effects + Resolume-style compositions

There is no primitive graph any more. A *layer* is a single Python `Effect` subclass authored by the LLM (or hand-written + saved as an example). A *composition* is a Resolume-style stack of layers (each with a blend mode + opacity + enabled toggle). The `Runtime` owns two compositions — **PREVIEW** (the LLM's scratchpad, rendered to the simulator in design mode) and **LIVE** (always rendered to the LEDs). The operator clicks **Promote** to crossfade live ← preview.

`surface_v2_design_plan.md` is the long-form rationale; `surface_v2_phased_plan.md` carves out the v1 vs v1.1 cuts. The summary below describes the code as it actually exists today on the `surface-v2-rewrite` branch.

### Package layout (`src/ledctl/surface/`)

```
src/ledctl/surface/
  __init__.py        — re-exports: Runtime, Effect, EffectInitContext, EffectFrameContext,
                        AudioView, FrameMap, MastersView, ParamStore, ParamView,
                        EffectStore, StoredEffect, EffectCompileError, MAX_SOURCE_BYTES,
                        Composition, Layer, ActiveEffect (alias), CrossfadeState, BLEND_MODES,
                        WriteEffectArgs, write_effect_tool_schema, apply_write_effect,
                        WRITE_EFFECT_TOOL_NAME, build_runtime_namespace, build_system_prompt,
                        compile_effect, named_palette, named_palette_names, NAMED_STOPS, LUT_SIZE
  base.py            — Effect base class + EffectInitContext / EffectFrameContext / FrameMap /
                        AudioView / MastersView / ParamView / ParamStore / RigInfo
  helpers.py         — hex_to_rgb, hsv_to_rgb, lerp, clip01, gauss, pulse, tri, wrap_dist,
                        palette_lerp, log, PI, TAU, LUT_SIZE
  palettes.py        — NAMED_STOPS + named_palette()/named_palette_names() — the LUTs the LLM
                        gets via `named_palette('fire'|'rainbow'|'ice'|...)`
  frames.py          — FRAME_DESCRIPTIONS + build_frames(topology) → derived dict (unchanged
                        from v1 — ctx.frames.x / .u_loop / .side_top / ... etc.)
  sandbox.py         — compile_effect(): AST scan (rejects imports + dunder access except
                        __name__), restricted SAFE_BUILTINS, MAX_SOURCE_BYTES = 8 KB,
                        single-Effect-subclass extraction
  schema.py          — pydantic models for the write_effect tool call: WriteEffectArgs, the
                        ParamSpec discriminated union (slider / int_slider / color / select /
                        toggle / palette), key/source-size validators, ≤8 params per effect
  prompt.py          — build_system_prompt(...): assembles the live-regenerated system prompt
                        (YOUR JOB, PHYSICAL RIG, INSTALL, COORDINATE FRAMES, AUDIO INPUT,
                        EFFECT CONTRACT, RUNTIME API, PARAM SCHEMA, PERFORMANCE RULES,
                        ANTI-PATTERNS, EXAMPLE EFFECTS, OPERATOR MASTERS, CURRENT PREVIEW
                        COMPOSITION, LAST EFFECT ERROR?, TOOL)
  tool.py            — apply_write_effect handler + write_effect_tool_schema. Validate →
                        compile → init+fence-test → install into preview's selected layer →
                        save to disk. Auto-merge: prior preview-layer values for matching keys
                        carry forward as the new layer's initial values.
  runtime.py         — Runtime + Composition + Layer + CrossfadeState + RenderStats +
                        build_runtime_namespace(). Owns LIVE + PREVIEW slots, mode, crossfade,
                        masters output stage (saturation pull → adaptive brightness gain → clip),
                        per-layer rolling render-time stats, calibration override hook.
  persistence.py     — EffectStore: filesystem CRUD over `config/effects/<slug>/{effect.py,
                        effect.yaml}`. install_examples_if_missing() copies bundled examples on
                        first boot. save_values() persists slider tweaks alongside the source.
  examples/          — bundled defaults: pulse_mono, audio_radial,
                        palette_wash_with_kick_sparkles, twin_comets_with_sparkles
```

### The Effect contract

Each effect is a single Python module with exactly one `Effect` subclass at top level. Lifecycle:

```python
class MyEffect(Effect):
    def init(self, ctx):
        # ctx.n, ctx.pos (N,3) f32, ctx.frames.<name>, ctx.strips, ctx.rig
        # Precompute per-LED arrays + state buffers here. Runs ONCE per swap.
        # `self.out` is preallocated by the base class as (N, 3) float32.
        ...

    def render(self, ctx):
        # ctx.t, ctx.wall_t, ctx.dt, ctx.n
        # ctx.frames.<name>, ctx.pos
        # ctx.audio.{low, mid, high, beat, beats_since_start, bpm, connected, bands[name]}
        # ctx.params.<key>   (operator-controlled values; writes are SOFT no-op + warning in v1)
        # ctx.masters        (read-only MastersView for diagnostics)
        # MUST return (N, 3) float32 in [0, 1]. Canonical pattern: fill self.out, return self.out.
        ...
```

The runtime never mutates the effect's returned buffer (it copies once into a runtime-owned scratch before applying masters), so returning `self.out` and trusting next frame's `render` sees state untouched is safe.

The runtime namespace injected into LLM-authored modules (built in `runtime.build_runtime_namespace(name)`):

| name | purpose |
| --- | --- |
| `np` | numpy module |
| `Effect` | base class to subclass exactly once |
| `hex_to_rgb`, `hsv_to_rgb` | colour conversion (cached / broadcasting) |
| `lerp`, `clip01`, `gauss`, `pulse`, `tri`, `wrap_dist` | math / shape helpers (most accept `out=`) |
| `palette_lerp(stops, t)`, `named_palette(name)` | multi-stop palette sample / named LUT |
| `rng` | `np.random.default_rng(seed)` seeded by SHA-256 of effect name → deterministic across reloads |
| `log` | `logging.getLogger("ledctl.effect")` — never `print`, which isn't in builtins |
| `PI`, `TAU`, `LUT_SIZE` (=256), `PALETTE_NAMES` | constants |

Built-ins are stripped: no `import`, `eval`, `exec`, `open`, `__import__`, `getattr/setattr/delattr`, `globals`, `print`. The AST scan also rejects every dunder attribute access except `__name__`. Source size is capped at 8 KB. The threat model is "LLM typo," not "malicious operator" — see `surface_v2_design_plan.md` §14.

### Compositions, blend modes, crossfade

`Runtime.live` and `Runtime.preview` are each a `Composition`: a `list[Layer]` plus a `selected: int` index that the chat panel and param panel target.

A `Layer` carries `{name, summary, source, instance, params, blend, opacity, enabled, consecutive_failures, perf}`. Blend modes: `normal`, `add`, `screen`, `multiply` (declared in `BLEND_MODES`; full list constants live on `runtime.py`). Layers render bottom-up into a per-slot accumulator (`_blend_into(dst, src, mode, opacity)`); the accumulator is then run through the master output stage and shipped.

The render entry point is `Runtime.render(*, wall_t, dt, t_eff, audio) → (live_buf, sim_buf)`:
- LIVE composition is always rendered into `_live_buf`. If a crossfade is active, the previous composition is also rendered into `_cf_buf` and the two are linearly blended by `alpha = elapsed/duration` (alpha uses `wall_t` so the speed master doesn't slow promotes); when elapsed exceeds duration the crossfade is dropped.
- PREVIEW composition only renders in **design mode**, and at half the engine FPS by default (`preview_half_rate=True`) — the simulator is a UI preview and 30 fps is plenty; this halves design-mode CPU during the worst case (a live crossfade running concurrently with a preview render). On non-render frames the previous `_preview_buf` is returned.
- Master output stage runs on each leg: saturation pull toward Rec.709 luminance → brightness gain (≤1 multiplicative; >1 uses an adaptive headroom envelope tracking the recent peak with a 0.5 s release half-life so "louder than 1" doesn't just clip immediately — see `_apply_master_output` / `_update_peak_envelope`) → clip to [0, 1].
- Calibration override (`/calibration/solo|walk`) runs *after* the master stage on each leg, so calibration looks identical to what's actually being shipped.

Crossfades are triggered automatically inside `install_layer("live", …)`, `remove_layer("live", …)`, `reorder_layer("live", …)`, and `promote()` — anything that mutates the LIVE composition. Preview swaps are deliberately hard-cuts (the operator is iterating; "did my fix work?" needs unambiguous feedback). Crossfade duration is the operator-owned `runtime.crossfade_seconds` (default seeded from `agent.default_crossfade_seconds` in YAML; mirrored on PATCH `/agent/config`).

### Per-layer safety nets

- **Init budget.** `_compile_layer` rejects an effect whose `init(ctx)` runs >200 ms. Most common cause: an O(N²) per-pair precompute.
- **Fence test.** `_fence_test(layer, frames=30)` runs 30 synthetic render frames with a sine-modulated `low` and a `beat=1` every 6th frame; any exception, wrong shape `(N, 3)`, wrong dtype (must be float32), or NaN/Inf rejects the install. Exceptions are mapped to actionable hints by `_diagnostic_hint(...)` (the most common LLM misses are surfaced verbatim — `ctx.frames.x` vs `ctx.x`, `ctx.audio.low` vs `ctx.low`, `ctx.params.<key>` vs `ctx.params['<key>']`, write-to-params errors).
- **Per-frame render isolation.** A layer that raises in `render()` → log traceback, increment `consecutive_failures`, skip the layer for that frame. After **3 consecutive failures** the layer is auto-disabled (`enabled=False`); operator can flip it back on from the layer-meta toggle.
- **Watchdog (RenderStats).** Each layer tracks a 1 s rolling window of render times (`PER_LAYER_BUDGET_MS = 5.0`). If p95 stays over budget for 30 consecutive frames (~0.5 s @ 60 fps) the layer is disabled with a `[layer X] tripped render budget` warning. One-shot — the operator can re-enable.
- **`dt` clamp.** The engine clamps `dt` at 2× the target frame interval before passing it to the runtime, so a hiccup (DDP retransmit, GC pause) doesn't tele-port stateful effects (comet heads, ripple ages).

### Named coordinate frames (`surface/frames.py`)

Topology precomputes a `derived: dict[str, np.ndarray]` of named per-LED scalars; effects address them as `ctx.frames.<name>`. The same dict from v1; same headline frames:

| frame             | meaning                                                              |
| ----------------- | -------------------------------------------------------------------- |
| `x`, `y`, `z`     | Cartesian axis components in [0, 1]                                  |
| `signed_x/y/z`    | Same, but signed [-1, 1]                                             |
| `radius`          | √(x²+y²) clipped to [0, 1] — concentric rings around centre column   |
| `angle`           | atan2(y, x)/2π wrapped to [0, 1]                                     |
| **`u_loop`**      | **Clockwise arc-position around the rig, [0, 1] from top centre**    |
| `u_loop_signed`   | u_loop centred at top: [-0.5, +0.5]                                  |
| `side_top` / `_bottom` / `_signed` | Top/bottom row masks (1/0, 1/0, +1/-1)                  |
| `axial_dist`      | \|x\| ∈ [0, 1] — distance from the centre column                     |
| `axial_signed`    | x ∈ [-1, 1] — symmetric explode coordinate                           |
| `corner_dist`     | Distance to nearest corner, normalised                               |
| `strip_id`        | Integer rank per strip (per config-listing order, int32)             |
| `chain_index`     | Local index along the strip from the controller end, [0, 1]         |
| `distance`        | √(x²+y²+z²) normalised — legacy alias                                |

`u_loop` is the headline frame. It's a clockwise chain-order coordinate built by classifying each strip by `(y_sign, x sign of outer end)` and walking them `top-right (forward) → bottom-right (reversed) → bottom-left (forward) → top-left (reversed)`. Motion along `u_loop` reads as motion *around the rectangle* regardless of strip count or `reversed` flags.

`build_system_prompt` emits a `COORDINATE FRAMES` block listing every name with its one-line `FRAME_DESCRIPTIONS` entry.

### The audio contract (effect-side)

The audio bridge writes the upstream OSC streams into `AudioState`; the engine packages them once per tick into an `AudioView` and **pre-multiplies the bands by `masters.audio_reactivity`**. So the LLM uses raw values:

| field | type | semantics |
| ----- | ---- | --------- |
| `ctx.audio.low / .mid / .high` | float in [0, 1] | smoothed band energies, pre-multiplied by `masters.audio_reactivity` |
| `ctx.audio.bands` | dict | `{"low": …, "mid": …, "high": …}` — convenient for `select`-param-driven band choice |
| `ctx.audio.beat` | int | new onsets since the previous render — usually 0 or 1, occasionally 2; use `> 0` as a one-shot trigger |
| `ctx.audio.beats_since_start` | int | monotonic onset counter |
| `ctx.audio.bpm` | float | current tempo; falls back to 120.0 when disconnected |
| `ctx.audio.connected` | bool | False = audio-server silent. low/mid/high will be 0.0 in this case. |

**Migration rule for new effects.** Continuous reactivity uses raw `ctx.audio.low/mid/high`; never threshold those manually to detect kicks (use `ctx.audio.beat > 0` — the upstream onset detector is far better). Tempo uses `ctx.audio.bpm`. The audio server's smoothing + auto-scaling lives in *its* UI; tune there, not here. The system prompt's ANTI-PATTERNS block lists this verbatim so the LLM doesn't reinvent it.

### Param schema + dynamic operator UI

The LLM declares 0–8 params per effect alongside the code. Each control type has its own pydantic model in `schema.py`:

| `control` | extra fields | UI element |
| --------- | ------------ | ---------- |
| `slider` | `min, max, step?, default, unit?` | range input + numeric box |
| `int_slider` | `min, max, step?, default` | integer range |
| `color` | `default` (hex) | colour picker |
| `select` | `options: [str], default` | dropdown |
| `toggle` | `default: bool` | switch |
| `palette` | `default` (a `named_palette` key) | palette dropdown + swatch |

Slider drag → `PATCH /preview/params` (or `/live/params`) → `ParamStore.update(...)` (atomic on the asyncio loop, with bounds clamping in `ParamStore._coerce`) → next render's `ctx.params.<key>` sees the new value. No recompile, no LLM round-trip, no crossfade. Patches without `layer_index` target the slot's selected layer. Each successful patch also calls `EffectStore.save_values(...)` so a restart preserves the operator's tuning.

**Param auto-merge.** When `apply_write_effect` installs a new layer, it scans the prior preview-selected layer's current values and carries forward any whose `key` matches a key in the new schema — so "regenerate but keep my colour and lead-offset" is mechanical. The LLM is told this in `prompt.py` so it picks new keys deliberately when a knob's meaning changes.

### Bundled examples + persistence

Bundled examples live under `src/ledctl/surface/examples/<slug>/{effect.py, effect.yaml}` and are copied to `config/effects/<slug>/` on first boot via `EffectStore.install_examples_if_missing()`. Currently shipped: `pulse_mono`, `audio_radial`, `palette_wash_with_kick_sparkles`, `twin_comets_with_sparkles`.

On disk, every saved effect is `config/effects/<slug>/effect.py` (real Python, diffable and SSH-editable on the Pi) plus `effect.yaml` (`{name, summary, source: 'agent'|'user', created_at, updated_at, params: <schema>, param_values: <current operator values>, starred: bool}`). On boot the runtime tries to load `pulse_mono` into both slots; if that fails the slot stays empty and a black layer is implicit.

The v1 `config/presets/<name>.yaml` files are **not** loaded any more — left on disk for reference only (no migration tool today).

### Adding / authoring an effect

- LLM path: a chat message in design mode produces one `write_effect` tool call. The handler validates the schema, AST-scans + sandbox-compiles, runs `init()` against the real topology (≤200 ms), fence-tests 30 synthetic frames, persists, and swaps into the preview's selected layer (hard cut).
- Operator save: drag sliders in design mode → click 💾 save → `POST /preview/save` writes the current preview-selected layer (incl. tweaked `param_values`) under a chosen name.
- Hand-written: drop `effect.py` + `effect.yaml` under `config/effects/<slug>/` and reload. The same compile/init/fence-test pipeline runs on load.

**Token budget reminder.** Per-field `description` strings on pydantic models, the example-effect blocks (`prompt.EXAMPLE_BASIC`, `EXAMPLE_ADVANCED`), and the prose blocks (`PHYSICAL_RIG`, `EFFECT_CONTRACT`, `PERFORMANCE_RULES`, `ANTI_PATTERNS`) are the dominant cost of the system prompt. Keep them tight.

## Coordinate convention

Right-handed: `+x` = stage-right, `+y` = up, `+z` = toward audience. Origin = centre of scaffolding. `ctx.pos` is normalised so each axis is in [-1, 1]; `ctx.frames.x/y/z` are the same data rescaled to [0, 1].

## Phase status

Surface v2 (LLM-as-author + Resolume-style layered compositions) is the current branch (`surface-v2-rewrite`). v1 + v1.1 from `surface_v2_phased_plan.md` are landed: dual-mode UI (Design / Live), layered compositions per slot, 30-frame fence test, per-layer render-budget watchdog, `dt` clamping, init-budget enforcement, param auto-merge across regenerations, design-mode preview at half-rate, save / load / star library. Auth gate, audio bridge, calibration, and DDP debug controls are inherited unchanged from v1. Mobile operator UI, hands-free / MIDI control, and the v3 "Surprise me" / autoplay-queue ideas are deferred (see `surface_v2_design_plan.md` §20).

## Auth + Pi deploy artefacts

- `src/ledctl/api/auth.py` — shared-password gate (off in dev, on for Pi). Activated by setting `auth.password` in YAML. The cookie is `ledctl_auth`; first-visit login via `/login` form post or `?password=…` query. WS upgrades reject pre-accept with close code 4401 if the cookie is missing/wrong. `/login`, `/logout`, `/healthz` are always public so future watchdogs can probe past the gate. Render loop and DDP transport are unaffected.
- `config/config.pi.yaml` — `auth.password: kaailed`, `server.host: 0.0.0.0`, full `masters:` block, `audio_server:` block (autostart=true, OSC port 9000, UI at :8766). The audio device picked at the audio-server's UI is persisted in *its* config.yaml — the LED config has no device field.
- `config/config.dev.yaml` — no `auth.password`, so the gate is off. Tests assert this.
- No dedicated `tests/test_auth.py` in v2 — the gate logic in `api/auth.py` is exercised indirectly through `tests/test_api.py` / `tests/test_e2e_pipeline.py` (which run with the dev config, where `auth.password` is unset and the gate is off). If we ever break the auth path it'll show as production breakage; worth restoring a focused test file before the next on-site deploy.
- `deploy/ledctl.service` — systemd unit. `After=network-online.target sound.target`, `Restart=always`, `RestartSec=2`, `Nice=-5`, `audio` supplementary group, `ProtectSystem=full` + `ProtectHome=read-only` so it cohabits with overlayfs. Install: `sudo cp deploy/ledctl.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now ledctl`.
- `.env.example` — template; copy to `.env` (gitignored) for the OpenRouter key. `cli.py` auto-loads it.

## Agent

The OpenRouter API key is read from `OPENROUTER_API_KEY` (env var name configurable via `agent.api_key_env`). `cli.py` auto-loads `.env` from the repo root before parsing config — never put the key in YAML. With no key, `/agent/chat` returns a clear 503 and the render loop is unaffected.

**Contract that holds the design together:** the LLM has exactly one tool — `write_effect({name, summary, code, params})` — and emits one tool call per turn with the *complete* new effect (Python source + param schema). Never a diff. "Make it more red" → re-emit the whole effect with the shifted defaults. No `list_*` / `get_*` discovery tools — the system prompt carries the install summary, COORDINATE FRAMES list, AUDIO INPUT snapshot, EFFECT CONTRACT, RUNTIME API, PARAM SCHEMA reference, PERFORMANCE RULES, ANTI-PATTERNS, two reference example effects (`pulse_mono` + `twin_comets_with_sparkles`), the read-only OPERATOR MASTERS values, and the **PREVIEW composition only** (selected layer's source + every layer's `param_values`). LIVE composition is deliberately invisible to the LLM — it's operator-controlled exclusively, and showing live source would tempt the LLM to "preserve" what's playing instead of authoring the preview cleanly.

**Where the effect lands.** The tool call replaces the *selected* PREVIEW layer (hard cut). DDP / live LEDs are unaffected until the operator clicks **Promote to live**. Before swap-in we run the full `Runtime._compile_layer` pipeline: validate schema → AST + sandbox compile → `init(ctx)` ≤200 ms → 30-frame fence test → on success, install + persist to `config/effects/<name>/`. On failure (any stage) the tool result carries `{ok: false, error, details}` with a diagnostic hint; the next turn's system prompt embeds it under `LAST EFFECT ERROR` so the LLM self-corrects. Up to `agent.retry_on_tool_error` consecutive auto-retries within a single chat turn before the failure surfaces to the operator. Each retry rebuilds the prompt fresh so the LLM sees its own previous error.

**Master controls are deliberately invisible to the LLM as writes.** The system prompt's OPERATOR MASTERS block is read-only and the prompt explicitly tells the LLM: when a request can only be honoured by a master change ("brighter" while brightness is already 1.0; "less reactive" while audio_reactivity is high), tell the operator which slider to move instead of writing a redundant effect.

**Param auto-merge.** When `apply_write_effect` installs the new layer, it carries forward operator-tuned values for any matching `key` in the prior preview-selected layer. The LLM is told this in PARAM SCHEMA so it picks new keys deliberately when a knob's meaning changes.

**Operator side-channel.** When the operator does anything that mutates preview source outside the agent's own write_effect path — `POST /effects/<name>/load_preview`, `POST /pull_live_to_preview`, `POST /preview/save` (effectively a rename) — the API server calls `SessionStore.reset_all_buffers()` so the LLM's deque doesn't reference stale source. The operator-visible chat transcript (`turns`) is preserved; only the model-visible message buffer clears.

## Hardware notes

- 1800 LEDs across 4 strips (450 per strip), two 30 m horizontal rows, centre-fed (4 chain heads at `x=0`).
- Gledopto ESP32 WLED controller at `10.0.0.2:4048` (DDP, port 4048). DDP uses 480 px / packet, PUSH flag *only* on the final packet of each frame.
- WLED must be set to **GRB** colour order for WS2815 (RGB will swap red/green).
- If WLED's own gamma is enabled, set `output.gamma: 1.0` in YAML — never double-gamma.
- INMP441 I²S microphone (on Pi only; USB audio or built-in mic on dev).
- `config/config.dev.yaml` for Mac dev (transport=simulator); `config/config.pi.yaml` for Pi (transport=ddp).
- LedFx and `ledctl` cannot both talk to the same WLED at once.
- The Gledopto runs **WLED-MoonModules** (AudioReactive fork, build 2508020 confirmed 2026-05-08), not stock WLED. It has extra GEQ / AGC / I2S settings on the Info page; otherwise behaves like 0.14+. Stock-WLED tutorials mostly still apply.

## Strip mapping (verified 1:1 with operator UI, 2026-05-08)

The mapping below was verified end-to-end via `/calibration/solo` against the physical rig. Operator UI ↔ physical LEDs are 1:1; do not edit either side without re-running the test sequence.

**WLED outputs** (Gledopto LED Preferences) — all four are **WS281x, RGB, length 450, Reversed OFF, Skip 0**:

| WLED output | GPIO | Start | Length | Reversed |
| ----------- | ---- | ----- | ------ | -------- |
| 1           | 16   | 0     | 450    | off      |
| 2           | 12   | 450   | 450    | off      |
| 3           | 2    | 900   | 450    | off      |
| 4           | 4    | 1350  | 450    | off      |

(WLED outputs 2 and 4 used to be Reversed=ON; that was unchecked when the ledctl `pixel_offset`s were swapped — keeping reversal in WLED with the new offsets would have flipped those two strips.)

**ledctl strips** (`config/config.pi.yaml`) — all four `reversed: false`, all four with `geometry.start` at `x=0` (centre) and `geometry.end` at `x=±15` (outer edge):

| ledctl `id`   | `pixel_offset` | physical quadrant | geometry end |
| ------------- | -------------- | ----------------- | ------------ |
| top_right     | 0              | top-right         | (+15, +0.5)  |
| bottom_right  | 450            | bottom-right      | (+15, −0.5)  |
| bottom_left   | 900            | bottom-left       | (−15, −0.5)  |
| top_left      | 1350           | top-left          | (−15, +0.5)  |

All strips are physically wired controller-at-centre, so logical pixel 0 of each = centre end of the rig. No reversal anywhere — ledctl geometry, WLED logical addressing, and physical wiring all agree.

**Re-verifying after any change** (run on the Pi):

```bash
PI=http://localhost:8000; PW=kaailed
solo() { curl -s -X POST "$PI/calibration/solo?password=$PW" -H 'Content-Type: application/json' -d "{\"indices\": $(python3 -c "import json;print(json.dumps(list(range($1,$2))))")}"; }

# Quadrant test
solo 0 450      # top-right
solo 450 900    # bottom-right
solo 900 1350   # bottom-left
solo 1350 1800  # top-left

# Direction test (first 30 of each strip — should light at CENTRE)
solo 0 30; solo 450 480; solo 900 930; solo 1350 1380

curl -s -X POST "$PI/calibration/stop?password=$PW"
```

## Boot-time Gledopto reboot

`deploy/gledopto-reboot.service` is a oneshot systemd unit that fires `curl http://10.0.0.2/reset` 8 s after `ledctl.service` has started. WLED reboots and comes back up while DDP is already streaming, so it enters realtime override cleanly on every Pi boot — works around the WLED-MM realtime-intake wedge without manual intervention. Installed and enabled on the Pi at `/etc/systemd/system/gledopto-reboot.service`.

## DDP control: Pi vs Gledopto (debug + on-site toggle)

The render loop's DDP transport has a **`paused`** flag exposed over the API and as a button on the operator UI. Pausing stops `send_frame` to WLED while keeping the simulator leg streaming, so the operator UI viz stays live. After WLED's ~2.5 s realtime timeout the Gledopto resumes its own preset/effect — i.e. instant A/B between "Pi drives" and "Gledopto drives" with no service restart.

| method  | path                  | what it does                                                  |
| ------- | --------------------- | ------------------------------------------------------------- |
| GET     | `/transport`          | `{mode, ddp:{available, paused, host, port, frames_sent, packets_sent}}` |
| POST    | `/transport/pause`    | stop sending DDP → after ~2.5 s WLED takes back over          |
| POST    | `/transport/resume`   | resume DDP → ledctl drives the LEDs                           |

`/state` also embeds the same `ddp` block. The auth gate applies, so on the Pi: `curl 'localhost:8000/transport?password=kaailed' | python3 -m json.tool`. There's no dedicated UI button today — drive it from the API; disabled in dev (simulator-only mode) since there's no DDP transport to pause.

**Diagnostic counters.** `DDPTransport` exposes `frames_sent` / `packets_sent`; useful for confirming the Pi is actually transmitting. Healthy 60 fps × 1800 LEDs = 4 packets per frame (3×1450 B + 1×1090 B over the wire).

**On-site debug recipe** when the LEDs show WLED's own preset instead of ledctl content:

```bash
# 1. confirm ledctl is sending DDP and frames_sent is climbing
curl -s 'localhost:8000/transport?password=kaailed' | python3 -m json.tool

# 2. confirm packets actually leave eth0 toward 10.0.0.2:4048
sudo tcpdump -i any -n 'host 10.0.0.2 and udp port 4048' -c 20

# 3. confirm Gledopto is reachable on ETH and ARP'd
ping -c 3 10.0.0.2
arp -n | grep 10.0.0.2     # MAC must resolve, not "(incomplete)"

# 4. check WLED's Info page for a "Realtime: DDP, IP: 10.0.0.1" line.
#    Absence of that line == WLED is silently dropping the override even
#    though packets reach it. The most common cause we've seen is a wedged
#    realtime intake state — REBOOT THE GLEDOPTO before chasing config
#    (encountered on-site 2026-05-08 after ~2 h of WLED-MM uptime; full
#    power cycle restored normal DDP override with no settings changed).
```

If the Info page never shows a Realtime line even after a Gledopto reboot, then it's a config issue. In that order: WLED-MM **Sync Interfaces → Realtime → "Receive UDP Realtime"** must be on (this is the override-arming toggle, distinct from "Receive UDP" which is the WLED-to-WLED notifier protocol); **LED Preferences → Length** must be 1800; **Segments** must include a Segment 0 spanning 0–1799; and turn **PC Mode** off as a control. The `Force max brightness during realtime` toggle eliminates a separate "looks ignored because brightness=0" failure mode.

## External audio dependency

Audio capture / FFT / band-energy extraction / onset detection / tempo tracking is owned by [Realtime_PyAudio_FFT](https://github.com/Jaymon/Realtime_PyAudio_FFT). Install that package alongside `ledctl` so its `audio-server` console script is on PATH; ledctl will spawn it as a subprocess on boot (`audio_server.autostart: true`). If you'd rather run it manually (e.g. tuning bands via its UI on a different machine), set `audio_server.autostart: false` and start it however you like — the LED server will still happily consume the OSC feed.

The audio-server stores its own settings in its own `config.yaml` (device, band cutoffs, smoothing, autoscale window, onset / tempo params). Tune those from its browser UI at `http://127.0.0.1:8766` — the 'audio' link in the LED operator UI opens it in a new tab.

**OSC addresses consumed by the LED bridge** (handlers in `src/ledctl/audio/bridge.py`, fields in `src/ledctl/audio/state.py`, view in `src/ledctl/surface/base.py::AudioView`):

| address       | payload                          | written into AudioState | exposed to effects as |
| ------------- | -------------------------------- | ----------------------- | --------------------- |
| `/audio/lmh`  | three floats (low, mid, high)    | `state.{low,mid,high}` + `mark_packet()` (gates `connected`) | `ctx.audio.low/mid/high` (pre-multiplied by `masters.audio_reactivity`) |
| `/audio/meta` | sr/blocksize/n_fft + band cutoffs (+ optional device-name string trail) | `state.{samplerate,blocksize,n_fft_bins,*_lo,*_hi,device_name}` | informational (system prompt + audio HUD) |
| `/audio/beat` | empty (rising-edge trigger only) | `state.beat_count += 1`, `state.last_beat_at` | `ctx.audio.beat` (delta since last frame; usually 0 or 1) + `ctx.audio.beats_since_start` |
| `/audio/bpm`  | one float                        | `state.bpm` (None until first packet) | `ctx.audio.bpm` (falls back to 120.0 when disconnected) |

**Soft-fail across all four addresses**: if the audio-server isn't running, the OSC port is taken, or any individual stream stops, the LED render loop keeps going with `ctx.audio.low/mid/high` → 0, `ctx.audio.beat` → 0, `ctx.audio.bpm` → fallback, and a warning in the terminal. The watchdog only flags `connected = False` when `/audio/lmh` packets stop — beat/bpm absence is normal between onsets. Effects should still look good silent (e.g. drive brightness from a slider with audio mixed on top via `amp = base + reactive * ctx.audio.low`).

**Migration rule for new effects.** Continuous reactivity uses `audio_band(low|mid|high)` raw (no manual thresholding). Discrete triggers use `audio_beat()`. Tempo uses `audio_bpm()`. If you ever feel like writing `threshold(audio_band("low"), …)` to detect a kick — don't; that's exactly what `/audio/beat` is for, and the upstream onset detector has more signal than a level threshold ever will. Existing primitives that need beat semantics (e.g. `ripple.trigger`) take a `scalar_t` slot and the LLM is taught to wire `audio_beat()` in.
