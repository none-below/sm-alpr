var MEDALS = {1: '&#x1F947;', 2: '&#x1F948;', 3: '&#x1F949;'};

function formatValue(id, value) {
  if (value == null) return '—';
  if (id === 'retention') return value.toLocaleString() + ' days';
  if (id === 'vehicles_30d') return value.toLocaleString() + ' plates';
  if (id === 'searches_30d') return value.toLocaleString() + ' lookups';
  if (id === 'cameras') return value.toLocaleString() + ' cameras';
  return value.toLocaleString();
}

function renderCategory(cat) {
  var card = document.createElement('div');
  card.className = 'card';

  var maxVal = cat.podium.length > 0 ? cat.podium[0].value : 1;

  var html = '<div class="card-title">' + cat.title + '</div>' +
    '<div class="card-subtitle">' + cat.subtitle + '</div>' +
    '<div class="podium">';

  if (cat.podium.length === 0) {
    html += '<div class="empty-state">No data available</div>';
  } else {
    for (var i = 0; i < cat.podium.length; i++) {
      var p = cat.podium[i];
      var pct = Math.round((p.value / maxVal) * 100);
      html += '<div class="podium-row rank-' + p.rank + '">' +
        '<div class="medal">' + (MEDALS[p.rank] || '') + '</div>' +
        '<div class="agency-info">' +
          '<div class="agency-name" title="' + p.name + '"><a href="sharing_map.html#' + encodeURIComponent(p.slug) + '" target="_blank">' + p.name + '</a></div>' +
          '<div class="agency-value">' + formatValue(cat.id, p.value) + '</div>' +
          '<div class="value-bar-wrap"><div class="value-bar" style="width:' + pct + '%"></div></div>' +
        '</div>' +
      '</div>';
    }
  }

  html += '</div>';
  card.innerHTML = html;
  return card;
}

function renderDisclaimers(meta) {
  var html = '<ul>';
  html += '<li>Only <strong>California</strong> agencies are ranked.</li>';
  html += '<li>Only agencies with a visible Flock Safety transparency portal are included &mdash; '
    + '<span class="stat">' + meta.agencies_crawled + '</span> of '
    + '<span class="stat">' + meta.agencies_in_registry + '</span> known California Flock agencies.</li>';
  html += '<li><span class="stat">' + meta.agencies_no_portal
    + '</span> California agencies appear in other agencies\u2019 sharing lists but have no findable transparency portal.</li>';
  html += '</ul>';
  return html;
}

function renderListCard(title, subtitle, list) {
  if (!list || list.length === 0) return null;
  var card = document.createElement('div');
  card.className = 'no-sharing-card';
  var html = '<div class="card-title">' + title + '</div>'
    + '<div class="card-subtitle">' + subtitle + '</div>'
    + '<ul class="no-sharing-list">';
  for (var i = 0; i < list.length; i++) {
    html += '<li><a href="sharing_map.html#' + encodeURIComponent(list[i].slug)
      + '" target="_blank">' + list[i].name + '</a></li>';
  }
  html += '</ul>';
  card.innerHTML = html;
  return card;
}

function renderNoSharingCard(list) {
  return renderListCard(
    'Transparency Gaps',
    list.length + ' agencies have a portal but publish no sharing data',
    list
  );
}

fetch('data/scoreboard_data.json')
  .then(function(r) { return r.json(); })
  .then(function(data) {
    // Render disclaimers
    if (data.meta) {
      document.getElementById('disclaimers').innerHTML = renderDisclaimers(data.meta);
    }

    var grid = document.getElementById('grid');
    for (var i = 0; i < data.categories.length; i++) {
      grid.appendChild(renderCategory(data.categories[i]));
    }

    // Render no-sharing card at the end
    var noSharingCard = renderNoSharingCard(data.no_sharing_published);
    if (noSharingCard) {
      grid.appendChild(noSharingCard);
    }

    // Render no-portal card from map data
    fetch('data/map_data.json')
      .then(function(r) { return r.json(); })
      .then(function(mapData) {
        var noPortal = [];
        var ai = mapData.agencyInfo || {};
        for (var slug in ai) {
          var info = ai[slug];
          if (info.state === 'CA' && !info.crawled && info.public !== false
              && info.type !== 'test' && info.type !== 'decommissioned') {
            noPortal.push({ slug: slug, name: info.name || slug });
          }
        }
        noPortal.sort(function(a, b) { return a.name.localeCompare(b.name); });
        var card = renderListCard(
          'No Transparency Portal',
          noPortal.length + ' California agencies appear in sharing lists but have no findable portal',
          noPortal
        );
        if (card) grid.appendChild(card);
      });
  })
  .catch(function(err) {
    document.getElementById('grid').innerHTML =
      '<div style="color:#ef4444;padding:20px;">Failed to load scoreboard data.</div>';
  });
