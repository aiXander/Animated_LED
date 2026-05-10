// Surface v2 desktop UI bootstrap.
//
// Single-deck operator UI:
//   - DESIGN tab → operates on the PREVIEW slot, shows chat + Promote
//   - LIVE tab   → operates on the LIVE slot, shows masters
//   - Library "pull into Design"/"pull into Live" auto-switches tabs
//   - Single playlist modal (start / stop / reorder / change duration)
//   - Custom circular color picker for color params
//   - Token usage (input/output) printed after every assistant turn

import { bindViz } from "./viz.js";
import { $, setText } from "./util.js";

// --- state ---
let state = null;
let sessionId = null;
let libraryEffects = [];
const chatLog = $("chat-log");

// --- WS state stream ---
function connectStateStream() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/state`);
  ws.addEventListener("message", (e) => {
    try { state = JSON.parse(e.data); applyState(); } catch (_) {}
  });
  ws.addEventListener("close", () => setTimeout(connectStateStream, 500));
  ws.addEventListener("open", () => {
    setText($("ws-pill"), "connected"); $("ws-pill").className = "pill ok";
  });
  ws.addEventListener("error", () => {
    setText($("ws-pill"), "ws error"); $("ws-pill").className = "pill bad";
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

function maybeRestoreMode() {
  try {
    const saved = localStorage.getItem("ledctl.mode");
    if (saved === "design" || saved === "live") {
      if (state && state.mode !== saved) setMode(saved);
    }
  } catch (_) {}
}

// --- masters ---
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
  $("m-engfps").addEventListener("input", (e) => {
    const idx = clampIdx(parseInt(e.target.value, 10), ENGINE_FPS_VALUES.length);
    const v = ENGINE_FPS_VALUES[idx];
    $("v-engfps").textContent = String(v);
    sendEngineFps(v);
  });
  $("m-simfps").addEventListener("input", (e) => {
    const idx = clampIdx(parseInt(e.target.value, 10), SIM_FPS_VALUES.length);
    const v = SIM_FPS_VALUES[idx];
    $("v-simfps").textContent = String(v);
    sendSimFps(v);
  });
}
bindMasters();

// Slider value is the index into the snap-value table — guarantees the
// frontend can only emit values the backend `set_target_fps` accepts. Keep
// in sync with `Engine.ALLOWED_TARGET_FPS` and `SimulatorTransport.ALLOWED_FPS`.
const ENGINE_FPS_VALUES = [24, 30, 40, 60, 90];
const SIM_FPS_VALUES    = [12, 24, 30, 40, 60];
function clampIdx(i, len) { return Math.max(0, Math.min(len - 1, i | 0)); }
function nearestIdx(values, target) {
  // Server-pushed value may not exactly match a snap (e.g. boot value from
  // YAML). Pick the closest stop so the slider thumb visually agrees.
  let best = 0, bestDist = Infinity;
  for (let i = 0; i < values.length; i++) {
    const d = Math.abs(values[i] - target);
    if (d < bestDist) { bestDist = d; best = i; }
  }
  return best;
}

let engFpsTimer = null, pendingEngFps = null;
function sendEngineFps(v) {
  pendingEngFps = v;
  if (engFpsTimer) return;
  engFpsTimer = setTimeout(async () => {
    const value = pendingEngFps; pendingEngFps = null; engFpsTimer = null;
    await fetch("/engine/fps", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fps: value }),
    }).catch(() => {});
  }, 80);
}

let simFpsTimer = null, pendingSimFps = null;
function sendSimFps(v) {
  pendingSimFps = v;
  if (simFpsTimer) return;
  simFpsTimer = setTimeout(async () => {
    const value = pendingSimFps; pendingSimFps = null; simFpsTimer = null;
    await fetch("/sim/fps", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fps: value }),
    }).catch(() => {});
  }, 80);
}

let cfTimer = null, pendingCf = null;
function sendCrossfade(v) {
  pendingCf = v;
  if (cfTimer) return;
  cfTimer = setTimeout(async () => {
    const value = pendingCf; pendingCf = null; cfTimer = null;
    await fetch("/agent/config", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_crossfade_seconds: value }),
    }).catch(() => {});
  }, 60);
}

let pendingMaster = null, masterTimer = null;
function sendMaster(patch) {
  pendingMaster = { ...(pendingMaster || {}), ...patch };
  if (masterTimer) return;
  masterTimer = setTimeout(async () => {
    const body = pendingMaster; pendingMaster = null; masterTimer = null;
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
let ddpEditAt = 0;
$("btn-ddp").addEventListener("click", async () => {
  const ddp = state && state.ddp;
  if (!ddp || !ddp.available) return;
  const turningOff = !ddp.paused;
  ddpEditAt = performance.now();
  await fetch(turningOff ? "/transport/pause" : "/transport/resume",
    { method: "POST" }).catch(() => {});
});
$("btn-promote").addEventListener("click", async () => {
  await fetch("/promote", { method: "POST" });
});
$("btn-pull").addEventListener("click", async () => {
  await fetch("/pull_live_to_preview", { method: "POST" });
  setMode("design");
});

// --- library popover ---
$("btn-library").addEventListener("click", async () => {
  const lib = $("lib");
  if (lib.classList.contains("open")) { lib.classList.remove("open"); return; }
  await refreshLibrary();
  renderLibrary();
  lib.classList.add("open");
});
document.addEventListener("click", (e) => {
  const lib = $("lib");
  if (!lib.contains(e.target) && !$("btn-library").contains(e.target)) {
    lib.classList.remove("open");
  }
});

async function refreshLibrary() {
  const r = await fetch("/effects");
  const data = await r.json();
  libraryEffects = data.effects || [];
}

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
    row.innerHTML = `
      <div class="name">${escapeAttr(eff.name)}</div>
      <div class="summary">${escapeAttr(eff.summary || "")}</div>
      <div class="actions">
        <button class="pull-design" data-act="design" title="Replace the design preview layer">→ design</button>
        <button class="pull-live"   data-act="live"   title="Replace the live layer (with crossfade)">→ live</button>
        <button class="ghost rename-btn" data-act="rename" title="Rename this effect">✎</button>
        <button class="danger"      data-act="del"    title="Delete from library">×</button>
      </div>`;
    row.addEventListener("click", async (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === "design") {
        await fetch(`/effects/${eff.name}/load_preview`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        $("lib").classList.remove("open");
        setMode("design");
      } else if (act === "live") {
        await fetch(`/effects/${eff.name}/load_live`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        $("lib").classList.remove("open");
        setMode("live");
      } else if (act === "rename") {
        openRenameModal(eff.name);
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
  // Save the effect from whichever slot the current tab targets.
  const slot = focusedSlot();
  const layer = state && state[slot] && state[slot].layers
                && state[slot].layers[state[slot].selected];
  $("save-name").value = layer ? layer.name : "";
  $("save-err").textContent = "";
  $("save-modal").classList.add("open");
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
  // The save endpoint reads from the PREVIEW layer specifically. If we're
  // on the LIVE tab, pull the live composition into preview first so the
  // operator can save what's currently playing.
  if (state && state.mode === "live") {
    await fetch("/pull_live_to_preview", { method: "POST" }).catch(() => {});
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
      err.textContent = `save failed: ${r.status} ${await r.text()}`;
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

// ---- rename-effect modal ---- //
let pendingRename = null;
function openRenameModal(name) {
  pendingRename = name;
  $("rename-old").textContent = name;
  $("rename-name").value = name;
  $("rename-err").textContent = "";
  $("rename-modal").classList.add("open");
  requestAnimationFrame(() => {
    const inp = $("rename-name");
    inp.focus();
    inp.select();
  });
}
function closeRenameModal() {
  pendingRename = null;
  $("rename-modal").classList.remove("open");
}
$("rename-cancel").addEventListener("click", closeRenameModal);
$("rename-modal").addEventListener("click", (e) => {
  if (e.target.id === "rename-modal") closeRenameModal();
});
$("rename-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); doRenameEffect(); }
  if (e.key === "Escape") { e.preventDefault(); closeRenameModal(); }
});
$("rename-confirm").addEventListener("click", doRenameEffect);

async function doRenameEffect() {
  const oldName = pendingRename;
  if (!oldName) return;
  const newName = $("rename-name").value.trim();
  const err = $("rename-err");
  err.textContent = "";
  if (!/^[a-z][a-z0-9_]{0,40}$/.test(newName)) {
    err.textContent = "name must be snake_case ([a-z][a-z0-9_]{0,40})";
    return;
  }
  if (newName === oldName) { closeRenameModal(); return; }
  $("rename-confirm").disabled = true;
  $("rename-confirm").textContent = "…";
  try {
    const r = await fetch(`/effects/${oldName}/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_name: newName }),
    });
    if (!r.ok) {
      err.textContent = `rename failed: ${r.status} ${await r.text()}`;
      return;
    }
    await refreshLibrary();
    renderLibrary();
    closeRenameModal();
  } catch (e) {
    err.textContent = `rename error: ${e}`;
  } finally {
    $("rename-confirm").disabled = false;
    $("rename-confirm").textContent = "rename";
  }
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeSaveModal();
    closeDeleteModal();
    closeRenameModal();
    closePlaylistModal();
    closeColorPopover();
    closeCodeModal();
  }
});

