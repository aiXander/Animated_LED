// Surface v2 desktop UI bootstrap.
//
// Wires the dual-mode (Design / Live) operator UI:
//   - Resolume-style layered compositions (LIVE + PREVIEW decks)
//   - Per-layer param panel that applies tweaks live (no LLM round-trip)
//   - Chat (design-mode only) → write_effect → preview slot
//   - Promote / Pull-live-to-preview / blackout / library
//   - Stacked masters panel + audio HUD + WS state stream

import { bindViz } from "./viz.js";
import { $, setText } from "./util.js";

// --- state ---
let state = null;          // last full /state payload
let sessionId = null;
let libraryEffects = [];
const chatLog = $("chat-log");

// --- WS state stream ---
function connectStateStream() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/state`;
  const ws = new WebSocket(url);
  ws.addEventListener("message", (e) => {
    try {
      state = JSON.parse(e.data);
      applyState();
    } catch (_) {}
  });
  ws.addEventListener("close", () => setTimeout(connectStateStream, 500));
  ws.addEventListener("open", () => {
    setText($("ws-pill"), "connected");
    $("ws-pill").className = "pill ok";
  });
  ws.addEventListener("error", () => {
    setText($("ws-pill"), "ws error");
    $("ws-pill").className = "pill bad";
  });
}

// --- viz canvas ---
const viz = bindViz({
  root: $("viz"),
  canvas: $("canvas"),
  tooltip: null,
  calBanner: $("cal-banner"),
  calText: $("cal-text"),
  calClear: $("cal-clear"),
});
// Critical: bindViz returns the API; nothing connects the /ws/frames stream
// until we call start(). Without this the simulator stays black.
viz.start();

// --- mode toggle ---
$("btn-design").addEventListener("click", () => setMode("design"));
$("btn-live").addEventListener("click", () => setMode("live"));

async function setMode(mode) {
  await fetch("/mode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  try { localStorage.setItem("ledctl.mode", mode); } catch (_) {}
}

// Restore last-used mode on first state arrival, BUT default a fresh boot
// (no localStorage entry yet) to LIVE — never wake a hot Pi straight into a
// half-finished preview.
function maybeRestoreMode() {
  try {
    const saved = localStorage.getItem("ledctl.mode");
    if (saved === "design" || saved === "live") {
      if (state && state.mode !== saved) setMode(saved);
    }
  } catch (_) {}
}

// --- masters stack (brightness / speed / audio / saturation / crossfade) ---
function bindMasters() {
  const map = [
    ["m-bri", "v-bri", "brightness"],
    ["m-spd", "v-spd", "speed"],
    ["m-aud", "v-aud", "audio_reactivity"],
    ["m-sat", "v-sat", "saturation"],
  ];
  for (const [sliderId, valId, key] of map) {
    const slider = $(sliderId);
    slider.addEventListener("input", (e) => {
      const v = parseFloat(e.target.value);
      $(valId).textContent = v.toFixed(2);
      sendMaster({ [key]: v });
    });
  }
  $("m-cf").addEventListener("input", (e) => {
    const v = parseFloat(e.target.value);
    $("v-cf").textContent = v.toFixed(2);
    sendCrossfade(v);
  });
}
bindMasters();

let cfTimer = null;
let pendingCf = null;
function sendCrossfade(v) {
  pendingCf = v;
  if (cfTimer) return;
  cfTimer = setTimeout(async () => {
    const value = pendingCf;
    pendingCf = null;
    cfTimer = null;
    await fetch("/agent/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_crossfade_seconds: value }),
    }).catch(() => {});
  }, 60);
}

let pendingMaster = null;
let masterTimer = null;
function sendMaster(patch) {
  pendingMaster = { ...(pendingMaster || {}), ...patch };
  if (masterTimer) return;
  masterTimer = setTimeout(async () => {
    const body = pendingMaster;
    pendingMaster = null;
    masterTimer = null;
    await fetch("/masters", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).catch(() => {});
  }, 60);
}

// --- topbar buttons ---
$("btn-blackout").addEventListener("click", async () => {
  const on = state && state.blackout;
  await fetch(on ? "/resume" : "/blackout", { method: "POST" });
});
// DDP transport toggle. "Pi control" = ledctl streaming DDP (button .on);
// click → POST /transport/pause, after ~2.5s WLED's realtime override
// times out and the Gledopto resumes its own preset. Click again to take
// control back. Disabled when /state reports no DDP transport (dev mode).
let ddpEditAt = 0;
$("btn-ddp").addEventListener("click", async () => {
  const ddp = state && state.ddp;
  if (!ddp || !ddp.available) return;
  const turningOff = !ddp.paused;
  ddpEditAt = performance.now();
  try {
    await fetch(turningOff ? "/transport/pause" : "/transport/resume",
      { method: "POST" });
  } catch (_) { /* WS will resync */ }
});
$("btn-promote").addEventListener("click", async () => {
  await fetch("/promote", { method: "POST" });
});
$("btn-pull").addEventListener("click", async () => {
  await fetch("/pull_live_to_preview", { method: "POST" });
});

// --- library popover ---
$("btn-library").addEventListener("click", async () => {
  const lib = $("lib");
  if (lib.classList.contains("open")) {
    lib.classList.remove("open");
    return;
  }
  const r = await fetch("/effects");
  const data = await r.json();
  libraryEffects = data.effects || [];
  renderLibrary();
  lib.classList.add("open");
});
document.addEventListener("click", (e) => {
  const lib = $("lib");
  if (!lib.contains(e.target) && !$("btn-library").contains(e.target)) {
    lib.classList.remove("open");
  }
});
function renderLibrary() {
  const lib = $("lib");
  if (!libraryEffects.length) {
    lib.innerHTML = `<div class="lib-row"><div class="name">no saved effects</div></div>`;
    return;
  }
  lib.innerHTML = "";
  for (const eff of libraryEffects) {
    const row = document.createElement("div");
    row.className = "lib-row";
    // Hover-revealed action row: pull into Design / pull into Live / ×.
    row.innerHTML = `
      <div class="name">${escapeAttr(eff.name)}</div>
      <div class="summary">${escapeAttr(eff.summary || "")}</div>
      <div class="actions">
        <button class="pull-design" data-act="design" title="Replace the selected preview layer">pull into Design</button>
        <button class="pull-live"   data-act="live"   title="Replace the selected live layer (with crossfade)">pull into Live</button>
        <button class="danger"      data-act="del"    title="Delete from library">×</button>
      </div>`;
    row.addEventListener("click", async (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;  // plain row click is a no-op now — the operator
                         // picks Design vs Live deliberately.
      const act = btn.dataset.act;
      if (act === "design") {
        await fetch(`/effects/${eff.name}/load_preview`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        $("lib").classList.remove("open");
      } else if (act === "live") {
        await fetch(`/effects/${eff.name}/load_live`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        $("lib").classList.remove("open");
      } else if (act === "del") {
        openDeleteModal(eff.name);
      }
    });
    lib.appendChild(row);
  }
}

// ---- save-current-effect modal ---- //

$("btn-save-effect").addEventListener("click", openSaveModal);

function openSaveModal() {
  // Default the input to the selected preview layer's current name so the
  // operator can just edit & overwrite, or rename and save a copy.
  const seed = (state && state.preview && state.preview.layers
    && state.preview.layers[state.preview.selected])
    ? state.preview.layers[state.preview.selected].name
    : "";
  $("save-name").value = seed;
  $("save-err").textContent = "";
  $("save-modal").classList.add("open");
  // Defer focus so the modal is visible before the input grabs it.
  requestAnimationFrame(() => $("save-name").focus());
}

function closeSaveModal() { $("save-modal").classList.remove("open"); }

$("save-cancel").addEventListener("click", closeSaveModal);
$("save-modal").addEventListener("click", (e) => {
  if (e.target.id === "save-modal") closeSaveModal();
});
$("save-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); doSaveEffect(); }
  if (e.key === "Escape") { e.preventDefault(); closeSaveModal(); }
});
$("save-confirm").addEventListener("click", doSaveEffect);

async function doSaveEffect() {
  const name = $("save-name").value.trim();
  const err = $("save-err");
  err.textContent = "";
  if (!/^[a-z][a-z0-9_]{0,40}$/.test(name)) {
    err.textContent = "name must be snake_case ([a-z][a-z0-9_]{0,40})";
    return;
  }
  $("save-confirm").disabled = true;
  $("save-confirm").textContent = "…";
  try {
    const r = await fetch("/preview/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, overwrite: true }),
    });
    if (!r.ok) {
      const text = await r.text();
      err.textContent = `save failed: ${r.status} ${text}`;
      return;
    }
    closeSaveModal();
  } catch (e) {
    err.textContent = `save error: ${e}`;
  } finally {
    $("save-confirm").disabled = false;
    $("save-confirm").textContent = "save";
  }
}

// ---- delete-confirm modal ---- //

let pendingDelete = null;

function openDeleteModal(name) {
  pendingDelete = name;
  $("del-name").textContent = name;
  $("del-modal").classList.add("open");
}

function closeDeleteModal() {
  pendingDelete = null;
  $("del-modal").classList.remove("open");
}

$("del-cancel").addEventListener("click", closeDeleteModal);
$("del-modal").addEventListener("click", (e) => {
  if (e.target.id === "del-modal") closeDeleteModal();
});
$("del-confirm").addEventListener("click", async () => {
  const name = pendingDelete;
  if (!name) return;
  await fetch(`/effects/${name}`, { method: "DELETE" });
  libraryEffects = libraryEffects.filter((x) => x.name !== name);
  renderLibrary();
  closeDeleteModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeSaveModal();
    closeDeleteModal();
  }
});

// --- composition decks ---
function renderDecks() {
  if (!state) return;
  renderDeck("preview", state.preview, $("preview-layers"), $("preview-summary"));
  renderDeck("live", state.live, $("live-layers"), $("live-summary"));
}

function renderDeck(slot, comp, listEl, summaryEl) {
  if (!comp || !comp.layers || comp.layers.length === 0) {
    listEl.innerHTML = `<div class="deck-empty">no layers</div>`;
    if (summaryEl) summaryEl.textContent = "";
    return;
  }
  if (summaryEl) summaryEl.textContent = `${comp.layers.length} layer(s)`;
  listEl.innerHTML = "";
  comp.layers.forEach((layer, i) => {
    const row = document.createElement("div");
    row.className = "layer-row" + (i === comp.selected ? " selected" : "")
                                + (layer.enabled ? "" : " disabled");
    row.innerHTML = `
      <span class="idx">#${i}</span>
      <span class="name" title="${escapeAttr(layer.summary || "")}">${escapeAttr(layer.name)}</span>
      <span class="blend">${layer.blend}</span>
      <span class="opacity">${(layer.opacity * 100).toFixed(0)}%</span>`;
    row.addEventListener("click", async () => {
      await fetch(`/${slot}/select`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ index: i }),
      });
    });
    listEl.appendChild(row);
  });
}

function escapeAttr(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

$("preview-add-layer").addEventListener("click", async () => {
  await fetch("/effects/pulse_mono/load_preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ add_layer: true }),
  });
});
$("live-add-layer").addEventListener("click", async () => {
  await fetch("/effects/pulse_mono/load_live", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ add_layer: true }),
  });
});
$("preview-remove-layer").addEventListener("click", async () => {
  if (!state || !state.preview.layers.length) return;
  const idx = state.preview.selected;
  await fetch("/preview/layer/remove", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index: idx }),
  });
});
$("live-remove-layer").addEventListener("click", async () => {
  if (!state || !state.live.layers.length) return;
  const idx = state.live.selected;
  await fetch("/live/layer/remove", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index: idx }),
  });
});

// --- which deck is the operator focused on for the side panel? ---
function focusedSlot() {
  return (state && state.mode === "design") ? "preview" : "live";
}

function focusedLayer() {
  if (!state) return null;
  const slot = focusedSlot();
  const comp = state[slot];
  if (!comp || !comp.layers.length) return null;
  return { slot, index: comp.selected, layer: comp.layers[comp.selected] };
}

// --- layer meta ---
$("meta-blend").addEventListener("change", (e) => {
  const f = focusedLayer();
  if (!f) return;
  patchMeta(f.slot, f.index, { blend: e.target.value });
});
$("meta-opacity").addEventListener("input", (e) => {
  const f = focusedLayer();
  if (!f) return;
  const v = parseFloat(e.target.value);
  $("meta-opacity-v").textContent = v.toFixed(2);
  patchMeta(f.slot, f.index, { opacity: v });
});
$("meta-enabled").addEventListener("change", (e) => {
  const f = focusedLayer();
  if (!f) return;
  patchMeta(f.slot, f.index, { enabled: e.target.checked });
});
let metaTimer = null;
let pendingMeta = null;
function patchMeta(slot, index, patch) {
  pendingMeta = { slot, index, patch: { ...(pendingMeta?.patch || {}), ...patch } };
  if (metaTimer) return;
  metaTimer = setTimeout(async () => {
    const m = pendingMeta;
    pendingMeta = null;
    metaTimer = null;
    await fetch(`/${m.slot}/layer/blend`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index: m.index, ...m.patch }),
    }).catch(() => {});
  }, 80);
}

// --- params panel (per-layer) ---
function renderParams() {
  const form = $("params-form");
  const f = focusedLayer();
  if (!f || !f.layer) {
    form.innerHTML = `<div class="params-empty">No layer selected.</div>`;
    setText($("side-layer-name"), "—");
    return;
  }
  // Layer label includes a perf hint: "preview#0 · pulse_mono · 0.7 ms p95"
  const perf = f.layer.perf || {};
  const perfTxt = (typeof perf.p95_ms === "number" && perf.p95_ms > 0)
    ? ` · ${perf.p95_ms.toFixed(1)}ms`
    : "";
  setText($("side-layer-name"), `${f.slot}#${f.index} · ${f.layer.name}${perfTxt}`);
  $("meta-blend").value = f.layer.blend;
  if (document.activeElement !== $("meta-opacity")) $("meta-opacity").value = f.layer.opacity;
  $("meta-opacity-v").textContent = (f.layer.opacity).toFixed(2);
  $("meta-enabled").checked = !!f.layer.enabled;

  if (!f.layer.param_schema || !f.layer.param_schema.length) {
    form.innerHTML = `<div class="params-empty">No params declared.</div>`;
    return;
  }
  const sigKey = `${f.slot}#${f.index}#${f.layer.name}`;
  if (form.dataset.sig !== sigKey) {
    form.dataset.sig = sigKey;
    form.innerHTML = "";
    for (const spec of f.layer.param_schema) {
      const row = makeParamRow(spec, f.layer.param_values?.[spec.key], (val) => {
        sendParam(f.slot, f.index, spec.key, val);
      });
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
    const input = row.querySelector("input, select");
    if (!input || document.activeElement === input) continue;
    if (input.type === "checkbox") input.checked = !!v;
    else if (input.type === "color") input.value = String(v);
    else input.value = v;
    const valEl = row.querySelector(".value");
    if (valEl && (input.type === "range" || input.type === "number")) {
      valEl.textContent = (typeof v === "number") ? v.toFixed(2) : String(v);
    }
  }
}

