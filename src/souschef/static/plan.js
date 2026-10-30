(function () {
  var EDGE = 60;
  var drag = null;

  function closest(target, selector) {
    var node = target && target.nodeType === 1 ? target : target && target.parentElement;
    return node && node.closest ? node.closest(selector) : null;
  }

  function postMove(id, day) {
    var form = document.createElement("form");
    var input = document.createElement("input");
    form.method = "post";
    form.action = "/plan/" + id + "/move";
    input.type = "hidden";
    input.name = "plan_date";
    input.value = day;
    form.appendChild(input);
    document.body.appendChild(form);
    form.submit();
  }

  function floorY() {
    var bar = document.querySelector(".tabbar");
    var height = window.innerHeight;
    if (!bar) return height;
    var rect = bar.getBoundingClientRect();
    // The phone tab bar covers the bottom edge. The desktop one is a side column.
    if (rect.top > height * 0.5 && rect.bottom >= height - 1) return rect.top;
    return height;
  }

  function dayUnderPointer() {
    var node = document.elementFromPoint(drag.x, drag.y);
    return node ? node.closest("[data-drop]") : null;
  }

  function mark(day) {
    if (drag.day === day) return;
    if (drag.day) drag.day.classList.remove("drop-target");
    drag.day = day;
    if (day) day.classList.add("drop-target");
  }

  function place() {
    drag.ghost.style.left = (drag.x - drag.dx) + "px";
    drag.ghost.style.top = (drag.y - drag.dy) + "px";
  }

  function tick() {
    if (!drag) return;
    var y = drag.y;
    var limit = floorY();
    var delta = 0;
    if (y < EDGE) delta = y - EDGE;
    else if (y > limit - EDGE) delta = y - (limit - EDGE);
    if (delta) {
      var step = Math.round(delta / 3);
      if (step === 0) step = delta > 0 ? 1 : -1;
      if (step > 20) step = 20;
      if (step < -20) step = -20;
      window.scrollBy(0, step);
      mark(dayUnderPointer());
    }
    drag.frame = requestAnimationFrame(tick);
  }

  function clear() {
    if (!drag) return;
    cancelAnimationFrame(drag.frame);
    drag.ghost.remove();
    drag.meal.classList.remove("dragging");
    if (drag.day) drag.day.classList.remove("drop-target");
    drag = null;
  }

  function neighbor(row, down) {
    var drops = document.querySelectorAll("[data-drop]");
    var previous = null;
    var i;
    for (i = 0; i < drops.length; i++) {
      var drop = drops[i];
      if (drop === row) continue;
      var position = row.compareDocumentPosition(drop);
      if (down) {
        if (position & Node.DOCUMENT_POSITION_FOLLOWING) return drop;
      } else if (position & Node.DOCUMENT_POSITION_PRECEDING) {
        previous = drop;
      }
    }
    return previous;
  }

  document.addEventListener("pointerdown", function (event) {
    if (event.button !== 0 || drag) return;
    var handle = closest(event.target, "[data-move]");
    if (!handle) return;
    var meal = handle.closest(".plan-meal");
    var row = handle.closest("[data-plan-day]");
    if (!meal || !row) return;
    handle.setPointerCapture(event.pointerId);
    event.preventDefault();
    var rect = meal.getBoundingClientRect();
    var ghost = meal.cloneNode(true);
    ghost.classList.add("plan-ghost");
    ghost.querySelectorAll("[data-move]").forEach(function (node) {
      node.removeAttribute("data-move");
    });
    ghost.style.width = rect.width + "px";
    ghost.style.left = rect.left + "px";
    ghost.style.top = rect.top + "px";
    document.body.appendChild(ghost);
    meal.classList.add("dragging");
    drag = {
      id: handle.getAttribute("data-move"),
      pointerId: event.pointerId,
      from: row.getAttribute("data-plan-day"),
      meal: meal,
      ghost: ghost,
      dx: event.clientX - rect.left,
      dy: event.clientY - rect.top,
      x: event.clientX,
      y: event.clientY,
      day: null,
      frame: 0,
    };
    drag.frame = requestAnimationFrame(tick);
  });

  document.addEventListener("pointermove", function (event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.preventDefault();
    drag.x = event.clientX;
    drag.y = event.clientY;
    place();
    mark(dayUnderPointer());
  });

  document.addEventListener("pointerup", function (event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    drag.x = event.clientX;
    drag.y = event.clientY;
    var day = dayUnderPointer();
    var id = drag.id;
    var from = drag.from;
    var target = day ? day.getAttribute("data-plan-day") : "";
    clear();
    if (id && target && target !== from) postMove(id, target);
  });

  document.addEventListener("pointercancel", function (event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    clear();
  });

  document.addEventListener("keydown", function (event) {
    if ((event.key !== "ArrowUp" && event.key !== "ArrowDown") || event.repeat) return;
    var handle = closest(event.target, "[data-move]");
    if (!handle) return;
    var row = handle.closest("[data-plan-day]");
    if (!row) return;
    var next = neighbor(row, event.key === "ArrowDown");
    if (!next) return;
    var id = handle.getAttribute("data-move");
    var day = next.getAttribute("data-plan-day");
    if (!id || !day) return;
    event.preventDefault();
    postMove(id, day);
  });
})();
