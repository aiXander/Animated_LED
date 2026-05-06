// The 'audio' link in the top nav opens the external audio-feature
// server's UI in a new tab. We re-resolve the URL via /audio/state's
// `ui_url` field (carried into applyState) so a config edit shows up
// without reload.
//
// `links` may be a single anchor or a list — the desktop has one in the
// viz nav and a fallback in the chat header (used only when the viz is
// removed); the mobile has one in the header. All bound the same way.

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
        if (body && body.ui_url) {
          audioUiUrl = body.ui_url;
          for (const l of anchors) l.href = body.ui_url;
          window.open(audioUiUrl, "_blank", "noopener");
        }
      } catch (_) { /* ignore */ }
    });
  }

  return {
    applyAudio(audio) {
      if (!audio) return;
      if (audio.ui_url && audioUiUrl !== audio.ui_url) {
        audioUiUrl = audio.ui_url;
        for (const l of anchors) l.href = audio.ui_url;
      }
    },
  };
}