function makeParamRow(spec, currentValue, onChange) {
  const row = document.createElement("div");
  row.className = "param-row";
  row.dataset.key = spec.key;
  const label = document.createElement("label");
  label.textContent = spec.label || spec.key;
  if (spec.help) label.title = spec.help;
  row.appendChild(label);

  const ctrl = document.createElement(spec.control === "select" ? "select" : "input");
  const val = currentValue ?? spec.default;
  let valNode = null;

  if (spec.control === "slider") {
    ctrl.type = "range";
    ctrl.min = spec.min; ctrl.max = spec.max;
    ctrl.step = spec.step ?? 0.01;
    ctrl.value = val;
    valNode = document.createElement("span");
    valNode.className = "value";
    valNode.textContent = Number(val).toFixed(2);
    ctrl.addEventListener("input", () => {
      const v = parseFloat(ctrl.value);
      valNode.textContent = v.toFixed(2);
      onChange(v);
    });
  } else if (spec.control === "int_slider") {
    ctrl.type = "range";
    ctrl.min = spec.min; ctrl.max = spec.max;
    ctrl.step = spec.step ?? 1;
    ctrl.value = val;
    valNode = document.createElement("span");
    valNode.className = "value";
    valNode.textContent = String(val);
    ctrl.addEventListener("input", () => {
      const v = parseInt(ctrl.value, 10);
      valNode.textContent = String(v);
      onChange(v);
    });
  } else if (spec.control === "color") {
    ctrl.type = "color";
    ctrl.value = String(val || "#ffffff");
    ctrl.addEventListener("input", () => onChange(ctrl.value));
  } else if (spec.control === "toggle") {
    ctrl.type = "checkbox";
    ctrl.checked = !!val;
    ctrl.addEventListener("change", () => onChange(ctrl.checked));
  } else if (spec.control === "select") {
    for (const opt of spec.options || []) {
      const o = document.createElement("option");
      o.value = opt; o.textContent = opt;
      if (opt === val) o.selected = true;
      ctrl.appendChild(o);
    }
    ctrl.addEventListener("change", () => onChange(ctrl.value));
  } else if (spec.control === "palette") {
    ctrl.type = "text";
    ctrl.value = String(val || "");
    ctrl.placeholder = "palette name";
    ctrl.addEventListener("change", () => onChange(ctrl.value));
  }
  row.appendChild(ctrl);
  if (valNode) row.appendChild(valNode);
  else row.appendChild(document.createElement("span"));
  return row;
}

