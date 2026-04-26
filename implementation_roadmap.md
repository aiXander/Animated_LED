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
                 │  + LLM agent (MCP client, Phase 6)          │
                 └────────────────┬────────────────────────────┘
                                  │ HTTPS / MCP
                  ┌───────────────▼────────────────┐
                  │  FastAPI control server        │
                  │  ─ REST endpoints (Phase 3)    │
                  │  ─ MCP server  (Phase 6)       │
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
| MCP server | **`fastmcp`** | Actively maintained, clean tool-schema story; mounts alongside FastAPI so REST and MCP share routes. |
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
│   │   │   └── mcp.py          # fastmcp mount
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
1. **Live view** — what we built in Phase 1, polished. Toggle between top-down 2D and isometric 3D (Three.js). Hover an LED → tooltip with `global_index`, `strip_id`, `local_index`. Click → solo light it red (calibration mode for physical wiring).
2. **Editor view** — drag strip endpoints on a 2D canvas; edit length/density/orientation in a side panel. "Save" writes a new `config.yaml` (with confirmation diff). Reload the engine without restarting the process.

**Don't build from scratch if avoidable.** Concretely:
- Look at **xLights**' layout import format and **LedMapper** — if either's export can feed our pydantic schema, we ship that pipeline and skip the editor for v1.
- Otherwise build the minimal version: 2D canvas, drag endpoints, save YAML. Add 3D later.

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

### Phase 6 — MCP server for LLM control (½–1 day)

**Goal:** an LLM agent can take "make the LEDs morph gently from orange to red in a smooth wave going from left to right" and turn it into the right API calls.

- Mount **`fastmcp`** alongside the FastAPI app — REST routes' pydantic schemas become MCP tool input schemas, "for free."
- Add **descriptions** to each effect's params (`Field(..., description="…")`) — these are the prompts the LLM sees. This is where vibecoding pays off: write descriptions like you'd brief a junior collaborator ("speed in cycles per second; 0.1 is gentle, 2.0 is aggressive").
- Add a **`/describe_topology` tool** that returns a short natural-language summary of the install (dimensions, orientation, LED count) so the LLM has spatial context without us hard-coding it in a system prompt.
- Add **safety rails**: clamp brightness, clamp parameter ranges, rate-limit `POST /effects` so an over-eager agent can't thrash. The MCP `mcpinfo` description should mention these limits.

**Test on mac:** Claude Desktop (or the agent SDK in a script) connected to the MCP endpoint. Type the morph-wave prompt; watch the simulator do the right thing.

---

### Phase 7 — Operator mobile UI (1–2 days)

**Goal:** a phone-friendly page bar staff can pull up to change vibe quickly.

- Single page, big buttons:
  - 4–8 saved presets ("chill", "peak", "cooldown", "blackout").
  - Master brightness slider.
  - Master speed scaler (multiplies all effect `speed` params).
  - Color picker for the "primary" colour (effects with a primary slot reflect it).
  - A free-text box → POSTs to a `/agent/prompt` endpoint that calls the MCP tools server-side. Same code path as the LLM, just initiated by a human.
- Auth: simple shared password on a query string or basic auth; rotate per event. Behind Tailscale or Cloudflare Tunnel — never expose the Pi directly.
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
8. **LLM rate limits / cost.** The MCP path is fine for "set the vibe" prompts, not for per-frame control. Make that explicit in tool descriptions so the agent doesn't try to drive an animation by polling.
9. **Where logs go under read-only FS.** Pick: `journald` (volatile, fine for live), a writable `/var/lib/ledctl/logs` partition, or shipping to a remote endpoint (e.g. a free Logtail tier). Decide before flipping the FS to read-only.
10. **MCP transport.** `fastmcp` supports SSE and stdio. SSE over Tailscale is the path of least resistance — but for an LLM running in the cloud, you'll want the public URL of the operator UI to *also* be the MCP endpoint, not a separate one.

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
5. **Evening 5**: Phase 6 → first LLM-driven prompt working end-to-end.
6. **Weekend before the event**: Phase 7 (mobile UI) + Phase 8 (Pi cutover) + Phase 9 (reliability).

That's a working show with weeks of margin to refine effects, build presets, and add bells and whistles.
