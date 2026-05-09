# Surface v2 — Phased Implementation Plan (v1 + v1.1)

> Companion to `surface_v2_design_plan.md`. Same architecture, sliced for shipping. **v1 = the smallest fully-working system the user can demo on the rig.** **v1.1 = the hardening + polish that turns the demo into a festival-grade tool.** v1.1 lands immediately after v1; nothing here gets shelved indefinitely.
>
> When v1 and v1.1 disagree with the master design doc, this doc wins (it's the deltas / cuts / tweaks I'd push for).

---

## 0. Guiding cuts

The headline demo path the user actually wants to see working:

1. Operator opens UI → switches to **Design mode**.
2. Types "two red comets at the top, blue at the bottom, brightness pulsing on the kick" into chat.
3. ~5 s later, simulator viz shows the new effect; LEDs keep showing the previous live effect.
4. Operator drags the colour pickers + speed slider — sees changes instantly in the sim.
5. Clicks **Promote to live** → LEDs crossfade to the new look.
6. Switches to **Live mode** for the rest of the set.

Anything that isn't on that path is a v1.1 candidate. Anything that *prevents* that path from working — even occasionally — is v1.

The cuts below come from one rule: **the v1 system can be visibly less polished, but it cannot break the show.** Reliability stays in v1; observability/auto-recovery moves to v1.1.

---

## 1. v1 — the working demo (target: 2–3 dev days)

This is everything required for the §0 path to work end-to-end on the Pi, with one operator, one rig, no audience-visible failures.

### 1.1 Runtime + sandbox  (Phase 1 of master doc)

**Keep all of this in v1**:
- `Effect` base class with `init(ctx)` + `render(ctx)` contract.
- `EffectInitContext`, `EffectFrameContext`, `FrameMap`, `AudioView`, `ParamView`, read-only `MastersView`.
- `helpers.py` full §5.1 surface: `np`, `hex_to_rgb`, `hsv_to_rgb`, `lerp`, `clip01`, `gauss`, `pulse`, `tri`, `wrap_dist`, `palette_lerp`, `named_palette`, `rng`, `log`, `PI`, `TAU`, `LUT_SIZE`.
- `sandbox.py`: AST scan rejecting `import` / `ImportFrom`, restricted builtins, source-size cap (8 KB), `compile_effect()` extracting the single `Effect` subclass.
- One-time `init` precompute, no per-frame allocation rule.
- Buffer copy from effect's returned `out` into runtime-owned `master_buf` before masters apply (avoids the "why does my effect drift" class of bugs — small cost, big sanity win).

**Tweaks vs. master doc**:
- **Skip dunder-attribute AST rejection in v1.** Just block imports and cap source size. Dunder access is a tertiary attack surface and our threat model is "LLM typo," not "evade sandbox." Adds noise to error messages without a real benefit at festival scale. → v1.1.
- **`ParamView` writes:** soft no-op + `log.warning` in v1 (instead of `TypeError`). Strict-raise in v1.1. The point is to keep a sloppy LLM-emitted `ctx.params.x = 0.5` from crashing render in front of the dance floor while we're still tuning the system prompt. (Can flip to strict once the prompt is reliable — set behind a config flag from day one to make the flip a one-line change.)

### 1.2 Engine integration + transport split  (Phase 1 of master doc)

**Keep all of this in v1**:
- Destructive cut: delete v1 `surface/` package, `mixer.py`, `agent/tool.py`, `agent/system_prompt.py`, `presets.py`, related tests. Branch `surface-v2-rewrite`.
- Move `frames.py` into `surface/frames.py` content-unchanged.
- `transports/split.py` replacing `multi.py`: separate sim/led frame paths, same bytes when identical.
- New `Runtime` owning: live slot + preview slot + mode + crossfade + ParamStore + master output stage (saturation pull → adaptive brightness → clip, lifted from old mixer).
- Engine boots → loads `pulse_mono` into both slots → renders.
- Calibration overrides hooked into `Runtime.encode` (parity with v1 mixer).
- `transport/pause` API still works (only blocks led leg).

### 1.3 Two-mode operator UI  (Phase 5 of master doc)

