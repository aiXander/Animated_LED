# Implementation Roadmap — Audio-Reactive LED Installation

> Companion to `Audio-Reactive LED installation.md` (physical/electrical) and `Hardware_setup.md` (gear-list and on-site wiring). This doc tracks the **software** build, sequenced from a laptop test-bench to the on-site Raspberry Pi.

**Status:** Phases 0–6 are complete. Phase 8.1's digital prep landed early (see below). Phase 7 (mobile UI), the rest of Phase 8 (Pi/INMP441 hardware bring-up), and Phase 9 (reliability) are next.

---

## Guiding principles (still in force)

1. **Hardware-agnostic core.** All effects render into a `PixelBuffer`; a `Transport` ships frames to either a real WLED/Gledopto over DDP or a browser simulator. Mac ↔ Pi ↔ real hardware is a config change, not a code change.
2. **Single source of spatial truth.** `config.yaml` describes controllers, strips, and geometry. Effects use normalised coords (`x, y, z ∈ [-1, 1]`) — no hard-coded indices.
3. **Lean on what exists.** WLED runs on the Gledopto and speaks DDP. We wrap it with a control layer + spatial model + LLM-friendly API.
4. **Vectorised numpy only.** A Pi 4 pushes 60 FPS to 1800 LEDs comfortably as long as no per-pixel Python loops sneak in.
5. **Every layer testable on a mac without LEDs plugged in.** The simulator is the default transport during dev.

**Coordinate convention:** right-handed, `+x` = stage-right, `+y` = up, `+z` = toward audience. Origin = centre of scaffolding (15 m mark, midway between rows). All effect math in normalised `[-1, 1]`.

---

## What's already built (Phases 0–6)

The full architecture and module-level contracts live in `CLAUDE.md`. Brief recap:

- **Phase 0–1 — Scaffolding, topology, transports, simulator.** `uv` + `ruff` + `pytest`; `ledctl run --config …` entrypoint. `config.yaml` + pydantic schemas with duplicate-strip / overlap / over-capacity validation. `Topology` gives every LED a stable `(strip_id, local_index, global_index, x, y, z)`. `PixelBuffer` is a float32 `(N, 3)` array in `[0, 1]` with a single gamma stage. `DDPTransport` (480 px/packet, PUSH only on the final packet), `SimTransport` (WebSocket binary frames), `MultiTransport` (both at once). Engine is a fixed-timestep async loop at `target_fps`; frames drop rather than spiral on lag.
- **Phase 2 — Surface engine.** Replaced the originally-planned "named effects" library with a polymorphic primitive surface (`surface.py`). A layer is a tree of `{kind, params}` nodes resolving to one of four output kinds: `scalar_field`, `scalar_t`, `palette`, `rgb_field`. Combinators (`mix`, `mul`, `add`, `screen`, `max`, `min`, `remap`, `threshold`, `clamp`, `range_map`, `trail`) broadcast across kinds at compile time. Scalar/palette sugar (bare numbers → `constant`, palette name strings → `palette_named`). `Mixer` stacks layers with blend modes (`normal/add/screen/multiply`) + opacity, and crossfades between stacks using `wall_t` so operator transitions aren't slowed by `speed=0.5` or stopped by `freeze=true`. `MasterControls` (`brightness`, `speed`, `audio_reactivity`, `saturation`, `freeze`) are deliberately operator-only — never in the DSL.
- **Phase 3 — REST API.** FastAPI app at `/`, OpenAPI at `/docs`. Endpoints: `/state`, `/topology`, `/config` (PUT for layout edits), `/surface/primitives`, `/layers` (POST/PATCH/DELETE), `/masters` (GET/PATCH), `/presets/{name}` (POST), `/blackout` + `/resume`, `/calibration/*`, `/audio/*`, `/ws/frames`. Four seed presets ship: `default`, `chill`, `peak`, `cooldown`.
- **Phase 4 — Browser simulator + layout editor.** Top-down 2D canvas at `/`: live LED viz, hover for `global_index/strip_id/local_index`, click to solo-light for calibration. Editor view drags strip endpoints, edits length/density/orientation, writes back to `config.yaml`. Calibration helper script walks LEDs at fixed indices in red.
- **Phase 5 — Audio analysis.** `sounddevice` source → `AudioAnalyser` (FFT ring buffer, RMS / peak / band features) → `AudioState` (raw, instantaneous, single-writer/many-readers, no locks) → `RollingNormalizer` per-feature auto-gain. Bindings consume `*_norm`, so reactivity auto-scales to room loudness. Modulation lives directly on the parameter: `palette_lookup.brightness = envelope(audio_band(band="low"), attack_ms=10, release_ms=120)` — no separate `bindings` block.
- **Phase 6 — Language-driven control panel.** Thin layer over OpenRouter (`openai` SDK pointed at `https://openrouter.ai/api/v1`), NOT a multi-tool agent loop. **One tool, `update_leds(layers, crossfade_seconds, blackout)`**, whose argument is the *complete* new layer stack — never a diff. Calls `Engine.crossfade_to`, same code path as `POST /presets/{name}`. System prompt regenerated **fresh every turn** with: install summary, current layer-stack JSON, audio snapshot, read-only master values, full primitive catalogue from `surface.generate_docs()`, anchor examples, anti-patterns. Rolling buffer of last `N` messages (default 20, heals dangling `tool` messages after trim). Per-session rolling-window rate limit. Sessions wipe on restart (v1). On compile/validation failure the tool result carries a structured `{path, msg, valid_kinds}` error which the LLM sees on its next turn and self-corrects. `OPENROUTER_API_KEY` read from `.env`; missing key → 503 on `/agent/chat`, render loop unaffected.

