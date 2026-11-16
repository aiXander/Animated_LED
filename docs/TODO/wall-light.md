# Wall light: how WZRD's picture drives the LEDs

> **Status: design, nothing built.** Replaces the colour-mapping package (P5) of
> [hub-rig.md](hub-rig.md) and depends on its geometry core (frames `radius` and `leg`).
> How the WZRD input works today: [reference/wzrd-input.md](../reference/wzrd-input.md).
> Xander's brief (2026-11-16): make the LEDs feel as much as possible like they follow the
> visuals, with a mapping that is simple, general and works for any future visual or effect;
> selectable modes, the default one identical on every leg. What he settled and the one choice
> still open are under "Decisions for Xander". This repo is public: no hostnames or addresses in this file.

## What is wrong today (measured, not guessed)

Two WZRD shaders showed the gap. Both were pushed through WZRD's extractor maths
(`led_out::extract`) and ledctl's real `WzrdStage` in a throwaway script.

**Snowfall** (dark brick, sparse cream snow dots) gave flat grey LEDs. The wall sent
`level` 0.09, `white` 0.68, and 97 % of the coloured weight in three orange bins.

1. **One global `white` number bleaches every colour.** Cream is two thirds "white" by
   min-over-max, so the whole colour table is pulled 68 % toward white. Every library effect
   came out beige-grey, whatever its own colours.
2. **A colourless effect is not recoloured at all.** The stage keeps each pixel's smallest
   channel, so an effect that draws white or grey stays exactly neutral. The screenshot's LEDs
   were exactly R = G = B, which only this (or a saturation master at 0) produces.
3. **The contract carries no picture.** `level` is one mean, so "a few bright dots on black"
   and "even dim grey" are the same number; the LEDs got one uniform dim. The bands on the
   LEDs were the effect's own pattern, unrelated to the wall.

**SliceStrobe** (slices that step on and off up to 12 times a second) gave the right colours
with none of the glitch. The wall's timing is on the wire (`level` is unsmoothed), but the
default settings remove it: colours are smoothed over 0.7 s, the level falls back over 0.25 s,
and `led_floor` 0.5 with `wall_bright` 0.25 (then named `follow_level` / `level_full`) lets a dim shader move the LEDs by about a tenth.

## The design

**One buffer, two sliders, one dropdown.** Each frame the stage builds the *wall light*: one
colour per LED, "what the wall shines on this LED". Two amounts say how the effect and the
wall light combine, and `mapping` says which part of the wall each LED sees.

```
tint(i) = wall(i) scaled so its largest channel is 1        # the wall's colour at this LED
E'(i)   = E(i)  + colour * (max(E(i)) * tint(i) - E(i))     # effect keeps its brightness
out(i)  = E'(i) + wash   * (wall(i) - E'(i))                # crossfade to the wall itself
```

- `colour` (exists): the effect's pattern, drawn in the wall's colours.
- `wash` (new, default 0): the wall's own light on the LEDs. At 1 the effect is gone.
- Everything after this is unchanged: the saturation and brightness masters, then the gain
  `led_floor + (1 - led_floor) * clip((level - wall_dark) / (wall_bright - wall_dark))` times
  WZRD's brightness.

`wall(i)` is **relative**: converted from linear light with the output gamma, scaled so its
brightest LED is 1 (98th percentile, with a floor of `0.08 * wall_bright` so a black wall fades
out instead of amplifying noise), clipped by scaling, never per channel. How bright the LEDs
are overall stays the job of the one existing gain. That keeps the rule in
`Runtime._apply_master_output` (gains come after the brightness envelope) intact.

**Mapping modes.** One table of name to "which wall cells feed this LED". A mode needs only
`radius`, `leg` and the legs' left-to-right order, never exact angles, so three or four legs at
any angles work.

| `mapping` | Each LED shows | Needs |
|---|---|---|
| `palette` (default) | the wall's palette laid from hub to tip, each colour as long as its share; every leg identical | today's packets |
| `rows` | the picture from bottom (hub) to top (tip), averaged across its width; every leg identical | the picture |
| `rings` | the picture from its centre (hub) to its edge (tip), averaged round the ring; every leg identical | the picture |
| `bands` | the picture cut into K vertical bands, one per leg in left-to-right order, bottom (hub) to top (tip) | the picture |

`palette` is today's baked table (`bake_table`, kept) read at `radius`. The picture modes read
a 32 x 18 grid that WZRD already computes and does not send (contract W1). With no grid
arriving, a picture mode behaves as `palette` and the modal says so.

