// Inventory column chooser: which optional columns (th/td[data-col]) are
// hidden, remembered per browser in localStorage -- a viewing preference,
// not data. Re-applied after every htmx swap of #inventory-results, since
// filters/sorts/pages replace the whole table. Classification/Location/
// Notes are additionally left out server-side when empty (see app.inventory).
(function () {
  var KEY = "tcg-inventory-hidden-cols";

  function hidden() {
    try {
      return new Set(JSON.parse(localStorage.getItem(KEY) || "[]"));
    } catch (e) {
      return new Set();
    }
  }

  function save(set) {
    try {
      localStorage.setItem(KEY, JSON.stringify(Array.from(set)));
    } catch (e) {
      // Storage unavailable -- the choice just won't survive a reload.
    }
  }

  function apply() {
    var set = hidden();
    document.querySelectorAll(".inventory-table [data-col]").forEach(function (el) {
      el.classList.toggle("col-hidden", set.has(el.dataset.col));
    });
    document.querySelectorAll("[data-col-toggle]").forEach(function (cb) {
      cb.checked = !set.has(cb.dataset.colToggle);
    });
  }

  document.addEventListener("change", function (event) {
    var cb = event.target.closest("[data-col-toggle]");
    if (!cb) return;
    var set = hidden();
    if (cb.checked) set.delete(cb.dataset.colToggle);
    else set.add(cb.dataset.colToggle);
    save(set);
    apply();
  });

  document.addEventListener("htmx:afterSwap", function (event) {
    if (event.target && event.target.id === "inventory-results") apply();
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", apply);
  else apply();
})();
