// Preset save/load — modal popups + REST round-trips. The save dialog
// posts the current layer stack + masters to /presets; load applies a
// selected preset via /presets/{name} with optional apply_masters and an
// explicit crossfade_seconds (read live from the masters slider via the
// passed `getCrossfade` getter, so a still-pending debounced PATCH to
// /agent/config can't race the load and apply the old default).

export function bindPresets({
  saveBtn,
  loadBtn,
  saveModal,
  loadModal,
  saveNameInput,
  saveErr,
  loadErr,
  presetList,
  loadWithMasters,
  saveCancel,
  saveConfirm,
  loadCancel,
  loadConfirm,
  getCrossfade,
}) {
  let selectedPreset = null;

  const openModal  = (m) => m && m.classList.add("open");
  const closeModal = (m) => m && m.classList.remove("open");

  if (saveBtn) {
    saveBtn.addEventListener("click", () => {
      saveNameInput.value = "";
      saveErr.textContent = "";
      openModal(saveModal);
      setTimeout(() => saveNameInput.focus(), 0);
    });
  }
  saveCancel && saveCancel.addEventListener("click", () => closeModal(saveModal));
  saveConfirm && saveConfirm.addEventListener("click", doSave);
  saveNameInput && saveNameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); doSave(); }
    else if (e.key === "Escape") { closeModal(saveModal); }
  });

  async function doSave() {
    const raw = saveNameInput.value.trim();
    if (!raw) { saveErr.textContent = "name is required"; return; }
    if (raw.length > 40) { saveErr.textContent = "max 40 characters"; return; }
    if (!/^[A-Za-z0-9][A-Za-z0-9_\-]*$/.test(raw)) {
      saveErr.textContent = "letters/digits/_/- only; must start with alphanumeric";
      return;
    }
    saveErr.textContent = "saving…";
    try {
      const r = await fetch("/presets", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: raw, overwrite: true }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        const msg = typeof err.detail === "string"
          ? err.detail
          : JSON.stringify(err.detail || err) || `HTTP ${r.status}`;
        saveErr.textContent = msg;
        return;
      }
      closeModal(saveModal);
    } catch (e) {
      saveErr.textContent = "network error: " + e.message;
    }
  }

  if (loadBtn) {
    loadBtn.addEventListener("click", async () => {
      loadErr.textContent = "";
      selectedPreset = null;
      presetList.replaceChildren();
      presetList.appendChild(Object.assign(document.createElement("div"), {
        className: "empty", textContent: "loading…",
      }));
      openModal(loadModal);
      try {
        const r = await fetch("/presets");
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        renderPresetList(data.presets || []);
      } catch (e) {
        presetList.replaceChildren();
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "failed to load presets: " + e.message;
        presetList.appendChild(empty);
      }
    });
  }

  function renderPresetList(names) {
    presetList.replaceChildren();
    if (!names.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "no saved presets";
      presetList.appendChild(empty);
      return;
    }
    const sorted = [...names].sort((a, b) => a.localeCompare(b));
    for (const name of sorted) {
      const item = document.createElement("div");
      item.className = "preset-item";
      item.textContent = name;
      item.addEventListener("click", () => {
        for (const el of presetList.querySelectorAll(".preset-item.selected")) {
          el.classList.remove("selected");
        }
        item.classList.add("selected");
        selectedPreset = name;
      });
      item.addEventListener("dblclick", () => {
        selectedPreset = name;
        item.classList.add("selected");
        doLoad();
      });
      presetList.appendChild(item);
    }
  }

  loadCancel && loadCancel.addEventListener("click", () => closeModal(loadModal));
  loadConfirm && loadConfirm.addEventListener("click", doLoad);

  async function doLoad() {
    if (!selectedPreset) { loadErr.textContent = "select a preset"; return; }
    loadErr.textContent = "loading…";
    const crossfade = getCrossfade ? getCrossfade() : null;
    try {
      const body = { apply_masters: !!(loadWithMasters && loadWithMasters.checked) };
      if (typeof crossfade === "number" && !Number.isNaN(crossfade)) {
        body.crossfade_seconds = crossfade;
      }
      const r = await fetch("/presets/" + encodeURIComponent(selectedPreset), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        const msg = typeof err.detail === "string"
          ? err.detail
          : JSON.stringify(err.detail || err) || `HTTP ${r.status}`;
        loadErr.textContent = msg;
        return;
      }
      closeModal(loadModal);
    } catch (e) {
      loadErr.textContent = "network error: " + e.message;
    }
  }

  // Click on overlay (outside the modal box) closes the modal.
  for (const overlay of [saveModal, loadModal]) {
    if (!overlay) continue;
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeModal(overlay);
    });
  }
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (saveModal && saveModal.classList.contains("open")) closeModal(saveModal);
    if (loadModal && loadModal.classList.contains("open")) closeModal(loadModal);
  });

  return {};
}
