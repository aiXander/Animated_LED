// MIDI controller view, shared by the desktop modal and the mobile sheet: a
// drawing of the Akai APC mini that shows what every control is mapped to,
// explains it on hover, and is also where the mapping is edited.
//
// The drawing and the picker come from `GET /midi` (fetched when the view
// opens and again whenever `state.midi.rev` moves); lights, fader positions
// and the control touched last come from `state.midi` on every state tick.
// A change goes out as `PUT /midi/bindings` the moment it is picked.

import { setText } from "./util.js";

const TIP_DELAY_MS = 500;
const GROUP_ORDER = ["Effects", "Masters", "Master effects", "WZRD input", "Actions"];
// Light value -> class (pads: 1 green, 2 green blink, 3 red, 5 yellow; buttons: 1 on).
const PAD_LIGHT = { 1: "lit-g", 2: "lit-g blink", 3: "lit-r", 5: "lit-y" };

const CSS = `
.modal.mk-modal { max-width: 47rem; width: min(47rem, 96vw); max-height: 94vh; overflow-y: auto; }
.mk-panel { display: flex; flex-direction: column; gap: 0.6rem; font-size: 0.8rem; color: var(--dim);
  container-type: inline-size; }
.mk-head { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.mk-head .mk-hint { flex: 1 1 14rem; font-size: 0.74rem; line-height: 1.35; }
.mk-link { color: var(--text); white-space: nowrap; }
.mk-dot { display: inline-block; width: 0.55rem; height: 0.55rem; border-radius: 50%;
  background: var(--dim); margin-right: 0.35rem; }
.mk-dot.on { background: var(--ok); }
.mk-device { --gap: clamp(3px, 0.9cqw, 7px); display: grid; gap: var(--gap);
  grid-template-columns: repeat(9, minmax(0, 1fr));
  background: #17181c; border: 1px solid #33353d; border-radius: 10px;
  padding: clamp(6px, 1.8cqw, 14px); user-select: none; -webkit-user-select: none; }
.mk-c { position: relative; margin: 0; padding: 1px 2px; min-width: 0; font: inherit; cursor: pointer;
  display: flex; align-items: center; justify-content: center; text-align: center;
  color: #6b6f7c; background: #0b0c0f; border: 1px solid #2c2e36; border-radius: 4px;
  font-size: clamp(0.42rem, 1.55cqw, 0.66rem); line-height: 1.08; overflow: hidden; }
.mk-c span { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3;
  overflow: hidden; overflow-wrap: anywhere; }
.mk-c.pad { aspect-ratio: 1.7; }
.mk-c.round { aspect-ratio: 1.7; border-radius: 999px; margin: 8% 3%; }
.mk-c.round span { display: block; white-space: nowrap; text-overflow: ellipsis; }
.mk-c.bound { color: var(--text); border-color: #565a68; background: #15171d; }
.mk-c.reserved { cursor: help; color: #5b5e6a; border-style: dashed; background: transparent; }
.mk-c.lit-y { background: #5a4708; border-color: #d9a80f; color: #ffe9a3; }
.mk-c.lit-g { background: #0d4a22; border-color: #33d06a; color: #c9ffdb; }
.mk-c.lit-r { background: #571313; border-color: #f06060; color: #ffd0d0; }
.mk-c.blink { animation: mk-blink 0.5s steps(2, start) infinite; }
@keyframes mk-blink { to { filter: brightness(0.45); } }
.mk-c.sel { outline: 2px solid var(--accent); outline-offset: 1px; }
.mk-c.touched { box-shadow: 0 0 0 2px var(--warn); }
.mk-c.fader { flex-direction: column; justify-content: flex-start; gap: 3px; padding: 4px 2px;
  height: clamp(4.8rem, 19cqw, 7rem); }
.mk-track { position: relative; flex: 1 1 auto; width: 26%; min-width: 7px; border-radius: 3px;
  background: #050505; border: 1px solid #3a3d48; --pos: 0; --val: 0; }
.mk-thumb { position: absolute; left: -85%; right: -85%; height: 9px; margin-bottom: -5px;
  bottom: calc(var(--pos) * 100%); border-radius: 2px; background: #9a9eab; border: 1px solid #1c1d22; }
.mk-track.unknown .mk-thumb { opacity: 0.25; }
.mk-tick { position: absolute; left: -130%; right: -130%; height: 2px; margin-bottom: -1px;
  bottom: calc(var(--val) * 100%); background: var(--ok); display: none; }
.mk-c.bound .mk-tick { display: block; }
.mk-tip { position: fixed; z-index: 400; max-width: 17rem; padding: 0.45rem 0.6rem; pointer-events: none;
  background: #1d1f26; color: var(--text); border: 1px solid #50535f; border-radius: 5px;
  font-size: 0.76rem; line-height: 1.4; box-shadow: 0 6px 22px rgba(0,0,0,0.6); display: none; }
.mk-tip b { display: block; margin-bottom: 0.15rem; }
.mk-tip i { color: var(--dim); font-style: normal; }
.mk-edit { border: 1px solid var(--line-bright); border-radius: 6px; padding: 0.6rem 0.7rem;
  display: flex; flex-direction: column; gap: 0.45rem; min-height: 5.6rem; }
.mk-edit .mk-title { color: var(--text); font-weight: 600; }
.mk-edit .mk-title i { color: var(--dim); font-style: normal; font-weight: 400; margin-left: 0.4rem; }
.mk-edit select { width: 100%; font: inherit; color: var(--text); background: #050505;
  border: 1px solid #4a4d5a; border-radius: 4px; padding: 0.35rem 0.4rem; }
.mk-edit .mk-help { color: var(--text); line-height: 1.45; }
.mk-err { color: var(--bad); font-size: 0.74rem; min-height: 1rem; }
.mk-foot { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }
.mk-foot .mk-note { flex: 1 1 10rem; font-size: 0.72rem; }
.mk-legend { display: flex; gap: 0.9rem; flex-wrap: wrap; font-size: 0.72rem; }
.mk-legend span::before { content: ""; display: inline-block; width: 0.6rem; height: 0.6rem;
  border-radius: 2px; margin-right: 0.3rem; vertical-align: -1px; background: var(--sw); }
`;

