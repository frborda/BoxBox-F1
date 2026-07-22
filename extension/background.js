// BoxBox-F1 Companion — service worker.
//
// Flujo: la app abre https://f1login.fastf1.dev?port=N (mismo disparador que
// usa FastF1, así la app no cambia). Acá guardamos el puerto, llevamos al
// usuario al login de Formula 1 y, cuando la cuenta queda logueada
// (my-account), abrimos connect.html para entregar el token al puerto local.

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  const url = changeInfo.url;
  if (!url) return;

  if (url.includes("f1login.fastf1.dev")) {
    let port = null;
    try {
      port = new URL(url).searchParams.get("port");
    } catch (e) { /* URL rara: ignorar */ }
    if (port) {
      chrome.storage.local.set({ port: port, pendingAuth: true });
      chrome.tabs.update(tabId, { url: "https://account.formula1.com/" });
    }
    return;
  }

  if (url.includes("account.formula1.com") && url.includes("my-account")) {
    chrome.storage.local.get(["pendingAuth"], (r) => {
      if (r && r.pendingAuth) {
        chrome.tabs.update(tabId, { url: chrome.runtime.getURL("connect.html") });
      }
    });
  }
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.action === "getCookie") {
    chrome.cookies
      .get({ url: "https://livetiming.formula1.com", name: "login-session" })
      .then((c) => sendResponse({ cookie: c ? c.value : null }))
      .catch((e) => sendResponse({ error: String(e) }));
    return true; // respuesta asíncrona
  }
});
