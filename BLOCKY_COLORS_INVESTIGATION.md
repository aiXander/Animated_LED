# Investigation: blocky color jumps in LED simulator

This document summarises an investigation into a persistent visual artefact —
**discrete color "jumps" between adjacent LEDs** in the browser simulator —
that has resisted multiple fixes. Tests pass, the rendered data is verifiably
smooth at the float and uint8 layer, and the simulator now renders each LED as
a discrete integer-pixel rectangle, yet the user still observes color jumps.
Handing this off to a fresh agent.

---

## Symptom

Reported by user: *"there's a sudden jump in the color values at neighbouring
pixels"*. Visible in the browser simulator (config `config.dev.yaml`, transport
`simulator`). User has not yet confirmed whether the same artefact appears on
physical LEDs.

The simplest reproducer the user provided is loading
`config/presets/color_waves.yaml`:

```yaml
layers:
- blend: normal
  opacity: 1.0
  node:
    kind: palette_lookup
    params:
      brightness:
        kind: envelope
        params:
          input: {kind: audio_band, params: {band: low}}
          attack_ms: 30
          release_ms: 500
          floor: 0.65
          ceiling: 1
      palette: rainbow
      scalar:
        kind: wave
        params:
          axis: x
          shape: cosine
          softness: 1
          speed: 0.3
          wavelength: 1
          cross_phase: [0, 0.15, 0]
```

User has shown four photos over the course of the investigation, each after a
different fix attempt. The artefact persists in the latest one.

---

## What we know is correct (verified)

1. **The control-surface output is smooth at the float layer.** Rendering
   `color_waves.yaml` via `compile_layers()` and `node.render(ctx)` gives
   per-LED RGB tuples that walk smoothly through the rainbow. Verified by
   dumping LED 0–449 of the first strip — e.g. LEDs 165→225 trace
   `(255,0,255) → (225,0,255) → (185,0,255) → (126,0,255) → (0,82,255)`,
   monotone in every channel. There is no plateau-then-jump in the raw data.

2. **uint8 + gamma quantisation produces ~700 distinct color tuples** on the
   1800-LED install for this preset. Nowhere near the ~9 visible blocks the
   user originally reported, so uint8/gamma is not the bottleneck. Even at
   master brightness 0.05, 321 distinct tuples remain.

3. **All 180 backend tests pass** including new HSV-palette and
   smooth-gradient regression tests.

4. **The transport sends raw bytes**: `SimulatorTransport.send_frame` does
   `pixels.tobytes()` and `ws.send_bytes(data)` — no transformation between
   the engine output and the websocket frame.

5. **The browser renders each LED as a precomputed integer rectangle**: see
   `src/web/index.html` `reproject()` and `draw()`. Adjacent rects share an
   integer edge so there is no overlap, no gap, and no sub-pixel AA on the
   left/right boundary pixels.

---

## Hypotheses tried (chronological)

### 1. Nearest-neighbor LUT lookup → coarse quantisation

**Assumption.** `_CompiledPaletteLookup.render()` rounded `t * 255` to the
nearest of the LUT's 256 entries. With 1800 LEDs across one palette pass,
~7 LEDs collapsed onto each LUT entry, producing visible color stairs.

**Fix attempted.** Linear interpolation between adjacent LUT entries.

**Result.** Test pinned that distinct color count exceeded 256. **Did not fix
the user-visible artefact.** Reverted in favor of #2.

### 2. Bigger LUT instead of per-LED interp

**Assumption.** A larger nearest-neighbor LUT (1024 entries) gives the same
visual smoothness as linear interp, at lower per-frame cost.

**Fix attempted.** `LUT_SIZE = 256 → 1024`. Kept nearest-neighbor lookup.

**Benchmarks.**

| variant | per-frame cost (1800 LEDs) |
|---|---|
| 256 LUT, nearest (original) | 47 µs |
| 256 LUT, linear interp | 161 µs |
| **1024 LUT, nearest (chosen)** | **59 µs** |