let info = null;            // the last GET /midi answer
let loadedRev = -1;         // the mapping revision `info` belongs to
let fetching = false;
let selected = null;        // hardware id of the control in the editor
let lastSeq = 0;            // newest "touched" event already shown
let touchedTimer = null;
const nodes = {};           // hardware id -> { el, label, track }
let root = null, deviceEl = null, editEl = null, errEl = null, noteEl = null,
  linkEl = null, dotEl = null, tipEl = null, resetBtn = null, tipTimer = null;

const visible = () => !!root && root.offsetParent !== null;
const controlOf = (hw) => info?.device.controls.find((c) => c.hw === hw);
const targetOf = (hw) => {
  const id = info?.bindings[hw];
  return id ? info.targets.find((t) => t.id === id) : null;
};
const pretty = (label) => label.replace(/_/g, " ");

async function load() {
  if (fetching) return;
  fetching = true;
  try {
    const r = await fetch("/midi");
    if (r.ok) setInfo(await r.json());
  } catch (_) { /* offline: the next state tick tries again */ }
  fetching = false;
}

function setInfo(next) {
  info = next;
  loadedRev = next.rev;
  if (!deviceEl.childElementCount) buildDevice();
  for (const c of info.device.controls) {
    const n = nodes[c.hw];
    const t = targetOf(c.hw);
    n.el.classList.toggle("bound", !!t);
    setText(n.label, c.reserved ? "WZRD" : t ? pretty(t.label) : "");
  }
  setText(noteEl, info.custom
    ? "Your own mapping, saved in config/midi.yaml."
    : "The preset mapping. It is saved as your own the first time you change a control.");
  renderEditor();
}

function buildDevice() {
  for (const c of info.device.controls) {
    const el = document.createElement("button");
    el.type = "button";
    const shape = c.kind === "fader" ? "fader" : c.led === "pad" ? "pad" : "round";
    el.className = `mk-c ${shape}${c.reserved ? " reserved" : ""}`;
    el.style.gridColumn = String(c.at[0] + 1);
    el.style.gridRow = String(c.at[1] + 1);
    let track = null;
    if (c.kind === "fader") {
      track = document.createElement("div"); track.className = "mk-track unknown";
      const thumb = document.createElement("div"); thumb.className = "mk-thumb";
      const tick = document.createElement("div"); tick.className = "mk-tick";
      track.append(tick, thumb);
      el.appendChild(track);
    }
    const label = document.createElement("span");
    el.appendChild(label);
    el.addEventListener("click", () => select(c.hw));
    el.addEventListener("pointerenter", (e) => { if (e.pointerType === "mouse") armTip(c.hw, el); });
    el.addEventListener("pointerleave", hideTip);
    deviceEl.appendChild(el);
    nodes[c.hw] = { el, label, track };
  }
}

