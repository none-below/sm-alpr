// ALPR Contract Status Map — renders data/contract_map_data.json.

// Soft preview gate. NOT real authentication — the password is in the source
// and this is shipped as a static page. Purpose: keep random visitors who
// stumble across the URL from seeing preliminary, not-yet-fact-checked data,
// and signal to reviewers that the page isn't for public citation yet.
// Change the password here when you want to rotate access.
const PREVIEW_PASSWORD = 'preview';
const UNLOCK_KEY = 'contracts-preview-unlocked';

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function safeUrl(u) {
  if (typeof u !== 'string') return '';
  return /^https?:\/\//.test(u) ? u : '';
}

const STATUS_LABEL = {
  canceled: 'Canceled',
  paused: 'Paused',
  reviewing: 'Reviewing',
  considering: 'Considering',
  reinstated: 'Reinstated',
  signed: 'Signed',
};

const STATUS_COLOR = {
  canceled: '#dc2626',
  paused: '#ea580c',
  reviewing: '#f59e0b',
  considering: '#fbbf24',
  reinstated: '#2563eb',
  signed: '#6b7280',
};

function markerIcon(status) {
  const color = STATUS_COLOR[status] || '#6b7280';
  if (status === 'canceled') {
    // Red X
    const html = `<div style="font-size:22px;font-weight:900;color:${color};
      text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff,0 0 3px #fff;
      line-height:1;user-select:none;">&#x2715;</div>`;
    return L.divIcon({ className: 'contract-icon', html, iconSize: [22, 22], iconAnchor: [11, 11] });
  }
  // Filled circle for other statuses
  const html = `<div style="width:16px;height:16px;border-radius:50%;
    background:${color};border:2px solid #fff;
    box-shadow:0 0 0 1px rgba(0,0,0,0.3);"></div>`;
  return L.divIcon({ className: 'contract-icon', html, iconSize: [20, 20], iconAnchor: [10, 10] });
}

function formatDate(raw) {
  if (!raw) return 'undated';
  return raw;
}

function renderStatusChip(status) {
  const cls = STATUS_LABEL[status] ? status : 'signed';
  return `<span class="status-chip ${cls}">${escapeHtml(STATUS_LABEL[status] || status || 'unknown')}</span>`;
}

function renderFlockSnapshot(payload) {
  const snap = payload.flock_snapshot;
  if (!snap) return '';
  const portal = safeUrl(payload.flock_portal_url);
  const prior = payload.flock_snapshot_prior;
  let delta = '';
  if (prior && typeof snap.camera_count === 'number' && typeof prior.camera_count === 'number') {
    const diff = snap.camera_count - prior.camera_count;
    if (diff < 0) {
      delta = `<span class="delta-down">&#x2193; ${Math.abs(diff)} cameras since ${escapeHtml(prior.as_of || '')}</span>`;
    } else if (diff > 0) {
      delta = `<span class="delta-same">&#x2191; ${diff} cameras since ${escapeHtml(prior.as_of || '')}</span>`;
    } else {
      delta = `<span class="delta-same">unchanged since ${escapeHtml(prior.as_of || '')}</span>`;
    }
  }
  const stats = [];
  if (typeof snap.camera_count === 'number') stats.push(`<span class="stat"><b>${snap.camera_count}</b> cameras</span>`);
  if (typeof snap.vehicles_detected_30d === 'number') stats.push(`<span class="stat"><b>${snap.vehicles_detected_30d.toLocaleString()}</b> vehicles / 30d</span>`);
  if (typeof snap.hotlist_hits_30d === 'number') stats.push(`<span class="stat"><b>${snap.hotlist_hits_30d.toLocaleString()}</b> hotlist hits / 30d</span>`);
  const portalLink = portal
    ? `<a class="portal-link" href="${escapeHtml(portal)}" target="_blank" rel="noopener">Live Flock portal &rarr;</a>`
    : '';
  const asOf = snap.as_of ? ` <small style="color:#6b7280">(as of ${escapeHtml(snap.as_of)})</small>` : '';
  return `
    <div class="flock-snapshot">
      <div style="margin-bottom:6px;font-weight:600;">Flock transparency${asOf}</div>
      ${stats.join(' ')}
      ${delta ? `<div style="margin-top:6px;font-size:12px">${delta}</div>` : ''}
      ${portalLink}
    </div>`;
}

let REASONS_META = {};

