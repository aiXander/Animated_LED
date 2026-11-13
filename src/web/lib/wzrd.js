// WZRD input panel, shared by the desktop modal and the mobile sheet.
//
// One binding table drives both front-ends, so they cannot drift apart: each
// page only supplies a container, a way to open it, and somewhere to show the
// one-word link summary. Settings go out as `PATCH /wzrd` on a 60 ms debounce
// (like `sendMaster`); controls refresh from `state.wzrd` on every state tick,
// skipping the one the operator is touching.

import { setText } from "./util.js";

// [key, label, min, max, step, tooltip]
const SLIDERS_LOOK = [
  ["colour", "colour", 0, 1, 0.01, "How far the LED colours move to the wall's colours."],
  ["spread", "spread", 0, 1, 0.01,
    "How much of the wall's palette is laid out along the strips. 0 = the effect's own hue picks the wall colour."],
  ["follow_level", "follow level", 0, 1, 0.01, "How far the LEDs dim when the wall is dark."],
  ["level_full", "full at level", 0.02, 1, 0.01,
    "The wall level that counts as fully lit. Read it off 'wall level' below while typical content plays."],
];
const SLIDERS_TIME = [
  ["colour_smoothing_s", "colour smooth", 0, 5, 0.05, "Seconds for the LED colours to follow a change on the wall."],
  ["level_attack_s", "level attack", 0, 1, 0.01, "0 = the LEDs flash on the same frame the wall does."],
  ["level_release_s", "level release", 0, 3, 0.01, "Seconds for the LEDs to fall back after a flash."],
];
// [key, label, tooltip]
const SWITCH_LISTEN = [["listen", "listen to WZRD", "Off = packets still show below, but the LEDs are untouched."]];
const SWITCHES_COUPLE = [
  ["couple_brightness", "follow WZRD brightness", "Multiply by WZRD's master brightness (its BLACKOUT and FLASH included)."],
  ["couple_saturation", "follow WZRD saturation", "Multiply by WZRD's master saturation."],
];
// [id, label]
const STATUS = [
  ["link", "signal"], ["level", "wall level"], ["bri", "WZRD brightness"], ["sat", "WZRD saturation"],
  ["gain", "LED gain"], ["pps", "packets / s"], ["gap", "largest gap"], ["drop", "dropped"],
];

const CSS = `
.wz-panel { display: flex; flex-direction: column; gap: 0.55rem; font-size: 0.8rem; color: var(--dim); }
.wz-panel .wz-row { font-size: inherit; color: inherit; display: grid; grid-template-columns: 7.2rem 1fr 2.8rem; gap: 0.6rem; align-items: center; margin: 0; }
.wz-panel .wz-row input[type="range"] { width: 100%; }
.wz-panel .wz-row .wz-val { color: var(--text); text-align: right; font-variant-numeric: tabular-nums; }
.wz-panel .wz-switch { font-size: inherit; display: flex; align-items: center; gap: 0.6rem; margin: 0; color: var(--text); cursor: pointer; }
.wz-panel .wz-switch input { width: 1.1rem; height: 1.1rem; margin: 0; accent-color: var(--accent-strong); }
.wz-panel .wz-switch.wz-main { font-weight: 600; padding: 0.15rem 0 0.35rem; }
.wz-sep { height: 1px; background: var(--line-bright); margin: 0.15rem 0; }
.wz-status { display: grid; grid-template-columns: 1fr 1fr; gap: 0.3rem 1.2rem; }
.wz-stat { display: flex; justify-content: space-between; gap: 0.5rem; }
.wz-stat b { color: var(--text); font-weight: 500; font-variant-numeric: tabular-nums; }
.wz-stat b.ok { color: var(--ok); } .wz-stat b.bad { color: var(--bad); } .wz-stat b.warn { color: var(--warn); }
.wz-meter { height: 5px; border-radius: 3px; background: var(--line-bright); overflow: hidden; }
.wz-meter i { display: block; height: 100%; width: 0; background: var(--accent); }
.wz-err { color: var(--bad); min-height: 1rem; font-size: 0.74rem; }
.wz-dot { display: inline-block; width: 0.55rem; height: 0.55rem; border-radius: 50%;
          background: var(--dim); margin-right: 0.35rem; vertical-align: baseline; }
.wz-dot.live { background: var(--ok); } .wz-dot.lost { background: var(--bad); }
.wz-dot.standby { background: var(--warn); }
`;

const controls = {};   // settings key -> { input, valueNode }
const stats = {};      // status id -> node
const holdUntil = {};  // settings key -> timestamp: ignore server echoes until then
let meter = null, errNode = null;
let pending = null, timer = null;

