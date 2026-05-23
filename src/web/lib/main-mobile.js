// Surface v2 MOBILE UI bootstrap (served at /m).
//
// Portrait, thumb-first restructuring of the desktop console. Reuses the
// view-agnostic modules (viz.js / state.js / util.js) and the same HTTP/WS
// API; only the layout + interaction model differ:
//   - header: status dot + DESIGN/LIVE segmented toggle + overflow menu
//   - LED simulator strip with a compact audio HUD
//   - context action bar (Promote / Pull in design; LIVE status in live)
//   - bottom tab bar → Layers / Knobs / Chat / Output panels
//   - bottom sheets for menu / library / playlist / colour / palette
//
// Desktop (index.html + main-desktop.js) is untouched.

import { bindViz } from "./viz.js";
import { $, setText } from "./util.js";

// ---------------- state ----------------
let state = null;
let libraryEffects = [];
let palettes = {};
let paletteNames = [];
let lastChatEpoch = null;
const chatLog = $("chat-log");

const ENGINE_FPS_VALUES = [24, 30, 40, 60, 90];
const SIM_FPS_VALUES = [12, 24, 30, 40, 60];
const clampIdx = (i, len) => Math.max(0, Math.min(len - 1, i | 0));
function nearestIdx(values, target) {
  let best = 0, bestDist = Infinity;
  for (let i = 0; i < values.length; i++) {
    const d = Math.abs(values[i] - target);
    if (d < bestDist) { bestDist = d; best = i; }
  }
  return best;
}

function escapeAttr(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

async function loadPalettes() {
  try {
    const r = await fetch("/palettes");
    const data = await r.json();
    palettes = data.palettes || {};
    paletteNames = data.names || Object.keys(palettes).sort();
  } catch (_) { palettes = {}; paletteNames = []; }
}
function paletteGradientCss(name) {
  const stops = palettes[name];
  if (!stops || !stops.length) return "linear-gradient(90deg,#222,#444)";
  return "linear-gradient(90deg," +
    stops.map(([pos, hex]) => `${hex} ${(pos * 100).toFixed(1)}%`).join(",") + ")";
}

// ---------------- WS state stream ----------------
function connectStateStream() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/state`);
  ws.addEventListener("message", (e) => {
    try { state = JSON.parse(e.data); applyState(); } catch (_) {}
  });
  ws.addEventListener("close", () => { setDot("bad"); setTimeout(connectStateStream, 500); });
  ws.addEventListener("open", () => setDot("ok"));
  ws.addEventListener("error", () => setDot("bad"));
}
function setDot(cls) {
  const d = $("status-dot");
  if (d) d.className = cls || "";
}

// ---------------- viz canvas ----------------
const viz = bindViz({
  root: $("sim"),
  canvas: $("canvas"),
  tooltip: null,            // touch — no hover tooltip
  calBanner: $("cal-banner"),
  calText: $("cal-text"),
  calClear: $("cal-clear"),
});
viz.start();

// ---------------- mode toggle ----------------
for (const btn of document.querySelectorAll("#mode-seg button")) {
  btn.addEventListener("click", () => setMode(btn.dataset.mode));
}
async function setMode(mode) {
  // Optimistic: flip body class immediately so the tab bar / action bar
  // recolour without waiting for the /ws/state round-trip.
  document.body.classList.toggle("design-mode", mode === "design");
  document.body.classList.toggle("live-mode", mode === "live");
  await fetch("/mode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  }).catch(() => {});
  try { localStorage.setItem("ledctl.mode", mode); } catch (_) {}
}
function maybeRestoreMode() {
  try {
    const saved = localStorage.getItem("ledctl.mode");
    if ((saved === "design" || saved === "live") && state && state.mode !== saved) setMode(saved);
  } catch (_) {}
}

// ---------------- bottom tab navigation ----------------
let activeTab = "layers";
for (const btn of document.querySelectorAll("#tabbar button")) {
  btn.addEventListener("click", () => selectTab(btn.dataset.tab));
}
function selectTab(tab) {
  activeTab = tab;
  for (const b of document.querySelectorAll("#tabbar button"))
    b.classList.toggle("active", b.dataset.tab === tab);
  for (const p of document.querySelectorAll(".tab-panel"))
    p.classList.toggle("active", p.dataset.tab === tab);
  if (tab === "knobs") renderParams();
  if (tab === "chat") setTimeout(() => { chatLog.scrollTop = chatLog.scrollHeight; }, 0);
}

// ---------------- bottom sheets / modal helpers ----------------
function openSheet(id) { $(id).classList.add("open"); }
function closeSheet(id) { $(id).classList.remove("open"); }
function closeAllSheets() {
  for (const s of document.querySelectorAll(".sheet-back.open")) s.classList.remove("open");
}
// Tap on the dark backdrop (outside the .sheet) closes the sheet.
for (const back of document.querySelectorAll(".sheet-back")) {
  back.addEventListener("click", (e) => { if (e.target === back) back.classList.remove("open"); });
}

// ---------------- overflow menu ----------------
$("btn-menu").addEventListener("click", () => openSheet("sheet-menu"));
$("mi-library").addEventListener("click", () => { closeSheet("sheet-menu"); openLibrary(); });
$("mi-playlist").addEventListener("click", () => { closeSheet("sheet-menu"); openPlaylist(); });
$("mi-save").addEventListener("click", () => { closeSheet("sheet-menu"); openSaveModal(); });
$("mi-ddp").addEventListener("click", async () => {
  const ddp = state && state.ddp;
  if (!ddp || !ddp.available) return;
  ddpEditAt = performance.now();
  await fetch(ddp.paused ? "/transport/resume" : "/transport/pause", { method: "POST" }).catch(() => {});
});
$("mi-audio").addEventListener("click", async (e) => {
  e.preventDefault();
  try {
    const j = await (await fetch("/audio/ui")).json();
    const url = (j.tailnet_ui_url && location.protocol === "https:") ? j.tailnet_ui_url : j.ui_url;
    if (url) window.open(url, "_blank");
  } catch (_) {}
});

// ---------------- masters / fps / crossfade ----------------
function bindMasters() {
  const map = [
    ["m-bri", "v-bri", "brightness"],
    ["m-spd", "v-spd", "speed"],
    ["m-aud", "v-aud", "audio_reactivity"],
    ["m-sat", "v-sat", "saturation"],
  ];
  for (const [sliderId, valId, key] of map) {
    $(sliderId).addEventListener("input", (e) => {
      const v = parseFloat(e.target.value);
      setText($(valId), v.toFixed(2));
      sendMaster({ [key]: v });
    });
  }
  $("m-cf").addEventListener("input", (e) => {
    const v = parseFloat(e.target.value);
    setText($("v-cf"), v.toFixed(2));
    sendCrossfade(v);
  });
  $("m-engfps").addEventListener("input", (e) => {
    const v = ENGINE_FPS_VALUES[clampIdx(parseInt(e.target.value, 10), ENGINE_FPS_VALUES.length)];
    setText($("v-engfps"), String(v));
    sendDebounced("/engine/fps", { fps: v }, "eng");
  });
  $("m-simfps").addEventListener("input", (e) => {
    const v = SIM_FPS_VALUES[clampIdx(parseInt(e.target.value, 10), SIM_FPS_VALUES.length)];
    setText($("v-simfps"), String(v));
    sendDebounced("/sim/fps", { fps: v }, "sim");
  });
}
bindMasters();

const _debounce = {};
function sendDebounced(url, body, key, method = "PATCH", ms = 70) {
  _debounce[key] = { url, body, method };
  if (_debounce[key + "_t"]) return;
  _debounce[key + "_t"] = setTimeout(async () => {
    const job = _debounce[key];
    _debounce[key + "_t"] = null;
    await fetch(job.url, {
      method: job.method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(job.body),
    }).catch(() => {});
  }, ms);
}
let pendingMaster = null, masterTimer = null;
function sendMaster(patch) {
  pendingMaster = { ...(pendingMaster || {}), ...patch };
  if (masterTimer) return;
  masterTimer = setTimeout(async () => {
    const body = pendingMaster; pendingMaster = null; masterTimer = null;
    await fetch("/masters", {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).catch(() => {});
  }, 60);
}
function sendCrossfade(v) {
  sendDebounced("/agent/config", { default_crossfade_seconds: v }, "cf");
}

// ---------------- blackout / ddp / promote / pull ----------------
$("btn-blackout-big").addEventListener("click", async () => {
  await fetch(state && state.blackout ? "/resume" : "/blackout", { method: "POST" }).catch(() => {});
});
let ddpEditAt = 0;
$("btn-promote").addEventListener("click", () => fetch("/promote", { method: "POST" }).catch(() => {}));
$("btn-pull").addEventListener("click", async () => {
  await fetch("/pull_live_to_preview", { method: "POST" }).catch(() => {});
  setMode("design");
});

// ---------------- add layer ----------------
$("add-layer").addEventListener("click", async () => {
  const slot = focusedSlot();
  const ep = slot === "live" ? "load_live" : "load_preview";
  await fetch(`/effects/pulse_mono/${ep}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ add_layer: true }),
  }).catch(() => {});
});

