
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

  const markerLayer = L.markerClusterGroup({
    maxClusterRadius: 20,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    zoomToBoundsOnClick: true,
    iconCreateFunction: function(cluster) {
      const children = cluster.getAllChildMarkers();
      const size = Math.min(44, 22 + children.length * 2);
      const r = size / 2;

      // Count categories
      let red = 0, orange = 0, blue = 0, gray = 0;
      children.forEach(cm => {
        const slug = cm.options.slug;
        if (!slug) { gray++; return; }
        const info = agencyInfo[slug] || {};
        if (isViolation(slug)) red++;
        else if (hasOutboundViolation(cm._markerData || {})) orange++;
        else if (info.crawled || cm.options.fillColor === '#2563eb') blue++;
        else gray++;
      });

      // Build SVG pie chart
      const total = children.length;
      const segments = [];
      let angle = 0;
      [[red, '#dc2626'], [orange, '#f97316'], [blue, '#2563eb'], [gray, '#8b5cf6']].forEach(([count, color]) => {
        if (count === 0) return;
        const sweep = (count / total) * 360;
        if (count === total) {
          segments.push('<circle cx="' + r + '" cy="' + r + '" r="' + (r-1) + '" fill="' + color + '"/>');
        } else {
          const startRad = angle * Math.PI / 180;
          const endRad = (angle + sweep) * Math.PI / 180;
          const x1 = r + (r-1) * Math.sin(startRad);
          const y1 = r - (r-1) * Math.cos(startRad);
          const x2 = r + (r-1) * Math.sin(endRad);
          const y2 = r - (r-1) * Math.cos(endRad);
          const large = sweep > 180 ? 1 : 0;
          segments.push('<path d="M' + r + ',' + r + ' L' + x1 + ',' + y1 + ' A' + (r-1) + ',' + (r-1) + ' 0 ' + large + ',1 ' + x2 + ',' + y2 + ' Z" fill="' + color + '"/>');
        }
        angle += sweep;
      });

      const svg = '<svg width="' + size + '" height="' + size + '" xmlns="http://www.w3.org/2000/svg">' +
        segments.join('') +
        '<circle cx="' + r + '" cy="' + r + '" r="' + (r * 0.55) + '" fill="white"/>' +
        '<text x="' + r + '" y="' + (r + 4) + '" text-anchor="middle" font-size="11" font-weight="bold" fill="#374151">' + total + '</text>' +
        '</svg>';

      return L.divIcon({
        html: svg,
        className: '',
        iconSize: [size, size],
      });
    },
  });

  // Show member names on cluster hover
  markerLayer.on('clustermouseover', function(e) {
    const children = e.layer.getAllChildMarkers();
    if (children.length > 15) {
      e.layer.bindTooltip(children.length + ' agencies').openTooltip();
      return;
    }
    const names = children.map(cm => {
      const slug = cm.options.slug;
      const info = agencyInfo[slug] || {};
      let name = info.name || slug;
      if (isViolation(slug)) name = '\u26a0 ' + name;
      return name;
    }).sort();
    e.layer.bindTooltip(names.join('<br>'), { direction: 'top' }).openTooltip();
  });
  markerLayer.on('clustermouseout', function(e) {
    e.layer.unbindTooltip();
  });

  markerLayer.addTo(map);
  const lineLayer = L.layerGroup().addTo(map);
  const markersBySlug = {};

  function defaultRadius(m) {
    if (m.crawled) return Math.max(4, Math.min(10, Math.sqrt(m.cameras || 1) * 2));
    return 4;  // uncrawled: same base size as small cities
  }

  function isViolation(slug) {
    const info = agencyInfo[slug] || {};
    if (info.public === false && info.type !== 'test') return true;  // private entity
    if (info.state && info.state !== 'CA') return true;               // out-of-state
    if (info.type === 'federal') return true;                         // federal — not "agency of the state" per §1798.90.5(f)
    if (info.type === 'decommissioned') return true;
    if (info.type === 'test') return true;
    return false;
  }

  // Does this agency share with any violation entities?
  function hasOutboundViolation(m) {
    return (m.outbound_slugs || []).some(s => isViolation(s));
  }

  function defaultColor(m) {
    if (isViolation(m.slug)) return { fill: '#dc2626', border: '#991b1b', opacity: 0.8 };
    if (hasOutboundViolation(m)) return { fill: '#f97316', border: '#c2410c', opacity: 0.7 };
    if (m.crawled) return { fill: '#2563eb', border: '#1e40af', opacity: 0.6 };
    return { fill: '#8b5cf6', border: '#6d28d9', opacity: 0.5 };
  }

  function distKm(lat1, lng1, lat2, lng2) {
    const R = 6371;
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLng = (lng2 - lng1) * Math.PI / 180;
    const a = Math.sin(dLat/2)**2 + Math.cos(lat1*Math.PI/180) * Math.cos(lat2*Math.PI/180) * Math.sin(dLng/2)**2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
  }

  function sortPriority(info) {
    if (info.state && info.state !== 'CA') return 0;           // out-of-state
    if (info.public === false) return 1;                        // private
    if (info.type === 'federal') return 2;                      // federal — not agency of the state
    if (info.type === 'decommissioned') return 3;               // decommissioned/DNU
    if (info.type === 'test') return 4;                         // test/demo
    if (info.notes && info.notes.indexOf('re-sharing') >= 0) return 5;  // re-sharing risk
    return 10;                                                  // normal
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
    if (info.type === 'federal')
      tag += ' <span style="color:#dc2626;font-weight:bold" title="Federal entity \u2014 not an agency of the state per CA Civil Code \u00a71798.90.5(f). AG Bulletin 2023-DLE-06 prohibits sharing with federal agencies.">[FEDERAL]</span>';
    if (info.type === 'decommissioned')
      tag += ' <span style="color:#f97316;font-weight:bold" title="Marked Do Not Use by Flock but still appears in sharing lists">[DECOMMISSIONED]</span>';
    if (info.type === 'test')
      tag += ' <span style="color:#f97316;font-weight:bold" title="Test/demo entry still in sharing list">[TEST]</span>';
    if (info.notes && info.notes.indexOf('re-sharing') >= 0)
      tag += ' <span style="color:#d97706;font-weight:bold" title="' + info.notes.replace(/"/g, '&quot;').replace(/<[^>]*>/g, '') + '">[RE-SHARES TO VIOLATIONS]</span>';
    const loc = coords[s];
    if (!loc) tag += ' <span style="color:#9ca3af">(not mapped)</span>';
    if (info.crawled) {
      tag += ' <a href="https://transparency.flocksafety.com/' + s + '" target="_blank" style="color:#6b7280;text-decoration:none" title="View transparency portal">\u2197</a>';
    }
    return label + tag;
  }

  // Place markers
  markers.forEach(m => {
    const col = defaultColor(m);
    const radius = isViolation(m.slug) ? Math.max(6, defaultRadius(m)) : defaultRadius(m);
    const circle = L.circleMarker([m.lat, m.lng], {
      radius: radius,
      fillColor: col.fill,
      color: col.border,
      weight: isViolation(m.slug) ? 2 : 1,
      fillOpacity: col.opacity,
      slug: m.slug,
    }).addTo(markerLayer);
    const info = agencyInfo[m.slug] || {};
    circle._markerData = m;
    const tipName = (info.name || m.slug) + (isViolation(m.slug) ? ' \u26a0' : '');
    circle.bindTooltip(tipName, { direction: 'top', offset: [0, -8], sticky: true });
    circle.on('click', (e) => { L.DomEvent.stopPropagation(e); showAgency(m); });
    markersBySlug[m.slug] = circle;
  });

  // After spiderfy, re-bind tooltips on the spidered markers
  markerLayer.on('spiderfied', function(e) {
    e.markers.forEach(cm => {
      const slug = cm.options.slug;
      const info = agencyInfo[slug] || {};
      const tipName = (info.name || slug) + (isViolation(slug) ? ' \u26a0' : '');
      cm.bindTooltip(tipName, { direction: 'top', offset: [0, -8], sticky: true });
    });
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
    const status = m.crawled ? 'Crawled' : 'No transparency page found (inferred from other portals)';
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
        c.setRadius(14);
        c.setStyle({ fillColor: '#06b6d4', fillOpacity: 1, weight: 3, color: '#0e7490' });
        c.bringToFront();
      } else if (myMismatches.has(mm.slug)) {
        c.setRadius(Math.max(6, defaultRadius(mm)));
        c.setStyle({ fillColor: '#f97316', fillOpacity: 0.9, weight: 2, color: '#c2410c' });
      } else if (connected.has(mm.slug)) {
        const col = defaultColor(mm);
        c.setRadius(defaultRadius(mm));
        c.setStyle({ fillColor: col.fill, fillOpacity: 0.8, weight: 1, color: col.border });
      } else if (isViolation(mm.slug)) {
        c.setRadius(3);
        c.setStyle({ fillColor: '#dc2626', fillOpacity: 0.3, weight: 1, color: '#991b1b' });
      } else {
        c.setRadius(2);
        c.setStyle({ fillColor: '#d1d5db', fillOpacity: 0.2, weight: 0.5, color: '#e5e7eb' });
      }
    });
  }

  function resetMarkers() {
    markers.forEach(mm => {
      const c = markersBySlug[mm.slug];
      if (!c) return;
      const col = defaultColor(mm);
      const radius = isViolation(mm.slug) ? Math.max(5, defaultRadius(mm)) : defaultRadius(mm);
      c.setRadius(radius);
      c.setStyle({ fillColor: col.fill, fillOpacity: col.opacity, weight: isViolation(mm.slug) ? 2 : 1, color: col.border });
    });
  }

  // Click map background to reset
  map.on('click', () => {
    lineLayer.clearLayers();
    resetMarkers();
    document.getElementById('info').innerHTML =
      '<h3>Flock ALPR Sharing Map</h3>' +
      '<p class="stat">Click an agency to see its sharing web.</p>' +
      '<p class="stat">311 agencies mapped.</p>';
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
      if (info.type === 'federal') html += '<p class="stat" style="color:#dc2626">Federal entity \u2014 not an \u201cagency of the state\u201d per \u00a71798.90.5(f). AG Bulletin prohibits sharing with federal agencies.</p>';
      else if (info.public === true) html += '<p class="stat" style="color:#16a34a">Public agency</p>';
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

  // Populate off-map violations panel
  const offmapPanel = document.getElementById('offmap');
  const offmapEntities = Object.entries(agencyInfo).filter(([slug, info]) => {
    return isViolation(slug) && !coords[slug];
  }).sort((a, b) => sortPriority(a[1]) - sortPriority(b[1]));

  if (offmapEntities.length) {
    let html = '<h4>\u26a0 Off-map violations (' + offmapEntities.length + ')</h4>';
    offmapEntities.slice(0, 30).forEach(([slug, info]) => {
      html += '<div onclick="clickSlug(\'' + slug + '\')">' + slugLabel(slug) + '</div>';
    });
    if (offmapEntities.length > 30) html += '<div>... and ' + (offmapEntities.length - 30) + ' more</div>';
    offmapPanel.innerHTML = html;
  } else {
    offmapPanel.style.display = 'none';
  }
});
