import assert from "node:assert/strict";
import test from "node:test";

import {
  STORAGE_KEY,
  applyTicks,
  clearTick,
  mergeTick,
  offlineBanner,
  readPending,
  serverAnswers,
  writePending,
} from "../../src/souschef/static/list-sync.js";

test("tick then untick keeps the later", () => {
  let pending = {};
  pending = mergeTick(pending, { itemId: 7, checked: true, ts: 100 });
  pending = mergeTick(pending, { itemId: 7, checked: false, ts: 200 });
  assert.deepEqual(pending["7"], { checked: false, ts: 200 });

  const stale = mergeTick(pending, { itemId: "7", checked: true, ts: 150 });
  assert.deepEqual(stale["7"], { checked: false, ts: 200 });
});

test("a 404 result clears the entry", () => {
  const pending = {
    "4": { checked: true, ts: 1 },
    "5": { checked: false, ts: 2 },
  };
  assert.deepEqual(clearTick(pending, "4", 404), {
    "5": { checked: false, ts: 2 },
  });
  assert.deepEqual(pending["4"], { checked: true, ts: 1 });
});

test("a success clears the entry and any other response keeps it", () => {
  const pending = {
    "4": { checked: true, ts: 1 },
    "5": { checked: false, ts: 2 },
  };
  assert.deepEqual(clearTick(pending, "4", 200), {
    "5": { checked: false, ts: 2 },
  });
  assert.equal(clearTick(pending, "5", 500), pending);
  assert.equal(clearTick(pending, "5", 303), pending);
  assert.equal(clearTick(pending, "9", 200), pending);
});

test("re-applying pending ticks to markup sets each checkbox", () => {
  const forms = [
    tickForm("1", { pressed: "false", value: "1", checked: false }),
    tickForm("2", { pressed: "true", value: "0", checked: true }),
  ];
  const root = {
    querySelector(selector) {
      const match = selector.match(/^form\[action="([^"]+)"\]$/);
      if (!match) return null;
      return forms.find((form) => form.action === match[1]) || null;
    },
  };

  applyTicks(root, {
    "1": { checked: true, ts: 1 },
    "2": { checked: false, ts: 2 },
    "9": { checked: true, ts: 3 },
  });

  assert.equal(forms[0].button.getAttribute("aria-pressed"), "true");
  assert.equal(forms[0].classList.contains("checked"), true);
  assert.equal(forms[0].input.value, "0");
  assert.equal(forms[1].button.getAttribute("aria-pressed"), "false");
  assert.equal(forms[1].classList.contains("checked"), false);
  assert.equal(forms[1].input.value, "1");
});

test("localStorage failures are ignored", () => {
  const broken = {
    getItem() {
      throw new Error("blocked");
    },
    setItem() {
      throw new Error("blocked");
    },
  };
  assert.deepEqual(readPending(broken), {});
  assert.equal(writePending(broken, { "1": { checked: true, ts: 1 } }), false);

  const storage = memory("{");
  assert.deepEqual(readPending(storage), {});

  const pending = { "3": { checked: true, ts: 9 } };
  assert.equal(writePending(storage, pending), true);
  assert.deepEqual(readPending(storage), pending);
});

test("the offline banner quotes the page time", () => {
  const stamp = new Date(2026, 9, 30, 17, 42);
  assert.equal(offlineBanner(stamp.toISOString()), "Offline — list from 5:42 pm");
  assert.equal(offlineBanner("not-a-time"), "Offline");
});

test("a healthz response means the server is reachable", async () => {
  let seen = null;
  const fetchImpl = async (url, options) => {
    seen = { url, cache: options.cache };
    return { status: 503 };
  };
  assert.equal(await serverAnswers(fetchImpl, 1000), true);
  assert.equal(seen.url, "/healthz");
  assert.equal(seen.cache, "no-store");
});

test("a health check that does not answer is unreachable", async () => {
  const fetchImpl = (_url, { signal }) =>
    new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason));
    });
  assert.equal(await serverAnswers(fetchImpl, 20), false);
});

function tickForm(id, { pressed, value, checked }) {
  const button = {
    attrs: { "aria-pressed": pressed },
    getAttribute(name) {
      return this.attrs[name] ?? null;
    },
    setAttribute(name, next) {
      this.attrs[name] = String(next);
    },
  };
  const input = { value };
  const names = new Set(checked ? ["shop-item", "checked"] : ["shop-item"]);
  return {
    action: `/shopping/item/${id}/check`,
    button,
    input,
    classList: {
      contains(name) {
        return names.has(name);
      },
      toggle(name, force) {
        const on = force === undefined ? !names.has(name) : Boolean(force);
        if (on) names.add(name);
        else names.delete(name);
        return on;
      },
    },
    querySelector(selector) {
      if (selector === "button.tick") return button;
      if (selector === 'input[name="checked"]') return input;
      return null;
    },
  };
}

function memory(initial) {
  const data = new Map();
  if (initial !== undefined) data.set(STORAGE_KEY, initial);
  return {
    getItem(key) {
      return data.has(key) ? data.get(key) : null;
    },
    setItem(key, value) {
      data.set(key, String(value));
    },
  };
}
