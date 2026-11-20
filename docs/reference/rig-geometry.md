# Rig geometry: hub and legs

The rig is a **hub** (the Gledopto, at the origin) and **K legs** (one LED strip each, data
entering at the hub). Every LED is addressed by which leg it is on and how far out it sits, so
the same effect runs on three legs or four. The path is: the `strips` block of the config →
`Topology` (positions in metres, then normalised) → named per-LED frames → `ctx.frames` and
`ctx.rig` in an effect. K is never typed anywhere in the code: going from three legs to four is
one strip entry in the config.

## Where the code lives

- `src/ledctl/config.py`: the two geometry types, `LineGeometry` (`start`, `end`) and
  `SpokeGeometry` (`angle_deg`, optional `length_m`, `offset_m`), and `StripConfig.start` /
  `.end`, the endpoints in metres for either type.
- `src/ledctl/topology.py`: `Topology.from_config` places every LED, sets `radius_m` and
  normalises the positions.
- `src/ledctl/surface/frames.py`: `build_frames` computes every frame; `strip_direction` and
  `rank_legs` decide which leg is which; `FRAME_DESCRIPTIONS` names every frame (the sandbox
  lints `ctx.<name>` against it) and `PROMPT_FRAMES` is the subset the LLM is shown.
- `src/ledctl/surface/base.py`: `RigInfo` (`ctx.rig`), filled in `Runtime._build_init_ctx`.
- `src/ledctl/api/server.py`: `/topology` (metres, plus each strip's `start` and `end`, which
  the simulator draws from) and `_strips_to_yaml_dicts`, the writer behind `PUT /config`.
- `src/web/lib/viz.js`: the simulator's drawing, shared by `/` and `/m`. It publishes the view
  it chose as `<html data-rig-layout="lotus|strip">` and the lotus's width:height as
  `--rig-aspect`; the CSS of `web/index.html` and `web/index-mobile.html` sizes the canvas box
  from those.
- `config/config.dev.yaml`, `config/config.pi.yaml`: the `strips` block.
- `config/effects/star/`: the library, every effect written on these frames;
  `config/effects/double-line/` is the frozen rectangle version of twelve of them.

## The model

**Axes.** Origin = the Gledopto. x-y = the plane the legs lie in (a floor rig is seen from
above; point +y at the projection wall). z = height off that plane.

**A spoke** is a strip written as `geometry: {type: spoke, angle_deg: 90}`: 0 = +x,
counter-clockwise. `length_m` defaults to `(pixel_count - 1) / leds_per_meter`; `offset_m` is
the first LED's distance from the hub, default 0.

**Normalisation.** `normalised = positions / radius_m`, where `radius_m` is the distance of the
farthest LED from the origin. One scale for all three axes, no re-centring. `bbox_min` and
`bbox_max` stay in metres.

**The hub-rig frames.** New effects read only these:

| Frame | Meaning |
|---|---|
| `radius` | 0 at the hub, 1 at the farthest LED |
| `chain_index` | fraction along the LED's own leg, 0 at the hub end, 1 at its tip; equals `radius` when all legs are the same length |
| `leg` | int32 `0..K-1`, legs ranked by ascending direction |
| `leg_phase` | `leg / K` |
| `angle` | the leg's direction as a turn fraction in `[0, 1)`, the same on every LED of the leg |
| `signed_x`, `signed_y` | Cartesian in `[-1, 1]`, for a 2-D field sampled along the legs |

`ctx.rig` carries the same model as numbers: `n_legs`, `leg_angles` and `leg_lengths_m` (both
indexed by `leg`) and `radius_m` (metres at `radius` = 1, so a 1 m comet is `1 / radius_m`
long).

**The older frames** (`x y z`, `signed_z`, `u_loop`, `u_loop_signed`, `side_*`, `axial_*`,
`corner_dist`, `strip_id`, `distance`) come from the rectangle this rig replaced. Their
formulas are unchanged so effects written for it still compile; on a star most of them are an
arbitrary order.

## Why it's built this way

- **One uniform scale about the origin, not a fit to the bounding box.** A 3-leg star has a
  lopsided bounding box: fitted per axis, the hub lands at `radius` 0.24 and angles are
  distorted. With one scale the hub is at 0 for any K.
- **Endpoints live on `StripConfig`, not on the geometry.** A spoke's default length needs the
  strip's `pixel_count`, which a geometry model cannot see.
- **`angle` and `leg` come from the strip's direction, not from LED positions.** The first LED
  of every leg sits exactly on the origin, where `atan2(0, 0)` is 0, so a per-LED angle gives
  one pixel at the hub of each leg the wrong leg. `strip_direction` reads the strip's endpoint
  that is farther from the origin.
- **A leg is one straight line.** A leg that bends on the floor is entered as straight: leg
  and distance survive the bend, only Cartesian effects and the simulator picture drift.
  Polyline legs are not built; build them only if one of those two needs a real bend.
- **The simulator draws a hub rig as a lotus, not to scale.** Drawn to scale a star is a
  near-square that costs a lot of screen height. The lotus puts the hub at the bottom centre
  and splits the legs left and right (by the side their real direction points to; a leg
  straight up or down goes to the emptier side). On each side the leg nearest horizontal lies
  flat and the others fan upward evenly, up to a max angle the operator sets with a slider on
  the sim (5–90°, kept per browser in `localStorage`). Each LED keeps its real distance from
  the hub, so only the angles are schematic. Four legs at 90° draw the two upper legs on top
  of each other. A layout whose bounding box is longer than 6 times its width (the old 30:1
  rectangle) is strip-like instead: x and y stretch separately into thick bands.
