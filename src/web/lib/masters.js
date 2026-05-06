// Master controls: brightness / speed / audio_reactivity / saturation
// sliders, plus the freeze toggle, blackout toggle, and the crossfade
// slider (which is conceptually a master in the UI even though it lives
// on agent.default_crossfade_seconds in the config).
//
// Local edits go out as debounced PATCH /masters; incoming /ws/state
// snapshots would otherwise fight a dragging slider, so apply() suppresses
// sync for ~400ms after a user touched a given field.

const MASTER_FIELDS = [
  { key: "brightness",       field: "brightness" },
  { key: "speed",            field: "speed" },
  { key: "audio_reactivity", field: "audio_reactivity" },
  { key: "saturation",       field: "saturation" },
];

export function bindMasters({
  sliders,         // { brightness: HTMLInputElement, ... } — required keys above
  values,          // { brightness: HTMLElement, ... } — text node next to each slider
  freezeBtn,
  blackoutBtn,
  crossfadeSlider,
  crossfadeVal,
}) {
  const lastEdit = new Map();
  const pending = {};
  let sendTimer = null;
  let crossfadeUserEditAt = 0;
  let crossfadeSendTimer = null;

  function flush() {
    sendTimer = null;
    if (!Object.keys(pending).length) return;
    const payload = JSON.stringify(pending);
    for (const k in pending) delete pending[k];
    fetch("/masters", {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: payload,
    }).catch(() => { /* WS will resync */ });
  }
  function queue(field, value) {
    pending[field] = value;
    lastEdit.set(field, performance.now());
    if (!sendTimer) sendTimer = setTimeout(flush, 33);
  }

  for (const m of MASTER_FIELDS) {
    const slider = sliders[m.key];
    const valEl = values[m.key];
    if (!slider) continue;
    slider.addEventListener("input", () => {
      const v = parseFloat(slider.value);
      if (valEl) valEl.textContent = v.toFixed(2);
      queue(m.field, v);
    });
  }

  if (freezeBtn) {
    freezeBtn.addEventListener("click", () => {
      const next = !freezeBtn.classList.contains("on");
      freezeBtn.classList.toggle("on", next);
      freezeBtn.textContent = next ? "freeze on" : "freeze";
      queue("freeze", next);
    });
  }

  if (blackoutBtn) {
    blackoutBtn.addEventListener("click", async () => {
      const next = !blackoutBtn.classList.contains("on");
      // Optimistic update; /ws/state confirms within ~16ms.
      blackoutBtn.classList.toggle("on", next);
      blackoutBtn.textContent = next ? "blackout on" : "blackout";
      try {
        await fetch(next ? "/blackout" : "/resume", { method: "POST" });
      } catch (_) { /* WS will resync */ }
    });
  }

  // Crossfade slider — single source of truth for transition speed across
  // the whole UI. Both the agent's `update_leds` tool and POST /presets/{name}
  // resolve their crossfade duration from agent.default_crossfade_seconds,
  // so whatever this slider is set to is what every fade actually uses.
  if (crossfadeSlider) {
    const flushCrossfade = () => {
      crossfadeSendTimer = null;
      const v = parseFloat(crossfadeSlider.value);
      fetch("/agent/config", {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ default_crossfade_seconds: v }),
      }).catch(() => { /* ignore */ });
    };
    crossfadeSlider.addEventListener("input", () => {
      const v = parseFloat(crossfadeSlider.value);
      if (crossfadeVal) crossfadeVal.textContent = v.toFixed(2) + "s";
      crossfadeUserEditAt = performance.now();
      if (!crossfadeSendTimer) crossfadeSendTimer = setTimeout(flushCrossfade, 150);
    });
  }

  function applyMasters(m) {
    if (!m) return;
    const now = performance.now();
    for (const cfg of MASTER_FIELDS) {
      const slider = sliders[cfg.key];
      const valEl = values[cfg.key];
      if (!slider) continue;
      if (now - (lastEdit.get(cfg.field) || 0) < 400) continue;
      const v = m[cfg.field];
      if (typeof v !== "number") continue;
      const current = parseFloat(slider.value);
      if (Math.abs(current - v) > 1e-4) slider.value = String(v);
      const text = v.toFixed(2);
      if (valEl && valEl.textContent !== text) valEl.textContent = text;
    }
    if (freezeBtn && now - (lastEdit.get("freeze") || 0) >= 400) {
      const on = !!m.freeze;
      if (freezeBtn.classList.contains("on") !== on) {
        freezeBtn.classList.toggle("on", on);
        freezeBtn.textContent = on ? "freeze on" : "freeze";
      }
    }
  }

  function applyBlackout(blackout) {
    if (!blackoutBtn) return;
    const bo = !!blackout;
    if (blackoutBtn.classList.contains("on") !== bo) {
      blackoutBtn.classList.toggle("on", bo);
      blackoutBtn.textContent = bo ? "blackout on" : "blackout";
    }
  }

  // The agent config endpoint returns a default_crossfade_seconds the user
  // didn't actively choose this session — apply it on bootstrap unless the
  // slider was already touched.
  function setCrossfadeFromConfig(seconds) {
    if (!crossfadeSlider) return;
    if (performance.now() - crossfadeUserEditAt < 400) return;
    crossfadeSlider.value = String(seconds);
    if (crossfadeVal) crossfadeVal.textContent = seconds.toFixed(2) + "s";
  }

  function getCrossfade() {
    return crossfadeSlider ? parseFloat(crossfadeSlider.value) : null;
  }

  return { applyMasters, applyBlackout, setCrossfadeFromConfig, getCrossfade };
}
