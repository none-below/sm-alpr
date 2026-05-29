// Article viewer: list + tag filter (include/exclude/off cycle).

var STATE = {
  data: null,
  filters: {},          // tag -> 'include' | 'exclude'
  agencyFilter: null,   // agency_id when narrowed by city/agency
  agencyIndex: [],      // [{ agency_id, slug, name, state, count, _lower }]
  map: null,
  markerLayer: null
};


function cycleFilter(tag) {
  var cur = STATE.filters[tag];
  if (!cur) STATE.filters[tag] = 'include';
  else if (cur === 'include') STATE.filters[tag] = 'exclude';
  else delete STATE.filters[tag];
  render();
}

function clearFilters() {
  STATE.filters = {};
  STATE.agencyFilter = null;
  syncUrl();
  render();
}

function setAgencyFilter(agencyId) {
  STATE.agencyFilter = agencyId || null;
  syncUrl();
  render();
}

// Reflect the active agency filter in the URL so the page is shareable
// and the sharing_map / report pages can deep-link in. Use replaceState
// (not pushState) so the browser back button doesn't trap users in a
// chain of intermediate filter states.
function syncUrl() {
  var url = new URL(window.location.href);
  if (STATE.agencyFilter) {
    var entry = (STATE.data && STATE.data.articles_by_agency || {})[STATE.agencyFilter];
    var key = (entry && entry.slug) || STATE.agencyFilter;
    url.searchParams.set('agency', key);
  } else {
    url.searchParams.delete('agency');
  }
  history.replaceState(null, '', url.toString());
}

function resolveAgencyParam(data, param) {
  if (!param) return null;
  var byAgency = data.articles_by_agency || {};
  if (byAgency[param]) return param;
  for (var id in byAgency) {
    if (byAgency[id].slug === param) return id;
  }
  return null;
}

function buildAgencyIndex(data) {
  var byAgency = data.articles_by_agency || {};
  var out = [];
  for (var id in byAgency) {
    var e = byAgency[id];
    // primary_count matches what articles.html actually shows after
    // a filter is applied — mention-only matches are excluded so they
    // don't pad the suggestion's "N articles" badge with stories the
    // filter won't surface.
    var count = e.primary_count || 0;
    if (!count) continue;
    var label = e.name || id;
    out.push({
      agency_id: id,
      slug: e.slug || '',
      name: label,
      state: e.state || '',
      count: count,
      _lower: label.toLowerCase()
    });
  }
  out.sort(function (a, b) {
    if (b.count !== a.count) return b.count - a.count;
    return a._lower < b._lower ? -1 : 1;
  });
  return out;
}

function tagNamespace(tag) {
  var i = tag.indexOf(':');
  return i >= 0 ? tag.substring(0, i) : 'other';
}

// Filter semantics: OR within a namespace (selecting outcome:terminated and
// outcome:rejected widens the result), AND across namespaces. Excludes always
// drop matching articles regardless of grouping.
function articleMatches(article) {
  // Match the agency filter against primary_subject_agencies only,
  // not the broader agencies[] mentions list. The mentions list is
  // noisy — name-checks, list asides, and curator slip-ups (e.g. an
  // article about Bloomington IN tagged with Bloomington IL PD) leak
  // into the filter result and make it feel like the filter is broken.
  // Primary subjects are the curator's judgment of what the piece is
  // substantively about, which is what users expect when they pick a
  // city.
  if (STATE.agencyFilter) {
    var primaries = article.primary_subject_agencies || [];
    var hit = false;
    for (var pi = 0; pi < primaries.length; pi++) {
      if (primaries[pi] && primaries[pi].agency_id === STATE.agencyFilter) {
        hit = true;
        break;
      }
    }
    if (!hit) return false;
  }
  var tags = article.tags || [];
  var tagSet = {};
  for (var i = 0; i < tags.length; i++) tagSet[tags[i]] = true;
  var srcKey = 'source:' + (article.source_domain || '');

  function tagPresent(t) {
    return t.indexOf('source:') === 0 ? (t === srcKey) : !!tagSet[t];
  }

  var includesByNs = {};
  for (var t in STATE.filters) {
    var mode = STATE.filters[t];
    if (mode === 'exclude' && tagPresent(t)) return false;
    if (mode === 'include') {
      var ns = tagNamespace(t);
      if (!includesByNs[ns]) includesByNs[ns] = [];
      includesByNs[ns].push(t);
    }
  }

  for (var ns2 in includesByNs) {
    var group = includesByNs[ns2];
    var anyMatch = false;
    for (var k = 0; k < group.length; k++) {
      if (tagPresent(group[k])) { anyMatch = true; break; }
    }
    if (!anyMatch) return false;
  }
  return true;
}

