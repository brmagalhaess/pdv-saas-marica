// Bump VERSION on every release: the old cache is dropped when the new worker activates.
const VERSION = "2.0.0-painel.3";
const CACHE = "pdv2-shell-" + VERSION;
const PAGES = ["/", "/login/", "/app/", "/platform/", "/offline/"];
const SHELL = [
  ...PAGES,
  "/assets/ui.css", "/assets/landing.css", "/assets/core.js",
  "/assets/landing.js", "/assets/auth.js", "/assets/app.js",
  "/assets/pwa.js", "/assets/icon-192.png", "/assets/icon-512.png",
  "/assets/icon-maskable-192.png", "/assets/icon-maskable-512.png",
  "/assets/pdv2-simbolo.svg", "/assets/pdv2-por-crowntech.svg",
  "/assets/pdv2-por-crowntech-negativo.svg", "/assets/pdv2-por-crowntech-empilhado.svg",
  "/manifest.webmanifest"
];
self.addEventListener("install", event => {
  // "reload" skips the HTTP cache, so the offline copy never starts from a stale file.
  event.waitUntil(caches.open(CACHE)
    .then(cache => cache.addAll(SHELL.map(url => new Request(url, { cache: "reload" }))))
    .then(() => self.skipWaiting()));
});
self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});
function keep(key, response) {
  if (!response.ok || response.redirected) return;
  const copy = response.clone();
  caches.open(CACHE).then(cache => cache.put(key, copy));
}
self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).then(response => {
        // Keep the offline shell in step with the scripts it loads.
        if (PAGES.includes(url.pathname)) keep(url.pathname, response);
        return response;
      }).catch(async () =>
        await caches.match(url.pathname) || await caches.match("/offline/")
      )
    );
    return;
  }
  if (!url.pathname.startsWith("/assets/") && url.pathname !== "/manifest.webmanifest") return;
  // Scripts and styles are revalidated with the server on every load, so a new
  // app.js is never held back by the HTTP cache; the stored copy is only for offline use.
  const code = /\.(js|css|webmanifest)$/.test(url.pathname);
  event.respondWith(
    fetch(request, code ? { cache: "no-cache" } : {}).then(response => {
      keep(request, response);
      return response;
    }).catch(() => caches.match(request))
  );
});