// ---- tooltip: what this control does, after half a second of hovering ----

function describe(hw) {
  const c = controlOf(hw);
  const t = targetOf(hw);
  if (c.reserved) return { title: c.label, sub: "", text: c.reserved };
  if (!t) return { title: c.label, sub: "not mapped", text: "Click to give it a job." };
  return { title: c.label, sub: t.name, text: t.help };
}

function armTip(hw, el) {
  hideTip();
  tipTimer = setTimeout(() => {
    const d = describe(hw);
    tipEl.replaceChildren();
    const b = document.createElement("b"); b.textContent = d.title;
    if (d.sub) { const i = document.createElement("i"); i.textContent = `  ${d.sub}`; b.appendChild(i); }
    tipEl.append(b, d.text);
    tipEl.style.display = "block";
    const r = el.getBoundingClientRect(), t = tipEl.getBoundingClientRect();
    const left = Math.max(6, Math.min(window.innerWidth - t.width - 6, r.left + r.width / 2 - t.width / 2));
    const above = r.top - t.height - 8;
    tipEl.style.left = `${left}px`;
    tipEl.style.top = `${above >= 6 ? above : r.bottom + 8}px`;
  }, TIP_DELAY_MS);
}

function hideTip() {
  clearTimeout(tipTimer);
  if (tipEl) tipEl.style.display = "none";
}

// ---- editor: pick what the selected control does ----

function select(hw) {
  selected = hw;
  for (const [id, n] of Object.entries(nodes)) n.el.classList.toggle("sel", id === hw);
  setText(errEl, "");
  renderEditor();
}

function renderEditor() {
  editEl.replaceChildren();
  const c = selected && controlOf(selected);
  if (!c) {
    const p = document.createElement("div");
    p.textContent = "Click a control in the drawing, or touch one on the controller, to see and change what it does.";
    editEl.appendChild(p);
    return;
  }
  const title = document.createElement("div"); title.className = "mk-title";
  title.textContent = c.label;
  const where = document.createElement("i");
  where.textContent = c.hw.replace("note:", "note ").replace("cc:", "CC ");
  title.appendChild(where);
  const help = document.createElement("div"); help.className = "mk-help";
  if (c.reserved) {
    help.textContent = `${c.reserved} It stays with WZRD, so it cannot be mapped here.`;
    editEl.append(title, help);
    return;
  }
  const pick = document.createElement("select");
  pick.setAttribute("aria-label", `What ${c.label} does`);
  const none = document.createElement("option"); none.value = ""; none.textContent = "— nothing —";
  pick.appendChild(none);
  const groups = {};
  for (const t of info.targets) {
    if (t.kind !== c.kind) continue;
    (groups[t.group] ||= []).push(t);
  }
  const names = Object.keys(groups).sort((a, b) => GROUP_ORDER.indexOf(a) - GROUP_ORDER.indexOf(b));
  for (const g of names) {
    const og = document.createElement("optgroup"); og.label = g;
    for (const t of groups[g]) {
      const o = document.createElement("option"); o.value = t.id; o.textContent = t.name;
      og.appendChild(o);
    }
    pick.appendChild(og);
  }
  const current = targetOf(c.hw);
  pick.value = current ? current.id : "";
  help.textContent = current ? current.help : "Not mapped: this control does nothing in the LEDs mode.";
  pick.addEventListener("change", () => bind(c.hw, pick.value || null));
  editEl.append(title, pick, help);
}

async function send(path, method, body) {
  setText(errEl, "");
  try {
    const r = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) setInfo(data);
    else { setText(errEl, `not saved: ${data.detail || r.status}`); renderEditor(); }
  } catch (_) {
    setText(errEl, "not saved (offline)");
    renderEditor();
  }
}

const bind = (hw, target) => send("/midi/bindings", "PUT", { bindings: { [hw]: target } });

