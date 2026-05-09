# `surface.py` Refactor Plan — Expressive VJ Vocabulary

> **Goal.** Keep the {kind, params} tree paradigm and the LLM-tool contract, but
> radically expand spatial expressiveness so the agent can prompt around-the-loop
> motion, axial-collapse / explode-from-centre, beat-locked direction flips,
> particle-style effects, GEQ bars, ripples on peak, and the kind of layered
> shimmer that makes a crowd dance — without doubling the system-prompt token
> bill.

---

## 1. Executive summary

`surface.py` today is a clean, well-typed graph of `scalar_field × scalar_t × palette → rgb_field`. The compositional core is excellent and we keep all of it. What it lacks is:

1. **Path-aware spatial frames.** The only spatial coordinates are Cartesian `x/y/z` plus radial-from-point. The rig is a *rectangle* (top row + bottom row, centre-fed). To shoot stuff around the perimeter, collapse to centre, or mirror left/right, the LLM has to fight Cartesian with `mul(wave_x, wave_y)` glue — most expressive ideas simply aren't reachable.
2. **No transient / event primitives.** `wave/radial/noise/sparkles/trail` cover steady-state spatial gradients and Poisson grain. There's nothing for a single-shot comet, a ripple that emits on peak, a bouncing ball, GEQ bars, a meteor with a stochastic tail, or any "ballistic state" that WLED gets so much mileage from.
3. **No beat-aware time vocabulary.** The agent has `lfo` (pure clock) and `audio_band` (smoothed energy). It cannot say "flip direction every 4 beats", "trigger on kick peak", "increment a counter on snare", or "every 8th beat, choose a new colour". These are exactly the moves a VJ needs.
4. **Limited spatial → spatial transforms.** No way to shape a field by another (e.g. `mask(field, side=top)`), reflect (`mirror`), warp (`displace`), or push it through a path remap.
5. **Token economy not yet a design pressure.** Adding 50 leaf primitives the WLED way would blow the system prompt. We need *recipes* (high-level effects exposed as one-line primitives) on top of an expanded but still-compact leaf vocabulary.

The plan keeps `surface.py` as the single source of truth, splits it into a small package (`surface/`), introduces **named coordinate frames**, **transient/particle primitives**, **beat-aware modulators**, and **recipe primitives** (parametrised compound effects that read as one node to the LLM but expand internally to a tree). Token impact projected: system-prompt block grows from ~3.5k to ~7-8k tokens — a manageable bump for a >5× expressivity gain, and recipes mean most prompts get *shorter*, not longer.

---

## 2. What the current surface does well — keep verbatim

These are load-bearing and the refactor must preserve them:

- **Strict type-checked compile** (`scalar_t / scalar_field / palette / rgb_field`) with structured per-path error messages — the agent self-corrects on this. The polymorphic combinators (`mix / mul / add / screen / max / min / remap / threshold / clamp / range_map / trail`) and broadcast rules stay exactly as-is.
- **Per-primitive `Params` (pydantic, `extra="forbid"`)** + the `_recover_flattened_params` validator that catches the recurring Gemini-flattening shape.
- **Numeric / palette sugar** (`0.3` → `constant`, `"fire"` → `palette_named`).
- **Live doc generation** (`generate_docs()` ⇒ system prompt block, `primitives_json()` ⇒ REST catalogue).
- **`pulse(by, floor)`** as the canonical audio-reactive shape — it's the right idea and the LLM uses it correctly. Stays.
- **HSV palette baking** (`palette_hsv` with explicit hue paths, `rainbow` baked at uniform brightness).

Nothing in `engine.py / mixer.py / masters.py / topology.py` needs to change behaviourally. We *extend* `Topology` (precompute new derived fields) and *split* `surface.py` into a package, but the public surface (`compile_layers`, `generate_docs`, `primitives_json`, `LayerSpec`, `UpdateLedsSpec`, `EXAMPLE_TREES`) is preserved.

---

## 3. What WLED does that we want — distilled patterns

I read through `wled00/FX.cpp` end-to-end (300+ effects). Stripping the C++ and the historical accretion, every effect reduces to one of a handful of *building blocks*. These are the patterns we want as primitives:

### 3.1 Spatial generators

| WLED idea | What it really is | Already in our surface? |
| --- | --- | --- |
| `mode_palette` (rotated rectangle palette mapping) | scalar_field along arbitrary axis × palette | ✅ `wave` + `palette_lookup` |
| `mode_plasma` | sum of two `cubicwave8` with different phase clocks | ⚠️ Possible via `add(wave, wave)` but verbose |
| `mode_lake` | 3-wave interference: pos-phase × time-phase × amplitude-mask | ❌ No way to say "this is a multi-wave plasma" without 7-node tree |
| `mode_pacifica` | 4 layered noises with independent palettes & phase clocks | ❌ Same |
| `mode_FlowStripe` | radial-from-centre mapped through `sin8(sin8(...))` | ⚠️ Nested `remap` chains |
| `mode_juggle` | 8 colored dots, each at `beatsin8(speed*i)` position | ❌ No "n indexed sine-positioned dots" primitive |
| `mode_wavesins` | `beatsin8` palette index per pixel | ⚠️ |
| `mode_gradient`, `mode_bpm`, `mode_colorwaves` | scrolling palette, optionally width-modulated | ✅ |
| `mode_two_dots` | two equally-spaced moving dots (loop-aware) | ❌ Requires loop coordinate |
| `mode_railway` | alternating colour pairs ramping in/out | ⚠️ |
| `mode_chunchun` / `mode_flow` | offset cycling birds along the strip | ❌ Particle pattern |

