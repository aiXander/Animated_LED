# Hub rig: legs outward from the Gledopto

> **Closed 2026-11-16: every package (G1 to G7) is built.** What is still open needs Xander and
> the real rig and lives in [the gig-day plan](../TODO/wzrd-input.md) ("the hub rig on site").
> Xander's brief: *the Gledopto is the origin, a number of legs run outward from it, three or
> four must not matter, and effects are rewritten from the origin outward.* First use: the gig
> of 21 Nov 2026 (indoor lie-down concert, LEDs on the floor beside a projection).

## The model

A rig is a **hub** (the Gledopto, at the origin) and **K legs** (one strip each, data entering
at the hub). Every LED is addressed by **which leg** it is on and **how far out** it sits.
Effects think in those two numbers, so the same effect runs on three legs or four.

Geometry, simulator, LLM prompt and every library effect are on this model. The quadrant walk
`_compute_u_loop` in `surface/frames.py` is kept on purpose (D3).

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
| D8 | **WZRD's colour mapping has selectable modes; in the default every leg behaves the same.** | Xander's call. The design is [wall-light.md](../TODO/wall-light.md). |
| D9 | **The new effects are tuned for the first night: slow, no strobe, low base brightness.** | A lie-down concert beside a projection, LEDs at eye level. |
| D10 | **The effect library is tracked in git and is the same on every device.** Saved knob values are the shared defaults. The Pi's old Pi-only library is set aside, not merged. | Xander's call. A device gets effects by `git pull` and shares its own by commit and push. |

## Work packages

**G1: geometry core. Built.** Spoke strips, origin-centred positions, the `leg` frames and the
`ctx.rig` fields are in the code, and both configs hold `leg_1..3` as spokes at 90, 210 and
330 degrees (placeholders until the room is measured) on pixels 0-1349, with `leg_4`
(1350-1799) commented out. The Pi takes this at its next pull and restart; the rectangle
install cannot be driven after that. Every test that loads `config.dev.yaml` now runs on 1350
LEDs and three legs. Done: see [rig-geometry.md](../reference/rig-geometry.md).

**G2: simulator. Built.** `web/lib/viz.js` draws the legs to scale at any angle (a layout
longer than 6 times its width keeps the stretched band view), in a near-square canvas box on
`/` and `/m`. Tap-to-solo picks the tapped LED and rings it; hover identification is not wired
on either page (both pass `tooltip: null`). Checked in the browser on three legs, four legs
and the rectangle. Done: see [rig-geometry.md](../reference/rig-geometry.md).

**G3: WZRD colours on the legs. Built.** Package WL1 of [wall-light.md](../TODO/wall-light.md): the
WZRD stage lays the wall's palette from hub to tip, the same on every leg, and an effect's
white takes the wall's colour. The rest of that plan (the picture mappings) is not part of
this one. Done: see [wzrd-input.md](../reference/wzrd-input.md).

**G4: the first-night effects. Built.** `config/effects/` holds `double-line/` (the 12
rectangle effects, byte-identical to the last commit, never loaded) and `star/` (the live
library, `DEFAULT_EFFECTS_DIR`). `star/` holds seven effects written on leg and radius:
`pulse_mono` (also the seed in `surface/examples/`), `hub_ember`, `slow_breath_radial`, `tide`,
`aurora_drift`, `beat_rings`, `leg_comets`; and 11 rectangle-era effects, which G6 redid.
`config/playlist.yaml` plays the seven, 120 s each. `tests/test_examples.py` installs every
effect in `star/` and the seed folder through the real pipeline on K = 3 and K = 4. All seven
were watched on both layouts with a fake audio feed at 84 bpm: calm, nothing flashes. p95 per
frame on the Mac in a tight loop, K = 3 / K = 4, in µs: `pulse_mono` 32 / 38, `hub_ember`
26 / 31, `slow_breath_radial` 25 / 30, `tide` 44 / 51, `aurora_drift` 35 / 45, `beat_rings`
59 / 67, `leg_comets` 58 / 56 (the budget is 5,000, judged on the Pi).

**G5: prompt and its examples. Built.** `surface/prompt.py` generates one `RIG` section from
the topology (leg count, each leg's LEDs, length and direction, LED density, metres per radius
unit) followed by the three fixed rules; it lists only `PROMPT_FRAMES`; `PERFORMANCE` teaches
the three habits and the leg-masked `matmul` particle idiom. The runtime's error hints name the
same frames. **Four examples, not five:** `beat_rings`, `leg_comets`, `hub_ember` and the new
`leg_sweep` (also in `star/`), each a byte copy of its library effect. Five did not fit the
26,000-character limit, so `fluid_strobe_nebula` is a library effect only. The prompt is 25,821
characters on three legs and 25,865 on four; `tests/test_examples.py` pins the limit, the leg
count and the absence of rectangle words, and installs the examples on both layouts. Chat
check, 6 paid calls to `openai/gpt-6-luna`: rings on the beat, comets that hop legs and a slow
ambient wash each installed on the first attempt on three legs and on four, reading only
prompt frames and never `ctx.t`. Done: see `CLAUDE.md` ("The system prompt").

**G6: the rest of the library. Built.** The 11 rectangle-era effects in `star/` and the three
other seeds (`audio_radial`, `palette_wash_with_kick_sparkles`, `twin_comets_with_sparkles`,
identical in `surface/examples/`) are rewritten on leg and radius and tuned for D9. No file in
`star/`, `surface/examples/` or `surface/LLM_example_effects/` reads a frame outside
`PROMPT_FRAMES` or uses `ctx.t`. All were watched on three and four legs with a fake audio
feed at 84 bpm. p95 per frame on the Mac, K = 3 / K = 4, in µs: 24 / 30 (`audio_radial`) to
73 / 92 (`twin_comets_with_sparkles`). Xander's saved values were kept where the knob kept its
meaning, except speeds and strobe amounts, which were lowered for D9. Done: see
[rig-geometry.md](../reference/rig-geometry.md) ("Writing an effect for the hub rig").

**G7: docs. Built.** `CLAUDE.md` describes the hub rig throughout and holds the rules for the
tracked library; [rig-geometry.md](../reference/rig-geometry.md) holds the model, the effect
habits and the unchecked 1350-pixel frame; the on-site steps and first-night risks moved to
the gig-day plan.
