# WZRD input, network audio and audio swell

The LED rig can run as part of a projection. WZRD (a projection tool on a Mac, another repo)
sends the wall's colours, level and master sliders over OSC/UDP; an audio server on that Mac
can send its audio features to this machine too. ledctl only receives: it imports, launches
and configures neither program. Two listeners take the packets (`audio/bridge.py` on UDP 9000,
`wzrd/listener.py` on UDP 9100), and one hook in the master output stage applies the wall to
whatever effect is playing. Effects, layers, the playlist and the chat are untouched.
Audio swell is ledctl's own feature on the same output stage: LED brightness follows loudness
(`/audio/level`), as strongly as the `audio_swell` master says.

## Where the code lives

- `src/ledctl/wzrd/` — the whole WZRD module. `listener.py` (parse, validate, store the latest
  packet), `state.py` (`WzrdSettings`, and `WzrdState`: all smoothing), `stage.py` (`WzrdStage`,
  the output hook: table bake, recolour, gains, status payload), `api.py` (`GET/PATCH /wzrd`),
  `__init__.py` (`mount_wzrd`, the only entry point).
- `src/ledctl/api/server.py`, `create_app` — the one block that mounts the module when
  `wzrd_input.enabled` is true. Deleting `src/ledctl/wzrd/`, that block, and the import in
  `tests/conftest.py` leaves ledctl working unchanged.
- `src/ledctl/surface/runtime.py`, `_apply_master_output` — where the hook runs
  (`Runtime.output_hook`, `Runtime.swell_gain`).
- `src/ledctl/engine.py`, `_update_swell` — the swell gain, computed once per tick and written to
  `Runtime.swell_gain`. `quiet` / `loud` / time constants are the `audio_swell:` config block.
- `src/ledctl/osc.py` — shared by both listeners: the allowlist server and `LinkStats`
  (packets per second, largest gap).
- `src/ledctl/audio/bridge.py` — audio listener; `AudioBridge.from_config` picks local or
  network mode.
- `src/web/lib/wzrd.js` — the operator panel, mounted by both front-ends (desktop modal,
  mobile sheet). One binding table, so the two pages cannot drift.
- `scripts/fake_wzrd.py` — sends both WZRD messages at 60 Hz. The development target, and the
  stand-in for WZRD when checking a Pi.
- Operator settings persist in `config/wzrd_input.yaml` (gitignored runtime state, like
  `playlist.yaml`), never in the main YAML.

## Contracts with the other programs

**C-LED version 2** (owned by the WZRD repo's `docs/reference/render-engine.md`, section "LED link"; a change is never made
here first). Two datagrams per rendered WZRD frame, to UDP 9100, IPv4:

- `/wzrd/colors`: `level`, `white`, then `k` x `(r, g, b, weight)`, one per hue bin in hue order
  (k = 12 today; derive it from the argument count). `level` is unsmoothed and measured
  **before** WZRD's masters. Weights sum to 1, or are all 0 on a wall with no coloured light.
  Bin colours are sRGB-encoded with the largest channel at 1; an empty bin sends its centre hue.
- `/wzrd/masters`: `brightness` 0..2 (BLACKOUT = 0, FLASH raises it), `saturation` 0..2.
- No order, pairing or exact rate is guaranteed. Both are the heartbeat: a black wall still sends.

**Audio** (`/audio/*`, owned by the audio server repo): `/audio/lmh`, `/audio/bpm` and
`/audio/level` once per audio block (about 187 a second), `/audio/onset/low` as a single
packet per onset with no repeat. `/audio/level` is a fixed scale (0 = -60 dBFS, 1 = 0 dBFS).

## Why it's built this way

- **The order of gains in the output stage is the design.** Master brightness above 1 uses an
  envelope that pushes the recent peak toward full output, so any gain applied before it is
  cancelled within seconds. The hook's gain and the swell gain therefore multiply after the
  brightness step; the recolour runs first, on the effect's raw output.
- **`spread` defaults to 1.** With the effect's own hue alone choosing the wall colour, an effect
  that uses one or two hues shows one wall colour and can miss the main one. At 1 the wall's
  palette lies along the strips in proportion to each colour's share, whatever effect runs.
  Rejected: spreading the effect's hue histogram over the table (hard-edged colour blocks);
  mapping brightness through the table (a full-brightness wash collapses to one colour).
- **Colours are smoothed premultiplied by weight**, and `white` weighted by level. Smoothing
  colours directly lets an empty bin's centre hue, or a faint bin's noise, bleed into the table.
- **WZRD's masters are not smoothed**, so its BLACKOUT and FLASH land on the next LED frame.
- **Signal lost means hold.** If WZRD restarts mid-show the LEDs keep their look. The operator
  releases a stuck state by switching `listen` off.
- **Only onset packets wake the render loop.** Waking on every band packet drove the loop to
  93 fps at a 60 fps target and made frame intervals follow WiFi bursts.
- **The frame clock schedules each frame from when the previous one was due**, not from when it
  finished; otherwise render and send time stretch every interval (53 fps at a 60 target).
