// LED simulator canvas. Owns:
//   - the canvas projection / per-LED integer rect grid
//   - the /ws/frames binary feed (one frame = uint8 RGB triples)
//   - the /topology fetch
//   - hover-tooltip + tap-to-solo (calibration POSTs)
//   - the connection pill text + the engine-status row text
// Everything DOM-shaped is passed in by the bootstrap so the same code
// drives both desktop and (smaller, no-tooltip) mobile. The tooltip node
// is optional: pass `tooltip: null` to disable hover identification.

import { loadTopology } from "./state.js";
import { setText } from "./util.js";

// Two views, chosen from the rig's bounding box (metres, from /topology):
//   - "lotus": the hub rig as a schematic, not a map. The hub sits at the
//     bottom centre and the legs split between the left and the right; on
//     each side the first leg lies flat and the others fan upward, evenly up
//     to a max angle the operator sets with a slider over the sim (kept per
//     browser). Each LED keeps its real distance from the hub. A leg goes to
//     the side its real direction points to (a leg straight up or down goes
//     to the emptier side), and the leg nearest horizontal lies flat. Wide
//     and low, so it costs far less height than the rig drawn to scale.
//   - "strip": a layout longer than STRIP_LIKE_ASPECT times its width (the
//     old rectangle of two 30 m rows is 30:1). Drawn to scale it would be a
//     hairline, so x and y stretch independently to fill the canvas and each
//     row is a thick band. The sim is a HUD there, not a map.
// The view is published as <html data-rig-layout="lotus|strip">, the lotus's
// width:height as the CSS variable --rig-aspect; each page's CSS sizes its
// canvas box from them.
const STRIP_LIKE_ASPECT = 6;
// Strip view: band thickness as a fraction of each row's share of the canvas.
const STRIP_THICKNESS_FRAC = 0.53;
// Lotus view: leg thickness as a fraction of the canvas's short side, clamped
// (CSS px). The legs are 1D lines; anything fat reads as a band with width.
const LOTUS_THICKNESS_FRAC = 0.012;
const LOTUS_THICKNESS_MIN = 3;
const LOTUS_THICKNESS_MAX = 7;
const HOVER_DOT = 8;
// Side of the ring drawn round a calibration-lit LED (CSS px). In the lotus one
// LED is about one device px long, too small to find without it.
const CAL_RING = 12;
// Breathing gap at the canvas edges, as a fraction of the canvas extent.
const HORIZONTAL_MARGIN_FRAC = 0.05;
const VERTICAL_MARGIN_FRAC = 0.05;
// Lotus view: the fan's max angle above horizontal (degrees), its default and
// where the operator's choice is kept.
const LOTUS_MAX_DEG = 90;
const LOTUS_DEFAULT_DEG = 35;
const LOTUS_STORAGE_KEY = "ledctl.lotusMaxDeg";

