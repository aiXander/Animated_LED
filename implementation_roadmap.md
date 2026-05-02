x# Implementation Roadmap — Audio-Reactive LED Installation

> Companion to `Audio-Reactive LED installation.md`. That doc covers the **physical/electrical** build; this one covers the **software** build, sequenced from a laptop test-bench to the on-site Raspberry Pi.

---

## 0. Guiding principles

1. **Hardware-agnostic core.** All effects/animations write into an abstract `PixelBuffer` (1D array of RGB(W) values, indexed 0..N-1). A separate `Transport` layer ships that buffer to either:
   - a real **WLED/Gledopto** device over **DDP** (UDP port 4048), or
   - a **virtual simulator** (browser canvas / Three.js scene).
   Swapping mac ↔ Pi ↔ real hardware is a config change, not a code change.
2. **Single source of spatial truth.** A `config.yaml` describes controllers, strips, and the spatial layout. Everything (effects, simulator, GUI, MCP tool descriptions) reads from this — no hard-coded indices anywhere.
3. **Lean on what exists.** WLED already runs on the Gledopto and speaks DDP. LedFx already does audio→DDP. We won't reinvent the protocol or the audio pipeline; we wrap them with a control layer + spatial model + LLM-friendly API.
4. **Don't optimise prematurely for the Pi.** Build with stdlib + `numpy` and keep the per-frame work in vectorised array ops. A Pi 4 can comfortably push 60 FPS to 1800 LEDs over Ethernet if we don't do anything silly (per-pixel Python loops are the only real trap).
5. **Every layer is testable on a mac without LEDs plugged in.** No code path should require physical hardware to run; the simulator is the default transport during dev.

---

## 1. Architecture overview

```
                 ┌─────────────────────────────────────────────┐
                 │  Operator phone (mobile web UI, Phase 7)    │
                 │  + chat UI → LLM agent (OpenRouter, Ph. 6)  │
                 └────────────────┬────────────────────────────┘
                                  │ HTTPS (REST + SSE)
                  ┌───────────────▼────────────────┐
                  │  FastAPI control server        │
                  │  ─ REST endpoints (Phase 3)    │
                  │  ─ /agent/chat   (Phase 6)     │
                  │  ─ WebSocket → simulator       │
                  └───────────────┬────────────────┘
                                  │
            ┌─────────────────────▼──────────────────────┐
            │  Effect Engine (Phase 2)                   │
            │  ─ scheduled effects                       │
            │  ─ parameter envelopes                     │
            │  ─ audio modulation hooks (Phase 5)        │
            │  outputs ───► PixelBuffer (numpy array)    │
            └─────────────────────┬──────────────────────┘
                                  │
              ┌───────────────────▼─────────────────────┐
              │  Spatial mapper (config.yaml -> coords) │
              │  per-LED (x, y, z) + per-strip metadata │
              └───────────────────┬─────────────────────┘
                                  │
              ┌───────────────────▼─────────────────────┐
              │  Transport layer (pluggable)            │
              │   • DDPTransport ── UDP ──► WLED        │
              │   • SimTransport ── WS  ──► browser     │
              │   • MultiTransport (both at once)       │
              └─────────────────────────────────────────┘
```

Key idea: effects don't know where they're going. The `MultiTransport` lets us run real LEDs and the simulator simultaneously — invaluable for on-site debugging.

---

## 2. Tech stack choices