function renderReasonChip(code) {
  const meta = REASONS_META[code];
  if (!meta) return '';
  return `<span class="reason-chip" style="background:${escapeHtml(meta.color)}" title="${escapeHtml(meta.description || '')}">
    <span class="reason-icon">${escapeHtml(meta.icon || '?')}</span>${escapeHtml(meta.label || code)}
  </span>`;
}

function renderEvent(ev, articlesById) {
  const type = ev.type;
  const date = formatDate(ev.date);
  const vendor = ev.vendor ? ` &middot; ${escapeHtml(ev.vendor)}` : '';
  const notes = ev.notes ? `<div class="event-notes">${escapeHtml(ev.notes)}</div>` : '';
  const cameras = (typeof ev.cameras_affected === 'number')
    ? `<div class="event-cameras">${ev.cameras_affected} cameras</div>`
    : '';
  const reasons = Array.isArray(ev.reasons) ? ev.reasons : [];
  const reasonsHtml = reasons.length
    ? `<div class="event-reasons">${reasons.map(renderReasonChip).join('')}</div>`
    : '';
  const articleIds = Array.isArray(ev.article_ids) ? ev.article_ids : [];
  const links = articleIds
    .map(id => articlesById[id])
    .filter(Boolean)
    .map(a => {
      const url = safeUrl(a.url);
      if (!url) return '';
      return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(a.outlet || 'source')}</a>`;
    })
    .filter(Boolean)
    .join(' &middot; ');
  const linksHtml = links ? `<div class="event-articles">${links}</div>` : '';
  return `
    <li>
      <div class="event-dot ${type}"></div>
      <div class="event-body">
        <div class="event-head">${escapeHtml(STATUS_LABEL[type] || type)}<span class="event-date">${escapeHtml(date)}</span><span class="event-vendor">${vendor}</span></div>
        ${notes}
        ${cameras}
        ${reasonsHtml}
        ${linksHtml}
      </div>
    </li>`;
}

function renderReasonLegend(reasonsMeta, events) {
  // Only show reasons actually used in the dataset
  const used = new Set();
  for (const e of events) {
    for (const r of e.reasons || []) used.add(r);
  }
  const legend = document.getElementById('reason-legend');
  const body = document.getElementById('reason-legend-body');
  if (!used.size) {
    legend.style.display = 'none';
    return;
  }
  body.innerHTML = Array.from(used)
    .filter(code => reasonsMeta[code])
    .map(renderReasonChip)
    .join('<br>');
  legend.style.display = 'block';
}

function renderArticleList(articles) {
  if (!articles.length) return '';
  const items = articles.map(a => {
    const url = safeUrl(a.url);
    const title = escapeHtml(a.title || '(untitled)');
    const outlet = escapeHtml(a.outlet || '');
    const date = escapeHtml(a.published_date || '');
    const link = url
      ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${title}</a>`
      : title;
    const quote = a.quote
      ? `<div class="pull-quote">${escapeHtml(a.quote)}</div>`
      : '';
    return `<li>${link}${quote}<div class="outlet">${outlet}${date ? ' &middot; ' + date : ''}</div></li>`;
  }).join('');
  return `
    <div class="section-title">Articles (${articles.length})</div>
    <ul class="articles">${items}</ul>`;
}

function renderPanel(payload) {
  if (!payload) return '';
  const articlesById = {};
  for (const a of payload.articles || []) articlesById[a.id] = a;

  const loc = [payload.city, payload.state].filter(Boolean).join(', ');
  const vendorRows = Object.entries(payload.status_by_vendor || {}).map(([v, info]) => {
    return `<div class="vendor-row">
      <span class="vendor-name">${escapeHtml(v)}</span>
      ${renderStatusChip(info.status)}
      <span style="color:#6b7280;font-size:12px">${escapeHtml(formatDate(info.last_event_date))}</span>
    </div>`;
  }).join('');

  const timeline = (payload.events || []).map(ev => renderEvent(ev, articlesById)).join('');
  const flock = renderFlockSnapshot(payload);
  const notes = payload.notes ? `<div style="font-size:13px;margin-top:8px;color:#374151">${escapeHtml(payload.notes)}</div>` : '';

  return `
    <h2>${escapeHtml(payload.name)}</h2>
    <div class="locality">${escapeHtml(loc)}</div>
    ${vendorRows ? `<div class="section-title">Current status by vendor</div>${vendorRows}` : ''}
    ${flock ? `<div class="section-title">Live data</div>${flock}` : ''}
    ${timeline ? `<div class="section-title">Timeline (${payload.events.length})</div><ul class="timeline">${timeline}</ul>` : ''}
    ${renderArticleList(payload.articles || [])}
    ${notes}
  `;
}

