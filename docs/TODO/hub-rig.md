# Hub rig: legs outward from the Gledopto

> **Status: spec, nothing built; reviewed against the code on 2026-11-16.** First use: the gig
> of 21 Nov 2026 (indoor lie-down concert, live guitar, LEDs on the floor beside a projection;
> network and audio for that night are in [wzrd-input.md](wzrd-input.md)). Xander's brief
> (2026-11-16): *the Gledopto is the origin, a number of legs run outward from it, three or
> four must not matter, and effects are rewritten from the origin outward.* He settled D1, D7,
> D8, D9 and D10 the same day; do not reopen them. How WZRD's colours reach the legs is its own
> plan, [wall-light.md](wall-light.md). Decisions still his are under "Open, for Xander".
> This repo is public: no hostnames, addresses or passwords in this file.

## The model

A rig is a **hub** (the Gledopto, at the origin) and **K legs** (one strip each, data entering
at the hub). Every LED is addressed by **which leg** it is on and **how far out** it sits.
Effects think in those two numbers, so the same effect runs on three legs or four.

The code assumes the old rectangle (two 30 m rows) in these places: the bounding-box
normalisation in `topology.py`; the quadrant walk `_compute_u_loop` in `surface/frames.py`;
the hard-coded `PHYSICAL_RIG` text and the `y ∈ {+1, -1}` anti-pattern in `surface/prompt.py`;
the frame names in `_diagnostic_hint` (`surface/runtime.py`); the strip drawing and
`_approxRowCount` in `web/lib/viz.js`; the palette spread along `u_loop` in `wzrd/stage.py`;
and the effects. Measured on a 3-leg layout with today's code: the hub lands at `radius` 0.24
instead of 0, and `u_loop` jumps from tip to tip.

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **One rig model: hub and legs.** The rectangle stops being described in the prompt and examples. | One vocabulary keeps the prompt inside its size budget. The rectangle's strip config stays in git history. |
| D2 | **Positions are normalised about the origin with one uniform scale** (C2), not per axis about the bounding box. | Puts the hub at 0 for any K and keeps angles true. A 3-leg star has a lopsided bounding box. |
| D3 | **Old frame names stay computed and leave the prompt** (C3). | An effect not yet re-implemented must still compile; the LLM must stop reaching for them. |
| D4 | **The rig text in the prompt is generated from the topology**, never typed. | It is wrong already (it says 60 LEDs/m; the strips are 30) and K must not be baked in. |
| D5 | **No Gledopto change.** Dropping a leg is an unplugged output plus one removed strip entry. | WLED knows four outputs of 450 px. Geometry lives only in ledctl. |
| D6 | **A leg is one straight line.** A leg that bends is entered as straight. | Leg and distance survive a bend; only Cartesian effects and the simulator picture drift. |
| D7 | **Two library folders: `config/effects/double-line/` is a frozen copy of the 12 effects tuned for the rectangle; `config/effects/star/` is the live library, and every effect in it is re-implemented for the hub rig** (G4, G6). | Xander's call. The copy is the reference for each rewrite. |
| D8 | **WZRD's colour mapping has selectable modes; in the default every leg behaves the same.** | Xander's call. The design is [wall-light.md](wall-light.md). |
| D9 | **The new effects are tuned for the first night: slow, no strobe, low base brightness.** | A lie-down concert beside a projection, LEDs at eye level. |
| D10 | **The effect library is tracked in git and is the same on every device.** Saved knob values are the shared defaults. The Pi's old Pi-only library is set aside, not merged. | Xander's call. A device gets effects by `git pull` and shares its own by commit and push. |

## Before anything is built

**`config/effects/` is not in git yet.** `.gitignore` no longer excludes it, but the folder is
untracked (`git ls-files config/effects` prints nothing) and the working tree holds about
thirty other uncommitted files. Until Xander commits the library, `git mv` cannot move it,
"the library before the split" has no reference to compare against, and no other device can
receive anything. G4 step 1 waits for that commit; everything else can start.

## Working with the tracked library

Everything done in the UI on any device (knob tuning, an LLM save, rename, delete, star)
changes files under `config/effects/` and shows in that device's `git status`.

- **Those changes are Xander's tuning.** Never revert them to tidy the tree. Before a step that
  moves or rewrites library files, look at `git status` for uncommitted library changes and
  ask him whether to commit them first.
- **Move library files with `git mv`,** with ledctl stopped.
- **A pull is refused** when a library file was changed on both sides. Show Xander that file's
  `git diff`; he picks a side.
- **Knob values reach the file up to 10 s after the drag** and at shutdown. Stop ledctl before
  committing a tuning session.
- **A re-implementation overwrites the saved values** of its effect for every device. Carry a
  tuned value over where the param `key` keeps its meaning.