**Take-aways.** WLED's biggest expressive wins come from (a) sums of phase-shifted waves, (b) field-along-arbitrary-axis with rotation, and (c) running multiple lightweight emitters that each *index a sine* for position.

### 3.2 Transient / particle effects

| WLED idea | Mechanism |
| --- | --- |
| `mode_comet` | One head moves, exponential fade-out trail. State: previous head position. |
| `mode_meteor` | Like comet but the trail is a stochastic decay (each pixel decays independently). |
| `mode_multi_comet` / `mode_juggle` | N comets with phase-offset positions. |
| `mode_starburst` / `mode_exploding_fireworks` | Explosion: one source emits N fragments at random angles, each fades over its lifetime. |
| `mode_bouncing_balls` | Gravity-driven ballistics: each ball has `(height, vy)`, integrates, bounces, picks new impact velocity. |
| `mode_drip` / `mode_popcorn` | Same shape but inverted gravity / different reset. |
| `mode_ripple` | N ripple slots; each is `{state, color, origin}`, propagates outward as a triwave/cubicwave bump. New ripples are emitted on peak (`mode_ripplepeak`). |
| `mode_lightning` | Random multi-pulse strobe with timing envelope. |
| `mode_fire_2012` / `mode_particlefire` | Heat lattice: cool down each cell, drift, randomly spark, palette-map. |
| `mode_dancing_shadows` | Many beam emitters with random pos / width / palette indices, accumulate. |
| `mode_starshine` etc. | Per-pixel twinkle with PRNG-stable per-pixel clocks (TwinkleFOX). |

**The unifying abstraction:** an *emitter* with state `(position, velocity, lifetime, hue, ...)`, an `update(dt)` step, and a `render(field)` that draws into a fade-buffer. Particle systems = list of emitter instances.

### 3.3 Audio-reactive

| WLED idea | What it does |
| --- | --- |
| `mode_2DGEQ`, `mode_pixels` | Map `fftResult[bin]` to bar height / pixel position. |
| `mode_puddlepeak`, `mode_ripplepeak`, `mode_starburst` | Trigger on `samplePeak == 1` (peak detector). |
| `mode_freqwave`, `mode_freqmatrix`, `mode_rocktaves` | Map `FFT_MajorPeak` (dominant frequency) to hue. |
| `mode_gravcenter*` | Volume drives a "gravity meter" that grows from centre and slowly falls. |
| `mode_DJLight` | Use FFT bins 0/5/15 directly as RGB channels. |
| `mode_blurz` | Splat per-bin colour at random pixel, blur. |

**Take-aways.** Three audio shapes our current surface can't express cleanly:

1. **Peak triggers.** A boolean impulse on transient detection. We must add a `peak` scalar_t (0 most of the time, 1 for one frame on detection) and let it trigger emitters.
2. **Per-bar GEQ.** A vector of band energies indexed spatially — *not* the same as `audio_band(low/mid/high)`. The audio server already has 16 bins; we just don't expose them.
3. **Slow fall ("gravity meter").** A scalar that *attacks* fast on rise and *decays* linearly on idle — `mode_gravcenter`'s `topLED--` per gravity cycle. This is its own shape; pulse() doesn't capture it.

### 3.4 Time / clock vocabulary

WLED leans heavily on `beat8/beat16/beat88` (BPM-synced sawtooth) and `beatsin8` (BPM-synced cosine, range-mappable). Most direction tricks are `beat16(N)` with the bottom bit used as a sign. We don't have a BPM concept yet; `lfo.period_s` is wall-clock seconds.

### 3.5 Compositional operators we're missing

- `blur(field, amount)` — one of WLED's most-used moves; smooths a field across pixel neighbours. We have `trail` for temporal smoothing, but not spatial. Important for masking the seam between strips.
- `mirror(field)` / `kaleidoscope` — fold left/right or N-way.
- `mask(field, region)` — restrict a field to a side, half, or quadrant.
- `displace(field, by)` — warp a field by another (motion turbulence).
- `pixel_shift(field, n)` — offset the entire field by an integer count along an axis (gives discrete WLED-style chasing).

---

## 4. Why a topology-frames overhaul is the centre of gravity

The rig (verified `2026-05-08` mapping in CLAUDE.md):

```
top_left   o─────────────●──────────────o   top_right       y = +0.5
                         │                                  (centre-fed)
                         │ (logical pixel 0 of all 4 strips lives here)
                         │
bottom_left o────────────●──────────────o   bottom_right    y = −0.5
            x = -15                    x = +15
```

— so spatially it's two parallel rows, ~1 m apart, ~30 m wide, all four strips fed at the centre column. The "rectangle" is degenerate (no left / right verticals), but conceptually we still have a *perimeter loop* the agent should be able to address.

**Today** the agent only sees `x ∈ [-1, 1]`, `y ∈ [-1, 1]`, `z ∈ [-1, 1]`. To prompt "rotate around the rectangle clockwise" the LLM would need to *manually* compose `wave(x) * gradient(y > 0) + reverse_wave(x) * gradient(y < 0)`. It doesn't. The fix is to expose **named coordinate frames** — derived once at topology-build time, indexed by name.

### Frames to precompute (fixed cost: 1× topology load)

