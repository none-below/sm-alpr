// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 zero-below
//
// Backlog dashboard. Fetches data/dashboard.json (built by
// scripts/build_dashboard.py) and renders crawl + article backlog. Staleness
// is recomputed against the viewer's clock on load and on a slow interval, so
// "days idle" / "due" counts advance between site rebuilds.

var DAY = 86400000;
var DATA = null;

// Whole days since an ISO date (YYYY-MM-DD), or null. Date.parse on a bare
// date is UTC midnight; good enough for day-granularity staleness.
function daysSince(iso) {
  if (!iso) return null;
  var t = Date.parse(iso);
  if (isNaN(t)) return null;
  return Math.floor((Date.now() - t) / DAY);
}

function relAge(ms) {
  var s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 90) return s + 's ago';
  var m = Math.round(s / 60);
  if (m < 90) return m + 'm ago';
  var h = Math.round(m / 60);
  if (h < 36) return h + 'h ago';
  return Math.round(h / 24) + 'd ago';
}

// Classify a portal by the same timer the crawler uses: due once the latest
// *attempt* is >= refresh_max_age_days old, overdue at >= stale_days.
function classify(agency, cfg) {
  var age = daysSince(agency.last_attempt);
  if (age === null || age >= cfg.stale_days) return 'overdue';
  if (age >= cfg.refresh_max_age_days) return 'due';
  return 'fresh';
}

function renderBuilt() {
  var el = document.getElementById('built');
  if (!DATA) return;
  var built = Date.parse(DATA.generated_at);
  el.textContent = 'Data built ' + relAge(built) + ' (' + formatDate(DATA.generated_at) +
    ') · staleness recomputed live in your browser';
}

function statCard(num, label, sub, cls) {
  var d = document.createElement('div');
  d.className = 'stat' + (cls ? ' ' + cls : '');
  var n = document.createElement('div'); n.className = 'num'; n.textContent = num; d.appendChild(n);
  var l = document.createElement('div'); l.className = 'label'; l.textContent = label; d.appendChild(l);
  if (sub) { var s = document.createElement('div'); s.className = 'sub'; s.textContent = sub; d.appendChild(s); }
  return d;
}

function renderStats() {
  var cfg = DATA.crawl;
  var agencies = cfg.agencies;
  var due = 0, overdue = 0, maxIdle = 0, stalest = null;
  agencies.forEach(function (a) {
    var c = classify(a, cfg);
    if (c === 'due') due++;
    if (c === 'overdue') overdue++;
    var age = daysSince(a.last_attempt);
    if (age !== null && age > maxIdle) { maxIdle = age; stalest = a; }
  });

  var art = DATA.articles;
  var grid = document.getElementById('stats');
  grid.innerHTML = '';
  grid.appendChild(statCard(due + overdue, 'Portals due', 'of ' + cfg.total_agencies + ' tracked',
    (due + overdue) ? 'warn' : 'ok'));
  grid.appendChild(statCard(overdue, 'Overdue', '≥ ' + cfg.stale_days + ' days idle',
    overdue ? 'alert' : 'ok'));
  grid.appendChild(statCard(stalest ? maxIdle + 'd' : '—', 'Stalest portal',
    stalest ? stalest.name : '', stalest && maxIdle >= cfg.stale_days ? 'alert' : ''));
  var stuck = (cfg.quarantined || []).length;
  grid.appendChild(statCard(stuck, 'Quarantined', 'skipped until retry', stuck ? 'alert' : 'ok'));
  grid.appendChild(statCard(art.needs_review, 'Articles to review', 'curation flagged',
    art.needs_review ? 'warn' : 'ok'));
  grid.appendChild(statCard(art.failed_urls, 'Failed article URLs', 'fetch gave up',
    art.failed_urls ? 'warn' : 'ok'));
}

function renderHealth() {
  var cfg = DATA.crawl;
  var t = cfg.throughput || {};
  var health = document.getElementById('health');
  health.innerHTML = '';
  health.appendChild(statCard(t.mean_7d != null ? t.mean_7d : '—', 'Captures / day', '7-day average'));
  var cycle = t.effective_cycle_days;
  health.appendChild(statCard(cycle != null ? cycle + 'd' : '—', 'Effective cycle',
    'to refresh all ' + cfg.total_agencies,
    cycle != null && cycle > cfg.stale_days ? 'warn' : ''));
  health.appendChild(statCard(cfg.total_captures, 'Total snapshots', 'all-time'));

  // Trend sparkline: bar height ∝ that day's new-snapshot count.
  var series = t.per_day || [];
  var max = series.reduce(function (m, x) { return Math.max(m, x.captures); }, 0) || 1;
  var bars = series.map(function (x, i) {
    var pct = Math.max(Math.round((x.captures / max) * 100), 3);
    var today = i === series.length - 1;
    return '<div class="bar' + (today ? ' today' : '') + '" style="height:' + pct + '%" ' +
      'title="' + escapeHtml(x.date) + ': ' + x.captures + ' captures"></div>';
  }).join('');
  var wrap = document.getElementById('sparkwrap');
  if (!series.length) { wrap.innerHTML = ''; return; }
  wrap.innerHTML =
    '<div class="caption">New snapshots per day — last ' + series.length + ' days (a portal only writes one when its content changed)</div>' +
    '<div class="spark">' + bars + '</div>' +
    '<div class="axis"><span>' + escapeHtml(formatDate(series[0].date)) + '</span>' +
    '<span>' + escapeHtml(formatDate(series[series.length - 1].date)) + ' (today)</span></div>';
}