// ---------------- composition deck ----------------
function focusedSlot() { return (state && state.mode === "design") ? "preview" : "live"; }
function focusedComp() { return state ? state[focusedSlot()] : null; }
function focusedLayer() {
  const comp = focusedComp();
  if (!comp || !comp.layers.length) return null;
  return { slot: focusedSlot(), index: comp.selected, layer: comp.layers[comp.selected] };
}

// Guards mirroring the desktop: don't tear down rows mid-interaction.
let layerSliderActive = false;
let layerActionGuardUntil = 0;

function renderDeck(opts) {
  if (!state) return;
  if (layerSliderActive) return;
  if (!opts?.force && performance.now() < layerActionGuardUntil) return;
  const slot = focusedSlot();
  const comp = state[slot];
  const list = $("layer-list");
  setText($("deck-title"), slot === "live" ? "LIVE composition" : "DESIGN composition");
  if (!comp || !comp.layers || comp.layers.length === 0) {
    list.innerHTML = `<div class="deck-empty">no layers</div>`;
    setText($("deck-count"), "");
    return;
  }
  setText($("deck-count"), `${comp.layers.length} layer${comp.layers.length === 1 ? "" : "s"}`);
  list.innerHTML = "";
  comp.layers.forEach((layer, i) => {
    const row = document.createElement("div");
    row.className = "layer-row" + (i === comp.selected ? " selected" : "") + (layer.enabled ? "" : " disabled");
    row.dataset.idx = String(i);
    const blendOpts = ["normal", "add", "screen", "multiply"]
      .map((b) => `<option value="${b}"${b === layer.blend ? " selected" : ""}>${b}</option>`).join("");
    const playGlyph = layer.enabled ? "❚❚" : "▶";
    row.innerHTML = `
      <span class="idx">${i}.</span>
      <span class="name">${escapeAttr(layer.name)}<span class="sum">${escapeAttr(layer.summary || "")}</span></span>
      <div class="layer-actions">
        <button class="enabled-btn" type="button" title="enable/pause">${playGlyph}</button>
        <button class="delete-btn" type="button" title="delete">×</button>
      </div>
      <div class="op">
        <span class="op-lbl">opacity</span>
        <input class="op-slider" type="range" min="0" max="1" step="0.01" value="${layer.opacity}">
        <span class="op-num">${layer.opacity.toFixed(2)}</span>
      </div>
      <div class="blend-row">
        <span class="bl-lbl">blend</span>
        <select class="layer-blend">${blendOpts}</select>
      </div>`;
    wireLayerRow(row, slot, i, layer);
    list.appendChild(row);
  });
}

