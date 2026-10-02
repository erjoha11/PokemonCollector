// Manual KPI carousel (issue #279): the Dashboard's Price movers tile also
// holds a "Recently added" slide. User-controlled only -- prev/next arrows
// and one tab per slide, no auto-rotation. The chosen slide is remembered
// per browser in localStorage (default: the first slide, Price movers).
//
// Server markup (partials/kpi_module.html) renders every slide but the
// first `hidden`, and the controls `hidden` too -- so without JS the tile
// is just Price movers. This script unhides the controls and takes over.
(function () {
  "use strict";

  function storageKey(root) {
    return "tcg.kpiCarousel." + root.getAttribute("data-kpi-carousel");
  }

  function load(root) {
    try {
      return window.localStorage.getItem(storageKey(root));
    } catch (e) {
      return null;
    }
  }

  function save(root, name) {
    try {
      window.localStorage.setItem(storageKey(root), name);
    } catch (e) {
      /* private mode / storage disabled: just don't remember */
    }
  }

  function init(root) {
    if (root.getAttribute("data-carousel-ready")) return;
    root.setAttribute("data-carousel-ready", "1");

    var slides = Array.prototype.slice.call(root.querySelectorAll("[data-slide]"));
    var tabs = Array.prototype.slice.call(root.querySelectorAll("[data-carousel-tab]"));
    var nav = root.querySelector(".kpi-carousel-nav");
    if (slides.length < 2 || !nav) return;

    var status = root.querySelector(".kpi-carousel-status");
    // Panels keep their own heading as label; the role only makes sense
    // once the tablist is visible, so it's added here rather than server-side.
    slides.forEach(function (slide) {
      slide.setAttribute("role", "tabpanel");
    });

    function indexOf(name) {
      for (var i = 0; i < slides.length; i++) {
        if (slides[i].getAttribute("data-slide") === name) return i;
      }
      return -1;
    }

    var current = 0;

    function show(i, focusTab, announce) {
      current = (i + slides.length) % slides.length;
      slides.forEach(function (slide, n) {
        slide.hidden = n !== current;
      });
      tabs.forEach(function (tab, n) {
        var on = n === current;
        tab.setAttribute("aria-selected", on ? "true" : "false");
        tab.tabIndex = on ? 0 : -1;
        if (on && focusTab) tab.focus();
      });
      save(root, slides[current].getAttribute("data-slide"));
      // Arrow-button clicks don't move focus, so say which slide is showing.
      if (announce && status && tabs[current]) {
        status.textContent = tabs[current].getAttribute("aria-label") +
          ", slide " + (current + 1) + " of " + slides.length;
      }
    }

    nav.addEventListener("click", function (event) {
      var step = event.target.closest("[data-carousel-step]");
      if (step) {
        show(current + parseInt(step.getAttribute("data-carousel-step"), 10), false, true);
        return;
      }
      var tab = event.target.closest("[data-carousel-tab]");
      if (tab) show(indexOf(tab.getAttribute("data-carousel-tab")), false, false);
    });

    // Arrow keys / Home / End move between tabs (WAI-ARIA tabs pattern).
    nav.addEventListener("keydown", function (event) {
      if (!event.target.closest("[data-carousel-tab]")) return;
      var next = null;
      if (event.key === "ArrowRight") next = current + 1;
      else if (event.key === "ArrowLeft") next = current - 1;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = slides.length - 1;
      if (next === null) return;
      event.preventDefault();
      show(next, true, false);
    });

    var saved = indexOf(load(root));
    nav.hidden = false;
    show(saved >= 0 ? saved : 0, false, false);
  }

  function initAll(scope) {
    var roots = (scope || document).querySelectorAll("[data-kpi-carousel]");
    for (var i = 0; i < roots.length; i++) init(roots[i]);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { initAll(); });
  } else {
    initAll();
  }
  // In case a future htmx swap re-renders the KPI band.
  document.addEventListener("htmx:load", function (event) { initAll(event.target); });
})();
