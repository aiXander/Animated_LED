# MIDI control and master effects

A hardware controller (the Akai APC mini mk1) plays the LEDs: pads pick library effects,
faders ride the masters, buttons black out, flash and run the playlist. The controller is
plugged into the Mac, not the Pi. A *sender* on the Mac forwards its raw MIDI messages over one
WebSocket and lights the pads with what comes back; ledctl owns the mapping. The sender is WZRD
in its "LEDs" mode (so the same AKAI drives the wall in WZRD's other modes), or
`scripts/midi_relay.py` when WZRD is not running. The operator sees and edits the mapping on a
drawing of the controller in the UI ("🎛 AKAI" on desktop, "AKAI controller" in the phone menu).
Four master effects (sparkle, glitch, strobe, trails) exist for the faders to drive.

## Where the code lives

- `src/ledctl/midi/__init__.py` — `mount_midi` (the only entry point, called at the end of
  `create_app`), the routes `GET /midi`, `PUT /midi/bindings`, `POST /midi/reset`, and the
  `/ws/midi` WebSocket with its light loop.
- `src/ledctl/midi/surface.py` — `MidiSurface`: the target registry (what a control can do),
  the mapping and its file, message handling, soft takeover, the effect loader, the lights.
- `src/ledctl/midi/apc_mini.py` — the controller: every control's hardware id, label and place
  on the drawing, the light values, which controls WZRD keeps, and the preset mapping.
- `src/ledctl/surface/master_fx.py` — `MasterFx`, the four master effects and the flash; run
  from `Runtime._apply_master_output`. The amounts are ordinary masters (`masters.py::BOUNDS`).
- `src/web/lib/midi.js` — the drawing, tooltips and editor, mounted by both front-ends.
- `scripts/midi_relay.py` — a sender without WZRD (`uv run scripts/midi_relay.py`; it carries
  its own dependencies, ledctl itself has no MIDI library).
- The mapping persists in `config/midi.yaml`, written only once the operator changes a control.

## Contract with the sender (C-MIDI version 1)

Owned here; WZRD's forwarder follows it. A change is made in both repos or not at all.

- `ws://<ledctl>:8000/ws/midi`, behind the same password as the UI: the sender puts
  `Cookie: ledctl_auth=<password>` on the handshake (close code 4401 without it).
- Sender to ledctl: one binary frame per raw MIDI message, three bytes as received (note-on,
  note-off or note-on with velocity 0, control change; the channel is ignored). The text frame
  `sync` asks for every light again.
- ledctl to sender: binary frames `[0x90, note, value]`, one per light that changed; everything,
  zeros included, after connect and after `sync`. Pads: 0 off, 1 green (live), 2 green blink
  (loading), 3 red (would not load), 5 yellow (ready). Round buttons: 0 / 1.
- WZRD keeps the four mode buttons (notes 68-71), Shift (98) and fader 9 (CC 56) in every mode.
  They carry `reserved` in `apc_mini.py`: never bound, never lit, shown as "WZRD" on the drawing.
- The sender forwards faders as they come. Soft takeover happens here.

## Why it's built this way

- **Raw forwarding, mapping in ledctl.** Rejected: button presses over the existing UDP 9100
  link (a lost packet is a lost press, nothing comes back for the lights, and that port has no
  password, so anyone on the hotspot could switch effects); WZRD mapping LED actions itself
  (every tweak in two repos, and WZRD would need ledctl's state for lights and takeover); Web
  MIDI in the browser (needs HTTPS and an open tab, and fights WZRD for the lights).
- **Soft takeover is stateless.** A fader moves its target only when it is within `PICKUP` of
  the target's value, was there on its previous message, or crosses it. Nothing is "attached",
  so a value changed in the UI or by WZRD's own mode needs no bookkeeping.
- **The master effects run after the brightness step and before the wall and swell gains.**
  Before brightness, a spark at full would drag the adaptive-brightness peak to 1 and dim the
  effect under it; after the gains, sparks would ignore a dark wall. Sparks and the flash are
  scaled by the brightness master (capped at 1) so they dim with the room.
- **One effect loader, last press wins.** Pad presses during a compile replace the queued
  effect; loads never overlap. A pad press stops the playlist, which would otherwise replace the
  choice at its next step.
- **The preset is recomputed at boot until the first edit** (starred effects first, then by
  name), so a new library effect gets a pad. The first edit saves the whole mapping, which
  freezes it.

## Invariants and traps

- With every master effect at 0 the output stage is bit-for-bit what it was without them, and a
  blackout stays black whatever the amounts. Pinned by
  `tests/test_midi.py::test_master_effects_are_off_at_zero_and_never_light_a_blackout`.
- The wire contract and soft takeover are pinned by
  `test_controller_plays_the_leds_over_the_websocket`; the saved mapping and its validation by
  `test_mapping_edits_are_validated_and_survive_a_restart`.
- `MidiSurface.handle` runs on the event loop. It may set a master or start the loader task; it
  must never read a file or compile (see `render-performance.md`). The WZRD-tint fader writes
  `wzrd_input.yaml` one second after it rests, on a worker thread.
- A fader that "does nothing" after a mode switch is soft takeover working: move it to the
  green tick on the drawing.
- A virtual MIDI port named like the controller is heard by a running WZRD too. Test with
  another name (`--device`), or pad presses fire WZRD clips.
- `config/midi.yaml` is tracked in git once it exists, like `playlist.yaml`: an edit on the Mac
  reaches the Pi by commit and pull.

## Runbook

- **Pads dark, drawing says "no controller connected":** no sender. WZRD is not in its LEDs
  mode, its ledctl address or password is wrong, or the relay is not running.
- **Sender connects and drops at once:** close code 4401 = wrong or missing password cookie.
- **A pad turns red:** its effect would not load (deleted, renamed or failing to compile); the
  log has the reason. Rebind the pad in the drawing.
- **Start over:** "reset to preset" in the drawing (deletes `config/midi.yaml`).
