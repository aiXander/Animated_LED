// Mobile bootstrap. Same module wiring as desktop but a different DOM
// shape (single column, tabs) and different optional features (no
// hover tooltip on the canvas; tap-to-solo only).
//
// Tabs: [layers] [chat]. Presets stay as save/load buttons that pop a
// full-screen sheet, reusing the desktop modal styles.

import { $, fmtNum, setText } from "./util.js";
import { connectStateWs, fetchStateOnceRaw } from "./state.js";
import { bindViz } from "./viz.js";
import { bindLayers } from "./layers.js";
import { bindMasters } from "./masters.js";
import { bindChat } from "./chat.js";
import { bindPresets } from "./presets.js";
import { bindAudioLink } from "./audio-link.js";

function bindTabs() {
  const buttons = Array.from(document.querySelectorAll(".tabbar [data-tab]"));
  const panels  = Array.from(document.querySelectorAll(".tab-panel"));
  function activate(name) {
    for (const b of buttons) {
      b.classList.toggle("active", b.dataset.tab === name);
      b.setAttribute("aria-selected", b.dataset.tab === name ? "true" : "false");
    }
    for (const p of panels) {
      p.classList.toggle("active", p.dataset.tab === name);
    }
  }
  for (const b of buttons) {
    b.addEventListener("click", () => activate(b.dataset.tab));
  }
  return { activate };
}

async function boot() {
  let initialState = null;
  try {
    initialState = await fetchStateOnceRaw();
  } catch (_) { /* ignore */ }
  const transportMode = (initialState && initialState.transport_mode) || "simulator";
  const wantsViz = transportMode === "simulator" || transportMode === "multi";

  if (!wantsViz) {
    const viz = $("viz");
    if (viz) viz.remove();
    document.body.classList.add("no-viz");
  }

  const layers = bindLayers({ host: $("layers-list") });

  const masters = bindMasters({
    sliders: {
      brightness:       $("m-brightness"),
      speed:            $("m-speed"),
      audio_reactivity: $("m-audio-reactivity"),
      saturation:       $("m-saturation"),
    },
    values: {
      brightness:       $("m-brightness-v"),
      speed:            $("m-speed-v"),
      audio_reactivity: $("m-audio-reactivity-v"),
      saturation:       $("m-saturation-v"),
    },
    freezeBtn:       $("m-freeze"),
    blackoutBtn:     $("m-blackout"),
    crossfadeSlider: $("m-crossfade"),
    crossfadeVal:    $("m-crossfade-v"),
  });

  const chat = bindChat({
    log:      $("log"),
    input:    $("input"),
    sendBtn:  $("send"),
    statusEl: $("chat-status"),
    newBtn:   $("new"),
    form:     $("form"),
    onFocus: () => {
      // Virtual keyboard pushes the composer up; let the browser
      // re-anchor the log scroll after the layout settles.
      setTimeout(() => {
        const log = $("log");
        if (log) log.scrollTop = log.scrollHeight;
      }, 250);
    },
  });
  chat.loadAgentConfig({
    onCrossfade: (s) => masters.setCrossfadeFromConfig(s),
  });

  bindPresets({
    saveBtn:        $("save-preset-btn"),
    loadBtn:        $("load-preset-btn"),
    saveModal:      $("save-modal"),
    loadModal:      $("load-modal"),
    saveNameInput:  $("save-name"),
    saveErr:        $("save-err"),
    loadErr:        $("load-err"),
    presetList:     $("preset-list"),
    loadWithMasters:$("load-with-masters"),
    saveCancel:     $("save-cancel"),
    saveConfirm:    $("save-confirm"),
    loadCancel:     $("load-cancel"),
    loadConfirm:    $("load-confirm"),
    getCrossfade:   () => masters.getCrossfade(),
  });

  const audioLink  = bindAudioLink({ links: $("audio-ui-link") });
  // The audio-meter mount target is omitted from mobile.html (the whole
  // bottom info strip is gone there). Fall back to a no-op so applyState's
  // audioMeter.update(...) call stays safe.
  const audioMeterEl = $("audio-meter");
  const audioMeter = audioMeterEl
    ? window.AudioMeter.mount(audioMeterEl)
    : { update() {} };

  let viz = null;
  if (wantsViz) {
    viz = bindViz({
      root:         $("viz"),
      canvas:       $("canvas"),
      tooltip:      null,            // no hover on touch
      calBanner:    $("cal-banner"),
      calText:      $("cal-text"),
      calClear:     $("cal-clear"),
      wsPill:       $("ws-pill"),
      statusEngine: null,
      pixelCountEl: $("leds"),
    });
    viz.start();
  }

  bindTabs();

  const els = {
    fps:        $("fps"),
    targetFps:  $("target-fps"),
    transport:  $("transport"),
    audioStatus:$("audio-status"),
    statusEngine: $("status-engine"),
  };

  function applyState(s) {
    if (!s) return;
    setText(els.fps,        fmtNum(s.fps, 1));
    setText(els.targetFps,  s.target_fps != null ? String(s.target_fps) : "—");
    setText(els.transport,  s.transport_mode || "—");

    masters.applyBlackout(s.blackout);
    layers.render(s.layers);
    masters.applyMasters(s.masters);

    const a = s.audio || {};
    let statusText;
    if (a.connected) statusText = "connected";
    else if (a.error) statusText = "disconnected · " + a.error;
    else statusText = "disconnected";
    setText(els.audioStatus, statusText);
    audioLink.applyAudio(a);
    audioMeter.update(a);

    if (viz) viz.applyCalibration(s.calibration);
  }

  if (initialState) applyState(initialState);
  connectStateWs(applyState, {
    onOpen: () => {
      if (els.statusEngine) {
        els.statusEngine.textContent = "connected";
        els.statusEngine.className = "ok";
      }
    },
    onClose: () => {
      if (els.statusEngine) {
        els.statusEngine.textContent = "disconnected";
        els.statusEngine.className = "bad";
      }
    },
  });
}

boot();