What this leaves us with going into Phase 7: a complete dev-grade controller that runs on a mac, drives the simulator at 60 FPS, accepts REST + chat input, and is one config flag (`transport.mode: simulator → multi → ddp`) away from talking to real hardware.

---

## Phase 7 — Operator mobile UI (1–2 days)

**Goal:** a phone-friendly page bar staff can pull up to change vibe quickly.

- Single page, big buttons:
  - All master sliders (brightness, speed, audio_reactivity, saturation, freeze).
  - Chat window (re-uses `/agent/chat` from Phase 6).
  - A row of preset tiles (`default`, `chill`, `peak`, `cooldown` + any added during the build).
  - Blackout / resume.
- **Auth:** simple shared password "kaailed" (or whatever the event rotates to). **Landed early as part of the Phase 8 prep** — see `src/ledctl/api/auth.py`. Activated by setting `auth.password` in `config.pi.yaml`; off by default in `config.dev.yaml`. Cookie-based with a `?password=…` shortcut for first visit, gates HTTP via Starlette middleware and rejects WS upgrades pre-accept (close 4401). Still meant to live behind Tailscale — never expose the Pi directly.
- **PWA manifest** so it installs as an app icon on the bar staff's home screens.
- Touch-friendly: 44px minimum hit targets, no hover-only affordances, single-column layout, no horizontal scroll.

**Test on mac:** open the page from a phone over Tailscale, drive the simulator from the phone while the laptop displays the LED viz.

---

## Phase 8 — Move to the Pi + on-site physical install (1 day SW + a build day)

**Goal:** identical software behaviour on the Pi, with INMP441 audio and Ethernet to the Gledopto, and the physical install wired up to match `config.pi.yaml`.

This phase is half software (provisioning + config) and half hardware (mounting, wiring, power injection, waterproofing). Both halves need to be sequenced carefully — most of what can go wrong here is physical, not code.

### 8.1 Software cutover

The digital pieces below are already in place — the Pi just needs to be flashed, joined to Tailscale, and pointed at `config.pi.yaml`.

**Already landed (pre-build day):**

- **`config/config.pi.yaml`** — `transport.mode: ddp`, `controllers.gledopto_main.host: 10.0.0.2`, `server.host: 0.0.0.0` so the venue WiFi can reach it, `auth.password: kaailed` to keep randoms out, full `masters:` block for parity with dev. `audio.device: null` is the only field the build day still has to fill in (the INMP441 ALSA card name from `arecord -l`; `/audio/select` persists it).
- **Auth gate** (`src/ledctl/api/auth.py`) — Starlette middleware + `/login` (form post or `?password=…`) + `ledctl_auth` cookie + WS upgrade gate (close 4401). `/healthz`, `/login`, `/logout` are always public so a future watchdog can probe past the gate. Off automatically in `config.dev.yaml` (no `auth.password`).
- **`/healthz`** endpoint — JSON `{ok, fps}`, public, suitable for a systemd watchdog ping.
- **systemd unit** (`deploy/ledctl.service`) — `After=network-online.target sound.target`, `Restart=always`, `RestartSec=2`, `Nice=-5`, `audio` supplementary group for ALSA, `ProtectSystem=full` + `ProtectHome=read-only` so it plays well with overlayfs, journald logging only. Install: `sudo cp deploy/ledctl.service /etc/systemd/system/ && sudo systemctl enable --now ledctl`.
- **`.env.example`** — template the Pi flow copies to `.env` (gitignored) for the OpenRouter key. `cli.py` already auto-loads it.
- **Auth + healthz tests** (`tests/test_auth.py`, 11 cases) — cookie/query/POST/WS paths and confirmation that the dev config remains open.