function renderTagGroups() {
  var data = STATE.data;
  var nsOrder = data.namespace_order || [];
  var byNs = {};
  for (var tag in data.tags) {
    var info = data.tags[tag];
    var ns = info.namespace || 'other';
    if (!byNs[ns]) byNs[ns] = [];
    byNs[ns].push({ tag: tag, info: info });
  }

  var sources = data.sources || [];
  if (sources.length) {
    byNs['source'] = sources.map(function (s) {
      return {
        tag: 'source:' + s.domain,
        info: { count: s.count, description: s.domain }
      };
    });
  }

  // Append any namespaces not in nsOrder.
  for (var ns2 in byNs) {
    if (nsOrder.indexOf(ns2) === -1) nsOrder.push(ns2);
  }

  var html = '';
  for (var i = 0; i < nsOrder.length; i++) {
    var ns = nsOrder[i];
    var entries = byNs[ns];
    if (!entries || entries.length === 0) continue;
    entries.sort(function (a, b) {
      if (b.info.count !== a.info.count) return b.info.count - a.info.count;
      return a.tag < b.tag ? -1 : 1;
    });

    html += '<div class="ns-row">';
    html += '<span class="ns-label">' + escapeHtml(ns) + '</span>';
    for (var j = 0; j < entries.length; j++) {
      var t = entries[j].tag;
      var info = entries[j].info;
      var mode = STATE.filters[t];
      var cls = 'chip' + (mode ? ' ' + mode : '');
      var label = t.indexOf(':') >= 0 ? t.split(':').slice(1).join(':') : t;
      var title = info.description ? info.description : t;
      html += '<span class="' + cls + '" data-tag="' + escapeHtml(t)
        + '" title="' + escapeHtml(title) + '">'
        + escapeHtml(label)
        + '<span class="count">' + info.count + '</span>'
        + '</span>';
    }
    html += '</div>';
  }
  document.getElementById('tag-groups').innerHTML = html;

  var chips = document.querySelectorAll('#tag-groups .chip');
  for (var k = 0; k < chips.length; k++) {
    chips[k].addEventListener('click', function (ev) {
      cycleFilter(ev.currentTarget.getAttribute('data-tag'));
    });
  }
}

function renderAgencyPin() {
  var wrap = document.getElementById('agency-search-input-wrap');
  if (!wrap) return;
  if (!STATE.agencyFilter) {
    // Restore the search input if it was replaced by the pin earlier.
    if (!document.getElementById('agency-search-input')) {
      wrap.innerHTML = '<input type="text" id="agency-search-input" '
        + 'placeholder="Type a city or agency name…" '
        + 'autocomplete="off" spellcheck="false">';
      wireAgencySearchInput();
    }
    return;
  }
  var byAgency = (STATE.data && STATE.data.articles_by_agency) || {};
  var ag = byAgency[STATE.agencyFilter];
  var label = ag ? ag.name : STATE.agencyFilter;
  var state = ag && ag.state ? ag.state : '';
  var count = ag ? (ag.primary_count || 0) : 0;
  wrap.innerHTML = '<div class="agency-pin-inline">'
    + '<div><span class="pin-label">' + escapeHtml(label) + '</span>'
    + (state ? '<span class="pin-meta">' + escapeHtml(state) + '</span>' : '')
    + '<span class="pin-meta">· ' + count + ' article' + (count !== 1 ? 's' : '') + '</span>'
    + '</div>'
    + '<button type="button" id="clear-agency-btn" aria-label="Clear city filter">'
    + '× Remove filter</button>'
    + '</div>';
  var btn = document.getElementById('clear-agency-btn');
  if (btn) btn.addEventListener('click', function () { setAgencyFilter(null); });
}

