# ledctl — audio-reactive LED installation

Python control layer for a 1800-LED festival install (4 × 450 WS2815 strips fed by a centre-mounted Gledopto / WLED via DDP). Mac-first dev with a browser simulator; same code ships to the Pi at the venue with a one-line config flip.

Design docs in repo root:
- `Audio-Reactive LED installation.md` — hardware/electrical build (gear, power injection, waterproofing, signal integrity)
- `implementation_roadmap.md` — software roadmap, phases 0–9

---

## Run it yourself

```bash
# one-time
uv venv --python 3.11
uv pip install -e ".[dev]"

# run
.venv/bin/ledctl run --config config/config.dev.yaml
# open http://127.0.0.1:8000  →  see the wave at ~60 FPS

# inspect parsed config
.venv/bin/ledctl show-config --config config/config.dev.yaml

# tests / lint
.venv/bin/pytest
.venv/bin/ruff check src tests
```

`ledctl run` accepts `--host`, `--port`, `--log-level`. Defaults come from the config's `server` block.

---

## Switching transport

`config.transport.mode` is the only thing that changes between mac dev and the Pi at the venue:

| Mode        | Where frames go                                         | Use for             |
| ----------- | ------------------------------------------------------- | ------------------- |
| `simulator` | WebSocket `/ws/frames` → browser canvas                 | mac dev (default)   |
| `ddp`       | UDP DDP → controller `host:port`                        | Pi → real Gledopto  |
| `multi`     | both at once (sim + DDP)                                | on-site debugging   |

