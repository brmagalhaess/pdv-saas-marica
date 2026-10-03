const CACHE = "pdv2-shell-v5-suporte-sistema";
const SHELL = [
  "/", "/login/", "/app/", "/platform/", "/offline/",
  "/assets/ui.css", "/assets/landing.css", "/assets/core.js",
  "/assets/landing.js", "/assets/auth.js", "/assets/app.js",
  "/assets/pwa.js", "/assets/icon-192.png", "/assets/icon-512.png",
  "/manifest.webmanifest"
];
self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(async () =>
        await caches.match(url.pathname) || await caches.match("/offline/")
      )
    );
    return;
  }
  if (!url.pathname.startsWith("/assets/") && url.pathname !== "/manifest.webmanifest") return;
  event.respondWith(
    fetch(request).then(response => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(request, copy));
      }
      return response;
    }).catch(() => caches.match(request))
  );
});