// ---- playlist modal ---- //
$("btn-playlist").addEventListener("click", openPlaylistModal);
$("pl-close").addEventListener("click", closePlaylistModal);
$("pl-modal").addEventListener("click", (e) => {
  if (e.target.id === "pl-modal") closePlaylistModal();
});
$("pl-add-btn").addEventListener("click", () => {
  const sel = $("pl-add-select");
  const name = sel.value;
  if (!name) return;
  const seconds = Math.max(5, parseFloat($("pl-add-seconds").value) || 120);
  pendingPlaylist.push({ name, play_seconds: seconds });
  renderPlaylistRows();
  pushPlaylist();
});
$("pl-start").addEventListener("click", async () => {
  $("pl-err").textContent = "";
  await pushPlaylist();
  const r = await fetch("/playlist/start", { method: "POST" });
  if (!r.ok) { $("pl-err").textContent = `start failed: ${await r.text()}`; }
});
$("pl-stop").addEventListener("click", async () => {
  await fetch("/playlist/stop", { method: "POST" });
});

let pendingPlaylist = [];

async function openPlaylistModal() {
  await refreshLibrary();
  // Hydrate the dropdown of available effects.
  const sel = $("pl-add-select");
  sel.innerHTML = "";
  for (const eff of libraryEffects) {
    const o = document.createElement("option");
    o.value = eff.name; o.textContent = eff.name;
    sel.appendChild(o);
  }
  // Load current playlist (state may already have it).
  let pl = state?.playlist;
  if (!pl) {
    try { pl = await (await fetch("/playlist")).json(); } catch (_) { pl = { entries: [] }; }
  }
  pendingPlaylist = (pl.entries || []).map((e) => ({
    name: e.name, play_seconds: e.play_seconds,
  }));
  renderPlaylistRows();
  $("pl-modal").classList.add("open");
}
function closePlaylistModal() { $("pl-modal").classList.remove("open"); }

function renderPlaylistRows() {
  const list = $("pl-list");
  list.innerHTML = "";
  if (!pendingPlaylist.length) {
    list.innerHTML = `<div class="pl-status">empty — add effects below</div>`;
    return;
  }
  pendingPlaylist.forEach((entry, i) => {
    const row = document.createElement("div");
    row.className = "pl-row";
    row.innerHTML = `
      <span class="idx">#${i + 1}</span>
      <span class="name" title="${escapeAttr(entry.name)}">${escapeAttr(entry.name)}</span>
      <input type="number" min="5" max="3600" step="5" value="${entry.play_seconds}">
      <span class="reorder">
        <button data-act="up" ${i === 0 ? "disabled" : ""}>↑</button>
        <button data-act="dn" ${i === pendingPlaylist.length - 1 ? "disabled" : ""}>↓</button>
      </span>
      <button class="del" data-act="del">×</button>`;
    const num = row.querySelector("input[type=number]");
    num.addEventListener("change", () => {
      const v = Math.max(5, parseFloat(num.value) || 120);
      pendingPlaylist[i].play_seconds = v;
      pushPlaylist();
    });
    row.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === "up" && i > 0) {
        [pendingPlaylist[i - 1], pendingPlaylist[i]] = [pendingPlaylist[i], pendingPlaylist[i - 1]];
      } else if (act === "dn" && i < pendingPlaylist.length - 1) {
        [pendingPlaylist[i + 1], pendingPlaylist[i]] = [pendingPlaylist[i], pendingPlaylist[i + 1]];
      } else if (act === "del") {
        pendingPlaylist.splice(i, 1);
      }
      renderPlaylistRows();
      pushPlaylist();
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
      const r = await fetch("/playlist", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entries: pendingPlaylist }),
      });
      if (!r.ok) $("pl-err").textContent = `save failed: ${await r.text()}`;
      else $("pl-err").textContent = "";
    } catch (e) { $("pl-err").textContent = `save error: ${e}`; }
  }, 80);
}