| Frame | Per-LED scalar (or vector) | Use |
| --- | --- | --- |
| `x`, `y`, `z` | Existing Cartesian, [0, 1] from [-1, 1] | What we have today |
| `signed_x`, `signed_y` | Same but [-1, 1] | "from centre going outward" |
| `radius` | √(x² + y²), normalised to [0, 1] | Concentric rings |
| `angle` | atan2(y, x) / 2π, [0, 1] | Around the centre |
| `u_loop` | Arc-length along perimeter, 0 → 1 going clockwise from top centre | "around the loop" — *the* big win |
| `u_loop_signed` | Same, but [-0.5, +0.5] centred at top | symmetric prompts |
| `side_top` | 1 if y > 0 else 0 | Mask top row |
| `side_bottom` | 1 if y < 0 else 0 | Mask bottom row |
| `side_signed` | +1 top / −1 bottom | "swap top/bottom direction" |
| `axial_dist` | \|x\| ∈ [0, 1] | Distance from centre column (for explode/collapse axially) |
| `axial_signed` | x ∈ [-1, 1] | Symmetric explode |
| `corner_dist` | distance to nearest corner, normalised | Fade towards corners |
| `strip_id` | 0 / 1 / 2 / 3 | Per-quadrant masking |
| `chain_index` | local pixel index along the strip from the controller end, normalised to [0, 1] | Mirrors physical wiring |

**`u_loop` is the killer feature.** With a single scalar field that walks the perimeter, every wave/comet/chase along it reads as motion *around the rectangle*. Direction = sign of the speed. Every existing primitive (`wave`, `radial`, `position`, `gradient`, `noise`) gains an `axis` extension to address it.

### Implementation sketch (topology.py)

```python
@dataclass
class Topology:
    ...
    # New: derived fields, computed once at from_config()
    derived: dict[str, np.ndarray]   # name → (N,) float32 (or int32 for strip_id)

# Order chosen to match a clockwise perimeter walk from top centre:
#   1) top_right (x: 0 → +15, y = +0.5)
#   2) bottom_right (x: +15 → 0, y = -0.5)
#   3) bottom_left (x: 0 → -15, y = -0.5)
#   4) top_left (x: -15 → 0, y = +0.5)
# Each strip is centre-fed, so chain order along the loop sometimes runs
# *with* the strip and sometimes *against*. The walker resolves that from
# (start, end) of each StripConfig.geometry plus `reversed`.
```

The frames live on `Topology.derived` and are immutable after build. Primitives that take an `axis` accept any registered frame name (Cartesian + the new ones above). Any custom frame the operator wants to add later (e.g. "audience-facing front face") becomes a one-liner registered against the topology, no surface change.

### How the agent talks about frames

