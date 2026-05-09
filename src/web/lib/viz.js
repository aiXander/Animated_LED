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

// STRIP_THICKNESS used to be a fixed 12 device-px which was fine for a
// roughly-square canvas but looks lost in a wide 15vh strip — the rig is
// 30:1, so faithful aspect-ratio projection wastes most of the vertical
// space. We now stretch x and y independently to fill the canvas (the sim
// is a HUD, not a to-scale model) and derive strip thickness from the
// per-row vertical share so each strip visually fills its half of the rig.
const LED_WIDTH_SCALE = 1.0;
const HOVER_DOT = 8;
// Margins as a fraction of the canvas extent — keeps a tiny breathing
// gap at either end of the rig regardless of viewport width.
const HORIZONTAL_MARGIN_FRAC = 0.05;
const VERTICAL_MARGIN_FRAC = 0.05;

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
  let bboxMin = [0, 0, 0];
  let bboxMax = [1, 1, 1];
  let lastFrame = null;
  let projected = null;
  let mouseX = -1, mouseY = -1;
  let mouseAbsX = 0, mouseAbsY = 0;
  let mySolo = null;
  let canvasW = 1, canvasH = 1;
  let canvasDpr = 1;

  function setStatus(txt, cls) {
    if (wsPill) { wsPill.textContent = txt; wsPill.className = cls; }
    if (statusEngine) { statusEngine.textContent = txt; statusEngine.className = cls; }
  }

  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    canvasDpr = dpr;
    const rect = canvas.getBoundingClientRect();
    canvasW = Math.max(1, Math.floor(rect.width));
    canvasH = Math.max(1, Math.floor(rect.height));
    canvas.width = Math.floor(canvasW * dpr);
    canvas.height = Math.floor(canvasH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    reproject();
  }

  let reprojectScheduled = false;
  function requestReproject() {
    if (reprojectScheduled) return;
    reprojectScheduled = true;
    requestAnimationFrame(() => { reprojectScheduled = false; resizeCanvas(); });
  }

  function reproject() {
    if (!leds.length) { projected = null; return; }
    const ww = canvasW * (1 - 2 * HORIZONTAL_MARGIN_FRAC);
    const hh = canvasH * (1 - 2 * VERTICAL_MARGIN_FRAC);
    const dx = bboxMax[0] - bboxMin[0] || 1;
    const dy = bboxMax[1] - bboxMin[1] || 1;
    // Stretch axes independently — the sim is a HUD, not a faithful map.
    const sx = ww / dx;
    const sy = hh / dy;
    const cx = canvasW / 2;
    const cy = canvasH / 2;
    const ox = (bboxMin[0] + bboxMax[0]) / 2;
    const oy = (bboxMin[1] + bboxMax[1]) / 2;
    projected = new Float32Array(leds.length * 2);
    for (let i = 0; i < leds.length; i++) {
      projected[i * 2]     = cx + (leds[i].position[0] - ox) * sx;
      projected[i * 2 + 1] = cy - (leds[i].position[1] - oy) * sy;
    }
    ledRects = new Int32Array(leds.length * 4);
    const dpr = canvasDpr;
    // Strip thickness = 80% of a per-row vertical share. With 2 rows in our
    // rig: each row gets canvasH/2; strips fill ~80% of that, leaving a thin
    // visual gap between top and bottom rows.
    const rows = _approxRowCount();
    const perRowPx = canvasH / Math.max(1, rows);
    const thickCss = Math.max(2, Math.floor(perRowPx * 0.8));
    const thickDev = Math.max(1, Math.round(thickCss * dpr));
    const halfThickDev = (thickDev / 2) | 0;
    strips.forEach((strip) => {
      const off = strip.pixel_offset;
      const n = strip.pixel_count;
      if (n === 0) return;
      const sx = projected[off * 2];
      const sy = projected[off * 2 + 1];
      const ex = projected[(off + n - 1) * 2];
      const ey = projected[(off + n - 1) * 2 + 1];
      const ddx = ex - sx;
      const ddy = ey - sy;
      const horizontal = Math.abs(ddx) >= Math.abs(ddy);
      if (horizontal) {
        const yTopDev = Math.round(sy * dpr) - halfThickDev;
        const stepF = n > 1 ? ddx / (n - 1) : (ddx >= 0 ? 1 : -1);
        const halfStepCss = stepF / 2;
        let prevBoundDev = Math.round((sx - halfStepCss) * dpr);
        for (let i = 0; i < n; i++) {
          const nextCss = (i < n - 1)
            ? (sx + stepF * (i + 0.5))
            : (sx + stepF * (n - 1) + halfStepCss);
          const nextBoundDev = Math.round(nextCss * dpr);
          const slotW = Math.abs(nextBoundDev - prevBoundDev);
          const slotL = Math.min(prevBoundDev, nextBoundDev);
          let xL, w;
          if (LED_WIDTH_SCALE >= 0.999) {
            xL = slotL; w = Math.max(1, slotW);
          } else {
            w = Math.max(1, Math.round(slotW * LED_WIDTH_SCALE));
            xL = slotL + ((slotW - w) >> 1);
          }
          const idx = (off + i) * 4;
          ledRects[idx]     = xL;
          ledRects[idx + 1] = yTopDev;
          ledRects[idx + 2] = w;
          ledRects[idx + 3] = thickDev;
          prevBoundDev = nextBoundDev;
        }
      } else {
        const xLeftDev = Math.round(sx * dpr) - halfThickDev;
        const stepF = n > 1 ? ddy / (n - 1) : (ddy >= 0 ? 1 : -1);
        const halfStepCss = stepF / 2;
        let prevBoundDev = Math.round((sy - halfStepCss) * dpr);
        for (let i = 0; i < n; i++) {
          const nextCss = (i < n - 1)
            ? (sy + stepF * (i + 0.5))
            : (sy + stepF * (n - 1) + halfStepCss);
          const nextBoundDev = Math.round(nextCss * dpr);
          const slotH = Math.abs(nextBoundDev - prevBoundDev);
          const slotT = Math.min(prevBoundDev, nextBoundDev);
          let yT, h;
          if (LED_WIDTH_SCALE >= 0.999) {
            yT = slotT; h = Math.max(1, slotH);
          } else {
            h = Math.max(1, Math.round(slotH * LED_WIDTH_SCALE));
            yT = slotT + ((slotH - h) >> 1);
          }
          const idx = (off + i) * 4;
          ledRects[idx]     = xLeftDev;
          ledRects[idx + 1] = yT;
          ledRects[idx + 2] = thickDev;
          ledRects[idx + 3] = h;
          prevBoundDev = nextBoundDev;
        }
      }
    });
  }

  // Count unique y-bands among strips — used to size strip thickness so the
  // sim fills its viz strip vertically regardless of how many parallel rows
  // the rig has. Two close-by y values count as one band.
  function _approxRowCount() {
    if (!strips.length) return 1;
    const ys = strips.map((s) => 0.5 * (s.start[1] + s.end[1]));
    ys.sort((a, b) => a - b);
    let bands = 1;
    for (let i = 1; i < ys.length; i++) {
      if (Math.abs(ys[i] - ys[i - 1]) > 0.01) bands += 1;
    }
    return bands;
  }

  function pickLED() {
    if (!projected || mouseX < 0) return -1;
    const radius = 10, r2 = radius * radius;
    let best = -1, bestD = r2;
    for (let i = 0; i < leds.length; i++) {
      const dx = projected[i * 2] - mouseX;
      const dy = projected[i * 2 + 1] - mouseY;
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
      if (tooltip) {
        const hit = pickLED();
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

  // Tap / click → solo that LED via /calibration/solo. Works on both
  // desktop (precise hit via pickLED with mouseX) and touch (we set
  // mouseX from the tap location).
  canvas.addEventListener("click", async (e) => {
    if (mouseX < 0) {
      const r = canvas.getBoundingClientRect();
      mouseX = e.clientX - r.left;
      mouseY = e.clientY - r.top;
    }
    const hit = pickLED();
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

  async function reloadTopology() {
    const topo = await loadTopology();
    leds = topo.leds || [];
    strips = topo.strips || [];
    bboxMin = topo.bbox_min;
    bboxMax = topo.bbox_max;
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
    const ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";
    ws.onopen = () => setStatus("connected", "ok");
    ws.onclose = () => setStatus("disconnected", "bad");
    ws.onerror = () => setStatus("error", "bad");
    ws.onmessage = (ev) => {
      const buf = new Uint8Array(ev.data);
      // Pixel count drift → topology changed (e.g., layout edit). Refetch.
      if (buf.length !== leds.length * 3) { reloadTopology(); }
      lastFrame = buf;
    };
    requestAnimationFrame(draw);
  }

  // Drive the calibration banner from /ws/state snapshots.
  function applyCalibration(cal) {
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
