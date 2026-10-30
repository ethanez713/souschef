import {
  HEALTH_TIMEOUT_MS,
  applyTicks,
  clearTick,
  clearedBy,
  mergeTick,
  offlineBanner,
  readPending,
  serverAnswers,
  writePending,
} from "/static/list-sync.js";

// Reachable means GET /healthz answered. The phone stays "online" in a shop
// while the server at home is off, so navigator.onLine is never consulted.
let reachable = null;
let installEvent = null;
let wake = Promise.resolve();
let flushing = null;
let flushAgain = false;

const OFFLINE_LABEL = "Available when connected";
const WAITING_LABEL = "Waiting to sync";

function tickId(form) {
  const action = form.getAttribute("action") || "";
  const match = action.match(/\/shopping\/item\/(\d+)\/check$/);
  return match ? match[1] : "";
}

function isTickForm(form) {
  return Boolean(form && form.tagName === "FORM" && tickId(form));
}

function rememberTick(form) {
  const id = tickId(form);
  const input = form.querySelector('input[name="checked"]');
  if (!id || !input) return;
  const pending = mergeTick(readPending(localStorage), {
    itemId: id,
    checked: input.value === "1",
    ts: Date.now(),
  });
  writePending(localStorage, pending);
  applyTicks(document, pending);
  renderOfflineState();
}

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!isTickForm(form)) return;
  event.preventDefault();
  event.stopPropagation();
  // Keep the tick even if the post never leaves the phone. A later flush sends it.
  rememberTick(form);
  if (reachable !== false) flushPending();
}, true);

function controlsNeedingServer(root) {
  const found = [];
  root.querySelectorAll("form").forEach((form) => {
    const method = (form.getAttribute("method") || "get").toLowerCase();
    if (method !== "post" || isTickForm(form)) return;
    form.querySelectorAll("button, input, select, textarea").forEach((control) => {
      if (control.type === "hidden") return;
      if (control.classList.contains("list-row-open")) return;
      if (control.hasAttribute("data-sheet-close")) return;
      if (control.hasAttribute("data-copy") || control.hasAttribute("data-copy-target")) return;
      found.push(control);
    });
  });
  root.querySelectorAll('[data-sheet="shop-add"], [data-sheet="pantry-add"]').forEach((control) => {
    found.push(control);
  });
  return found;
}

function lock(control, label, relabel) {
  if (!control.hasAttribute("data-offline-locked")) {
    control.setAttribute("data-offline-locked", "1");
    control.setAttribute("data-was-disabled", control.disabled ? "1" : "0");
  }
  control.disabled = true;
  control.title = label;
  if (!relabel || control.tagName !== "BUTTON") return;
  if (control.children.length > 0) {
    if (!control.hasAttribute("data-online-aria")) {
      control.setAttribute("data-online-aria", control.getAttribute("aria-label") || "");
    }
    control.setAttribute("aria-label", label);
    return;
  }
  if (!control.hasAttribute("data-online-label")) {
    control.setAttribute("data-online-label", control.textContent);
  }
  control.textContent = label;
}

function unlock(control) {
  if (control.getAttribute("data-was-disabled") !== "1") control.disabled = false;
  control.removeAttribute("data-was-disabled");
  if (control.title === OFFLINE_LABEL || control.title === WAITING_LABEL) {
    control.removeAttribute("title");
  }
  if (control.hasAttribute("data-online-label")) {
    control.textContent = control.getAttribute("data-online-label");
    control.removeAttribute("data-online-label");
  }
  if (control.hasAttribute("data-online-aria")) {
    const aria = control.getAttribute("data-online-aria");
    if (aria) control.setAttribute("aria-label", aria);
    else control.removeAttribute("aria-label");
    control.removeAttribute("data-online-aria");
  }
  control.removeAttribute("data-offline-locked");
}

function renderBadge(pending) {
  const badge = document.getElementById("sync-badge");
  if (!badge) return;
  const count = Object.keys(pending).length;
  if (!count) {
    badge.hidden = true;
    badge.textContent = "";
    return;
  }
  badge.hidden = false;
  badge.textContent = `${count} waiting to sync`;
}

function renderBanner(root) {
  const existing = document.getElementById("offline-banner");
  if (reachable !== false) {
    if (existing) existing.remove();
    return;
  }
  const banner = existing || document.createElement("p");
  banner.id = "offline-banner";
  banner.className = "notice offline-banner";
  banner.setAttribute("role", "status");
  banner.textContent = offlineBanner(root.dataset.generatedAt || "");
  if (!existing) root.prepend(banner);
}

