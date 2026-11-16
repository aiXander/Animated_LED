# Render-loop performance

One asyncio thread runs the render loop, every HTTP handler and every WebSocket. A frame is
cheap: 0.1–0.3 ms on an M2 for one layer of 1800 LEDs, against a 16.7 ms budget at 60 fps.
So frames are not dropped by the effects. They are dropped when **something else holds the
event loop**, or when the Pi itself slows down (CPU governor, heat, under-voltage). The design
follows from that: the tick never waits on anything, slow work runs on worker threads, and
browsers are fed through mailboxes that drop stale data instead of applying back-pressure.

All timings here were measured on the Mac. Nothing has been measured on the Pi 4 yet: the first
thing to do on-site is read the `perf` block (see Runbook).

## Where the code lives

- `engine.py`: `_loop` (the tick, no `await` between deadline and send), `_sleep` / `_on_kick`
  (sleep on a Future that a timer or an audio onset resolves), `FrameStats` (rolling
  `render_ms`, `out_ms`, `late_ms`; logs when frames start more than 50 ms late).
- `fanout.py`: `LatestFanout`, one mailbox and one sender task per WebSocket client. Used by
  `transports/simulator.py` (preview frames) and by `_state_broadcaster` in `api/server.py`.
- `transports/split.py`: `send_now`, DDP first, then the preview hand-off.
- `surface/runtime.py`: `compile_layer` (worker thread) / `place_layer` (event loop) /
  `install_layer_async`; `_frame_ctx`, `RenderStats`, `_render_composition` for the per-frame path.
- `surface/persistence.py`: `DeferredValueSaver` (slider tweaks reach disk off the loop).
  `yamlio.py`: libyaml-backed load/dump.
- `osc.py`: `AllowlistOSCUDPServer`, the UDP receiver both listeners use.
- `hostinfo.py`: Pi temperature, clock, governor, throttle flags. Shown with `FrameStats` as the
  `perf` key of `/state`; `perfSummary` in `web/lib/util.js` renders it (fps pill tooltip on
  desktop, Output tab on mobile).

## Why it's built this way

- **What used to hold the loop** (Mac; expect several times more on the Pi): a browser on a
  congested link (`await ws.send_bytes` sat in the tick, ahead of the DDP send: unbounded), a
  slider PATCH (2.5 ms of YAML per request, ~20 requests/s while dragging), an effect load or
  playlist step (5–10 ms compile + fence test), opening the library (12 ms), a chat turn (5 ms
  of prompt building plus the compile).
- **Worker threads share the interpreter lock** with the render loop, so off-loop work still
  costs time, in slices. `cli.py` sets the switch interval to 1 ms (default 5 ms) so a busy
  worker cannot hold a frame back longer than that.
- **`/ws/state` is split into light and full pushes.** The compositions (`live`, `preview`:
  every layer's source and schema) are most of the payload and rarely change. They go out on
  connect, after any mutating HTTP request (`_MarkStateDirty`) and every 0.5 s; other ticks
  carry the rest. Both front-ends merge with `Object.assign`.
- **Rejected: the render loop on its own thread.** Every composition mutation would need a
  lock, and with the heavy work already off the loop the lock-free single thread is simpler.
- **Rejected: turning off WebSocket compression.** Measured: 16 µs to deflate a 5.4 KB frame,
  which shrinks smooth frames by up to 100×. It is worth its CPU on a tailnet link.
- **Not tried: real-time scheduling.** `deploy/ledctl.service` carries it commented out; it
  needs a check on the Pi that SSH stays responsive.
- **python-osc's server was replaced** because its per-packet path (socketserver object, regex
  match of the address against every mapping) ran ~400 times a second on a thread competing
  with the render loop. python-osc still parses bundles and string arguments.

## Invariants and traps

- **Nothing in the tick may wait on a peer.** Pinned by
  `tests/test_engine.py::test_a_stalled_browser_does_not_hold_up_led_frames`.
- **A request handler does no file I/O, YAML, compile or LLM call on the loop.** A plain `def`
  endpoint (FastAPI runs it on a thread) is only right when it touches no runtime state;
  otherwise `asyncio.to_thread` the pure part and mutate on the loop. No test catches a
  violation: it shows up as `late_ms` and a "render loop held up" line in the journal.
- **`compile_layer` must not touch a composition**: it runs while the loop renders.
- **An async install fixes its target layer before the compile, by `Layer.seat`**, because the
  operator can select, remove or reorder layers while it runs; a vanished target means the new
  layer is added. Pinned by
  `tests/test_runtime.py::test_async_install_replaces_the_layer_it_was_aimed_at`.
