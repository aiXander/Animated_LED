// The 'audio' link in the top nav opens the external audio-feature
// server's UI in a new tab. We re-resolve the URL via /audio/state's
// `ui_url` field (carried into applyState) so a config edit shows up
// without reload.
//
// `links` may be a single anchor or a list — the desktop has one in the
// viz nav and a fallback in the chat header (used only when the viz is
// removed); the mobile has one in the header. All bound the same way.
//
// When the operator UI is loaded over the tailnet (https + non-loopback
// host), the configured `ui_url` (loopback) is unreachable from the
// remote browser. The server also publishes `tailnet_ui_url` from
// `audio_server.tailnet_ui_url` in YAML — we prefer it in that case.

function isTailnetClient() {
  return location.protocol === "https:" && location.hostname !== "127.0.0.1"
    && location.hostname !== "localhost";
}

function pickUrl(audio) {
  if (!audio) return "";
  if (isTailnetClient() && audio.tailnet_ui_url) return audio.tailnet_ui_url;
  return audio.ui_url || "";
}

export function bindAudioLink({ links }) {
  const anchors = (Array.isArray(links) ? links : [links]).filter(Boolean);
  if (!anchors.length) return { applyAudio() {} };

  let audioUiUrl = "";
  for (const link of anchors) {
    link.target = "_blank";
    link.rel = "noopener";
    link.addEventListener("click", async (e) => {
      if (audioUiUrl) return; // anchor navigates naturally
      e.preventDefault();
      try {
        const r = await fetch("/audio/ui");
        const body = await r.json();
        const url = pickUrl(body);
        if (url) {
          audioUiUrl = url;
          for (const l of anchors) l.href = url;
          window.open(url, "_blank", "noopener");
        }
      } catch (_) { /* ignore */ }
    });
  }

  return {
    applyAudio(audio) {
      const url = pickUrl(audio);
      if (url && audioUiUrl !== url) {
        audioUiUrl = url;
        for (const l of anchors) l.href = url;
      }
    },
  };
}