function send(patch) {
  for (const k of Object.keys(patch)) holdUntil[k] = performance.now() + 500;
  pending = { ...(pending || {}), ...patch };
  if (timer) return;
  timer = setTimeout(async () => {
    const body = pending; pending = null; timer = null;
    try {
      const r = await fetch("/wzrd", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (errNode) errNode.textContent = r.ok ? "" : `not saved (${r.status})`;
    } catch (_) {
      if (errNode) errNode.textContent = "not saved (offline)";
    }
  }, 60);
}

function addSlider(root, [key, label, min, max, step, tip]) {
  const row = document.createElement("label");
  row.className = "wz-row"; row.title = tip;
  const name = document.createElement("span"); name.textContent = label;
  const input = document.createElement("input");
  input.type = "range"; input.min = min; input.max = max; input.step = step;
  const val = document.createElement("span"); val.className = "wz-val";
  input.addEventListener("input", () => {
    const v = parseFloat(input.value);
    val.textContent = v.toFixed(2);
    send({ [key]: v });
  });
  row.append(name, input, val);
  root.appendChild(row);
  controls[key] = { input, val };
}

function addSwitch(root, [key, label, tip], main) {
  const row = document.createElement("label");
  row.className = main ? "wz-switch wz-main" : "wz-switch"; row.title = tip;
  const input = document.createElement("input"); input.type = "checkbox";
  const name = document.createElement("span"); name.textContent = label;
  input.addEventListener("change", () => send({ [key]: input.checked }));
  row.append(input, name);
  root.appendChild(row);
  controls[key] = { input };
}

function addSep(root) {
  const sep = document.createElement("div"); sep.className = "wz-sep"; root.appendChild(sep);
}

// Build the panel into `container` (once per page).
export function mountWzrdPanel(container) {
  if (!document.getElementById("wz-style")) {
    const style = document.createElement("style");
    style.id = "wz-style"; style.textContent = CSS;
    document.head.appendChild(style);
  }
  const root = document.createElement("div"); root.className = "wz-panel";
  SWITCH_LISTEN.forEach((s) => addSwitch(root, s, true));
  SLIDERS_LOOK.forEach((s) => addSlider(root, s));
  SWITCHES_COUPLE.forEach((s) => addSwitch(root, s, false));
  SLIDERS_TIME.forEach((s) => addSlider(root, s));
  addSep(root);
  const status = document.createElement("div"); status.className = "wz-status";
  for (const [id, label] of STATUS) {
    const cell = document.createElement("div"); cell.className = "wz-stat";
    const name = document.createElement("span"); name.textContent = label;
    const b = document.createElement("b"); b.textContent = "—";
    cell.append(name, b); status.appendChild(cell);
    stats[id] = b;
  }
  root.appendChild(status);
  meter = document.createElement("div"); meter.className = "wz-meter";
  meter.title = "wall level";
  meter.appendChild(document.createElement("i"));
  root.appendChild(meter);
  errNode = document.createElement("div"); errNode.className = "wz-err";
  root.appendChild(errNode);
  container.appendChild(root);
}

function setStat(id, text, cls = "") {
  const node = stats[id];
  if (!node) return;
  setText(node, text);
  if (node.className !== cls) node.className = cls;
}

// Refresh the panel from `state.wzrd`. Returns the link summary for the page's
// own button / menu item, or null when the server has no WZRD module:
//   { kind: "live" | "lost" | "standby" | "off", label }
export function applyWzrd(w) {
  if (!w) return null;
  const s = w.settings || {}, st = w.status || {};
  const now = performance.now();
  for (const [key, c] of Object.entries(controls)) {
    if (!(key in s) || (holdUntil[key] || 0) > now) continue;
    if (c.input.type === "checkbox") {
      if (c.input.checked !== !!s[key]) c.input.checked = !!s[key];
    } else {
      if (document.activeElement !== c.input) c.input.value = s[key];
      setText(c.val, Number(document.activeElement === c.input ? c.input.value : s[key]).toFixed(2));
    }
  }
  const everSeen = st.age_s != null;
  if (st.error) setStat("link", "listener down", "bad");
  else if (st.connected) setStat("link", "live", "ok");
  else setStat("link", everSeen ? `no signal (${Math.round(st.age_s)} s)` : "no signal", everSeen ? "bad" : "");
  setStat("level", Number(st.level ?? 0).toFixed(2));
  setStat("bri", Number(st.brightness ?? 1).toFixed(2));
  setStat("sat", Number(st.saturation ?? 1).toFixed(2));
  setStat("gain", Number(st.gain ?? 1).toFixed(2));
  setStat("pps", Number(st.packets_per_s ?? 0).toFixed(0));
  setStat("gap", st.max_gap_ms == null ? "—" : `${Math.round(st.max_gap_ms)} ms`,
    st.connected && st.max_gap_ms > 30 ? "warn" : "");
  setStat("drop", String(st.dropped ?? 0), st.dropped ? "warn" : "");
  if (meter) {
    const width = `${Math.round(Math.max(0, Math.min(1, st.level || 0)) * 100)}%`;
    if (meter.firstChild.style.width !== width) meter.firstChild.style.width = width;
  }
  if (errNode && st.error) setText(errNode, st.error);

  if (s.listen) {
    return st.connected ? { kind: "live", label: "live" }
      : { kind: "lost", label: everSeen ? "no signal" : "waiting" };
  }
  return st.connected ? { kind: "standby", label: "signal, off" } : { kind: "off", label: "off" };
}
