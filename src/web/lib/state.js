// /ws/state plumbing — connect, auto-reconnect with backoff, fall back to a
// 1Hz GET poll while the socket is down so the UI never freezes. The
// caller passes a single onState(snapshot) callback; everything DOM-shaped
// lives in their bootstrap.

export function fetchStateOnce(onState) {
  return fetch("/state")
    .then((r) => r.json())
    .then(onState)
    .catch(() => { /* WS will catch up */ });
}

export function fetchStateOnceRaw() {
  return fetch("/state").then((r) => r.json());
}

export async function loadTopology() {
  const r = await fetch("/topology");
  return r.json();
}

export function connectStateWs(onState, { onOpen, onClose } = {}) {
  let backoff = 500;
  let fallbackTimer = null;

  const startFallback = () => {
    if (fallbackTimer) return;
    fallbackTimer = setInterval(() => fetchStateOnce(onState), 1000);
  };
  const stopFallback = () => {
    if (!fallbackTimer) return;
    clearInterval(fallbackTimer);
    fallbackTimer = null;
  };

  const open = () => {
    const url = (location.protocol === "https:" ? "wss:" : "ws:")
              + "//" + location.host + "/ws/state";
    const ws = new WebSocket(url);
    ws.onopen = () => {
      backoff = 500;
      stopFallback();
      onOpen && onOpen();
    };
    ws.onmessage = (ev) => {
      try { onState(JSON.parse(ev.data)); } catch (_) { /* ignore */ }
    };
    const reconnect = () => {
      onClose && onClose();
      startFallback();
      setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, 5000);
    };
    ws.onclose = reconnect;
    ws.onerror = () => { try { ws.close(); } catch (_) { /* ignore */ } };
  };
  open();
}
