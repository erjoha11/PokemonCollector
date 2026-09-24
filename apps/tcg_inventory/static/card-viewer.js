// Card viewer: a click on any [data-card-view] element (a card's photo --
// names link to /cards/{id} instead; see card_view_attrs in templates/partials/macros.html) shows the
// card large in the shared <dialog id="card-viewer">, with links to its
// detail page and to Dex. Delegated from document so it also works for rows htmx
// swaps in later (e.g. re-sorting Most valuable cards).
(function () {
  function viewer() { return document.getElementById("card-viewer"); }

  function open(trigger) {
    var dlg = viewer();
    if (!dlg) return;
    var d = trigger.dataset;
    var img = dlg.querySelector(".card-viewer-image");
    var noImage = dlg.querySelector(".card-viewer-noimage");
    img.onerror = null;
    if (d.img) {
      img.hidden = false;
      noImage.hidden = true;
      // The large version is derived from the thumbnail's URL; fall back to
      // the thumbnail itself if that host doesn't serve it.
      img.onerror = function () {
        img.onerror = null;
        if (d.imgFallback && img.src !== d.imgFallback) img.src = d.imgFallback;
      };
      img.src = d.img;
      img.alt = d.name || "";
    } else {
      img.hidden = true;
      img.removeAttribute("src");
      noImage.hidden = false;
    }
    dlg.querySelector(".card-viewer-name").textContent = d.name || "";
    dlg.querySelector(".card-viewer-meta").textContent = d.meta || "";
    dlg.querySelector(".card-viewer-price").textContent = d.price && d.price !== "-" ? d.price : "";
    dlg.querySelector(".card-viewer-dex").href = d.dex || "#";
    var detail = dlg.querySelector(".card-viewer-detail");
    if (detail) {
      detail.href = d.detail || "#";
      detail.hidden = !d.detail;
    }
    if (typeof dlg.showModal === "function") dlg.showModal();
    else dlg.setAttribute("open", "");
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-card-view]");
    if (trigger) {
      event.preventDefault();
      open(trigger);
      return;
    }
    var dlg = viewer();
    if (!dlg || !dlg.open) return;
    // Close on the Close button, or a click on the backdrop (the dialog
    // element itself, outside its content box).
    if (event.target.closest(".card-viewer-close") || event.target === dlg) dlg.close();
  });
})();
