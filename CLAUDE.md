# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
.venv/bin/pytest -v tests/test_surface_primitives.py            # single file
.venv/bin/pytest tests/test_mixer.py::test_crossfade_uses_wall_t # single test

# Lint
.venv/bin/ruff check src tests
```

## Architecture

The system is a real-time LED controller for an 1800-LED festival install (4 × 450 WS2815 strips on metal scaffolding, centre-fed via DDP into a Gledopto/WLED controller).

**Render loop** (`engine.py`): fixed-timestep async loop at `target_fps`. Each tick — build a `RenderContext` (effective `t = wall_t × masters.speed`, `audio.*_norm` pre-scaled by `masters.audio_reactivity`, frozen `t` if `masters.freeze`) → `Mixer.render(ctx, out)` walks the compiled layer trees and blends each into the accumulator → master output stage (saturation pull → brightness gain) → `PixelBuffer.to_uint8(gamma)` → `Transport.send_frame()`. Frames drop rather than spiral on lag.

**Key layers:**
- `topology.py` — Spatial model of all 1800 LEDs. Normalised positions (each axis in [-1, 1]) derived from `config.yaml` strip geometries. Primitives must use `topology.normalised_positions`, never raw indices.
- `surface.py` — **The single control surface.** Owns the entire visual vocabulary: colour/shape utilities, the primitive registry, every primitive's pydantic `Params` + `compile()`, named palettes, the spec types (`NodeSpec`, `LayerSpec`, `UpdateLedsSpec`), the compiler (`compile(spec, topology) → CompiledNode`), and `generate_docs()` for the prompt-ready CONTROL SURFACE block. **Adding a new visual idea is a one-place change**: write a `@primitive` class — the agent prompt and operator UI both pick it up via `generate_docs()` / `GET /surface/primitives` automatically. This module never imports from `engine.py` or `mixer.py` (they import it).
- `masters.py` — Operator-owned `MasterControls` (`brightness`, `speed`, `audio_reactivity`, `saturation`, `freeze`) and per-frame `RenderContext`. Deliberately kept out of the DSL — the LLM never produces or alters them. Bounds enforced in `clamped()`: brightness/saturation ∈ [0, 1], speed/audio_reactivity ∈ [0, 3], freeze: bool.
- `mixer.py` — Stack of `Layer(node, blend, opacity)` with blend modes (normal/add/screen/multiply), crossfade between stacks, master output stage. Crossfade alpha uses `ctx.wall_t` so operator direction isn't slowed by `speed=0.5` or stopped by `freeze=true`. Per-layer rendering uses `ctx.t` (master-speed-scaled).
- `pixelbuffer.py` — Float32 working buffer in [0, 1]. Gamma (default 2.2) applied once here, not in WLED.
- `transports/` — Pluggable output: `simulator` (WebSocket to browser), `ddp` (UDP to WLED, 480 px/packet, PUSH on the final packet only), `multi` (both). Swapped via `config.transport.mode`.
- `audio/` — Three-piece split so swapping the input source is a one-class change:
  - `source.py` — `AudioSource` ABC + `SoundDeviceSource` (PortAudio).
  - `analyser.py` — `AudioAnalyser` consumes blocks, maintains an FFT ring buffer, computes RMS / peak / band features. `blocksize` (HW capture latency) and `fft_window` (FFT length) are independent.
  - `state.py` — `AudioState` is **raw and instantaneous** (no EMA, no peak hold). Single writer (callback thread), many readers, no locks.
  - `normalizer.py` — `RollingNormalizer` per-feature rolling-window auto-gain. Bindings consume `*_norm`, not raw values, so they auto-scale to room loudness.
  - `capture.py` — Thin convenience wrapper that pre-wires source + analyser.
- `agent/` — Phase 6 language-driven control panel. Thin layer over OpenRouter, NOT a multi-tool agent loop:
  - `tool.py` — single `update_leds(layers, crossfade_seconds, blackout)` tool. The argument is the *complete* new layer stack as a tree of `{kind, params}` primitives, never a diff. The surface compiler type-checks the tree (palette in a scalar slot is rejected, leaf must be `rgb_field`, etc.); on failure the tool result carries a structured `{path, msg, valid_kinds}` error which the LLM sees on the next turn (via the rolling buffer) and self-corrects. Calls `Engine.crossfade_to` — same code path as `POST /presets/{name}`.
  - `system_prompt.py` — `build_system_prompt(...)` regenerated **fresh every turn**: install summary, current layer-stack JSON, audio snapshot, **read-only master values**, full primitive catalogue from `surface.generate_docs()`, anchor examples, anti-patterns. Dominant token cost — keep primitive `Params` `description=` strings tight.
  - `session.py` — in-memory `SessionStore` + `ChatSession` with `history_max`-capped rolling buffer (heals dangling `tool` messages after trim) and per-session rolling-window rate limit. Sessions wipe on restart (v1).
  - `client.py` — thin OpenAI-compatible wrapper aimed at OpenRouter. Imports `openai` lazily; `MissingApiKey` raised at first call.
- `api/server.py` — FastAPI app. Endpoints: `/state`, `/topology`, `/config` (PUT for layout edits), `/surface/primitives`, `/layers` (POST/PATCH/DELETE), `/masters` (GET/PATCH), `/presets/{name}` (POST), `/blackout` + `/resume`, `/calibration/*`, `/audio/*`, `/healthz`, `/ws/frames` (WebSocket frame broadcast). The landing page (`/`) hosts the LED viz, chat UI, and live status panel.
- `api/auth.py` — optional shared-password gate for the entire HTTP/WS surface (Phase 8). Activated by setting `auth.password` in YAML; off by default for dev. Sets a `ledctl_auth` cookie via `/login` (form post) or `?password=…` query, gates HTTP via Starlette middleware, and rejects WS upgrades pre-accept with close code 4401 if the cookie is missing/wrong. `/login`, `/logout`, `/healthz` are always public. Render loop and DDP transport are unaffected — auth only protects the public-facing API surface.
- `api/agent.py` — `/agent/chat` (synchronous LLM round-trip via `asyncio.to_thread`), `/agent/sessions/{id}` (GET/DELETE), `/agent/config` (read-only; never echoes the API key). 503 on disabled/missing key, 429 on rate-limit hit, 502 on LLM failure.

**Config validation** (`config.py` Pydantic schemas): duplicate strip IDs, overlapping pixel ranges, and over-capacity caught at startup.

**Transport swap:** change `config.transport.mode` between `simulator` (dev) and `ddp` (production). All code above the transport layer is identical.

## The control surface

Every visual primitive lives in `surface.py`. There are no named effects — a layer is a tree of `{kind, params}` nodes, each validated by its own pydantic `Params` model. The full catalogue (with JSON Schema for every primitive) is served live at `GET /surface/primitives` and embedded into the LLM system prompt every turn.

**The four output kinds the compiler enforces:**

| kind            | what it produces                       | typical primitives                                                |
| --------------- | -------------------------------------- | ----------------------------------------------------------------- |
| `scalar_field`  | per-LED scalar in [0, 1]               | `wave`, `radial`, `noise2d`, `sparkles`, `position`, `gradient`   |
| `scalar_t`      | one scalar per frame                   | `lfo`, `audio_band`, `envelope`, `constant`                       |
| `palette`       | 256-entry RGB LUT                      | `palette_named`, `palette_stops`                                  |
| `rgb_field`     | per-LED RGB (the layer leaf)           | `palette_lookup`, `solid`                                         |

Polymorphic combinators (`mix`, `mul`, `add`, `screen`, `max`, `min`, `remap`, `threshold`, `clamp`, `range_map`, `trail`) resolve their output kind from their inputs at compile time and broadcast where it makes sense (`rgb_field × scalar_t → rgb_field`, `palette × palette → palette` for `mix`).

**Two pieces of sugar in the spec language:**
- a bare number anywhere a node is expected becomes a `constant` (so `"speed": 0.3` is fine);
- a bare palette string becomes a `palette_named` (so `"palette": "fire"` is fine).

**Modulation lives directly on the parameter.** Instead of an old-style `bindings.brightness` slot, you pass an `envelope(audio_band(...))` node into `palette_lookup.brightness`. Audio reactivity is composable: `audio_band(band="rms"|"low"|"mid"|"high"|"peak")` returns a `scalar_t`; wrap it in `envelope(input=..., attack_ms, release_ms, gain, floor, ceiling)` for smooth attack/release.

**Adding a new primitive:**
1. Write a `@primitive` class in `surface.py` with a pydantic `Params` model and a `compile()` that returns a `CompiledNode` of the right `output_kind`.
2. That's it. The doc generator, REST primitive catalogue, and LLM system prompt all pick it up.

The `Params` `description=` strings are user-facing — they feed both `GET /surface/primitives` and the LLM system prompt. **One line per field is the budget** (this is the dominant token cost).

Named palettes ship in `surface.NAMED_PALETTES`: `rainbow`, `fire`, `ice`, `sunset`, `ocean`, `warm`, `white`, `black`, `mono_<hex>`. Custom palettes are `{kind: "palette_stops", params: {stops: [{pos, color}, …]}}`.

Presets live in `config/presets/<name>.yaml` (sibling of the active config file). Each preset is `{ crossfade_seconds, layers: [{ node: {kind, params}, blend, opacity }, …] }`. Four seed presets ship: `default`, `chill`, `peak`, `cooldown`. Loaded by `presets.py`.

## Coordinate convention

Right-handed: `+x` = stage-right, `+y` = up, `+z` = toward audience. Origin = centre of scaffolding. All primitive math in normalised [-1, 1].

## Phase status

Phases 0–6 are complete (topology, DDP transport, surface engine, REST API, browser simulator + layout editor, audio analysis, language-driven control panel).

Phase 8.1's digital prep also landed early — see "Auth + Pi deploy artefacts" below. Phase 7 (mobile operator UI), the rest of Phase 8 (INMP441 I²S setup, Tailscale, read-only rootfs, on-site bring-up), and Phase 9 (reliability/watchdog) are next.

## Auth + Pi deploy artefacts

- `src/ledctl/api/auth.py` — shared-password gate (off in dev, on for Pi). Activated by setting `auth.password` in YAML. The cookie is `ledctl_auth`; first-visit login via `/login` form post or `?password=…` query. WS upgrades reject pre-accept with close code 4401 if the cookie is missing/wrong. `/login`, `/logout`, `/healthz` are always public so future watchdogs can probe past the gate. Render loop and DDP transport are unaffected.
- `config/config.pi.yaml` — `auth.password: kaailed`, `server.host: 0.0.0.0`, full `masters:` block. `audio.device: null` is the only field the build day fills in (`/audio/select` persists it).
- `config/config.dev.yaml` — no `auth.password`, so the gate is off. Tests assert this.
- `tests/test_auth.py` — 11 cases (cookie/query/POST/WS, public-path allow-list, dev-config still open).
- `deploy/ledctl.service` — systemd unit. `After=network-online.target sound.target`, `Restart=always`, `RestartSec=2`, `Nice=-5`, `audio` supplementary group, `ProtectSystem=full` + `ProtectHome=read-only` so it cohabits with overlayfs. Install: `sudo cp deploy/ledctl.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now ledctl`.
- `.env.example` — template; copy to `.env` (gitignored) for the OpenRouter key. `cli.py` auto-loads it.

## Agent (Phase 6)

The OpenRouter API key is read from `OPENROUTER_API_KEY` (env var name configurable via `agent.api_key_env`). `cli.py` auto-loads `.env` from the repo root before parsing config — never put the key in YAML. With no key, `/agent/chat` returns a clear 503 and the render loop is unaffected.

**Contract that holds the design together:** the LLM emits **one** `update_leds` per turn, with the *complete* new state. "Make it more red" → re-emit the whole stack with shifted colour stops. No `list_*` / `get_*` discovery tools — the system prompt carries the catalogue + current layer stack + audio snapshot + master values every turn.

**Master controls are deliberately invisible to the LLM as writes.** The agent sees them as a read-only block in the system prompt and tells the user which slider to move when a request can only be honoured that way.

## Hardware notes

- 1800 LEDs across 4 strips (450 per strip), two 30 m horizontal rows, centre-fed (4 chain heads at `x=0`).
- Gledopto ESP32 WLED controller at `10.0.0.2:4048` (DDP, port 4048). DDP uses 480 px / packet, PUSH flag *only* on the final packet of each frame.
- WLED must be set to **GRB** colour order for WS2815 (RGB will swap red/green).
- If WLED's own gamma is enabled, set `output.gamma: 1.0` in YAML — never double-gamma.
- INMP441 I²S microphone (on Pi only; USB audio or built-in mic on dev).
- `config/config.dev.yaml` for Mac dev (transport=simulator); `config/config.pi.yaml` for Pi (transport=ddp).
- LedFx and `ledctl` cannot both talk to the same WLED at once.

## Possible future improvement — biquad IIR filter bank

Today's analyser does block-rate FFT-based band detection. For tighter transient response on mid/high bands, a per-band biquad band-pass filter running sample-by-sample (with a short sliding RMS) would track onsets faster than block-FFT — roughly 3–8 ms better on hi-hats / snare; bass is physics-bound either way. Trade-off: more moving parts and per-band coefficient design (`scipy.signal.butter`). Park behind a config flag if reactivity ever feels sluggish on real DJ audio.