function renderActiveBar(matchedCount, totalCount) {
  var bar = document.getElementById('active-bar');
  var includes = [], excludes = [];
  for (var t in STATE.filters) {
    if (STATE.filters[t] === 'include') includes.push(t);
    else excludes.push(t);
  }
  var html = '<span><strong>' + matchedCount + '</strong> of '
    + totalCount + ' articles</span>';
  if (includes.length) {
    html += '<span class="dot">·</span>require: ';
    for (var i = 0; i < includes.length; i++) {
      html += '<span class="chip include" style="cursor:default">'
        + escapeHtml(includes[i]) + '</span>';
    }
  }
  if (excludes.length) {
    html += '<span class="dot">·</span>exclude: ';
    for (var j = 0; j < excludes.length; j++) {
      html += '<span class="chip exclude" style="cursor:default">'
        + escapeHtml(excludes[j]) + '</span>';
    }
  }
  if (includes.length || excludes.length) {
    html += '<button class="clear" id="clear-btn">clear filters</button>';
  }
  bar.innerHTML = html;
  var btn = document.getElementById('clear-btn');
  if (btn) btn.addEventListener('click', clearFilters);
}

function renderArticleCard(article) {
  var metaParts = [];
  if (article.source_domain) metaParts.push(escapeHtml(article.source_domain));
  if (article.published_at) metaParts.push(formatDate(article.published_at));
  if (article.byline) metaParts.push('by ' + escapeHtml(article.byline));
  var meta = metaParts.join(' <span class="dot">·</span> ');

  var tagsHtml = '';
  if (article.tags && article.tags.length) {
    for (var i = 0; i < article.tags.length; i++) {
      var t = article.tags[i];
      var mode = STATE.filters[t];
      var cls = 'chip' + (mode ? ' ' + mode : '');
      tagsHtml += '<span class="' + cls + '" data-tag="' + escapeHtml(t)
        + '" title="' + escapeHtml(t) + '">' + escapeHtml(t) + '</span>';
    }
  }

  var articleUrl = safeUrl(article.url);
  var waybackUrl = safeUrl(article.wayback_url);
  var links = [];
  if (articleUrl) {
    links.push('<a href="' + escapeHtml(articleUrl)
      + '" target="_blank" rel="noopener">original</a>');
  }
  if (article.paths && article.paths.pdf) {
    links.push('<a href="../' + escapeHtml(article.paths.pdf)
      + '" target="_blank">pdf</a>');
  }
  if (waybackUrl) {
    links.push('<a href="' + escapeHtml(waybackUrl)
      + '" target="_blank" rel="noopener">archive.org</a>');
  }

  var titleHtml = articleUrl
    ? '<a href="' + escapeHtml(articleUrl)
      + '" target="_blank" rel="noopener">' + escapeHtml(article.title) + '</a>'
    : escapeHtml(article.title);

  return '<div class="article">'
    + '<div class="article-title">' + titleHtml + '</div>'
    + '<div class="article-meta">' + meta + '</div>'
    + (article.summary
        ? '<div class="article-summary">' + escapeHtml(article.summary) + '</div>'
        : '')
    + (tagsHtml ? '<div class="article-tags">' + tagsHtml + '</div>' : '')
    + (links.length
        ? '<div class="article-links">' + links.join('') + '</div>'
        : '')
    + '</div>';
}

function renderArticleList(matched) {
  var articles = STATE.data.articles;
  var listEl = document.getElementById('article-list');
  if (matched.length === 0) {
    listEl.innerHTML = '<div class="empty">No articles match the current filters.</div>';
  } else {
    var html = '';
    for (var k = 0; k < matched.length; k++) {
      html += renderArticleCard(matched[k]);
    }
    listEl.innerHTML = html;
  }

  var chips = listEl.querySelectorAll('.chip');
  for (var m = 0; m < chips.length; m++) {
    chips[m].addEventListener('click', function (ev) {
      cycleFilter(ev.currentTarget.getAttribute('data-tag'));
    });
  }
  renderActiveBar(matched.length, articles.length);
}

