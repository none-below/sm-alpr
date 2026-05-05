// Article viewer: list + tag filter (include/exclude/off cycle).

var STATE = {
  data: null,
  filters: {},   // tag -> 'include' | 'exclude'
  map: null,
  markerLayer: null
};

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatDate(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined,
    { year: 'numeric', month: 'short', day: 'numeric' });
}

function cycleFilter(tag) {
  var cur = STATE.filters[tag];
  if (!cur) STATE.filters[tag] = 'include';
  else if (cur === 'include') STATE.filters[tag] = 'exclude';
  else delete STATE.filters[tag];
  render();
}

function clearFilters() {
  STATE.filters = {};
  render();
}

function articleMatches(article) {
  var tags = article.tags || [];
  var tagSet = {};
  for (var i = 0; i < tags.length; i++) tagSet[tags[i]] = true;
  var srcKey = 'source:' + (article.source_domain || '');
  for (var t in STATE.filters) {
    var mode = STATE.filters[t];
    var present = t.indexOf('source:') === 0 ? (t === srcKey) : tagSet[t];
    if (mode === 'include' && !present) return false;
    if (mode === 'exclude' && present) return false;
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

function renderActiveBar(matchedCount, totalCount) {
  var bar = document.getElementById('active-bar');
  var parts = [];
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

  var links = [];
  if (article.url) {
    links.push('<a href="' + escapeHtml(article.url)
      + '" target="_blank" rel="noopener">original</a>');
  }
  if (article.paths && article.paths.pdf) {
    links.push('<a href="../' + escapeHtml(article.paths.pdf)
      + '" target="_blank">pdf</a>');
  }
  if (article.wayback_url) {
    links.push('<a href="' + escapeHtml(article.wayback_url)
      + '" target="_blank" rel="noopener">archive.org</a>');
  }

  var titleHtml = article.url
    ? '<a href="' + escapeHtml(article.url)
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

function initMap() {
  if (STATE.map) return;
  var map = MapCommon.createCartoMap('map', { theme: 'dark' });
  var markerLayer = L.markerClusterGroup(MapCommon.clusterOptions({
    iconCreateFunction: MapCommon.countClusterIcon()
  }));
  MapCommon.attachClusterTooltips(markerLayer, function (cm) {
    return cm.options.agencyName;
  });
  markerLayer.addTo(map);
  STATE.map = map;
  STATE.markerLayer = markerLayer;
}

function bubbleRadius(count) {
  return 6 + Math.sqrt(count) * 5;
}

function renderMap(matched) {
  if (!STATE.map) return;
  STATE.markerLayer.clearLayers();

  // Plot each article at its primary subject agency only — mentions of
  // other agencies (Flock HQ, peer cities) would otherwise scatter the
  // article across the map.
  var byAgency = {};
  for (var i = 0; i < matched.length; i++) {
    var a = matched[i];
    var ag = a.primary_subject_agency;
    if (!ag || ag.lat == null || ag.lng == null) continue;
    var key = ag.agency_id;
    if (!byAgency[key]) {
      byAgency[key] = { agency: ag, articles: [] };
    }
    byAgency[key].articles.push(a);
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
      popup += '<li>'
        + (art.url
            ? '<a href="' + escapeHtml(art.url) + '" target="_blank" rel="noopener">'
              + escapeHtml(art.title) + '</a>'
            : escapeHtml(art.title))
        + (art.published_at
            ? ' <span class="pop-meta">' + formatDate(art.published_at) + '</span>'
            : '')
        + '</li>';
    }
    popup += '</ul>';
    marker.bindPopup(popup);
    STATE.markerLayer.addLayer(marker);
    bounds.push([ag.lat, ag.lng]);
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
  renderTagGroups();
  renderMap(matched);
  renderArticleList(matched);
}

fetch('data/articles_data.json')
  .then(function (r) {
    if (!r.ok) throw new Error('fetch failed: ' + r.status);
    return r.json();
  })
  .then(function (data) {
    STATE.data = data;
    initMap();
    render();
  })
  .catch(function (err) {
    document.getElementById('article-list').innerHTML =
      '<div class="empty">Failed to load article data: '
      + escapeHtml(err.message) + '</div>';
  });
