(function () {
  var PACK = {
    bag: 1, jar: 1, bottle: 1, can: 1, box: 1, pack: 1,
    bunch: 1, head: 1, dozen: 1, loaf: 1, carton: 1,
  };
  var GRAM = 28.3495;
  var OZ_PER_LB = 16;
  var TSP = 4.92892;
  var TBSP = 14.7868;
  var CUP = 236.588;
  var QT = 946.353;
  var GAL = 3785.41;
  var SWITCH = 0.98;
  var EIGHTHS = ["", "⅛", "¼", "⅜", "½", "⅝", "¾", "⅞"];
  var IRREGULAR = { leaf: "leaves", loaf: "loaves", tomato: "tomatoes", potato: "potatoes" };
  var MASS = { g: 1, kg: 1000, oz: GRAM, lb: GRAM * OZ_PER_LB };
  var VOLUME = { ml: 1, l: 1000, cup: CUP, qt: QT, "fl oz": 29.5735 };

  function attr(root, name) {
    var value = root.getAttribute(name);
    return value ? value : "";
  }

  function trim(value, places) {
    return value.toFixed(places).replace(/0+$/, "").replace(/\.$/, "");
  }

  function fraction(value, step) {
    var eighths = Math.round(value * 8 / step) * step;
    if (value > 0) eighths = Math.max(eighths, step);
    var whole = Math.floor(eighths / 8);
    var part = eighths % 8;
    return (whole ? String(whole) : "") + EIGHTHS[part] || "0";
  }

  function plural(noun, amount) {
    if (Math.abs(amount - 1) < 0.05) return noun;
    var parts = noun.split(" ");
    var last = parts.pop();
    var prefix = parts.length ? parts.join(" ") + " " : "";
    var word = IRREGULAR[last];
    if (!word) {
      if (last.length > 1 && last.endsWith("y") && "aeiou".indexOf(last.charAt(last.length - 2)) < 0) {
        word = last.slice(0, -1) + "ies";
      } else if (/s$|x$|ch$|sh$/.test(last)) {
        word = last + "es";
      } else {
        word = last + "s";
      }
    }
    return prefix + word;
  }

  function metric(value, small, large) {
    if (value >= 1000) return trim(value / 1000, 2) + " " + large;
    return (value >= 100 ? String(Math.round(value)) : trim(value, 1)) + " " + small;
  }

  function usWeight(grams) {
    var ounces = grams / GRAM;
    if (ounces >= OZ_PER_LB * SWITCH) {
      var rounded = Math.round(ounces);
      var pounds = Math.floor(rounded / OZ_PER_LB);
      var remainder = rounded % OZ_PER_LB;
      return remainder >= 4 ? pounds + " lb " + remainder + " oz" : pounds + " lb";
    }
    if (ounces < 4) return fraction(ounces, 2) + " oz";
    return Math.round(ounces) + " oz";
  }

  function usVolume(ml) {
    if (ml < TBSP * SWITCH) return fraction(ml / TSP, 2) + " tsp";
    if (ml < CUP / 4 * SWITCH) return fraction(ml / TBSP, 2) + " tbsp";
    if (ml < QT * SWITCH) {
      var cups = ml / CUP;
      return fraction(cups, 1) + (cups <= 1.06 ? " cup" : " cups");
    }
    if (ml < GAL * SWITCH) return trim(ml / QT, 1) + " qt";
    return trim(ml / GAL, 1) + " gal";
  }

  function formatCount(qty, noun, name) {
    var whole = Math.abs(qty - Math.round(qty)) < 0.05;
    var shown = whole ? Math.round(qty) : qty;
    var label = noun || name || "count";
    if (whole && shown >= 12 && shown % 12 === 0 && noun && name.toLowerCase().indexOf(noun.toLowerCase()) >= 0) {
      return (shown / 12) + " dozen";
    }
    var text = whole ? String(shown) : trim(qty, 1);
    return text + " " + plural(label, shown);
  }

  function formatSize(qty, unit, system, countNoun, name) {
    var key = (unit || "").trim().toLowerCase();
    if (!key || key === "count") return formatCount(qty, countNoun, name);
    if (MASS[key]) {
      var grams = qty * MASS[key];
      return system === "metric" ? metric(grams, "g", "kg") : usWeight(grams);
    }
    if (VOLUME[key]) {
      var ml = qty * VOLUME[key];
      return system === "metric" ? metric(ml, "ml", "L") : usVolume(ml);
    }
    return trim(qty, 1) + " " + key;
  }

  function isVolume(base) {
    return base === "ml" || base === "milliliter" || base === "l" || base === "liter";
  }

  function isMass(base) {
    return base === "g" || base === "gram" || base === "kg" || base === "oz" || base === "lb";
  }

  function dimensionWords(base, countNoun) {
    if (isVolume(base)) return ["ml", "cup", "qt"];
    if (base === "count") return [countNoun || "count"];
    if (isMass(base)) return ["g", "oz", "lb"];
    return [countNoun || base || "count"];
  }

  function baseWord(base, countNoun) {
    if (base === "count") return countNoun || "count";
    if (isVolume(base)) return "ml";
    if (isMass(base)) return "g";
    return countNoun || base || "count";
  }

  function chipWords(root) {
    var packageNoun = attr(root, "data-package-noun");
    var countNoun = attr(root, "data-count-noun");
    var base = attr(root, "data-base-unit") || "count";
    var hasSize = attr(root, "data-package-qty") && attr(root, "data-package-unit");
    var generic = !packageNoun && hasSize ? "pack" : "";
    var preferred = packageNoun || countNoun || generic || baseWord(base, countNoun);
    var words = [];
    function add(word) {
      if (word && words.indexOf(word) < 0) words.push(word);
    }
    add(preferred);
    add(countNoun);
    add(generic);
    dimensionWords(base, countNoun).forEach(add);
    return { words: words, selected: preferred };
  }

  function packageWord(root) {
    var noun = attr(root, "data-package-noun");
    if (noun) return noun;
    var count = attr(root, "data-count-noun");
    if (count && PACK[count]) return count;
    if (attr(root, "data-package-qty") && attr(root, "data-package-unit")) return "pack";
    return "";
  }

  function showTextUnit(slot) {
    var id = slot.getAttribute("data-unit-id") || "unit";
    var placeholder = slot.getAttribute("data-unit-placeholder") || "";
    var label = document.createElement("label");
    label.htmlFor = id;
    label.textContent = "Unit";
    var input = document.createElement("input");
    input.id = id;
    input.name = "unit";
    input.placeholder = placeholder;
    slot.replaceChildren(label, input);
  }

  function showChips(slot, words, selected) {
    var label = document.createElement("label");
    label.textContent = "Unit";
    var group = document.createElement("div");
    group.className = "daychips";
    group.setAttribute("role", "radiogroup");
    group.setAttribute("aria-label", "Unit");
    words.forEach(function (word) {
      var choice = document.createElement("label");
      var input = document.createElement("input");
      input.type = "radio";
      input.name = "unit";
      input.value = word;
      input.checked = word === selected;
      var text = document.createElement("span");
      text.textContent = word;
      choice.append(input, text);
      group.append(choice);
    });
    slot.replaceChildren(label, group);
  }

  function clearPackageFields(form) {
    var fields = form.querySelector("[data-package-fields]");
    if (!fields) return;
    fields.hidden = true;
    fields.querySelectorAll("input").forEach(function (input) {
      input.value = "";
      input.required = false;
    });
  }

  function showCaption(form, root) {
    var caption = form.querySelector("[data-package-caption]");
    if (!caption) return;
    var text = caption.querySelector("[data-package-text]");
    var qty = attr(root, "data-package-qty");
    var unit = attr(root, "data-package-unit");
    var word = packageWord(root);
    if (!qty || !unit || !word) {
      caption.hidden = true;
      if (text) text.textContent = "";
      return;
    }
    var system = form.getAttribute("data-unit-system") || "us";
    var size = formatSize(
      parseFloat(qty), unit, system, attr(root, "data-count-noun"), attr(root, "data-name")
    );
    if (text) text.textContent = "1 " + word + " = " + size;
    caption.hidden = false;
    var label = form.querySelector("[data-package-label]");
    if (label) label.textContent = "How much is one " + word + "?";
  }

  function render(form, root) {
    var slot = form.querySelector("[data-unit-slot]");
    var amount = form.querySelector('input[name="quantity"], input[name="amount"]');
    if (amount) amount.value = "1";
    clearPackageFields(form);
    var known = root && !root.hasAttribute("data-new") && attr(root, "data-base-unit");
    if (!slot) return;
    if (!known) {
      showTextUnit(slot);
      var caption = form.querySelector("[data-package-caption]");
      if (caption) caption.hidden = true;
      return;
    }
    var chips = chipWords(root);
    showChips(slot, chips.words, chips.selected);
    showCaption(form, root);
  }

  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input || !input.matches || !input.matches('input[type="hidden"][name="name"]')) return;
    var form = input.closest("form[data-add-form]");
    if (!form) return;
    render(form, input.closest("[data-picker-sheet]"));
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) return;
    var button = target.closest("[data-package-change]");
    if (!button) return;
    var form = button.closest("form");
    if (!form) return;
    var fields = form.querySelector("[data-package-fields]");
    if (!fields) return;
    fields.hidden = false;
    var root = form.querySelector("[data-picker-sheet]");
    var qty = fields.querySelector('input[name="package_qty"]');
    var unit = fields.querySelector('input[name="package_unit"]');
    if (qty && !qty.value && root) qty.value = attr(root, "data-package-qty");
    if (unit && !unit.value && root) unit.value = attr(root, "data-package-unit");
    if (qty) qty.focus();
  });
})();