// Cluster icon that totals articles across all clustered agencies.
// The plain agency count was misleading — a cluster of 3 cities with
// 2/4/1 articles read as "3" but the bubble actually carries 7 pieces
// of coverage. Format: "ARTICLES / CITIES" when the cluster spans more
// than one city, just "ARTICLES" otherwise.
function articleClusterIcon(cluster) {
  var children = cluster.getAllChildMarkers();
  var totalArticles = 0;
  for (var i = 0; i < children.length; i++) {
    totalArticles += (children[i].options.articleCount || 0);
  }
  var label = children.length > 1
    ? (totalArticles + ' / ' + children.length)
    : String(totalArticles);
  // Scale with article count so heavier clusters read as bigger pins.
  var size = Math.min(54, 22 + Math.sqrt(totalArticles) * 6);
  var r = size / 2;
  var fontSize = label.length > 4 ? 10 : 12;
  var svg = '<svg width="' + size + '" height="' + size
    + '" xmlns="http://www.w3.org/2000/svg">'
    + '<circle cx="' + r + '" cy="' + r + '" r="' + (r - 1)
      + '" fill="#60a5fa" fill-opacity="0.85" stroke="#1e3a8a" stroke-width="1"/>'
    + '<text x="' + r + '" y="' + (r + fontSize / 3)
      + '" text-anchor="middle" font-size="' + fontSize
      + '" font-weight="bold" fill="#0f172a">'
      + label + '</text>'
    + '</svg>';
  return L.divIcon({ html: svg, className: '', iconSize: [size, size] });
}

function initMap() {
  if (STATE.map) return;
  var map = MapCommon.createCartoMap('map', { theme: 'dark' });
  var markerLayer = L.markerClusterGroup(MapCommon.clusterOptions({
    iconCreateFunction: articleClusterIcon
  }));
  MapCommon.attachClusterTooltips(markerLayer, function (cm) {
    var n = cm.options.articleCount || 0;
    return cm.options.agencyName + ' — ' + n + ' article' + (n !== 1 ? 's' : '');
  });
  markerLayer.addTo(map);
  // Vendor-bucket pip lives outside the cluster group so it never
  // collapses into a Bay Area cluster — it's a categorical "no city"
  // marker, not a geographic one.
  var vendorLayer = L.layerGroup().addTo(map);
  STATE.map = map;
  STATE.markerLayer = markerLayer;
  STATE.vendorLayer = vendorLayer;
}

function bubbleRadius(count) {
  return 6 + Math.sqrt(count) * 5;
}

// Off-coast point for vendor-primary articles (Flock Safety etc.) so they
// don't visually claim a real city. ~Pacific west of central California,
// visible at the default Bay Area zoom.
var VENDOR_BUCKET_LATLNG = [36.5, -125.0];