**Currently active.** All `palette_lookup` calls now sample a 1024-entry LUT.
Test `test_palette_lookup_smooth_on_axis_gradient` pins this. **Did not fix
the user-visible artefact.**

### 3. Stale running process

**Assumption.** User's `ledctl run` server had old code in memory.

**Fix attempted.** Asked user to restart the process.

**Result.** User restarted. Artefact persisted.

### 4. Cosine wave plateaus

**Assumption.** `wave.shape: cosine` has `d/dx[cos(2πx)] = -2π sin(2πx)`,
which is exactly **zero at peaks and troughs**. So near each cosine extreme,
many LEDs share near-identical scalar values → near-identical palette positions
→ visible color plateau. Computed: 26-LED plateau at the trough of a
wavelength=1 cosine wave on a 1800-LED install.

| shape | longest plateau | plateaus ≥ 5 LEDs |
|---|---|---|
| cosine | 26 | 17 |
| sawtooth | 4 | 0 |
| triangle | 2 | 0 |

**Fix attempted.**
- Changed `wave.shape` default `cosine → sawtooth`.
- Rewrote the `shape` field description to call out the cosine plateau.
- Added an anti-pattern entry teaching the agent which shape to pick when.
- Pinned `axis_cross` example tree to explicit `cosine` (it relied on the old
  default for its diamond visual).

**Currently active.** **Did not fix the user-visible artefact.** User
clarified: *"the problem is not regions of flat, equal color, that's fine. The
real issue is non-gradual jumps between neighbouring pixels."* So plateaus
were not the user's complaint.

### 5. RGB-space palette interp produces muddy midpoints

**Assumption.** Linear interpolation in RGB between e.g. red and cyan walks
through grey at the midpoint, conflating chromaticity with brightness.
Brightness should be controlled separately (master / per-LED) so the LUT can
be used purely for chromaticity.

**Fix attempted.** Added a `palette_hsv` primitive that interpolates in HSV
space (hue/sat/val stops, signed hue for direction control). Restructured
`NAMED_PALETTES` to a tagged `{interp: "rgb"|"hsv", stops: ...}` dict.
Converted the `rainbow` named palette to be HSV-baked under the hood (every
entry now sits on the saturated chromatic surface at full brightness).

**Currently active.** Tests pin uniform brightness and direction control.
**Did not fix the user-visible artefact.**

### 6. Simulator drew LEDs at fixed 6×6 px → overpainting

**Assumption.** `web/index.html` had `LED_SIZE = 6`; on a 1200px-wide canvas,
the install's 1800 LEDs (900 per row) projected to ~1.33px spacing. Each LED's
6×6 square overpainted its 4–5 right-side neighbours. Only ~every 6th LED's
color was visible, exactly matching the original photo's ~9 blocks per row.

**Fix attempted.** Made `ledSize` adaptive: `floor(median consecutive same-strip
LED spacing)`, capped at 6, min 1. The "skip cross-strip jumps" detail used
`local_index === 0` to detect chain resets so cross-strip gaps did not skew
the median.

**Result.** First photo's 9-block artefact disappeared. User now saw ~1 px
dots, said they were too small and *still* showed jumps.

### 7. Per-strip image bar with `drawImage` smoothing

**Assumption.** Render each strip as a `(pixel_count, 1)` offscreen canvas,
then `drawImage` it onto the main canvas with `imageSmoothingEnabled = "high"`
to interpolate between adjacent LED colors. Each strip becomes a thick smooth
bar.

**Fix attempted.** Added per-strip offscreen canvases, `putImageData` per
frame, `drawImage` with rotation and smoothing.

**Result.** **User pushed back hard:** *"the screen should just directly
visualize the actual LED RGB colors that are coming out of the controller… It
doesn't help me if things somehow look fine on my screen due to some
interpolation hack."* Reverted.

### 8. Per-LED tall rectangles with exact float spacing