let paramQueues = {};
let paramTimers = {};
function sendParam(slot, layerIndex, key, value) {
  const q = paramQueues[slot] = paramQueues[slot] || {};
  q[key] = value;
  paramQueues[slot]._layer_index = layerIndex;
  if (paramTimers[slot]) return;
  paramTimers[slot] = setTimeout(async () => {
    const queue = paramQueues[slot];
    paramQueues[slot] = null;
    paramTimers[slot] = null;
    const layer_index = queue._layer_index;
    delete queue._layer_index;
    await fetch(`/${slot}/params`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: queue, layer_index }),
    }).catch(() => {});
  }, 60);
}

// --- chat ---
$("chat-send").addEventListener("click", sendChat);
$("chat-input").addEventListener("keydown", (e) => {
  // Enter sends; Shift+Enter inserts a literal newline.
  // (The textarea's default behaviour for plain Enter is to insert "\n";
  // we override that for an instant-send feel matching every other chat UI.)
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    sendChat();
  }
});

async function sendChat() {
  const input = $("chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  appendMsg("user", message);
  $("chat-send").disabled = true;
  $("chat-send").textContent = "…";
  try {
    const r = await fetch("/agent/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: sessionId }),
    });
    if (!r.ok) {
      const text = await r.text();
      appendMsg("tool-error", `chat failed: ${r.status} ${text}`);
      return;
    }
    const body = await r.json();
    sessionId = body.session_id;
    if (body.assistant_text) appendMsg("assistant", body.assistant_text);
    if (body.tool_call) {
      const tr = body.tool_result || {};
      const ok = tr.ok;
      const name = body.tool_call.arguments?.name || body.tool_call.name;
      if (ok) {
        appendMsg("tool-ok", `↪ wrote effect: ${name}`);
      } else {
        const detail = JSON.stringify(tr.details ?? tr.error ?? tr);
        appendMsg("tool-error", `❌ ${tr.error || "tool error"}\n${detail}`);
      }
    }
  } catch (e) {
    appendMsg("tool-error", `chat error: ${e}`);
  } finally {
    $("chat-send").disabled = false;
    $("chat-send").textContent = "send";
  }
}

