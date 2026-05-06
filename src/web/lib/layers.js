// Layer rows (one per active layer in the mixer stack). Each row is a
// horizontal drag-opacity bar with a remove "×" button. Structural changes
// (count / effect summary / blend) trigger a rebuild; opacity-only changes
// only update the CSS var so an active drag isn't interrupted.
//
// The summarizer walks the surface DSL tree and pulls a short ordered list
// of human-recognisable tokens (palette names, audio bands, wave/radial,
// …) skipping pure plumbing nodes.

const SUMMARY_SKIP = new Set([
  "constant", "mix", "mul", "add", "screen", "max", "min",
  "remap", "threshold", "clamp", "range_map",
]);
const SUMMARY_MAX_TOKENS = 5;
const SUMMARY_MAX_CHARS = 56;

function summaryToken(node) {
  const k = node.kind;
  const p = node.params || {};
  if (k === "palette_named") return String(p.name || "palette");
  if (k === "palette_stops") return "custom-palette";
  if (k === "palette_lookup") return "palette";
  if (k === "audio_band") return `audio:${p.band || "?"}`;
  return k;
}
function walkSummary(node, out, seen) {
  if (!node || typeof node !== "object") return;
  if (typeof node.kind === "string" && !SUMMARY_SKIP.has(node.kind)) {
    const tok = summaryToken(node);
    if (tok && !seen.has(tok)) { seen.add(tok); out.push(tok); }
  }
  const params = node.params;
  if (params && typeof params === "object") {
    for (const key in params) {
      const v = params[key];
      if (key === "palette" && typeof v === "string") {
        if (!seen.has(v)) { seen.add(v); out.push(v); }
      } else if (Array.isArray(v)) {
        for (const item of v) walkSummary(item, out, seen);
      } else if (v && typeof v === "object") {
        walkSummary(v, out, seen);
      }
    }
  }
}
export function summarizeLayer(layer) {
  const out = [];
  walkSummary(layer && layer.node, out, new Set());
  if (!out.length) return "—";
  let tokens = out.slice(0, SUMMARY_MAX_TOKENS);
  let truncated = out.length > SUMMARY_MAX_TOKENS;
  let s = tokens.join(", ");
  while (tokens.length > 1 && s.length > SUMMARY_MAX_CHARS) {
    tokens.pop();
    truncated = true;
    s = tokens.join(", ");
  }
  return truncated ? s + " …" : s;
}

export function bindLayers({ host }) {
  let lastSig = null;
  const rowCache = []; // [{row, name, blend, opNum, idx}]
  const lastEdit = new Map();           // index → ms (suppress incoming sync mid-drag)
  const pendingOpacity = new Map();     // index → 0..1
  let sendTimer = null;

  function flushOpacity() {
    sendTimer = null;
    const snapshot = Array.from(pendingOpacity.entries());
    pendingOpacity.clear();
    for (const [i, v] of snapshot) {
      fetch(`/layers/${i}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ opacity: v }),
      }).catch(() => { /* WS will resync */ });
    }
  }
  function queueOpacity(i, v) {
    pendingOpacity.set(i, v);
    lastEdit.set(i, performance.now());
    if (!sendTimer) sendTimer = setTimeout(flushOpacity, 33);
  }
  function setRowOpacity(cache, opacity) {
    const p = Math.max(0, Math.min(1, opacity)) * 100;
    cache.row.style.setProperty("--op", p.toFixed(1) + "%");
    cache.opNum.textContent = opacity.toFixed(2);
  }

  let activeDrag = null; // { i, cache }
  function opacityFromEvent(cache, clientX) {
    const r = cache.row.getBoundingClientRect();
    return Math.max(0, Math.min(1, (clientX - r.left) / r.width));
  }
  function onDragMove(e) {
    if (!activeDrag) return;
    e.preventDefault();
    const x = e.touches ? e.touches[0].clientX : e.clientX;
    const v = opacityFromEvent(activeDrag.cache, x);
    setRowOpacity(activeDrag.cache, v);
    queueOpacity(activeDrag.i, v);
  }
  function onDragEnd() {
    if (!activeDrag) return;
    activeDrag.cache.row.classList.remove("dragging");
    activeDrag = null;
    window.removeEventListener("mousemove", onDragMove);
    window.removeEventListener("mouseup", onDragEnd);
    window.removeEventListener("touchmove", onDragMove);
    window.removeEventListener("touchend", onDragEnd);
    window.removeEventListener("touchcancel", onDragEnd);
  }
  function startDrag(i, cache, e) {
    if (e.type === "mousedown" && e.button !== 0) return;
    e.preventDefault();
    activeDrag = { i, cache };
    cache.row.classList.add("dragging");
    if (e.type === "touchstart") {
      window.addEventListener("touchmove", onDragMove, { passive: false });
      window.addEventListener("touchend", onDragEnd);
      window.addEventListener("touchcancel", onDragEnd);
      onDragMove(e);
    } else {
      window.addEventListener("mousemove", onDragMove);
      window.addEventListener("mouseup", onDragEnd);
      onDragMove(e);
    }
  }

  function render(layers) {
    layers = layers || [];
    const sig = JSON.stringify(
      layers.map((l) => [summarizeLayer(l), l.blend || "normal"])
    );
    const structuralChanged = sig !== lastSig;
    lastSig = sig;

    if (!layers.length) {
      if (structuralChanged) {
        host.replaceChildren();
        const empty = document.createElement("div");
        empty.className = "dim";
        empty.textContent = "— no layers —";
        host.appendChild(empty);
        rowCache.length = 0;
      }
      return;
    }

    if (structuralChanged || rowCache.length !== layers.length) {
      host.replaceChildren();
      rowCache.length = 0;
      layers.forEach((_, i) => {
        const row = document.createElement("div");
        row.className = "layer-row";
        const head = document.createElement("div");
        head.className = "head";
        const idx = document.createElement("span");
        idx.className = "dim";
        idx.textContent = `#${i}`;
        const name = document.createElement("span");
        name.className = "name";
        const opNum = document.createElement("span");
        opNum.className = "op-num";
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "remove";
        remove.textContent = "×";
        remove.title = "remove layer";
        remove.addEventListener("mousedown", (e) => e.stopPropagation());
        remove.addEventListener("touchstart", (e) => e.stopPropagation());
        remove.addEventListener("click", (e) => {
          e.stopPropagation();
          fetch(`/layers/${i}`, { method: "DELETE" })
            .catch(() => { /* WS will resync */ });
        });
        head.appendChild(idx);
        head.appendChild(document.createTextNode(" "));
        head.appendChild(name);
        head.appendChild(remove);
        const blend = document.createElement("div");
        blend.className = "blend";
        row.appendChild(head);
        row.appendChild(blend);
        row.appendChild(opNum);
        host.appendChild(row);
        const cache = { row, name, blend, opNum, idx: i };
        row.addEventListener("mousedown", (e) => startDrag(i, cache, e));
        row.addEventListener("touchstart", (e) => startDrag(i, cache, e), { passive: false });
        rowCache.push(cache);
      });
    }
    layers.forEach((l, i) => {
      const cache = rowCache[i];
      const summary = summarizeLayer(l);
      cache.name.textContent = summary;
      cache.row.title = `#${i} ${summary} · drag to set opacity`;
      cache.blend.textContent = `blend ${l.blend || "normal"}`;
      if (performance.now() - (lastEdit.get(i) || 0) < 400) return;
      const opacity = (typeof l.opacity === "number") ? l.opacity : 1;
      setRowOpacity(cache, opacity);
    });
  }

  return { render };
}
