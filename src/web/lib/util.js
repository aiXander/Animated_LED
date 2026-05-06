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
