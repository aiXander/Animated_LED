// Desktop bootstrap. Wires the DOM defined in index.html to the shared
// modules. The HTML still owns layout + CSS; this file owns composition.
//
// Order of operations:
//   1. Pre-fetch /state once. Use transport_mode to decide whether the
//      LED viz section should exist at all (it gets removed entirely
//      when we're driving real LEDs without the simulator transport).
//   2. Bind every module against its DOM nodes.
//   3. Compose a single applyState() that fans the snapshot out to each
//      module + the engine-stats DOM in this file.
//   4. Open /ws/state — that drives the steady-state UI.

import { $, fmtNum, setText } from "./util.js";
import { connectStateWs, fetchStateOnceRaw } from "./state.js";
import { bindViz } from "./viz.js";
import { bindLayers } from "./layers.js";
import { bindMasters } from "./masters.js";
import { bindChat } from "./chat.js";
import { bindPresets } from "./presets.js";
import { bindAudioLink } from "./audio-link.js";

// Splitter: drag the boundary between #chat and #status-panel.
function bindSplitter() {
  const middle = $("middle");
  const chatPane = $("chat");
  const statusPane = $("status-panel");
  const splitter = $("splitter");
  if (!middle || !splitter) return { reproject: () => {} };

  function setSplit(leftPct, onResize) {
    const clamped = Math.max(35, Math.min(85, leftPct));
    chatPane.style.flex = `0 0 calc(${clamped}% - 0.1875rem)`;
    statusPane.style.flex = `0 0 calc(${100 - clamped}% - 0.1875rem)`;
    if (onResize) onResize();
  }
  setSplit(60);

  let dragging = false;
  let onReproject = null;
  splitter.addEventListener("mousedown", (e) => {
    dragging = true;
    document.body.classList.add("dragging");
    splitter.classList.add("dragging");
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const rect = middle.getBoundingClientRect();
    const pct = ((e.clientX - rect.left) / rect.width) * 100;
    setSplit(pct, onReproject);
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("dragging");
    splitter.classList.remove("dragging");
  });

  return {
    setOnResize(fn) { onReproject = fn; },
  };
}

async function boot() {
  // Phase 1 — fetch /state synchronously so we can decide on the viz before
  // first paint and avoid mounting/closing it unnecessarily.
  let initialState = null;
  try {
    initialState = await fetchStateOnceRaw();
  } catch (_) {
    // Server unreachable; render the page anyway, /ws/state fallback poll
    // will eventually pull a snapshot.
  }
  const transportMode = (initialState && initialState.transport_mode) || "simulator";
  const wantsViz = transportMode === "simulator" || transportMode === "multi";

  if (!wantsViz) {
    // Driving real LEDs only — drop the entire viz section.
    const viz = $("viz");
    const tooltip = $("tooltip");
    if (viz) viz.remove();
    if (tooltip) tooltip.remove();
    document.body.classList.add("no-viz");
  }

  // Phase 2 — bind modules.
  const layers  = bindLayers({ host: $("layers-list") });
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
    log:       $("log"),
    input:     $("input"),
    sendBtn:   $("send"),
    statusEl:  $("chat-status"),
    newBtn:    $("new"),
    form:      $("form"),
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

  const audioLink = bindAudioLink({
    links: [$("audio-ui-link"), $("audio-ui-link-fallback")],
  });
  const audioMeter = window.AudioMeter.mount($("audio-meter"));

  let viz = null;
  if (wantsViz) {
    viz = bindViz({
      root:         $("viz"),
      canvas:       $("canvas"),
      tooltip:      $("tooltip"),
      calBanner:    $("cal-banner"),
      calText:      $("cal-text"),
      calClear:     $("cal-clear"),
      wsPill:       $("ws-pill"),
      statusEngine: $("status-engine"),
      pixelCountEl: $("leds"),
    });
    viz.start();
  } else {
    // Engine status row is normally driven by the frames WS open/close;
    // when there's no viz, drive it from the state WS instead.
    const eng = $("status-engine");
    if (eng) { eng.textContent = "—"; eng.className = "dim"; }
  }

  const splitter = bindSplitter();
  if (viz) splitter.setOnResize(viz.requestReproject);

  // Phase 3 — central applyState. Touches only DOM that exists on this page.
  const els = {
    fps:        $("fps"),
    targetFps:  $("target-fps"),
    frames:     $("frames"),
    elapsed:    $("elapsed"),
    transport:  $("transport"),
    simClients: $("sim-clients"),
    gamma:      $("gamma"),
    audioStatus:$("audio-status"),
    statusEngine: $("status-engine"),
  };

  function applyState(s) {
    if (!s) return;
    setText(els.fps,        fmtNum(s.fps, 1));
    setText(els.targetFps,  s.target_fps != null ? String(s.target_fps) : "—");
    setText(els.frames,     String(s.frame_count ?? 0));
    setText(els.elapsed,    s.elapsed != null ? `${s.elapsed.toFixed(1)}s` : "—");
    setText(els.transport,  s.transport_mode || "—");
    setText(els.simClients, String(s.sim_clients ?? 0));
    setText(els.gamma,      fmtNum(s.gamma, 2));

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

  // Apply the initial snapshot we already fetched, then open the live feed.
  if (initialState) applyState(initialState);
  connectStateWs(applyState, {
    onOpen: () => {
      if (!viz && els.statusEngine) {
        els.statusEngine.textContent = "connected";
        els.statusEngine.className = "ok";
      }
    },
    onClose: () => {
      if (!viz && els.statusEngine) {
        els.statusEngine.textContent = "disconnected";
        els.statusEngine.className = "bad";
      }
    },
  });
}

boot();