**Still to do on build day:**

- **Pi networking:** static IP on `eth0 = 10.0.0.1/24`, no gateway on that interface (it's a private link to the Gledopto). Pi keeps its WiFi for venue access. WLED on the Gledopto is pre-set to static `10.0.0.2/24` on its Ethernet interface.
- **WLED LED Preferences (per-Gledopto setup):**
  - Color order: **GRB** (WS2815 default — RGB swaps red/green and is the #1 "wrong colors" support thread). Confirm with a `solid(red)` from the engine.
  - Ethernet Type: `Gledopto Series with Ethernet` (or generic LAN8720 with correct clock pins) so the RJ45 port wakes up.
  - Map the four GPIO outputs to four 450-LED segments matching the topology IDs in the config.
  - WLED's own gamma off (the renderer is the single source — `output.gamma: 1.5`). If you must turn WLED gamma on, set `output.gamma: 1.0` in YAML to avoid double-gamma.
- **I²S setup** (`/boot/firmware/config.txt`): `dtparam=i2s=on` plus the right overlay for INMP441 (e.g. `dtoverlay=googlevoicehat-soundcard` or a custom INMP441 overlay). Verify with `arecord -l` and a 3-second `arecord` capture **before** plugging into our pipeline. Then pick the device at `/audio` and click "apply & save" — `audio.device` gets persisted into `config.pi.yaml`.
- **Read-only rootfs (overlayfs)** as in the hardware doc — flip on **after** everything works, not before. Persist presets and the active `config.yaml` on a separate writable partition (e.g. `/var/lib/ledctl`).
- **Tailscale** on the Pi → stable hostname for the operator UI from any phone, no port-forwarding.
- **mDNS** (`avahi`) so the Pi answers to `ledctl.local` on the venue WiFi.

**Test on Pi:** repeat all Phase 1–7 tests with `transport.mode: multi` so frames go to **both** the real LEDs and the laptop's browser simulator over Tailscale — invaluable for on-site debugging. Confirm the auth gate by hitting `http://ledctl.local:8000/?password=kaailed` from a phone once; subsequent loads skip the query string thanks to the cookie.

### 8.2 Physical install — wiring & mounting

The hardware doc covers the gear-list, power math, and waterproofing in full; this section is the install order, calling out the bits where software and hardware have to agree.

**Topology to keep in mind while wiring:** 1800 WS2815 IP67 LEDs on two parallel 30 m rows (six 5 m strips per row). The Gledopto sits at the **15 m centre** of the scaffolding and feeds **four chain heads** — one per row-half (top-left, top-right, bottom-left, bottom-right), each 450 LEDs. The Pi is in a separate IP65 box ~10 m away under the DJ booth, connected to the Gledopto with a single Ethernet patch cable (Option 2 from the hardware doc — eliminates the data-run danger zone entirely).

**Step-by-step:**

1. **Mount the LED channels.** Aluminium channels with milky diffusers, zip-tied to the scaffolding with black UV-resistant ties. Two parallel 30 m runs, one above the other. Channels self-ground against the metal scaffolding.
2. **Pre-label every strip.** Print stickers showing each strip's `global_index` range from the topology (e.g. `top_left_a: 0–149`, `top_left_b: 150–299`, …). Label both endpoints. Future-you on a ladder at midnight will be grateful.
3. **Mind the strip direction at the centre.** Two of the six strips per row are flipped relative to "natural" left-to-right reading: the strips running from the 15 m mark outward toward 0 m must have their data-arrow pointing **away** from the centre. Same on the 15 m → 30 m side. Plan and label this **before** mounting — re-mounting silicone-sealed strips is miserable. The `reversed: true` flag on the relevant strips in `config.yaml` must match this physical reality, and the calibration helper (Phase 4) is what verifies it.
4. **Run the 12 AWG power bus.** Centre-fed at the 15 m mark, **+12 V and GND as a twisted pair** to reduce noise coupling into data. PSU 1 powers one half (e.g. left of centre), PSU 2 powers the other. Both PSU negative terminals must be tied together with a heavy ground wire — without a common ground reference, the data signal between rows behaves unpredictably and ground loops cause flickering. **PSU V+ outputs must never touch each other.**
5. **Inject power at every junction.** Tap into the 12 AWG bus at the 5 m, 15 m, and 25 m marks on each row, feeding 12 V + GND into the two neighbouring 5 m strips at each tap. Each 18 AWG injection line gets its own 5–7.5 A blade fuse on an automotive fuse-bus. A larger (~25–30 A) fuse on each PSU output protects against a bus short upstream of the branch fuses. During burn-in, watch the LEDs at 0 m and 30 m for visible drop — those endpoints sit a full 5 m of trace away from the nearest tap; add 0 m / 30 m taps if needed.
6. **Power the Gledopto from one PSU's V+** (small 18 AWG pair to the Gledopto's `V+` / `GND` input terminals). The Gledopto's `GND` output terminal connects to the common ground bus. **Do not connect the Gledopto's `V+` output terminals to the LED strips at all** — all LED power comes straight from the external fuse blocks.
7. **Run data lines from Gledopto to chain heads.** Four data outputs, each going to one of the four chain heads at the 15 m centre. Use the shielded 4-conductor alarm cable; inside it, twist Data + GND as one pair and Backup Data + GND as the other. **On every chain head's first pixel, tie BI (Backup In) to Ground** — if BI is left floating at the start of a chain it picks up interference and causes flickering. After the first pixel of each chain, BI just connects to the previous pixel's DI as normal.
8. **Connect Pi → Gledopto via Ethernet.** ~10 m solid Ethernet patch cable from the Pi's IP65 box under the DJ booth straight to the Gledopto's RJ45 port on the scaffolding. Static `10.0.0.1` ↔ `10.0.0.2`. This is the data-integrity win that lets the Gledopto sit right next to the strips: Ethernet is balanced, differential, and noise-immune over far longer than 10 m.
9. **Waterproof every cut and joint.** Pull back the silicone sleeve ~1 inch at every junction, solder pad-to-pad (or short bridging wires), solder the power injection leads to those same joints, inject neutral-cure silicone into the sleeve opening, slide marine-grade adhesive heat-shrink over the whole joint, hit with the heat gun. Drip loops on every cable entering an enclosure. Mean Well HLG-320H-12 PSUs mounted **terminals-down** under the scaffold so water doesn't pool on the housing.
10. **Acoustic port for the INMP441.** Sealed in an IP65 box the mic can't hear anything. Either drill a small hole and cover it with a Gore-Tex / waterproof acoustic membrane, or mount the mic on the **outside** with a short cable through a sealed gland (wire run from mic to Pi GPIO must stay <15 cm).
11. **Power the system off the long mains line.** 40 m H07RN-F 14 AWG run to the two PSUs from a single AC source so the team can kill power at night to drop the WS2815 idle draw.

### 8.3 Bring-up order on-site

Doing these in order avoids a bricked LED line on demo day.

1. **No-LED Pi boot.** Pi up on Tailscale, `ledctl.service` running with `transport.mode: simulator`. Phone hits the operator UI. Confirm chat works (OpenRouter reachable from venue WiFi).
2. **Gledopto-only test, no LEDs connected.** Pi → Gledopto Ethernet link up, ping `10.0.0.2`, WLED dashboard reachable from the Pi. Switch `transport.mode: ddp`, send a `solid(red)` — verify with WLED's own preview that DDP packets are arriving.
3. **One strip live.** Connect the first 5 m strip on `top_left_a` only. Run the calibration script — first pixel should be bright red, no flicker. If you see green instead of red, fix WLED's color order to GRB (don't fix it in software). If you see flicker, BI is floating somewhere on that chain head.
4. **Whole top row.** Add the rest of `top_left_*` and `top_right_*`. Run a horizontal-wave preset; verify both centre-out directions are correct. If a half runs backwards, flip `reversed:` on the affected strips in `config.pi.yaml`, not in the wiring.
5. **Both rows + power injection burn-in.** All 1800 LEDs, full white, 5-minute hold. Watch the 0 m and 30 m endpoints for voltage drop. Watch each PSU's enclosure for warmth (rain-resistant, not heat-spec'd for sustained 27 A). Watch the Pi CPU temp (`vcgencmd measure_temp`) — sustained >75 °C means add the heatsink before the read-only flip.
6. **Audio in the loop.** Point a speaker at the mic, run a peak-hour preset that maps `audio_band(band="low")` to brightness, watch reactivity. Adjust `RollingNormalizer` window if it auto-scales too slowly for the venue.
7. **Power-cycle test.** Yank mains 5 times in a row. Pi must come back up cleanly each time (this is what the read-only rootfs is for). Flip overlayfs on **only after** this passes.
8. **Image the SD card** the night before the event. Keep a spare card pre-flashed in the gig bag.