function renderQuarantine() {
  var q = DATA.crawl.quarantined || [];
  var box = document.getElementById('quarantined');
  if (!q.length) {
    box.innerHTML = '<div class="empty">None — every previously-captured agency is still in rotation.</div>';
    return;
  }
  var html = '<table><thead><tr>' +
    '<th>Agency</th><th>Reason</th><th>Quarantined</th><th>Last capture</th><th class="num">Days idle</th>' +
    '</tr></thead><tbody>';
  q.forEach(function (a) {
    var age = daysSince(a.last_capture);
    html += '<tr>' +
      '<td>' + escapeHtml(a.name) + ' <span class="badge stuck">stuck</span></td>' +
      '<td>' + escapeHtml(a.reason || '—') + '</td>' +
      '<td>' + (a.since ? formatDate(a.since) : '—') + '</td>' +
      '<td>' + (a.last_capture ? formatDate(a.last_capture) : '—') + '</td>' +
      '<td class="num">' + (age === null ? '—' : age) + '</td>' +
      '</tr>';
  });
  html += '</tbody></table>' +
    '<div class="note">Skipped on every run until a manual <code>crawl --retry-failed</code> ' +
    '(or an explicit <code>--slugs</code> crawl). A 404 / not-a-portal reason usually means the ' +
    'portal moved or was taken down.</div>';
  box.innerHTML = html;
}

function renderCrawl() {
  var cfg = DATA.crawl;
  var q = document.getElementById('filter').value.trim().toLowerCase();
  var dueOnly = document.getElementById('dueOnly').checked;

  var rows = cfg.agencies.filter(function (a) {
    if (q) {
      var hay = (a.name + ' ' + (a.state || '') + ' ' + a.slug).toLowerCase();
      if (hay.indexOf(q) === -1) return false;
    }
    if (dueOnly && classify(a, cfg) === 'fresh') return false;
    return true;
  });

  document.getElementById('crawlCount').textContent =
    rows.length + ' shown · ' + cfg.total_agencies + ' tracked · ' + cfg.total_captures + ' captures';

  var box = document.getElementById('crawl');
  if (!rows.length) {
    box.innerHTML = '<div class="empty">Nothing matches — everything in view is fresh.</div>';
    return;
  }

  var html = '<table><thead><tr>' +
    '<th>Agency</th><th>State</th><th>Last capture</th>' +
    '<th class="num">Days idle</th><th>Status</th>' +
    '</tr></thead><tbody>';
  rows.forEach(function (a) {
    var cls = classify(a, cfg);
    var age = daysSince(a.last_attempt);
    // Last attempt newer than last successful capture → recent crawls aren't
    // parsing into a snapshot.
    var failing = a.last_capture && a.last_attempt && a.last_capture < a.last_attempt;
    html += '<tr>' +
      '<td>' + escapeHtml(a.name) +
        (failing ? '<span class="flag" title="Recent crawl attempts did not parse into a snapshot">⚠ attempts not parsing</span>' : '') +
      '</td>' +
      '<td>' + escapeHtml(a.state || '—') + '</td>' +
      '<td>' + (a.last_capture ? formatDate(a.last_capture) : '—') + '</td>' +
      '<td class="num">' + (age === null ? '—' : age) + '</td>' +
      '<td><span class="badge ' + cls + '">' + cls + '</span></td>' +
      '</tr>';
  });
  html += '</tbody></table>';
  box.innerHTML = html;
}

function renderArticles() {
  var art = DATA.articles;
  var box = document.getElementById('articles');
  box.innerHTML = '';
  box.appendChild(statCard(art.total, 'Total fetched', 'in registry'));
  box.appendChild(statCard(art.enriched, 'Enriched', 'full curation', 'ok'));
  box.appendChild(statCard(art.mechanical, 'Mechanical', 'no AI summary'));
  box.appendChild(statCard(art.needs_review, 'Needs review', 'curation error', art.needs_review ? 'warn' : 'ok'));
  box.appendChild(statCard(art.failed_urls, 'Failed URLs', 'fetch gave up', art.failed_urls ? 'warn' : 'ok'));
  var last = statCard(art.last_crawl_run ? relAge(Date.parse(art.last_crawl_run)) : '—',
    'Last discovery', art.last_crawl_run ? formatDate(art.last_crawl_run) : '');
  box.appendChild(last);
}

function renderAll() {
  if (!DATA) return;
  renderBuilt();
  renderStats();
  renderHealth();
  renderQuarantine();
  renderCrawl();
  renderArticles();
}

fetch('data/dashboard.json')
  .then(function (r) { return r.json(); })
  .then(function (data) {
    DATA = data;
    document.getElementById('filter').addEventListener('input', renderCrawl);
    document.getElementById('dueOnly').addEventListener('change', renderCrawl);
    renderAll();
    // Keep "built ago" / staleness honest for a long-open tab.
    setInterval(renderAll, 60000);
  })
  .catch(function () {
    document.getElementById('stats').innerHTML =
      '<div class="empty">Failed to load dashboard data.</div>';
  });
