// Master-set blocks (partials/master_set_block.html, #385).
//
// 1. Copy lists: each button carries its list's plain text (built
//    server-side in app.py) in data-copy-text. A click shows that list in
//    the block's "Show text" textarea and copies it: the button reads
//    "Copied" for ~2 s on success; on failure (no clipboard API, permission
//    denied) the textarea is opened and selected with a Ctrl/Cmd+C hint.
//    Every id carries the block's dom_key, so two blocks on one page each
//    copy their own lists.
// 2. Filter pills: after a pill swaps the grid (hx-select, outerHTML), focus
//    would fall to <body>; put it back on the active pill of the same group.
(function () {
  "use strict";

  function setStatus(id, text) {
    var el = id && document.getElementById(id);
    if (el) el.textContent = text;
  }

  function showText(button) {
    var area = document.getElementById(button.dataset.copyTarget);
    if (!area) return null;
    var text = button.dataset.copyText || "";
    area.value = text;
    area.rows = Math.min(text.split("\n").length, 10);
    var group = button.closest("[role=group]");
    if (group) {
      group.querySelectorAll("[data-copy-list]").forEach(function (b) {
        b.setAttribute("aria-pressed", b === button ? "true" : "false");
      });
    }
    return area;
  }

  function fallback(button, area) {
    var details = area && area.closest("details");
    if (details) details.open = true;
    if (area) {
      area.focus();
      area.select();
    }
    setStatus(button.dataset.copyStatus, "Press Ctrl/Cmd+C to copy");
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy-list]");
    if (!button) return;
    var area = showText(button);
    setStatus(button.dataset.copyStatus, "");
    var text = button.dataset.copyText || "";
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      fallback(button, area);
      return;
    }
    navigator.clipboard.writeText(text).then(
      function () {
        var label = button.querySelector(".copy-label");
        if (!label) return;
        if (!label.dataset.original) label.dataset.original = label.innerHTML;
        label.textContent = "Copied";
        clearTimeout(button._copiedTimer);
        button._copiedTimer = setTimeout(function () {
          label.innerHTML = label.dataset.original;
        }, 2000);
      },
      function () {
        fallback(button, area);
      }
    );
  });

  // Filter pills: remember which pill group of which grid was used, then
  // focus that group's active pill in the swapped-in grid.
  var pending = null;
  document.addEventListener("htmx:beforeRequest", function (event) {
    var pill = event.target.closest && event.target.closest(".master-set-grid .viz-filter-pill");
    if (!pill) return;
    var grid = pill.closest(".master-set-grid");
    var group = pill.closest(".viz-filter");
    pending = { gridId: grid && grid.id, group: group && group.getAttribute("aria-label") };
  });
  document.addEventListener("htmx:afterSettle", function () {
    if (!pending) return;
    var grid = pending.gridId && document.getElementById(pending.gridId);
    var groups = grid ? grid.querySelectorAll(".viz-filter") : [];
    for (var i = 0; i < groups.length; i++) {
      if (groups[i].getAttribute("aria-label") === pending.group) {
        var active = groups[i].querySelector(".viz-filter-pill.active");
        if (active) active.focus();
      }
    }
    pending = null;
  });
})();