function wireLayerRow(row, slot, idx, layer) {
  // tap name/idx → select this layer (and hop to Knobs for quick editing)
  const selectZone = row.querySelector(".name");
  const idxZone = row.querySelector(".idx");
  const doSelect = () => {
    fetch(`/${slot}/select`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index: idx }),
    }).catch(() => {});
    if (state && state[slot]) { state[slot].selected = idx; renderDeck({ force: true }); renderParams(); }
  };
  selectZone.addEventListener("click", doSelect);
  idxZone.addEventListener("click", doSelect);

  // opacity slider (debounced PATCH; guard re-render while dragging)
  const slider = row.querySelector(".op-slider");
  const opNum = row.querySelector(".op-num");
  slider.addEventListener("pointerdown", () => { layerSliderActive = true; });
  slider.addEventListener("input", () => {
    const v = parseFloat(slider.value);
    opNum.textContent = v.toFixed(2);
    layer.opacity = v;
    patchMeta(slot, idx, { opacity: v });
  });
  const endSlide = () => { layerSliderActive = false; };
  slider.addEventListener("pointerup", endSlide);
  slider.addEventListener("pointercancel", endSlide);
  slider.addEventListener("change", endSlide);

  // enable / delete
  row.querySelector(".enabled-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    layerActionGuardUntil = performance.now() + 500;
    const comp = state && state[slot];
    if (!comp?.layers?.[idx]) return;
    const next = !comp.layers[idx].enabled;
    comp.layers[idx].enabled = next;
    renderDeck({ force: true });
    patchMetaNow(slot, idx, { enabled: next });
  });
  row.querySelector(".delete-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    layerActionGuardUntil = performance.now() + 500;
    const comp = state && state[slot];
    if (!comp?.layers?.[idx]) return;
    comp.layers.splice(idx, 1);
    if (comp.selected >= comp.layers.length) comp.selected = Math.max(0, comp.layers.length - 1);
    renderDeck({ force: true });
    fetch(`/${slot}/layer/remove`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index: idx }),
    }).catch(() => {});
  });
  // blend
  const blend = row.querySelector(".layer-blend");
  blend.addEventListener("pointerdown", () => { layerActionGuardUntil = performance.now() + 1000; });
  blend.addEventListener("change", (e) => {
    e.stopPropagation();
    layerActionGuardUntil = performance.now() + 500;
    const comp = state && state[slot];
    if (comp?.layers?.[idx]) comp.layers[idx].blend = blend.value;
    patchMetaNow(slot, idx, { blend: blend.value });
  });
}

let metaTimer = null, pendingMeta = null;
function patchMeta(slot, index, patch) {
  pendingMeta = { slot, index, patch: { ...(pendingMeta?.patch || {}), ...patch } };
  if (metaTimer) return;
  metaTimer = setTimeout(async () => {
    const m = pendingMeta; pendingMeta = null; metaTimer = null;
    await fetch(`/${m.slot}/layer/blend`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index: m.index, ...m.patch }),
    }).catch(() => {});
  }, 80);
}
function patchMetaNow(slot, index, patch) {
  fetch(`/${slot}/layer/blend`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index, ...patch }),
  }).catch(() => {});
}

// ---------------- params (knobs) ----------------
function renderParams() {
  const form = $("params-form");
  const f = focusedLayer();
  if (!f || !f.layer) {
    form.innerHTML = `<div class="params-empty">No layer selected.<br>Tap a layer in the Layers tab.</div>`;
    setText($("knobs-layer"), "—");
    form.dataset.sig = "";
    return;
  }
  const perf = f.layer.perf || {};
  const perfTxt = (typeof perf.p95_ms === "number" && perf.p95_ms > 0) ? ` · ${perf.p95_ms.toFixed(1)}ms` : "";
  setText($("knobs-layer"), `${f.layer.name}${perfTxt}`);
  if (!f.layer.param_schema || !f.layer.param_schema.length) {
    form.innerHTML = `<div class="params-empty">No knobs for this effect.</div>`;
    form.dataset.sig = "";
    return;
  }
  const sigKey = `${f.slot}#${f.index}#${f.layer.name}`;
  if (form.dataset.sig !== sigKey) {
    form.dataset.sig = sigKey;
    form.innerHTML = "";
    for (const spec of f.layer.param_schema) {
      const row = makeParamRow(spec, f.layer.param_values?.[spec.key], (val) => sendParam(f.slot, f.index, spec.key, val));
      form.appendChild(row);
    }
  } else {
    syncParamValues(form, f.layer.param_values || {});
  }
}

function syncParamValues(form, values) {
  for (const row of form.children) {
    const key = row.dataset.key;
    if (!key || !(key in values)) continue;
    const v = values[key];
    const colorBtn = row.querySelector(".color-btn");
    if (colorBtn) { colorBtn.style.background = String(v); colorBtn.dataset.color = String(v); continue; }
    const paletteBtn = row.querySelector(".palette-btn");
    if (paletteBtn) {
      const name = String(v || "");
      paletteBtn.dataset.palette = name;
      const sw = paletteBtn.querySelector(".palette-swatch");
      if (sw) sw.style.background = paletteGradientCss(name);
      const ne = paletteBtn.querySelector(".palette-name");
      if (ne) ne.textContent = name || "—";
      continue;
    }
    const input = row.querySelector("input, select");
    if (!input || document.activeElement === input) continue;
    if (input.type === "checkbox") input.checked = !!v;
    else input.value = v;
    const valEl = row.querySelector(".value");
    if (valEl && (input.type === "range" || input.type === "number"))
      valEl.textContent = (typeof v === "number") ? v.toFixed(2) : String(v);
  }
}

function makeParamRow(spec, currentValue, onChange) {
  const row = document.createElement("div");
  row.className = "param-row";
  row.dataset.key = spec.key;
  const val = currentValue ?? spec.default;

  const top = document.createElement("div");
  top.className = "top";
  const label = document.createElement("label");
  label.textContent = spec.label || spec.key;
  top.appendChild(label);
  const valNode = document.createElement("span");
  valNode.className = "value";
  top.appendChild(valNode);
  row.appendChild(top);
  if (spec.help) {
    const help = document.createElement("div");
    help.className = "help"; help.textContent = spec.help;
    row.appendChild(help);
  }

  if (spec.control === "slider" || spec.control === "int_slider") {
    const intMode = spec.control === "int_slider";
    const ctrl = document.createElement("input");
    ctrl.type = "range"; ctrl.min = spec.min; ctrl.max = spec.max;
    ctrl.step = spec.step ?? (intMode ? 1 : 0.01); ctrl.value = val;
    valNode.textContent = intMode ? String(val) : Number(val).toFixed(2);
    ctrl.addEventListener("input", () => {
      const v = intMode ? parseInt(ctrl.value, 10) : parseFloat(ctrl.value);
      valNode.textContent = intMode ? String(v) : v.toFixed(2);
      onChange(v);
    });
    row.appendChild(ctrl);
    return row;
  }
  if (spec.control === "color") {
    valNode.textContent = "";
    const btn = document.createElement("div");
    btn.className = "color-btn";
    btn.style.background = String(val || "#ffffff");
    btn.dataset.color = String(val || "#ffffff");
    btn.addEventListener("click", () => openColorSheet(spec.label || spec.key, btn.dataset.color, (hex) => {
      btn.style.background = hex; btn.dataset.color = hex; onChange(hex);
    }));
    row.appendChild(btn);
    return row;
  }
  if (spec.control === "toggle") {
    const wrap = document.createElement("label");
    wrap.className = "switch";
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = !!val;
    const track = document.createElement("span"); track.className = "track";
    const knob = document.createElement("span"); knob.className = "knob";
    cb.addEventListener("change", () => onChange(cb.checked));
    wrap.appendChild(cb); wrap.appendChild(track); wrap.appendChild(knob);
    top.appendChild(wrap);   // toggle sits in the header row, right-aligned
    return row;
  }
  if (spec.control === "select") {
    const ctrl = document.createElement("select");
    ctrl.className = "param-select";
    for (const opt of (spec.options || [])) {
      const o = document.createElement("option");
      o.value = opt; o.textContent = opt; if (opt === val) o.selected = true;
      ctrl.appendChild(o);
    }
    ctrl.value = String(val);
    ctrl.addEventListener("change", () => onChange(ctrl.value));
    row.appendChild(ctrl);
    return row;
  }
  if (spec.control === "palette") {
    const names = paletteNames.length ? paletteNames : Object.keys(palettes);
    let current = String(val || "");
    if (!names.includes(current)) current = names.includes(spec.default) ? String(spec.default) : (names[0] || "");
    const btn = document.createElement("div");
    btn.className = "palette-btn";
    const sw = document.createElement("div"); sw.className = "palette-swatch"; sw.style.background = paletteGradientCss(current);
    const nm = document.createElement("span"); nm.className = "palette-name"; nm.textContent = current || "—";
    btn.appendChild(sw); btn.appendChild(nm); btn.dataset.palette = current;
    btn.addEventListener("click", () => openPaletteSheet(current, (picked) => {
      current = picked; btn.dataset.palette = picked;
      sw.style.background = paletteGradientCss(picked); nm.textContent = picked; onChange(picked);
    }));
    row.appendChild(btn);
    return row;
  }
  return row;
}