function appendMsg(kind, text) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  if (kind === "user") el.innerHTML = `<div class="label">you</div>${escapeAttr(text)}`;
  else if (kind === "assistant") el.innerHTML = `<div class="label">agent</div>${escapeAttr(text)}`;
  else el.textContent = text;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
}

// --- audio link ---
$("btn-audio").addEventListener("click", async (e) => {
  e.preventDefault();
  try {
    const r = await fetch("/audio/ui");
    if (!r.ok) return;
    const j = await r.json();
    let url = j.tailnet_ui_url && location.protocol === "https:" ? j.tailnet_ui_url : j.ui_url;
    if (url) window.open(url, "_blank");
  } catch (_) {}
});

// --- audio HUD (low / mid / high / beat meters + bpm) ---
//
// Bands are already in [0, 1] (smoothed + auto-scaled upstream by the
// audio-server). beat_count is monotonic — we snap the beat meter to 1.0 on
// every onset and decay it exponentially in a rAF loop so it stays visible
// despite the underlying signal being a single-frame pulse.
let lastBeatCount = 0;
let beatLevel = 0;             // [0, 1] decaying value for the beat meter
let beatLastTickWall = 0;      // performance.now() of last decay step
const BEAT_DECAY_HALFLIFE_MS = 180;

function updateAudioHud(a) {
  const hud = $("audio-hud");
  if (!a || !a.connected) {
    hud.classList.add("disconnected");
    setMeter($("meter-low"), 0);  setText($("num-low"), "0.00");
    setMeter($("meter-mid"), 0);  setText($("num-mid"), "0.00");
    setMeter($("meter-high"), 0); setText($("num-high"), "0.00");
    setMeter($("meter-beat"), 0); setText($("num-beat"), "0");
    setText($("audio-bpm"), "—");
    lastBeatCount = (a && a.beat_count) || 0;
    return;
  }
  hud.classList.remove("disconnected");
  setMeter($("meter-low"),  a.low  || 0);  setText($("num-low"),  (a.low  || 0).toFixed(2));
  setMeter($("meter-mid"),  a.mid  || 0);  setText($("num-mid"),  (a.mid  || 0).toFixed(2));
  setMeter($("meter-high"), a.high || 0);  setText($("num-high"), (a.high || 0).toFixed(2));
  setText($("audio-bpm"), a.bpm != null ? `${a.bpm.toFixed(0)}` : "—");
  // Beat: snap to 1.0 on each new onset; the decay loop handles fadeout.
  if (typeof a.beat_count === "number") {
    if (a.beat_count > lastBeatCount) {
      beatLevel = 1.0;
    }
    lastBeatCount = a.beat_count;
    setText($("num-beat"), String(a.beat_count));
  }
}

