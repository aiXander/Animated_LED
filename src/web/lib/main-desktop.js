// Surface v2 desktop UI bootstrap.
//
// Wires the dual-mode (Design / Live) operator UI:
//   - Resolume-style layered compositions (LIVE + PREVIEW decks)
//   - Per-layer param panel that applies tweaks live (no LLM round-trip)
//   - Chat (design-mode only) → write_effect → preview slot
//   - Promote / Pull-live-to-preview / blackout / library
//   - Master row + audio pill + WS state stream

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
bindViz({
  root: $("viz"),
  canvas: $("canvas"),
  tooltip: null,
  calBanner: $("cal-banner"),
  calText: $("cal-text"),
  calClear: $("cal-clear"),
});

// --- mode toggle ---
$("btn-design").addEventListener("click", () => setMode("design"));
$("btn-live").addEventListener("click", () => setMode("live"));

async function setMode(mode) {
  await fetch("/mode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
}

// --- masters row ---
function bindMasters() {
  const map = [
    ["m-bri", "v-bri", "brightness"],
    ["m-sat", "v-sat", "saturation"],
    ["m-spd", "v-spd", "speed"],
    ["m-aud", "v-aud", "audio_reactivity"],
  ];
  for (const [sliderId, valId, key] of map) {
    const slider = $(sliderId);
    slider.addEventListener("input", (e) => {
      const v = parseFloat(e.target.value);
      $(valId).textContent = v.toFixed(2);
      sendMaster({ [key]: v });
    });
  }
  $("m-frz").addEventListener("change", (e) => sendMaster({ freeze: e.target.checked }));
}
bindMasters();

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
    lib.innerHTML = `<div class="lib-row"><div>no saved effects</div></div>`;
    return;
  }
  lib.innerHTML = "";
  for (const eff of libraryEffects) {
    const row = document.createElement("div");
    row.className = "lib-row";
    row.innerHTML = `
      <div>
        <div>${escapeAttr(eff.name)}</div>
        <div class="summary">${escapeAttr(eff.summary || "")}</div>
      </div>
      <div class="actions">
        <button data-act="preview">preview</button>
        <button data-act="add-pre">+pre</button>
        <button data-act="add-live">+live</button>
        <button data-act="del" class="danger">×</button>
      </div>`;
    row.addEventListener("click", async (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === "preview") {
        await fetch(`/effects/${eff.name}/load_preview`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
      } else if (act === "add-pre") {
        await fetch(`/effects/${eff.name}/load_preview`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ add_layer: true }),
        });
      } else if (act === "add-live") {
        await fetch(`/effects/${eff.name}/load_live`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ add_layer: true }),
        });
      } else if (act === "del") {
        if (!confirm(`delete ${eff.name}?`)) return;
        await fetch(`/effects/${eff.name}`, { method: "DELETE" });
        libraryEffects = libraryEffects.filter((x) => x.name !== eff.name);
        renderLibrary();
      }
    });
    lib.appendChild(row);
  }
}

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
  setText($("side-layer-name"), `${f.slot}#${f.index} · ${f.layer.name}`);
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
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) sendChat();
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

// --- per-tick state apply ---
function applyState() {
  if (!state) return;
  setText($("fps-pill"), `${state.fps?.toFixed?.(0) ?? "?"} fps`);
  const a = state.audio;
  if (a && a.connected) {
    setText($("audio-pill"), `audio L${a.low?.toFixed?.(2) ?? "0"} M${a.mid?.toFixed?.(2) ?? "0"}`);
    $("audio-pill").className = "pill ok";
  } else {
    setText($("audio-pill"), "audio off");
    $("audio-pill").className = "pill warn";
  }
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
    $("m-frz").checked = !!m.freeze;
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

  renderDecks();
  renderParams();
}

connectStateStream();
fetch("/state").then((r) => r.json()).then((s) => { state = s; applyState(); });
