// Card modal (issue #280): a plain left click on any [data-card-modal]
// element -- card names via card_link, card photos via card_view_attrs (see
// templates/partials/macros.html) -- opens the card page in base.html's
// shared <dialog id="card-modal"> instead of navigating, so closing it
// leaves the page underneath exactly as it was. /cards/{id} stays a real
// full page: ctrl/cmd/shift/alt-click, middle-click and no-JS still go there.
//
// - The panel (GET /cards/{id}/panel) is fetched and inserted with
//   innerHTML, then htmx.process() wires up htmx forms inside it.
// - An AbortController cancels a stale request, so a quick second click
//   can never be overwritten by the first card's late response.
// - No pushState/hx-push-url: htmx 1.9's popstate handler would re-swap
//   Inventory's own snapshot and lose the user's state. Android Back fires
//   `cancel` on a showModal() dialog, which closes it.
// - Chart.js is lazy-loaded once, after showModal() (a hidden canvas
//   measures 0 wide); charts are destroyed and the body emptied on close.
// - [data-card-zoom] (the card page's photo, full page and modal alike)
//   toggles a zoomed photo in place -- this replaced the separate
//   #card-viewer lightbox, so there is never a dialog inside a dialog.
// Delegated from document so it also works for rows htmx swaps in later.
(function () {
  if (window.__tcgCardModalInit) return;
  window.__tcgCardModalInit = true;

  var controller = null;   // AbortController of the in-flight panel request
  var returnFocus = null;  // element to refocus on close
  var downOutside = false; // mousedown landed on the backdrop
  var chartsLoading = null;

  function dialog() { return document.getElementById("card-modal"); }
  function content() {
    var dlg = dialog();
    return dlg && dlg.querySelector(".card-modal-content");
  }

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    if (attrs) for (var k in attrs) node.setAttribute(k, attrs[k]);
    if (text != null) node.textContent = text;
    return node;
  }

  // Same markup as partials/card_panel.html's header, so the swap from
  // placeholder to the real panel doesn't jump.
  function header(title, sub, fullHref) {
    var head = el("header", { "class": "card-modal-head" });
    var titles = el("div", { "class": "card-modal-titles" });
    titles.appendChild(el("h2", { id: "card-modal-title", "class": "card-modal-title" }, title || "Card"));
    titles.appendChild(el("p", { "class": "card-modal-sub muted" }, sub || ""));
    head.appendChild(titles);
    var actions = el("div", { "class": "card-modal-actions" });
    if (fullHref) actions.appendChild(el("a", { "class": "card-modal-full", href: fullHref }, "Open full page"));
    var close = el("button", { type: "button", "class": "card-modal-close", "data-card-modal-close": "", "aria-label": "Close" });
    close.innerHTML = "&times;";
    actions.appendChild(close);
    head.appendChild(actions);
    return head;
  }

  function destroyCharts(root) {
    if (!root || !window.Chart || !window.Chart.getChart) return;
    root.querySelectorAll("canvas").forEach(function (canvas) {
      var chart = window.Chart.getChart(canvas);
      if (chart) chart.destroy();
    });
  }

  function reset() {
    var box = content();
    if (!box) return;
    destroyCharts(box);
    box.innerHTML = "";
    box.removeAttribute("aria-busy");
  }

  function placeholder(trigger, fullHref) {
    var box = content();
    var d = trigger.dataset;
    var name = d.name || trigger.textContent.trim();
    box.appendChild(header(name, d.meta, fullHref));
    var body = el("div", { "class": "card-modal-body card-modal-loading" });
    if (d.img) {
      var img = el("img", { "class": "card-modal-loading-img", alt: name });
      img.onerror = function () {
        img.onerror = null;
        if (d.imgFallback && img.src !== d.imgFallback) img.src = d.imgFallback;
      };
      img.src = d.img;
      body.appendChild(img);
    }
    var text = el("div", { "class": "card-modal-loading-text" });
    if (d.price && d.price !== "-" && d.price !== "–") text.appendChild(el("p", { "class": "card-modal-loading-price" }, d.price));
    text.appendChild(el("p", { "class": "muted" }, "Loading card…"));
    body.appendChild(text);
    box.appendChild(body);
    box.setAttribute("aria-busy", "true");
  }

  function showError(title, message, fullHref) {
    reset();
    var box = content();
    box.appendChild(header(title, "", fullHref));
    var body = el("div", { "class": "card-modal-body" });
    body.appendChild(el("p", { "class": "warnings", role: "alert" }, message));
    var actions = el("p", { "class": "card-modal-error-actions" });
    if (fullHref) actions.appendChild(el("a", { href: fullHref }, "Open full page"));
    actions.appendChild(el("button", { type: "button", "class": "secondary", "data-card-modal-close": "" }, "Close"));
    body.appendChild(actions);
    box.appendChild(body);
    focusClose();
  }

  function focusClose() {
    var dlg = dialog();
    var btn = dlg && dlg.querySelector(".card-modal-close");
    if (btn) btn.focus({ preventScroll: true });
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  // Chart.js (204KB) + the shared renderer, once per page, and only when a
  // card with a chart is actually opened.
  function loadCharts() {
    if (window.Chart && window.initTcgChart) return Promise.resolve();
    if (!chartsLoading) {
      chartsLoading = (window.Chart ? Promise.resolve() : loadScript("/static/chart.umd.js"))
        .then(function () { return window.initTcgChart ? null : loadScript("/static/tcg-charts.js"); })
        .catch(function (err) { chartsLoading = null; throw err; });
    }
    return chartsLoading;
  }

  function startCharts(ctl) {
    var box = content();
    var ids = [];
    box.querySelectorAll('script[type="application/json"][id$="-data"]').forEach(function (s) {
      ids.push(s.id.slice(0, -5));
    });
    if (!ids.length) return;
    loadCharts().then(function () {
      var dlg = dialog();
      if (ctl !== controller || !dlg.open) return; // closed or switched meanwhile
      ids.forEach(function (id) { window.initTcgChart(id); });
    }).catch(function () { /* the "View as table" fallback is still there */ });
  }

  function open(trigger) {
    var dlg = dialog();
    var href = trigger.getAttribute(trigger.tagName === "A" ? "href" : "data-href") || "";
    var m = href.match(/^\/cards\/(\d+)/);
    if (!dlg || !m || typeof dlg.showModal !== "function") return false;
    var fullHref = "/cards/" + m[1];

    if (controller) controller.abort();
    var ctl = new AbortController();
    controller = ctl;
    if (!dlg.open) returnFocus = trigger;
    reset();
    placeholder(trigger, fullHref);
    if (!dlg.open) {
      dlg.showModal();
      document.documentElement.classList.add("card-modal-open");
    }
    dlg.scrollTop = 0;
    focusClose();

    fetch(fullHref + "/panel", { signal: ctl.signal, credentials: "same-origin", headers: { Accept: "text/html" } })
      .then(function (resp) {
        if (ctl !== controller) return;
        // Expired session: auth_guard's 303 to /login was followed. Go to
        // the full page (which lands on the login form) rather than showing
        // the login page inside the modal.
        if (resp.redirected && new URL(resp.url, location.href).pathname === "/login") {
          location.href = fullHref;
          return;
        }
        if (resp.status === 404) {
          showError("Card not found", "This card no longer exists.", null);
          return;
        }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text().then(function (html) {
          if (ctl !== controller) return;
          reset();
          var box = content();
          box.innerHTML = html;
          if (window.htmx) window.htmx.process(box);
          focusClose();
          startCharts(ctl);
        });
      })
      .catch(function (err) {
        if (ctl !== controller || (err && err.name === "AbortError")) return;
        showError("Could not load the card", "Could not load the card. Check your connection and try again, or open the full page.", fullHref);
      });
    return true;
  }

  function isPlainClick(event) {
    return event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;
  }

  function outside(dlg, event) {
    var r = dlg.getBoundingClientRect();
    return event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom;
  }

  document.addEventListener("click", function (event) {
    var zoom = event.target.closest("[data-card-zoom]");
    if (zoom) {
      var wrap = zoom.closest(".card-detail");
      if (wrap) {
        var zoomed = wrap.classList.toggle("is-zoomed");
        zoom.setAttribute("aria-expanded", zoomed ? "true" : "false");
      }
      return;
    }

    var dlg = dialog();
    if (dlg && dlg.open && event.target.closest("[data-card-modal-close]")) {
      dlg.close();
      return;
    }

    var trigger = event.target.closest("[data-card-modal]");
    if (!trigger || event.defaultPrevented) return;
    // Links keep their normal new-tab/new-window behaviour on modified
    // clicks; the card page itself never opens a modal over itself.
    if (trigger.tagName === "A" && !isPlainClick(event)) return;
    if (/^\/cards\//.test(location.pathname)) return;
    if (open(trigger)) event.preventDefault();
  });

  function wireDialog() {
    var dlg = dialog();
    if (!dlg || dlg.__tcgWired) return;
    dlg.__tcgWired = true;
    // Close on a backdrop click only when both mousedown and click land
    // outside the dialog box: a click on its scrollbar, or a text selection
    // dragged out of it, must not close it.
    dlg.addEventListener("mousedown", function (event) {
      downOutside = event.target === dlg && outside(dlg, event);
    });
    dlg.addEventListener("click", function (event) {
      if (downOutside && event.target === dlg && outside(dlg, event)) dlg.close();
      downOutside = false;
    });
    dlg.addEventListener("close", function () {
      if (controller) controller.abort();
      controller = null;
      reset();
      document.documentElement.classList.remove("card-modal-open");
      var back = returnFocus;
      returnFocus = null;
      if (back && document.contains(back)) back.focus({ preventScroll: true });
    });
  }

  // Delete-missing inside the modal (#278): drop the card's row from the
  // "Missing from Dex" list underneath, if that's the page below.
  document.addEventListener("cardDeleted", function (event) {
    var id = event.detail && event.detail.id;
    var row = id != null && document.getElementById("missing-card-" + id);
    var dlg = dialog();
    if (!row || (dlg && dlg.contains(row))) return;
    var list = row.closest("#missing-cards");
    row.remove();
    var count = list && list.querySelector("[data-missing-count]");
    if (count) count.textContent = String(Math.max(0, parseInt(count.textContent, 10) - 1));
  });

  // A page restored from the back/forward cache must not come back with the
  // modal still open.
  window.addEventListener("pageshow", function (event) {
    var dlg = dialog();
    if (event.persisted && dlg && dlg.open) dlg.close();
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wireDialog);
  else wireDialog();
})();