export function bindViz({
  root,           // outer #viz section (used for ResizeObserver)
  canvas,
  tooltip = null, // optional — set null on touch devices
  calBanner = null,
  calText = null,
  calClear = null,
  wsPill = null,  // optional connection pill
  statusEngine = null, // optional engine-status row in the side panel
  pixelCountEl = null, // optional "leds" stat
}) {
  const ctx = canvas.getContext("2d", { alpha: false });

  let leds = [];
  let strips = [];
  let ledRects = null;
  let stripLike = false;
  // Where each LED is drawn, before scaling to the canvas: real x/y for the
  // strip view, the fanned-out lotus otherwise. [x0, y0, x1, y1, ...].
  let layoutPos = null;
  let layoutMin = [0, 0];
  let layoutMax = [1, 1];
  let lotusDeg = LOTUS_DEFAULT_DEG;
  try {
    const saved = Number(localStorage.getItem(LOTUS_STORAGE_KEY));
    if (saved > 0 && saved <= LOTUS_MAX_DEG) lotusDeg = saved;
  } catch (_) { /* storage blocked: keep the default */ }
  let lastFrame = null;
  let projected = null;
  let mouseX = -1, mouseY = -1;
  let mouseAbsX = 0, mouseAbsY = 0;
  let mySolo = null;
  let calLit = null;   // global indices lit by calibration, ringed in draw()
  let canvasW = 1, canvasH = 1;
  let canvasDpr = 1;

  function setStatus(txt, cls) {
    if (wsPill) { wsPill.textContent = txt; wsPill.className = cls; }
    if (statusEngine) { statusEngine.textContent = txt; statusEngine.className = cls; }
  }

  // The backing store must hold exactly the device pixels the canvas covers.
  // One pixel off and the browser rescales the whole bitmap, which blends
  // neighbouring LEDs however carefully the rects were rounded. The CSS box
  // is rarely a whole number of device px (vh heights, the lotus
  // aspect-ratio box), so take the browser's own count where it reports one.
  let devBox = null;   // [w, h] in device px, from the ResizeObserver below
  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    canvasDpr = dpr;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, devBox ? devBox[0] : Math.round(rect.width * dpr));
    canvas.height = Math.max(1, devBox ? devBox[1] : Math.round(rect.height * dpr));
    canvasW = canvas.width / dpr;
    canvasH = canvas.height / dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    reproject();
  }

  let reprojectScheduled = false;
  function requestReproject() {
    if (reprojectScheduled) return;
    reprojectScheduled = true;
    requestAnimationFrame(() => { reprojectScheduled = false; resizeCanvas(); });
  }

  // Fill layoutPos for the current view. Strip view: the real x/y. Lotus:
  // see the header comment.
  function buildLayout() {
    layoutPos = new Float32Array(leds.length * 2);
    if (stripLike) {
      leds.forEach((led, i) => {
        layoutPos[i * 2] = led.position[0];
        layoutPos[i * 2 + 1] = led.position[1];
      });
    } else {
      const sides = { left: [], right: [] };
      const vertical = [];
      for (const s of strips) {
        const dx = s.end[0] - s.start[0], dy = s.end[1] - s.start[1];
        const len = Math.hypot(dx, dy) || 1;
        const leg = { strip: s, flatness: Math.abs(dy) / len };
        if (dx / len < -0.01) sides.left.push(leg);
        else if (dx / len > 0.01) sides.right.push(leg);
        else vertical.push(leg);
      }
      for (const leg of vertical) {
        (sides.left.length < sides.right.length ? sides.left : sides.right).push(leg);
      }
      const maxRad = lotusDeg * Math.PI / 180;
      for (const [side, legs] of Object.entries(sides)) {
        legs.sort((a, b) => a.flatness - b.flatness);
        const sign = side === "left" ? -1 : 1;
        legs.forEach((leg, j) => {
          const a = legs.length > 1 ? maxRad * j / (legs.length - 1) : 0;
          const ux = sign * Math.cos(a), uy = Math.sin(a);
          const { pixel_offset: off, pixel_count: n } = leg.strip;
          for (let i = off; i < off + n; i++) {
            const r = Math.hypot(leds[i].position[0], leds[i].position[1]);
            layoutPos[i * 2] = ux * r;
            layoutPos[i * 2 + 1] = uy * r;
          }
        });
      }
    }
    layoutMin = [Infinity, Infinity];
    layoutMax = [-Infinity, -Infinity];
    if (!stripLike) { layoutMin = [0, 0]; layoutMax = [0, 0]; }  // keep the hub in view
    for (let i = 0; i < leds.length; i++) {
      for (let k = 0; k < 2; k++) {
        const v = layoutPos[i * 2 + k];
        if (v < layoutMin[k]) layoutMin[k] = v;
        if (v > layoutMax[k]) layoutMax[k] = v;
      }
    }
    if (!stripLike) {
      // Width:height of the lotus, padded like the canvas, clamped so a flat
      // fan still gets a usable band and a tall one stays on screen.
      const w = (layoutMax[0] - layoutMin[0]) * (1 + 2 * HORIZONTAL_MARGIN_FRAC);
      const h = (layoutMax[1] - layoutMin[1]) * (1 + 2 * VERTICAL_MARGIN_FRAC);
      const aspect = Math.min(6, Math.max(1.2, w / (h || 1)));
      document.documentElement.style.setProperty("--rig-aspect", aspect.toFixed(3));
    }
  }

  function reproject() {
    if (!leds.length || !layoutPos) { projected = null; return; }
    const dpr = canvasDpr;
    const dx = layoutMax[0] - layoutMin[0];
    const dy = layoutMax[1] - layoutMin[1];
    // Thickness first: it is reserved in the padding so the outermost LEDs
    // sit fully inside the canvas.
    let thickCss, padX, padY, sx, sy;
    if (stripLike) {
      // Bands run along the long axis; thickness comes from each band's share
      // of the short one, and only that axis needs room for it.
      const alongX = dx >= dy;
      const share = (alongX ? canvasH : canvasW) / _bandCount(alongX ? 1 : 0);
      thickCss = Math.max(2, Math.floor(share * STRIP_THICKNESS_FRAC));
      padX = canvasW * HORIZONTAL_MARGIN_FRAC + (alongX ? 0 : thickCss / 2);
      padY = canvasH * VERTICAL_MARGIN_FRAC + (alongX ? thickCss / 2 : 0);
      sx = Math.max(1, canvasW - 2 * padX) / (dx || 1);
      sy = Math.max(1, canvasH - 2 * padY) / (dy || 1);
    } else {
      thickCss = Math.min(LOTUS_THICKNESS_MAX, Math.max(LOTUS_THICKNESS_MIN,
        Math.round(Math.min(canvasW, canvasH) * LOTUS_THICKNESS_FRAC)));
      padX = canvasW * HORIZONTAL_MARGIN_FRAC + thickCss / 2;
      padY = canvasH * VERTICAL_MARGIN_FRAC + thickCss / 2;
      sx = sy = Math.min(
        Math.max(1, canvasW - 2 * padX) / (dx || 1),
        Math.max(1, canvasH - 2 * padY) / (dy || 1),
      );
    }
    const thickDev = Math.max(1, Math.round(thickCss * dpr));
    const cx = canvasW / 2;
    const cy = canvasH / 2;
    const ox = (layoutMin[0] + layoutMax[0]) / 2;
    const oy = (layoutMin[1] + layoutMax[1]) / 2;
    projected = new Float32Array(leds.length * 2);
    for (let i = 0; i < leds.length; i++) {
      projected[i * 2]     = cx + (layoutPos[i * 2] - ox) * sx;
      projected[i * 2 + 1] = cy - (layoutPos[i * 2 + 1] - oy) * sy;
    }
    ledRects = new Int32Array(leds.length * 4);
    strips.forEach((strip) => {
      const off = strip.pixel_offset;
      const n = strip.pixel_count;
      if (n === 0) return;
      _layStrip(
        off, n,
        projected[off * 2], projected[off * 2 + 1],
        projected[(off + n - 1) * 2], projected[(off + n - 1) * 2 + 1],
        thickDev, dpr,
      );
    });
  }

  // One strip from (ax, ay) to (bx, by) in CSS px, at any angle, as n
  // axis-aligned rects in DEVICE px. The strip is tiled along whichever
  // canvas axis it runs closer to: there each LED owns the slot between two
  // rounded bounds, so neighbours share an exact integer edge. Across that
  // axis each rect is centred on the strip's line and rounded on its own,
  // which gives a diagonal strip a staircase of whole pixels. Every edge is
  // an integer on purpose: a fractional edge is anti-aliased by the canvas
  // and blends neighbouring LEDs' colours
  // (LED_color_debug/BLOCKY_COLORS_INVESTIGATION.md). Do not draw a leg with
  // a rotated transform or a stroked line.
  function _layStrip(off, n, ax, ay, bx, by, thickDev, dpr) {
    const alongX = Math.abs(bx - ax) >= Math.abs(by - ay);
    const a0 = alongX ? ax : ay;            // along the tiling axis
    const c0 = alongX ? ay : ax;            // across it
    const dA = alongX ? bx - ax : by - ay;
    const dC = alongX ? by - ay : bx - ax;
    const stepA = n > 1 ? dA / (n - 1) : 1;
    const stepC = n > 1 ? dC / (n - 1) : 0;
    // A rect's extent across the tiling axis is longer than the strip is
    // thick by 1/cos(tilt); widen it so every leg looks equally thick.
    const tilt = dA !== 0 ? Math.hypot(dA, dC) / Math.abs(dA) : 1;
    const across = Math.max(1, Math.round(thickDev * tilt));
    const half = across >> 1;
    let prevBound = Math.round((a0 - stepA / 2) * dpr);
    for (let i = 0; i < n; i++) {
      const nextBound = Math.round((a0 + stepA * (i + 0.5)) * dpr);
      const lo = Math.min(prevBound, nextBound);
      // Under one device px per LED the slot rounds to 0: draw 1 px and let
      // the next LED paint over it.
      const size = Math.max(1, Math.abs(nextBound - prevBound));
      const cLo = Math.round((c0 + stepC * i) * dpr) - half;
      const idx = (off + i) * 4;
      ledRects[idx]     = alongX ? lo : cLo;
      ledRects[idx + 1] = alongX ? cLo : lo;
      ledRects[idx + 2] = alongX ? size : across;
      ledRects[idx + 3] = alongX ? across : size;
      prevBound = nextBound;
    }
  }

  // Strip view only: how many parallel bands the strips form across the
  // layout's short axis (`axis`: 0 = x, 1 = y). Sizes the band thickness so
  // the bands fill the canvas however many rows the rig has. Two strips
  // closer than 1 cm count as one band.
  function _bandCount(axis) {
    if (!strips.length) return 1;
    const mids = strips.map((s) => 0.5 * (s.start[axis] + s.end[axis]));
    mids.sort((a, b) => a - b);
    let bands = 1;
    for (let i = 1; i < mids.length; i++) {
      if (Math.abs(mids[i] - mids[i - 1]) > 0.01) bands += 1;
    }
    return bands;
  }

  // Nearest LED centre within 10 CSS px of (px, py), canvas-relative; -1 if none.
  function pickLED(px, py) {
    if (!projected || px < 0) return -1;
    const radius = 10, r2 = radius * radius;
    let best = -1, bestD = r2;
    for (let i = 0; i < leds.length; i++) {
      const dx = projected[i * 2] - px;
      const dy = projected[i * 2 + 1] - py;
      const d2 = dx * dx + dy * dy;
      if (d2 < bestD) { bestD = d2; best = i; }
    }
    return best;
  }

  function draw() {
    ctx.fillStyle = "#050505";
    ctx.fillRect(0, 0, canvasW, canvasH);
    if (ledRects && lastFrame && projected) {
      ctx.imageSmoothingEnabled = false;
      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = "source-over";
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      const n = leds.length;
      for (let i = 0; i < n; i++) {
        const src = i * 3;
        const idx = i * 4;
        ctx.fillStyle = `rgb(${lastFrame[src]},${lastFrame[src + 1]},${lastFrame[src + 2]})`;
        ctx.fillRect(ledRects[idx], ledRects[idx + 1], ledRects[idx + 2], ledRects[idx + 3]);
      }
      ctx.setTransform(canvasDpr, 0, 0, canvasDpr, 0, 0);
      if (calLit) {
        ctx.strokeStyle = "#fbbf24";
        ctx.lineWidth = 1;
        for (const gid of calLit) {
          if (gid < 0 || gid >= n) continue;
          ctx.strokeRect(
            projected[gid * 2] - CAL_RING / 2, projected[gid * 2 + 1] - CAL_RING / 2,
            CAL_RING, CAL_RING,
          );
        }
      }
      if (tooltip) {
        const hit = pickLED(mouseX, mouseY);
        if (hit >= 0) {
          const x = projected[hit * 2], y = projected[hit * 2 + 1];
          ctx.strokeStyle = "#fbbf24";
          ctx.lineWidth = 1;
          ctx.strokeRect(
            x - HOVER_DOT / 2 - 2, y - HOVER_DOT / 2 - 2,
            HOVER_DOT + 4, HOVER_DOT + 4,
          );
          const led = leds[hit];
          const cSrc = hit * 3;
          const cr = lastFrame[cSrc], cg = lastFrame[cSrc + 1], cb = lastFrame[cSrc + 2];
          const swatch = `display:inline-block;width:0.7em;height:0.7em;`
                       + `vertical-align:middle;margin-right:0.35em;border:1px solid #333;`
                       + `background:rgb(${cr},${cg},${cb})`;
          tooltip.style.display = "block";
          tooltip.style.left = (mouseAbsX + 12) + "px";
          tooltip.style.top = (mouseAbsY + 12) + "px";
          tooltip.innerHTML =
            `<div class="gid">global #${led.global_index}</div>` +
            `<div>${led.strip_id} · local ${led.local_index}</div>` +
            `<div class="dim">x=${led.position[0].toFixed(2)} y=${led.position[1].toFixed(2)}</div>` +
            `<div><span style="${swatch}"></span>rgb(${cr}, ${cg}, ${cb})</div>`;
        } else {
          tooltip.style.display = "none";
        }
      }
    }
    requestAnimationFrame(draw);
  }

  if (tooltip) {
    canvas.addEventListener("mousemove", (e) => {
      const r = canvas.getBoundingClientRect();
      mouseX = e.clientX - r.left;
      mouseY = e.clientY - r.top;
      mouseAbsX = e.clientX;
      mouseAbsY = e.clientY;
    });
    canvas.addEventListener("mouseleave", () => {
      mouseX = -1;
      tooltip.style.display = "none";
    });
  }

  // Tap / click → solo that LED via /calibration/solo. Picks from the
  // click's own position, never from the hover state: both pages run without
  // a tooltip, so nothing else would update it between taps.
  canvas.addEventListener("click", async (e) => {
    const r = canvas.getBoundingClientRect();
    const hit = pickLED(e.clientX - r.left, e.clientY - r.top);
    if (hit < 0) return;
    const gid = leds[hit].global_index;
    if (mySolo === gid) {
      await fetch("/calibration/stop", { method: "POST" });
      mySolo = null;
    } else {
      await fetch("/calibration/solo", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ indices: [gid] }),
      });
      mySolo = gid;
    }
  });

  if (calClear) {
    calClear.addEventListener("click", async () => {
      await fetch("/calibration/stop", { method: "POST" });
      mySolo = null;
    });
  }

  window.addEventListener("resize", requestReproject);
  if (window.ResizeObserver && root) {
    new ResizeObserver(requestReproject).observe(root);
  }
  if (window.ResizeObserver) {
    try {
      new ResizeObserver((entries) => {
        const box = entries[0].devicePixelContentBoxSize;
        if (!box || !box[0]) return;
        devBox = [box[0].inlineSize, box[0].blockSize];
        requestReproject();
      }).observe(canvas, { box: "device-pixel-content-box" });
    } catch (e) { /* Safari has no device-pixel-content-box: rounding stays */ }
  }

  // The lotus's max-angle slider, overlaid on the sim's bottom-left corner.
  // Built here so both front-ends get it; hidden in the strip view.
  const angleCtl = document.createElement("label");
  angleCtl.className = "lotus-angle";
  angleCtl.title = "Lotus view: how far the legs fan up from horizontal";
  angleCtl.style.cssText =
    "position:absolute;left:0.5rem;bottom:0.4rem;display:none;align-items:center;"
    + "gap:0.35rem;font-size:0.68rem;color:#9ca3af;background:rgba(12,12,12,0.7);"
    + "padding:0.15rem 0.45rem;border-radius:6px;z-index:2;";
  const angleInput = Object.assign(document.createElement("input"), {
    type: "range", min: "5", max: String(LOTUS_MAX_DEG), step: "5", value: String(lotusDeg),
  });
  angleInput.style.width = "5.5rem";
  const angleVal = document.createElement("span");
  angleVal.style.cssText = "min-width:2.2em;font-variant-numeric:tabular-nums;";
  angleVal.textContent = `${lotusDeg}°`;
  angleCtl.append("∠", angleInput, angleVal);
  (root || canvas.parentElement).appendChild(angleCtl);
  angleInput.addEventListener("input", () => {
    lotusDeg = Number(angleInput.value);
    angleVal.textContent = `${lotusDeg}°`;
    try { localStorage.setItem(LOTUS_STORAGE_KEY, String(lotusDeg)); } catch (_) {}
    buildLayout();
    reproject();
  });

  async function reloadTopology() {
    const topo = await loadTopology();
    leds = topo.leds || [];
    strips = topo.strips || [];
    const dx = topo.bbox_max[0] - topo.bbox_min[0];
    const dy = topo.bbox_max[1] - topo.bbox_min[1];
    stripLike = Math.max(dx, dy) > STRIP_LIKE_ASPECT * Math.min(dx, dy);
    buildLayout();
    // The page's CSS resizes the canvas box on these; the ResizeObserver then
    // reprojects into the new box.
    document.documentElement.dataset.rigLayout = stripLike ? "strip" : "lotus";
    angleCtl.style.display = stripLike ? "none" : "flex";
    if (pixelCountEl) pixelCountEl.textContent = topo.pixel_count;
    reproject();
  }

  async function start() {
    try {
      await reloadTopology();
    } catch (e) {
      setStatus("topology fetch failed", "bad");
      return;
    }
    resizeCanvas();

    const wsUrl = (location.protocol === "https:" ? "wss:" : "ws:")
                + "//" + location.host + "/ws/frames";
    // Reconnect on close: a ledctl restart (service restart on the Pi) must not
    // freeze the sim until the operator reloads the page.
    let backoffMs = 500;
    const connect = () => {
      const ws = new WebSocket(wsUrl);
      ws.binaryType = "arraybuffer";
      ws.onopen = () => { backoffMs = 500; setStatus("connected", "ok"); };
      ws.onclose = () => {
        setStatus("reconnecting", "bad");
        setTimeout(connect, backoffMs);
        backoffMs = Math.min(backoffMs * 2, 4000);
      };
      ws.onerror = () => setStatus("error", "bad");
      ws.onmessage = (ev) => {
        const buf = new Uint8Array(ev.data);
        // Pixel count drift → topology changed (e.g., layout edit). Refetch.
        if (buf.length !== leds.length * 3) { reloadTopology(); }
        lastFrame = buf;
      };
    };
    connect();
    requestAnimationFrame(draw);
  }

  // Drive the calibration banner from /ws/state snapshots.
  function applyCalibration(cal) {
    calLit = !cal ? null : (cal.mode === "solo" ? cal.indices : [cal.current]);
    if (!calBanner) return;
    if (cal) {
      calBanner.style.display = "block";
      const txt = (cal.mode === "solo")
        ? `solo lit: #${cal.indices.join(", #")}`
        : `walk · step ${cal.step} · ${cal.interval}s · current #${cal.current}`;
      if (calText) setText(calText, txt);
    } else {
      if (calBanner.style.display !== "none") calBanner.style.display = "none";
      mySolo = null;
    }
  }

  return { start, requestReproject, applyCalibration };
}
