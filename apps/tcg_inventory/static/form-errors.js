// One error pattern for every htmx form (issue #228).
//
// htmx doesn't swap a 4xx/5xx response, so on its own a rejected save is a
// silent no-op. The server answers a rejected value with a 422 and a short
// plain-text message naming the field; this puts it into the submitting
// form's error slot:
//
//   <p class="warnings" role="alert" hidden data-form-error></p>
//
// Nothing is swapped, so everything the user typed stays as it was. Any
// other error status gets the generic "Could not save" text. The slot is
// cleared and re-hidden when the next request starts.
(function () {
  if (window.__tcgFormErrorsInit) return;
  window.__tcgFormErrorsInit = true;

  function findSlot(elt) {
    if (!elt || !elt.closest) return null;
    var form = elt.closest('form');
    var slot = form && form.querySelector('[data-form-error]');
    if (slot) return slot;
    var card = elt.closest('.card');
    return card ? card.querySelector('[data-form-error]') : null;
  }

  // Only saves: a GET inside the same form (the cart's card search, Edit
  // order's relink search) neither wipes a shown error nor reports
  // "Could not save" for itself.
  function isSave(evt) {
    var cfg = evt.detail.requestConfig;
    return !!cfg && String(cfg.verb).toLowerCase() !== 'get';
  }

  document.addEventListener('htmx:beforeRequest', function (evt) {
    if (!isSave(evt)) return;
    var slot = findSlot(evt.detail.elt);
    if (slot) {
      slot.textContent = '';
      slot.hidden = true;
    }
  });

  document.addEventListener('htmx:responseError', function (evt) {
    if (!isSave(evt)) return;
    var slot = findSlot(evt.detail.elt);
    if (!slot) return;
    var xhr = evt.detail.xhr;
    // Unhide first, then set the text, so screen readers announce the
    // role="alert" content.
    slot.hidden = false;
    if (xhr.status === 422 && xhr.responseText) {
      slot.textContent = xhr.responseText;
    } else {
      slot.textContent = 'Could not save (error ' + xhr.status + ') -- nothing was changed. Try again, or reload the page.';
    }
    slot.scrollIntoView({ block: 'center' });
  });
})();