| Concern | Choice | Why |
|---|---|---|
| Language | **Python 3.11+** | Matches WLED ecosystem (LedFx, python-wled), good numpy perf, easy on Pi. |
| Web server | **FastAPI** + `uvicorn` | Async-first, OpenAPI docs out of the box, pairs cleanly with MCP. |
| LLM control panel | **`openai` SDK → OpenRouter** + `python-dotenv` | OpenAI-compatible client; OpenRouter routes to any model by string id. API key in `.env`, never in YAML. Single `update_leds` tool — current state + control surface auto-injected into the system prompt, no `list_*`/`get_*` round-trips. |
| Frame math | **`numpy`** | Vectorised RGB ops; fine on Pi 4 for 1800 px @ 60 FPS. |
| Config | **`pydantic` + YAML** | Validated config, IDE autocomplete, descriptive errors. |
| DDP client | tiny custom module (~50 lines) | The protocol is trivial; existing libs add deps without much value. Reference: `wledcast`, `ha_ddp2wled`. |
| Simulator (browser) | plain HTML + **Canvas2D** (later **Three.js**) served from FastAPI; live frames over **WebSocket** | No build step initially; Three.js for the 3D layout view in Phase 4. |
| Layout editor | same web app, drag/drop on Canvas, writes back to `config.yaml` (Phase 4) | Avoids a separate desktop tool. |
| Audio capture | **`sounddevice`** (PortAudio) | Cross-platform — same code on mac and Pi (with INMP441 via I²S/ALSA). |
| BPM detection | **`aubio`** primary, **`BeatNet`** as a research alternative | Aubio is light and proven; BeatNet is more accurate but heavier. |
| Process supervision | **systemd** on Pi | Auto-restart, runs at boot, integrates with read-only rootfs. |
| Remote access | **Tailscale** (or Cloudflare Tunnel) | The Pi sits behind an arbitrary venue WiFi NAT; Tailscale gives a stable, private URL without port-forwarding. |

---

## 3. Existing tools we'll reuse or borrow ideas from

Before writing anything, scan these — several solve sub-problems entirely:

- **[WLED](https://kno.wled.ge/)** — already on the Gledopto. Has a built-in 2D matrix mapper, gamma correction, hundreds of effects, and DDP receive. Our server *augments* WLED, it doesn't replace it. We can fall back to WLED's own effects if our process dies.
- **[LedFx](https://www.ledfx.app/)** — audio-reactive engine that already speaks DDP→WLED. Worth running once end-to-end early on as a sanity check that the network/DDP/Gledopto path is healthy, before our own effects exist.
- **[wled-sim](https://github.com/13rac1/wled-sim)** — desktop WLED simulator with a DDP listener on port 4048. Useful as a *receive-side* sanity check: aim our DDP packets at it and confirm pixels light up before we have our own browser visualizer.
- **[wled-tools](https://github.com/isra17/wled-tools)** — DDP parser + virtual LED viewer (`python -m wled_tools.viewer`). Same idea, more pythonic.
- **[WLEDVideoSync](https://github.com/zak-45/WLEDVideoSync)** — NiceGUI-based web visualizer that streams DDP. Good reference for the browser-side architecture.
- **[python-wled](https://github.com/frenck/python-wled)** — async client for WLED's JSON API. We'll use it to *configure* the Gledopto (set effect, brightness, segments) but **not** for realtime pixel data — that goes via DDP.
- **[WLED Pixel Studio](https://wps.tsp.tools/)** / **[wled-matrix-tool](https://github.com/kudp02/wled-matrix-tool)** — browser-based pixel editors. Useful inspiration for the layout/preview UI.
- **xLights** / **LedMapper** — full-blown 3D pixel-mapping tools. Overkill for two parallel lines, but worth a look if the install grows.

**Recommendation:** treat LedFx as the "stock" install path and only run our custom server when we want richer control or LLM integration. They can coexist (different effect at different times — only one talks to WLED at once).

---

## 4. Repo layout

```
animated_LED/
├── config/
│   ├── config.yaml             # active config
│   ├── config.dev.yaml         # mac/sim defaults
│   └── config.pi.yaml          # on-site defaults
├── src/
│   ├── ledctl/
│   │   ├── __init__.py
│   │   ├── config.py           # pydantic models
│   │   ├── topology.py         # strip/LED spatial model
│   │   ├── pixelbuffer.py
│   │   ├── transports/
│   │   │   ├── base.py
│   │   │   ├── ddp.py
│   │   │   ├── simulator.py
│   │   │   └── multi.py
│   │   ├── effects/
│   │   │   ├── base.py         # Effect ABC + parameter schema
│   │   │   ├── solid.py
│   │   │   ├── gradient.py
│   │   │   ├── wave.py         # the "left→right morph" example
│   │   │   ├── sparkle.py
│   │   │   └── audio_pulse.py  # Phase 5
│   │   ├── engine.py           # scheduler / mixer / FPS loop
│   │   ├── audio/
│   │   │   ├── capture.py      # sounddevice
│   │   │   ├── bpm.py          # aubio
│   │   │   └── features.py     # bands, RMS, onset
│   │   ├── api/
│   │   │   ├── server.py       # FastAPI app
│   │   │   ├── routes.py
│   │   │   ├── ws.py           # simulator websocket
│   │   │   └── agent.py        # /agent/chat (SSE), /agent/sessions/*
│   │   ├── agent/              # Phase 6
│   │   │   ├── client.py       # OpenRouter (OpenAI-compatible) wrapper
│   │   │   ├── session.py      # ChatSession + rolling message buffer
│   │   │   ├── tool.py         # single `update_leds` tool: schema + handler
│   │   │   └── system_prompt.py # rebuilt every turn (state + catalogue + audio)
│   │   └── cli.py              # `ledctl run` etc.
│   └── web/
│       ├── index.html          # operator UI + simulator
│       ├── editor.html         # spatial layout editor
│       └── static/
├── scripts/
│   ├── calibrate.py            # light strips one at a time, with on-screen IDs
│   └── stress_test.py          # burn-in: full white at full FPS
├── tests/
├── pyproject.toml
└── README.md
```

---

## 5. Phased plan

Each phase has a **Goal**, **Deliverables**, **Test you can run on the mac alone**, and **Risks**.

### Phase 0 — Project scaffolding (½ day)

**Goal:** repo, deps, lint, run target.

- `uv` for dep management.
- `ruff` + `pytest`.
- `ledctl run --config config/config.dev.yaml` entrypoint that just prints the parsed config — proves the wiring is real.

---

### Phase 1 — Topology, transport, and a working simulator (1–2 days)

**Goal:** End-to-end `effect → buffer → simulator` pipeline with **no hardware** and **no audio**, running at a stable FPS.

**Deliverables:**
- `config.yaml` schema (pydantic):
  ```yaml
  project: { name: festival_scaffold, target_fps: 60 }
  controllers:
    gledopto_main:
      type: wled-ddp
      host: 10.0.0.2
      port: 4048
      pixel_count: 1800
  strips:
    - id: top_left
      controller: gledopto_main
      output: 1
      pixel_offset: 0
      pixel_count: 450
      leds_per_meter: 30
      geometry: { type: line, start: [0, 1.0, 0], end: [-15, 1.0, 0] }
      reversed: true
    - id: top_right        { ...offset: 450, end: [15, 1.0, 0] }
    - id: bottom_left      { ...offset: 900, y: 0.0 }
    - id: bottom_right     { ...offset: 1350, y: 0.0 }
  transport:
    mode: simulator        # ddp | simulator | multi
    sim:
      ws_path: /ws/frames
  ```
- `Topology` object: every LED has a stable `(strip_id, local_index, global_index, x, y, z)` and a global ID. **This ID is the one we'll print on labels when wiring up.**
- `PixelBuffer`: numpy `(N, 3) uint8` array.
- `DDPTransport`: builds DDP packets (10-byte header, push flag on last packet, 480 px/packet to stay under MTU). Reference: `http://www.3waylabs.com/ddp/`.
- `SimTransport`: pushes frames over a WebSocket as a packed binary blob.
- A static `web/index.html` that opens the WS and draws every LED as a circle at its `(x,y)` from the config. Show measured FPS.
- `Engine` main loop: fixed timestep (e.g. 60 Hz), one effect for now (a moving sine wave), `time.perf_counter`-based pacing.

**Test on mac:**
- `ledctl run` → open `http://localhost:8000` → see ~60 FPS animated dots in the browser.
- Switch `transport.mode: ddp` and aim it at **wled-sim** running locally (port 4048) — confirms our DDP packets are valid.

**Risks:**
- DDP packet boundaries: only the *last* packet of a frame must have the PUSH bit set. Get this wrong and WLED holds the previous frame.
- Sending too fast: cap at `target_fps`; never spin-loop. Drop frames if the encode/transport falls behind.

---

### Phase 2 — Effect engine (1–2 days)

**Goal:** a small library of parameterised effects + a way to layer/blend them.

**Deliverables:**
- `Effect` ABC with a `parameters: pydantic.BaseModel` class attr. The schema is what the REST API (and later, the LLM) sees.
- A handful of effects whose parameter space is *expressive*, not exhaustive:
  - `solid(color)`
  - `gradient(stops, angle, speed)`
  - `wave(color_a, color_b, wavelength, speed, direction, softness)` — covers the "morph orange→red, smooth wave, left→right" prompt directly.
  - `sparkle(base, color, density, decay)`
  - `chase(color, length, speed)`
- `Mixer`: stack of effects with per-layer blend mode (`add`, `screen`, `multiply`, `mask`) and opacity. Crossfade between presets over N seconds.
- Effects work in **normalised spatial coords** (`x ∈ [-1, 1]`, `y ∈ [-1, 1]`) using the topology's bounding box, so writing a "left→right" effect doesn't depend on LED count or strip lengths. This is the abstraction that makes the LLM's job tractable.
- Gamma correction (2.2) before transport. WLED can also do this — pick one place, not both.

**Test on mac:** crossfade between two effects in the browser; confirm a "wave" written in normalised space looks identical regardless of strip count or whether the layout is split into 4 strips or 1.

---

### Phase 3 — REST control API (½ day)

**Goal:** drive the engine from `curl` / a phone.

**Endpoints (all JSON):**
- `GET  /state` — current effect stack, FPS, transport status.
- `GET  /effects` — list of effect names + their parameter schemas.
- `POST /effects/{name}` — push an effect onto the stack with params.
- `PATCH /layer/{i}` — tweak parameters live.
- `DELETE /layer/{i}` — remove.
- `POST /presets/{name}` — apply a saved preset (yaml file in `config/presets/`).
- `POST /blackout` / `POST /resume`.
- `GET  /topology` — strip + LED metadata, used by the editor UI.

OpenAPI docs at `/docs` auto-generated by FastAPI. This becomes the contract for both the mobile UI and the MCP layer.

**Test on mac:** drive a full show from `curl` while the simulator is open in the browser. No GUI needed yet.

---

### Phase 4 — Spatial GUI (layout editor + live visualizer) (2–3 days)

**Goal:** browser-based tool to (a) preview the live output and (b) edit `config.yaml` visually.

**Two views, same page:**
1. **Live view** — what we built in Phase 1, polished. Uses top-down 2D view. Hover an LED → tooltip with `global_index`, `strip_id`, `local_index`. Click → solo light it red (calibration mode for physical wiring).
2. **Editor view** — drag strip endpoints on a 2D canvas; edit length/density/orientation in a side panel. "Save" writes a new `config.yaml` (with confirmation diff). Reload the engine without restarting the process.

**Don't build from scratch if avoidable.** Concretely:
- Look at **xLights**' layout import format and **LedMapper** — if either's export can feed our pydantic schema, we ship that pipeline and skip the editor for v1.
- Otherwise build the minimal version: 2D canvas, drag endpoints, save YAML.

**Calibration helper script** (`scripts/calibrate.py`): walks the strip lighting LEDs at indices 0, 100, 200, … in red while the GUI labels which logical strip each is on. Used during physical install to verify wiring matches the config.

---

### Phase 5 — Audio analysis (1–2 days, mac-first)

**Goal:** real-time audio features the effect engine can read.

**Deliverables:**
- `audio.capture` — `sounddevice.InputStream` callback into a ring buffer; identical interface on mac (built-in mic) and Pi (INMP441 via ALSA).
- `audio.features` — RMS, broad-band energy (low/mid/high), onset flag, BPM (aubio).
- A shared `AudioState` object the effect engine reads each frame. Effects can opt in via a `modulators` dict in their params — e.g. `wave.speed = "bpm"` instead of a fixed number.
- Toggle in the GUI: input device, gain, AGC on/off, "tap tempo" override.

**Test on mac:** point the laptop mic at a speaker, see BPM lock onto the track in the GUI, watch the wave effect snap to the beat.

**Risks:**
- I²S on Pi is its own configuration adventure (`dtoverlay=googlevoicehat-soundcard` or similar for INMP441 — verify on Pi early in Phase 8, not on demo day).
- Latency: audio buffer + analysis hop + effect + DDP. Keep the audio buffer small (256–512 samples @ 48 kHz ≈ 5–10 ms) and analysis hop ≤ 1 frame.

---

### Phase 6 — Language-driven control panel via OpenRouter (1–2 days)

**Goal:** turn natural-language prompts into LED state changes as fast as possible. This is **not** an "agent loop" — there's no plan/observe/act cycle, no multi-tool orchestration. It's a thin language layer over the existing engine: the user types, the LLM emits **one** tool call describing the *complete* desired layer stack, the engine crossfades to it. Follow-ups like *"more red, slower"* work because the LLM is given a fresh, complete picture of the install on every turn.

**The shape:**
- **One tool: `update_leds`.** Its argument is a full `{layers, crossfade_seconds, blackout?}` spec — same shape as a preset YAML. The LLM never patches individual fields and never deletes single layers; every turn it emits the complete new state and the engine handles the morph. This makes "make it more red" simply: re-emit the current spec with redder colours.
- **No `list_*` / `get_*` / `describe_*` tools.** Everything the LLM might ask for is **auto-injected into the system prompt every turn**, freshly. No round-trips wasted on discovery — by the time the LLM sees the user's message, it already knows what's running, what tools/effects exist, and what the room sounds like.
- **Rolling buffer** of the last `N` messages (default **20**, `agent.history_max_messages`). Each user turn carries: `{user message}` + `{assistant tool call}` + `{tool result with the resulting state}`. The system prompt is regenerated fresh on every turn so the *current* state always reflects whatever the engine actually did, even if it differs from what the LLM intended (e.g. validation clamped a value).

**The system prompt (regenerated per turn):**
1. **Install description** — auto-summary of `Topology`: 1800 LEDs across 4 strips on two parallel rows, 30 m × 1 m, axis convention (`+x` stage-right, `+y` up, normalised coords).
2. **Current LED state** — the active layer stack as JSON (`{effect, params, blend, opacity}` per layer), `blackout`, last `crossfade_seconds`, `fps` / drops.
3. **Live audio reading** — `rms`, `peak`, and the three band levels with their **frequency ranges read from `config.audio.bands`** (e.g. *"low 20–250 Hz: 0.42 / mid 250–2000 Hz: 0.18 / high 2–12 kHz: 0.09"*). Snapshotted at the moment the user hits enter.
4. **Control surface** — the full effect catalogue (every effect, every param: type, range, units, one-line description), the list of available presets by name, and a short rubric on blend modes and opacity.
5. **Examples** — 3–5 example driving states across the design space (e.g. *"warm slow ambient: amber→red wave + sparkle on multiply"*, *"peak hour: fast fire-coloured chase + audio-pulse on add"*, *"blackout"*) so the model has anchors for what good output looks like.
6. **Rubric** — emit one `update_leds` per turn; the spec is the *complete* new state, never a diff; pick a `crossfade_seconds` that fits ("snappy ~0.3 s, smooth ~1.5 s, slow drift ~5 s"); keep the assistant text terse — the user can see the lights.

**The one tool:**
```python
update_leds(
    layers: list[Layer],            # ordered, layer 0 renders onto black
    crossfade_seconds: float = 1.0, # how fast to morph from old → new
    blackout: bool = False,         # convenience: kill output, ignore layers
)
# Layer matches the preset YAML schema:
# { effect: str, params: dict, blend: "normal"|"add"|"screen"|"multiply" = "normal", opacity: float = 1.0 }
```
Server-side, `update_leds` calls the existing `Mixer.crossfade_to(layers, duration)` — the same code path that powers `POST /presets/{name}`. Validation reuses each effect's pydantic schema; on failure the tool result contains the structured pydantic error, which the LLM sees on its next turn (via the buffer) and can correct.

**New API endpoints:**
- `POST   /agent/chat` — body `{message: str, session_id?: str}`. Streams (SSE) the assistant's tokens + the single tool call back to the browser. Creates a new session if `session_id` is omitted.
- `GET    /agent/sessions/{id}` — full transcript for UI rehydration.
- `DELETE /agent/sessions/{id}` — wipe.
- `GET    /agent/config` — model id, history cap (read-only; tweaks via YAML).

**New web view:** `/chat` — transcript on the left, input box at the bottom, "new session" + model dropdown at the top. Each assistant turn collapses the resulting `update_leds` payload under the bubble (so the operator can see exactly what was sent). The simulator stays on `/`; the operator can keep both tabs open.

**Config (YAML):**
```yaml
agent:
  enabled: true
  provider: openrouter
  model: anthropic/claude-sonnet-4-6      # any OpenRouter model id
  history_max_messages: 20                 # rolling buffer size, excl. system prompt
  request_timeout_seconds: 60
  rate_limit_per_minute: 30
  default_crossfade_seconds: 1.0           # used if the model omits the field
  # OPENROUTER_API_KEY is read from .env, never YAML
```
(No `max_tool_rounds_per_turn` — by design the model emits exactly one `update_leds` per turn. If it skips the tool call entirely, that's fine: it just answered with text and didn't change the lights.)

**Deps added:** `openai>=1.0` (OpenAI-compatible client; point `base_url` at `https://openrouter.ai/api/v1`), `python-dotenv`. **No FastMCP** in v1.

**Safety rails:**
- Per-effect param clamps come for free from the existing pydantic validation — same schemas the REST API already enforces.
- `rate_limit_per_minute` on `/agent/chat`, keyed by session id.
- If `OPENROUTER_API_KEY` is missing, `/agent/chat` returns a clear 503 with the cause; the render loop is unaffected.

**Test on mac (golden flow):**
1. *"warm slow wave, orange to red, drifting left"* → one `update_leds` call with one `wave` layer; engine crossfades.
2. Same session: *"more red, slower drift"* → one `update_leds` call with `color_a`/`color_b` shifted toward red and `speed` reduced. The freshly-injected current-state in the system prompt + the buffered prior turn give the model everything it needs.
3. *"add some sparkle on top"* → one `update_leds` call with a 2-layer spec (the wave from step 2 plus a sparkle layer).
4. *"blackout"* → one `update_leds(blackout=true)`.
5. Send 25 short messages; only the last 20 + the freshly-regenerated system prompt go to OpenRouter on turn 26.
6. Send a deliberately-broken request ("cyan and fluorescent puce in 17D") — the model picks reasonable defaults instead of asking. If validation rejects something, the next turn auto-corrects from the error in the tool result.
7. Disable the API key → endpoint returns 503 with a clear "missing OPENROUTER_API_KEY" message. `/state`, simulator, presets all keep working.

**Risks / open questions:**
- **System-prompt size.** Catalogue + current state + audio + examples is the heaviest part — probably 1.5–3k tokens. Comfortably within 20-message budget but worth measuring on the real model. Trim effect descriptions to one-liners and drop unused fields if it grows.
- **Audio-snapshot staleness.** We sample `AudioState` at `/agent/chat` request time. If the model takes 2 s to respond, the reading is 2 s old. Worth saying so in the rubric so the model treats it as "the room a moment ago", not "the room right now".
- **Persistence.** Sessions are in-memory for v1 — restart wipes them. Promote to disk (sqlite under `/var/lib/ledctl/sessions/`) once we know how it's used.
- **Streaming.** SSE for the assistant text; the tool-call payload is small enough to deliver as a single event.
- **Auth.** Same as the rest of the API — bind to `127.0.0.1` (or Tailscale only) until Phase 7 adds a shared password. OpenRouter key's blast radius is spend.
- **MCP path is deferred, not deleted.** The same `update_leds` schema can be re-exposed via FastMCP later if we want Claude Desktop to drive the install directly — same shape, no rewrite.

---

### Phase 7 — Operator mobile UI (1–2 days)

**Goal:** a phone-friendly page bar staff can pull up to change vibe quickly.

- Single page, big buttons:
  - All master sliders.
  - Chat window
- Auth: simple shared password "kaailed" on a query string (rotate per event). Behind Tailscale — never expose the Pi directly.
- PWA manifest so it installs as an app icon on the bar staff's home screens.

---

### Phase 8 — Move to the Pi (1 day on a quiet afternoon)

**Goal:** identical behaviour on the Pi, with INMP441 audio and Ethernet to the Gledopto.

- `config.pi.yaml`: `transport.mode: ddp`, controller host `10.0.0.2`, audio device set to the I²S card.
- Static IP on `eth0` = `10.0.0.1`, WLED static IP = `10.0.0.2` (matches the hardware doc).
- **WLED LED Preferences:** set color order to **GRB** (WS2815 default — leaving it on RGB swaps red/green and is the #1 "wrong colors" support thread). Confirm with a `solid(red)` effect from the engine.
- I²S setup (`/boot/firmware/config.txt`: `dtparam=i2s=on` + the right overlay for INMP441). Verify with `arecord -l` and a 3-second capture before plugging in to our pipeline.
- Install as a **systemd service** (`ledctl.service`):
  - `Restart=always`, `RestartSec=2`.
  - `After=network-online.target sound.target`.
  - `Nice=-5` (small priority bump; no real-time scheduling needed).
- Logs to `journald`, not files (compatible with read-only rootfs).
- **Read-only rootfs (overlayfs)** as in the hardware doc — flip on *after* everything works, not before. Anything we need to persist (presets, the active `config.yaml`) goes on a separate writable partition (e.g. `/var/lib/ledctl`).
- **Tailscale** installed on the Pi → stable hostname for the operator UI from any phone.
- **mDNS** (`avahi`) so the Pi answers to `ledctl.local` on the venue WiFi.

**Test on Pi:** repeat all the Phase 1–7 tests with `transport.mode: multi` so frames go to **both** the real LEDs and your laptop's browser simulator over Tailscale — invaluable for on-site debugging.

---

### Phase 9 — On-site reliability (½ day)

- **Watchdog**: if frames-sent FPS drops below 10 for >2 s, log + try to reconnect DDP socket; if WLED is unreachable for >30 s, fall back to running its built-in effect via the JSON API so the LEDs aren't dead.
- **Brownout behavior**: on startup, wait until WLED responds before pushing frames (`python-wled` has health checks).
- **Power-cycle resilience**: read-only FS (already covered), `systemd` auto-start, no SD writes in our hot path.
- **Heat**: Pi 4 in a sealed IP65 box doing audio FFTs — log CPU temp; if it sustains >75 °C, add the heatsink the hardware doc mentions (or a small thermal pad to the box wall).
- **Time**: install `chrony` for NTP — log timestamps need to be useful post-event.
- **Backups**: image the SD card the night before the event. Keep a spare card pre-flashed.

---

## 6. What I think the brief was missing

A few things worth deciding now rather than mid-build:

1. **Latency budget.** Audio→light should land under ~30 ms to feel "tight" on beats. That constrains the audio buffer (≤256 samples @ 48 kHz) and the DDP send cadence. Worth measuring early, not at soundcheck.
2. **Coordinate convention (decided).** `+x` = stage-right, `+y` = up, `+z` = out toward the audience (right-handed). Origin `(0, 0, 0)` is pinned to the **centre of the scaffolding** — i.e. the 15 m mark horizontally, midway between the top and bottom rows vertically. All effects work in **normalised** spatial coords (`x, y ∈ [-1, 1]`) derived from the topology's bounding box, so "left → right" is unambiguous regardless of how strips are split or reversed in `config.yaml`.
3. **Per-LED ID labelling for physical install.** The calibration script (Phase 4) is the bridge between software IDs and physical strips — print a small sticker for each strip endpoint with its `global_index` range. Future-you on a ladder at midnight will be grateful.
4. **Effect transitions.** Hard cuts look amateur. Build a crossfade primitive into the mixer from day one — much harder to retrofit.
5. **Brightness ceiling = current ceiling.** WS2815 is 12 V with 3 LEDs in series per pixel and draws ~15 mA at full white, so 1800 px ≈ **27 A theoretical** — matching the hardware doc's number, not the 60 mA/pixel WS2812B figure. With 2× HLG-320H-12 (2× ~26.6 A capacity, split across the two halves) there's headroom, but the engine should still enforce a per-frame *power estimate* clamp (sum of channel values × calibration factor) and scale the whole frame down if it would exceed budget — cheap to add, prevents PSU foldback during a "all white" effect mistake.
6. **Two PSUs / two halves.** If they ever boot at slightly different times, the data line can momentarily see a 12 V row next to a 0 V row. WLED handles this fine, but our effects shouldn't *assume* both halves are alive — always render the full frame and let any dark half just be dark.
7. **Multi-controller future.** The schema supports multiple controllers from day one (`controllers:` is a dict, not a single entry). Adding a third row of LEDs later is then a config change.
8. **LLM cost / rate limits.** OpenRouter bills per token. The system prompt (catalogue + state + audio + examples) is regenerated every turn — that's the dominant token cost, so keep effect descriptions one-liner-tight. The rolling-buffer cap is the secondary lever. The control panel is for "set the vibe" prompts and follow-up adjustments at human typing speed, not per-frame control — say so in the system prompt rubric so the model doesn't try to drive an animation by spamming turns.
9. **Where logs go under read-only FS.** Pick: `journald` (volatile, fine for live), a writable `/var/lib/ledctl/logs` partition, or shipping to a remote endpoint (e.g. a free Logtail tier). Decide before flipping the FS to read-only.
10. **Chat UI streaming.** SSE from the FastAPI app is the path of least resistance for token streaming — same host as the simulator/operator UI, so a single Tailscale hostname covers the whole control surface. (If we ever revive the MCP path for Claude Desktop, it would also speak SSE on the same host.)

---

## 7. Will this work? A short answer

**Yes — and almost all of it can be built and validated on the mac before any soldering.** The seams that genuinely require hardware are:

- I²S microphone on the Pi (Phase 8 task; allow a buffer day for ALSA fiddling).
- The first DDP packet to a real Gledopto (Phase 8 — but `wled-sim` and the in-browser visualizer cover ~95 % of the same testing on the mac).
- Power-injection-induced data line behaviour — pure hardware, not in software's hands.

The hardware-agnostic-core + transport-swap pattern means there's exactly one config line to flip when you go from "developing on the couch" to "running the festival": `transport.mode: simulator` → `multi` → `ddp`. Everything above it (effects, audio, REST, MCP, mobile UI) is identical code running on identical inputs.

---

## 8. Suggested execution order if you only have a few evenings

If you want a *visible result* fast and then iterate:

1. **Evening 1**: Phase 0 + Phase 1 → wave animation in the browser at 60 FPS.
2. **Evening 2**: Phase 2 + minimal Phase 3 → swap effects from `curl`.
3. **Evening 3**: Phase 4 (live view first, editor later) → looks "real."
4. **Evening 4**: Phase 5 → audio reactivity to a Spotify track playing through the laptop.
5. **Evening 5**: Phase 6 → first language-driven session end-to-end (initial request *and* a "make it more X" follow-up that the model handles by re-emitting a refined `update_leds` spec — proving the rolling buffer + per-turn state injection actually carry context).
6. **Weekend before the event**: Phase 7 (mobile UI) + Phase 8 (Pi cutover) + Phase 9 (reliability).

That's a working show with weeks of margin to refine effects, build presets, and add bells and whistles.