**This is the headline differentiator and must ship in v1.** Without it the user can't experiment in front of the crowd, which is the whole point.

- Mode toggle (Design / Live) in the top bar.
- Design mode: chat panel + dynamic param panel for the *preview* effect + masters + "Promote to live" button.
- Live mode: dynamic param panel for the *live* effect + masters + "Switch to design".
- Single simulator canvas: shows preview in design mode, live in live mode.
- Simulator + WebSocket frame stream re-used verbatim.
- Mode default on boot = **Live** (a half-finished preview never reaches LEDs after a kicked Pi).

**Cuts to v1.1**:
- **"Pull live to preview" button.** Convenience; not on the demo path.
- **Mode persistence to localStorage.** v1 always boots into Live; that's the right default anyway.
- **Library / saved-effects rail in the UI.** v1 ships the REST endpoints for save/load (§1.6), but the operator drives saves from a single button + uses chat history for recall. The dedicated library UI lands in v1.1.
- **Live-code "View source" disclosure on chat messages.** Useful for debugging, distracting on stage. Add behind a "developer mode" flag in v1.1.
- **Perf badges / render p95 surface** — depends on watchdog (v1.1).

### 1.4 Param schema + dynamic controls  (Phase 5 of master doc)

**Keep all of this in v1** — this is what makes "tune by feel" work:
- Six control types: `slider`, `int_slider`, `color`, `select`, `toggle`, `palette`.
- Pydantic validation of incoming schema, structured error to LLM on bad shape.
- Live update path: slider drag → `PATCH /preview/params` or `/live/params` → `ParamStore.update` → next render sees new value, no recompile.
- Soft cap: ≤8 params per effect (enforced server-side, structured error if exceeded).
- Bounds-clamping on incoming patches (server side) — UI doesn't have to be the source of truth.

**Cuts to v1.1**:
- **Auto-merge param values across regenerations (§6.1).** Defer. The system prompt already shows the current effect's `param_values` to the LLM, so the LLM can preserve tweaks by carrying them as new defaults — verbally. That's good enough for demo and tests our prompt's ability to follow instructions. v1.1 adds the deterministic key-match auto-merge as a safety net + a token saver.

### 1.5 The agent (write_effect)  (Phase 4 of master doc)

**Keep all of this in v1**:
- Single tool: `write_effect` with `{name, summary, code, params}`.
- System prompt assembly per `surface/prompt.py`: PHYSICAL RIG, COORDINATE FRAMES, AUDIO INPUT, EFFECT CONTRACT, RUNTIME API, PARAM SCHEMA, PERFORMANCE RULES, EXAMPLE EFFECTS, ANTI-PATTERNS, CURRENT EFFECTS, LAST EFFECT ERROR (when present), TOOL.
- Server flow: schema validate → AST + sandbox compile → `init(ctx)` cap at 200 ms → fence-test → save → swap into preview (hard cut).
- Errors surfaced back to the LLM under `LAST EFFECT ERROR`.

**Tweaks vs. master doc**:
- **Fence test: 10 synthetic frames in v1, not 30.** Keeps `write_effect` round-trip snappy (~0.16 s instead of ~0.5 s) and still catches the bugs that matter for demo (NaN drift, off-by-one, wrong shape). Bump to 30 in v1.1 once we know what classes of bugs slip through 10.
- **No automatic LLM retries in v1.** Surface the error to the operator + to the LLM context for the *next manual turn*. Auto-retry adds a chunk of session-state machinery (rate-limit interactions, message-buffer healing on partial failures) and a user-visible "the system is doing 3 things" loading state that'll be confusing during the first week of use. v1.1 turns on `auto_retry: 2` once the failure modes are understood.

### 1.6 Persistence  (Phase 3 of master doc)

**Keep all of this in v1**:
- `config/effects/<slug>/effect.py` (real `.py` file) + `effect.yaml` (metadata + schema + values).
- REST endpoints: `GET /effects`, `POST /effects/{name}/save`, `POST /effects/{name}/load_preview`, `POST /effects/{name}/load_live`, `DELETE /effects/{name}`, `GET /active`, `PATCH /preview/params`, `PATCH /live/params`, `POST /promote`, `POST /mode`.
- Boot: re-instantiate live + preview from disk; on compile failure, fall back to `pulse_mono` for that slot and surface a warning.
- Persisted across restart: `mode`, both slot names, both slot `param_values`.

