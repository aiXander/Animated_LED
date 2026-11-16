// WZRD input panel, shared by the desktop modal and the mobile sheet.
//
// One binding table drives both front-ends, so they cannot drift apart: each
// page only supplies a container, a way to open it, and somewhere to show the
// one-word link summary. Settings go out as `PATCH /wzrd` on a 60 ms debounce
// (like `sendMaster`); controls refresh from `state.wzrd` on every state tick,
// skipping the one the operator is touching.

import { setText } from "./util.js";

// The wall level drives LED brightness on a straight line: "LEDs when dark" at
// the range's dark end or below, full at its bright end or above.
const WALL_RANGE_TIP = "Wall level → LED brightness. At or below the left handle the LEDs sit at " +
  "'LEDs when dark'; at or above the right handle they are at full. The green line is the wall " +
  "right now: set the handles around where it moves while typical content plays.";
const FLOOR = ["led_floor", "LEDs when dark", 0, 1, 0.01,
  "LED brightness while the wall is at or below the range's left handle. 1 = ignore the wall level."];
const WALL_GAP = 0.01;   // the server rejects wall_bright < wall_dark + 0.01
// The wall level mostly lives below 0.3, so the range track is a square-root
// scale: 0.01 sits at 10 %, 0.04 at 20 %, 0.25 at half way.
const toPos = (v) => Math.sqrt(Math.max(0, Math.min(1, v)));
const toVal = (p) => p * p;
// [key, label, min, max, step, tooltip]
const SLIDERS_LOOK = [
  ["colour", "colour", 0, 1, 0.01, "How far the LED colours move to the wall's colours."],
  ["spread", "spread", 0, 1, 0.01,
    "How much of the wall's palette is laid out along the strips. 0 = the effect's own hue picks the wall colour."],
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
.wz-dual { --th: 18px; --lo: 0; --hi: 1; --now: 0; position: relative; height: var(--th); }
@media (pointer: coarse) { .wz-dual { --th: 28px; } }
.wz-dual-track, .wz-dual-fill { position: absolute; top: 50%; height: 8px; margin-top: -4px; border-radius: 4px; box-sizing: border-box; }
.wz-dual-track { left: 0; right: 0; background: #050505; border: 1px solid #4a4d5a; }
.wz-dual-fill { background: var(--accent); opacity: 0.45;
  left: calc(var(--th) / 2 + var(--lo) * (100% - var(--th)));
  width: calc((var(--hi) - var(--lo)) * (100% - var(--th))); }
.wz-dual-now { position: absolute; top: -3px; bottom: -3px; width: 2px; margin-left: -1px; border-radius: 1px;
  background: var(--ok); left: calc(var(--th) / 2 + var(--now) * (100% - var(--th))); }
.wz-dual input[type="range"] { position: absolute; left: 0; top: 0; width: 100%; height: var(--th);
  margin: 0; padding: 0; border: 0; background: transparent; pointer-events: none;
  -webkit-appearance: none; appearance: none; }
.wz-dual input[type="range"]::-webkit-slider-runnable-track { height: var(--th); background: transparent; border: 0; }
.wz-dual input[type="range"]::-moz-range-track { background: transparent; border: 0; }
.wz-dual input[type="range"]::-webkit-slider-thumb { pointer-events: auto; margin-top: 0;
  width: var(--th); height: var(--th); box-sizing: border-box; }
.wz-dual input[type="range"]::-moz-range-thumb { pointer-events: auto;
  width: var(--th); height: var(--th); box-sizing: border-box; }
.wz-dual-vals { grid-column: 2 / 4; display: flex; justify-content: space-between; margin-top: -0.3rem;
  font-size: 0.72rem; font-variant-numeric: tabular-nums; }
.wz-dual-vals b { color: var(--text); font-weight: 500; }
.wz-err { color: var(--bad); min-height: 1rem; font-size: 0.74rem; }
.wz-dot { display: inline-block; width: 0.55rem; height: 0.55rem; border-radius: 50%;
          background: var(--dim); margin-right: 0.35rem; vertical-align: baseline; }
.wz-dot.live { background: var(--ok); } .wz-dot.lost { background: var(--bad); }
.wz-dot.standby { background: var(--warn); }
`;

const controls = {};   // settings key -> { set(value), busy() }
const stats = {};      // status id -> node
const holdUntil = {};  // settings key -> timestamp: ignore server echoes until then
let wallRange = null, errNode = null;
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
  controls[key] = {
    set: (v) => { input.value = v; setText(val, Number(v).toFixed(2)); },
    busy: () => document.activeElement === input,
  };
}

function addSwitch(root, [key, label, tip], main) {
  const row = document.createElement("label");
  row.className = main ? "wz-switch wz-main" : "wz-switch"; row.title = tip;
  const input = document.createElement("input"); input.type = "checkbox";
  const name = document.createElement("span"); name.textContent = label;
  input.addEventListener("change", () => send({ [key]: input.checked }));
  row.append(input, name);
  root.appendChild(row);
  controls[key] = { set: (v) => { if (input.checked !== !!v) input.checked = !!v; }, busy: () => false };
}

// Two handles on one track (wall_dark, wall_bright) plus a live marker at the
// wall's current level. Two range inputs stacked; only their thumbs take the
// pointer. The handles keep WALL_GAP apart, as the server requires.
function addWallRange(root) {
  const row = document.createElement("div");
  row.className = "wz-row"; row.title = WALL_RANGE_TIP;
  const name = document.createElement("span"); name.textContent = "wall range";
  const dual = document.createElement("div"); dual.className = "wz-dual";
  const track = document.createElement("div"); track.className = "wz-dual-track";
  const fill = document.createElement("div"); fill.className = "wz-dual-fill";
  const now = document.createElement("div"); now.className = "wz-dual-now";
  const mk = (label) => {
    const i = document.createElement("input");
    i.type = "range"; i.min = 0; i.max = 1; i.step = 0.001; i.setAttribute("aria-label", label);
    return i;
  };
  const lo = mk("wall dark at"), hi = mk("wall bright at");
  dual.append(track, fill, now, lo, hi);
  const vals = document.createElement("div"); vals.className = "wz-dual-vals";
  const loTxt = document.createElement("b"), hiTxt = document.createElement("b");
  const loCell = document.createElement("span"), hiCell = document.createElement("span");
  loCell.append("dark at ", loTxt); hiCell.append("full at ", hiTxt);
  vals.append(loCell, hiCell);
  row.append(name, dual, document.createElement("span"), document.createElement("span"), vals);
  root.appendChild(row);

  const draw = () => {
    dual.style.setProperty("--lo", lo.value);
    dual.style.setProperty("--hi", hi.value);
    setText(loTxt, toVal(+lo.value).toFixed(3));
    setText(hiTxt, toVal(+hi.value).toFixed(3));
  };
  lo.addEventListener("input", () => {
    const v = Math.max(0, Math.min(toVal(+lo.value), toVal(+hi.value) - WALL_GAP));
    lo.value = toPos(v); draw();
    send({ wall_dark: +v.toFixed(3) });
  });
  hi.addEventListener("input", () => {
    const v = Math.min(1, Math.max(toVal(+hi.value), toVal(+lo.value) + WALL_GAP));
    hi.value = toPos(v); draw();
    send({ wall_bright: +v.toFixed(3) });
  });
  controls.wall_dark = { set: (v) => { lo.value = toPos(v); draw(); }, busy: () => document.activeElement === lo };
  controls.wall_bright = { set: (v) => { hi.value = toPos(v); draw(); }, busy: () => document.activeElement === hi };
  return { setNow: (level) => dual.style.setProperty("--now", toPos(level).toFixed(4)) };
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
  wallRange = addWallRange(root);
  addSlider(root, FLOOR);
  addSep(root);
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
    if (!(key in s) || (holdUntil[key] || 0) > now || c.busy()) continue;
    c.set(s[key]);
  }
  const everSeen = st.age_s != null;
  if (st.error) setStat("link", "listener down", "bad");
  else if (st.connected) setStat("link", "live", "ok");
  else setStat("link", everSeen ? `no signal (${Math.round(st.age_s)} s)` : "no signal", everSeen ? "bad" : "");
  setStat("level", Number(st.level ?? 0).toFixed(3));
  setStat("bri", Number(st.brightness ?? 1).toFixed(2));
  setStat("sat", Number(st.saturation ?? 1).toFixed(2));
  setStat("gain", Number(st.gain ?? 1).toFixed(2));
  setStat("pps", Number(st.packets_per_s ?? 0).toFixed(0));
  setStat("gap", st.max_gap_ms == null ? "—" : `${Math.round(st.max_gap_ms)} ms`,
    st.connected && st.max_gap_ms > 30 ? "warn" : "");
  setStat("drop", String(st.dropped ?? 0), st.dropped ? "warn" : "");
  if (wallRange) wallRange.setNow(st.level || 0);
  if (errNode && st.error) setText(errNode, st.error);

  if (s.listen) {
    return st.connected ? { kind: "live", label: "live" }
      : { kind: "lost", label: everSeen ? "no signal" : "waiting" };
  }
  return st.connected ? { kind: "standby", label: "signal, off" } : { kind: "off", label: "off" };
}
