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
- `masters.py` — Operator-owned `MasterControls` (`brightness`, `speed`, `audio_reactivity`, `saturation`, `freeze`) and per-frame `RenderContext`. Deliberately kept out of the DSL — the LLM never produces or alters them. Bounds enforced in `clamped()`: brightness/saturation ∈ [0, 1], speed/audio_reactivity ∈ [0, 3], freeze: bool. Audio reactivity is a single multiplier applied to the band scalars from the external feed; there is no in-process feature cleaning slider.
- `mixer.py` — Stack of `Layer(node, blend, opacity)` with blend modes (normal/add/screen/multiply), crossfade between stacks, master output stage. Crossfade alpha uses `ctx.wall_t` so operator direction isn't slowed by `speed=0.5` or stopped by `freeze=true`. Per-layer rendering uses `ctx.t` (master-speed-scaled).
- `pixelbuffer.py` — Float32 working buffer in [0, 1]. Gamma (default 2.2) applied once here, not in WLED.
- `transports/` — Pluggable output: `simulator` (WebSocket to browser), `ddp` (UDP to WLED, 480 px/packet, PUSH on the final packet only), `multi` (both). Swapped via `config.transport.mode`.
- `audio/` — Thin bridge to the external [Realtime_PyAudio_FFT](https://github.com/Jaymon/Realtime_PyAudio_FFT) audio-feature server. The LED controller does **not** capture or analyse audio itself — it spawns the audio-server as a subprocess and consumes its OSC feed:
  - `state.py` — `AudioState` holds the latest `low/mid/high` band energies (already auto-scaled to ~[0, 1] on the audio server side), plus device name, samplerate, blocksize, band cutoffs, and a `connected` flag. Single writer (the OSC listener thread), many lock-free readers.
  - `bridge.py` — `OscFeatureListener` (UDP socket on port 9000 by default; `/audio/lmh` + `/audio/meta` handlers; staleness watchdog), `AudioServerSupervisor` (launches the `audio-server` console script as a subprocess, pipes its stdout into the LED logger, terminates cleanly on shutdown), and `AudioBridge` that owns both. Failures are deliberately soft: if the binary is missing, port is taken, or packets stop arriving, the LED render loop keeps going with `audio_band` returning 0 and a warning in the terminal.
  - The audio-server's own browser UI (default `http://127.0.0.1:8766`) is the canonical place to pick the input device, retune band cutoffs, and save presets. The 'audio' link in the LED operator UI opens that URL in a new tab.
- `agent/` — Phase 6 language-driven control panel. Thin layer over OpenRouter, NOT a multi-tool agent loop:
  - `tool.py` — single `update_leds(layers, crossfade_seconds, blackout)` tool. The argument is the *complete* new layer stack as a tree of `{kind, params}` primitives, never a diff. The surface compiler type-checks the tree (palette in a scalar slot is rejected, leaf must be `rgb_field`, etc.); on failure the tool result carries a structured `{path, msg, valid_kinds}` error which the LLM sees on the next turn (via the rolling buffer) and self-corrects. Calls `Engine.crossfade_to` — same code path as `POST /presets/{name}`.
  - `system_prompt.py` — `build_system_prompt(...)` regenerated **fresh every turn**: install summary, current layer-stack JSON, audio snapshot, **read-only master values**, full primitive catalogue from `surface.generate_docs()`, anchor examples, anti-patterns. Dominant token cost — keep primitive `Params` `description=` strings tight.
  - `session.py` — in-memory `SessionStore` + `ChatSession` with `history_max`-capped rolling buffer (heals dangling `tool` messages after trim) and per-session rolling-window rate limit. Sessions wipe on restart (v1).
  - `client.py` — thin OpenAI-compatible wrapper aimed at OpenRouter. Imports `openai` lazily; `MissingApiKey` raised at first call.
- `api/server.py` — FastAPI app. Endpoints: `/state`, `/topology`, `/config` (PUT for layout edits), `/surface/primitives`, `/layers` (POST/PATCH/DELETE), `/masters` (GET/PATCH), `/presets` (GET list, POST save current stack as new preset), `/presets/{name}` (POST to apply), `/blackout` + `/resume`, `/calibration/*`, `/audio/state` + `/audio/ui` (read-only — device picking lives on the external audio-server), `/healthz`, `/ws/frames` (WebSocket frame broadcast), `/ws/state` (WebSocket state broadcast). Landing page (`/`) hosts the LED viz, chat UI, and live status panel; `/editor` is the layout editor.
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
| `scalar_t`      | one scalar per frame                   | `lfo`, `audio_band`, `constant`                                   |
| `palette`       | 256-entry RGB LUT                      | `palette_named`, `palette_stops`                                  |
| `rgb_field`     | per-LED RGB (the layer leaf)           | `palette_lookup`, `solid`                                         |

Polymorphic combinators (`mix`, `mul`, `add`, `screen`, `max`, `min`, `remap`, `threshold`, `clamp`, `range_map`, `trail`) resolve their output kind from their inputs at compile time and broadcast where it makes sense (`rgb_field × scalar_t → rgb_field`, `palette × palette → palette` for `mix`).

**Two pieces of sugar in the spec language:**
- a bare number anywhere a node is expected becomes a `constant` (so `"speed": 0.3` is fine);
- a bare palette string becomes a `palette_named` (so `"palette": "fire"` is fine).

**Modulation lives directly on the parameter.** Instead of an old-style `bindings.brightness` slot, you pass an `audio_band(...)` node into `palette_lookup.brightness`. Audio reactivity is composable: `audio_band(band="low"|"mid"|"high")` returns a `scalar_t` sourced from the external audio-feature server (already smoothed and auto-scaled to ~[0, 1] — all attack/release/shaping live upstream and are tuned in the audio server's UI, not here). Wrap an `audio_band` in `range_map(in_min=0, in_max=1, out_min=floor, out_max=ceiling)` if you need a baseline glow or soft cap.

**Adding a new primitive:**
1. Write a `@primitive` class in `surface.py` with a pydantic `Params` model and a `compile()` that returns a `CompiledNode` of the right `output_kind`.
2. That's it. The doc generator, REST primitive catalogue, and LLM system prompt all pick it up.

The `Params` `description=` strings are user-facing — they feed both `GET /surface/primitives` and the LLM system prompt. **One line per field is the budget** (this is the dominant token cost).

Named palettes ship in `surface.NAMED_PALETTES`: `rainbow`, `fire`, `ice`, `sunset`, `ocean`, `warm`, `white`, `black`, `mono_<hex>`. Custom palettes are `{kind: "palette_stops", params: {stops: [{pos, color}, …]}}`.

Presets live in `config/presets/<name>.yaml` (sibling of the active config file). Each preset is `{ crossfade_seconds, layers: [{ node: {kind, params}, blend, opacity }, …] }`. Seed presets shipped today: `default`, `chill`, `peak`, `color_waves`, `gentle_whiteblue_waves`, `red_waves_blue_base`, `snare_sparkles`, `soft_purple_blue_wave`, `sunset_breathing`. Loaded by `presets.py`; saved via `POST /presets`.

## Coordinate convention

Right-handed: `+x` = stage-right, `+y` = up, `+z` = toward audience. Origin = centre of scaffolding. All primitive math in normalised [-1, 1].

## Phase status

Phases 0–6 are complete (topology, DDP transport, surface engine, REST API, browser simulator + layout editor, audio analysis, language-driven control panel).

Phase 8.1's digital prep also landed early — see "Auth + Pi deploy artefacts" below. Phase 7 (mobile operator UI), the rest of Phase 8 (INMP441 I²S setup, Tailscale, read-only rootfs, on-site bring-up), and Phase 9 (reliability/watchdog) are next.

## Auth + Pi deploy artefacts

- `src/ledctl/api/auth.py` — shared-password gate (off in dev, on for Pi). Activated by setting `auth.password` in YAML. The cookie is `ledctl_auth`; first-visit login via `/login` form post or `?password=…` query. WS upgrades reject pre-accept with close code 4401 if the cookie is missing/wrong. `/login`, `/logout`, `/healthz` are always public so future watchdogs can probe past the gate. Render loop and DDP transport are unaffected.
- `config/config.pi.yaml` — `auth.password: kaailed`, `server.host: 0.0.0.0`, full `masters:` block, `audio_server:` block (autostart=true, OSC port 9000, UI at :8766). The audio device picked at the audio-server's UI is persisted in *its* config.yaml — the LED config has no device field.
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

## External audio dependency

Audio capture / FFT / band-energy extraction is owned by [Realtime_PyAudio_FFT](https://github.com/Jaymon/Realtime_PyAudio_FFT). Install that package alongside `ledctl` so its `audio-server` console script is on PATH; ledctl will spawn it as a subprocess on boot (`audio_server.autostart: true`). If you'd rather run it manually (e.g. tuning bands via its UI on a different machine), set `audio_server.autostart: false` and start it however you like — the LED server will still happily consume the OSC feed.

The audio-server stores its own settings in its own `config.yaml` (device, band cutoffs, smoothing, autoscale window). Tune those from its browser UI at `http://127.0.0.1:8766` — the 'audio' link in the LED operator UI opens it in a new tab. Soft-fail behaviour: if the audio-server isn't running, the OSC port is taken, or packets stop arriving, the LED render loop keeps going with `audio_band` returning 0 and a warning in the terminal.