function updatePlaylistStatus(pl) {
  const banner = $("playlist-banner");
  const status = $("pl-status");
  if (!pl) {
    banner.style.display = "none";
    if (status) status.textContent = "stopped";
    return;
  }
  if (pl.running) {
    const remaining = Math.max(0, (pl.current_total || 0) - (pl.current_elapsed || 0));
    const txt = `▶ ${pl.current_name || "?"} · ${formatSec(remaining)} left`;
    banner.style.display = "inline-block";
    banner.textContent = txt;
    banner.className = "pill ok";
    if (status) status.textContent = `running · ${pl.current_name} · ${formatSec(remaining)} remaining`;
  } else {
    banner.style.display = "none";
    if (status) status.textContent = pl.entries?.length
      ? `stopped (${pl.entries.length} entr${pl.entries.length === 1 ? "y" : "ies"})`
      : "stopped (empty)";
  }
}

function formatSec(s) {
  s = Math.round(s);
  const m = Math.floor(s / 60), r = s % 60;
  return m > 0 ? `${m}:${String(r).padStart(2, "0")}` : `${r}s`;
}

// --- composition deck (single, mode-driven) ---
function focusedSlot() {
  return (state && state.mode === "design") ? "preview" : "live";
}
function focusedComp() {
  if (!state) return null;
  return state[focusedSlot()];
}
function focusedLayer() {
  const comp = focusedComp();
  if (!comp || !comp.layers.length) return null;
  return { slot: focusedSlot(), index: comp.selected, layer: comp.layers[comp.selected] };
}

// While the operator is mid-drag on a layer box, a state push from the
// server would otherwise wipe the row's DOM and cancel the gesture. We
// suppress the deck re-render until the drag releases.
let layerDragActive = false;

// Skip re-render briefly after a layer-action click. State pushes arrive
// at ~30 Hz and would otherwise wipe innerHTML mid-click, which on macOS
// + Chrome cancels the in-flight click event before its handler runs.
let layerActionGuardUntil = 0;

function renderDeck(opts) {
  if (!state) return;
  if (layerDragActive) return;
  // If the user is interacting with a select dropdown (focused element
  // is inside the deck), don't tear it down.
  const ae = document.activeElement;
  if (ae && $("layer-list").contains(ae)) return;
  // `force` lets optimistic-update paths bypass the action-guard window —
  // they want to repaint *now*, not in 500 ms.
  if (!opts?.force && performance.now() < layerActionGuardUntil) return;

  const slot = focusedSlot();
  const comp = state[slot];
  const listEl = $("layer-list");
  const summaryEl = $("comp-summary");
  const titleEl = $("comp-title");
  const headerEl = $("comp-header");
  titleEl.textContent = slot === "live" ? "LIVE composition" : "DESIGN composition";
  headerEl.classList.toggle("live-header", slot === "live");
  headerEl.classList.toggle("design-header", slot === "preview");
  if (!comp || !comp.layers || comp.layers.length === 0) {
    listEl.innerHTML = `<div class="deck-empty">no layers</div>`;
    summaryEl.textContent = "";
    return;
  }
  summaryEl.textContent = `${comp.layers.length} layer(s)`;
  listEl.innerHTML = "";
  comp.layers.forEach((layer, i) => {
    const row = document.createElement("div");
    row.className = "layer-row" + (i === comp.selected ? " selected" : "")
                                 + (layer.enabled ? "" : " disabled");
    row.dataset.idx = String(i);
    row.dataset.slot = slot;
    const opPct = (layer.opacity * 100).toFixed(2) + "%";
    const blendOpts = ["normal", "add", "screen", "multiply"]
      .map((b) => `<option value="${b}"${b === layer.blend ? " selected" : ""}>${b}</option>`)
      .join("");
    const playGlyph = layer.enabled ? "❚❚" : "▶";
    const playTitle = layer.enabled ? "pause this layer" : "resume this layer";
    row.innerHTML = `
      <div class="opacity-fill" style="width:${opPct}"></div>
      <span class="idx">${i}.</span>
      <span class="name" title="${escapeAttr(layer.summary || "")}">${escapeAttr(layer.name)}</span>
      <div class="opacity-num">${layer.opacity.toFixed(2)}</div>
      <div class="layer-actions">
        <button class="enabled-btn" type="button" title="${playTitle}">${playGlyph}</button>
        <select class="layer-blend" title="blend mode">${blendOpts}</select>
        <button class="delete-btn" type="button" title="delete layer">×</button>
      </div>`;
    attachLayerDrag(row, slot, i, layer);
    listEl.appendChild(row);
  });
}

