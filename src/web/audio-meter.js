// Shared audio level meter, used by /index and /audio.
// Renders five bars (RMS, peak, low, mid, high) bound to the rolling-window-
// normalised audio fields (`*_norm`) — same values modulators consume.
(() => {
  const KEYS = ["rms", "peak", "low", "mid", "high"];
  const STYLE_ID = "audio-meter-styles";
  const STYLE = `
    .am-meter { display: grid; grid-template-columns: auto minmax(0, 1fr) auto;
                gap: 4px 8px; align-items: center; font-size: 11px;
                padding-right: 4px; }
    .am-meter-label { opacity: 0.6; }
    .am-bar { position: relative; height: 10px; background: #111;
              border: 1px solid #1f1f1f; border-radius: 2px; overflow: hidden; }
    .am-fill { height: 100%; }
    .am-fill.am-rms  { background: linear-gradient(90deg, #22c55e, #facc15, #f87171); }
    .am-fill.am-peak { background: #93c5fd; opacity: 0.7; }
    .am-fill.am-low  { background: #ef4444; }
    .am-fill.am-mid  { background: #facc15; }
    .am-fill.am-high { background: #38bdf8; }
    .am-num { text-align: right; font-variant-numeric: tabular-nums; opacity: 0.8;
              min-width: 44px; }
  `;

  function ensureStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const el = document.createElement("style");
    el.id = STYLE_ID;
    el.textContent = STYLE;
    document.head.appendChild(el);
  }

  function rowHTML(key, label) {
    return (
      `<span class="am-meter-label">${label}</span>` +
      `<div class="am-bar"><div class="am-fill am-${key}" data-bar="${key}" style="width:0%"></div></div>` +
      `<span class="am-num" data-num="${key}">0.000</span>`
    );
  }

  function mount(container) {
    ensureStyles();
    container.innerHTML =
      `<div class="am-meter">` +
      rowHTML("rms", "RMS") +
      rowHTML("peak", "peak") +
      rowHTML("low", "low") +
      rowHTML("mid", "mid") +
      rowHTML("high", "high") +
      `</div>`;
    const bars = {};
    const nums = {};
    for (const k of KEYS) {
      bars[k] = container.querySelector(`[data-bar="${k}"]`);
      nums[k] = container.querySelector(`[data-num="${k}"]`);
    }
    const pct = (v) => Math.max(0, Math.min(100, (v || 0) * 100)).toFixed(1) + "%";
    const fmt = (v) => (v == null ? "—" : Number(v).toFixed(3));
    return {
      update(audio) {
        if (!audio) return;
        for (const k of KEYS) {
          const v = audio[k + "_norm"];
          bars[k].style.width = pct(v);
          nums[k].textContent = fmt(v);
        }
      },
    };
  }

  window.AudioMeter = { mount };
})();
