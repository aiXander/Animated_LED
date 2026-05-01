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
.venv/bin/pytest -v tests/test_effects.py   # single file

# Lint
.venv/bin/ruff check src tests
```

## Architecture

The system is a real-time LED controller for a 1800-LED festival install (WS2815 strips on metal scaffolding, driven via DDP over Ethernet to a Gledopto/WLED controller).

**Render loop** (in `engine.py`): fixed-timestep async loop at `target_fps` — calls `Mixer.render(t, out)` → `PixelBuffer.to_uint8(gamma)` → `Transport.send_frame()`. Frames drop rather than spiral on lag.

**Key layers:**
- `topology.py` — Spatial model of all 1800 LEDs. Normalised positions (all axes in [-1, 1]) derived from `config.yaml` strip geometries. Effects must use normalised coords, never raw pixel indices.
- `effects/` — `field × palette × bindings` pipeline. Field generators (`scroll`, `radial`, `sparkle`, `noise` — in `effects/fields/`) produce a scalar field over the topology; `palette.py` lifts that scalar to RGB via a compiled LUT (named palettes in `NAMED_PALETTES`); `modulator.py` drives `brightness` / `speed` / `hue_shift` slots from audio bands or LFOs with per-slot smoothing (`Envelope`, asymmetric attack/release). Each generator self-registers via `@register` in `effects/registry.py`; new ones must also be re-exported from `effects/fields/__init__.py` so importing the package triggers registration.
- `mixer.py` — Layer stack with blend modes (normal/add/screen/multiply), opacity, and crossfade transitions between stacks.
- `pixelbuffer.py` — Float32 working buffer [0.0, 1.0]. Gamma (default 2.2) applied once here, not in WLED.
- `transports/` — Pluggable output: `simulator` (WebSocket to browser), `ddp` (UDP to WLED), `multi` (both). Swapped via `config.transport.mode`.
- `audio/` — Split into three pieces so swapping the input source is a one-class change:
  - `source.py` — `AudioSource` ABC + `SoundDeviceSource` (PortAudio). New sources (network audio over RTP/AES67/NDI, file replay for tests) drop in here.
  - `analyser.py` — `AudioAnalyser` consumes an `AudioSource`, maintains an FFT ring buffer, computes raw RMS / peak / band features, and stamps an `AudioState`. `blocksize` (HW capture latency) and `fft_window` (FFT length / freq resolution) are independent.
  - `state.py` — `AudioState` is *raw and instantaneous*: no EMA, no peak hold. All temporal smoothing for visual output happens per-binding in `effects/modulator.Envelope` (asymmetric attack/release). One writer (callback thread), many readers, no locks needed.
  - `normalizer.py` — `RollingNormalizer` per-feature rolling-window auto-gain. Tracks a high-percentile ceiling over the last `window_s` and divides; `*_norm` companions on `AudioState` map recent dynamic range to ~[0, 1] regardless of mic level. This is what makes audio bindings auto-scale to room loudness — bindings should consume `*_norm`, not raw values.
  - `capture.py` — `AudioCapture` is a thin convenience wrapper that pre-wires source + analyser; that's what `server.py` and tests hold.
- `agent/` — Phase 6 language-driven control panel. Thin layer over OpenRouter, NOT a multi-tool agent loop:
  - `tool.py` — single `update_leds(layers, crossfade_seconds, blackout)` tool. Argument is the *complete* new layer stack, never a diff. Validates each layer through the effect's pydantic schema; on failure the tool result carries a structured error which the LLM sees on the next turn (via the rolling buffer) and self-corrects. Ultimately calls `Engine.crossfade_to` — same code path as `POST /presets/{name}`.
  - `system_prompt.py` — `build_system_prompt(...)` regenerated *fresh every turn*: install summary (from `Topology`), current layer-stack JSON, audio snapshot (with band freq ranges), full effect catalogue (auto-derived from each effect's pydantic schema), palette names + bindings rubric, 3 anchor examples, behavioural rubric. The dominant token cost — keep effect param descriptions one-liner-tight.
  - `session.py` — in-memory `SessionStore` + `ChatSession` with `history_max`-capped rolling buffer (heals dangling `tool` messages after trim) and per-session rolling-window rate limit. Sessions wipe on restart (v1).
  - `client.py` — thin OpenAI-compatible wrapper aimed at OpenRouter (`base_url`). Imports `openai` lazily; `MissingApiKey` raised at first call so module import never explodes when the env var is absent.
- `api/server.py` — FastAPI app: effects, layers, presets, blackout, calibration, topology, audio device management, WebSocket frame broadcast. The landing page (`/`) hosts the LED viz, the chat UI, and the live status panel in one view.
- `api/agent.py` — `/agent/chat` (synchronous LLM round-trip via `asyncio.to_thread`), `/agent/sessions/{id}` (GET/DELETE), `/agent/config` (read-only; never echoes the API key). 503 on disabled / missing key, 429 on rate-limit hit, 502 on LLM failure.

**Config validation** (`config.py` Pydantic schemas): duplicate strip IDs, overlapping pixel ranges, and over-capacity are caught at startup.

**Transport swap:** change `config.transport.mode` between `simulator` (dev) and `ddp` (production). All code above the transport layer is identical.

## Effect System

The base `Effect` ABC (in `effects/base.py`) implements `render(t: float, out: np.ndarray)` against an `(num_leds, 3)` float32 buffer; LED positions live on `self.topology.positions` (normalised, all axes ∈ [-1, 1] — never raw indices).

Most effects are *field generators* that subclass `FieldEffect` (in `effects/fields/base.py`). A field generator:

1. Computes a scalar field per LED (e.g. `scroll` is a 1-D travelling wave whose `shape` param picks cosine / sawtooth / pulse / gauss — the merger of the old `wave` / `gradient` / `chase` effects).
2. Samples a `PaletteSpec` LUT (`effects/palette.py`) to turn the scalar into RGB. Named palettes ship in `NAMED_PALETTES`; ad-hoc palettes are `{stops: [{pos, color}, …]}`.
3. Applies `Bindings` (`effects/modulator.py`) — three optional slots `brightness`, `speed`, `hue_shift`, each driven by a `ModulatorSpec` (`source ∈ {const, audio.rms|low|mid|high|peak, lfo.sin|saw|triangle|pulse}`, with per-slot envelope defaults so the LLM rarely needs to specify attack/release). Bindings read `audio.*_norm`, not raw audio.

To add a new field effect:
1. Subclass `FieldEffect` + `FieldParams` under `effects/fields/`.
2. Decorate the class with `@register` (from `effects/registry.py`).
3. Re-export it from `effects/fields/__init__.py` so the import side-effect runs at module load.

The pydantic `Params` schema's `description=` strings are user-facing — they feed both the `/effects` REST schema and the LLM system prompt. Keep them tight: one line per field is the budget.

**Possible future improvement — biquad IIR filter bank:** Today's analyser does block-rate FFT-based band detection. For tighter transient response on mid/high bands, a per-band biquad band-pass filter running sample-by-sample (with a short sliding RMS) would track onsets faster than block-FFT — roughly 3–8 ms better on hi-hats / snare; bass is physics-bound either way. Trade-off: more moving parts and per-band coefficient design (`scipy.signal.butter`). Park behind a config flag if reactivity ever feels sluggish on real DJ audio.

## Coordinate Convention

Right-handed: `+x` = stage-right, `+y` = up, `+z` = toward audience. Origin = center of scaffolding. All effect math in normalised [-1, 1].

## Phase Status

Phases 0–6 are complete (topology, DDP transport, effect engine, REST API, browser simulator + layout editor, audio analysis, language-driven control panel).

Phases 7–9 are planned: mobile operator UI, Pi I²S/systemd cutover, reliability/watchdog.

## Agent (Phase 6)

The OpenRouter API key is read from `OPENROUTER_API_KEY` (env var name configurable via `agent.api_key_env`). `cli.py` auto-loads `.env` from the repo root (preferred) or `~/.env` before parsing config — never put the key in YAML. With no key, `/agent/chat` returns a clear 503 and the render loop is unaffected.

Contract that holds the design together: the LLM emits **one** `update_leds` per turn, with the *complete* new state. "Make it more red" → re-emit the whole stack with shifted colour stops. No `list_*` / `get_*` discovery tools — the system prompt already carries the catalogue + current state + audio snapshot.

## Hardware Notes

- 1800 LEDs across 4 strips (450 per strip), two 30 m horizontal rows, centre-fed (4 chain heads at `x=0`)
- Gledopto ESP32 WLED controller at `10.0.0.2:4048` (DDP, port 4048). DDP uses 480 px / packet, PUSH flag *only* on the final packet of each frame
- WLED must be set to **GRB** colour order for WS2815 (RGB will swap red/green)
- If WLED's own gamma is enabled, set `output.gamma: 1.0` in YAML — never double-gamma
- INMP441 I²S microphone (on Pi only; USB audio or built-in mic on dev)
- `config/config.dev.yaml` for Mac dev (transport=simulator); `config/config.pi.yaml` for Pi (transport=ddp)
- LedFx and `ledctl` cannot both talk to the same WLED at once