function renderOfflineState() {
  document.querySelectorAll("[data-offline-locked]").forEach(unlock);
  const pending = readPending(localStorage);
  renderBadge(pending);
  const root = document.querySelector("main[data-generated-at]");
  if (!root) return;
  renderBanner(root);
  if (reachable === false) {
    root.setAttribute("data-offline", "");
    controlsNeedingServer(root).forEach((control) => lock(control, OFFLINE_LABEL, true));
  } else {
    root.removeAttribute("data-offline");
  }
  // Stocking would record ticks the server has not stored yet.
  if (Object.keys(pending).length > 0) {
    const stock = root.querySelector('form[action="/shopping/stock"] button');
    if (stock && !stock.hasAttribute("data-offline-locked")) lock(stock, WAITING_LABEL, false);
  }
}

async function postTick(id, tick) {
  try {
    const response = await fetch(`/shopping/item/${id}/check`, {
      method: "POST",
      body: new URLSearchParams({ checked: tick.checked ? "1" : "0" }),
    });
    return response.status;
  } catch {
    return 0;
  }
}

async function refreshShopList() {
  const current = document.getElementById("shop-list");
  if (!current) return;
  try {
    const response = await fetch("/shopping", { cache: "no-store" });
    if (!response.ok) return;
    const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
    const next = parsed.getElementById("shop-list");
    if (!next) return;
    current.replaceWith(next);
    if (window.htmx) window.htmx.process(next);
    applyTicks(document, readPending(localStorage));
  } catch {
    // The queued ticks already show. The next load picks up the server copy.
  }
}

function flushPending() {
  if (flushing) {
    flushAgain = true;
    return flushing;
  }
  flushing = (async () => {
    do {
      flushAgain = false;
      await doFlush();
    } while (flushAgain && reachable !== false);
  })().finally(() => {
    flushing = null;
  });
  return flushing;
}

async function doFlush() {
  const snapshot = readPending(localStorage);
  const ids = Object.keys(snapshot).sort((a, b) => snapshot[a].ts - snapshot[b].ts);
  let posted = false;
  for (const id of ids) {
    const pending = readPending(localStorage);
    const tick = pending[id];
    if (!tick) continue;
    const status = await postTick(id, tick);
    const latest = readPending(localStorage);
    const current = latest[id];
    if (!current || current.ts !== tick.ts || !clearedBy(status)) continue;
    writePending(localStorage, clearTick(latest, id, status));
    posted = true;
  }
  if (posted) await refreshShopList();
  renderOfflineState();
}

function askCacheLists() {
  if (!("serviceWorker" in navigator)) return;
  const timeout = new Promise((resolve) => {
    setTimeout(resolve, HEALTH_TIMEOUT_MS);
  });
  Promise.race([navigator.serviceWorker.ready, timeout])
    .then((registration) => {
      if (registration && registration.active) {
        registration.active.postMessage({ type: "cache-lists" });
      }
    })
    .catch(() => {});
}

function scheduleWake() {
  wake = wake.then(probe).catch(() => {});
}

async function probe() {
  const wasReachable = reachable;
  reachable = await serverAnswers((url, options) => fetch(url, options), HEALTH_TIMEOUT_MS);
  renderOfflineState();
  if (!reachable) return;
  await flushPending();
  if (wasReachable !== true) askCacheLists();
}

function installed() {
  return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
}

function installDismissed() {
  try {
    return sessionStorage.getItem("souschef.installDismissed") === "1";
  } catch {
    return false;
  }
}

function keepStored() {
  if (!installed() || !navigator.storage || !navigator.storage.persist) return;
  navigator.storage.persist().catch(() => {});
}

window.addEventListener("beforeinstallprompt", (event) => {
  const nudge = document.getElementById("install-nudge");
  if (!nudge || installed() || installDismissed()) return;
  event.preventDefault();
  installEvent = event;
  nudge.hidden = false;
});

document.addEventListener("click", (event) => {
  const dismiss = event.target.closest && event.target.closest("#install-dismiss");
  const install = event.target.closest && event.target.closest("#install-action");
  if (!dismiss && !install) return;
  const nudge = document.getElementById("install-nudge");
  if (dismiss) {
    if (nudge) nudge.hidden = true;
    try {
      sessionStorage.setItem("souschef.installDismissed", "1");
    } catch {
      // Private mode can refuse storage. The line stays gone for this page.
    }
    return;
  }
  const prompt = installEvent;
  installEvent = null;
  if (nudge) nudge.hidden = true;
  if (prompt) prompt.prompt().catch(() => {});
});

window.addEventListener("appinstalled", () => {
  const nudge = document.getElementById("install-nudge");
  if (nudge) nudge.hidden = true;
  keepStored();
});

document.addEventListener("htmx:afterSwap", () => {
  applyTicks(document, readPending(localStorage));
  renderOfflineState();
});

applyTicks(document, readPending(localStorage));
renderOfflineState();
keepStored();
scheduleWake();
window.addEventListener("online", scheduleWake);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") scheduleWake();
});
