// Orders page cart + card picker (issue #255: moved out of the old
// transactions.html inline <script> so the Purchased and Sold tabs share
// one copy). Loaded with a plain <script src> at the end of #main-content,
// so htmx re-evaluates it whenever it swaps #main-content (cart Register,
// Edit order -> Back); the function definitions are idempotent, and the
// document-level listeners below are registered only once per page load.
//
// Shared by the New Order cart's own search results and its "Show cards
// without an order" results -- both append into #cart-body, which only
// exists once a cart is actually open. The card picker below no longer
// depends on this guard (its target is always an explicit choice), but
// these two callers still do.
// Returns the htmx promise so callers adding several cards can chain them
// one at a time -- see addCardsToCart.
// The New Order cart's type drives which columns show: Trade adds In/Out,
// Ripped hides Price and fills 0 (ripped cards are free -- the server
// forces 0 anyway). Re-run after every row htmx appends, since those rows
// arrive with an empty required price input.
// The cart's type is a <select> on Purchased but a hidden type=sale input on
// Sold -- form.elements.type reads either. (A select-only querySelector threw
// on the Sold cart and silently broke every row append.)
function cartType(form) {
  var el = form.elements.type;
  return el ? el.value : 'purchase';
}

function syncCartType(form) {
  if (!form) return;
  var type = cartType(form);
  form.classList.toggle('cart-is-trade', type === 'trade');
  form.classList.toggle('cart-is-ripped', type === 'ripped');
  form.querySelectorAll('#cart-body input[name="price"]').forEach(function (el) {
    if (type === 'ripped') {
      el.value = 0;
      el.readOnly = true;
    } else if (el.readOnly) {
      el.readOnly = false;
      el.value = '';
    }
  });
  updateCartAutoTotal(form);
}

// Same formatting as the server's `kr` filter (app.py _format_kr).
function formatKr(v) {
  return Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' kr';
}