---

## Phase 9 — On-site reliability (½ day)

- **Watchdog:** if frames-sent FPS drops below 10 for >2 s, log + try to reconnect the DDP socket; if WLED is unreachable for >30 s, fall back to running its built-in effect via the JSON API (`python-wled`) so the LEDs aren't dead.
- **Brownout behaviour:** on startup, wait until WLED responds before pushing frames (`python-wled` health check). Render loop should never block on a missing transport.
- **Power-cycle resilience:** read-only FS (8.2 above), `systemd` auto-start, no SD writes in our hot path.
- **Heat:** Pi 4 in a sealed IP65 box doing audio FFTs. Log CPU temp to journald; if it sustains >75 °C, add the heatsink the hardware doc mentions (or a small thermal pad to the box wall).
- **Time:** install `chrony` for NTP — log timestamps need to be useful post-event for blame-the-bug investigations.
- **Backups:** SD card image plus a printed copy of `config.pi.yaml` and the global-index sticker layout. If the Pi dies, a spare boots into the same install.
- **Power-budget clamp** (worth adding to the engine, not just hoping no-one writes a "all white" preset by mistake): per-frame estimate `Σ channel × calibration_factor`, scale the whole frame down if it would exceed the PSU budget. Cheap to add, prevents PSU foldback during a "all white at 100 %" mistake.