**Cuts to v1.1**:
- **`POST /pull_live_to_preview` endpoint.** Pairs with the UI button cut above.
- **Hot-reload-from-disk via filesystem watcher.** Already deferred in master doc Q #4. Stays deferred.

### 1.7 Example effects  (Phase 2 of master doc)

**v1 ships TWO examples**, not four:
- `pulse_mono` — simplest possible, also the safe-idle and boot default.
- `twin_comets_with_sparkles` — the §3.2 reference, which is also the user's flagship "north star" prompt. If our system can run this one effect cleanly, it can run anything the demo will throw at it.

**v1.1 adds**:
- `audio_radial` — palette-mapped `frames.radius` scrolled by `t`. Reference for "audio-driven scalar field" patterns.
- `palette_wash_with_kick_sparkles` — multi-component effect in one file. Reference for "X plus Y" prompts.

Rationale: each example is ~50 lines of hand-written reference code that lives in the system prompt verbatim. Two examples cover the prompt patterns that exist on the demo path (one trivial; one stateful particle + audio + side masks). The other two show patterns we'll need eventually but don't need on Day 1, and they cost prompt tokens until we do.

### 1.8 Crossfade  (Phase 1 of master doc)

**Keep in v1**:
- Crossfade on live promote only. Preview is hard-cut. Alpha uses `wall_t` (freeze/speed don't slow the crossfade). Duration = master crossfade slider.
- Implementation per §12 of master doc.

### 1.9 Error handling — minimum-viable safety  (Phase 6-ish of master doc)

**v1 ships the safety floor**:
- `write_effect` errors (compile, AST, fence-test) → structured tool result, preview unchanged.
- Live `render` raises → catch, log traceback, blank that frame (write zeros into the master buffer for that slot).
- After **3 consecutive frame failures** on a slot → swap that slot to `pulse_mono` and surface the error in the chat for the next turn.
- Shape/dtype validation on first render after a swap (one-time, ~50 ns/frame after that).

**Cuts to v1.1**:
- **Render budget watchdog with p95/p99 tracking + 0.5 s trip window.** Defer. The 3-consecutive-failures rule covers the *crash* case; the budget watchdog covers the *slow* case. Slow effects on the Pi degrade to dropped frames at the engine level (the existing "frames drop rather than spiral on lag" behaviour), which is visible but not catastrophic. We get the performance signal from operator complaints during testing and from a manual `GET /perf` endpoint (cheap).
- **Soft `dt` clamping** (master doc §20.1). Defer.
- **Watchdog UI badge** ("render p95 = 14 ms — slow"). Pairs with the watchdog itself → v1.1.

### 1.10 Audio integration

**No work needed in v1.** The audio bridge (`audio/state.py`, `audio/bridge.py`, `AudioServerSupervisor`) stays as-is — it's already written and tested in the v1 codebase, and v2 just consumes the same `AudioState` via the new `AudioView` adapter. This is genuinely free.

### 1.11 Tests

**v1 minimum**:
- `test_sandbox.py`: imports rejected, restricted builtins enforced, source-size cap, normal numpy code accepted.
- `test_runtime.py`: live + preview slot rendering, hard preview swap, live-promote crossfade, master output stage parity with old mixer.
- `test_examples.py`: load each shipped example, instantiate against synthetic 1800-LED topology, render 60 frames with synthetic audio, assert no exceptions + bounded RGB. Skip wall-time assertions in CI; only assert on Pi runs.
- `test_persistence.py`: round-trip an effect through save → load → render.

**v1.1 adds**:
- `test_prompt.py` — sanity checks on assembled system prompt structure.
- Watchdog tests (once watchdog exists).
- Param auto-merge tests.

---

## 2. v1.1 — hardening + polish (target: 2–3 more dev days, immediately after)

Everything below assumes v1 is deployed and being used. Each item is independent — pick whichever pain points hit hardest first.

### 2.1 Reliability / observability

- **Render budget watchdog** (§4.4 of master doc): per-effect p50/p95/p99 over 1 s rolling window. Trip on p95 > 5 ms for 0.5 s straight → swap to `pulse_mono` + post perf report to chat.
- **Strict `ParamView` write-raise.** Flip the config flag from "warn + ignore" to "raise TypeError." Now that the prompt is dialled in, silent failures are the bigger evil.
- **Stricter sandbox**: AST reject of dunder access (`__class__`, `__globals__`, etc.). Catches a class of LLM mistakes earlier.
- **`dt` clamping** at 2× target frame interval. Prevents stateful effects from teleporting after a hiccup.
- **Soft `init` budget enforcement.** Reject `init` taking >200 ms (master doc §9.1 already specifies; v1.1 adds the timing wrapper + structured error).
- **30-frame fence test** with a synthetic audio impulse train. Catches NaN drift / sparkle pool overflow / off-by-one bugs that 10 frames can miss.
- **Auto-retry on `write_effect` failure**: 2 consecutive retries before surfacing to the operator. Configurable in `config.yaml`.

### 2.2 Authoring ergonomics

- **Param auto-merge across regenerations** (§6.1 of master doc). Mechanical key-match merge for matching param keys whose previous value fits the new bounds. Drops a recurring burden on the prompt + saves tokens. The system prompt gets a one-paragraph explainer ("if you reuse a key, the operator's current value carries forward").
- **Two more example effects**: `audio_radial`, `palette_wash_with_kick_sparkles`. Now the LLM has reference templates for the multi-component "X plus Y" pattern and the audio-mapped scalar field pattern.
- **Library UI rail**: list of saved effects, click to load into preview, ⋆ to mark favourites.
- **"Pull live to preview" button + endpoint.** Now the operator can iterate on what's actually playing.
- **"View source" disclosure on each effect** (operator-facing). Useful for the operator to learn what the LLM emitted.

### 2.3 Operator UX

- **Mode persistence in `localStorage`.** Reload → returns to last mode (still Live on a fresh Pi boot).
- **Perf badge in the param panel header**: `render p95 = 1.4 ms · audio low=0.7`. Lives with the watchdog.
- **Slot indicators** in the simulator chrome (`PREVIEW · audio_radial` vs `LIVE · twin_comets`) so the operator never has to guess what they're looking at.

### 2.4 Engine

- **Design-mode preview at half-rate** (master doc §20.1). Drops the worst-case 3-render frame to 2.5 renders averaged. Only matters under live crossfade in design mode, but it's a free 30% headroom on the Pi during the trickiest scenario.

---

## 3. Explicitly NOT in v1 or v1.1 (future / v2)

Carried forward from master doc §20. Calling them out here so we don't re-litigate during implementation:

- Mobile / tablet operator UI (separate phone-friendly Live mode).
- Hands-free / MIDI / OSC param control.
- "Surprise me" / generative riffs (`user_design_spec.md` §10).
- Auto-play mode chaining queued effects with loop counts (`user_design_spec.md` §11). **Worth flagging:** this is in the user spec but not in the v2 design doc at all. It's a queue + scheduler on top of the Runtime, not a Runtime feature, so it's a separate slice when we get to it.
- `update_params` second tool for LLM-driven "warmer, slower" tweaks without rewriting code.
- Sub-effects / effect-calls-effect.
- Hot-reload-from-disk filesystem watcher.
- v1 preset migration tool (translate old layer-stack YAML → new Effect).
- Stronger sandbox (subinterpreter / RestrictedPython) for untrusted-input scenarios.
- Effect state-snapshot on watchdog crash for post-mortem.

---

## 4. Suggested tweaks vs. the master plan

Collected in one place so they're easy to push back on:

1. **Fence test = 10 frames in v1, 30 in v1.1.** Faster `write_effect` round-trip; bump up once we know what slips through.
2. **Two example effects in v1, four in v1.1.** Halves prompt-token cost on day one; the two we ship cover the demo path completely.
3. **Soft `ParamView` writes in v1, strict raise in v1.1.** Behind a config flag from day one — flip later.
4. **Skip dunder AST reject in v1.** Drop it back in once the system prompt is dialled in.
5. **No automatic LLM retries in v1.** Surface errors and let the operator drive the next turn; auto-retry in v1.1.
6. **Param auto-merge → v1.1.** The system prompt + LLM follow-instructions covers it for demo.
7. **Drop "Pull live to preview" + library-rail UI from v1.** Convenience features that don't block the demo path.
8. **Mode default = Live on every boot in v1; localStorage persistence in v1.1.** A hot Pi never wakes up rendering a half-baked preview.
9. **Watchdog (with p95 + soft-degrade) → v1.1.** v1 covers crash failure modes (3-strikes); slow-effect failure mode lands a week later with the perf UI.
10. **Estimated time-to-demo: ~2.5 dev days for v1, +2 days for v1.1.** Down from the master plan's "4–5 dev days to a usable system" because we're shipping the unpolished version first.

---

## 5. Concrete v1 phasing (day-by-day)

### Day 1 — substrate
- [ ] Branch `surface-v2-rewrite`, destructive cut of v1 surface/mixer/agent-tool/presets.
- [ ] `surface/base.py` (Effect + contexts), `surface/frames.py` (moved), `surface/helpers.py`, `surface/palettes.py`.
- [ ] `surface/sandbox.py` (AST scan: imports + size cap; restricted builtins; `compile_effect`).
- [ ] `transports/split.py` replacing `multi.py`.
- [ ] `surface/runtime.py` shell: live + preview slots, mode field, crossfade state, ParamStore, master output stage lifted from old mixer.
- [ ] Engine wired to Runtime; renders an in-code `pulse_mono` placeholder into both slots.
- [ ] Server tree compiles; `ledctl run` boots; sim shows a flat colour. **Day 1 done.**

### Day 2 — examples + agent + persistence
- [ ] `examples/pulse_mono/` and `examples/twin_comets_with_sparkles/` (effect.py + effect.yaml each), wired as bundled defaults.
- [ ] `tests/test_examples.py` green for both.
- [ ] `surface/persistence.py` (load/save under `config/effects/<slug>/`).
- [ ] REST endpoints from §1.6.
- [ ] `surface/prompt.py` (build_system_prompt, all sections from §8 of master doc).
- [ ] `surface/tool.py` (`write_effect` handler: validate → compile → init → fence-test 10 frames → save → swap-into-preview).
- [ ] `api/agent.py` rewired. End-to-end: chat sends "twin comets …" → effect appears in preview slot. **Day 2 done.**

### Day 3 — UI + polish
- [ ] `index.html` rebuilt as dual-mode shell: mode toggle, sim canvas, dynamic param panel, masters row, chat (design only), Promote-to-live button.
- [ ] `lib/app.js`: render the six control types from the param schema; live PATCH on slider drag; mode toggle plumbing.
- [ ] On-rig test pass: load preview, promote, drag sliders, observe LEDs.
- [ ] Tighten the system prompt against any failure modes the LLM hits.
- [ ] Demo-able. **v1 done.**

(Days are notional. The plan is "this is the next thing to do," not "this fits in 8 hours." If something's hard, let it slip — v1.1 is still planned right after.)

---

## 6. Demo-day acceptance check

The build is v1-done when, on the rig:

- [ ] Boot → both slots render `pulse_mono`, mode = live.
- [ ] Operator types the user's flagship "twin comets with sparkle trails" prompt → preview renders within ~5 s, no LED disruption.
- [ ] Operator drags `lead_offset` and `leader_color` sliders → simulator preview updates in real time, no chat round-trip.
- [ ] Operator clicks "Promote to live" → LEDs crossfade to the new effect over the master crossfade duration.
- [ ] Operator switches to live mode → simulator now mirrors LEDs; chat panel collapses; sliders persist.
- [ ] Reboot Pi → live slot's effect + slider values come back identical; mode resets to live.
- [ ] Type a deliberately bad prompt that yields a `render` crash → after 3 consecutive failures, the slot swaps to `pulse_mono` and the error appears in chat. Crowd never sees a blackout.

If all seven hold, we ship v1, demo, then start v1.1 the next morning.
