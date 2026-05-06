// Minimal service worker. Exists only so Chrome's PWA install prompt fires
// on Android — we deliberately don't cache anything (the LED control UI is
// real-time and lives behind auth). Every fetch passes through to the
// network unchanged.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => { /* pass-through */ });
