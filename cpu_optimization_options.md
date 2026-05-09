# CPU optimization options (Pi 4, 60 fps × 1800 LEDs)

Scouted from the render hot path. Line numbers below are from a pre-refactor
snapshot of `surface.py` — treat them as hints, not literal locations. The
underlying patterns (per-frame allocations, redundant work that could move to
compile time) should still be visible after the primitive refactor; re-grep
before editing.

## Big wins

### 1. Gamma LUT instead of `np.power` per frame
**File:** `pixelbuffer.py` (`to_uint8`, ~lines 27–36)
**Issue:** `np.power(scratch, gamma, out=scratch)` raises 5400 floats to ~2.2
every frame — one of the heaviest single ops in the loop on ARM.
**Fix:** Reorder to clip → ×255 → cast to uint8 → index into a 256-entry uint8
LUT. Build the LUT once at construction:
`((np.arange(256) / 255.0) ** gamma * 255 + 0.5).astype(np.uint8)`.
Typically halves `to_uint8`. Zero risk, ~10 lines.

### 2. Engine wait loop allocates two `asyncio.Task`s per inner iteration
**File:** `engine.py` (~lines 371–379, the `wait_kick` / `wait_stop` block)
**Issue:** At 60 fps with audio kicks you create + cancel two tasks every
period — measurable async/GC overhead.
**Fix:** Either `await asyncio.wait_for(kick.wait(), timeout=remaining)` inside
`try/except TimeoutError` and poll `_stop` once after, or keep a single
long-lived `wait_stop` task reused across iterations.

### 3. `noise2d` per-frame allocations
**File:** `surface.py` — find the noise2d primitive's `render` (post-refactor
it may live in a separate file or class; grep for `noise2d` / perlin code).
**Issue:** Per-octave `.astype(np.int32)` / `.astype(np.float32)` and several
intermediate arrays per frame. This is usually the most expensive primitive.
**Fix:** Preallocate index + scratch buffers at compile time; use `out=` on
`np.floor`, `np.mod`, `np.add`, `np.multiply`. Can shave several ms/frame
when noise layers are active.

### 4. Pre-bake constants at compile time across primitives
**File:** `surface.py` (multiple primitives — re-locate after refactor)
- **Wave:** store `_u_axis_norm = u_axis / wavelength` at compile so the per-
  frame call is just `sin(_u_axis_norm * TAU - phase)`. Saves a 1800-element
  division each frame.
- **PaletteLookup / Trail:** when the scalar input is a `scalar_t` (one float
  per frame), they currently do `np.full((N,), v)` every frame. Allocate the
  `(N,)` scratch once at compile and use `.fill(v)`.
- **Topology-derived arrays:** drop `copy=True` on arrays that are never
  mutated by the render — let numpy reuse the existing buffer.

After the refactor these patterns may have moved or already been fixed —
search for `np.full((`, `astype(`, and `copy=True` inside primitive `render`
methods to spot remaining instances.

## Smaller wins

### 5. SimulatorTransport keeps doing work when nobody's looking
**File:** `transports/simulator.py`
**Issue:** Early-return on empty clients is in place, but every connected
browser tab still gets a 5.4 KB `bytes` per frame.
**Fix:** Use the existing pause button when the UI isn't needed. Optionally,
send simulator frames at half the render rate (every other frame) — the viz
doesn't need 60 Hz.

### 6. `Sparkles`: redundant `np.clip`
**File:** `surface.py` — sparkles primitive
**Issue:** `np.clip(palette_idx, 0, 1)` after a modulo that already bounds
the value. Drop the clip (verify after refactor).

### 7. Crossfade renders both stacks
**File:** `mixer.py` (`render`, crossfade branch)
**Note:** Doubles per-frame work but only while a fade is active. Not worth
optimizing — listed for completeness.

## Suggested order

1. **Gamma LUT (#1)** — biggest single win, mechanical, zero risk. Do this first.
2. **Compile-time pre-baking (#4)** — low risk, but coordinate with the
   surface refactor so we're not editing primitives twice. Best done *after*
   the refactor lands, against the new primitive structure.
3. **Noise2d (#3)** — only if noise-heavy presets are common in actual use.
4. **Engine wait loop (#2)** — do this if `asyncio` shows up in a profile;
   otherwise lower priority.

## Profiling note

Before/after each change, sample real CPU on the Pi with `py-spy top --pid
$(pgrep -f ledctl)` for ~30 s under a representative preset. The render
loop's `fps` and `dropped_frames` counters in `/state` are the cheapest
end-to-end signal that a change actually helped.
