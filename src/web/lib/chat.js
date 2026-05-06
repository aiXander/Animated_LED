// Chat panel — composer + log of bubbles. Each user message hits
// /agent/chat and the response (which may include a tool call result)
// is rendered as one assistant bubble. Sessions live in-memory only:
// `new session` simply clears sessionId and the log.

import { escapeHtml } from "./util.js";

export function bindChat({
  log,
  input,
  sendBtn,
  statusEl,
  newBtn,
  form,
  // Mobile: when the chat tab gets focused we may want to scroll the
  // composer into view (virtual keyboard pushes it). Hook is optional.
  onFocus = null,
}) {
  let sessionId = null;
  let inflight = false;

  function setChatStatus(text, cls = "dim") {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = cls;
  }

  function renderUserBubble(text) {
    const row = document.createElement("div");
    row.className = "turn user row";
    row.innerHTML = `<div class="bubble">${escapeHtml(text)}</div>`;
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
    return row;
  }

  function renderAsstBubble(turn) {
    const row = document.createElement("div");
    row.className = "turn asst row";
    if (turn.pending) {
      row.innerHTML = `<div><div class="bubble dim">…</div></div>`;
      log.appendChild(row);
      log.scrollTop = log.scrollHeight;
      return row;
    }

    const hasText = !!(turn.assistant_text && turn.assistant_text.trim());
    const hasTool = !!turn.tool_call;

    let bubbleHtml = "";
    if (hasText) {
      bubbleHtml = `<div class="bubble">${escapeHtml(turn.assistant_text)}</div>`;
    } else if (!hasTool) {
      const fallback = `(no response${turn.finish_reason ? ` · finish_reason=${turn.finish_reason}` : ""})`;
      bubbleHtml = `<div class="bubble dim">${escapeHtml(fallback)}</div>`;
    }

    let toolHtml = "";
    if (hasTool) {
      const okClass = turn.tool_result && turn.tool_result.ok ? "ok" : (turn.tool_result ? "bad" : "dim");
      const baseSummary = turn.tool_result
        ? (turn.tool_result.ok
           ? `tool: ${turn.tool_call.name} ✓`
           : `tool: ${turn.tool_call.name} ✗ ${escapeHtml(turn.tool_result.error || "error")}`)
        : `tool: ${turn.tool_call.name}`;
      const u = turn.usage || null;
      const usageHtml = u
        ? ` <span class="dim">· tokens: ${u.input_tokens ?? "?"} in / ${u.output_tokens ?? "?"} out</span>`
        : "";
      const retriesHtml = (typeof turn.retries_used === "number" && turn.retries_used > 0)
        ? ` <span class="warn">· ${turn.retries_used} retr${turn.retries_used === 1 ? "y" : "ies"}</span>`
        : "";
      toolHtml = `
        <details class="toolblock">
          <summary class="${okClass}">${baseSummary}${retriesHtml}${usageHtml}</summary>
          <pre>arguments: ${escapeHtml(JSON.stringify(turn.tool_call.arguments, null, 2))}
result: ${escapeHtml(JSON.stringify(turn.tool_result, null, 2))}</pre>
        </details>`;
    }
    const errHtml = turn.error
      ? `<div class="meta bad">error: ${escapeHtml(turn.error)}</div>` : "";
    row.innerHTML = `
      <div>
        ${bubbleHtml}
        ${toolHtml}
        ${errHtml}
      </div>`;
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
    return row;
  }

  function replaceAsstBubble(placeholder, turn) {
    placeholder.remove();
    return renderAsstBubble(turn);
  }

  async function send(text) {
    if (inflight) return;
    inflight = true;
    if (sendBtn) sendBtn.disabled = true;
    setChatStatus("thinking…");
    renderUserBubble(text);
    const placeholder = renderAsstBubble({ pending: true });
    try {
      const body = { message: text };
      if (sessionId) body.session_id = sessionId;
      const r = await fetch("/agent/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${r.status}`);
      }
      const data = await r.json();
      sessionId = data.session_id;
      replaceAsstBubble(placeholder, {
        assistant_text: data.assistant_text || "",
        tool_call: data.tool_call,
        tool_result: data.tool_result,
        finish_reason: data.finish_reason,
        usage: data.usage,
        retries_used: data.retries_used,
      });
      setChatStatus(`history ${data.history_size}`);
    } catch (e) {
      setChatStatus("error: " + e.message, "bad");
      replaceAsstBubble(placeholder, {
        assistant_text: "(request failed)",
        error: e.message,
      });
    } finally {
      inflight = false;
      if (sendBtn) sendBtn.disabled = false;
    }
  }

  if (form) {
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      send(text);
    });
  }

  if (input) {
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        if (form) form.requestSubmit();
      }
    });
    if (onFocus) input.addEventListener("focus", onFocus);
  }

  if (newBtn) {
    newBtn.addEventListener("click", () => {
      sessionId = null;
      log.innerHTML = "";
      setChatStatus("new session");
      input && input.focus();
    });
  }

  async function loadAgentConfig({ onCrossfade } = {}) {
    try {
      const r = await fetch("/agent/config");
      if (!r.ok) return;
      const cfg = await r.json();
      if (!cfg.enabled) setChatStatus("agent disabled in config", "bad");
      if (typeof cfg.default_crossfade_seconds === "number" && onCrossfade) {
        onCrossfade(cfg.default_crossfade_seconds);
      }
    } catch (_) { /* ignore */ }
  }

  return { send, loadAgentConfig, setChatStatus };
}