- **The Gledopto is not reconfigured per layout.** WLED keeps four outputs of 450 pixels and
  the controller entry stays at 1800. Three legs is the last output unplugged and its strip
  left out of the config; ledctl then sends 1350 pixels a frame. Each DDP packet carries its
  own byte offset and length, so WLED has no frame length to compare against. If a plugged
  output ever shows the unplugged range, pad each frame with black to the controller's
  `pixel_count`.
- **The rectangle still loads** as four `line` strips, but its effects were tuned to the
  per-axis fit: under the uniform scale its two rows sit at `signed_y` ±0.03, not ±1.

## Writing an effect for the hub rig

`beat_rings`, `leg_comets`, `hub_ember` and `leg_sweep` in `config/effects/star/` are the
patterns to copy; the LLM prompt shows the same four.

- **Three habits.** Motion comes from the effect's own wrapped phase advanced by `ctx.dt`, one
  clock per wave term, never from `ctx.t`. Colour comes from a table built once in `init` and
  read with `np.take(..., mode="wrap")`; particles are summed with `np.matmul`. A beat drives
  a decaying envelope (`env = max(env * exp(-dt / tau), beat)`), never a one-frame flash.
  The measurements behind them are in [render-performance.md](render-performance.md).
- **What a rectangle idea becomes.** Distance from the centre is `radius`. A lap of the rig is
  out along a leg and back in: `(leg + 0.5 * chain_index) / K` going out,
  `(leg + 1 - 0.5 * chain_index) / K` coming back (`rainbow_comet`). Legs that fire in turn:
  `radius + leg_phase` (`center_color_comets`). A particle lives on one leg, hops to another
  when it reaches the hub and turns back at a tip (`fluid_particles`, `leg_comets`). A 2-D
  field is sampled with `signed_x` / `signed_y` (`fluid_strobe_nebula`).
- **The library is tuned calm:** slow, no strobe, low base brightness, for LEDs at eye level
  beside a projection. Bass swells the brightness by a fraction of its silent level instead of
  filling the range to full: `palette_pattern_pulse` jumped half the brightness range in one
  frame on a kick until it did.
- **A passing test does not show which frames an effect reads**, since the older frames still
  compute. Check with `grep -o "ctx.frames.[a-z_]*"` against `PROMPT_FRAMES`.

## Invariants and traps

- `test_hub_rig_geometry` in `tests/test_topology.py` pins, for K = 3 and K = 4: every leg's
  first LED at `radius` 0, the longest leg's tip at 1, one `angle` per leg, `leg` ranked by
  direction, every older frame still computed, and a spoke config surviving the YAML writer.
- **In the simulator every LED is an axis-aligned rectangle on whole device pixels, and the
  canvas bitmap is exactly the device pixels the canvas covers.** A leg drawn with a rotated
  transform or a stroked line, or a bitmap one pixel off its box, makes the browser blend
  neighbouring LEDs' colours. No error; it looks like a blocky or smeared effect
  (`LED_color_debug/BLOCKY_COLORS_INVESTIGATION.md`). On a star one LED is about one device
  pixel long, so below that neighbours overpaint each other.
- **Read `strip.start` / `strip.end`, never `strip.geometry.start`.** A spoke has no `start`;
  the read raises only once a spoke config is loaded.
- **The YAML writer dumps the geometry model as it is.** Written by hand as `type` + `start` +
  `end`, a spoke is saved as a line with no angle and the config stops loading.
- **`PUT /config` rewrites the whole file** and drops its comments, including a commented-out
  fourth leg. The previous file is kept next to it as `.bak`.
- **Every name in `PROMPT_FRAMES` is printed in the LLM prompt**, which has a size limit
  (26,000 characters, pinned by `tests/test_examples.py`), and named in the runtime's error
  hints. A frame left out of it still computes but the LLM never hears of it. The test above
  fails if a frame has no description.
- **Keep `reversed: false` on a spoke.** `reversed` puts pixel 0 at the tip, and then
  `chain_index` runs tip to hub while `radius` still runs hub to tip.
- **Pixels in a gap of the pixel range** (no strip) sit at the origin: `leg` 0, `radius` 0.
- **A palette's dark tones multiply into an effect's brightness.** On thin legs an effect
  whose brightness comes from a separate pattern (a breath, a band, a ring) and whose colour
  comes from a palette entry ends up near black wherever the palette is dark; no error, the
  simulator just looks empty. Such an effect lifts the table once:
  `lut / np.maximum(lut.max(axis=1, keepdims=True), 0.3)` (a black entry stays black).
  `slow_breath_radial` in `config/effects/star/` is the pattern to copy.
- **Most tests load `config/config.dev.yaml`**, so its layout is the rig they run on. A test
  that asserts a pixel count, or is tuned to one (an effect "slow enough" at 1800 LEDs, a
  slice that assumes the count divides by 4), breaks when the layout changes.

## Runbook: laying the legs out in a room

1. Lay the legs out from the Gledopto. Solo each pixel range (`/calibration/solo`, or tap the
   simulator) to see which leg lights, then set that strip's `angle_deg`.
2. Restart ledctl and compare the simulator with the floor.
3. A leg that is shorter, or starts away from the hub: set `length_m` or `offset_m`.
4. Three legs instead of four: unplug the output that carries the last pixel range and remove
   that strip entry. Unplugging another output leaves a gap in the pixel range; it works, but
   prefer the last range.