---

## Cross-phase open questions (keep on the radar)

1. **System-prompt size.** Catalogue + current state + audio + examples is ~1.5–3 k tokens. Comfortable, but every new primitive added to `surface.py` widens the prompt — keep `Params` `description=` strings to one line.
2. **Audio-snapshot staleness.** `AudioState` is sampled at `/agent/chat` request time; if the model takes 2 s to respond, the reading is 2 s old. The system prompt says "the room a moment ago", not "the room right now."
3. **Session persistence.** In-memory for v1. Promote to sqlite under `/var/lib/ledctl/sessions/` once we know how the operators actually use chat.
4. **Auth scope.** ~~Bind to `127.0.0.1` (or Tailscale-only) until the shared password lands in Phase 7.~~ Shared password landed early as part of Phase 8 prep (`auth.password` in YAML, `src/ledctl/api/auth.py`). Still recommended to keep the Pi behind Tailscale at the venue — the password is anti-randoms-on-LAN, not anti-determined-attacker. OpenRouter key blast radius remains spend; a leaked key = a bigger bill, not a hacked install.
5. **MCP path is deferred, not deleted.** The same `update_leds` schema can be re-exposed via FastMCP later for Claude Desktop — same shape, no rewrite.
6. **Multi-controller future.** `controllers:` is a dict, not a single entry. Adding a third row of LEDs is a config change, not a refactor.
7. **Two-PSU boot skew.** If the two PSUs ever boot at slightly different times, the data line momentarily sees a 12 V row next to a 0 V row. WLED handles this fine, but our effects shouldn't *assume* both halves are alive — render the full frame and let any dark half just be dark.
8. **Possible future improvement — biquad IIR filter bank** (already noted in `CLAUDE.md`): per-band band-pass filters for tighter transient response on mid/high bands. Park behind a config flag if reactivity feels sluggish on real DJ audio.
