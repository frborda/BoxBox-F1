// Entrega el token F1TV al servidor local del capturador.
//
// Chrome 130+ (Local/Private Network Access) exige declarar el destino con
// targetAddressSpace: "loopback" para que una extensión llegue a localhost —
// exactamente lo que le falta a la extensión vieja de FastF1. Si aun así el
// POST falla, mostramos el token para pegarlo a mano en "Paste token…".

const $ = (id) => document.getElementById(id);

function extractToken(cookie) {
  try {
    const parsed = JSON.parse(decodeURIComponent(cookie));
    return (parsed && parsed.data && parsed.data.subscriptionToken) || null;
  } catch (e) {
    return null;
  }
}

async function getCookie() {
  const resp = await chrome.runtime.sendMessage({ action: "getCookie" });
  if (resp && resp.error) throw new Error(resp.error);
  return (resp && resp.cookie) || null;
}

async function postTo(host, port, cookie) {
  const resp = await fetch(`http://${host}:${port}/auth`, {
    method: "POST",
    cache: "no-cache",
    targetAddressSpace: "loopback",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ loginSession: cookie }),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
}

async function connect() {
  const status = $("status");
  status.textContent = "Connecting…";
  status.className = "";
  try {
    const cookie = await getCookie();
    if (!cookie) {
      throw new Error(
        "login-session cookie not found — sign in at formula1.com and retry."
      );
    }
    const stored = await chrome.storage.local.get("port");
    const port = stored && stored.port;
    if (!port) {
      throw new Error(
        'no port stored — start from the app: "Sign in with F1TV…".'
      );
    }
    let delivered = false;
    let lastError = null;
    for (const host of ["localhost", "127.0.0.1"]) {
      try {
        await postTo(host, port, cookie);
        delivered = true;
        break;
      } catch (e) {
        lastError = e;
      }
    }
    if (delivered) {
      status.textContent = "✔ Token delivered — you can close this tab.";
      status.className = "ok";
      chrome.storage.local.remove("pendingAuth");
      return;
    }
    // plan B: entregar por el protocolo f1telemetry:// (diálogo nativo
    // "Abrir BoxBox-F1") o copiar para "Paste token…"
    const token = extractToken(cookie);
    if (token) {
      $("tokenbox").style.display = "block";
      $("token").value = token;
      $("openapp").onclick = () => {
        location.href = "f1telemetry://auth?token=" + encodeURIComponent(token);
        status.textContent =
          'If the browser asked to open "BoxBox-F1", accept — the ' +
          "capturer picks the token up automatically.";
        status.className = "warn";
      };
      status.textContent =
        "Could not reach the app on localhost (" + lastError.message + "). " +
        'Use "Open in the app", or copy the token below.';
      status.className = "warn";
      return;
    }
    throw lastError;
  } catch (e) {
    status.textContent = "Error: " + e.message;
    status.className = "err";
  }
}

$("connect").addEventListener("click", connect);
$("copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("token").value);
  $("copy").textContent = "Copied ✔";
});
connect(); // intento automático al abrir
