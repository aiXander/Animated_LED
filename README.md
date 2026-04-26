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
│   └── config.pi.yaml       # on-site defaults (transport.mode = ddp, host 10.0.0.2)
├── src/
│   ├── ledctl/
│   │   ├── config.py        # pydantic schema + load_config()
│   │   ├── topology.py      # per-LED (strip_id, local_index, global_index, x,y,z)
│   │   ├── pixelbuffer.py   # float32 working buffer, uint8 at the boundary
│   │   ├── effects/         # Effect ABC + wave (Phase 1 only)
│   │   ├── transports/      # base / ddp / simulator / multi
│   │   ├── engine.py        # fixed-timestep async render loop
│   │   ├── api/server.py    # FastAPI: /state, /topology, /ws/frames
│   │   └── cli.py           # `ledctl run` / `ledctl show-config`
│   └── web/index.html       # Canvas2D simulator viewer
└── tests/
```

---

## Architecture in one paragraph

`Engine` ticks at `target_fps` using `time.perf_counter`. Each tick: clear the `PixelBuffer` (float32 RGB ∈ [0,1]) → call the active `Effect.render(t, out)` writing into normalised spatial coords → convert to uint8 → hand to the `Transport`. Transports are pluggable (`SimulatorTransport` broadcasts to all WS clients, `DDPTransport` chunks to UDP packets with PUSH on the last only, `MultiTransport` fans out). Effects are deliberately blind to LED count and strip layout — they only see `topology.normalised_positions` (each axis in [-1, 1]), so "left → right" is unambiguous regardless of how strips are split or reversed.

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

## Choices made (Phase 0/1) worth re-examining later

1. **Row separation = 1 m** in the dev/pi configs (`y = ±0.5`). Placeholder — replace with the measured value once the scaffolding is up.
2. **DDP destination id = 1** (WLED's default primary output). Configurable on `DDPTransport(..., dest_id=...)` if a multi-segment WLED setup needs a different id.
3. **480 px/packet** in DDP. 1440 byte payload + 10 byte header = 1450 < 1500 MTU. Don't raise without checking the path MTU.
4. **PUSH flag only on the final packet** of each frame. Per the DDP spec — getting this wrong means WLED holds the previous frame.
5. **PixelBuffer is float32 internally**, converted to uint8 at the transport. Keeps mixer/blend/gamma math clean for Phase 2.
6. **No gamma correction yet.** Roadmap puts it in Phase 2; pick *one* place (engine OR WLED) — not both.
7. **WS frame format is raw packed RGB bytes** (`N×3`). Browser fetches `/topology` once for positions. No per-frame metadata.
8. **`asyncio.wait_for` on a stop-event** is how the engine paces sleep, so `engine.stop()` returns promptly without waiting for the next tick.
9. **Engine drops frames rather than spiralling** if it falls behind (`engine.dropped_frames` is exposed via `/state`).
10. **No `git init`** yet. Repo is plain files; add when the user wants commits.
11. **No tests for `MultiTransport`** because it instantiates a real DDP socket at app boot — needs a UDP listener fixture. Add when we exercise that path.

---

## Test surface (Phase 0/1)

- `tests/test_config.py` — dev/pi YAML loads, overlap/over-capacity rejection
- `tests/test_topology.py` — 1800 LEDs total, bbox spans 30×1 m, normalised in [-1,1], `reversed` semantics
- `tests/test_ddp.py` — packet count, PUSH-only-on-last, payload round-trip, single-packet frame still has PUSH
- `tests/test_wave_effect.py` — output bounded in [0,1], wave actually travels over time

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
- [ ] Phase 2 — effect engine (mixer, more effects, gamma)
- [ ] Phase 3 — REST API
- [ ] Phase 4 — spatial GUI / layout editor
- [ ] Phase 5 — audio analysis
- [ ] Phase 6 — MCP server for LLM control
- [ ] Phase 7 — operator mobile UI
- [ ] Phase 8 — Pi cutover
- [ ] Phase 9 — on-site reliability