- **Audio swell needs `/audio/level`**, because every band value is auto-scaled upstream and
  says nothing about loudness. `audio_swell` is a master (it is ledctl's own room control); the
  WZRD settings are not.
- **Dead audio must not black out the room**, so the swell fades out over a second while the
  feed is stale. This is keyed on staleness (`connected`), never on the value: a stalled server
  sends one last 0, and real silence is also 0 and must dim. Between that last 0 and the
  watchdog's timeout the lights dip partway and then recover; that dip is accepted rather than
  special-cased.
- **Environment overrides are read at use time** (`AudioServerConfig.resolved_source`,
  `resolved_allow_from`) and never stored on the config model. This repo is public, and the
  config writer dumps the whole model: a stored override would copy a real address from `.env`
  into tracked YAML.

## Invariants and traps

- With no hook, or a hook whose `listen` is off, the output stage gives exactly the output it
  gave before the module existed. Pinned by
  `tests/test_wzrd.py::test_output_stage_recolours_and_follows_wzrd_masters`.
- A malformed packet (wrong count, NaN, inf, non-number) is dropped and counted, and the last
  good packet stays. Pinned by `test_cled_packets_are_parsed_or_dropped`.
- The config writer keeps every block. A hand-kept list once left out `auth`, and the rewritten
  YAML served the operator UI with no password. Pinned by
  `tests/test_api.py::test_config_write_back_keeps_the_auth_block`.
- **UDP 9000 on a dev Mac.** WZRD's engine binds it too, and `config/config.dev.yaml` both binds
  it and spawns an audio server; the second program to start loses its audio with no error.
  `tests/conftest.py` gives every test app ephemeral ports and no spawn. To run ledctl beside
  WZRD, follow "Developing on one Mac" below.
- A listener handler stores one packet by a single reference assignment and does no maths. Keep
  it that way: the render thread reads those tuples without a lock.
- `status.gain` is the gain actually applied, so it reads 1.0 while `listen` is off;
  `status.level` is the raw wall level, the number `level_full` should be set from.
- The recolour cost is unmeasured on a Pi 4 (about 0.1 ms per leg on an M2).

## Developing on one Mac

Never run the dev config while WZRD or its audio server may be up (see the UDP 9000 trap), and
never edit the audio repo's `configs/main.yaml`: a running server rewrites it from memory. Run
ledctl from a scratch copy that listens for audio on 9001 and spawns nothing. It must sit in
`config/` (effects and the playlist load from the folder next to it) and is gitignored:

```bash
uv run python - <<'PY'
import pathlib, yaml
cfg = yaml.safe_load(pathlib.Path("config/config.dev.yaml").read_text())
cfg["audio_server"].update(autostart=False, osc_listen_port=9001)
pathlib.Path("config/config.wzrd-check.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
PY
uv run ledctl run --config config/config.wzrd-check.yaml
```

Colours come from `scripts/fake_wzrd.py` (port 9100, clashes with nothing), or from the real
WZRD with its LED link target set to `127.0.0.1:9100`. Audio comes from a throwaway `python-osc`
sender to `127.0.0.1:9001` (`/audio/lmh`, `/audio/bpm`, `/audio/level` 187 times a second in
that order, an `/audio/onset/low` now and then), or from the real audio server started from a
copy of its config with that destination.

## Flash timing

Two paths make the LEDs flash, and they land at different times against the wall.

- **Onset packets** (beat-reactive effects). From the packet reaching the listener to the frame
  being handed to the transport: median 6 ms, 95th percentile 23 ms, on the Mac at 60 fps. Not
  measured: WiFi transit, the Pi's slower render, and WLED writing the strips (about 13.5 ms for
  450 pixels). A fair estimate is 20 to 40 ms from audio server to light. The wall goes through
  WZRD's frame, the GPU queue and the projector's lag, so these flashes usually **lead the wall**
  by a few tens of milliseconds. If that shows at rehearsal, the fix is an event delay on the
  onset (not built).
- **The wall's `level`** with `level_attack_s = 0`. WZRD measures it after its own render, so
  this flash lands about **with the wall**.

## Security

Both listeners accept unauthenticated UDP once a host is configured for a gig: 9000 (audio, in
`network` mode only) and 9100 (WZRD).

- **Who can reach them:** devices on the same network as the host and devices on its tailnet.
  A Tailscale Funnel carries HTTPS only, no UDP.
- **The worst a forged packet does:** flash, dim, black out or recolour the LEDs for as long as
  it keeps arriving, at up to the frame rate. A strobe in a dark room is the real harm. It
  cannot change effects, masters, settings or config, cannot reach the HTTP API, and does not
  drive the render rate. Every value is validated and clamped.
- **Mitigations:** `listen` is off by default; in `local` mode the audio listener stays on
  loopback; the allowlist (`audio_server.allow_from`, or `LEDCTL_OSC_ALLOW_FROM` so real
  addresses stay in `.env`) drops other senders before dispatch. It applies only to
  non-loopback listeners, and empty accepts any sender. A phone hotspot hands out a new address
  range each session, so a fixed list is brittle there.

## Runbook

- **Switch a host to network audio:** `LEDCTL_AUDIO_SOURCE=network` in its `.env`. The listener
  binds every interface and nothing is spawned. Remove the line to go back.
- **Modal shows "no signal":** WZRD's LED link is off, its target is wrong, or UDP 9100 is
  blocked. Run `scripts/fake_wzrd.py --host <this machine>` from the sending machine to split
  network from WZRD.
- **"listener down" in the modal:** UDP 9100 could not be bound (a second ledctl is running).
- **Flashes arrive late or unevenly:** read `max_gap_ms` in the modal (WZRD) and in
  `/audio/state` (audio). A 40 ms gap delays a flash by 40 ms; WiFi power saving on the
  receiver is the usual cause.
- **Setting up audio swell:** read `level` beside the swell slider at soundcheck (0.33 is
  -40 dBFS, 0.80 is -12 dBFS) and put the quiet and loud readings in the `audio_swell:` block.
- **Beat effects never fire:** `/audio/state` `beat_count` must climb. Onsets are
  `/audio/onset/low` (current server) or `/audio/beat` (older installs).