// Event delegation on the layer list — listeners survive innerHTML wipes,
// and they don't depend on closure-captured stale `layer`/`i` values.
//
// The action buttons (pause / delete) and the blend <select> fire on
// `pointerdown` rather than `click`/`change`. Why: `/ws/state` arrives
// at ~24 Hz and renderDeck() rebuilds the row's innerHTML on each push.
// If a state push lands between pointerdown and click, the button DOM
// is wiped before click can fire — the click then dies silently and
// the button feels broken until you rapid-click and one happens to
// land in a quiet window between pushes. Firing on pointerdown closes
// that race entirely.
(function bindLayerListDelegation() {
  const listEl = $("layer-list");

  listEl.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    const btn = e.target.closest(".enabled-btn, .delete-btn");
    if (!btn) return;
    e.stopPropagation();
    e.preventDefault();
    // Belt-and-braces: also freeze re-renders for half a second so the
    // round-trip /ws/state push doesn't tear down the row mid-interaction.
    layerActionGuardUntil = performance.now() + 500;
    const row = btn.closest(".layer-row");
    if (!row) return;
    const i = parseInt(row.dataset.idx, 10);
    const slot = row.dataset.slot;
    const comp = state && state[slot];
    if (!comp || !comp.layers || !comp.layers[i]) return;
    if (btn.classList.contains("enabled-btn")) {
      // Optimistic: flip the local flag and repaint now, then send the
      // PATCH (no 80 ms debounce — discrete actions go straight to wire).
      const next = !comp.layers[i].enabled;
      comp.layers[i].enabled = next;
      renderDeck({ force: true });
      patchMetaNow(slot, i, { enabled: next });
    } else {
      // Optimistic remove: drop the row from local state and repaint, then
      // send the actual delete. The follow-up /ws/state will reconcile any
      // server-side selection changes.
      comp.layers.splice(i, 1);
      if (comp.selected >= comp.layers.length) {
        comp.selected = Math.max(0, comp.layers.length - 1);
      }
      renderDeck({ force: true });
      fetch(`/${slot}/layer/remove`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ index: i }),
      }).catch(() => {});
    }
  });

  // Freeze re-renders the moment the user touches the blend dropdown,
  // before the browser's native open/close animation can race a state push.
  listEl.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".layer-blend")) {
      layerActionGuardUntil = performance.now() + 500;
    }
  });
  listEl.addEventListener("change", (e) => {
    const sel = e.target.closest(".layer-blend");
    if (!sel) return;
    e.stopPropagation();
    layerActionGuardUntil = performance.now() + 500;
    const row = sel.closest(".layer-row");
    if (!row) return;
    const i = parseInt(row.dataset.idx, 10);
    const slot = row.dataset.slot;
    const comp = state && state[slot];
    if (comp && comp.layers && comp.layers[i]) {
      comp.layers[i].blend = sel.value;
    }
    patchMetaNow(slot, i, { blend: sel.value });
  });
})();

// Pointer-driven interaction on a single layer box:
//   - tap (no movement)   → select layer (POST /<slot>/select)
//   - horizontal drag     → scrub opacity 0..1 across the row width,
//                           updating the fill bar and centred .2f number
//                           live, then committing via /<slot>/layer/blend
function attachLayerDrag(row, slot, layerIndex, layer) {
  let startX = 0;
  let startOpacity = layer.opacity;
  let totalDist = 0;
  let active = false;
  let pid = null;
  const fill = row.querySelector(".opacity-fill");
  const num = row.querySelector(".opacity-num");

  row.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    if (e.target.closest(".layer-actions")) return;
    active = true;
    pid = e.pointerId;
    startX = e.clientX;
    startOpacity = layer.opacity;
    totalDist = 0;
    layerDragActive = true;
    try { row.setPointerCapture(pid); } catch (_) {}
    e.preventDefault();
  });
  row.addEventListener("pointermove", (e) => {
    if (!active) return;
    const dx = e.clientX - startX;
    if (Math.abs(dx) > totalDist) totalDist = Math.abs(dx);
    if (totalDist < 3) return;
    const rect = row.getBoundingClientRect();
    const op = Math.max(0, Math.min(1, startOpacity + dx / rect.width));
    layer.opacity = op;
    if (fill) fill.style.width = (op * 100).toFixed(2) + "%";
    if (num) num.textContent = op.toFixed(2);
    patchMeta(slot, layerIndex, { opacity: op });
  });
  function endDrag() {
    if (!active) return;
    if (pid !== null) {
      try { row.releasePointerCapture(pid); } catch (_) {}
    }
    active = false;
    pid = null;
    layerDragActive = false;
    if (totalDist < 3) {
      // Treat as click → activate this layer's params in the side panel.
      fetch(`/${slot}/select`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ index: layerIndex }),
      }).catch(() => {});
    }
  }
  row.addEventListener("pointerup", endDrag);
  row.addEventListener("pointercancel", endDrag);
}

