
// Load data
fetch('data/map_data.json').then(r => r.json()).then(data => {
  const markers = data.markers;
  const coords = data.coords;
  const agencyInfo = data.agencyInfo;
  const mismatches = data.mismatches;

  const map = L.map('map').setView([37.5, -121.5], 7);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png', {
    attribution: '&copy; OpenStreetMap, &copy; CARTO',
    maxZoom: 18,
  }).addTo(map);

  const markerLayer = L.layerGroup().addTo(map);
  const lineLayer = L.layerGroup().addTo(map);
  const markersBySlug = {};

  function defaultRadius(m) {
    return m.crawled ? Math.max(4, Math.min(10, Math.sqrt(m.cameras || 1) * 2)) : 3;
  }

  function distKm(lat1, lng1, lat2, lng2) {
    const R = 6371;
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLng = (lng2 - lng1) * Math.PI / 180;
    const a = Math.sin(dLat/2)**2 + Math.cos(lat1*Math.PI/180) * Math.cos(lat2*Math.PI/180) * Math.sin(dLng/2)**2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
  }

  function sortPriority(info) {
    if (info.state && info.state !== 'CA') return 0;
    if (info.public === false) return 1;
    if (info.type === 'decommissioned') return 2;
    if (info.type === 'test') return 3;
    return 10;
  }

  function sortOutbound(slugs, fromLat, fromLng) {
    return [...slugs].sort((a, b) => {
      const ai = agencyInfo[a] || {};
      const bi = agencyInfo[b] || {};
      const aPri = sortPriority(ai);
      const bPri = sortPriority(bi);
      if (aPri !== bPri) return aPri - bPri;
      const aCoord = coords[a];
      const bCoord = coords[b];
      const aDist = aCoord ? distKm(fromLat, fromLng, aCoord[0], aCoord[1]) : 0;
      const bDist = bCoord ? distKm(fromLat, fromLng, bCoord[0], bCoord[1]) : 0;
      return bDist - aDist;
    });
  }

  function slugLabel(s) {
    const info = agencyInfo[s] || {};
    let label = info.name || s;
    let tag = '';
    if (info.state && info.state !== 'CA')
      tag += ' <span style="color:#dc2626;font-weight:bold" title="Out-of-state sharing may violate CA Civil Code \u00a71798.90.55(b)">[' + info.state + ' \u2014 out of state]</span>';
    if (info.public === false && info.type !== 'decommissioned' && info.type !== 'test')
      tag += ' <span style="color:#dc2626;font-weight:bold" title="CA Civil Code \u00a71798.90.55(b) restricts ALPR sharing to public agencies">[PRIVATE \u2014 likely violates SB 34]</span>';
    if (info.type === 'decommissioned')
      tag += ' <span style="color:#f97316;font-weight:bold" title="Marked Do Not Use by Flock but still appears in sharing lists">[DECOMMISSIONED]</span>';
    if (info.type === 'test')
      tag += ' <span style="color:#f97316;font-weight:bold" title="Test/demo entry still in sharing list">[TEST]</span>';
    const loc = coords[s];
    if (!loc) tag += ' <span style="color:#9ca3af">(not mapped)</span>';
    if (info.crawled) {
      tag += ' <a href="https://transparency.flocksafety.com/' + s + '" target="_blank" style="color:#6b7280;text-decoration:none" title="View transparency portal">\u2197</a>';
    }
    return label + tag;
  }

  // Place markers
  markers.forEach(m => {
    const circle = L.circleMarker([m.lat, m.lng], {
      radius: defaultRadius(m),
      fillColor: m.crawled ? '#2563eb' : '#9ca3af',
      color: m.crawled ? '#1e40af' : '#6b7280',
      weight: 1,
      fillOpacity: m.crawled ? 0.6 : 0.3,
    }).addTo(markerLayer);
    circle.bindTooltip(m.slug, { direction: 'top', offset: [0, -8] });
    circle.on('click', (e) => { L.DomEvent.stopPropagation(e); showAgency(m); });
    markersBySlug[m.slug] = circle;
  });

  function showAgency(m) {
    lineLayer.clearLayers();

    let outConnected = 0;
    (m.outbound_slugs || []).forEach(target => {
      if (coords[target]) {
        L.polyline([[m.lat, m.lng], coords[target]], { color: '#2563eb', weight: 1.5, opacity: 0.3 }).addTo(lineLayer);
        outConnected++;
      }
    });

    let inConnected = 0;
    (m.inbound_slugs || []).forEach(source => {
      if (coords[source]) {
        L.polyline([coords[source], [m.lat, m.lng]], { color: '#16a34a', weight: 1.5, opacity: 0.3, dashArray: '4 4' }).addTo(lineLayer);
        inConnected++;
      }
    });

    const info = document.getElementById('info');
    const status = m.crawled ? 'Crawled' : 'Not crawled (inferred from other portals)';
    const statusColor = m.crawled ? '#16a34a' : '#f97316';
    let html = '<h3>' + m.slug + '</h3>';
    if (m.crawled) {
      html += '<p class="stat"><a href="https://transparency.flocksafety.com/' + m.slug + '" target="_blank" style="color:#2563eb">View transparency portal \u2197</a></p>';
    }
    html += '<p class="stat" style="color:' + statusColor + '">' + status + '</p>';
    if (m.cameras) html += '<p class="stat">Cameras: ' + m.cameras + '</p>';
    if (m.retention_days) html += '<p class="stat">Retention: ' + m.retention_days + ' days</p>';
    html += '<p class="stat">Shares with: ' + m.outbound_count + ' agencies (' + outConnected + ' mapped)</p>';
    html += '<p class="stat">Receives from: ' + (m.inbound_count || (m.inbound_slugs ? m.inbound_slugs.length : 0)) + ' agencies (' + inConnected + ' mapped)</p>';
    const mInfo = agencyInfo[m.slug] || {};
    if (mInfo.notes) html += '<p class="stat" style="background:#fef3c7;padding:6px 8px;border-radius:4px;color:#92400e;margin-top:6px">' + mInfo.notes + '</p>';

    if (m.outbound_slugs && m.outbound_slugs.length) {
      html += '<div class="sharing-list"><strong>Shares with (outbound):</strong>';
      sortOutbound(m.outbound_slugs, m.lat, m.lng).slice(0, 50).forEach(function(s) {
        html += '<div style="cursor:pointer" onclick="clickSlug(\'' + s + '\')">' + slugLabel(s) + '</div>';
      });
      if (m.outbound_slugs.length > 50) html += '<div>... and ' + (m.outbound_slugs.length - 50) + ' more</div>';
      html += '</div>';
    }

    if (m.inbound_slugs && m.inbound_slugs.length) {
      html += '<div class="sharing-list"><strong>Receives from (inbound):</strong>';
      sortOutbound(m.inbound_slugs, m.lat, m.lng).slice(0, 50).forEach(function(s) {
        html += '<div style="cursor:pointer" onclick="clickSlug(\'' + s + '\')">' + slugLabel(s) + '</div>';
      });
      if (m.inbound_slugs.length > 50) html += '<div>... and ' + (m.inbound_slugs.length - 50) + ' more</div>';
      html += '</div>';
    }

    info.innerHTML = html;

    const connected = new Set();
    connected.add(m.slug);
    (m.outbound_slugs || []).forEach(s => connected.add(s));
    (m.inbound_slugs || []).forEach(s => connected.add(s));
    const myMismatches = new Set(mismatches[m.slug] || []);

    markers.forEach(mm => {
      const c = markersBySlug[mm.slug];
      if (!c) return;
      if (mm.slug === m.slug) {
        c.setRadius(12);
        c.setStyle({ fillColor: '#dc2626', fillOpacity: 1, weight: 2, color: '#991b1b' });
        c.bringToFront();
      } else if (myMismatches.has(mm.slug)) {
        c.setRadius(Math.max(6, defaultRadius(mm)));
        c.setStyle({ fillColor: '#f97316', fillOpacity: 0.9, weight: 2, color: '#c2410c' });
      } else if (connected.has(mm.slug)) {
        c.setRadius(defaultRadius(mm));
        c.setStyle({ fillColor: mm.crawled ? '#2563eb' : '#9ca3af', fillOpacity: 0.8, weight: 1, color: mm.crawled ? '#1e40af' : '#6b7280' });
      } else {
        c.setRadius(2);
        c.setStyle({ fillColor: '#d1d5db', fillOpacity: 0.2, weight: 0.5, color: '#e5e7eb' });
      }
    });
  }

  // Click map background to reset
  map.on('click', () => {
    lineLayer.clearLayers();
    markers.forEach(mm => {
      const c = markersBySlug[mm.slug];
      if (!c) return;
      c.setRadius(defaultRadius(mm));
      c.setStyle({
        fillColor: mm.crawled ? '#2563eb' : '#9ca3af',
        fillOpacity: mm.crawled ? 0.6 : 0.3,
        weight: 1,
        color: mm.crawled ? '#1e40af' : '#6b7280',
      });
    });
    document.getElementById('info').innerHTML =
      '<h3>Flock ALPR Sharing Map</h3>' +
      '<p class="stat">Click an agency to see its sharing web.</p>' +
      '<p class="stat">310 agencies mapped.</p>';
  });

  // Navigate to slug from info panel
  const markerDataBySlug = {};
  markers.forEach(m => { markerDataBySlug[m.slug] = m; });

  window.clickSlug = function(slug) {
    const m = markerDataBySlug[slug];
    if (m) {
      map.setView([m.lat, m.lng], 10);
      showAgency(m);
    } else {
      const info = agencyInfo[slug] || {};
      const panel = document.getElementById('info');
      let html = '<h3>' + (info.name || slug) + '</h3>';
      html += '<p class="stat" style="color:#f97316">No map location</p>';
      if (info.crawled) {
        html += '<p class="stat"><a href="https://transparency.flocksafety.com/' + slug + '" target="_blank" style="color:#2563eb">View transparency portal \u2197</a></p>';
      }
      if (info.state) html += '<p class="stat">State: ' + info.state + '</p>';
      if (info.role) html += '<p class="stat">Role: ' + info.role + '</p>';
      if (info.type) html += '<p class="stat">Type: ' + info.type + '</p>';
      if (info.public === true) html += '<p class="stat" style="color:#16a34a">Public agency</p>';
      if (info.public === false) html += '<p class="stat" style="color:#dc2626">Not a public agency \u2014 sharing likely violates SB 34</p>';
      if (info.notes) html += '<p class="stat" style="background:#fef3c7;padding:6px 8px;border-radius:4px;color:#92400e;margin-top:6px">' + info.notes + '</p>';

      const sharedBy = markers.filter(mm => (mm.outbound_slugs || []).includes(slug));
      if (sharedBy.length) {
        html += '<div class="sharing-list"><strong>Receives data from (' + sharedBy.length + '):</strong>';
        sharedBy.forEach(function(mm) {
          html += '<div style="cursor:pointer" onclick="clickSlug(\'' + mm.slug + '\')">' + slugLabel(mm.slug) + '</div>';
        });
        html += '</div>';
      }
      panel.innerHTML = html;
    }
  };
});