// New Order cart's Total (issue #202): while the field is blank, its
// placeholder shows the live Σ(card prices) + Shipping, marked "auto".
// Deliberately a placeholder, not a value -- a placeholder is never
// submitted, so an untouched Total registers as NULL (Order history then
// shows the auto figure and Remaining "—"), and only a Total the user
// actually typed is saved. Typing stops the auto figure from mattering;
// clearing the field brings it back. Trade/ripped card prices aren't cash
// for the order (NON_CASH_TYPES in app.py), so only shipping counts there.
function updateCartAutoTotal(form) {
  if (!form) return;
  var totalInput = form.querySelector('input[name="purchase_total"]');
  if (!totalInput) return;
  var type = cartType(form);
  var sum = 0;
  if (type !== 'trade' && type !== 'ripped') {
    form.querySelectorAll('#cart-body input[name="price"]').forEach(function (el) {
      var v = parseFloat(el.value);
      if (!isNaN(v)) sum += v;
    });
  }
  var shipping = parseFloat((form.querySelector('input[name="purchase_shipping"]') || {}).value);
  if (!isNaN(shipping)) sum += shipping;
  var hasInputs = form.querySelectorAll('#cart-body tr').length > 0 || !isNaN(shipping);
  totalInput.placeholder = hasInputs ? 'Total (auto ' + formatKr(sum) + ')' : 'Total (optional)';
  refreshImportedSummary(form);
}
if (!window.__tcgOrdersCartInit) {
  document.body.addEventListener('htmx:afterSwap', function (e) {
    if (e.detail.target && e.detail.target.id === 'cart-body') {
      syncCartType(e.detail.target.closest('form'));
    }
    // A cart just opened: "Open in cart" from Facebook wins (#309) arrives
    // prefilled, so its summary needs a first pass, and it opens below the
    // inbox list, so bring it into view.
    if (e.detail.target && e.detail.target.id === 'purchase-cart-container') {
      var form = document.getElementById('purchase-cart-form');
      if (form) updateCartAutoTotal(form);
      if (e.detail.target.querySelector('[data-imported-sale]')) {
        e.detail.target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }
  });
}

// `extra` (optional) carries a Facebook wins link (#309): {won_item_id,
// price}; the server adds the item's note to the row.
function addCardToCart(cardId, extra) {
  var cartBody = document.getElementById('cart-body');
  if (!cartBody) {
    alert('Open a new order first (the button above), then add cards to it.');
    return Promise.resolve();
  }
  var url = '/transactions/purchase/add-row?card_id=' + encodeURIComponent(cardId);
  if (extra) {
    Object.keys(extra).forEach(function (key) {
      if (extra[key] !== undefined && extra[key] !== null && extra[key] !== '') {
        url += '&' + encodeURIComponent(key) + '=' + encodeURIComponent(extra[key]);
      }
    });
  }
  return htmx.ajax('GET', url, { target: '#cart-body', swap: 'beforeend', indicator: '#cart-add-indicator' });
}

// --- Facebook wins in the cart (#309 slice 2) ---------------------------
// The "Imported items" panel (partials/purchase_cart.html) sits outside the
// form. Which items are linked is never stored on the panel: it's read off
// the cart rows' won_item_id inputs, so removing a row unlinks its item.

function importedPanel(form) {
  var card = form && form.closest('.card');
  return card ? card.querySelector('[data-imported-sale]') : null;
}

function itemPrice(el) {
  return el.dataset.price === '' ? NaN : parseFloat(el.dataset.price);
}

function linkedRowCount(form, itemId) {
  return form.querySelectorAll('#cart-body input[name="won_item_id"][value="' + itemId + '"]').length;
}

function importedState(form) {
  var panel = importedPanel(form);
  if (!panel) return null;
  var state = { items: 0, unlinked: 0, unlinkedKr: 0, unlinkedUnknown: 0, unpriced: 0 };
  panel.querySelectorAll('.fb-import-item').forEach(function (el) {
    var price = itemPrice(el);
    state.items += 1;
    if (isNaN(price)) state.unpriced += 1;
    if (linkedRowCount(form, el.dataset.itemId) === 0) {
      state.unlinked += 1;
      if (isNaN(price)) state.unlinkedUnknown += 1;
      else state.unlinkedKr += price;
    }
  });
  return state;
}

function unlinkedText(state) {
  return state.unlinked + ' of ' + state.items + ' item' + (state.items === 1 ? '' : 's') + ' not linked (' +
    formatKr(state.unlinkedKr) + (state.unlinkedUnknown ? ' + ?' : '') + ')';
}

// Keeps each item's "Linked to N cards" / "Lot not complete" and the
// summary above Register in step with the cart rows. Called from
// updateCartAutoTotal, which runs on every input, row add and row remove.
function refreshImportedSummary(form) {
  var panel = importedPanel(form);
  if (!panel) return;
  panel.querySelectorAll('.fb-import-item').forEach(function (el) {
    var n = linkedRowCount(form, el.dataset.itemId);
    el.classList.toggle('is-linked', n > 0);
    var status = el.querySelector('[data-item-status]');
    if (status) status.textContent = n ? 'Linked to ' + n + ' card' + (n === 1 ? '' : 's') : 'Not linked';
    var keep = el.querySelector('.fb-keep');
    if (keep) {
      keep.hidden = n === 0;
      if (n === 0) keep.querySelector('input').checked = false;
    }
  });
  var state = importedState(form);
  var summary = form.querySelector('[data-import-summary]');
  if (!summary) return;
  var unlinked = summary.querySelector('[data-import-unlinked]');
  unlinked.innerHTML = '';
  var strong = document.createElement('strong');
  if (state.unlinked) {
    strong.textContent = unlinkedText(state);
    unlinked.appendChild(strong);
    unlinked.appendChild(document.createTextNode(' — they stay in the inbox.'));
  } else {
    strong.textContent = 'All ' + state.items + ' item' + (state.items === 1 ? '' : 's') + ' linked.';
    unlinked.appendChild(strong);
  }

  // Remaining exactly as Order history computes it once registered:
  // Total − card prices − shipping (trade/ripped prices aren't cash).
  var totalInput = form.querySelector('input[name="purchase_total"]');
  var total = parseFloat(totalInput.value);
  var remaining = summary.querySelector('[data-import-remaining]');
  if (isNaN(total)) {
    remaining.hidden = true;
  } else {
    var type = cartType(form);
    var sum = 0;
    if (type !== 'trade' && type !== 'ripped') {
      form.querySelectorAll('#cart-body input[name="price"]').forEach(function (el) {
        var v = parseFloat(el.value);
        if (!isNaN(v)) sum += v;
      });
    }
    var shipping = parseFloat(form.querySelector('input[name="purchase_shipping"]').value);
    var left = Math.round((total - sum - (isNaN(shipping) ? 0 : shipping)) * 100) / 100;
    remaining.hidden = false;
    remaining.textContent = 'Remaining ' + formatKr(left) +
      (left !== 0 && state.unlinked ? ' (Total − card prices − shipping: includes what isn\'t linked yet)' : '');
  }

  var warning = summary.querySelector('[data-import-warning]');
  var message = '';
  if (isNaN(total) && (state.unlinked || state.unpriced)) {
    message = 'No Total: with items not linked or without a price, the order won\'t show what\'s unaccounted for. Enter what you paid.';
  } else if (state.unpriced) {
    message = state.unpriced + ' item' + (state.unpriced === 1 ? '' : 's') +
      ' without a known price: the Total counts only known prices. Set it to what you actually paid.';
  }
  warning.textContent = message;
  warning.hidden = !message;
}

// The prefilled Total is the sale's known prices (data-prefill-base). Until
// it's edited by hand, Shipping typed is added to it, so Remaining stays
// "what isn't linked" rather than going negative by the shipping.
function syncImportedTotal(form, evt) {
  var total = form.querySelector('input[name="purchase_total"]');
  if (!total || total.dataset.prefillBase === undefined || !evt || !evt.target) return;
  if (evt.target === total) {
    total.dataset.tracking = 'off';
    return;
  }
  if (total.dataset.tracking === 'off' || evt.target.name !== 'purchase_shipping') return;
  var base = parseFloat(total.dataset.prefillBase);
  var shipping = parseFloat(evt.target.value);
  total.value = Math.round((base + (isNaN(shipping) ? 0 : shipping)) * 100) / 100;
}
// Capture phase, so the Total is already updated when the form's own
// oninput (updateCartAutoTotal) recomputes Remaining.
if (!window.__tcgOrdersCartInit) {
  document.addEventListener('input', function (e) {
    var form = e.target && e.target.form;
    if (form && form.id === 'purchase-cart-form') syncImportedTotal(form, e);
  }, true);
}

// "Link selected": the ticked cards of one item become normal cart rows
// carrying the item's ref, note and price. A lot's price is split evenly
// over its cards, the last one taking the rounding (splitEvenly). If the
// item already has rows in the cart, the new ones come without a price
// rather than re-splitting prices that may have been typed over.
function linkSelected(itemEl) {
  var form = document.getElementById('purchase-cart-form');
  if (!itemEl || !form) return Promise.resolve();
  var boxes = Array.prototype.filter.call(itemEl.querySelectorAll('input.fb-cand'), function (el) { return el.checked; });
  var ids = [];
  boxes.forEach(function (el) { if (ids.indexOf(el.value) === -1) ids.push(el.value); });
  if (!ids.length) {
    alert('Tick the card this item is first (several for a lot).');
    return Promise.resolve();
  }
  var itemId = itemEl.dataset.itemId;
  var price = itemPrice(itemEl);
  var shares = !isNaN(price) && linkedRowCount(form, itemId) === 0 ? splitEvenly(price, ids.length) : [];
  var chain = Promise.resolve();
  ids.forEach(function (id, i) {
    chain = chain.then(function () {
      return addCardToCart(id, { won_item_id: itemId, price: shares.length ? shares[i] : '' });
    });
  });
  return chain.then(function () {
    boxes.forEach(function (el) { el.checked = false; });
    updateCartAutoTotal(form);
  });
}

// Shared by "+ New Order" (this file) and "Cancel" (partials/purchase_cart.html)
// -- both discard whatever's in the purchase cart, so both should only nag
// when there's actually something to lose.
function confirmDiscardCart() {
  if (!cartHasUnsavedWork()) return true;
  return confirm('Discard the in-progress order?');
}

// A value the cart was opened with (a Facebook sale's prefilled Total, #309)
// isn't work to lose: only one that differs from it counts.
function cartHasUnsavedWork() {
  var cartBody = document.getElementById('cart-body');
  var hasRows = !!cartBody && cartBody.children.length > 0;
  var hasTotalOrShipping = Array.prototype.some.call(
    document.querySelectorAll('#purchase-cart-container input[name="purchase_total"], #purchase-cart-container input[name="purchase_shipping"]'),
    function (el) { return el.value.trim() !== '' && el.value !== el.defaultValue; }
  );
  return hasRows || hasTotalOrShipping;
}

// The New Order cart exists only in the DOM until Register: its rows and
// every typed price are lost on any full-page navigation. Every sort header
// and filter pill on this page is a plain <a href>, so "add 12 cards, type
// prices, re-sort the card list to find the next one" used to wipe the lot
// with no warning at all. Guard the page once rather than patching each
// link, so a link added later can't quietly reintroduce it.
var leavingDeliberately = false;
if (!window.__tcgOrdersCartInit) {
  window.addEventListener('beforeunload', function (evt) {
    if (leavingDeliberately || !cartHasUnsavedWork()) return;
    evt.preventDefault();
    evt.returnValue = '';
    return '';
  });
  // confirmRegisterOrder/submitPicker switch the guard off just before
  // their request. If that request is rejected (a 422 shown in the form's
  // error slot, issue #228) or never reaches the server, the cart is still
  // on screen and unsaved -- switch the guard back on.
  ['htmx:responseError', 'htmx:sendError'].forEach(function (name) {
    document.addEventListener(name, function () { leavingDeliberately = false; });
  });
}
window.__tcgOrdersCartInit = true;

// The cart form's price/date inputs are `required`, so a blank one blocks
// native validation *before* the request reaches the server -- no
// server-side error can show, which reads exactly like "Register does
// nothing, not even an error". Check the two likely gaps explicitly.
function confirmRegisterOrder(form) {
  var cartBody = document.getElementById('cart-body');
  var rows = cartBody ? cartBody.querySelectorAll('tr') : [];
  if (rows.length === 0) {
    alert('Search for at least one card and add it to the order first.');
    return false;
  }
  var emptyPrice = Array.prototype.find.call(
    cartBody.querySelectorAll('input[name="price"]'),
    function (el) { return el.value.trim() === ''; }
  );
  if (emptyPrice) {
    alert('Enter a price for every card before registering (or use "Distribute evenly across empty prices").');
    emptyPrice.focus();
    return false;
  }
  // Facebook wins (#309): never leave imported items behind without asking.
  var state = importedState(form);
  if (state) {
    if (state.unlinked && !confirm(unlinkedText(state) + '. They stay in the inbox for a later order. Register the linked cards now?')) {
      return false;
    }
    var totalInput = form.querySelector('input[name="purchase_total"]');
    if (totalInput.value.trim() === '' && (state.unlinked || state.unpriced) &&
        !confirm('There\'s no Total, so the order won\'t show what\'s unaccounted for (items not linked or without a price). Register anyway?')) {
      totalInput.focus();
      return false;
    }
  }
  leavingDeliberately = true;
  return true;
}

// Toggle for the New Order cart's "Show cards without an order" button
// (issue #156) -- reuses the same #cart-search-results target as the typed
// search box, since browsing is just another way to fill the same list.
function toggleBrowseUnordered(btn) {
  var results = document.getElementById('cart-search-results');
  if (btn.dataset.active === '1') {
    btn.dataset.active = '';
    btn.textContent = 'Show cards without an order';
    results.innerHTML = '';
  } else {
    btn.dataset.active = '1';
    btn.textContent = 'Hide';
    htmx.ajax('GET', '/transactions/purchase/browse-unordered', { target: '#cart-search-results', swap: 'innerHTML' });
  }
}

function resetBrowseUnorderedToggle() {
  var btn = document.getElementById('browse-unordered-toggle');
  if (btn && btn.dataset.active === '1') {
    btn.dataset.active = '';
    btn.textContent = 'Show cards without an order';
  }
}

// Splits the "Remaining amount" value evenly across every still-empty price
// input in the purchase-cart table, the last one taking the rounding so the
// shares add up exactly (#312: 100 over 3 used to leave 0.01 kr behind).
function distributeRemaining() {
  var sumInput = document.getElementById('remaining_sum');
  var sum = parseFloat(sumInput.value);
  if (isNaN(sum)) return;
  var empties = Array.prototype.filter.call(
    document.querySelectorAll('#cart-body input[name="price"]'),
    function (el) { return el.value.trim() === ''; }
  );
  if (empties.length === 0) return;
  var shares = splitEvenly(sum, empties.length);
  empties.forEach(function (el, i) { el.value = shares[i]; });
  sumInput.value = '';
  updateCartAutoTotal(sumInput.form);
}

// --- Card picker (partials/card_picker.html) ---------------------------
// One picker per page, one explicit target. Replaces the old two-table,
// two-submit-path arrangement and its "nothing happens when no cart is
// open" failure.

function pickerCheckboxes() {
  return Array.prototype.slice.call(document.querySelectorAll('.card-pick-checkbox'));
}

function toggleAllCardCheckboxes(headerCheckbox) {
  pickerCheckboxes().forEach(function (el) { el.checked = headerCheckbox.checked; });
  updatePickerSelection();
}

function clearPickerSelection() {
  pickerCheckboxes().forEach(function (el) { el.checked = false; });
  var all = document.getElementById('picker-select-all');
  if (all) all.checked = false;
  updatePickerSelection();
}

// Shows/hides the sticky bar and keeps its count honest. The bar is the
// only submit path, so an empty selection simply can't be submitted --
// the old table let you submit nothing and complained afterwards.
function updatePickerSelection() {
  var checked = pickerCheckboxes().filter(function (el) { return el.checked; });
  var bar = document.getElementById('picker-bar');
  if (!bar) return;
  bar.hidden = checked.length === 0;
  document.getElementById('picker-bar-count').textContent =
    checked.length + ' card' + (checked.length === 1 ? '' : 's') + ' selected';
  savePickerSelection(checked);
}

// A sort or filter click is a full-page navigation, which would silently
// drop a half-built selection -- the same failure /sales solved with
// sessionStorage (see static/sale-list.js). Same approach here: remember
// the checked ids for this tab and re-apply them on load.
var PICKER_STORAGE_KEY = 'tcg-picker-selection';

function savePickerSelection(checked) {
  try {
    var ids = checked.map(function (el) { return el.value; });
    if (ids.length) sessionStorage.setItem(PICKER_STORAGE_KEY, JSON.stringify(ids));
    else sessionStorage.removeItem(PICKER_STORAGE_KEY);
  } catch (e) { /* private mode / storage disabled -- selection just won't persist */ }
}

function restorePickerSelection() {
  var ids;
  try {
    ids = JSON.parse(sessionStorage.getItem(PICKER_STORAGE_KEY) || '[]');
  } catch (e) { return; }
  if (!ids || !ids.length) return;
  var wanted = {};
  ids.forEach(function (id) { wanted[id] = true; });
  pickerCheckboxes().forEach(function (el) { if (wanted[el.value]) el.checked = true; });
  updatePickerSelection();
}

// An order's "+ Add cards to this order" button: point the single picker at
// that order and bring it into view, instead of rendering another copy of
// the table inside the order.
function pickCardsFor(purchaseId) {
  var target = document.getElementById('picker-target');
  if (target) target.value = String(purchaseId);
  var picker = document.getElementById('card-picker');
  if (picker) picker.scrollIntoView({ behavior: 'smooth', block: 'start' });
  updatePickerSelection();
}

// Two destinations, because a brand-new order has no server-side identity
// yet: an existing order is a plain POST (this table can run to hundreds of
// rows), while "New order" means filling the cart's #cart-body instead.
function submitPicker(form) {
  var checked = pickerCheckboxes().filter(function (el) { return el.checked; });
  if (checked.length === 0) return false;
  var target = document.getElementById('picker-target').value;

  if (target !== 'new') {
    document.getElementById('picker-purchase-id').value = target;
    try { sessionStorage.removeItem(PICKER_STORAGE_KEY); } catch (e) {}
    leavingDeliberately = true;
    return true;
  }

  // "New order": append into the open cart, opening one first if needed.
  // htmx.ajax is async, so the rows must be appended in the swap callback
  // -- appending on the next line would no-op for every card, which is the
  // exact "target doesn't exist yet" bug this page already had once.
  var ids = checked.map(function (el) { return parseInt(el.value, 10); });
  var cartBody = document.getElementById('cart-body');
  if (cartBody) {
    addCardsToCart(ids);
  } else {
    htmx.ajax('GET', '/transactions/purchase/start', { target: '#purchase-cart-container', swap: 'innerHTML' })
      .then(function () { addCardsToCart(ids); });
  }
  return false;
}

// Strictly one request at a time. Firing all of them at once loses rows:
// they're concurrent htmx requests appending into the same #cart-body, and
// htmx drops the ones that collide (selecting 3 cards reliably landed 2).
function addCardsToCart(ids) {
  var chain = Promise.resolve();
  ids.forEach(function (id) {
    chain = chain.then(function () { return addCardToCart(id); });
  });
  return chain.then(function () {
    clearPickerSelection();
    var container = document.getElementById('purchase-cart-container');
    if (container) container.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

restorePickerSelection();
updatePickerSelection();