// Continuous decay of the beat meter, independent of /ws/state cadence so
// the bar feels alive even between state pushes.
function tickBeatDecay() {
  const now = performance.now();
  if (beatLastTickWall === 0) beatLastTickWall = now;
  const dt = now - beatLastTickWall;
  beatLastTickWall = now;
  if (beatLevel > 0) {
    beatLevel *= Math.pow(0.5, dt / BEAT_DECAY_HALFLIFE_MS);
    if (beatLevel < 0.005) beatLevel = 0;
    setMeter($("meter-beat"), beatLevel);
  }
  requestAnimationFrame(tickBeatDecay);
}
requestAnimationFrame(tickBeatDecay);

function setMeter(el, v) {
  if (!el) return;
  const pct = Math.max(0, Math.min(100, (v || 0) * 100));
  const next = `${pct.toFixed(0)}%`;
  if (el.dataset.w !== next) {
    el.style.width = next;
    el.dataset.w = next;
  }
}

// Reflect the current /state.ddp into the topbar Pi/Gledopto toggle.
// Suppresses transient WS pushes for 600ms after a local click so the
// optimistic UI doesn't flicker between request and resync.
function applyDdp(ddp) {
  const btn = $("btn-ddp");
  if (!btn) return;
  if (performance.now() - ddpEditAt < 600) return;
  if (!ddp || !ddp.available) {
    btn.disabled = true;
    btn.classList.remove("danger");
    btn.classList.add("ghost");
    setText(btn, "no DDP");
    btn.title = "Current transport mode has no DDP leg (e.g. simulator-only).";
    return;
  }
  btn.disabled = false;
  const piOn = !ddp.paused;
  if (piOn) {
    btn.classList.remove("danger");
    btn.classList.add("ghost");
  } else {
    btn.classList.remove("ghost");
    btn.classList.add("danger");
  }
  setText(btn, piOn ? "Pi control" : "Gledopto");
  const sentBits = ` · ${ddp.frames_sent} frames → ${ddp.host}:${ddp.port}`;
  btn.title = (piOn
    ? "ledctl is sending DDP. Click to release: WLED reverts to its own preset after ~2.5s."
    : "DDP paused — Gledopto's own preset is on. Click to take Pi control back.")
    + sentBits;
}

