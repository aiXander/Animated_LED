# Wall light: how WZRD's picture drives the LEDs

> **Status: WL1 is built (the `palette` mapping, `colour` / `wash` / `tint_white`, the
> settings and the panel controls). Open: WL2 (WZRD sends its picture, in the WZRD repo), WL3
> (the three picture mappings) and WL4 (docs).** The geometry it reads (frames `radius` and
> `leg`): [reference/rig-geometry.md](../reference/rig-geometry.md).
> How the WZRD input works today: [reference/wzrd-input.md](../reference/wzrd-input.md).
> Xander's brief (2026-11-16): make the LEDs feel as much as possible like they follow the
> visuals, with a mapping that is simple, general and works for any future visual or effect;
> selectable modes, the default one identical on every leg. What he settled and the one choice
> still open are under "Decisions for Xander". This repo is public: no hostnames or addresses in this file.

## What was wrong, and what is still wrong (measured, not guessed)

Of the three causes below, WL1 removed the second (a white effect now takes the wall's
colour). The first and the third need the picture: the `palette` mapping still has only the
one global `white` number and no idea where on the wall the light is.

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

**One buffer, two sliders, one dropdown.** Each frame the stage holds the *wall light*: one
colour per LED, "what the wall shines on this LED". Two amounts say how the effect and the
wall light combine, and `mapping` says which part of the wall each LED sees.

```
tint(i) = wall(i) scaled so its largest channel is 1        # the wall's colour at this LED
E'(i)   = E(i)  + colour * (max(E(i)) * tint(i) - E(i))     # effect keeps its brightness
out(i)  = E'(i) + wash   * (wall(i) - E'(i))                # crossfade to the wall itself
```

Built in `WzrdStage.recolour` for the `palette` mapping, where every entry's largest channel
is already 1, so one array (`_wall`) serves as both `wall` and `tint`. A picture mapping has
dim cells, so WL3 keeps the two apart. Everything after the recolour is unchanged: the
saturation and brightness masters, then the gain that follows the wall level.

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

`palette` is the baked table (`bake_table`) read at `radius`. The picture modes read a 32 x 18
grid that WZRD already computes and does not send (contract W1). With no grid arriving, a
picture mode behaves as `palette` and the panel says so under the dropdown; the stage reports
the mapping in use as `status.mapping_active`, which is hard-coded to `palette` until WL3.

**Still automatic in WL3, not built:** exposure of the wall light. Hold on lost signal and the
fallback to `palette` exist.

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

**W2: settings. Built:** `mapping`, `wash` and `tint_white` are in `WzrdSettings`; `spread` is
gone and dropped from old files on load.

## Work packages

**WL1: the stage on today's packets. Built.** `WzrdStage` (`src/ledctl/wzrd/stage.py`) lays
the wall's palette from hub to tip on every leg and combines it with the effect by the
formula above; the panel (`src/web/lib/wzrd.js`) has the mapping dropdown, the `wash` slider
and the `tint_white` switch. One change from the design: `bake_table` now lays the palette on
a line (entry 0 at the hub, the ends not blurred into each other) instead of round the hue
circle, because nothing looks it up by hue any more and the circle put a sliver of the first
colour at every tip. Watched with `scripts/fake_wzrd.py` on 3 and 4 legs.
Done — see [reference/wzrd-input.md](../reference/wzrd-input.md).

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

**WL4: docs.** `reference/wzrd-input.md` has the "why" of position-not-hue since WL1. Left:
the contract copy, the SliceStrobe recipe below as a runbook entry; close this plan.

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
