// Pending shop ticks. Last write per item wins. The page posts these when
// /healthz answers; nothing here touches the network or the DOM's globals.

export const STORAGE_KEY = "souschef.pendingTicks";
export const HEALTH_TIMEOUT_MS = 3000;

function asChecked(value) {
  if (value === true || value === 1 || value === "1") return true;
  if (value === false || value === 0 || value === "0") return false;
  return null;
}

export function mergeTick(pending, tick) {
  const id = String(tick.itemId);
  const ts = Number(tick.ts);
  const checked = asChecked(tick.checked);
  if (!/^\d+$/.test(id) || !Number.isFinite(ts) || checked === null) return pending;
  const existing = pending[id];
  if (existing && existing.ts > ts) return pending;
  return { ...pending, [id]: { checked, ts } };
}

export function clearedBy(status) {
  return status === 404 || (status >= 200 && status < 300);
}

export function clearTick(pending, itemId, status) {
  const id = String(itemId);
  if (!clearedBy(status) || !Object.hasOwn(pending, id)) return pending;
  const next = { ...pending };
  delete next[id];
  return next;
}

export function applyTicks(root, pending) {
  for (const [id, tick] of Object.entries(pending)) {
    if (!/^\d+$/.test(id) || !tick || asChecked(tick.checked) === null) continue;
    const form = root.querySelector(`form[action="/shopping/item/${id}/check"]`);
    if (!form) continue;
    const on = asChecked(tick.checked);
    form.classList.toggle("checked", on);
    const button = form.querySelector("button.tick");
    if (button) button.setAttribute("aria-pressed", on ? "true" : "false");
    const input = form.querySelector('input[name="checked"]');
    if (input) input.value = on ? "0" : "1";
  }
}

export function readPending(storage) {
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    let pending = {};
    for (const [id, tick] of Object.entries(parsed)) {
      if (!tick || typeof tick !== "object") continue;
      pending = mergeTick(pending, { itemId: id, checked: tick.checked, ts: tick.ts });
    }
    return pending;
  } catch {
    return {};
  }
}

export function writePending(storage, pending) {
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(pending));
    return true;
  } catch {
    return false;
  }
}

export function offlineBanner(generatedAt) {
  const when = clockTime(generatedAt);
  if (!when) return "Offline";
  return `Offline — list from ${when}`;
}

function clockTime(generatedAt) {
  const date = new Date(generatedAt);
  if (Number.isNaN(date.getTime())) return "";
  const hours24 = date.getHours();
  const minutes = String(date.getMinutes()).padStart(2, "0");
  const suffix = hours24 >= 12 ? "pm" : "am";
  const hours = hours24 % 12 || 12;
  return `${hours}:${minutes} ${suffix}`;
}

// Any HTTP response counts. A degraded /healthz is still the server answering.
export async function serverAnswers(fetchImpl, timeoutMs = HEALTH_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    await fetchImpl("/healthz", { signal: controller.signal, cache: "no-store" });
    return true;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}
