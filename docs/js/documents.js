'use strict';

const TYPE_LABELS = {
  pra_attachment: 'PRA attachment',
  correspondence: 'Correspondence',
  reference: 'Reference doc',
  report: 'Report',
  portal_snapshot: 'Portal snapshot',
  cited: 'Cited (external)',
};

const state = {
  index: null,
  search: '',
  agency: '',
  types: new Set(),
};

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  attrs = attrs || {};
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on')) node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

function formatBytes(n) {
  if (!n) return null;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function matchSearch(doc, q) {
  if (!q) return true;
  const hay = [
    doc.filename,
    doc.blurb,
    doc.context_title,
    doc.agency_label,
    doc.cite_label,
    ...(doc.tags || []),
  ].filter(Boolean).join(' ').toLowerCase();
  return hay.includes(q);
}

function filterDocs() {
  return state.index.docs.filter(d => {
    if (state.agency && d.agency_key !== state.agency) return false;
    if (state.types.size && !state.types.has(d.source_type)) return false;
    return matchSearch(d, state.search);
  });
}

function renderRow(doc) {
  const typeLabel = TYPE_LABELS[doc.source_type] || doc.source_type;
  const titleText = doc.filename || doc.blurb;
  const title = doc.url
    ? el('a', { href: doc.url, target: '_blank', rel: 'noopener' }, titleText)
    : document.createTextNode(titleText);

  const meta = [];
  if (doc.agency_label) meta.push(doc.agency_label);
  if (doc.date) meta.push(formatDate(doc.date));
  const size = formatBytes(doc.size_bytes);
  if (size) meta.push(size);
  if (doc.cite_label) meta.push(doc.cite_label);

  const badges = [el('span', { class: `badge type-${doc.source_type}` }, typeLabel)];
  if (doc.source_number) {
    badges.push(el('span', { class: 'badge source-num' }, `source [${doc.source_number}]`));
  }

  const actions = el('div', { class: 'row-actions' });
  if (doc.context_url) {
    actions.appendChild(el('a', { href: doc.context_url }, 'Context'));
  }
  if (doc.url) {
    actions.appendChild(el('a', { href: doc.url, target: '_blank', rel: 'noopener' }, 'View'));
  }

  return el('div', { class: `row-item${doc.featured ? ' featured' : ''}` },
    el('div', { class: 'row-head' },
      el('div', { class: 'row-title-group' },
        el('div', { class: 'row-title' }, title),
        doc.blurb && doc.blurb !== titleText
          ? el('div', { class: 'row-blurb' }, doc.blurb)
          : null,
        el('div', { class: 'row-meta' }, ...badges, ...meta.map(m => el('span', {}, m))),
      ),
      actions,
    ),
  );
}

function render() {
  const rows = document.getElementById('rows');
  rows.textContent = '';
  const filtered = filterDocs();
  document.getElementById('summary').textContent =
    `${filtered.length} of ${state.index.docs.length} documents`;
  if (!filtered.length) {
    rows.appendChild(el('div', { class: 'empty' }, 'No documents match.'));
    return;
  }
  const frag = document.createDocumentFragment();
  for (const doc of filtered) frag.appendChild(renderRow(doc));
  rows.appendChild(frag);
}

function wireControls() {
  const search = document.getElementById('search');
  search.addEventListener('input', (e) => {
    state.search = e.target.value.trim().toLowerCase();
    render();
  });

  const agencySelect = document.getElementById('agency-select');
  agencySelect.appendChild(el('option', { value: '' }, 'All agencies'));
  for (const a of state.index.agencies) {
    agencySelect.appendChild(el('option', { value: a.key }, `${a.label} (${a.count})`));
  }
  agencySelect.addEventListener('change', (e) => {
    state.agency = e.target.value;
    render();
  });

  const typeCounts = {};
  for (const d of state.index.docs) {
    typeCounts[d.source_type] = (typeCounts[d.source_type] || 0) + 1;
  }
  const chipsEl = document.getElementById('type-chips');
  for (const [type, count] of Object.entries(typeCounts).sort((a, b) => b[1] - a[1])) {
    const chip = el('span', { class: 'chip' },
      TYPE_LABELS[type] || type, ' ', el('span', { class: 'count' }, `${count}`));
    chip.addEventListener('click', () => {
      if (state.types.has(type)) { state.types.delete(type); chip.classList.remove('active'); }
      else { state.types.add(type); chip.classList.add('active'); }
      render();
    });
    chipsEl.appendChild(chip);
  }
}

async function init() {
  const res = await fetch('data/document_index.json');
  state.index = await res.json();
  document.getElementById('subtitle').textContent =
    `${state.index.docs.length} documents · index generated ${state.index.generated_at.slice(0, 16).replace('T', ' ')} UTC`;
  wireControls();
  render();
}

init();
