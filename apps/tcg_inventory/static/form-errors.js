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
// other error status gets the generic "Could not save" text, and a request
// that got no answer at all (htmx:sendError / htmx:timeout, #401) a
// "Could not reach the server" one. The slot is cleared and re-hidden when
// the next request starts.
//
// A form whose request may have done its work even when the answer was lost
// (the Run-now buttons on /sync-status, #401) sets its own text for both:
//
//   <form ... data-error-message="Lost contact ... ({error}) ...">
//
// "{error}" becomes "error 504", "network error" or "timed out".
//
// data-refocus="<button id>" on a form puts focus back on that button once
// the request is done, if hx-disabled-elt dropped it to <body> meanwhile.
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

  function showError(evt, defaultText, errorLabel) {
    if (!isSave(evt)) return;
    var elt = evt.detail.elt;
    var slot = findSlot(elt);
    if (!slot) return;
    var custom = elt && elt.closest && elt.closest('[data-error-message]');
    // Unhide first, then set the text, so screen readers announce the
    // role="alert" content.
    slot.hidden = false;
    if (custom) {
      slot.textContent = custom.getAttribute('data-error-message').replace('{error}', errorLabel);
    } else {
      slot.textContent = defaultText;
    }
    slot.scrollIntoView({ block: 'center' });
  }

  document.addEventListener('htmx:responseError', function (evt) {
    var xhr = evt.detail.xhr;
    if (isSave(evt) && xhr.status === 422 && xhr.responseText) {
      var slot = findSlot(evt.detail.elt);
      if (!slot) return;
      slot.hidden = false;
      slot.textContent = xhr.responseText;
      slot.scrollIntoView({ block: 'center' });
      return;
    }
    showError(evt,
      'Could not save (error ' + xhr.status + ') -- nothing was changed. Try again, or reload the page.',
      'error ' + xhr.status);
  });

  var NO_ANSWER = 'Could not reach the server -- the change may not have been saved. Reload the page to check, then try again.';

  document.addEventListener('htmx:sendError', function (evt) {
    showError(evt, NO_ANSWER, 'network error');
  });

  document.addEventListener('htmx:timeout', function (evt) {
    showError(evt, NO_ANSWER, 'timed out');
  });

  document.addEventListener('htmx:afterRequest', function (evt) {
    var elt = evt.detail.elt;
    var form = elt && elt.closest && elt.closest('[data-refocus]');
    if (!form) return;
    var button = document.getElementById(form.getAttribute('data-refocus'));
    var active = document.activeElement;
    // Only when focus was lost, never pulled away from wherever the user
    // moved it during the run.
    if (button && (!active || active === document.body || active === button)) {
      button.focus();
    }
  });
})();