function escapeAttr(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

$("add-layer").addEventListener("click", async () => {
  const slot = focusedSlot();
  const ep = slot === "live" ? "load_live" : "load_preview";
  await fetch(`/effects/pulse_mono/${ep}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ add_layer: true }),
  });
});
// Per-layer × button handles removal; the bottom-bar "remove" button is gone.

// --- layer meta (per-layer controls now live on the layer rows themselves) ---
// Opacity drag sends through the debounced path (pointermove fires at display
// rate; we coalesce into one PATCH every 80 ms to keep the wire quiet).
// Discrete actions (pause/resume, blend change) send through patchMetaNow —
// no debounce, no perceived lag.
let metaTimer = null, pendingMeta = null;
function patchMeta(slot, index, patch) {
  pendingMeta = { slot, index, patch: { ...(pendingMeta?.patch || {}), ...patch } };
  if (metaTimer) return;
  metaTimer = setTimeout(async () => {
    const m = pendingMeta; pendingMeta = null; metaTimer = null;
    await fetch(`/${m.slot}/layer/blend`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ index: m.index, ...m.patch }),
    }).catch(() => {});
  }, 80);
}
function patchMetaNow(slot, index, patch) {
  fetch(`/${slot}/layer/blend`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index, ...patch }),
  }).catch(() => {});
}

// --- params panel ---
function renderParams() {
  const form = $("params-form");
  const f = focusedLayer();
  if (!f || !f.layer) {
    form.innerHTML = `<div class="params-empty">No layer selected.</div>`;
    setText($("side-layer-name"), "—");
    return;
  }
  const perf = f.layer.perf || {};
  const perfTxt = (typeof perf.p95_ms === "number" && perf.p95_ms > 0)
    ? ` · ${perf.p95_ms.toFixed(1)}ms` : "";
  setText($("side-layer-name"), `${f.slot}#${f.index} · ${f.layer.name}${perfTxt}`);

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
    const colorBtn = row.querySelector(".color-btn");
    if (colorBtn) {
      colorBtn.style.background = String(v);
      colorBtn.dataset.color = String(v);
      const swatch = row.querySelector(".value");
      if (swatch) swatch.textContent = String(v);
      continue;
    }
    if (!input || document.activeElement === input) continue;
    if (input.type === "checkbox") input.checked = !!v;
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

  const val = currentValue ?? spec.default;
  let valNode = null;

  if (spec.control === "slider" || spec.control === "int_slider") {
    const ctrl = document.createElement("input");
    ctrl.type = "range";
    ctrl.min = spec.min; ctrl.max = spec.max;
    ctrl.step = spec.step ?? (spec.control === "int_slider" ? 1 : 0.01);
    ctrl.value = val;
    valNode = document.createElement("span");
    valNode.className = "value";
    const intMode = spec.control === "int_slider";
    valNode.textContent = intMode ? String(val) : Number(val).toFixed(2);
    ctrl.addEventListener("input", () => {
      const v = intMode ? parseInt(ctrl.value, 10) : parseFloat(ctrl.value);
      valNode.textContent = intMode ? String(v) : v.toFixed(2);
      onChange(v);
    });
    row.appendChild(ctrl);
    row.appendChild(valNode);
    return row;
  }
  if (spec.control === "color") {
    const btn = document.createElement("div");
    btn.className = "color-btn";
    btn.tabIndex = 0;
    btn.style.background = String(val || "#ffffff");
    btn.dataset.color = String(val || "#ffffff");
    valNode = document.createElement("span");
    valNode.className = "value";
    valNode.textContent = String(val || "#ffffff");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openColorPopover(btn, btn.dataset.color, (hex) => {
        btn.style.background = hex;
        btn.dataset.color = hex;
        valNode.textContent = hex;
        onChange(hex);
      });
    });
    row.appendChild(btn);
    row.appendChild(valNode);
    return row;
  }
  if (spec.control === "toggle") {
    const ctrl = document.createElement("input");
    ctrl.type = "checkbox"; ctrl.checked = !!val;
    ctrl.addEventListener("change", () => onChange(ctrl.checked));
    row.appendChild(ctrl);
    row.appendChild(document.createElement("span"));
    return row;
  }
  if (spec.control === "select" || spec.control === "palette") {
    const ctrl = document.createElement("select");
    for (const opt of (spec.options || [])) {
      const o = document.createElement("option");
      o.value = opt; o.textContent = opt;
      if (opt === val) o.selected = true;
      ctrl.appendChild(o);
    }
    if (spec.control === "palette" && (spec.options || []).length === 0) {
      // Fallback when prompt didn't enumerate; render plain text input.
      const t = document.createElement("input");
      t.type = "text"; t.value = String(val || "");
      t.addEventListener("change", () => onChange(t.value));
      row.appendChild(t);
      row.appendChild(document.createElement("span"));
      return row;
    }
    ctrl.value = String(val);
    ctrl.addEventListener("change", () => onChange(ctrl.value));
    row.appendChild(ctrl);
    row.appendChild(document.createElement("span"));
    return row;
  }
  return row;
}

let paramQueues = {}, paramTimers = {};
function sendParam(slot, layerIndex, key, value) {
  const q = paramQueues[slot] = paramQueues[slot] || {};
  q[key] = value;
  paramQueues[slot]._layer_index = layerIndex;
  if (paramTimers[slot]) return;
  paramTimers[slot] = setTimeout(async () => {
    const queue = paramQueues[slot];
    paramQueues[slot] = null; paramTimers[slot] = null;
    const layer_index = queue._layer_index;
    delete queue._layer_index;
    await fetch(`/${slot}/params`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: queue, layer_index }),
    }).catch(() => {});
  }, 60);
}

// --- color wheel popover (HSV with brightness slider) ---
const colorPop = $("color-popover");
const wheelCanvas = $("color-wheel");
const wheelCtx = wheelCanvas.getContext("2d");
let activeColorTarget = null;
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
      if (dist > r) {
        img.data[idx] = 0; img.data[idx + 1] = 0; img.data[idx + 2] = 0; img.data[idx + 3] = 0;
        continue;
      }
      const hue = ((Math.atan2(dy, dx) * 180 / Math.PI) + 360) % 360;
      const sat = Math.min(1, dist / r);
      const [rr, gg, bb] = hsvToRgb(hue / 360, sat, v);
      img.data[idx] = rr; img.data[idx + 1] = gg; img.data[idx + 2] = bb;
      img.data[idx + 3] = 255;
    }
  }
  wheelCtx.putImageData(img, 0, 0);
}

function hsvToRgb(h, s, v) {
  let r, g, b;
  const i = Math.floor(h * 6);
  const f = h * 6 - i;
  const p = v * (1 - s);
  const q = v * (1 - f * s);
  const t = v * (1 - (1 - f) * s);
  switch (i % 6) {
    case 0: r = v; g = t; b = p; break;
    case 1: r = q; g = v; b = p; break;
    case 2: r = p; g = v; b = t; break;
    case 3: r = p; g = q; b = v; break;
    case 4: r = t; g = p; b = v; break;
    case 5: r = v; g = p; b = q; break;
  }
  return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
}
function rgbToHsv(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h, s, v = max;
  const d = max - min;
  s = max === 0 ? 0 : d / max;
  if (max === min) h = 0;
  else {
    switch (max) {
      case r: h = (g - b) / d + (g < b ? 6 : 0); break;
      case g: h = (b - r) / d + 2; break;
      case b: h = (r - g) / d + 4; break;
    }
    h /= 6;
  }
  return [h, s, v];
}
function hexToRgb(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex.trim());
  if (!m) return [255, 255, 255];
  return [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)];
}
function rgbToHex(r, g, b) {
  const c = (n) => n.toString(16).padStart(2, "0");
  return "#" + c(r) + c(g) + c(b);
}

function updateMarker() {
  const w = wheelCanvas.clientWidth, h = wheelCanvas.clientHeight;
  const cx = w / 2, cy = h / 2, r = Math.min(cx, cy) - 1;
  const angle = colorH * 2 * Math.PI;
  const dist = colorS * r;
  const x = cx + Math.cos(angle) * dist;
  const y = cy + Math.sin(angle) * dist;
  $("color-marker").style.left = x + "px";
  $("color-marker").style.top = y + "px";
}
function applyColorToTarget() {
  const [r, g, b] = hsvToRgb(colorH, colorS, colorV);
  const hex = rgbToHex(r, g, b);
  $("color-hex").value = hex;
  if (activeColorOnChange) activeColorOnChange(hex);
}

