// Tracks cards checked for a finn.no sale listing (see /sales, app.py) in
// sessionStorage rather than relying on checkbox DOM state alone -- every
// Inventory filter/sort change re-fetches and replaces #inventory-results
// wholesale (hx-swap="innerHTML"), which would otherwise silently drop
// whatever was checked before the user narrowed the filter to find more
// cards. Deliberately session-only, not persisted server-side or across
// browser sessions -- this is scratch state for "I'm building a listing
// right now", not a durable draft.
(function () {
  const STORAGE_KEY = "tcg-sale-list";

  function readSelection() {
    try {
      const raw = sessionStorage.getItem(STORAGE_KEY);
      return raw ? new Set(JSON.parse(raw)) : new Set();
    } catch (e) {
      return new Set();
    }
  }

  function writeSelection(set) {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(Array.from(set)));
    } catch (e) {
      // sessionStorage unavailable (private mode, quota) -- selection just
      // won't survive an htmx swap; not worth failing the page over.
    }
  }

  function updateBar(set) {
    const bar = document.getElementById("sale-list-bar");
    const count = document.getElementById("sale-list-count");
    if (!bar || !count) return;
    count.textContent = set.size;
    bar.hidden = set.size === 0;
  }

  function rehydrateCheckboxes(set) {
    document.querySelectorAll("input.sale-select").forEach((cb) => {
      cb.checked = set.has(cb.dataset.cardId);
    });
  }

  // /sales with no card_ids (e.g. the Orders page's "Sell on finn.no"
  // button): offer the selection still held from Inventory, if any.
  function salesUrl(ids) {
    return "/sales?" + ids.map((id) => "card_ids=" + encodeURIComponent(id)).join("&");
  }

  function offerResume(set) {
    const resume = document.getElementById("sale-list-resume");
    if (!resume || set.size === 0) return;
    resume.querySelector("a").href = salesUrl(Array.from(set));
    const count = document.getElementById("sale-list-resume-count");
    if (count) count.textContent = set.size;
    resume.hidden = false;
  }

  function init() {
    const selection = readSelection();
    rehydrateCheckboxes(selection);
    updateBar(selection);
    offerResume(selection);

    document.body.addEventListener("change", (evt) => {
      if (!evt.target.matches("input.sale-select")) return;
      const id = evt.target.dataset.cardId;
      const current = readSelection();
      if (evt.target.checked) {
        current.add(id);
      } else {
        current.delete(id);
      }
      writeSelection(current);
      updateBar(current);
    });

    document.body.addEventListener("htmx:afterSwap", (evt) => {
      if (evt.target && evt.target.id === "inventory-results") {
        rehydrateCheckboxes(readSelection());
      }
    });

    const generateBtn = document.getElementById("sale-list-generate");
    if (generateBtn) {
      generateBtn.addEventListener("click", () => {
        const ids = Array.from(readSelection());
        if (ids.length === 0) return;
        window.location.href = salesUrl(ids);
      });
    }

    const clearBtn = document.getElementById("sale-list-clear");
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        writeSelection(new Set());
        rehydrateCheckboxes(new Set());
        updateBar(new Set());
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