// --- per-tick state apply ---
function applyState() {
  if (!state) return;
  setText($("fps-pill"), `${state.fps?.toFixed?.(0) ?? "?"} fps`);
  updateAudioHud(state.audio);
  document.body.classList.toggle("design-mode", state.mode === "design");
  document.body.classList.toggle("live-mode", state.mode === "live");
  $("btn-design").classList.toggle("active", state.mode === "design");
  $("btn-live").classList.toggle("active", state.mode === "live");
  setText($("viz-mode-pill"), state.mode);
  $("viz-mode-pill").className = "pill " + (state.mode === "live" ? "bad" : "ok");

  const m = state.masters;
  if (m) {
    if (document.activeElement !== $("m-bri")) $("m-bri").value = m.brightness;
    if (document.activeElement !== $("m-sat")) $("m-sat").value = m.saturation;
    if (document.activeElement !== $("m-spd")) $("m-spd").value = m.speed;
    if (document.activeElement !== $("m-aud")) $("m-aud").value = m.audio_reactivity;
    setText($("v-bri"), m.brightness?.toFixed?.(2) ?? "1.00");
    setText($("v-sat"), m.saturation?.toFixed?.(2) ?? "1.00");
    setText($("v-spd"), m.speed?.toFixed?.(2) ?? "1.00");
    setText($("v-aud"), m.audio_reactivity?.toFixed?.(2) ?? "1.00");
  }
  if (typeof state.crossfade_seconds === "number"
      && document.activeElement !== $("m-cf")) {
    $("m-cf").value = state.crossfade_seconds;
    setText($("v-cf"), state.crossfade_seconds.toFixed(2));
  }

  if (state.blackout) {
    $("btn-blackout").classList.add("danger");
    $("btn-blackout").classList.remove("ghost");
    setText($("btn-blackout"), "● BLACKOUT");
  } else {
    $("btn-blackout").classList.remove("danger");
    $("btn-blackout").classList.add("ghost");
    setText($("btn-blackout"), "⚫ blackout");
  }

  applyDdp(state.ddp);

  viz.applyCalibration(state.calibration);
  renderDecks();
  renderParams();
}

connectStateStream();
fetch("/state").then((r) => r.json()).then((s) => {
  state = s;
  applyState();
  maybeRestoreMode();
});