function openColorPopover(anchor, currentHex, onChange) {
  activeColorTarget = anchor;
  activeColorOnChange = onChange;
  const [r, g, b] = hexToRgb(currentHex || "#ffffff");
  const [h, s, v] = rgbToHsv(r, g, b);
  colorH = h; colorS = s; colorV = v;
  $("color-v").value = v;
  $("color-hex").value = currentHex;
  drawWheel();
  // Position the popover near the anchor button.
  const rect = anchor.getBoundingClientRect();
  const popW = 224, popH = 290;
  let left = rect.left;
  let top = rect.bottom + 6;
  if (left + popW > window.innerWidth - 8) left = window.innerWidth - popW - 8;
  if (top + popH > window.innerHeight - 8) top = rect.top - popH - 6;
  colorPop.style.left = left + "px";
  colorPop.style.top = top + "px";
  colorPop.classList.add("open");
  requestAnimationFrame(updateMarker);
}
function closeColorPopover() {
  colorPop.classList.remove("open");
  activeColorTarget = null;
  activeColorOnChange = null;
}
$("color-close").addEventListener("click", closeColorPopover);

document.addEventListener("click", (e) => {
  if (!colorPop.classList.contains("open")) return;
  if (colorPop.contains(e.target)) return;
  if (activeColorTarget && activeColorTarget.contains(e.target)) return;
  closeColorPopover();
});

let dragging = false;
function pickFromCanvas(e) {
  const rect = wheelCanvas.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  const cx = rect.width / 2, cy = rect.height / 2;
  const r = Math.min(cx, cy) - 1;
  const dx = x - cx, dy = y - cy;
  const dist = Math.sqrt(dx * dx + dy * dy);
  const sat = Math.min(1, dist / r);
  const hue = ((Math.atan2(dy, dx) * 180 / Math.PI) + 360) % 360;
  colorH = hue / 360; colorS = sat;
  updateMarker();
  applyColorToTarget();
}
wheelCanvas.addEventListener("mousedown", (e) => {
  dragging = true;
  pickFromCanvas(e);
});
window.addEventListener("mousemove", (e) => {
  if (dragging) pickFromCanvas(e);
});
window.addEventListener("mouseup", () => { dragging = false; });
$("color-v").addEventListener("input", (e) => {
  colorV = parseFloat(e.target.value);
  drawWheel();
  applyColorToTarget();
});
$("color-hex").addEventListener("change", (e) => {
  const v = e.target.value.trim();
  if (!/^#?[a-f0-9]{6}$/i.test(v)) return;
  const [r, g, b] = hexToRgb(v);
  const [h, s, vv] = rgbToHsv(r, g, b);
  colorH = h; colorS = s; colorV = vv;
  $("color-v").value = vv;
  drawWheel(); updateMarker();
  if (activeColorOnChange) activeColorOnChange(rgbToHex(r, g, b));
});

// --- chat ---
$("chat-send").addEventListener("click", sendChat);
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault(); sendChat();
  }
});

// Hard cap on a chat round-trip. If the LLM hasn't responded in this many
// ms, abort the fetch and re-enable the send button so the operator can
// retry. Default OpenRouter calls usually complete in 4–10 s; >20 s means
// something's wrong (rate limit, network, model stalled).
const CHAT_TIMEOUT_MS = 20000;

let chatBusy = false;

function appendThinking() {
  const el = document.createElement("div");
  el.className = "msg thinking";
  el.innerHTML = `<div class="label">agent</div>thinking<span class="dots"></span>`;
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}

async function sendChat() {
  if (chatBusy) return;
  const input = $("chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  appendMsg("user", message);

  chatBusy = true;
  const sendBtn = $("chat-send");
  sendBtn.disabled = true;
  sendBtn.textContent = "…";
  input.disabled = true;
  const thinkingEl = appendThinking();

  const ctrl = new AbortController();
  const timeoutId = setTimeout(() => ctrl.abort(), CHAT_TIMEOUT_MS);

  let timedOut = false;
  try {
    const r = await fetch("/agent/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: sessionId }),
      signal: ctrl.signal,
    });
    if (!r.ok) {
      appendMsg("tool-error", `chat failed: ${r.status} ${await r.text()}`);
      return;
    }
    const body = await r.json();
    sessionId = body.session_id;
    if (body.assistant_text) appendMsg("assistant", body.assistant_text, body.usage);
    if (body.tool_call) {
      const tr = body.tool_result || {};
      const ok = tr.ok;
      const args = body.tool_call.arguments || {};
      const name = args.name || body.tool_call.name;
      if (ok) {
        appendToolOk(name, args.code || "", args.summary || "");
      } else {
        const detail = JSON.stringify(tr.details ?? tr.error ?? tr);
        appendMsg("tool-error", `❌ ${tr.error || "tool error"}\n${detail}`);
      }
    }
    if (!body.assistant_text && body.usage) appendUsage(body.usage);
  } catch (e) {
    if (e && e.name === "AbortError") {
      timedOut = true;
      appendMsg("tool-error",
        `chat timed out after ${(CHAT_TIMEOUT_MS / 1000).toFixed(0)}s — LLM didn't respond. Try again.`);
    } else {
      appendMsg("tool-error", `chat error: ${e}`);
    }
  } finally {
    clearTimeout(timeoutId);
    if (thinkingEl.parentNode) thinkingEl.parentNode.removeChild(thinkingEl);
    chatBusy = false;
    sendBtn.disabled = false;
    sendBtn.textContent = "send";
    input.disabled = false;
    if (!timedOut) input.focus();
  }
}

function appendToolOk(name, code, summary) {
  // Render a clickable "wrote effect" pill — clicking opens the code viewer.
  const el = document.createElement("div");
  el.className = "msg tool-ok clickable";
  el.textContent = `↪ wrote effect: ${name}  (click to view code)`;
  if (code) {
    el.addEventListener("click", () => openCodeModal(name, code, summary));
  }
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
}

// ---- code-viewer modal ---- //
function openCodeModal(name, code, summary) {
  $("code-modal-title").textContent = `Effect source — ${name}`;
  const meta = $("code-meta");
  meta.innerHTML = "";
  const nameEl = document.createElement("span");
  nameEl.className = "name"; nameEl.textContent = name;
  meta.appendChild(nameEl);
  if (summary) {
    const sumEl = document.createElement("span");
    sumEl.textContent = `· ${summary}`;
    meta.appendChild(sumEl);
  }
  const lines = code.split("\n").length;
  const sizeEl = document.createElement("span");
  sizeEl.textContent = `· ${lines} lines · ${code.length} bytes`;
  meta.appendChild(sizeEl);
  $("code-content").innerHTML = highlightPython(code);
  $("code-modal").classList.add("open");
  $("code-modal").dataset.code = code;
}
function closeCodeModal() { $("code-modal").classList.remove("open"); }
$("code-close").addEventListener("click", closeCodeModal);
$("code-modal").addEventListener("click", (e) => {
  if (e.target.id === "code-modal") closeCodeModal();
});
$("code-copy").addEventListener("click", async () => {
  const code = $("code-modal").dataset.code || "";
  try {
    await navigator.clipboard.writeText(code);
    const btn = $("code-copy");
    const prev = btn.textContent;
    btn.textContent = "copied ✓";
    setTimeout(() => { btn.textContent = prev; }, 900);
  } catch (_) {}
});

