// Tiny shared helpers used by every UI module. No DOM coupling beyond
// `document` lookups. Intentionally trivial — anything bigger lives in its
// own module.

export const $ = (id) => document.getElementById(id);

export function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

export function fmtNum(v, digits = 3) {
  return v == null ? "—" : Number(v).toFixed(digits);
}

export function pct(v) {
  return Math.max(0, Math.min(100, (v || 0) * 100)).toFixed(1) + "%";
}

// Per-node "what did I last write" cache so applyState can short-circuit
// frame-rate DOM writes when the value didn't change. WeakMap so removed
// nodes drop their entry automatically.
const lastText = new WeakMap();

export function setText(node, text) {
  if (!node) return;
  if (lastText.get(node) === text) return;
  lastText.set(node, text);
  node.textContent = text;
}

export function setWidth(node, width) {
  if (!node) return;
  if (lastText.get(node) === width) return;
  lastText.set(node, width);
  node.style.width = width;
}

export function setClass(node, cls) {
  if (!node) return;
  if (node.className === cls) return;
  node.className = cls;
}

// One line on how the render loop is doing, from `state.perf` (the server's
// rolling frame timings plus the Pi's temperature / throttling flags).
//   frame    = p95 cost of one frame (render + encode + send), in ms
//   held up  = the longest a frame had to wait for the event loop, last ~4 s
// `warn` is true when a frame was held up long enough to see (> 50 ms) or the
// Pi is throttled / under-voltage: the two things that look like "laggy LEDs".
export function perfSummary(perf) {
  if (!perf || !perf.frames) return { short: "", detail: "", warn: false };
  const host = perf.host || {};
  const frame = (perf.render_ms_p95 || 0) + (perf.out_ms_p95 || 0);
  const late = perf.late_ms_max || 0;
  const parts = [`frame ${frame.toFixed(1)} ms`, `held up ${late.toFixed(0)} ms`];
  if (host.temp_c != null) parts.push(`${host.temp_c.toFixed(0)}°C`);
  if (host.cpu_mhz != null) parts.push(`${host.cpu_mhz} MHz`);
  if (host.governor) parts.push(host.governor);
  const alarms = [];
  if (host.undervoltage) alarms.push("UNDER-VOLTAGE");
  if (host.throttled) alarms.push("CPU THROTTLED");
  if (late > 50) alarms.push("render loop held up");
  return {
    short: `${frame.toFixed(1)} ms`,
    detail: (alarms.length ? alarms.join(" · ") + " · " : "") + parts.join(" · ")
      + ` · ${perf.stalls || 0} hold-ups since boot`,
    warn: alarms.length > 0,
  };
}
