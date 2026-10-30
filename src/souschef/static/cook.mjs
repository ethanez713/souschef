import { editAmount, scaleServings } from "./scale.mjs";

var MAX_SERVINGS = 50;

function formatAmount(value) {
  var number = Number(value);
  if (!isFinite(number)) return "";
  return String(Math.round(number * 100) / 100);
}

var EIGHTHS = ["", "⅛", "¼", "⅜", "½", "⅝", "¾", "⅞"];

// Reads like the server's quantities: "1½ cups", "¼ oz", "2 onions".
function kitchenText(amount, node) {
  var whole = Math.floor(amount + 1e-9);
  var eighths = Math.round((amount - whole) * 8);
  if (eighths === 8) { whole += 1; eighths = 0; }
  var number = Math.abs(amount - (whole + eighths / 8)) < 0.02
    ? (whole || !eighths ? String(whole) : "") + EIGHTHS[eighths]
    : formatAmount(amount);
  var unit = node.getAttribute(amount > 1 ? "data-many" : "data-one") || "";
  return (number + " " + unit).trim();
}

function readState() {
  var input = document.querySelector("[data-servings-input]");
  var lines = [];
  document.querySelectorAll("[data-line]").forEach(function (node) {
    lines.push({
      id: node.getAttribute("data-line"),
      amount: Number(node.getAttribute("data-amount")),
      perServing: Number(node.getAttribute("data-per")),
      edited: node.getAttribute("data-edited") === "1",
    });
  });
  return { servings: Number(input && input.value), lines: lines };
}

function writeState(state, keep) {
  var input = document.querySelector("[data-servings-input]");
  if (input) input.value = String(state.servings);
  var shown = document.querySelector("[data-servings]");
  if (shown) shown.textContent = String(state.servings);
  state.lines.forEach(function (line) {
    var node = document.querySelector('[data-line="' + line.id + '"]');
    if (node) {
      node.textContent = kitchenText(line.amount, node);
      node.setAttribute("data-amount", String(line.amount));
      if (typeof line.perServing === "number") node.setAttribute("data-per", String(line.perServing));
      if (line.edited) node.setAttribute("data-edited", "1");
    }
    var qty = document.querySelector('[data-qty="' + line.id + '"]');
    if (qty && qty !== keep) qty.value = formatAmount(line.amount);
  });
}

function holdWakeLock() {
  if (!navigator.wakeLock || typeof navigator.wakeLock.request !== "function") return;
  function request() {
    if (document.visibilityState !== "visible") return;
    try {
      navigator.wakeLock.request("screen").catch(function () {});
    } catch (err) {
      return;
    }
  }
  request();
  document.addEventListener("visibilitychange", request);
}

if (document.querySelector("[data-cook]")) {
  holdWakeLock();
  document.addEventListener("click", function (event) {
    var step = event.target.closest("[data-step]");
    if (!step) return;
    event.preventDefault();
    var state = readState();
    var next = state.servings + Number(step.getAttribute("data-step"));
    if (next < 1 || next > MAX_SERVINGS) return;
    writeState(scaleServings(state, next));
  });
  document.addEventListener("input", function (event) {
    var qty = event.target.closest("[data-qty]");
    if (!qty) return;
    writeState(editAmount(readState(), qty.getAttribute("data-qty"), qty.value), qty);
  });
  var form = document.getElementById("cook-form");
  if (form) {
    form.addEventListener("submit", function () {
      var state = readState();
      writeState(state);
      state.lines.forEach(function (line) {
        if (line.edited) return;
        var proposed = form.querySelector('[data-proposed="' + line.id + '"]');
        if (proposed) proposed.disabled = true;
      });
    });
  }
}