## Contracts

Build against these; a package that must change one edits it here in the same change.

**C1: strip geometry.** A second geometry type next to `line` in `config.py`:

```yaml
geometry: {type: spoke, angle_deg: 90}      # optional: length_m, offset_m
```

`angle_deg` is measured in the x-y plane, 0 = +x, counter-clockwise. `length_m` defaults to
`(pixel_count - 1) / leds_per_meter`; `offset_m` (the first LED's distance from the hub)
defaults to 0. The default length needs the strip's own `pixel_count`, which a geometry model
cannot see, so **the endpoints are resolved on `StripConfig`** (`strip.start`, `strip.end`,
valid for both types), and the four readers of `strip.geometry.start/.end` switch to them:
`Topology.from_config`, `_compute_u_loop`, `_summarise_install` (replaced by C5) and the
`/topology` payload in `api/server.py`, which keeps sending `start` and `end` because
`viz.js` reads them. Trap: `_strips_to_yaml_dicts` in `api/server.py` writes `type` + `start`
+ `end` by hand; it must dump the geometry model as it is, or the file `PUT /config` saves
turns a spoke into a line that fails validation.

**Axes.** Origin = the Gledopto. x-y = the plane the legs lie in, drawn by the simulator (a
floor rig is seen from above; point +y at the projection wall). z = height off that plane.

**C2: normalisation.** `normalised = positions / R`, with `R` the largest distance of any LED
from the origin. `bbox_min` / `bbox_max` stay in metres. Checked: this reaches effects only
through the frames; no effect in the library, the seed or the prompt examples reads `ctx.pos`,
and the other position readers (`RigInfo` spans, `/topology`, the simulator) use metres.

**C3: frames** (`surface/frames.py`).

| Frame | Definition | In the prompt |
|---|---|---|
| `radius` | distance from the hub: 0 at the hub, 1 at the farthest LED (formula unchanged; C2 makes it true) | yes, the headline frame |
| `chain_index` | fraction along the LED's own leg, 0 at the hub end, 1 at its tip (unchanged). Equals `radius` when all legs are the same length | yes |
| `leg` | int32 `0..K-1`, legs ranked by ascending direction | yes |
| `leg_phase` | float32 `leg / K` | yes |
| `angle` | the leg's direction as a turn fraction in `[0, 1)` | yes |
| `signed_x`, `signed_y` | Cartesian in `[-1, 1]`, for a 2-D field sampled on the legs | yes |
| `x y z`, `signed_z`, `u_loop`, `u_loop_signed`, `side_*`, `axial_*`, `corner_dist`, `strip_id`, `distance` | formulas unchanged; on a star most are arbitrary, and `z` is constant on a floor | no |

Trap, measured: with C2 the first LED of every leg sits exactly on the origin, where
`atan2(0, 0)` is 0. So `angle` must come from the strip's direction (`atan2` of `strip.end`),
written to every LED of that strip, and `leg` is ranked on the same number. Computed from LED
positions, one pixel at the hub of each leg takes leg 0's value. Pixels in a gap of the pixel
range (no strip) get `leg` 0 and `radius` 0.

**C4: `ctx.rig`** (`RigInfo` in `surface/base.py`) gains `n_legs`, `leg_angles` (tuple indexed
by `leg`, same unit as `angle`), `leg_lengths_m`, and `radius_m` (metres at `radius` = 1, so
"a 1 m comet" is `1 / radius_m`). Existing fields stay.

**C5: prompt** (`surface/prompt.py`). `PHYSICAL_RIG` and `INSTALL` merge into one generated
`RIG` section: K, each leg's direction, length and LED count, LED density, metres per radius
unit. Its fixed text carries three rules: LEDs exist only on K thin spokes, so a 2-D field is
sampled along them and a free-roaming 2-D particle lights nothing; things travel along legs,
hop between legs, or are rings (a function of `radius` alone); never hard-code K or N. The
`y ∈ {+1, -1}` anti-pattern goes. `_diagnostic_hint` builds its frame list from the frames the
prompt shows. **Size:** the whole prompt stays at or under 26,000 characters (measured today:
25,946, of which the five examples are 12,689). If five new examples do not fit, ship four.

## Work packages, in the order the first night needs them

G1 first. G2, G3 and G4 can then run in parallel. G5 before Xander writes hub-rig effects by
chat. G6 and G7 can follow the gig.

**G1: geometry core.** C1 to C4, plus the two configs: `config.dev.yaml` and `config.pi.yaml`
get three spokes at 90, 210 and 330 degrees (placeholders until the room is measured), ids
`leg_1..3` on pixel ranges 0-449, 450-899 and 900-1349, with `leg_4` (1350-1799) commented
out. Both configs carry other sessions' uncommitted edits: change the strips block only. The
Pi takes the new geometry at its next pull and restart, so the rectangle install cannot be
driven after that. *Done when* one test, parametrised over K = 3 and K = 4, pins: every leg's
first LED at `radius` 0, the longest leg's tip at 1, `angle` constant along each leg including
its first LED, `leg` ranked by direction, every legacy name still present, and a spoke config
round-tripping through the YAML writer. Most test files load `config.dev.yaml`; two assert
its old size and need updating: `tests/test_topology.py` and `tests/test_api.py` (the
`pixel_count == 1800` check).

**G2: simulator** (`web/lib/viz.js`, shared by both front-ends; independent of G1, since it
reads metres from `/topology`). Scale x and y equally unless the layout is strip-like (aspect
above about 6:1, where today's stretched view stays); draw a strip at any angle as per-LED
rectangles on its own line; give the canvas a near-square box for a star on desktop (`#viz`)
and on `/m` (`#sim`). Trap: keep rectangles on whole device pixels
(`LED_color_debug/BLOCKY_COLORS_INVESTIGATION.md`). *Done when* K = 3 and K = 4 draw as stars
and hover and tap-to-solo pick the right LED.

**G3: WZRD colours on the legs.** Package WL1 of [wall-light.md](wall-light.md): the wall's
palette laid from hub to tip, the same on every leg. Until it lands, the WZRD stage spreads
the palette along `u_loop`, which on a star is an arbitrary order.

**G4: the first-night effects.** Every effect is re-implemented against C3 and C4: same name
and intent, on leg and radius, reading only the frames the prompt shows. Keep a param's `key`
where the knob still means the same thing. Tune defaults per D9.

The library effects were rewritten for speed and smoothness on the rectangle; a
re-implementation keeps their three habits (measurements: the traps in
[render-performance.md](../reference/render-performance.md)): motion comes from the effect's
own wrapped phase advanced by `ctx.dt`, one clock per wave term, never from `ctx.t`; colour
comes from a table built in `init` and read with `np.take(..., mode="wrap")`, and particles
are summed with `np.matmul`, not `einsum`; a beat drives a decaying envelope
(`env = max(env * exp(-dt / tau), beat)`), never a one-frame flash.

1. *Split the library* (after the commit named under "Before anything is built"; ledctl
   stopped). `config/effects/` holds the effects directly. Afterwards it holds two tracked
   folders with the same content: `double-line/` (`git mv` the 12 folders there; never edited
   again, never loaded) and `star/` (a copy, the live library). Point the server at `star/`
   (`DEFAULT_EFFECTS_DIR` in `api/server.py`; `tests/test_examples.py` follows it). Trap: the
   playlist path and the WZRD settings path are both derived from the library folder's parent
   (`eff_dir.parent` in `create_app`); pass the config folder instead, so
   `config/playlist.yaml` and `config/wzrd_input.yaml` stay where they are. Not part of the
   library: `config/effects_originals_2026-11-16/` on the Mac, a gitignored backup. Leave it.
2. *The first-night set*, written into `star/`: `pulse_mono` (the boot default, also the seed
   in `surface/examples/`; the seed installer never overwrites, so write both), and six new
   ones: `hub_ember` (hot hub cooling to the tips, noise drifting outward), `slow_breath_radial`,
   `tide`, `aurora_drift`, `beat_rings` (rings leave the hub on a beat, soft), `leg_comets`
   (particles that own a leg and a distance).
3. Reset `config/playlist.yaml` to names that exist in `star/`. Today it names two effects
   that exist only in the Pi's old library; those steps are skipped.

*Done when* `double-line/` is byte-identical to the committed library; the set above installs
through the real compile pipeline in `tests/test_examples.py`, parametrised over K = 3 and
K = 4; and each has been watched in the simulator on both layouts. Report each effect's p95
render time on the Mac; the 5 ms budget is judged on the Pi.

**G5: prompt and its examples.** C5; the three habits listed under G4, taught in the
`PERFORMANCE` and `ANTI-PATTERNS` text inside the same size limit (today the particle idiom
there is `einsum` and nothing warns against `ctx.t` in array maths); and five examples in `surface/LLM_example_effects/`, one
family each: `beat_rings`, `leg_comets` (its `(P, N)` leg mask is the idiom to teach) and
`hub_ember` from G4, plus `fluid_strobe_nebula` (re-tuned: a 2-D field sampled on the spokes)
and `leg_sweep` (a rotating head lights legs by angular distance). The three older examples
predate the habits in G4: copy library versions, not those. *Done when* the prompt built on a
3-leg and a 4-leg topology names no rectangle, row or `u_loop`, shows the right K, is inside
the size limit, and three chat requests (rings on the beat, comets that hop legs, a slow
ambient wash) each install on the first attempt on both layouts. That last check is the only
test of whether the frame list is one an LLM writes good effects with; nothing else here is.

**G6: the rest of the library.** Every other effect in `star/`, with its `double-line/` twin
open as the reference, and the three other seeds. How much changes follows the frames the
original reads: `radius`, `angle` or `signed_x` + `signed_y` keeps its structure and is
re-tuned (`fluid_strobe_nebula`, `palette_pattern_pulse`); `axial_*` becomes `radius`
(`audio_radial`, `center_color_comets`, `sparkle_comets`, and `forest_hues` /
`multi_layer_organic`, which are one piece of code with two palettes: re-implement once,
write to both); `u_loop`, `side_*` or x used as a track is rebuilt on leg and radius
(`rainbow_comet`, `fluid_particles`, `palette_wash_with_kick_sparkles`,
`twin_comets_with_sparkles`). The Pi's old library is not merged (D10); an effect from it
comes back only if Xander names it. *Done when* no file under `star/`, `surface/examples/` or
`surface/LLM_example_effects/` reads a frame outside C3's prompt list, and G4's test covers
all of them.

**G7: docs.** New `docs/reference/rig-geometry.md` (the model, C2, C3, laying out a new room)
with one row in the `CLAUDE.md` documentation map; drain the rectangle from `CLAUDE.md` ("What
this is", coordinate frames, strip mapping, coordinate convention, prompt section order) and
name `star/` as the library and `double-line/` as the frozen copy; move what "Working with the
tracked library" says that still holds into `CLAUDE.md`. Then this plan closes.

## The Gledopto

Nothing to change (D5). For three legs, unplug the output that carries pixels 1350-1799 and
leave its strip out of the config; ledctl then sends 1350 pixels a frame (checked: such a
config loads with the controller still declared at 1800). Unplugging another output leaves a
gap in the pixel range; it works, but prefer the last range.

**Expected to work, not checked on the firmware:** a 1350-pixel frame. Each DDP packet
carries its own byte offset and length (`transports/ddp.py`), so a shorter frame is three
ordinary packets and WLED has no frame length to compare against. If the unplugged range
shows anything on a plugged output, pad each frame with black to the controller's
`pixel_count`.

## On site (needs Xander; an agent never touches the Pi or the Gledopto on its own)

1. Lay the legs out from the Gledopto. Solo each pixel range (`/calibration/solo`, or tap the
   simulator) to see which leg it is, then set that leg's `angle_deg`. Restart ledctl and
   compare the simulator with the floor.
2. Check one frame with a leg unplugged (see The Gledopto).
3. At gig brightness, look at white on the leg tips. Orange tips mean voltage drop: lower the
   brightness master or feed power at the tips.
4. The Pi's first `git pull` needs its old library moved aside first, and its password and
   performance settings are still pending: the three pending blocks under "Hardware & on-site
   operations" in `CLAUDE.md`, and the Pi day in [wzrd-input.md](wzrd-input.md). Then read
   the `perf` block: nothing has ever been measured on the Pi 4.
5. With WZRD running, set the wall-light settings ([wall-light.md](wall-light.md), "Settings
   to try").

## First-night risks this plan does not remove

- **Each leg is 15 m** (450 LEDs at 30 per metre). Three legs laid straight need a floor about
  26 m across. If the room is smaller, the legs bend or double back; D6 covers a bend, and
  the layout has to be decided before the day.
- **Power was designed for two rows** fed from the middle (`Hardware_setup.md`). A star moves
  every injection point; that is hardware work no agent can check.
- **No rehearsal gate exists yet.** The first night is done when, on the real rig, the
  first-night set and the playlist have run for 30 minutes at a steady 60 fps with WZRD
  driving the colours and nothing strobes.
- **Nothing reaches the Pi until it is committed and pushed**, and the tree is far from
  committed (see "Before anything is built").

## Open, for Xander

Answered on 2026-11-16: he commits the whole working tree next (G4 step 1 still waits until
`git ls-files config/effects` lists the library); the first night is **three legs, laid
organically outward from the hub**, so expect curved legs, which D6 enters as straight; the
prompt teaches the three habits (now part of G5).

1. *Proposal:* the order above, which puts seven effects and the simulator before the other
   ten effects and the prompt. Recommended, because the night needs a small tuned set, not the
   whole library.

## Later, not in this plan

- Polyline legs (a leg with a real bend), if a Cartesian effect or the simulator needs it.
- Running the rectangle again. It still loads as four `line` strips, and the `star/` effects
  run on it; the `double-line/` originals need the per-axis normalisation this plan removes.