To sanity-check the DDP packet shape without real hardware, run [`wled-sim`](https://github.com/13rac1/wled-sim) on `localhost:4048`, set `transport.mode: ddp`, and aim at it.

---

## Layout

```
animated_LED/
├── config/
│   ├── config.dev.yaml      # mac/sim defaults (transport.mode = simulator)
│   ├── config.pi.yaml       # on-site defaults (transport.mode = ddp, host 10.0.0.2)
│   └── presets/             # YAML preset files: chill, peak, cooldown
├── src/
│   ├── ledctl/
│   │   ├── config.py        # pydantic schema + load_config()
│   │   ├── topology.py      # per-LED (strip_id, local_index, global_index, x,y,z)
│   │   ├── pixelbuffer.py   # float32 working buffer, uint8 + gamma at the boundary
│   │   ├── effects/         # Effect ABC + registry + wave/solid/gradient/sparkle/chase/audio_pulse
│   │   ├── mixer.py         # layer stack, blend modes, crossfade, blackout
│   │   ├── presets.py       # YAML preset loader
│   │   ├── transports/      # base / ddp / simulator / multi
│   │   ├── audio/           # capture / features / shared AudioState (Phase 5)
│   │   ├── engine.py        # fixed-timestep async render loop, layer mutation API
│   │   ├── api/server.py    # FastAPI: /state, /topology, /effects, /layer/{i}, /presets, /blackout, /audio*, /ws/frames
│   │   └── cli.py           # `ledctl run` / `ledctl show-config`
│   └── web/
│       ├── index.html       # Canvas2D simulator viewer
│       ├── editor.html      # spatial layout editor (Phase 4)
│       └── audio.html       # audio device picker + live meter (Phase 5)
└── tests/
```

---

## Architecture in one paragraph

`Engine` ticks at `target_fps` using `time.perf_counter`. Each tick: clear the `PixelBuffer` (float32 RGB ∈ [0,1]) → `Mixer.render(t, out)` walks the layer stack (each `Effect.render` writes into a scratch buffer, the mixer blends it onto the accumulator with the layer's `blend` + `opacity`) → `to_uint8(gamma)` at the transport boundary → `Transport.send_frame`. Transports are pluggable (`SimulatorTransport` broadcasts to all WS clients, `DDPTransport` chunks to UDP packets with PUSH on the last only, `MultiTransport` fans out). Effects are deliberately blind to LED count and strip layout — they only see `topology.normalised_positions` (each axis in [-1, 1]), so "left → right" is unambiguous regardless of how strips are split or reversed.

---

## Effects, layers, and presets (Phase 2)

Five effects ship in `src/ledctl/effects/`, each with a pydantic `Params` class whose field `description=`s are what the LLM (Phase 6) and operator UI (Phase 7) will read:

| name       | params (highlights)                                                          |
| ---------- | ----------------------------------------------------------------------------- |
| `solid`    | `color`                                                                       |
| `gradient` | `stops` (list of `{pos, color}`), `direction`, `speed` (0 = anchored, ≠0 wraps), `cross_phase` (per-axis phase offset, e.g. `[0, 0.05, 0]` to skew the wave between top and bottom rows) |
| `wave`     | `color_a`, `color_b`, `wavelength`, `speed`, `direction`, `softness`            |
| `sparkle`  | `base`, `color`, `density`, `decay`, `seed`                                   |
| `chase`    | `color`, `length`, `speed`, `direction`                                       |

The `Mixer` holds an ordered layer stack; layer 0 renders onto black, each subsequent layer blends with `blend ∈ {normal, add, screen, multiply}` and `opacity ∈ [0, 1]`. `crossfade_to(new_layers, duration)` renders both stacks for `duration` seconds and lerps between them — no hard cuts.

Gamma 2.2 is applied once, in `PixelBuffer.to_uint8(gamma)`, configurable via `output.gamma` in YAML. Set it to `1.0` if WLED is also gamma-correcting (don't double up).

Presets live in `config/presets/<name>.yaml` (sibling of the active config file). Each preset is `{ crossfade_seconds, layers: [{ effect, params, blend, opacity }, ...] }`. Three seed presets ship: `chill`, `peak`, `cooldown`.

---

## REST API (Phase 3)

OpenAPI docs at `http://127.0.0.1:8000/docs`. All JSON.

| method  | path                | what it does                                              |
| ------- | ------------------- | --------------------------------------------------------- |
| GET     | `/state`            | fps, target, frames, drops, transport mode, blackout, crossfading, current layer stack, gamma |
| GET     | `/topology`         | strip + per-LED metadata (used by the simulator viewer)   |
| GET     | `/config`           | full parsed config (strips section is editable via PUT)   |
| PUT     | `/config`           | replace `strips`, validate, write YAML (`.bak` first), hot-swap topology — body `{strips: [...]}` |
| GET     | `/editor`           | serves the layout editor view (`/web/editor.html`)        |
| POST    | `/calibration/solo` | light only the listed `indices` in red — body `{indices: [int]}` |
| POST    | `/calibration/walk` | sweep the chain, one LED at a time — body `{step?, interval?}` |
| POST    | `/calibration/stop` | clear any active calibration override                     |
| GET     | `/effects`          | `{name: {params_schema: <pydantic JSON schema>}}`         |
| POST    | `/effects/{name}`   | push a layer; body `{params, blend, opacity}` (all optional) |
| PATCH   | `/layer/{i}`        | partial update; body `{params?, blend?, opacity?}`        |
| DELETE  | `/layer/{i}`        | drop the layer at index `i`                               |
| POST    | `/blackout`         | force black until `/resume`                               |
| POST    | `/resume`           | leave blackout mode                                       |
| GET     | `/presets`          | list of preset names found in the presets dir             |
| POST    | `/presets/{name}`   | crossfade into the preset; body `{crossfade_seconds?}` overrides the preset's own duration |

Quick smoke test from a second terminal while `ledctl run` is up:

```bash
curl -s localhost:8000/state | jq .layers
curl -s -X POST localhost:8000/effects/sparkle \
  -H 'content-type: application/json' \
  -d '{"params":{"density":0.2,"color":"#a8c0ff"},"blend":"add","opacity":0.6}'
curl -s -X POST localhost:8000/presets/peak | jq .
curl -s -X POST localhost:8000/blackout && curl -s -X POST localhost:8000/resume
```

---

## Audio (Phase 5)

`audio.capture` wraps `sounddevice.InputStream`; the PortAudio callback computes RMS, peak, and three FFT band energies (low 20–250 Hz, mid 250 Hz–2 kHz, high 2–12 kHz) into a shared `AudioState`. The render loop reads scalar fields from it without locking, and `Topology.audio_state` exposes it to effects (`audio_pulse` is the seed effect — sample any band, scale brightness with `floor` / `ceiling` / `sensitivity`).

The microphone is hardcoded in `config.yaml` under `audio:`:

```yaml
audio:
  enabled: true
  device: null            # null = system default; or a name fragment, or an index
  samplerate: 48000
  blocksize: 512
  channels: 1
  gain: 1.0
  smoothing: 0.4
```

Open `http://127.0.0.1:8000/audio` to:
- list every input device the OS sees (works on mac built-in mic and Pi I²S/ALSA),
- watch a live amplitude meter polled at ~20 fps (RMS, peak hold, low/mid/high bars),
- pick a device and click **apply & save** — the chosen name is written back into `audio.device` (a `.bak` of the previous config is kept alongside).

| method  | path             | what it does                                                      |
| ------- | ---------------- | ------------------------------------------------------------------ |
| GET     | `/audio`         | the audio config / live meter page                                |
| GET     | `/audio/devices` | enumerate input devices (`{index, name, hostapi, …}`)             |
| GET     | `/audio/state`   | current `{enabled, device, samplerate, rms, peak, low, mid, high}` |
| POST    | `/audio/select`  | switch device live; body `{device: str|int|null, persist?: bool}` |

`/state` also includes an `audio` block so a single poll covers everything.

The `audio_pulse` effect joins the registry next to `wave`/`solid`/`gradient`/`sparkle`/`chase`:

| name          | params (highlights)                                                     |
| ------------- | ----------------------------------------------------------------------- |
| `audio_pulse` | `color`, `band` (`rms`/`peak`/`low`/`mid`/`high`), `floor`, `ceiling`, `sensitivity`, `decay_seconds` (peak-hold time constant; 0 = follow signal directly) |

Stack it under another effect with `blend: multiply` for "fades to dark on quiet" or with `blend: add` for "punches up on the kick."

The default boot stack now layers a fire-coloured (orange / amber / red) `gradient` scrolling left → right (with a small `cross_phase` so the top row leads the bottom by ~0.1 cycles) under an `audio_pulse` with `blend: multiply`, `floor=0.5`, `ceiling=1.0`, and `decay_seconds=0.5` — the whole frame breathes between 50–100% brightness on the room's RMS, and a kick-drum spike lingers for ~half a second instead of snapping back. Tweak any of it via `PATCH /layer/{i}` or replace it with `POST /presets/{name}`.

---

## Spatial GUI (Phase 4)

Two views, one process:

- **`/`** — live simulator. Hover an LED → tooltip with `global_index`, `strip_id`, `local_index`. Click → solo it red (calibration mode); click again or hit "clear" in the banner to release.
- **`/editor`** — drag strip endpoints on a 2D canvas, edit numeric fields in the side panel, "preview diff" before "save & reload". Saving PUTs `/config`, which validates the new layout (overlap detection, controller-capacity check), writes the YAML to disk (with a `.bak` of the previous version), and hot-swaps the engine topology — current layer specs are preserved across the swap, even if `pixel_count` changes.

`scripts/calibrate.py` drives `/calibration/walk` from a terminal so the operator on the ladder gets a printed running log of which `global_index` is lit and which strip it belongs to:

```bash
python scripts/calibrate.py --base-url http://ledctl.local:8000 --step 100 --interval 1.5
# or step manually with the Enter key:
python scripts/calibrate.py --manual --step 50
```

The walk runs server-side; the script is purely a label printer + lifecycle manager (Ctrl+C calls `/calibration/stop`).

---

## Coordinate convention

Per roadmap §6 item 2 (locked):
- `+x` = stage-right, `+y` = up, `+z` = out toward audience (right-handed)
- Origin `(0, 0, 0)` = centre of the scaffolding (15 m horizontally, midway between top and bottom rows)
- Effects work in **normalised** coords (`x, y ∈ [-1, 1]`) derived from the topology bounding box

Strip semantics in `config.yaml`:
- `geometry.start` = position of the **first LED in the data chain** (`local_index=0`, the LED nearest the controller output)
- `geometry.end` = position of the last LED in the chain
- `reversed: true` swaps that mapping (use it when a strip ends up mounted backwards)

For centre-feed all 4 chain heads sit at `x=0`, so the dev/pi configs use `start: [0, …], end: [±15, …]` and no `reversed` flags.

---

## Choices made (Phase 0–3) worth re-examining later

1. **Row separation = 1 m** in the dev/pi configs (`y = ±0.5`). Placeholder — replace with the measured value once the scaffolding is up.
2. **DDP destination id = 1** (WLED's default primary output). Configurable on `DDPTransport(..., dest_id=...)` if a multi-segment WLED setup needs a different id.
3. **480 px/packet** in DDP. 1440 byte payload + 10 byte header = 1450 < 1500 MTU. Don't raise without checking the path MTU.
4. **PUSH flag only on the final packet** of each frame. Per the DDP spec — getting this wrong means WLED holds the previous frame.
5. **PixelBuffer is float32 internally**, converted to uint8 at the transport. Mixer blends in linear space; gamma is applied once in `to_uint8`.
6. **Gamma 2.2 in `PixelBuffer.to_uint8`** (configurable via `output.gamma`). If you turn on WLED's own gamma, set this to `1.0` — never both.
7. **WS frame format is raw packed RGB bytes** (`N×3`). Browser fetches `/topology` once for positions. No per-frame metadata.
8. **`asyncio.wait_for` on a stop-event** is how the engine paces sleep, so `engine.stop()` returns promptly without waiting for the next tick.
9. **Engine drops frames rather than spiralling** if it falls behind (`engine.dropped_frames` is exposed via `/state`).
10. **Default boot stack = single `wave` layer** so the install isn't dark on startup. The API can replace it via `/presets/{name}` or `/effects/{name}` + `DELETE /layer/0`.
11. **No auth on the REST API yet.** Phase 7 adds a shared password + Tailscale; until then, only bind to `127.0.0.1` (the default).
12. **No `git init`** yet. Repo is plain files; add when the user wants commits.
13. **No tests for `MultiTransport`** because it instantiates a real DDP socket at app boot — needs a UDP listener fixture. Add when we exercise that path.

---

## Test surface

- `tests/test_config.py` — dev/pi YAML loads, overlap/over-capacity rejection
- `tests/test_topology.py` — 1800 LEDs total, bbox spans 30×1 m, normalised in [-1,1], `reversed` semantics
- `tests/test_ddp.py` — packet count, PUSH-only-on-last, payload round-trip, single-packet frame still has PUSH
- `tests/test_wave_effect.py` — wave bounded and travels over time
- `tests/test_effects.py` — registry + each new effect's output bounded; gradient endpoints align with stops; sparkle reproducible with a seed; chase moves
- `tests/test_mixer.py` — blend modes (normal, add, screen, multiply), opacity lerp, blackout zeros output, crossfade transitions and snaps to target after `duration`
- `tests/test_pixelbuffer.py` — clip + rounding in `to_uint8`, gamma 2.2 darkens midtones
- `tests/test_api.py` — driven via FastAPI `TestClient` (exercises engine lifespan): `/state`, `/effects` schemas, push/patch/delete layer, blackout/resume, preset application + 404/422 paths
- `tests/test_audio_features.py` — RMS / peak / band split (sine-tone isolation in low / mid / high bins)
- `tests/test_audio_pulse.py` — `audio_pulse` reads `topology.audio_state`, honours floor / ceiling / sensitivity
- `tests/test_audio_api.py` — `/audio` endpoints with `AudioCapture.start` monkeypatched: `/state` audio block, device list, persisted YAML write on `/audio/select` (and the previous YAML survives a failed device switch)

---

## Known gotchas to remember (from the design docs)

These are project-level reminders to apply when relevant; not everything is wired up yet:
- WS2815 needs **GRB** colour order in WLED. RGB will swap red/green.
- With centre-feed there are **four chain heads** (one per Gledopto output, all at the 15 m mark). The `BI` pad on each of those four first-pixels must be tied to GND, not floating.
- INMP441 in a sealed IP65 box can't hear anything — needs an acoustic membrane or external mounting (Phase 5 / Phase 8).
- Roadmap §6 item 5 has a wrong figure (108 A theoretical) — should be 27 A for WS2815 at 12 V / ~15 mA per pixel.
- LedFx and our server can't both talk to the same WLED at once.

---

## Phase status

- [x] Phase 0 — scaffolding
- [x] Phase 1 — topology + transports + simulator
- [x] Phase 2 — effect engine (mixer, more effects, gamma)
- [x] Phase 3 — REST API
- [x] Phase 4 — spatial GUI / layout editor
- [x] Phase 5 — audio analysis (capture, RMS / band features, `/audio` UI, `audio_pulse` effect)
- [ ] Phase 6 — MCP server for LLM control
- [ ] Phase 7 — operator mobile UI
- [ ] Phase 8 — Pi cutover
- [ ] Phase 9 — on-site reliability