// Build the view into `container` (once per page).
export function mountMidiPanel(container) {
  if (!document.getElementById("mk-style")) {
    const style = document.createElement("style");
    style.id = "mk-style"; style.textContent = CSS;
    document.head.appendChild(style);
  }
  root = document.createElement("div"); root.className = "mk-panel";

  const head = document.createElement("div"); head.className = "mk-head";
  linkEl = document.createElement("span"); linkEl.className = "mk-link";
  dotEl = document.createElement("span"); dotEl.className = "mk-dot";
  const linkText = document.createElement("span");
  linkEl.append(dotEl, linkText);
  linkEl.text = linkText;
  const hint = document.createElement("span"); hint.className = "mk-hint";
  hint.textContent = "Hover a control to read what it does. Click or tap it, or touch it on the controller, to change it.";
  head.append(linkEl, hint);

  deviceEl = document.createElement("div"); deviceEl.className = "mk-device";

  const legend = document.createElement("div"); legend.className = "mk-legend";
  for (const [colour, text] of [["#33d06a", "live / on"], ["#d9a80f", "effect ready"],
    ["#f06060", "would not load"], ["#565a68", "mapped"]]) {
    const s = document.createElement("span"); s.style.setProperty("--sw", colour); s.textContent = text;
    legend.appendChild(s);
  }

  editEl = document.createElement("div"); editEl.className = "mk-edit";
  errEl = document.createElement("div"); errEl.className = "mk-err";

  const foot = document.createElement("div"); foot.className = "mk-foot";
  noteEl = document.createElement("span"); noteEl.className = "mk-note";
  resetBtn = document.createElement("button"); resetBtn.type = "button"; resetBtn.className = "ghost";
  const resetLabel = "reset to preset";
  resetBtn.textContent = resetLabel;
  let armed = null;
  resetBtn.addEventListener("click", () => {
    if (!armed) {   // first click asks, second click within 4 s does it
      resetBtn.textContent = "really reset every control?";
      armed = setTimeout(() => { armed = null; resetBtn.textContent = resetLabel; }, 4000);
      return;
    }
    clearTimeout(armed); armed = null; resetBtn.textContent = resetLabel;
    send("/midi/reset", "POST");
  });
  foot.append(noteEl, resetBtn);

  root.append(head, deviceEl, legend, editEl, errEl, foot);
  container.appendChild(root);

  tipEl = document.createElement("div"); tipEl.className = "mk-tip";
  document.body.appendChild(tipEl);
  renderEditor();
}

// Refresh from `state.midi`. Returns { connected } for the page's own button,
// or null when the server has no MIDI module.
export function applyMidi(m) {
  if (!m) return null;
  const connected = (m.clients || 0) > 0;
  if (!visible()) {   // closed: remember the newest touch so opening does not replay it
    hideTip();
    if (m.last) lastSeq = m.last.seq;
    return { connected };
  }
  if (m.rev !== loadedRev) load();
  if (!info) return { connected };

  if (dotEl.className !== (connected ? "mk-dot on" : "mk-dot")) dotEl.className = connected ? "mk-dot on" : "mk-dot";
  setText(linkEl.text, connected ? "controller connected" : "no controller connected");

  const lights = m.lights || {}, faders = m.faders || {}, values = m.values || {};
  for (const c of info.device.controls) {
    const n = nodes[c.hw];
    if (c.kind === "fader") {
      const known = c.hw in faders;
      n.track.classList.toggle("unknown", !known);
      n.track.style.setProperty("--pos", known ? faders[c.hw] : 0);
      n.track.style.setProperty("--val", values[c.hw] ?? 0);
      continue;
    }
    const v = lights[c.hw] || 0;
    const want = c.led === "pad" ? (PAD_LIGHT[v] || "") : (v ? "lit-g" : "");
    if (n.lit !== want) {
      for (const cls of ["lit-g", "lit-y", "lit-r", "blink"]) n.el.classList.remove(cls);
      for (const cls of want.split(" ").filter(Boolean)) n.el.classList.add(cls);
      n.lit = want;
    }
  }

  // A control touched on the hardware is found for you: flashed and selected.
  const last = m.last;
  if (last && last.seq !== lastSeq) {
    lastSeq = last.seq;
    const n = nodes[last.hw];
    if (n && (last.pos > 0 || last.hw.startsWith("cc:"))) {
      for (const o of Object.values(nodes)) o.el.classList.remove("touched");
      n.el.classList.add("touched");
      clearTimeout(touchedTimer);
      touchedTimer = setTimeout(() => n.el.classList.remove("touched"), 900);
      if (selected !== last.hw) select(last.hw);
    }
  }
  return { connected };
}