**What changes for the operator.** `spread` goes (its job is the mapping). `mapping` and `wash`
come. An effect's own hue no longer picks the wall colour, its position does; an effect's white
takes the wall's colour too (cause 2 above), unless the `tint_white` switch is off: then the
first line of the formula becomes `E + colour * (min(E) + (max(E) - min(E)) * tint - E)`,
which is today's rule and leaves white as white. Automatic: exposure of the wall light, the
fallback to `palette`, hold on lost signal.

**Time.** The grid is smoothed like the palette, by the same `colour_smoothing_s`; no new time
setting. Lost signal holds the last picture (settled 2026-11-13). A hard cut dissolves over the
smoothing time, passing through the mixed colour like a crossfade on the wall. A flash reaches
the LEDs through `level` (attack 0 by default).

## What the prototype showed

Throwaway code, 3 and 4 legs, wall pictures reduced to 32 x 18 exactly as WZRD's tap does.

| Wall | Today | New |
|---|---|---|
| Snowfall | beige-grey; grey with a white effect | warm in every mode; a sparkle effect at `colour` 1 gives cream sparkles on dark; `wash` 0.5 adds a dim warm base |
| One saturated colour | correct | correct in every mode |
| Left-to-right gradient, red/blue halves | both colours somewhere on each leg | `bands`: left leg one colour, right leg the other; `rows`/`rings`: the mix (lilac, purple) |
| Vertical gradient (sunset) | palette order, unrelated to the picture | `rows`/`bands`: orange hub, blue tips |
| Black with one bright region | one dim colour everywhere | `rows`/`rings`: lit where the region is, dark elsewhere; `bands`: only that leg |
| Full rainbow | the palette along each leg | `bands`: a hue per leg; `rows`/`rings`: **white** |
| SliceStrobe-like, tight settings | effect chopped in time with the steps | `bands` + `wash` 0.4: the slices show as segments that blink with the wall |

What it also showed, and the design accepts:

- **Averaging modes mix.** `rows` and `rings` show the light a region really sheds, so a
  many-hued picture goes pale. That is why `palette` is the default: it never mixes.
- **Fine moving texture does not survive a 32 x 18 mean.** Falling snow left no visible motion
  on the LEDs, and 64 x 36 barely helped. Texture and fine motion stay the effect's job: pick
  a sparkle effect for a snow scene. So the grid stays at 32 x 18.
- **Cost (M2, 1800 LEDs).** The per-LED part is 16 µs against 56 µs for today's recolour (the
  hue maths goes). The once-per-frame part (grid to 64 probes per leg, then interpolate) was
  170 µs unoptimised with a dense matrix. Unmeasured on the Pi, as today's stage is.

## Contracts

**W1: `/wzrd/grid`, a third message on the LED link. Addressed to whoever works in the WZRD
repo** (`render-core/src/led_out.rs`; the contract is owned by section 2.15 of that repo's
`docs/reference/render-engine.md` and becomes "C-LED v3 = v2 plus this message"). ledctl
cannot build the picture modes before this lands.

```
/wzrd/grid   int w, int h, int row0, blob
  blob = (rows x w x 3) bytes: rows top to bottom starting at row0, pixels left to right, r g b
  byte = round(255 * srgb(clamp(linear, 0, 1)))
```

- The pixels are the 32 x 18 linear means `extract` already reads (`decode_pixels`), so no
  GPU change. Same capture, same sampling point (before the masters), same rate as
  `/wzrd/colors`; a black wall still sends.
- **A datagram stays under 1200 bytes:** send rows 0-8 and rows 9-17 as two messages. A
  receiver patches the rows it gets and assumes no order or pairing.
- `/wzrd/colors` and `/wzrd/masters` do not change.
- Shared test vector: every pixel linear `(1, 0.5, 0)` gives every byte triple `(255, 188, 0)`.
  Check row 0 is the top of the projected picture with a top/bottom test pattern.
- Not asked for: a finer grid, motion vectors, per-cell contrast (see Later).

**W2: settings** (`WzrdSettings`, persisted as now). Add `mapping: palette|rows|rings|bands`
(default `palette`), `wash: 0..1` (default 0) and `tint_white: bool` (default on, a switch in
the modal); remove `spread`. Trap: a saved `config/wzrd_input.yaml` still has `spread`,
the model forbids unknown keys, and `WzrdSettings.load` answers a validation error by falling
back to every default, `listen` off included. Drop the old key on load.