let paramQueues = {}, paramTimers = {};
function sendParam(slot, layerIndex, key, value) {
  const q = paramQueues[slot] = paramQueues[slot] || {};
  q[key] = value; q._layer_index = layerIndex;
  if (paramTimers[slot]) return;
  paramTimers[slot] = setTimeout(async () => {
    const queue = paramQueues[slot];
    paramQueues[slot] = null; paramTimers[slot] = null;
    const layer_index = queue._layer_index; delete queue._layer_index;
    await fetch(`/${slot}/params`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: queue, layer_index }),
    }).catch(() => {});
  }, 60);
}

// ---------------- colour wheel sheet ----------------
const wheelCanvas = $("color-wheel");
const wheelCtx = wheelCanvas.getContext("2d");
let activeColorOnChange = null;
let colorH = 0, colorS = 0, colorV = 1;

function drawWheel() {
  const w = wheelCanvas.width, h = wheelCanvas.height;
  const cx = w / 2, cy = h / 2, r = Math.min(cx, cy) - 1;
  const img = wheelCtx.createImageData(w, h);
  const v = colorV;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const dx = x - cx, dy = y - cy;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const idx = (y * w + x) * 4;
      if (dist > r) { img.data[idx + 3] = 0; continue; }
      const hue = ((Math.atan2(dy, dx) * 180 / Math.PI) + 360) % 360;
      const sat = Math.min(1, dist / r);
      const [rr, gg, bb] = hsvToRgb(hue / 360, sat, v);
      img.data[idx] = rr; img.data[idx + 1] = gg; img.data[idx + 2] = bb; img.data[idx + 3] = 255;
    }
  }
  wheelCtx.putImageData(img, 0, 0);
}
function hsvToRgb(h, s, v) {
  let r, g, b; const i = Math.floor(h * 6); const f = h * 6 - i;
  const p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
  switch (i % 6) {
    case 0: r = v; g = t; b = p; break; case 1: r = q; g = v; b = p; break;
    case 2: r = p; g = v; b = t; break; case 3: r = p; g = q; b = v; break;
    case 4: r = t; g = p; b = v; break; case 5: r = v; g = p; b = q; break;
  }
  return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
}
function rgbToHsv(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h, s, v = max; const d = max - min;
  s = max === 0 ? 0 : d / max;
  if (max === min) h = 0;
  else { switch (max) { case r: h = (g - b) / d + (g < b ? 6 : 0); break; case g: h = (b - r) / d + 2; break; case b: h = (r - g) / d + 4; break; } h /= 6; }
  return [h, s, v];
}
function hexToRgb(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec((hex || "").trim());
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [255, 255, 255];
}
const rgbToHex = (r, g, b) => "#" + [r, g, b].map((n) => n.toString(16).padStart(2, "0")).join("");
function updateMarker() {
  const w = wheelCanvas.clientWidth, h = wheelCanvas.clientHeight;
  const cx = w / 2, cy = h / 2, r = Math.min(cx, cy) - 1;
  const angle = colorH * 2 * Math.PI, dist = colorS * r;
  $("color-marker").style.left = (cx + Math.cos(angle) * dist) + "px";
  $("color-marker").style.top = (cy + Math.sin(angle) * dist) + "px";
}
function applyColor() {
  const [r, g, b] = hsvToRgb(colorH, colorS, colorV);
  const hex = rgbToHex(r, g, b);
  $("color-hex").value = hex;
  if (activeColorOnChange) activeColorOnChange(hex);
}
function openColorSheet(title, currentHex, onChange) {
  activeColorOnChange = onChange;
  setText($("color-title"), title || "Color");
  const [r, g, b] = hexToRgb(currentHex || "#ffffff");
  const [h, s, v] = rgbToHsv(r, g, b);
  colorH = h; colorS = s; colorV = v;
  $("color-v").value = v; $("color-hex").value = currentHex;
  drawWheel();
  openSheet("sheet-color");
  requestAnimationFrame(updateMarker);
}
function pickFromWheel(clientX, clientY) {
  const rect = wheelCanvas.getBoundingClientRect();
  const cx = rect.width / 2, cy = rect.height / 2, r = Math.min(cx, cy) - 1;
  const dx = clientX - rect.left - cx, dy = clientY - rect.top - cy;
  const dist = Math.sqrt(dx * dx + dy * dy);
  colorS = Math.min(1, dist / r);
  colorH = (((Math.atan2(dy, dx) * 180 / Math.PI) + 360) % 360) / 360;
  updateMarker(); applyColor();
}
let wheelDrag = false;
wheelCanvas.addEventListener("pointerdown", (e) => { wheelDrag = true; try { wheelCanvas.setPointerCapture(e.pointerId); } catch (_) {} pickFromWheel(e.clientX, e.clientY); });
wheelCanvas.addEventListener("pointermove", (e) => { if (wheelDrag) pickFromWheel(e.clientX, e.clientY); });
wheelCanvas.addEventListener("pointerup", () => { wheelDrag = false; });
wheelCanvas.addEventListener("pointercancel", () => { wheelDrag = false; });
$("color-v").addEventListener("input", (e) => { colorV = parseFloat(e.target.value); drawWheel(); applyColor(); });
$("color-hex").addEventListener("change", (e) => {
  const v = e.target.value.trim();
  if (!/^#?[a-f0-9]{6}$/i.test(v)) return;
  const [r, g, b] = hexToRgb(v); const [h, s, vv] = rgbToHsv(r, g, b);
  colorH = h; colorS = s; colorV = vv; $("color-v").value = vv;
  drawWheel(); updateMarker(); if (activeColorOnChange) activeColorOnChange(rgbToHex(r, g, b));
});
$("color-done").addEventListener("click", () => closeSheet("sheet-color"));

// ---------------- palette sheet ----------------
function openPaletteSheet(currentName, onPick) {
  const list = $("palette-list");
  list.innerHTML = "";
  const names = paletteNames.length ? paletteNames : Object.keys(palettes);
  for (const name of names) {
    const item = document.createElement("div");
    item.className = "palette-item" + (name === currentName ? " selected" : "");
    item.innerHTML = `<div class="palette-swatch" style="background:${paletteGradientCss(name)}"></div><span class="nm">${escapeAttr(name)}</span>`;
    item.addEventListener("click", () => { onPick(name); closeSheet("sheet-palette"); });
    list.appendChild(item);
  }
  openSheet("sheet-palette");
}

// ---------------- library ----------------
async function openLibrary() {
  await refreshLibrary();
  $("lib-search").value = "";
  renderLibrary("");
  openSheet("sheet-library");
}
$("lib-search").addEventListener("input", (e) => renderLibrary(e.target.value.trim().toLowerCase()));
async function refreshLibrary() {
  try { libraryEffects = (await (await fetch("/effects")).json()).effects || []; }
  catch (_) { libraryEffects = []; }
}
function renderLibrary(filter) {
  const list = $("lib-list");
  const items = filter
    ? libraryEffects.filter((e) => e.name.toLowerCase().includes(filter) || (e.summary || "").toLowerCase().includes(filter))
    : libraryEffects;
  if (!items.length) { list.innerHTML = `<div class="params-empty">no effects</div>`; return; }
  list.innerHTML = "";
  for (const eff of items) {
    const row = document.createElement("div");
    row.className = "lib-row";
    row.innerHTML = `
      <div class="info">
        <div class="nm">${escapeAttr(eff.name)}</div>
        <div class="sm">${escapeAttr(eff.summary || "")}</div>
      </div>
      <div class="acts">
        <button class="pd" data-act="design">→ design</button>
        <button class="pl" data-act="live">→ live</button>
        <button class="ghost" data-act="rename">✎</button>
        <button class="danger" data-act="del">×</button>
      </div>`;
    row.querySelector(".acts").addEventListener("click", async (e) => {
      const btn = e.target.closest("button"); if (!btn) return;
      const act = btn.dataset.act;
      if (act === "design") {
        await fetch(`/effects/${eff.name}/load_preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        closeSheet("sheet-library"); setMode("design"); selectTab("layers");
      } else if (act === "live") {
        await fetch(`/effects/${eff.name}/load_live`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        closeSheet("sheet-library"); setMode("live"); selectTab("layers");
      } else if (act === "rename") { openRenameModal(eff.name); }
      else if (act === "del") { openDeleteModal(eff.name); }
    });
    list.appendChild(row);
  }
}

// ---------------- playlist ----------------
let pendingPlaylist = [];
async function openPlaylist() {
  await refreshLibrary();
  const sel = $("pl-add-select"); sel.innerHTML = "";
  for (const eff of libraryEffects) {
    const o = document.createElement("option"); o.value = eff.name; o.textContent = eff.name; sel.appendChild(o);
  }
  let pl = state?.playlist;
  if (!pl) { try { pl = await (await fetch("/playlist")).json(); } catch (_) { pl = { entries: [] }; } }
  pendingPlaylist = (pl.entries || []).map((e) => ({ name: e.name, play_seconds: e.play_seconds }));
  renderPlaylistRows();
  openSheet("sheet-playlist");
}
$("pl-add-btn").addEventListener("click", () => {
  const name = $("pl-add-select").value; if (!name) return;
  const seconds = Math.max(5, parseFloat($("pl-add-seconds").value) || 120);
  pendingPlaylist.push({ name, play_seconds: seconds });
  renderPlaylistRows(); pushPlaylist();
});
$("pl-start").addEventListener("click", async () => {
  $("pl-err").textContent = "";
  await pushPlaylist();
  const r = await fetch("/playlist/start", { method: "POST" });
  if (!r.ok) $("pl-err").textContent = `start failed: ${await r.text()}`;
});
$("pl-stop").addEventListener("click", () => fetch("/playlist/stop", { method: "POST" }).catch(() => {}));

function renderPlaylistRows() {
  const list = $("pl-list");
  list.innerHTML = "";
  if (!pendingPlaylist.length) { list.innerHTML = `<div class="pl-status">empty — add effects below</div>`; return; }
  pendingPlaylist.forEach((entry, i) => {
    const row = document.createElement("div");
    row.className = "pl-row";
    row.innerHTML = `
      <span class="idx">#${i + 1}</span>
      <span class="nm">${escapeAttr(entry.name)}</span>
      <input type="number" min="5" max="3600" step="5" value="${entry.play_seconds}">
      <span class="reord">
        <button data-act="up" ${i === 0 ? "disabled" : ""}>↑</button>
        <button data-act="dn" ${i === pendingPlaylist.length - 1 ? "disabled" : ""}>↓</button>
        <button class="del" data-act="del">×</button>
      </span>`;
    const num = row.querySelector("input[type=number]");
    num.addEventListener("change", () => { pendingPlaylist[i].play_seconds = Math.max(5, parseFloat(num.value) || 120); pushPlaylist(); });
    row.addEventListener("click", (e) => {
      const btn = e.target.closest("button"); if (!btn) return;
      const act = btn.dataset.act;
      if (act === "up" && i > 0) [pendingPlaylist[i - 1], pendingPlaylist[i]] = [pendingPlaylist[i], pendingPlaylist[i - 1]];
      else if (act === "dn" && i < pendingPlaylist.length - 1) [pendingPlaylist[i + 1], pendingPlaylist[i]] = [pendingPlaylist[i], pendingPlaylist[i + 1]];
      else if (act === "del") pendingPlaylist.splice(i, 1);
      renderPlaylistRows(); pushPlaylist();
    });
    list.appendChild(row);
  });
}
let pushTimer = null;
function pushPlaylist() {
  if (pushTimer) return;
  pushTimer = setTimeout(async () => {
    pushTimer = null;
    try {
      const r = await fetch("/playlist", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entries: pendingPlaylist }) });
      $("pl-err").textContent = r.ok ? "" : `save failed: ${await r.text()}`;
    } catch (e) { $("pl-err").textContent = `save error: ${e}`; }
  }, 80);
}
function formatSec(s) { s = Math.round(s); const m = Math.floor(s / 60), r = s % 60; return m > 0 ? `${m}:${String(r).padStart(2, "0")}` : `${r}s`; }
function updatePlaylistStatus(pl) {
  const banner = $("playlist-banner");
  const status = $("pl-status");
  const miState = $("mi-playlist-state");
  if (!pl || !pl.running) {
    banner.style.display = "none";
    const txt = pl?.entries?.length ? `stopped · ${pl.entries.length} entr${pl.entries.length === 1 ? "y" : "ies"}` : "stopped";
    if (status) status.textContent = txt;
    if (miState) miState.textContent = pl?.entries?.length ? `${pl.entries.length}` : "";
    return;
  }
  const remaining = Math.max(0, (pl.current_total || 0) - (pl.current_elapsed || 0));
  banner.style.display = "inline-block";
  banner.className = "pill ok";
  banner.textContent = `▶ ${pl.current_name || "?"} · ${formatSec(remaining)}`;
  if (status) status.textContent = `running · ${pl.current_name} · ${formatSec(remaining)} remaining`;
  if (miState) miState.textContent = "▶ running";
}

// ---------------- save / rename / delete modals ----------------
function openModal(id) { $(id).classList.add("open"); }
function closeModal(id) { $(id).classList.remove("open"); }
for (const m of document.querySelectorAll(".modal-back")) {
  m.addEventListener("click", (e) => { if (e.target === m) m.classList.remove("open"); });
}
function openSaveModal() {
  const f = focusedLayer();
  $("save-name").value = f?.layer?.name || "";
  $("save-err").textContent = "";
  openModal("save-modal");
  requestAnimationFrame(() => $("save-name").focus());
}
$("save-cancel").addEventListener("click", () => closeModal("save-modal"));
$("save-confirm").addEventListener("click", doSaveEffect);
async function doSaveEffect() {
  const name = $("save-name").value.trim();
  const err = $("save-err"); err.textContent = "";
  if (!/^[a-z][a-z0-9_]{0,40}$/.test(name)) { err.textContent = "name must be snake_case"; return; }
  if (state && state.mode === "live") await fetch("/pull_live_to_preview", { method: "POST" }).catch(() => {});
  $("save-confirm").disabled = true;
  try {
    const r = await fetch("/preview/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, overwrite: true }) });
    if (!r.ok) { err.textContent = `save failed: ${await r.text()}`; return; }
    closeModal("save-modal");
  } catch (e) { err.textContent = `save error: ${e}`; }
  finally { $("save-confirm").disabled = false; }
}

let pendingRename = null;
function openRenameModal(name) {
  pendingRename = name;
  $("rename-old").textContent = name;
  $("rename-name").value = name;
  $("rename-err").textContent = "";
  openModal("rename-modal");
  requestAnimationFrame(() => { const i = $("rename-name"); i.focus(); i.select(); });
}
$("rename-cancel").addEventListener("click", () => closeModal("rename-modal"));
$("rename-confirm").addEventListener("click", async () => {
  const oldName = pendingRename; if (!oldName) return;
  const newName = $("rename-name").value.trim();
  const err = $("rename-err"); err.textContent = "";
  if (!/^[a-z][a-z0-9_]{0,40}$/.test(newName)) { err.textContent = "name must be snake_case"; return; }
  if (newName === oldName) { closeModal("rename-modal"); return; }
  try {
    const r = await fetch(`/effects/${oldName}/rename`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ new_name: newName }) });
    if (!r.ok) { err.textContent = `rename failed: ${await r.text()}`; return; }
    await refreshLibrary(); renderLibrary($("lib-search").value.trim().toLowerCase()); closeModal("rename-modal");
  } catch (e) { err.textContent = `rename error: ${e}`; }
});

let pendingDelete = null;
function openDeleteModal(name) { pendingDelete = name; $("del-name").textContent = name; openModal("del-modal"); }
$("del-cancel").addEventListener("click", () => closeModal("del-modal"));
$("del-confirm").addEventListener("click", async () => {
  const name = pendingDelete; if (!name) return;
  await fetch(`/effects/${name}`, { method: "DELETE" }).catch(() => {});
  libraryEffects = libraryEffects.filter((x) => x.name !== name);
  renderLibrary($("lib-search").value.trim().toLowerCase());
  closeModal("del-modal");
});

// ---------------- chat ----------------
const CHAT_TIMEOUT_MS = 20000;
let chatBusy = false;
$("chat-send").addEventListener("click", sendChat);
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
});
$("chat-input").addEventListener("input", (e) => {
  // grow textarea up to its max-height
  e.target.style.height = "auto";
  e.target.style.height = Math.min(112, e.target.scrollHeight) + "px";
});
$("btn-new-chat").addEventListener("click", async () => {
  if (chatBusy) return;
  chatLog.innerHTML = ""; $("chat-input").value = "";
  try { await fetch("/agent/session", { method: "DELETE" }); } catch (_) {}
  $("chat-input").focus();
});

function appendThinking() {
  const el = document.createElement("div");
  el.className = "msg thinking";
  el.innerHTML = `thinking<span class="dots"></span>`;
  chatLog.appendChild(el); chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}
async function sendChat() {
  if (chatBusy) return;
  const input = $("chat-input");
  const message = input.value.trim(); if (!message) return;
  input.value = ""; input.style.height = "auto";
  appendMsg("user", message);
  chatBusy = true;
  const sendBtn = $("chat-send");
  sendBtn.disabled = true; sendBtn.textContent = "…"; input.disabled = true;
  const thinkingEl = appendThinking();
  const ctrl = new AbortController();
  const timeoutId = setTimeout(() => ctrl.abort(), CHAT_TIMEOUT_MS);
  let timedOut = false;
  try {
    const r = await fetch("/agent/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }), signal: ctrl.signal,
    });
    if (!r.ok) { appendMsg("tool-error", `chat failed: ${r.status} ${await r.text()}`); return; }
    const body = await r.json();
    if (body.assistant_text) appendMsg("assistant", body.assistant_text, body.usage);
    if (body.tool_call) {
      const tr = body.tool_result || {};
      const args = body.tool_call.arguments || {};
      const name = args.name || body.tool_call.name;
      if (tr.ok) appendToolOk(name, args.code || "", args.summary || "");
      else appendMsg("tool-error", `❌ ${tr.error || "tool error"}\n${JSON.stringify(tr.details ?? tr.error ?? tr)}`);
    }
  } catch (e) {
    if (e && e.name === "AbortError") { timedOut = true; appendMsg("tool-error", `chat timed out after ${(CHAT_TIMEOUT_MS / 1000) | 0}s.`); }
    else appendMsg("tool-error", `chat error: ${e}`);
  } finally {
    clearTimeout(timeoutId);
    if (thinkingEl.parentNode) thinkingEl.parentNode.removeChild(thinkingEl);
    chatBusy = false; sendBtn.disabled = false; sendBtn.textContent = "send"; input.disabled = false;
    if (!timedOut) input.focus();
  }
}
function appendMsg(kind, text, usage) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  if (kind === "user") el.innerHTML = `<div class="label">you</div>${escapeAttr(text)}`;
  else if (kind === "assistant") el.innerHTML = `<div class="label">agent</div>${escapeAttr(text)}`;
  else el.textContent = text;
  if (usage) { const u = document.createElement("div"); u.className = "usage"; u.textContent = formatUsage(usage); el.appendChild(u); }
  chatLog.appendChild(el); chatLog.scrollTop = chatLog.scrollHeight;
}
function appendToolOk(name, code, summary) {
  const el = document.createElement("div");
  el.className = "msg tool-ok clickable";
  el.textContent = `↪ wrote effect: ${name} (tap to view code)`;
  if (code) el.addEventListener("click", () => openCodeModal(name, code, summary));
  chatLog.appendChild(el); chatLog.scrollTop = chatLog.scrollHeight;
}
function formatUsage(u) {
  const inT = u.prompt_tokens ?? u.input_tokens ?? 0;
  const outT = u.completion_tokens ?? u.output_tokens ?? 0;
  return `tokens · in ${inT} · out ${outT} · total ${u.total_tokens ?? (inT + outT)}`;
}

// ---------------- code viewer ----------------
function openCodeModal(name, code, summary) {
  setText($("code-modal-title"), `Source — ${name}`);
  $("code-meta").innerHTML = `<span class="name">${escapeAttr(name)}</span>${summary ? " · " + escapeAttr(summary) : ""} · ${code.split("\n").length} lines`;
  $("code-content").innerHTML = highlightPython(code);
  $("code-modal").dataset.code = code;
  openModal("code-modal");
}
$("code-close").addEventListener("click", () => closeModal("code-modal"));
$("code-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("code-modal").dataset.code || ""); const b = $("code-copy"); const p = b.textContent; b.textContent = "copied ✓"; setTimeout(() => b.textContent = p, 900); } catch (_) {}
});
const PY_KEYWORDS = new Set(["and","as","assert","async","await","break","class","continue","def","del","elif","else","except","finally","for","from","global","if","import","in","is","lambda","nonlocal","not","or","pass","raise","return","try","while","with","yield","True","False","None"]);
const PY_BUILTINS = new Set(["np","Effect","hex_to_rgb","hsv_to_rgb","lerp","clip01","gauss","pulse","tri","wrap_dist","palette_lerp","named_palette","rng","log","PI","TAU","LUT_SIZE","PALETTE_NAMES","int","float","str","bool","range","len","min","max","abs","round","sum"]);
function escapeHtml(s) { return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]); }
function highlightPython(src) {
  let i = 0; const n = src.length; let out = ""; let pending = null;
  const isIdStart = (c) => /[A-Za-z_]/.test(c), isIdCont = (c) => /[A-Za-z0-9_]/.test(c);
  while (i < n) {
    const c = src[i];
    if (c === "#") { let j = i; while (j < n && src[j] !== "\n") j++; out += `<span class="tok-com">${escapeHtml(src.slice(i, j))}</span>`; i = j; continue; }
    if ((c === '"' || c === "'") && src.slice(i, i + 3) === c.repeat(3)) { const q = c.repeat(3); const end = src.indexOf(q, i + 3); const j = end < 0 ? n : end + 3; out += `<span class="tok-str">${escapeHtml(src.slice(i, j))}</span>`; i = j; continue; }
    if (c === '"' || c === "'") { let j = i + 1; while (j < n && src[j] !== c && src[j] !== "\n") { if (src[j] === "\\" && j + 1 < n) j += 2; else j += 1; } if (j < n && src[j] === c) j += 1; out += `<span class="tok-str">${escapeHtml(src.slice(i, j))}</span>`; i = j; continue; }
    if (c === "@" && i + 1 < n && isIdStart(src[i + 1])) { let j = i + 1; while (j < n && (isIdCont(src[j]) || src[j] === ".")) j++; out += `<span class="tok-deco">${escapeHtml(src.slice(i, j))}</span>`; i = j; continue; }
    if (/[0-9]/.test(c) || (c === "." && i + 1 < n && /[0-9]/.test(src[i + 1]))) { let j = i; while (j < n && /[0-9.eE_+\-xXabcdefABCDEF]/.test(src[j])) { if ((src[j] === "+" || src[j] === "-") && !(j > i && (src[j - 1] === "e" || src[j - 1] === "E"))) break; j++; } out += `<span class="tok-num">${escapeHtml(src.slice(i, j))}</span>`; i = j; continue; }
    if (isIdStart(c)) {
      let j = i + 1; while (j < n && isIdCont(src[j])) j++; const word = src.slice(i, j); let cls = null;
      if (PY_KEYWORDS.has(word)) { cls = "tok-kw"; if (word === "def") pending = "def"; else if (word === "class") pending = "cls"; }
      else if (pending) { cls = pending === "cls" ? "tok-cls" : "tok-def"; pending = null; }
      else if (word === "self" || word === "cls") cls = "tok-self";
      else if (PY_BUILTINS.has(word)) cls = "tok-bi";
      out += cls ? `<span class="${cls}">${escapeHtml(word)}</span>` : escapeHtml(word); i = j; continue;
    }
    out += escapeHtml(c); i += 1;
  }
  const lines = out.split("\n"); const pad = String(lines.length).length;
  return lines.map((line, idx) => `<span class="ln">${String(idx + 1).padStart(pad, " ")}</span>${line}`).join("\n");
}

// global Escape closes top-most overlay (useful with a hardware keyboard)
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const openModalEl = document.querySelector(".modal-back.open");
  if (openModalEl) { openModalEl.classList.remove("open"); return; }
  closeAllSheets();
});

// ---------------- audio HUD ----------------
let lastBeatCount = 0, beatLevel = 0, audioLastTickWall = 0;
const BEAT_DECAY_HALFLIFE_MS = 100, BAND_HALFLIFE_MS = 30;
const audioTargets = { low: 0, mid: 0, high: 0 }, audioCurrent = { low: 0, mid: 0, high: 0 };
let audioConnected = false;
function updateAudioHud(a) {
  const hud = $("audio-hud");
  if (!a || !a.connected) {
    hud.classList.add("disconnected"); audioConnected = false;
    audioTargets.low = audioTargets.mid = audioTargets.high = 0;
    setText($("audio-bpm"), "—"); lastBeatCount = (a && a.beat_count) || 0; return;
  }
  hud.classList.remove("disconnected"); audioConnected = true;
  audioTargets.low = a.low || 0; audioTargets.mid = a.mid || 0; audioTargets.high = a.high || 0;
  setText($("audio-bpm"), a.bpm != null ? `${a.bpm.toFixed(0)}` : "—");
  if (typeof a.beat_count === "number") { if (a.beat_count > lastBeatCount) beatLevel = 1.0; lastBeatCount = a.beat_count; }
}
function setMeter(el, v) { if (!el) return; const next = `${Math.max(0, Math.min(100, (v || 0) * 100)).toFixed(1)}%`; if (el.dataset.w !== next) { el.style.width = next; el.dataset.w = next; } }
function tickAudioHud() {
  const now = performance.now();
  if (audioLastTickWall === 0) audioLastTickWall = now;
  const dt = now - audioLastTickWall; audioLastTickWall = now;
  if (audioConnected) {
    const alpha = 1.0 - Math.pow(0.5, dt / BAND_HALFLIFE_MS);
    audioCurrent.low += (audioTargets.low - audioCurrent.low) * alpha;
    audioCurrent.mid += (audioTargets.mid - audioCurrent.mid) * alpha;
    audioCurrent.high += (audioTargets.high - audioCurrent.high) * alpha;
  } else { audioCurrent.low = audioCurrent.mid = audioCurrent.high = 0; }
  setMeter($("meter-low"), audioCurrent.low); setMeter($("meter-mid"), audioCurrent.mid); setMeter($("meter-high"), audioCurrent.high);
  if (beatLevel > 0) { beatLevel *= Math.pow(0.5, dt / BEAT_DECAY_HALFLIFE_MS); if (beatLevel < 0.005) beatLevel = 0; }
  setMeter($("meter-beat"), beatLevel);
  requestAnimationFrame(tickAudioHud);
}
requestAnimationFrame(tickAudioHud);

function applyDdp(ddp) {
  const st = $("mi-ddp-state");
  if (!st) return;
  if (performance.now() - ddpEditAt < 600) return;
  if (!ddp || !ddp.available) { st.textContent = "no DDP"; return; }
  st.textContent = ddp.paused ? "Gledopto" : "● on";
}

// ---------------- apply state ----------------
function applyState() {
  if (!state) return;
  if (typeof state.chat_epoch === "number") {
    if (lastChatEpoch === null) lastChatEpoch = state.chat_epoch;
    else if (state.chat_epoch !== lastChatEpoch) { lastChatEpoch = state.chat_epoch; chatLog.innerHTML = ""; }
  }
  updateAudioHud(state.audio);
  document.body.classList.toggle("design-mode", state.mode === "design");
  document.body.classList.toggle("live-mode", state.mode === "live");
  for (const b of document.querySelectorAll("#mode-seg button")) b.classList.toggle("active", b.dataset.mode === state.mode);

  const m = state.masters;
  if (m) {
    if (document.activeElement !== $("m-bri")) $("m-bri").value = m.brightness;
    if (document.activeElement !== $("m-spd")) $("m-spd").value = m.speed;
    if (document.activeElement !== $("m-aud")) $("m-aud").value = m.audio_reactivity;
    if (document.activeElement !== $("m-sat")) $("m-sat").value = m.saturation;
    setText($("v-bri"), m.brightness?.toFixed?.(2) ?? "1.00");
    setText($("v-spd"), m.speed?.toFixed?.(2) ?? "1.00");
    setText($("v-aud"), m.audio_reactivity?.toFixed?.(2) ?? "1.00");
    setText($("v-sat"), m.saturation?.toFixed?.(2) ?? "1.00");
  }
  if (typeof state.crossfade_seconds === "number" && document.activeElement !== $("m-cf")) {
    $("m-cf").value = state.crossfade_seconds; setText($("v-cf"), state.crossfade_seconds.toFixed(2));
  }
  if (typeof state.target_fps === "number" && document.activeElement !== $("m-engfps")) {
    const idx = nearestIdx(ENGINE_FPS_VALUES, state.target_fps); $("m-engfps").value = String(idx); setText($("v-engfps"), String(ENGINE_FPS_VALUES[idx]));
  }
  if (typeof state.sim_fps === "number" && document.activeElement !== $("m-simfps")) {
    const idx = nearestIdx(SIM_FPS_VALUES, state.sim_fps); $("m-simfps").value = String(idx); setText($("v-simfps"), String(SIM_FPS_VALUES[idx]));
  }

  // blackout button
  const bo = $("btn-blackout-big");
  if (state.blackout) { bo.classList.add("live"); bo.classList.remove("ghost"); setText(bo, "● BLACKOUT ON — tap to resume"); }
  else { bo.classList.remove("live"); bo.classList.add("ghost"); setText(bo, "⚫ Blackout"); }

  applyDdp(state.ddp);
  updatePlaylistStatus(state.playlist);
  // live composition summary in the action bar
  const live = state.live;
  setText($("comp-summary"), live?.layers?.length ? `${live.layers.length} layer${live.layers.length === 1 ? "" : "s"}` : "—");

  viz.applyCalibration(state.calibration);
  renderDeck();
  if (activeTab === "knobs") renderParams();
}

// ---------------- boot ----------------
connectStateStream();
Promise.all([loadPalettes(), fetch("/state").then((r) => r.json())]).then(([_, s]) => {
  state = s; applyState(); maybeRestoreMode();
});
