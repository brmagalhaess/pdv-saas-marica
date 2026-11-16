if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1")) {
  // A controller change on a page that already had one means a new version took over.
  const controlled = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (controlled) window.dispatchEvent(new CustomEvent("pdv2:update"));
  });
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope:"/", updateViaCache:"none" }).then(registration => {
      // Tabs left open for days still pick up a new release when they come back to the front.
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") registration.update().catch(() => {});
      });
    }).catch(() => {
      // A página continua funcional sem instalação.
    });
  });
}