function init(data) {
  REASONS_META = (data.meta && data.meta.reasons) || {};

  const map = L.map('map').setView([39.5, -98.35], 4);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png', {
    attribution: '&copy; OpenStreetMap &copy; CARTO',
    subdomains: 'abcd',
    maxZoom: 19,
  }).addTo(map);

  const markersById = {};
  const markerList = [];

  const markers = Array.isArray(data.markers) ? data.markers : [];
  for (const m of markers) {
    if (typeof m.lat !== 'number' || typeof m.lng !== 'number') continue;
    const marker = L.marker([m.lat, m.lng], { icon: markerIcon(m.status_overall) });
    marker.on('click', () => selectAgency(m.id));
    marker.addTo(map);
    markersById[m.id] = marker;
    markerList.push(m);
  }

  if (!markers.length) {
    const div = document.createElement('div');
    div.className = 'empty-state';
    div.textContent = 'No contract-status entries yet. As articles are curated into data/contracts/, agencies will appear here.';
    document.body.appendChild(div);
  }

  // Build the reasons legend from all events in the dataset
  const allEvents = [];
  for (const a of Object.values(data.agencies || {})) {
    for (const ev of a.events || []) allEvents.push(ev);
  }
  renderReasonLegend(REASONS_META, allEvents);

  const panel = document.getElementById('info');
  const body = document.getElementById('info-body');
  document.getElementById('info-close').addEventListener('click', () => {
    panel.classList.remove('open');
  });

  function selectAgency(id) {
    const payload = (data.agencies || {})[id];
    if (!payload) return;
    body.innerHTML = renderPanel(payload);
    panel.classList.add('open');
    const m = markersById[id];
    if (m) map.panTo(m.getLatLng());
  }

  const searchBox = document.getElementById('search-box');
  const results = document.getElementById('search-results');
  searchBox.addEventListener('input', () => {
    const q = searchBox.value.trim().toLowerCase();
    if (q.length < 2) {
      results.style.display = 'none';
      results.innerHTML = '';
      return;
    }
    const matches = markerList.filter(m => {
      const hay = `${m.name} ${m.full_name || ''} ${m.city || ''} ${m.state || ''}`.toLowerCase();
      return hay.includes(q);
    }).slice(0, 20);
    if (!matches.length) {
      results.style.display = 'block';
      results.innerHTML = '<div class="result" style="color:#9ca3af;cursor:default">No matches</div>';
      return;
    }
    results.innerHTML = matches.map(m => {
      const loc = [m.city, m.state].filter(Boolean).join(', ');
      return `<div class="result" data-id="${escapeHtml(m.id)}">
        <strong>${escapeHtml(m.full_name || m.name)}</strong>
        ${loc ? `<small> &middot; ${escapeHtml(loc)}</small>` : ''}
      </div>`;
    }).join('');
    results.style.display = 'block';
    results.querySelectorAll('.result[data-id]').forEach(el => {
      el.addEventListener('click', () => {
        const id = el.getAttribute('data-id');
        selectAgency(id);
        results.style.display = 'none';
        searchBox.value = '';
      });
    });
  });
  searchBox.addEventListener('blur', () => {
    setTimeout(() => { results.style.display = 'none'; }, 200);
  });
}

function bootstrap() {
  fetch('data/contract_map_data.json')
    .then(r => r.json())
    .then(init)
    .catch(err => {
      console.error('Failed to load contract_map_data.json', err);
      const div = document.createElement('div');
      div.className = 'empty-state';
      div.textContent = 'Failed to load contract data.';
      document.body.appendChild(div);
    });
}

function unlockAndBoot() {
  sessionStorage.setItem(UNLOCK_KEY, '1');
  const overlay = document.getElementById('gate-overlay');
  if (overlay) overlay.style.display = 'none';
  bootstrap();
}

function wireGate() {
  const submit = document.getElementById('gate-submit');
  const input = document.getElementById('gate-input');
  const err = document.getElementById('gate-error');
  const tryUnlock = () => {
    const val = (input.value || '').trim().toLowerCase();
    if (val === PREVIEW_PASSWORD) {
      err.textContent = '';
      unlockAndBoot();
    } else {
      err.textContent = 'Incorrect phrase. Try again.';
      input.select();
    }
  };
  submit.addEventListener('click', tryUnlock);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') tryUnlock();
  });
}

if (sessionStorage.getItem(UNLOCK_KEY) === '1') {
  const overlay = document.getElementById('gate-overlay');
  if (overlay) overlay.style.display = 'none';
  bootstrap();
} else {
  wireGate();
}