**Assumption.** Draw each LED as a discrete tall rectangle (no smoothing) at
its exact projected position with `step = length / (n - 1)`. Browser's edge
AA on rect boundaries was assumed to be acceptable since "it doesn't change
any LED's color".

**Fix attempted.** Per-strip rotation transform; per-LED `fillRect(i*step,
-halfThick, step, STRIP_THICKNESS)`.

**Result.** Photo showed visible **vertical dark lines between every pair of
LEDs** — the canvas's edge AA on fractional-coord rects WAS blending colors at
the boundary pixels, exactly the "stacking brightness" the user told us to
avoid.

### 9. Integer-pixel tiling (current state)

**Assumption.** Sub-pixel rect coords cause the canvas to alpha-blend
neighbouring colors at boundary pixels. Force every rect to integer
coordinates so adjacent rects share an exact integer edge with no
fractional-pixel coverage.

**Fix attempted.**
- Precomputed `Int32Array ledRects` of `(x, y, w, h)` per LED in `reproject()`.
- Each LED's left edge `= round(its float position)`. Width
  `= next LED's rounded position − this LED's`. Adjacent rects tile exactly.
- Strips treated as axis-aligned along the dominant axis (no rotation).
- `draw()` sets `globalAlpha = 1`, `globalCompositeOperation = "source-over"`,
  `imageSmoothingEnabled = false`, then plain `fillRect` per LED with solid
  `rgb(...)` fill.

**Currently active.** **User reports color jumps are still visible.**

---

## Current state of relevant code

### `src/ledctl/surface.py`
- `LUT_SIZE = 1024` (line 124).
- `_CompiledPaletteLookup.render` does nearest-neighbor lookup at the larger
  LUT size.
- `wave.shape` default = `sawtooth`. Cosine still available but documented
  as "for breathing brightness on mono palettes, NOT smooth color sweeps."
- New primitive `palette_hsv` with hue/sat/val stops; bakes via
  `_hsv_to_rgb01` (vectorised piecewise HSV→RGB).
- `rainbow` named palette is HSV-baked; `fire`, `ice`, `sunset`, `ocean`,
  `warm`, `white`, `black` stay RGB.
- New example tree `chromatic_drift` and an anti-pattern paragraph about the
  cosine plateau, both fed into the agent system prompt via `generate_docs()`.

### `src/web/index.html`
- `STRIP_THICKNESS = 16` (perpendicular bar height in CSS px).
- `loadTopology()` stores both `leds` and `strips` from `/topology`.
- `reproject()` projects every LED to canvas coords, then builds `ledRects`
  Int32Array. Strip orientation detected per-strip; horizontal strips use
  `(yTop, integer x columns)`, vertical strips use `(xLeft, integer y rows)`.
  Adjacent rects share an integer edge.
- `draw()` iterates `ledRects` with one `fillRect` per LED; no rotation, no
  smoothing, no alpha.

### Tests
- 180 pytest tests, all passing. Relevant ones:
  - `test_palette_lookup_smooth_on_axis_gradient`
  - `test_rainbow_uses_hsv_uniform_brightness`
  - `test_palette_hsv_endpoint_colours_match_hue_degrees`
  - `test_palette_hsv_signed_hue_picks_direction`
  - `test_palette_hsv_compiles_via_primitive`

---

## Things NOT yet investigated

A fresh pair of eyes should consider:

1. **Is the artefact on the actual LEDs or only in the browser?** User has
   only shown simulator photos. If the artefact is browser-only it is purely
   a render bug; if it's also visible on physical strips, there's something
   in the engine output / DDP path / WLED that we have not accounted for.
   *Easy test: hook up DDP, watch the physical install with the same preset.*

2. **Does the user's `pixelbuffer.to_uint8` path do something exotic?**
   I assumed it's a straightforward `clip · pow(1/gamma) · 255 · round`
   but never read it directly. Could there be a saturation pull (mentioned
   in CLAUDE.md as "saturation pull → brightness gain") that quantises the
   colours in some unexpected way?

