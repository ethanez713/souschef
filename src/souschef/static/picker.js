(function () {
  var CHOICE_FIELDS = [
    "id", "name", "base-unit", "count-noun", "package-qty", "package-unit", "package-noun",
  ];

  function openPicker(opener) {
    var dialog = document.getElementById(opener.getAttribute("data-sheet"));
    if (!dialog) return;
    if (!dialog.open && dialog.showModal) dialog.showModal();
    var field = dialog.querySelector("[data-picker-query]");
    if (!field) return;
    // Android Chrome opens the keyboard only for a focus that happens inside the tap.
    field.focus();
    if (window.htmx) window.htmx.trigger(field, "picker-open");
  }

  function choose(choice) {
    var dialog = choice.closest("dialog");
    if (!dialog) return;
    var root = document.querySelector(
      '[data-picker-sheet="' + (window.CSS && CSS.escape ? CSS.escape(dialog.id) : dialog.id) + '"]'
    );
    if (!root) return;
    var pickedId = choice.getAttribute("data-id") || "";
    var pickedName = choice.getAttribute("data-name") || "";
    CHOICE_FIELDS.forEach(function (key) {
      var attr = "data-" + key;
      if (choice.hasAttribute(attr)) root.setAttribute(attr, choice.getAttribute(attr));
      else root.removeAttribute(attr);
    });
    if (choice.hasAttribute("data-new")) root.setAttribute("data-new", "1");
    else root.removeAttribute("data-new");
    var hidden = root.querySelector('input[type="hidden"]');
    if (hidden) {
      var useName = root.getAttribute("data-picker-submit") === "name";
      hidden.value = useName ? pickedName : (pickedId || pickedName);
      hidden.dispatchEvent(new Event("change", { bubbles: true }));
    }
    var shown = root.querySelector("[data-picker-value]");
    if (shown) shown.textContent = pickedName;
    var trigger = root.querySelector("[data-picker-open]");
    if (trigger && pickedName) trigger.removeAttribute("data-empty");
    dialog.close();
  }

  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-picker-open]");
    if (opener) {
      openPicker(opener);
      return;
    }
    var choice = event.target.closest("[data-picker-choice]");
    if (choice) choose(choice);
  });
})();