function renderMap(matched) {
  if (!STATE.map) return;
  STATE.markerLayer.clearLayers();
  STATE.vendorLayer.clearLayers();

  // Plot one pin per agency the article is substantively about
  // (primary_subject_agencies). An article about three cities gets
  // three pins; an article that merely name-checks them gets zero
  // (the curator filters those out). Vendor-primary entries (no real
  // subject city) get bucketed into a single off-coast pip.
  //
  // When an agency filter is active, suppress sibling-subject pins:
  // a multi-city article only contributes a pin at the *filtered*
  // agency. Otherwise an article tagged [San Mateo, Foster City] would
  // still drop a Foster City pin even though the user narrowed to
  // San Mateo, which reads as "the filter leaked."
  var byAgency = {};
  var vendorBucket = { articles: [], names: {} };
  for (var i = 0; i < matched.length; i++) {
    var a = matched[i];
    var subjects = a.primary_subject_agencies || [];
    var seenForArticle = {};
    var vendorAddedForArticle = false;
    for (var s = 0; s < subjects.length; s++) {
      var ag = subjects[s];
      if (!ag) continue;
      if (STATE.agencyFilter && ag.agency_id !== STATE.agencyFilter) continue;
      if (ag.is_vendor) {
        if (vendorAddedForArticle) continue;
        vendorBucket.articles.push(a);
        vendorBucket.names[ag.name] = true;
        vendorAddedForArticle = true;
        continue;
      }
      if (ag.lat == null || ag.lng == null) continue;
      var key = ag.agency_id;
      if (seenForArticle[key]) continue;
      seenForArticle[key] = true;
      if (!byAgency[key]) {
        byAgency[key] = { agency: ag, articles: [] };
      }
      byAgency[key].articles.push(a);
    }
  }

  var bounds = [];
  for (var aid in byAgency) {
    var entry = byAgency[aid];
    var ag = entry.agency;
    var arts = entry.articles;
    var marker = L.circleMarker([ag.lat, ag.lng], {
      radius: bubbleRadius(arts.length),
      color: '#1e3a8a',
      weight: 1.5,
      fillColor: '#60a5fa',
      fillOpacity: 0.7,
      agencyName: ag.name,
      articleCount: arts.length
    });

    var popup = '<div class="pop-agency">' + escapeHtml(ag.name)
      + (ag.state ? ' <span class="pop-meta">' + escapeHtml(ag.state) + '</span>' : '')
      + '</div><ul>';
    for (var k = 0; k < arts.length; k++) {
      var art = arts[k];
      var artUrl = safeUrl(art.url);
      popup += '<li>'
        + (artUrl
            ? '<a href="' + escapeHtml(artUrl) + '" target="_blank" rel="noopener">'
              + escapeHtml(art.title) + '</a>'
            : escapeHtml(art.title))
        + (art.published_at
            ? ' <span class="pop-meta">' + formatDate(art.published_at) + '</span>'
            : '')
        + '</li>';
    }
    popup += '</ul>';
    marker.bindPopup(popup);
    marker.bindTooltip(
      ag.name + ' — ' + arts.length + ' article' + (arts.length !== 1 ? 's' : ''),
      { direction: 'top', offset: [0, -4] }
    );
    STATE.markerLayer.addLayer(marker);
    bounds.push([ag.lat, ag.lng]);
  }

  if (vendorBucket.articles.length > 0) {
    var arts = vendorBucket.articles;
    var vendorNames = Object.keys(vendorBucket.names).sort();
    var label = vendorNames.length === 1 ? vendorNames[0] : 'Vendor coverage';
    var marker = L.circleMarker(VENDOR_BUCKET_LATLNG, {
      radius: bubbleRadius(arts.length),
      color: '#94a3b8',
      weight: 1.5,
      fillColor: '#475569',
      fillOpacity: 0.7,
      dashArray: '3 3',
      agencyName: label,
      articleCount: arts.length
    });
    var popup = '<div class="pop-agency">' + escapeHtml(label)
      + ' <span class="pop-meta">no specific city</span></div><ul>';
    for (var v = 0; v < arts.length; v++) {
      var art = arts[v];
      var artUrl = safeUrl(art.url);
      popup += '<li>'
        + (artUrl
            ? '<a href="' + escapeHtml(artUrl) + '" target="_blank" rel="noopener">'
              + escapeHtml(art.title) + '</a>'
            : escapeHtml(art.title))
        + (art.published_at
            ? ' <span class="pop-meta">' + formatDate(art.published_at) + '</span>'
            : '')
        + '</li>';
    }
    popup += '</ul>';
    marker.bindPopup(popup);
    marker.bindTooltip(
      label + ' — ' + arts.length + ' article' + (arts.length !== 1 ? 's' : ''),
      { direction: 'top', offset: [0, -4] }
    );
    STATE.vendorLayer.addLayer(marker);
    bounds.push(VENDOR_BUCKET_LATLNG);
  }

  if (bounds.length > 0) {
    STATE.map.fitBounds(bounds, { padding: [30, 30], maxZoom: 9 });
  }
}

function getMatched() {
  var articles = STATE.data.articles;
  var matched = [];
  for (var i = 0; i < articles.length; i++) {
    if (articleMatches(articles[i])) matched.push(articles[i]);
  }
  return matched;
}

function render() {
  var matched = getMatched();
  renderAgencyPin();
  renderTagGroups();
  renderMap(matched);
  renderArticleList(matched);
}