3. **The mixer's master output stage.** CLAUDE.md mentions the engine does
   "master output stage (saturation pull → brightness gain)" before
   `to_uint8`. Have not inspected that code. A non-linear saturation pull
   could in principle posterise colors near saturation edges.

4. **Does the simulator photo actually show what's on screen, or is it phone
   camera JPEG posterisation?** The first photo had 9 visible blocks; the
   later photos (after the simulator-side fixes) show finer structure but the
   user still reports "jumps". Worth asking the user for a screenshot
   (lossless PNG, not a phone photo) so we know we're looking at the
   browser's actual output, not the camera's reconstruction of it.

5. **Are we actually rendering at the resolution we think?** With
   `devicePixelRatio` ≥ 2 (Retina), `canvas.width` is `cssWidth * dpr` and
   `setTransform(dpr,0,0,dpr,0,0)` is applied so drawing happens in CSS
   pixels. But `Math.round` in CSS pixels still maps to half-actual-pixels on
   Retina, which could re-introduce sub-pixel AA. Worth checking what
   integer rendering looks like at integer DEVICE pixels (i.e., divide by
   dpr in `reproject`).

6. **Is the user's issue actually a perceptual one with the cosine wave?**
   Cosine + rainbow on a 1800-LED install with wavelength=1 produces 4
   sweeps through red→cyan→red, with 26-LED plateaus near each peak/trough
   and steep colour gradients between. The eye can perceive a smooth
   gradient as "stepping" if the surrounding plateaus dominate the visual
   field. *Easy test: ask the user to load a preset that uses
   `shape: sawtooth` (the new default) with the rainbow palette and see if
   the artefact still appears.*

7. **The audio envelope multiplies palette output by a per-frame `bright`
   value** in `_CompiledPaletteLookup`. With the `color_waves.yaml`
   envelope (`floor: 0.65, ceiling: 1.0`), `bright` is in `[0.65, 1.0]`.
   Multiplying smooth RGB by a per-frame scalar should not introduce
   spatial banding. But it's worth verifying that `bright` is not somehow
   per-LED-quantised when the audio source is silent (input near 0).

8. **HiDPI integer alignment.** I treated CSS pixel coords as the integer
   grid. On a 2x display the `fillRect(int, int, int, int)` lands on
   even device-pixel boundaries — which is fine — but if the user's
   browser zoom is non-integer (e.g., 110%) the device-pixel mapping is
   fractional again and AA returns. Worth asking the user about browser
   zoom level.

---

## How to reproduce

1. `uv venv --python 3.11 && uv pip install -e ".[dev]"`
2. `.venv/bin/ledctl run --config config/config.dev.yaml`
3. Open `http://127.0.0.1:8000/`
4. POST `http://127.0.0.1:8000/presets/color_waves` (or load via the UI).
5. Observe the LED viz at the top of the page.

To verify the rendered data is smooth (sanity check that the bug is not in
the engine):

```python
import numpy as np, yaml
from ledctl.config import load_config
from ledctl.topology import Topology
from ledctl.surface import compile_layers, LayerSpec
from ledctl.masters import MasterControls, RenderContext
from ledctl.audio.state import AudioState

cfg = load_config('config/config.dev.yaml')
topo = Topology.from_config(cfg)
preset = yaml.safe_load(open('config/presets/color_waves.yaml'))
layers = compile_layers([LayerSpec(**l) for l in preset['layers']], topo)
audio = AudioState(); audio.low_norm = 1.0
ctx = RenderContext(t=2.0, wall_t=2.0, masters=MasterControls(), audio=audio)
out = layers[0].node.render(ctx)
u8 = np.clip(np.power(out, 1/2.2) * 255 + 0.5, 0, 255).astype(np.uint8)
# Inspect any range to confirm smoothness:
for i in range(165, 230, 5): print(i, tuple(u8[i]))
```

The float and uint8 outputs are smooth across the full strip; if the user
sees jumps in the simulator with this preset, the bug is downstream of
`palette_lookup.render`.