## Work packages

**WL1: the stage on today's packets** (ledctl only; needs the `radius` frame from hub-rig G1;
this is what the first night needs). Replace the hue lookup in `WzrdStage.recolour` with the
formula above, `wall(i)` from the palette table read at `radius`; W2; a dropdown and a `wash`
slider in `web/lib/wzrd.js` (it has sliders and switches only). Until this lands the hub rig
lays the palette along `u_loop`, which on a star is an arbitrary order. *Done when*
`tests/test_wzrd.py::test_output_stage_recolours_and_follows_wzrd_masters` pins the new maths
(a white effect takes the wall's colour; `listen` off leaves the output untouched), the mode
survives a restart, and `scripts/fake_wzrd.py` shows the palette running hub to tip on 3 and 4
legs.

**WL2: WZRD sends the grid** (WZRD repo; independent of WL1). W1, its unit test next to
`worker_sends_both_contract_messages`, section 2.15 updated.

**WL3: picture modes** (ledctl; after WL1; testable before WL2 through the fake). Parse and
store grid rows in `wzrd/listener.py`; smooth the grid in `WzrdState`; the mode table in
`wzrd/stage.py`, built on a mode change and a topology swap, never per frame; `--grid` in
`scripts/fake_wzrd.py` with a few pictures (gradient, two halves, a moving spot, stepping
slices). Traps: the OSC fast path (`osc.py`, `_FIXED`) knows numbers only, so blob messages
take the slow python-osc path 120 times a second, add a blob case; a handler stores one
reference and does no maths (two half-grids are two stored references); the wall light is
built once per frame in `tick`, not once per rendered leg. *Done when* the fake's pictures
give the "New" column of the table above on 3 and 4 legs, a test extends
`test_cled_packets_are_parsed_or_dropped` with grid packets (bad size, half a frame), and the
copy of the contract in `reference/wzrd-input.md` matches WZRD's.

**WL4: docs.** `reference/wzrd-input.md` (contract copy, the "why" of position-not-hue, the
SliceStrobe recipe below as a runbook entry); close this plan.

## Settings to try (no code)

- **A glitchy shader (SliceStrobe), today:** `led_floor` 0, `level_release_s` about 0.04,
  `colour_smoothing_s` about 0.1, and the "wall range" handles set around the "wall level" the modal shows
  while it plays. The effect is then chopped in time with the wall's steps. With WL3, add
  `mapping` `bands` and `wash` 0.3 to 0.5. No extra mechanism is planned for this: the LEDs
  can only be as glitchy as the wall if they take the wall's timing, and these settings do.
- **The first night (lie-down concert):** the defaults are the calm end. Raise
  `level_attack_s` to about 0.15 so a flashing shader cannot strobe LEDs that lie at eye level.

## Decisions for Xander

Settled by Xander (2026-11-16), do not reopen:

- **An effect's white takes the wall's colour, and he can switch that off.** It is what makes
  a white or grey effect warm under Snowfall. The switch is `tint_white` (W2).
- **`palette` is the default mode.**

Still open:

1. **Whether WZRD sends its picture (WL2) before or after the gig.** "The picture" is a
   32 x 18 pixel copy of what WZRD projects, sent every frame next to the colour statistics
   it sends today. Without it the LEDs have the `palette` mode only; `rows`, `rings` and
   `bands` need it. Recommended: WL1 for the gig, WL2 and WL3 right after, unless the WZRD
   repo has a free agent now.

## Later, not in this plan

- **`ctx.wall` for effects** (`rgb` per LED, `level`, `connected`): an effect that draws with
  the wall, such as sparkles where the wall is bright or rings on a cut. Cheap once the wall
  light exists; costs prompt space and a synthetic wall in the fence test. The stage above
  already covers every effect, including ones the LLM writes, with no effect changes.
- **Sub-cell texture from WZRD** (per-cell contrast in a fourth byte, a dominant motion
  direction), for sparkle density and drift that follow the picture.
- **A `sectors` mode** (each leg takes the wedge of the picture it points at): one table entry.

**Rejected:** auto level (dividing `level` by its own slow peak): Xander sets the wall range
himself so the LED brightness never shifts under him (2026-11-16). A multiply ("the wall gates the effect") beside `wash`: it looked close to `wash`
in the prototype and would be a second slider for the same job. Mapping the effect's
brightness through the wall's tones: a flat effect collapses to one colour. A finer grid: see
above. Per-LED colours computed in WZRD: WZRD must not know where an LED is.