The system prompt grows by ~250 tokens for a small **FRAMES** section listing each axis with a one-line description (no schema bloat — they're string literals, not new primitives). Existing primitives' `axis: Literal["x","y","z"]` becomes `axis: str` with a runtime check against `topology.derived`; the doc string lists the canonical seven.

---

## 5. Expanded primitive families

**Design rule:** each new leaf fills a gap WLED has shown is musically valuable, has a small `Params` (≤ 5 numeric/discrete fields), and a one-line `summary` in the registered docs. Compound, multi-step recipes go in §6 — this section is the leaves the recipes (and the LLM) compose from.

### 5.1 New `scalar_field` primitives (spatial generators)

| `kind` | Replaces / adds | Params (sketch) |
| --- | --- | --- |
| `frame` | replaces today's `position` | `axis: str` (any registered frame name) |
| `multi_wave` | "plasma / lake-style" — 2-3 phase-offset waves summed | `axis`, `wavelengths: [w1, w2, w3]`, `speeds: [s1, s2, s3]`, `mix: scalar_t` |
| `radial_loop` | radial but along the perimeter — distance is `u_loop` arc-length, not Cartesian | `centre_u`, `wavelength`, `speed`, `shape` |
| `noise3d` | 3D value-noise (current `noise` is 2D — useless on the rectangle's flat z=0 layout but the API stays the same) — *defer*, add only if needed |
| `voronoi` | nearest-of-N moving seeds → field of cell IDs | `n`, `speed`, `seed` |
| `pulse_train` | N evenly-spaced gaussian bumps moving along an axis | `axis`, `count`, `width`, `speed` |
| `lighthouse` | rotating "beam" — narrow gaussian indexed by `angle` minus phase | `width`, `speed`, `centre` |
| `gradient_mask` | piecewise linear ramp with N stops on an axis (for masking) | `axis`, `stops: [(pos, value), ...]` |

`pulse_train` is what we'll lean on hard for "two dots", "four dots", "8-segment chase" effects. `multi_wave` is `mode_plasma` / `mode_lake` collapsed to one node. `lighthouse` is the around-the-room sweeping spotlight we currently can't make.

### 5.2 New `scalar_t` primitives (clock + audio)

| `kind` | Purpose | Params |
| --- | --- | --- |
| `bpm_clock` | BPM-synced sawtooth in [0, 1) per beat | `bpm`, `phase`, `divisor` (1, 2, 4, 8 — beat / half / quarter / eighth) |
| `beat_count` | Floor of `wall_t * bpm / 60` mod N | `bpm`, `mod_n`, `divisor` |
| `audio_peak` | Schmitt-trigger on band energy: 1.0 for `hold_s` after rising-edge, 0 otherwise | `band`, `threshold`, `hold_s`, `cooldown_s` |
| `audio_envelope` | Per-band attack/release envelope (overrides the audio-server's smoothing) | `band`, `attack_s`, `release_s` |
| `audio_band_n` | Indexed band 0..15 (the 16-bin GEQ from the audio server) | `bin: int` |
| `gravity_meter` | Fast-attack / linear-decay scalar, à la `mode_gravcenter` top-LED state | `input` (scalar_t), `decay_per_s` |
| `latch` | Sample-and-hold: hold input value while `gate` is high; release on falling edge | `input`, `gate`, `decay_s` |
| `random_each` | New PRNG draw every `period_s` (or every `gate` rising edge) | `period_s`, `gate`, `seed`, `range` |
| `slew` | Rate-limit a scalar (max change per second) | `input`, `up_per_s`, `down_per_s` |
| `step_select` | Pick element `i` from a fixed list given an integer index | `index` (scalar_t), `values: [float, ...]` |

`step_select(index=beat_count(bpm=120, mod_n=4), values=[1, -1, 1, -1])` is the canonical way to flip direction every beat.

For the agent to use `bpm_clock` / `beat_count`, the operator first needs to set a BPM — the cleanest path is a new master `MasterControls.bpm: float | None` that the agent reads but cannot write (same contract as today's brightness/saturation). With BPM unset (auto-detect from the audio server in a later phase), `bpm_clock` falls back to interpreting `bpm` as Hz × 60 and the agent is told so in the prompt's READ-ONLY MASTERS block.

### 5.3 New `rgb_field` primitives (transient / particle leaves)

These own state across frames (RNGs, particle pools, fade buffers). Each is an emitter+integrator that draws into an internal buffer and returns it.

| `kind` | What it draws | Pool size | Params |
| --- | --- | --- | --- |
| `comet` | Head + exponentially fading tail | 1 | `axis`, `speed`, `length`, `palette`, `palette_pos`, `direction` |
| `comets` | N independent comets at phase-offset positions | 1..16 | `axis`, `count`, `speed`, `length`, `palette`, `phase_spread`, `direction` |
| `meteor` | Like `comet` but with stochastic per-pixel decay (WLED `mode_meteor`) | 1 | `axis`, `speed`, `length`, `palette`, `roughness`, `direction` |
| `ripple` | Outward-propagating bumps along an axis. Auto-emits on `trigger` rising edge. | up to 8 | `axis`, `trigger`, `centre`, `speed`, `width`, `palette`, `decay_s` |
| `bouncing` | Gravity-integrated balls (1 axis, free; or radial, exploding from centre) | 1..16 | `axis`, `count`, `gravity`, `bounce_loss`, `palette`, `trigger` |
| `geq_bars` | N bars from a chosen edge (top/bottom/centre/perimeter), heights from per-bin energies | 16 (fixed) | `axis`, `bins`, `mirror`, `palette`, `peak_decay_s`, `bar_brightness` |
| `starburst` | Emits a fragment burst on `trigger`, fragments fly out and fade | 1..8 emitters | `axis`, `trigger`, `centre`, `speed_range`, `palette`, `lifetime_s` |
| `lightning` | Multi-flash strobe envelope on `trigger` | n/a | `trigger`, `flashes`, `decay_ms`, `palette` |
| `fire` | Heat-lattice along an axis (WLED `mode_fire_2012`) | n/a | `axis`, `cooling`, `sparking`, `palette` |
| `chase_dots` | M evenly-spaced dots scrolling along an axis (no fade — a clean WLED-style chase) | M | `axis`, `count`, `width`, `speed`, `palette`, `direction` |

All particle leaves accept their `trigger` as a `scalar_t` — so `ripple(trigger=audio_peak(band=low))` is the stock "ripple on kick" pattern, and `bouncing(trigger=bpm_clock(bpm=120, divisor=2))` bounces on every other beat.

`geq_bars.axis` accepts `u_loop` ⇒ the bars wrap around the perimeter. With `axis=axial_signed` they explode mirrored from the centre column.

### 5.4 New combinators / spatial operators

| `kind` | Output kind | What it does |
| --- | --- | --- |
| `blur_axis` | input kind | Box-blur a scalar/rgb field along one axis (radius in normalised coords) |
| `mirror` | input kind | Fold a field along an axis (`x → \|x\|` then re-stretch) |
| `kaleido` | input kind | N-way fold (replicate + mirror); useful with `u_loop` for "8-fold around the loop" |
| `displace` | input kind | Sample input field at offset positions modulated by another scalar_field |
| `mask` | scalar_field | `where(field >= threshold, value_a, value_b)` — sharp regional gating |
| `region_mask` | scalar_field | Pre-baked masks: `top`, `bottom`, `left`, `right`, `top_left`, …, `loop_quarter[0..3]` |
| `tile` | input kind | Repeat a field N times along an axis (so you can take a 1-cycle wave and tile it) |
| `pixel_shift` | input kind | Integer-pixel chase along an axis (looks crisp on dense LEDs; differs from continuous `wave`) |
| `where` | input kind | Per-LED ternary: `mask >= 0.5 ? a : b` — the spatial equivalent of `mix.t` |

`where` finally fixes the long-standing anti-pattern: today the agent reaches for `mix.t` to split top/bottom and we have to tell it not to. With `where`, the agent gets a per-LED switch with the same ergonomics.

### 5.5 New palette helpers

- `palette_dynamic_hue` — palette whose hue stops are themselves `scalar_t` nodes (so a beat-driven palette hue cycle is one node, not a `mix(palette_a, palette_b, lfo)` workaround).
- `palette_window(palette, centre: scalar_t, width: scalar_t)` — shrinks/centres the LUT range that gets used. Lets the agent say "scroll a *segment* of the rainbow" with audio-driven width.

---

## 6. Recipes — compound effects exposed as one node

This is the token-economy lever. A *recipe* is a primitive whose `compile()` does not return a hand-written `CompiledNode` — it builds a sub-tree of leaf primitives and compiles that. From the LLM's point of view the recipe is one node with 4-6 params. From the rendering pipeline's point of view it's a normal subtree.

### Why recipes (and not just more leaves)

- **Token economy.** A `comet_chase` recipe with 5 params is ~80 tokens of prompt budget. Replacing it with the equivalent low-level subtree (wave + threshold + trail + palette_lookup + range_map + …) the LLM has to *write* every turn is ~250-400 tokens of *output* and 6-8 nodes the type-checker has to reason about — and the LLM still gets the topology wrong half the time.
- **Self-correction is cheaper.** Recipes have validated invariants (palette is always palette, axis is always a frame name). Most "layer_validation_failed" loops we see today are LLMs over-nesting; recipes flatten the path.
- **Operator presets become small.** `config/presets/peak.yaml` becomes ~5 lines instead of 40.
- **Internally, recipes keep `surface.py` honest.** Anything we'd be tempted to reach for a hidden helper for is just a leaf-tree we can inspect, swap, A/B, or generalise back down to a leaf.

### Recipes to ship in v1

| Recipe `kind` | What it expands to | Params (LLM-visible) |
| --- | --- | --- |
| `chase` | `palette_lookup(scalar=wave(axis, speed, sawtooth, wavelength=count⁻¹), palette, brightness=pulse(audio_band(band)))` | `axis`, `count`, `speed`, `palette`, `band` (optional), `floor` |
| `breathing` | `palette_lookup(scalar=frame(centre), palette=mono, brightness=pulse(lfo or audio, floor))` | `palette`, `period_s`, `band`, `floor` |
| `loop_orbit` | `chase` with axis defaulting to `u_loop`, plus `direction = step_select(beat_count(mod_n), [+1,-1,+1,-1])` | `count`, `speed`, `palette`, `flip_every_beats` |
| `axial_explode` | particles emitted at `axial_dist=0`, drift to `axial_dist=1`, on `trigger` | `palette`, `trigger` (default audio_peak low), `lifetime_s`, `count_per_burst` |
| `axial_collapse` | particles emitted at `axial_dist=1` going inward | mirror of above |
| `ripple_on_peak` | `ripple(trigger=audio_peak(band, threshold), axis=u_loop, palette)` | `band`, `threshold`, `palette`, `decay_s` |
| `geq` | `geq_bars(axis=u_loop, bins=16, palette, mirror, peak_decay)` | `bins`, `axis`, `mirror`, `palette` |
| `pacifica_lite` | 3 stacked waves with phase-offset clocks + an animated palette | `palette_a`, `palette_b`, `speed` |
| `comet_train` | `comets(axis, count, speed, length, palette, phase_spread)` (light wrapper for symmetry with `chase`) | `axis`, `count`, `speed`, `length`, `palette`, `direction` |
| `strobe` | `solid(palette[t]) * threshold(bpm_clock(bpm, divisor), 0.95)` | `bpm`, `divisor`, `palette` |
| `lightning_burst` | `lightning(trigger=audio_peak(low, threshold))` | `palette`, `threshold` |
| `fire_axis` | `fire(axis, cooling, sparking, palette=fire)` | `axis`, `intensity`, `palette` |
| `vortex` | `lighthouse(angle, width=…, speed=…)` × `wave(radius, …)` | `arms`, `speed`, `palette` |
| `kaleido_noise` | `kaleido(noise, n=4)` driving palette | `palette`, `n`, `speed` |

Each recipe's expansion is unit-tested: "given these params, the compiled subtree renders to *this* expected scalar/RGB sample at `t=0.5`". This catches regressions when we tune an internal subtree.

### Recipe is *not* a black box

To preserve the "operator can drill in" property:

- The recipe's `compile()` returns a `CompiledRecipe(CompiledNode)` whose `output_kind` matches the inner root, *and* whose `inner_spec_json` is exposed via `GET /surface/expand/{kind}?params=…`. The operator UI gets a "show as tree" button that swaps the recipe for its expanded form *with the same visual result* — so an operator can take a "chase" recipe, expand it, then tweak one wave inside.
- The agent never sees the expansion. Inspection is an operator-only affordance.

---

## 7. Audio + beat reactivity layer

### 7.1 Three new audio time primitives

1. **`audio_peak(band, threshold, hold_s, cooldown_s)`** — Schmitt trigger on band energy crossing `threshold`. Returns `1.0` for `hold_s` after a rising edge, then `0.0`. `cooldown_s` blanks re-triggers. This is the LLM-visible peak detector. Internally we either:
   - Shadow the audio server's existing `samplePeak`-style detector (best — preserves shaping), or
   - Run our own threshold on `audio_state.{low,mid,high}` if the server doesn't expose a peak signal yet.

2. **`audio_envelope(band, attack_s, release_s)`** — for occasional cases where the audio-server's smoothing is wrong for the visual. Default behaviour is to *not* expose this — `audio_band` already arrives smoothed — but it's there for the rare "I want an angry hard-attack envelope on the snare" prompt.

3. **`audio_band_n(bin: int)`** — direct read of bin 0..15 from the audio server's 16-bin GEQ. This is the underlying scalar the `geq_bars` primitive uses, exposed in case the LLM wants to do something custom.

The audio bridge (`audio/state.py`) needs one new field: `bands: np.ndarray` (16 floats, already ~[0, 1]). The audio server *already publishes them* (its UI shows a 16-bar GEQ); we just need to subscribe to that OSC address (`/audio/bands` or whatever the server uses — TBD by reading the audio-server code at integration time). Soft-fail behaviour: if the bands aren't in the OSC packet, `audio_band_n` returns 0, the `geq_bars` primitive renders empty, and we log once.

### 7.2 Beat clock: BPM-aware time

Two-step plan:

- **v1 — manual BPM master.** Add `MasterControls.bpm: float = 120.0` (new master) and `MasterControls.bpm_phase_offset: float = 0.0` (so the operator can tap-to-align). LLM reads, doesn't write. `bpm_clock(divisor=4)` returns a 4-bar sawtooth synced to the master. Operator UI grows a tap-tempo button + a +/- nudge for phase.
- **v2 — auto-detect.** The audio server already runs an FFT; adding a beat tracker (e.g. a simple onset-detection autocorrelator on the low band) is in-scope for it, not us. When that ships, the master `bpm` becomes "auto" and the agent's prompt block surfaces the detected value. No surface change.

### 7.3 Direction control idioms (now expressible)

```jsonc
// Flip direction every 4 beats:
"direction": {
  "kind": "step_select",
  "params": {
    "index": {"kind": "beat_count", "params": {"divisor": 4, "mod_n": 2}},
    "values": [1.0, -1.0]
  }
}

// Reverse on every kick peak:
"direction": {
  "kind": "step_select",
  "params": {
    "index": {"kind": "counter", "params": {"trigger": {"kind": "audio_peak", "params": {"band": "low"}}, "mod_n": 2}},
    "values": [1.0, -1.0]
  }
}

// Random ±1 per bar:
"direction": {
  "kind": "random_each",
  "params": {"period_s": 1.0, "range": [-1.0, 1.0], "seed": 7}
}
```

The fact that *this is just three lines of params* — not a custom direction primitive — is the win. The agent already understands `step_select` / `counter` / `random_each` semantically.

---

## 8. Token-budget strategy

System-prompt cost is the load-bearing constraint (Gemini, especially, gets confused as the catalogue grows). The expanded surface is designed to fit roughly:

| Section | Tokens (current) | Tokens (after) |
| --- | --- | --- |
| Frames | 0 | ~250 |
| `scalar_t` primitives | ~350 | ~700 |
| `scalar_field` primitives | ~600 | ~1000 |
| `palette` primitives | ~250 | ~300 |
| `rgb_field` primitives | ~250 | ~900 |
| Combinators | ~350 | ~600 |
| Recipes | 0 | ~1500 |
| Anti-patterns + examples | ~700 | ~900 |
| **Total** | **~2.5k** | **~6.2k** |

Budget knobs we keep available if we miss:

1. **Tag primitives with `audience: "llm" | "operator"`.** Anything `operator`-only (e.g. low-level building blocks the recipes use, like `audio_band_n` or `gradient_mask`) is in `primitives_json()` but *not* in `generate_docs()`. Saves ~1k tokens; LLM can still ask via a one-shot "describe primitive" tool if it ever needs one.
2. **Compress descriptions to one line.** Already a rule; will need to recommit during the refactor — `Pulse` and `Sparkles` summaries are currently 5 and 7 lines respectively.
3. **Anchor examples shrink.** Today's `EXAMPLE_TREES` has 7 entries; in the new world the LLM should be reaching for recipes, so examples become 4 entries showcasing one recipe + one custom tree + one audio-peak-driven tree + one beat-direction-flip tree.
4. **Per-output-kind primitive table compaction.** Every entry today emits its full `Params` schema. Switch to a two-line shape: `kind summary` + `params: {p1, p2, p3}` (bare names + types, drop the descriptions in the prompt; full schema still in `primitives_json` for the operator UI).

If we hit the budget early, the cheap escape valve is dropping the hidden-from-LLM building-block primitives (knob 1).

---

## 9. File / package structure

`surface.py` is at 2300 lines today. Adding ~30 new primitives and recipes pushes it to ~5k LoC — well past comfortable. Split it into a package while preserving the public API. **No external import paths change** — `from ledctl.surface import compile_layers, generate_docs, …` still works.

```
src/ledctl/surface/
  __init__.py              # re-exports the public API (compile_layers, generate_docs, EXAMPLE_TREES, …)
  spec.py                  # NodeSpec, LayerSpec, UpdateLedsSpec, _recover_flattened_params
  registry.py              # REGISTRY, @primitive, Primitive, CompiledNode, OutputKind, _broadcast_kind
  compiler.py              # Compiler, CompileError, _format_nodespec_error, compile_child, _compile_unconstrained
  shapes.py                # _apply_shape, hex_to_rgb01, _hsv_to_rgb01
  palettes.py              # NAMED_PALETTES, _bake_lut, _lut_from_*, set_lut_size
  frames.py                # build_topology_frames(topology) → dict[str, ndarray]; doc strings for each frame
  primitives/
    scalar_t.py            # Constant, Lfo, AudioBand, Pulse, Clamp, RangeMap, BpmClock, BeatCount,
                           #   AudioPeak, AudioEnvelope, AudioBandN, GravityMeter, Latch, RandomEach,
                           #   Slew, StepSelect, Counter
    scalar_field.py        # Frame (replaces Position), Wave, Radial, Gradient, Noise, Trail,
                           #   MultiWave, RadialLoop, Voronoi, PulseTrain, Lighthouse, GradientMask
    palette.py             # PaletteNamed, PaletteStops, PaletteHsv, PaletteWindow
    rgb_field.py           # PaletteLookup, Solid, Sparkles, Comet, Comets, Meteor, Ripple,
                           #   Bouncing, GeqBars, Starburst, Lightning, Fire, ChaseDots
    combinators.py         # Mix, Remap, Threshold, Add/Mul/Screen/Max/Min, BlurAxis, Mirror, Kaleido,
                           #   Displace, Mask, RegionMask, Tile, PixelShift, Where
  recipes/
    __init__.py            # registers every recipe via @recipe
    chase.py
    breathing.py
    loop_orbit.py
    axial.py               # axial_explode, axial_collapse
    ripple_on_peak.py
    geq.py
    pacifica.py
    comet_train.py
    strobe.py
    lightning_burst.py
    fire_axis.py
    vortex.py
    kaleido_noise.py
  docs.py                  # generate_docs, _kind_table_row, _compact_params_schema, audience filtering
  examples.py              # EXAMPLE_TREES, ANTI_PATTERNS
```

`recipes/__init__.py` calls `@recipe` on each subclass. Recipes share `compile()` machinery via a `Recipe` base class:

```python
class Recipe(Primitive):
    """A primitive that compiles to a sub-tree of leaf primitives."""

    @classmethod
    def expand(cls, params: BaseModel, topology: Topology) -> NodeSpec:
        raise NotImplementedError

    @classmethod
    def compile(cls, params, topology, compiler):
        sub = cls.expand(params, topology)
        return compiler.compile_child(sub, expect=cls.output_kind, path="(recipe)")
```

So `LoopOrbit.expand(params)` returns a NodeSpec dict and the rest of the compile pipeline runs unmodified. Operator inspection (`GET /surface/expand`) just calls `expand()` and returns the JSON.

---

## 10. Migration plan (phased, low-risk)

**Phase A — package split (no behaviour change).** Move `surface.py` into the package layout above. All tests pass unchanged. ~1 day.

**Phase B — frames.** Add `frames.py`, extend `Topology` with `derived` dict, add `Frame` primitive (a generalised `Position`). Frame `axis: str` validation against `topology.derived`. Update one example (`warm_drift`) to `axis: u_loop` to prove the surface-end works. Backwards-compat: `position` keeps working as a thin alias for `frame(axis=x)`. ~1 day.

**Phase C — leaf expansion (no recipes).** Add the new `scalar_t` (`bpm_clock`, `audio_peak`, `step_select`, `counter`, `random_each`, `slew`, `gravity_meter`, `audio_band_n`, `audio_envelope`), the new `scalar_field` (`multi_wave`, `pulse_train`, `lighthouse`, `gradient_mask`, `radial_loop`), and the new combinators (`blur_axis`, `mirror`, `where`, `mask`, `region_mask`, `tile`, `pixel_shift`, `kaleido`, `displace`). Add the new master `MasterControls.bpm`. ~3 days.

**Phase D — particle / transient leaves.** Add `comet`, `comets`, `meteor`, `ripple`, `bouncing`, `geq_bars`, `starburst`, `lightning`, `fire`, `chase_dots`. Each gets a fixed-size particle pool to keep memory predictable; pool sizes capped by a class constant. Each has unit tests for state determinism (seed → identical render @ given t). ~5 days. **This is where the festival-grade expressiveness shows up.**

**Phase E — recipes.** Add `Recipe` base + the v1 recipe set. Each has expansion tests and a presentational test (sample 5 random param settings, render @ t=0/1/5 s, snapshot). System prompt is regenerated to push the LLM toward recipes-first. Two of today's preset YAMLs get rewritten with recipes to validate the migration path. ~3 days.

**Phase F — audio bridge expansion.** Subscribe to the audio-server's bands OSC address; thread `bands` through `AudioState`; wire `audio_band_n` and `audio_peak`. If the server doesn't currently emit per-bin energy (need to inspect `Realtime_PyAudio_FFT/main.py`), this becomes a small upstream PR. ~1-2 days.

**Phase G — operator UI affordances.** Tap-tempo button, BPM display, recipe expand-to-tree button. Out of scope for this plan but listed because it's the obvious next move once surfaces are in.

**Total estimated effort: 14-16 days of focused work**, roughly two sprints, with the system in a working state after each phase (tests pass, presets render, agent stays functional). Phases C/D/E are cumulatively the biggest expressivity unlock; phases A/B/F unlock the new vocabulary.

### Backwards compatibility

- All existing primitive `kind`s stay registered with identical params.
- All existing presets in `config/presets/*.yaml` keep rendering identically.
- `position(axis=x|y|z|distance)` aliases to `frame(axis=...)` in the spec coercion layer.
- `EXAMPLE_TREES` keeps the v1 entries; new ones are added.
- The agent system prompt grows but the tool contract (single `update_leds(layers, blackout)`) is unchanged.

---

## 11. End-to-end example: what the agent can now say

### 11.1 "Make a clockwise rainbow chase around the rectangle that flips direction every 8 beats and pulses brighter on kicks"

**Today:** unreachable — no `u_loop`, no `step_select`, no `audio_peak`, no recipe. The LLM produces a Cartesian wave, gets it visually wrong (top half drifts left, bottom drifts right), the operator says "no, *around* the loop" and the LLM has no surface vocabulary to fix it.

**After:**

```jsonc
{
  "kind": "loop_orbit",
  "params": {
    "count": 3,
    "speed": 0.5,
    "palette": "rainbow",
    "flip_every_beats": 8,
    "band": "low",
    "floor": 0.4
  }
}
```

That's one node. The recipe expands internally to:

```jsonc
{
  "kind": "palette_lookup",
  "params": {
    "palette": "rainbow",
    "scalar": {"kind": "wave", "params": {
      "axis": "u_loop", "wavelength": 0.333, "shape": "sawtooth",
      "speed": {"kind": "mul", "params": {
        "a": 0.5,
        "b": {"kind": "step_select", "params": {
          "index": {"kind": "beat_count", "params": {"divisor": 8, "mod_n": 2}},
          "values": [1.0, -1.0]
        }}
      }}
    }},
    "brightness": {"kind": "pulse", "params": {
      "by": {"kind": "audio_band", "params": {"band": "low"}},
      "floor": 0.4
    }}
  }
}
```

### 11.2 "Ripple from the centre on every snare hit, ice palette, slowly fading"

```jsonc
{
  "kind": "ripple_on_peak",
  "params": {
    "axis": "axial_dist",
    "band": "mid",
    "threshold": 0.6,
    "palette": "ice",
    "decay_s": 1.2
  }
}
```

### 11.3 "GEQ around the perimeter mirrored from the top centre, colour follows the bin"

```jsonc
{
  "kind": "geq",
  "params": {"axis": "u_loop", "mirror": true, "palette": "rainbow", "peak_decay_s": 0.4}
}
```

### 11.4 "Two-layer scene: a slow purple breath underneath, plus comets shooting outward from the centre on every kick"

```jsonc
{"layers": [
  {"node": {"kind": "breathing", "params": {"palette": "mono_4030c0", "period_s": 4.0, "floor": 0.3}}, "blend": "normal", "opacity": 1.0},
  {"node": {"kind": "axial_explode", "params": {"palette": "fire", "trigger": {"kind": "audio_peak", "params": {"band": "low", "threshold": 0.55}}, "lifetime_s": 0.8, "count_per_burst": 6}}, "blend": "screen", "opacity": 1.0}
]}
```

These are the kind of prompts a VJ types between songs. They're 10-30 tokens of model output, render at 60 fps on the Pi, and they read like the effect they describe.

---

## 12. Testing & observability

- **Unit tests per primitive** (compile valid params, reject invalid ones, render at t=0.5 returns numerically expected slice for a 16-LED toy topology).
- **Recipe expansion snapshot tests** (the *tree* a recipe expands to is stable; a hashing test catches accidental re-architecture).
- **Determinism tests** — every stateful primitive (RNG-driven) given a `seed` renders identically across runs and across `t=0 → t=10` sampled at 60 fps.
- **Token-budget regression test** — `assert len(tokenize(generate_docs())) < 8000` in CI so prompt bloat is caught early.
- **End-to-end "what the LLM sees"** — a fixture that calls `system_prompt.build(...)` against a representative install and snapshots it; the snapshot updates only on explicit re-record.
- **Browser sim parity** — every recipe gets a 1-screenshot baseline in `/editor` so visual regressions surface.
- **Performance budget per primitive** — particle leaves benchmark at ≤ 0.5 ms / frame each on the M2; geq_bars ≤ 0.3 ms; the whole stack budget is 8 ms (1800 LEDs @ 60 fps with headroom). Add `ledctl bench` CLI output that prints per-primitive cost.

---

## 13. What we deliberately are NOT doing

- **No multi-tool agent loop.** One `update_leds` per turn stays. Recipes mean the LLM rarely wants to ask follow-ups anyway.
- **No client-side rendering / GPU.** All numpy on the Pi. Particle pools are bounded so the worst-case per-frame cost is provable.
- **No 2D matrix mode.** WLED's `mode_2D*` family stays out of scope — we have no matrix; the rectangle is sparse 1D-with-thickness, and `u_loop` already gives us the visually-2D moves people want.
- **No effect "layer cooldowns" or sequencer.** The mixer's existing crossfade + presets cover scene transitions; the agent's preset save/recall is the sequencer.
- **No surface-side LFO sync between primitives.** Two `lfo` nodes in the same tree run independent clocks; a `bpm_clock` *globally* phase-locks to the master BPM, which is the only sync the agent should think about.
- **No JS-style scripting / lambdas in params.** Every parameter is a node or a literal. The graph stays inspectable, type-checked, snapshotable, and 100% deterministic given (seed, t).

---

## 14. Bottom line

`surface.py` already nailed the hard part: a typed, composable graph that the LLM can write into. What's left is *vocabulary* — the rectangle, the loop, the beat, the comet. Add path-aware frames, a particle-leaf family, beat-aware time, and 13 recipes; split the file before it gets unmanageable; keep the prompt under 8 k tokens; and the same agent that today produces "warm drift" can produce "fire fountains exploding from centre on every kick, with a slow ice ripple drifting clockwise around the loop". That's the festival.
