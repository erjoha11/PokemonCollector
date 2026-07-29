/**
 * PokemonCollector — app.js
 * Manages the Pokemon Trading Card collection stored in localStorage.
 * On first load, seeds the collection from data/collection.json.
 */

'use strict';

const STORAGE_KEY = 'pokemonCollector_v1';

// ─── State ───────────────────────────────────────────────────────────────────

let collection = [];   // Array of card objects
let editingId   = null; // id of card being edited, or null for new

// ─── Persistence ─────────────────────────────────────────────────────────────

function saveCollection() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(collection));
}

function loadCollection() {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored) {
    try {
      collection = JSON.parse(stored);
      return;
    } catch (_) {
      // fall through to seed data
    }
  }
  // First visit — load sample data from data/collection.json
  fetch('data/collection.json')
    .then(r => r.json())
    .then(data => {
      collection = data;
      saveCollection();
      populateFilters();
      renderCards();
      renderStats();
    })
    .catch(() => {
      // Fetch failed (e.g. opened directly as a file:// without a server).
      // Start with an empty collection.
      collection = [];
      renderCards();
      renderStats();
    });
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function generateId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

function escapeHtml(str) {
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return String(str).replace(/[&<>"']/g, ch => map[ch]);
}

function typeClass(type) {
  return 'type-' + (type || 'colorless').toLowerCase().replace(/\s+/g, '-');
}

function rarityClass(rarity) {
  const r = (rarity || '').toLowerCase().replace(/\s+/g, '-');
  if (r.includes('secret')) return 'rarity-secret';
  if (r.includes('ultra'))  return 'rarity-ultra';
  if (r.includes('holo'))   return 'rarity-holo';
  return '';
}

function formatValue(v) {
  const n = parseFloat(v);
  return isNaN(n) ? '—' : '$' + n.toFixed(2);
}

// ─── Filters ─────────────────────────────────────────────────────────────────

function getFilterValues() {
  return {
    search: document.getElementById('search-input').value.trim().toLowerCase(),
    set:    document.getElementById('filter-set').value,
    type:   document.getElementById('filter-type').value,
    rarity: document.getElementById('filter-rarity').value,
  };
}

function filteredCards() {
  const { search, set, type, rarity } = getFilterValues();
  return collection.filter(card => {
    if (search && !card.name.toLowerCase().includes(search)) return false;
    if (set    && card.set    !== set)    return false;
    if (type   && card.type   !== type)   return false;
    if (rarity && card.rarity !== rarity) return false;
    return true;
  });
}

function populateFilters() {
  const sets     = [...new Set(collection.map(c => c.set).filter(Boolean))].sort();
  const types    = [...new Set(collection.map(c => c.type).filter(Boolean))].sort();
  const rarities = [...new Set(collection.map(c => c.rarity).filter(Boolean))].sort();

  const setEl    = document.getElementById('filter-set');
  const typeEl   = document.getElementById('filter-type');
  const rarityEl = document.getElementById('filter-rarity');

  const currentSet    = setEl.value;
  const currentType   = typeEl.value;
  const currentRarity = rarityEl.value;

  const rebuildSelect = (el, items, current) => {
    const first = el.options[0];
    el.innerHTML = '';
    el.appendChild(first);
    items.forEach(item => {
      const opt = document.createElement('option');
      opt.value = item;
      opt.textContent = item;
      if (item === current) opt.selected = true;
      el.appendChild(opt);
    });
  };

  rebuildSelect(setEl,    sets,     currentSet);
  rebuildSelect(typeEl,   types,    currentType);
  rebuildSelect(rarityEl, rarities, currentRarity);
}

// ─── Stats ───────────────────────────────────────────────────────────────────

function renderStats() {
  const totalCards  = collection.reduce((sum, c) => sum + (parseInt(c.quantity, 10) || 0), 0);
  const uniqueCards = collection.length;
  const totalValue  = collection.reduce((sum, c) => {
    const qty = parseInt(c.quantity, 10) || 0;
    const val = parseFloat(c.value) || 0;
    return sum + qty * val;
  }, 0);
  const sets = new Set(collection.map(c => c.set).filter(Boolean)).size;

  document.getElementById('stat-total-cards').textContent  = totalCards;
  document.getElementById('stat-unique-cards').textContent = uniqueCards;
  document.getElementById('stat-total-value').textContent  = '$' + totalValue.toFixed(2);
  document.getElementById('stat-sets').textContent         = sets;
}

// ─── Render Cards ─────────────────────────────────────────────────────────────

function renderCards() {
  const grid   = document.getElementById('card-grid');
  const empty  = document.getElementById('empty-state');
  const cards  = filteredCards();

  grid.innerHTML = '';

  if (cards.length === 0) {
    empty.hidden = false;
    return;
  }

  empty.hidden = true;

  cards.forEach(card => {
    const article = document.createElement('article');
    article.className = 'card';
    article.dataset.id = card.id;

    article.innerHTML = `
      <div class="card-header">
        <div>
          <div class="card-name">${escapeHtml(card.name)}</div>
          ${card.number ? `<div class="card-number">${escapeHtml(card.number)}</div>` : ''}
        </div>
        <span class="card-type-badge ${typeClass(card.type)}">${escapeHtml(card.type || '—')}</span>
      </div>
      <div class="card-body">
        <dl class="card-meta">
          <dt>Set</dt>      <dd>${escapeHtml(card.set || '—')}</dd>
          <dt>Rarity</dt>   <dd class="card-rarity ${rarityClass(card.rarity)}">${escapeHtml(card.rarity || '—')}</dd>
          <dt>Condition</dt><dd>${escapeHtml(card.condition || '—')}</dd>
        </dl>
      </div>
      <div class="card-footer">
        <div>
          <span class="card-value">${formatValue(card.value)}</span>
          <span class="card-qty"> × ${parseInt(card.quantity, 10) || 1}</span>
        </div>
        <div style="display:flex;gap:.4rem">
          <button class="btn btn-secondary" style="height:auto;padding:.3rem .65rem;font-size:.8rem"
                  data-action="edit" data-id="${escapeHtml(card.id)}" aria-label="Edit ${escapeHtml(card.name)}">
            Edit
          </button>
          <button class="btn btn-danger"
                  data-action="delete" data-id="${escapeHtml(card.id)}" aria-label="Delete ${escapeHtml(card.name)}">
            ✕
          </button>
        </div>
      </div>
    `;

    grid.appendChild(article);
  });
}

// ─── Modal ───────────────────────────────────────────────────────────────────

function openModal(card = null) {
  const modal  = document.getElementById('modal');
  const title  = document.getElementById('modal-title');
  const form   = document.getElementById('card-form');
  const errEl  = document.getElementById('form-error');

  editingId = card ? card.id : null;
  title.textContent = card ? 'Edit Card' : 'Add Card';

  form.reset();
  errEl.hidden = true;
  form.querySelectorAll('.invalid').forEach(el => el.classList.remove('invalid'));

  if (card) {
    form.elements['name'].value      = card.name      || '';
    form.elements['set'].value       = card.set       || '';
    form.elements['number'].value    = card.number    || '';
    form.elements['rarity'].value    = card.rarity    || '';
    form.elements['type'].value      = card.type      || '';
    form.elements['condition'].value = card.condition || '';
    form.elements['quantity'].value  = card.quantity  ?? 1;
    form.elements['value'].value     = card.value     ?? '';
  }

  modal.hidden = false;
  form.elements['name'].focus();
}

function closeModal() {
  document.getElementById('modal').hidden = true;
  editingId = null;
}

// ─── Form Validation & Submit ─────────────────────────────────────────────────

function validateForm(form) {
  const required = ['name', 'set', 'rarity', 'type', 'condition', 'quantity'];
  let valid = true;

  required.forEach(name => {
    const el = form.elements[name];
    if (!el.value.trim()) {
      el.classList.add('invalid');
      valid = false;
    } else {
      el.classList.remove('invalid');
    }
  });

  return valid;
}

function handleFormSubmit(e) {
  e.preventDefault();

  const form   = e.target;
  const errEl  = document.getElementById('form-error');

  if (!validateForm(form)) {
    errEl.textContent = 'Please fill in all required fields.';
    errEl.hidden = false;
    return;
  }

  errEl.hidden = true;

  const quantity = parseInt(form.elements['quantity'].value, 10) || 1;
  const value    = parseFloat(form.elements['value'].value) || 0;

  if (editingId) {
    const idx = collection.findIndex(c => c.id === editingId);
    if (idx !== -1) {
      collection[idx] = {
        ...collection[idx],
        name:      form.elements['name'].value.trim(),
        set:       form.elements['set'].value.trim(),
        number:    form.elements['number'].value.trim(),
        rarity:    form.elements['rarity'].value,
        type:      form.elements['type'].value,
        condition: form.elements['condition'].value,
        quantity,
        value,
      };
    }
  } else {
    collection.push({
      id:        generateId(),
      name:      form.elements['name'].value.trim(),
      set:       form.elements['set'].value.trim(),
      number:    form.elements['number'].value.trim(),
      rarity:    form.elements['rarity'].value,
      type:      form.elements['type'].value,
      condition: form.elements['condition'].value,
      quantity,
      value,
    });
  }

  saveCollection();
  populateFilters();
  renderCards();
  renderStats();
  closeModal();
}

// ─── Delete Card ─────────────────────────────────────────────────────────────

function deleteCard(id) {
  const card = collection.find(c => c.id === id);
  if (!card) return;
  if (!confirm(`Remove "${card.name}" from your collection?`)) return;

  collection = collection.filter(c => c.id !== id);
  saveCollection();
  populateFilters();
  renderCards();
  renderStats();
}

// ─── Event Wiring ─────────────────────────────────────────────────────────────

function init() {
  // Add Card button
  document.getElementById('btn-add').addEventListener('click', () => openModal());

  // Cancel button & backdrop close
  document.getElementById('btn-cancel').addEventListener('click', closeModal);
  document.getElementById('modal-backdrop').addEventListener('click', closeModal);

  // Escape key closes modal
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
  });

  // Form submit
  document.getElementById('card-form').addEventListener('submit', handleFormSubmit);

  // Clear invalid state on input
  document.getElementById('card-form').addEventListener('input', e => {
    e.target.classList.remove('invalid');
  });

  // Search & filter
  document.getElementById('search-input').addEventListener('input', renderCards);
  document.getElementById('filter-set').addEventListener('change', renderCards);
  document.getElementById('filter-type').addEventListener('change', renderCards);
  document.getElementById('filter-rarity').addEventListener('change', renderCards);

  // Edit / Delete via event delegation
  document.getElementById('card-grid').addEventListener('click', e => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;

    const { action, id } = btn.dataset;

    if (action === 'delete') {
      deleteCard(id);
    } else if (action === 'edit') {
      const card = collection.find(c => c.id === id);
      if (card) openModal(card);
    }
  });

  // Load data
  loadCollection();
}

// ─── Boot ─────────────────────────────────────────────────────────────────────

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