// Tiny Python tokenizer for the code viewer. Not perfect (string parsing
// can't handle every edge case) but good enough to read effect source at
// a glance: keywords / strings / comments / numbers / decorators / def +
// class names / common builtins.
const PY_KEYWORDS = new Set([
  "and","as","assert","async","await","break","class","continue","def","del",
  "elif","else","except","finally","for","from","global","if","import","in","is",
  "lambda","nonlocal","not","or","pass","raise","return","try","while","with","yield",
  "True","False","None",
]);
const PY_BUILTINS = new Set([
  "np","Effect","hex_to_rgb","hsv_to_rgb","lerp","clip01","gauss","pulse","tri",
  "wrap_dist","palette_lerp","named_palette","rng","log","PI","TAU","LUT_SIZE",
  "PALETTE_NAMES","int","float","str","bool","range","len","min","max","abs",
  "round","sum","print","getattr","setattr","hasattr","isinstance","tuple","list",
  "dict","set",
]);
function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
}
function highlightPython(src) {
  // Walk char-by-char, emitting <span class=tok-…> for each lexed token.
  let i = 0; const n = src.length;
  let out = "";
  function isIdStart(c) { return /[A-Za-z_]/.test(c); }
  function isIdCont(c)  { return /[A-Za-z0-9_]/.test(c); }
  // Track `def `/`class ` so we can colour the next identifier specially.
  let pendingNameKind = null;
  while (i < n) {
    const c = src[i];
    // Comment
    if (c === "#") {
      let j = i;
      while (j < n && src[j] !== "\n") j++;
      out += `<span class="tok-com">${escapeHtml(src.slice(i, j))}</span>`;
      i = j; continue;
    }
    // Triple-quoted strings
    if ((c === '"' || c === "'") && src.slice(i, i + 3) === c.repeat(3)) {
      const q = c.repeat(3);
      const end = src.indexOf(q, i + 3);
      const j = end < 0 ? n : end + 3;
      out += `<span class="tok-str">${escapeHtml(src.slice(i, j))}</span>`;
      i = j; continue;
    }
    // Single-line strings
    if (c === '"' || c === "'") {
      let j = i + 1;
      while (j < n && src[j] !== c && src[j] !== "\n") {
        if (src[j] === "\\" && j + 1 < n) j += 2; else j += 1;
      }
      if (j < n && src[j] === c) j += 1;
      out += `<span class="tok-str">${escapeHtml(src.slice(i, j))}</span>`;
      i = j; continue;
    }
    // Decorator
    if (c === "@" && i + 1 < n && isIdStart(src[i + 1])) {
      let j = i + 1;
      while (j < n && (isIdCont(src[j]) || src[j] === ".")) j++;
      out += `<span class="tok-deco">${escapeHtml(src.slice(i, j))}</span>`;
      i = j; continue;
    }
    // Number
    if (/[0-9]/.test(c) || (c === "." && i + 1 < n && /[0-9]/.test(src[i + 1]))) {
      let j = i;
      while (j < n && /[0-9.eE_+\-xXabcdefABCDEF]/.test(src[j])) {
        // Loose match; stops at obvious non-numeric chars.
        if ((src[j] === "+" || src[j] === "-") &&
            !(j > i && (src[j - 1] === "e" || src[j - 1] === "E"))) break;
        j++;
      }
      out += `<span class="tok-num">${escapeHtml(src.slice(i, j))}</span>`;
      i = j; continue;
    }
    // Identifier / keyword
    if (isIdStart(c)) {
      let j = i + 1;
      while (j < n && isIdCont(src[j])) j++;
      const word = src.slice(i, j);
      let cls = null;
      if (PY_KEYWORDS.has(word)) {
        cls = "tok-kw";
        if (word === "def") pendingNameKind = "def";
        else if (word === "class") pendingNameKind = "cls";
      } else if (pendingNameKind) {
        cls = pendingNameKind === "cls" ? "tok-cls" : "tok-def";
        pendingNameKind = null;
      } else if (word === "self" || word === "cls") {
        cls = "tok-self";
      } else if (PY_BUILTINS.has(word)) {
        cls = "tok-bi";
      }
      out += cls
        ? `<span class="${cls}">${escapeHtml(word)}</span>`
        : escapeHtml(word);
      i = j; continue;
    }
    // Default: single char passthrough (whitespace / punctuation)
    out += escapeHtml(c);
    i += 1;
  }
  // Prepend line numbers.
  const lines = out.split("\n");
  const pad = String(lines.length).length;
  return lines.map((line, idx) =>
    `<span class="ln">${String(idx + 1).padStart(pad, " ")}</span>${line}`
  ).join("\n");
}