- **Both effect budgets are CPU time of the calling thread** (`time.thread_time`): the init
  budget and the per-frame render watchdog. Wall time also counts waits for the interpreter
  lock (the OSC listeners take it hundreds of times a second) and OS preemption, which
  rejected legal effects and switched cheap live layers off at random. Pinned by
  `tests/test_runtime.py::test_render_budget_counts_the_layers_own_cpu_not_lock_waits`.
- **Effect files are written through `write_atomic`** (`surface/persistence.py`): the library
  listing, playlist loads and the value saver read them from other threads.
- **A socket error must not end a loop that has no supervisor.** `DDPTransport.send_now` skips
  the frame (`send_errors` in `GET /transport`), pinned by
  `tests/test_ddp.py::test_a_dead_link_skips_the_frame_instead_of_raising`; the OSC receiver
  logs and keeps listening.
- **Light pushes leave out `live` and `preview`.** A client that replaces its state instead of
  merging loses the decks. Pinned by `tests/test_api.py::test_state_stream_sends_layers_only_when_needed`.
- **Slider values reach disk up to 10 s late** (one batched write per effect, so tuning does
  not hammer the Pi's SD card). Anything that rewrites an `effect.yaml` calls
  `value_saver.flush()` first, and whatever loads one (the two effect-load routes, each
  playlist step) calls `flush_async()` first, or an effect swapped out and back in comes back
  with its old values. Pinned for the routes by
  `tests/test_api.py::test_knob_tuning_survives_a_reload_and_a_restart`.
- **In-place numpy code must never write into an array it did not allocate.** Indexing a
  palette table with a scalar returns a view of the shared table. Pinned for `palette_lerp` by
  `tests/test_helpers.py::test_palette_lerp_on_a_named_palette_table`.
- **Slow numpy shapes on 1800 × 3 data:** broadcasting against the trailing axis of 3, and
  reductions over it, run 1800 loops of 3; work on a channel-major `(3, N)` block. `np.mod` and
  `np.power` on floats are several times slower than arithmetic (hence the floor-based wrap in
  `hsv_to_rgb` and the gamma lookup table). Measured on an M2 at N=1800: `hsv_to_rgb` ~70 µs
  against ~20 µs for a colour table read with `np.take`; `np.einsum('pn,pc->nc')` over 10
  particles ~100 µs against ~14 µs for `np.matmul(falloff.T, colours)`. `np.take(..., out=)`
  only writes straight into `out` with `mode="wrap"` or `"clip"`; the default mode buffers.
- **The effect clock starts at 0 and never runs backwards** (`ctx.dt >= 0`, `ctx.t` near 0 on
  the first tick). A negative dt turns every `exp(-dt / tau)` envelope into NaN for good.
  Pinned by `tests/test_engine.py::test_effect_clock_starts_at_zero_and_never_runs_backwards`.
- **`ctx.t` in float32 array maths goes coarse as it grows; nothing errors.** `positions -
  ctx.t * speed` loses its fraction bits: at t ≈ 800,000 s a 60 fps ring effect updated 13
  times a second, and a few days of uptime are enough to see it. An effect that must stay smooth
  for days keeps its own phase, `phase = (phase + ctx.dt * speed) % 1.0`, with one wrapped
  clock per wave term (a single clock scaled by 0.7 jumps each time it wraps). The library
  effects in `surface/examples/` do this.
- **A tight-loop benchmark understates the live cost.** The real loop sleeps between frames,
  the CPU clocks down, and the same effect measured 2–3× slower in the running server.
- **`late_ms` on macOS is not evidence**: bare event-loop timers there fire 1–2 ms late and
  occasionally 10+ ms. On Linux it is.

## Runbook: the LEDs lag

1. Read `perf`: hover the fps pill (desktop), Output tab (mobile), or
   `curl -s localhost:8000/state | jq .perf`.
2. **`frame` near the frame period** (16.7 ms at 60 fps): the Pi is slow or the effect heavy.
   Check `host`: `governor` should be `performance` (install `deploy/cpu-performance.service`),
   `throttled` / `undervoltage` false, `cpu_mhz` at the Pi's full clock. Otherwise lower the LED
   engine fps or simplify the effect (per-layer p95 is next to the layer name).
3. **`held up` high, or "render loop held up" in `journalctl -u ledctl`**: code is running on
   the event loop that should not be. Look for sync work in whatever the operator did then.
4. **Both fine, LEDs still stutter**: the problem is after the Pi. Follow the DDP debug recipe
   in `CLAUDE.md`; WLED's own refresh is the ceiling (450 WS2815 pixels take 13.5 ms per output).
5. **Preview choppy, LEDs fine**: the link to the browser. Lower the UI viz fps.
