// Shared audio level meter — renders four band bars (beat, low, mid, high)
// bound to the auto-scaled feed published over OSC by the external audio-
// feature server. The status payload (`/audio/state`) carries `low/mid/high`
// already in ~[0, 1] (post-autoscale on the audio server side); the LED
// engine then multiplies them by `masters.audio_reactivity` for visual
// modulation. The `beat` row is a discrete pulse: `audio.beat_count` is a
// monotonically-increasing counter (incremented per `/audio/beat` rising
// edge) — when it advances we slam the bar to 1.0 and decay it locally.
// Band colors match the Realtime_PyAudio_FFT UI for visual consistency.
(() => {
  const BAND_KEYS = ["low", "mid", "high"];
  const STYLE_ID = "audio-meter-styles";
  const BEAT_DECAY_S = 0.25;
  const STYLE = `
    .am-meter { display: grid; grid-template-columns: auto minmax(0, 1fr) auto;
                gap: 0.25rem 0.5rem; align-items: center; font-size: 0.6875rem;
                padding-right: 0.25rem; }
    .am-meter-label { opacity: 0.6; }
    .am-bar { position: relative; height: 0.625rem; background: #111;
              border: 1px solid #1f1f1f; border-radius: 2px; overflow: hidden; }
    .am-fill { height: 100%; }
    .am-fill.am-beat { background: #2c4d99; }
    .am-fill.am-low  { background: #5a8dee; }
    .am-fill.am-mid  { background: #79d17a; }
    .am-fill.am-high { background: #e8a857; }
    .am-num { text-align: right; font-variant-numeric: tabular-nums; opacity: 0.8;
              min-width: 2.75rem; }
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
      rowHTML("beat", "beat") +
      rowHTML("low", "low") +
      rowHTML("mid", "mid") +
      rowHTML("high", "high") +
      `</div>`;
    const bars = {};
    const nums = {};
    for (const k of [...BAND_KEYS, "beat"]) {
      bars[k] = container.querySelector(`[data-bar="${k}"]`);
      nums[k] = container.querySelector(`[data-num="${k}"]`);
    }
    const pct = (v) => Math.max(0, Math.min(100, (v || 0) * 100)).toFixed(1) + "%";
    const fmt = (v) => (v == null ? "—" : Number(v).toFixed(3));

    let lastBeatCount = null;
    let beatPulseAt = -Infinity; // performance.now() ms
    let beatRafId = null;

    function paintBeat() {
      const now = performance.now();
      const dt = (now - beatPulseAt) / 1000;
      const v = dt < 0 ? 0 : Math.max(0, 1 - dt / BEAT_DECAY_S);
      bars.beat.style.width = pct(v);
      if (v > 0) {
        beatRafId = requestAnimationFrame(paintBeat);
      } else {
        beatRafId = null;
        bars.beat.style.width = "0%";
      }
    }

    function triggerBeat() {
      beatPulseAt = performance.now();
      if (beatRafId == null) beatRafId = requestAnimationFrame(paintBeat);
    }

    return {
      update(audio) {
        if (!audio) return;
        for (const k of BAND_KEYS) {
          const v = audio[k];
          bars[k].style.width = pct(v);
          nums[k].textContent = fmt(v);
        }
        const bc = audio.beat_count;
        if (typeof bc === "number") {
          if (lastBeatCount !== null && bc > lastBeatCount) triggerBeat();
          lastBeatCount = bc;
        }
        nums.beat.textContent = "";
      },
    };
  }

  window.AudioMeter = { mount };
})();