// Toolbar-style "Jump to city" search modeled on report.js#wireAgencySearch.
// Selecting a suggestion sets STATE.agencyFilter (rather than navigating)
// so the result feels like another filter in the chip system. ↓/↑ moves
// highlight, Enter accepts, Esc closes. Exposed via wireAgencySearchInput
// because renderAgencyPin() removes and rebuilds the <input> element
// when the user clears the pin, and the new input needs handlers wired.
function wireAgencySearchInput() {
  var input = document.getElementById('agency-search-input');
  var results = document.getElementById('agency-search-results');
  if (!input || !results) return;
  var highlightIdx = -1;

  function close() {
    results.classList.remove('open');
    results.innerHTML = '';
    highlightIdx = -1;
  }

  function pick(agencyId) {
    input.value = '';
    close();
    setAgencyFilter(agencyId);
  }

  function doSearch(q) {
    q = (q || '').trim().toLowerCase();
    if (!q) { close(); return; }
    var matches = [];
    for (var i = 0; i < STATE.agencyIndex.length; i++) {
      var e = STATE.agencyIndex[i];
      var n = e._lower;
      var score = 0;
      if (n === q) score = 100;
      else if (n.indexOf(q) === 0) score = 50;
      else if (n.indexOf(q) >= 0) score = 10;
      if (score > 0) matches.push({ e: e, score: score });
    }
    matches.sort(function (a, b) {
      if (b.score !== a.score) return b.score - a.score;
      if (b.e.count !== a.e.count) return b.e.count - a.e.count;
      return a.e._lower < b.e._lower ? -1 : 1;
    });
    var shown = matches.slice(0, 15);
    if (!shown.length) {
      results.innerHTML = '<div class="sr-empty">No matching agency in the article library</div>';
    } else {
      var html = '';
      for (var k = 0; k < shown.length; k++) {
        var ent = shown[k].e;
        var state = ent.state ? '<span class="sr-state">' + escapeHtml(ent.state) + '</span>' : '';
        var count = '<span class="sr-count">' + ent.count + ' article' + (ent.count !== 1 ? 's' : '') + '</span>';
        html += '<a href="#" data-id="' + escapeHtml(ent.agency_id) + '">'
          + count
          + escapeHtml(ent.name) + state
          + '</a>';
      }
      results.innerHTML = html;
    }
    results.classList.add('open');
    highlightIdx = -1;
    var links = results.querySelectorAll('a');
    for (var li = 0; li < links.length; li++) {
      links[li].addEventListener('click', function (ev) {
        ev.preventDefault();
        pick(ev.currentTarget.getAttribute('data-id'));
      });
    }
  }

  function updateHighlight() {
    var links = results.querySelectorAll('a');
    for (var i = 0; i < links.length; i++) {
      links[i].classList.toggle('hl', i === highlightIdx);
    }
    if (highlightIdx >= 0 && links[highlightIdx]) {
      links[highlightIdx].scrollIntoView({ block: 'nearest' });
    }
  }

  input.addEventListener('input', function () { doSearch(input.value); });
  input.addEventListener('keydown', function (e) {
    var links = results.querySelectorAll('a');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!results.classList.contains('open')) doSearch(input.value);
      if (links.length) {
        highlightIdx = Math.min(highlightIdx + 1, links.length - 1);
        updateHighlight();
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (links.length) {
        highlightIdx = Math.max(highlightIdx - 1, 0);
        updateHighlight();
      }
    } else if (e.key === 'Enter') {
      if (highlightIdx >= 0 && links[highlightIdx]) {
        e.preventDefault();
        pick(links[highlightIdx].getAttribute('data-id'));
      } else if (links.length === 1) {
        e.preventDefault();
        pick(links[0].getAttribute('data-id'));
      }
    } else if (e.key === 'Escape') {
      close();
      input.blur();
    }
  });
  input.addEventListener('focus', function () {
    if (input.value.trim()) doSearch(input.value);
  });
  document.addEventListener('click', function (e) {
    if (!e.target.closest('#agency-search')) close();
  });
}

fetch('data/articles_data.json')
  .then(function (r) {
    if (!r.ok) throw new Error('fetch failed: ' + r.status);
    return r.json();
  })
  .then(function (data) {
    STATE.data = data;
    STATE.agencyIndex = buildAgencyIndex(data);
    var param = new URL(window.location.href).searchParams.get('agency');
    STATE.agencyFilter = resolveAgencyParam(data, param);
    // If a URL param pointed at an unknown agency, drop it from the URL
    // rather than leaving a dead query string sitting on the address bar.
    if (param && !STATE.agencyFilter) syncUrl();
    initMap();
    wireAgencySearchInput();
    render();
  })
  .catch(function (err) {
    document.getElementById('article-list').innerHTML =
      '<div class="empty">Failed to load article data: '
      + escapeHtml(err.message) + '</div>';
  });