function appendMsg(kind, text, usage) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  if (kind === "user") el.innerHTML = `<div class="label">you</div>${escapeAttr(text)}`;
  else if (kind === "assistant") el.innerHTML = `<div class="label">agent</div>${escapeAttr(text)}`;
  else el.textContent = text;
  if (usage) {
    const u = document.createElement("div");
    u.className = "usage";
    u.textContent = formatUsage(usage);
    el.appendChild(u);
  }
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
}
function appendUsage(usage) {
  const el = document.createElement("div");
  el.className = "usage-line";
  el.textContent = formatUsage(usage);
  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
}
function formatUsage(u) {
  const inT = u.prompt_tokens ?? u.input_tokens ?? 0;
  const outT = u.completion_tokens ?? u.output_tokens ?? 0;
  const total = u.total_tokens ?? (inT + outT);
  return `tokens · in ${inT} · out ${outT} · total ${total}`;
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

// --- audio HUD ---
// All four meters render on requestAnimationFrame so the bars feel as smooth
// as the browser refresh, regardless of how often /ws/state arrives (now
// throttled to sim_fps, default 24 Hz). Targets land via updateAudioHud()
// when a state push arrives; the RAF loop lerps current values toward them.
let lastBeatCount = 0, beatLevel = 0, audioLastTickWall = 0;
// Tau-equivalent half-life: τ = halflife / ln(2). 100ms → τ≈144ms (< 200ms).
const BEAT_DECAY_HALFLIFE_MS = 100;
// Band smoothing time constant. Short half-life (~30 ms) means the meter has
// effectively reached the next /ws/state push by the time it lands at 24 Hz —
// no perceptible lag, but the eye sees motion every frame instead of in steps.
const BAND_HALFLIFE_MS = 30;
const audioTargets = { low: 0, mid: 0, high: 0 };
const audioCurrent = { low: 0, mid: 0, high: 0 };
let audioConnected = false;

function updateAudioHud(a) {
  const hud = $("audio-hud");
  if (!a || !a.connected) {
    hud.classList.add("disconnected");
    audioConnected = false;
    audioTargets.low = audioTargets.mid = audioTargets.high = 0;
    // Numeric labels — fine to update at WS rate, no smoothing needed.
    setText($("num-low"), "0.00");
    setText($("num-mid"), "0.00");
    setText($("num-high"), "0.00");
    setText($("num-beat"), "0");
    setText($("audio-bpm"), "—");
    lastBeatCount = (a && a.beat_count) || 0;
    return;
  }
  hud.classList.remove("disconnected");
  audioConnected = true;
  audioTargets.low  = a.low  || 0;
  audioTargets.mid  = a.mid  || 0;
  audioTargets.high = a.high || 0;
  setText($("num-low"),  audioTargets.low.toFixed(2));
  setText($("num-mid"),  audioTargets.mid.toFixed(2));
  setText($("num-high"), audioTargets.high.toFixed(2));
  setText($("audio-bpm"), a.bpm != null ? `${a.bpm.toFixed(0)}` : "—");
  if (typeof a.beat_count === "number") {
    if (a.beat_count > lastBeatCount) beatLevel = 1.0;
    lastBeatCount = a.beat_count;
    setText($("num-beat"), String(a.beat_count));
  }
}

function tickAudioHud() {
  const now = performance.now();
  if (audioLastTickWall === 0) audioLastTickWall = now;
  const dt = now - audioLastTickWall;
  audioLastTickWall = now;

  // Bands: exponential approach toward target. `alpha` is the fraction of
  // remaining error to close this frame — derived from a half-life so the
  // smoothing is framerate-independent (works the same at 60 Hz / 120 Hz).
  if (audioConnected) {
    const alpha = 1.0 - Math.pow(0.5, dt / BAND_HALFLIFE_MS);
    audioCurrent.low  += (audioTargets.low  - audioCurrent.low)  * alpha;
    audioCurrent.mid  += (audioTargets.mid  - audioCurrent.mid)  * alpha;
    audioCurrent.high += (audioTargets.high - audioCurrent.high) * alpha;
  } else {
    // Snap to zero on disconnect so the bar doesn't decay slowly while
    // the "disconnected" pill is showing.
    audioCurrent.low = audioCurrent.mid = audioCurrent.high = 0;
  }
  setMeter($("meter-low"),  audioCurrent.low);
  setMeter($("meter-mid"),  audioCurrent.mid);
  setMeter($("meter-high"), audioCurrent.high);

  // Beat: kicked to 1.0 by updateAudioHud on each new onset, then decays.
  if (beatLevel > 0) {
    beatLevel *= Math.pow(0.5, dt / BEAT_DECAY_HALFLIFE_MS);
    if (beatLevel < 0.005) beatLevel = 0;
  }
  setMeter($("meter-beat"), beatLevel);

  requestAnimationFrame(tickAudioHud);
}
requestAnimationFrame(tickAudioHud);

function setMeter(el, v) {
  if (!el) return;
  const pct = Math.max(0, Math.min(100, (v || 0) * 100));
  // 0.1% precision. At 1800 LEDs of viz pixels, the eye can pick up sub-pixel
  // motion; integer-% (the old behaviour) made small fluctuations look stuck
  // between /ws/state pushes. CSS handles fractional widths fine.
  const next = `${pct.toFixed(1)}%`;
  if (el.dataset.w !== next) { el.style.width = next; el.dataset.w = next; }
}

function applyDdp(ddp) {
  const btn = $("btn-ddp");
  if (!btn) return;
  if (performance.now() - ddpEditAt < 600) return;
  if (!ddp || !ddp.available) {
    btn.disabled = true; btn.classList.remove("danger"); btn.classList.add("ghost");
    setText(btn, "no DDP"); return;
  }
  btn.disabled = false;
  const piOn = !ddp.paused;
  btn.classList.toggle("danger", !piOn);
  btn.classList.toggle("ghost", piOn);
  setText(btn, piOn ? "Pi control" : "Gledopto");
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
  $("btn-promote").style.display = (state.mode === "design") ? "" : "none";

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
  if (typeof state.target_fps === "number"
      && document.activeElement !== $("m-engfps")) {
    const idx = nearestIdx(ENGINE_FPS_VALUES, state.target_fps);
    $("m-engfps").value = String(idx);
    setText($("v-engfps"), String(ENGINE_FPS_VALUES[idx]));
  }
  if (typeof state.sim_fps === "number"
      && document.activeElement !== $("m-simfps")) {
    const idx = nearestIdx(SIM_FPS_VALUES, state.sim_fps);
    $("m-simfps").value = String(idx);
    setText($("v-simfps"), String(SIM_FPS_VALUES[idx]));
  }

  if (state.blackout) {
    $("btn-blackout").classList.add("danger"); $("btn-blackout").classList.remove("ghost");
    setText($("btn-blackout"), "● BLACKOUT");
  } else {
    $("btn-blackout").classList.remove("danger"); $("btn-blackout").classList.add("ghost");
    setText($("btn-blackout"), "⚫ blackout");
  }
  applyDdp(state.ddp);
  updatePlaylistStatus(state.playlist);

  viz.applyCalibration(state.calibration);
  renderDeck();
  renderParams();
}

connectStateStream();
fetch("/state").then((r) => r.json()).then((s) => {
  state = s; applyState(); maybeRestoreMode();
});
