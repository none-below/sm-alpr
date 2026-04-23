// Feature flags
const SHOW_SHARES_WITH_TAGS = false; // [SHARES WITH FLAGGED ENTITY] and [SHARES WITH SUED AGENCY]

// Sanitization helpers
function escapeHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
const SLUG_RE = /^[a-z0-9][a-z0-9\-]*$/;
function safeSlug(s) { return SLUG_RE.test(s) ? s : ''; }

// Meeting-banner data + helpers live in docs/js/meeting_banners.js, loaded
// from sharing_map.html before this script. Call window.renderMeetingBannerHtml.

// Load data
Promise.all([
  fetch('data/map_data.json?v=CACHE_BUST').then(r => r.json()),
  fetch('data/agency_changelog.json?v=CACHE_BUST').then(r => r.ok ? r.json() : null).catch(() => null),
]).then(([data, changelog]) => {
  const markers = data.markers;
  const coords = data.coords;
  const agencyInfo = data.agencyInfo;
  const mismatches = data.mismatches;
  const indirectFlags = data.indirectFlags || {};
  const changelogBySlug = (changelog && changelog.by_slug) || {};
  const changelogMeta = changelog || { window_days: 90, tracking_days: null, window_complete: false };
  let showChanges = localStorage.getItem('smalpr-show-changes') !== 'false';
  let currentSelectionSlug = null;

  const map = L.map('map').setView([37.5, -121.5], 7);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png', {
    attribution: '&copy; OpenStreetMap, &copy; CARTO',
    maxZoom: 18,
  }).addTo(map);

  const markerLayer = L.markerClusterGroup({
    maxClusterRadius: 50,
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
        if (isFlagged(slug)) red++;
        else if (hasOutboundFlags(cm._markerData || {})) orange++;
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
      if (isFlagged(slug)) name = '\u26a0 ' + name;
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

  function isFlagged(slug) {
    const info = agencyInfo[slug] || {};
    if (info.public === false && info.type !== 'test') return true;  // private entity
    if (info.state && info.state !== 'CA') return true;               // out-of-state
    if (info.type === 'federal') return true;                         // federal — not "agency of the state" per §1798.90.5(f)
    if (info.type === 'fusion_center') return true;                    // fusion center — may not qualify as public agency per §1798.90.5(f)
    if (info.type === 'decommissioned') return true;
    if (info.type === 'test') return true;
    return false;
  }

  // Does this agency share with any flagged entities?
  function hasOutboundFlags(m) {
    return (m.outbound_slugs || []).some(s => isFlagged(s));
  }

  function defaultColor(m) {
    if (isFlagged(m.slug)) return { fill: '#dc2626', border: '#991b1b', opacity: 0.8 };
    if (hasOutboundFlags(m)) return { fill: '#f97316', border: '#c2410c', opacity: 0.7 };
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
    if (info.ag_lawsuit) return -1;                              // AG lawsuit — top priority
    if (info.state && info.state !== 'CA') return 0;           // out-of-state
    if (info.public === false) return 1;                        // private
    if (info.type === 'federal') return 2;                      // federal — not agency of the state
    if (info.type === 'fusion_center') return 2;                // fusion center — questionable public agency status
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
    let label = escapeHtml(info.name || s);
    let tag = '';
    if (info.state && info.state !== 'CA')
      tag += ' <span style="color:#dc2626;font-weight:bold" title="Out-of-state sharing may violate CA Civil Code \u00a71798.90.55(b)">[' + info.state + ' \u2014 out of state]</span>';
    // Category tag — one per entity, picked by the most specific concern.
    // Each label names the actual concern rather than a blanket "violates SB 34"
    // claim, since the problem differs by category (access controls, custodian
    // of record, statutory status, re-sharing scope, etc.).
    if (info.role === 'vendor') {
      tag += ' <span style="color:#dc2626;font-weight:bold" title="Private vendor. Flock MSA \u00a75.3 grants Flock independent authority to disclose agency data to third parties without agency approval. AG Bulletin 2023-DLE-06 directed agencies to review vendor contracts for exactly this type of provision. \u00a75.3 survives contract termination (\u00a77.3).">[VENDOR \u2014 \u00a75.3 disclosure authority]</span>';
    } else if (info.type === 'federal') {
      tag += ' <span style="color:#dc2626;font-weight:bold" title="Federal entity \u2014 not an agency of the state per CA Civil Code \u00a71798.90.5(f). AG Bulletin 2023-DLE-06 prohibits sharing with federal agencies.">[FEDERAL]</span>';
    } else if (info.type === 'fusion_center') {
      tag += ' <span style="color:#dc2626;font-weight:bold" title="Multi-agency re-sharing hub. Data sent here is redistributed to many downstream entities, some of which may not qualify as &ldquo;public agencies&rdquo; under CA Civil Code \u00a71798.90.5(f). See notes for per-hub specifics.">[RE-SHARING HUB]</span>';
    } else if (info.type === 'test') {
      tag += ' <span style="color:#dc2626;font-weight:bold" title="Test or demo account. Access controls are unknown and there is no agency of record accountable for queries against this data.">[TEST/FIXTURE \u2014 access controls unknown]</span>';
    } else if (info.type === 'decommissioned') {
      tag += ' <span style="color:#dc2626;font-weight:bold" title="Decommissioned or DNU entry on Flock\u2019s portal. No current custodian of record \u2014 who still holds credentials to query this data is unknown.">[INACTIVE \u2014 no current custodian]</span>';
    } else if (info.type === 'private') {
      const nm = (info.name || '').toLowerCase();
      if (nm.indexOf('university') >= 0 || nm.indexOf('college') >= 0) {
        tag += ' <span style="color:#dc2626;font-weight:bold" title="Private university police departments are authorized under CA Education Code \u00a776400 but their qualification as &ldquo;public agencies&rdquo; under CA Civil Code \u00a71798.90.5(f) is contested.">[PRIVATE UNIVERSITY PD \u2014 contested]</span>';
      } else {
        tag += ' <span style="color:#dc2626;font-weight:bold" title="Private entity \u2014 not a &ldquo;public agency&rdquo; under CA Civil Code \u00a71798.90.5(f). Sharing ALPR data with a private entity likely violates \u00a71798.90.55(b).">[PRIVATE ENTITY \u2014 not a public agency]</span>';
      }
    } else if (info.public === false) {
      tag += ' <span style="color:#dc2626;font-weight:bold" title="Not a &ldquo;public agency&rdquo; under CA Civil Code \u00a71798.90.5(f). Sharing ALPR data with non-agency recipients likely violates \u00a71798.90.55(b).">[PRIVATE ENTITY \u2014 not a public agency]</span>';
    }
    if (info.ag_lawsuit)
      tag += ' <span style="color:#dc2626;font-weight:bold" title="CA Attorney General lawsuit for illegally resharing ALPR data to out-of-state agencies in violation of SB 34.">[RESHARES OUT-OF-STATE \u2014 AG litigation]</span>';
    // Curated status flags from the registry (inactive, deactivated, dnu, duplicate, decommissioned)
    const FLAG_BADGES = {
      inactive: { color: '#6b7280', label: 'INACTIVE', title: 'Marked inactive on Flock\'s portal' },
      deactivated: { color: '#6b7280', label: 'DEACTIVATED', title: 'Marked deactivated on Flock\'s portal' },
      dnu: { color: '#dc2626', label: 'DNU', title: 'Do Not Use — flagged on Flock\'s portal' },
      duplicate: { color: '#dc2626', label: 'DUPLICATE', title: 'Marked as duplicate entry on Flock\'s portal' },
      decommissioned: { color: '#6b7280', label: 'DECOMMISSIONED', title: 'Decommissioned entry on Flock\'s portal' },
    };
    (info.flags || []).forEach(f => {
      const b = FLAG_BADGES[f];
      if (b) tag += ' <span style="color:' + b.color + ';font-weight:bold" title="' + b.title + '">[' + b.label + ']</span>';
    });
    // Check if this agency re-shares to flagged entities (from marker data)
    const mData = (typeof markerDataBySlug !== 'undefined') && markerDataBySlug[s];
    if (SHOW_SHARES_WITH_TAGS && mData && !isFlagged(s)) {
      const hasOutboundViol = (mData.outbound_slugs || []).some(t => isFlagged(t));
      if (hasOutboundViol)
        tag += ' <span style="color:#d97706;font-weight:bold" title="This agency shares data with flagged entities (private, federal, test)">[SHARES WITH FLAGGED ENTITY]</span>';
    }
    // Flag agencies sharing with a sued agency
    if (SHOW_SHARES_WITH_TAGS && mData && !info.ag_lawsuit) {
      const sharesWithSued = (mData.outbound_slugs || []).some(t => (agencyInfo[t] || {}).ag_lawsuit);
      if (sharesWithSued)
        tag += ' <span style="color:#d97706;font-weight:bold" title="This agency shares ALPR data with an agency under AG lawsuit for illegal sharing">[SHARES WITH SUED AGENCY]</span>';
    }
    // Flag uncrawled agencies with no transparency portal
    if (info.crawled === false && info.public !== false && info.type !== 'test' && info.type !== 'decommissioned')
      tag += ' <span style="color:#d97706;font-size:11px" title="No transparency portal found">[no portal]</span>';
    const loc = coords[s];
    if (!loc) tag += ' <span style="color:#9ca3af">(not mapped)</span>';
    if (info.crawled) {
      tag += ' <a href="https://transparency.flocksafety.com/' + safeSlug(s) + '" target="_blank" style="color:#6b7280;text-decoration:none" title="View transparency portal">\u2197</a>';
    }
    return label + tag;
  }

  // Place markers
  // Create all markers but only add public ones to the map initially.
  // Non-public (private, test, decommissioned) only appear when an agency is selected.
  markers.forEach(m => {
    const col = defaultColor(m);
    const radius = isFlagged(m.slug) ? Math.max(6, defaultRadius(m)) : defaultRadius(m);
    const circle = L.circleMarker([m.lat, m.lng], {
      radius: radius,
      fillColor: col.fill,
      color: col.border,
      weight: isFlagged(m.slug) ? 2 : 1,
      fillOpacity: col.opacity,
      slug: m.slug,
    });
    const info = agencyInfo[m.slug] || {};
    circle._markerData = m;
    const tipName = (info.name || m.slug) + (isFlagged(m.slug) ? ' \u26a0' : '');
    circle.bindTooltip(tipName, { direction: 'top', offset: [0, -8], sticky: true });
    circle.on('click', (e) => { L.DomEvent.stopPropagation(e); showAgency(m); });
    markersBySlug[m.slug] = circle;
    // Defer adding to map — done after recipientSlugs is computed
    circle._shouldAdd = true;
  });

  // After spiderfy, re-bind tooltips on the spidered markers
  markerLayer.on('spiderfied', function(e) {
    e.markers.forEach(cm => {
      const slug = cm.options.slug;
      const info = agencyInfo[slug] || {};
      const tipName = (info.name || slug) + (isFlagged(slug) ? ' \u26a0' : '');
      cm.bindTooltip(tipName, { direction: 'top', offset: [0, -8], sticky: true });
    });
  });

  // Initial marker population happens after resetMarkers is defined (see below)

  // Temporary markers for inbound-only entities (community cameras, HOAs, etc.)
  let tempMarkers = [];

  function clearTempMarkers() {
    tempMarkers.forEach(tm => markerLayer.removeLayer(tm));
    tempMarkers = [];
  }

  function showAgency(m) {
    lineLayer.clearLayers();
    clearTempMarkers();
    currentSelectionSlug = m.slug;

    // Reveal hidden markers connected to the selected agency
    const connectedSlugs = new Set([
      ...(m.outbound_slugs || []),
      ...(m.inbound_slugs || []),
    ]);
    connectedSlugs.forEach(slug => {
      const c = markersBySlug[slug];
      if (c && !markerLayer.hasLayer(c)) {
        markerLayer.addLayer(c);
      }
    });

    let outConnected = 0;
    let outFlags = 0;
    let outNotMapped = 0;
    const unmappedFlagged = [];
    (m.outbound_slugs || []).forEach(target => {
      if (isFlagged(target)) outFlags++;
      if (coords[target]) {
        const lineColor = isFlagged(target) ? '#dc2626' : '#2563eb';
        const lineWeight = isFlagged(target) ? 2 : 1.5;
        L.polyline([[m.lat, m.lng], coords[target]], { color: lineColor, weight: lineWeight, opacity: 0.4 }).addTo(lineLayer);
        outConnected++;
      } else if (isFlagged(target)) {
        unmappedFlagged.push(target);
      } else {
        outNotMapped++;
      }
    });

    // Place unmapped flagged entities as warning markers along the top edge
    if (unmappedFlagged.length > 0) {
      const spread = Math.min(unmappedFlagged.length, 20);
      const startLng = m.lng - 2.5;
      const lngStep = 5.0 / Math.max(spread - 1, 1);
      const topLat = m.lat + 3.5;

      unmappedFlagged.slice(0, 20).forEach((slug, i) => {
        const vLat = topLat + (Math.random() - 0.5) * 0.3;
        const vLng = startLng + i * lngStep + (Math.random() - 0.5) * 0.2;
        const vInfo = agencyInfo[slug] || {};
        const label = (vInfo.name || slug);

        // Warning triangle marker
        const icon = L.divIcon({
          html: '<div style="font-size:16px;text-align:center" title="' + escapeHtml(label) + '">\u26a0</div>',
          className: '',
          iconSize: [24, 24],
          iconAnchor: [12, 12],
        });
        const tm = L.marker([vLat, vLng], { icon: icon }).addTo(lineLayer);
        tm.bindTooltip(label, { direction: 'top', offset: [0, -10] });
        tm.on('click', (e) => { L.DomEvent.stopPropagation(e); window.clickSlug(slug); });

        // Red dashed line from agency to warning
        L.polyline([[m.lat, m.lng], [vLat, vLng]], {
          color: '#dc2626', weight: 1.5, opacity: 0.3, dashArray: '4 4'
        }).addTo(lineLayer);
      });

      if (unmappedFlagged.length > 20) {
        // Overflow indicator
        const overflowIcon = L.divIcon({
          html: '<div style="background:#dc2626;color:white;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold;white-space:nowrap">+' + (unmappedFlagged.length - 20) + ' more</div>',
          className: '',
          iconSize: [80, 20],
          iconAnchor: [40, 10],
        });
        L.marker([topLat + 0.4, m.lng], { icon: overflowIcon }).addTo(lineLayer);
      }
    }

    // Overlay recent sharing changes for the selected agency (last 90d)
    const chg = showChanges ? changelogBySlug[m.slug] : null;
    if (chg) {
      (chg.sharing_outbound_added || []).forEach(it => {
        if (it.slug && coords[it.slug]) {
          L.polyline([[m.lat, m.lng], coords[it.slug]], {
            color: '#16a34a', weight: 4, opacity: 0.55,
          }).addTo(lineLayer).bindTooltip(
            'Started sharing on/around ' + it.date,
            { direction: 'top', sticky: true }
          );
        }
      });
      (chg.sharing_outbound_removed || []).forEach(it => {
        if (it.slug && coords[it.slug]) {
          const tCoord = coords[it.slug];
          L.polyline([[m.lat, m.lng], tCoord], {
            color: '#9ca3af', weight: 1.5, opacity: 0.6, dashArray: '6 5',
          }).addTo(lineLayer).bindTooltip(
            'Stopped sharing on/around ' + it.date,
            { direction: 'top', sticky: true }
          );
          const xIcon = L.divIcon({
            html: '<div style="font-size:18px;font-weight:bold;color:#dc2626;text-shadow:0 0 2px #fff,0 0 2px #fff,0 0 2px #fff;line-height:1">\u2716</div>',
            className: '',
            iconSize: [20, 20],
            iconAnchor: [10, 10],
          });
          const tName = (agencyInfo[it.slug] || {}).name || it.name;
          L.marker(tCoord, { icon: xIcon, zIndexOffset: 500 })
            .addTo(lineLayer)
            .bindTooltip(
              escapeHtml(tName) + ' \u2014 sharing stopped on/around ' + it.date,
              { direction: 'top', offset: [0, -8], sticky: true }
            );
        }
      });
    }

    let inConnected = 0;
    let tempCount = 0;
    (m.inbound_slugs || []).forEach((source) => {
      if (coords[source]) {
        // Source has a map position — draw line
        L.polyline([coords[source], [m.lat, m.lng]], { color: '#16a34a', weight: 1.5, opacity: 0.3, dashArray: '4 4' }).addTo(lineLayer);
        inConnected++;
      } else {
        // No map position — create a temporary marker near the selected agency
        const angle = (tempCount * 2.399) + 0.5;  // golden angle spiral
        const dist = 0.008 + tempCount * 0.003;
        const tLat = m.lat + Math.cos(angle) * dist;
        const tLng = m.lng + Math.sin(angle) * dist;
        const info = agencyInfo[source] || {};
        const tipName = (info.name || source);
        const isComm = info.type === 'community' || info.type === 'other';
        const fillColor = isComm ? '#10b981' : '#8b5cf6';  // green for community, purple for unknown
        const tm = L.circleMarker([tLat, tLng], {
          radius: 4,
          fillColor: fillColor,
          color: '#065f46',
          weight: 1,
          fillOpacity: 0.7,
          slug: source,
        }).addTo(markerLayer);
        tm.bindTooltip(tipName + ' (community camera)', { direction: 'top', offset: [0, -6], sticky: true });
        tm.on('click', (e) => { L.DomEvent.stopPropagation(e); window.clickSlug(source); });
        // Draw dashed line from temp marker to agency
        L.polyline([[tLat, tLng], [m.lat, m.lng]], { color: '#10b981', weight: 1, opacity: 0.4, dashArray: '3 3' }).addTo(lineLayer);
        tempMarkers.push(tm);
        tempCount++;
        inConnected++;
      }
    });

    // Show flag summary as a fixed banner at top of map
    const bannerEl = document.getElementById('flag-banner');
    const myIndirectCount = (indirectFlags[m.slug] || []).length;
    const totalFlags = outFlags + myIndirectCount;
    if (totalFlags > 0) {
      let bannerText = '\u26a0 ' + outFlags + ' direct flag' + (outFlags !== 1 ? 's' : '');
      if (myIndirectCount > 0) {
        bannerText += ' + ' + myIndirectCount + ' indirect (via intermediaries)';
      }
      if (outNotMapped > 0) {
        bannerText += ' \u2014 ' + outNotMapped + ' not on map';
      }
      bannerEl.textContent = bannerText;
      bannerEl.style.display = 'block';
    } else {
      bannerEl.style.display = 'none';
    }

    const info = document.getElementById('info');
    const mCrawlInfo = agencyInfo[m.slug] || {};
    const crawlDate = mCrawlInfo.crawled_date;
    const status = m.crawled ? ('Crawled' + (crawlDate ? ' ' + crawlDate : '')) : 'No transparency page found (inferred from other portals)';
    const statusColor = m.crawled ? '#16a34a' : '#f97316';
    const shareUrl = window.location.href.split('#')[0] + '#' + m.slug;
    const mInfoForBanner = agencyInfo[m.slug] || {};
    let html = (typeof window.renderMeetingBannerHtml === 'function')
      ? window.renderMeetingBannerHtml([m.agency_id, m.slug, mInfoForBanner.name])
      : '';
    html += '<h3>' + escapeHtml(agencyInfo[m.slug]?.name || m.slug) + ' <a href="' + escapeHtml(shareUrl) + '" data-share-url="' + escapeHtml(shareUrl) + '" style="font-size:14px;text-decoration:none" title="Copy link">\ud83d\udd17</a></h3>';
    html += '<p class="stat"><a href="report.html?agency=' + encodeURIComponent(m.slug) + '" style="color:#2563eb;font-weight:600">View full report \u2192</a>';
    if (m.crawled) {
      html += ' &middot; <a href="https://transparency.flocksafety.com/' + safeSlug(m.slug) + '" target="_blank" style="color:#2563eb">Transparency portal \u2197</a>';
    }
    html += '</p>';
    html += '<p class="stat" style="color:' + statusColor + '">' + status + '</p>';
    if (m.cameras) html += '<p class="stat">Cameras: ' + m.cameras + '</p>';
    if (m.retention_days) html += '<p class="stat">Retention: ' + m.retention_days + ' days</p>';
    if (m.crawled) {
      html += '<p class="stat">Shares with: ' + m.outbound_count + ' entities</p>';
    } else {
      html += '<p class="stat">Shares with: unknown (no portal data)</p>';
    }
    const inCount = m.inbound_count || (m.inbound_slugs ? m.inbound_slugs.length : 0);
    if (inCount > 0) {
      html += '<p class="stat">Receives from: \u2265' + inCount + ' entities</p>';
    }
    const mInfo = agencyInfo[m.slug] || {};
    if (mInfo.notes) html += '<p class="stat" style="background:#fef3c7;padding:6px 8px;border-radius:4px;color:#92400e;margin-top:6px">' + mInfo.notes + '</p>';

    const inferredOut = new Set(m.inferred_outbound || []);
    const inferredIn = new Set(m.inferred_inbound || []);
    const inferTag = ' <span style="color:#6b7280;font-size:11px;font-style:italic" title="Not on this agency\'s portal — inferred from the other agency\'s portal">[inferred]</span>';

    if (m.outbound_slugs && m.outbound_slugs.length) {
      const sorted = sortOutbound(m.outbound_slugs, m.lat, m.lng);
      const directFlagged = sorted.filter(s => isFlagged(s));
      const clean = sorted.filter(s => !isFlagged(s));
      const myIndirects = indirectFlags[m.slug] || [];

      // Direct flags first
      if (directFlagged.length) {
        html += '<div class="sharing-list"><strong style="color:#dc2626">\u26a0 Direct flags (' + directFlagged.length + '):</strong>';
        directFlagged.forEach(function(s) {
          html += '<div style="cursor:pointer" data-slug="' + escapeHtml(s) + '">' + slugLabel(s) + (inferredOut.has(s) ? inferTag : '') + '</div>';
        });
        html += '</div>';
      }

      // Indirect flags — collapsed by default. For hub-adjacent agencies the
      // list runs into the hundreds and dominates the panel; the count in the
      // summary is the useful signal, the full list is on-demand detail.
      if (myIndirects.length) {
        html += '<details class="sharing-list" style="border-top:1px solid #fecaca;padding-top:6px">';
        html += '<summary style="cursor:pointer;color:#dc2626;font-weight:bold">\u26a0 Indirect flags (' + myIndirects.length + ')</summary>';
        html += '<p class="stat" style="font-size:11px;color:#92400e;margin:2px 0 4px 0">Data reaches these entities through intermediaries.</p>';
        myIndirects.forEach(function(iv) {
          // Inner span's [data-slug] wins via .closest() — clicking the
          // "via X" link navigates to X, not to the flagged recipient.
          html += '<div style="cursor:pointer;padding:2px 0" data-slug="' + escapeHtml(iv.flagged) + '">';
          html += slugLabel(iv.flagged);
          html += ' <span style="color:#6b7280;font-size:11px">via </span>';
          html += '<span style="cursor:pointer;color:#2563eb;font-size:11px" data-slug="' + escapeHtml(iv.via) + '">' + escapeHtml(iv.via_name) + '</span>';
          html += '</div>';
        });
        html += '</details>';
      }

      // Clean agencies
      if (clean.length) {
        html += '<div class="sharing-list" style="border-top:1px solid #e5e7eb;padding-top:6px"><strong>Shares with (' + clean.length + '):</strong>';
        clean.forEach(function(s) {
          html += '<div style="cursor:pointer" data-slug="' + escapeHtml(s) + '">' + slugLabel(s) + (inferredOut.has(s) ? inferTag : '') + '</div>';
        });
        html += '</div>';
      }
    }

    if (m.inbound_slugs && m.inbound_slugs.length) {
      html += '<div class="sharing-list" style="border-top:1px solid #e5e7eb;padding-top:6px"><strong>Receives from (inbound):</strong>';
      m.inbound_slugs.forEach(function(s) {
        const sInfo = agencyInfo[s] || {};
        const sName = escapeHtml(sInfo.name || s);
        html += '<div style="cursor:pointer" data-slug="' + escapeHtml(s) + '">' + sName + (inferredIn.has(s) ? inferTag : '') + '</div>';
      });
      html += '</div>';
    }

    info.innerHTML = html;

    const connected = new Set();
    connected.add(m.slug);
    (m.outbound_slugs || []).forEach(s => connected.add(s));
    (m.inbound_slugs || []).forEach(s => connected.add(s));
    const myMismatches = new Set(mismatches[m.slug] || []);

    // Remove unrelated markers from cluster group, keep connected ones
    markerLayer.clearLayers();
    markers.forEach(mm => {
      const c = markersBySlug[mm.slug];
      if (!c) return;
      if (!connected.has(mm.slug)) return;  // skip unrelated

      if (mm.slug === m.slug) {
        c.setRadius(14);
        c.setStyle({ fillColor: '#06b6d4', fillOpacity: 1, weight: 3, color: '#0e7490' });
      } else if (myMismatches.has(mm.slug)) {
        c.setRadius(Math.max(6, defaultRadius(mm)));
        c.setStyle({ fillColor: '#f97316', fillOpacity: 0.9, weight: 2, color: '#c2410c' });
      } else {
        const col = defaultColor(mm);
        c.setRadius(defaultRadius(mm));
        c.setStyle({ fillColor: col.fill, fillOpacity: 0.8, weight: 1, color: col.border });
      }
      markerLayer.addLayer(c);
    });

    // Center on the visible area (left of info panel)
    // Info panel is ~370px on the right, so the usable map width is smaller
    const panelWidth = 370;
    const mapWidth = map.getSize().x;
    const usableCenter = (mapWidth - panelWidth) / 2;
    const currentCenter = mapWidth / 2;
    const shiftRight = currentCenter - usableCenter;  // shift right in pixels
    const targetPoint = map.project([m.lat, m.lng], 6).add([shiftRight, 0]);
    const targetLatLng = map.unproject(targetPoint, 6);
    map.setView(targetLatLng, 6);
  }

  // Pre-compute marker data lookup and recipient set
  const markerDataBySlug = {};
  markers.forEach(m => { markerDataBySlug[m.slug] = m; });

  const recipientSlugs = new Set();
  markers.forEach(mm => {
    (mm.outbound_slugs || []).forEach(t => recipientSlugs.add(t));
  });

  function shouldShowByDefault(slug) {
    const md = markerDataBySlug[slug];
    if (md && md.visible !== undefined) return md.visible;
    // Fallback for off-map entities added dynamically
    const info = agencyInfo[slug] || {};
    return info.public !== false || recipientSlugs.has(slug);
  }

  function resetMarkers() {
    markerLayer.clearLayers();
    markers.forEach(mm => {
      const c = markersBySlug[mm.slug];
      if (!c) return;
      if (!shouldShowByDefault(mm.slug)) return;
      const col = defaultColor(mm);
      const radius = isFlagged(mm.slug) ? Math.max(5, defaultRadius(mm)) : defaultRadius(mm);
      c.setRadius(radius);
      c.setStyle({ fillColor: col.fill, fillOpacity: col.opacity, weight: isFlagged(mm.slug) ? 2 : 1, color: col.border });
      markerLayer.addLayer(c);
    });
  }

  // Initial marker population
  resetMarkers();

  // Click map background to reset
  map.on('click', () => {
    history.replaceState(null, '', window.location.pathname);
    lineLayer.clearLayers();
    clearTempMarkers();
    resetMarkers();
    document.getElementById('flag-banner').style.display = 'none';
    document.getElementById('info').innerHTML =
      '<h3>Flock ALPR Sharing Map</h3>' +
      '<p class="stat">Click an agency to see its sharing web.</p>' +
      '<p class="stat">MARKER_COUNT agencies mapped.</p>';
  });

  // Edge indicators for off-screen markers
  function updateEdgeIndicators() {
    const bounds = map.getBounds();
    let left = 0, right = 0, top = 0, bottom = 0;
    let leftViol = false, rightViol = false, topViol = false, bottomViol = false;

    markers.forEach(m => {
      if (bounds.contains([m.lat, m.lng])) return;
      const viol = isFlagged(m.slug);
      if (m.lng < bounds.getWest()) { left++; if (viol) leftViol = true; }
      if (m.lng > bounds.getEast()) { right++; if (viol) rightViol = true; }
      if (m.lat > bounds.getNorth()) { top++; if (viol) topViol = true; }
      if (m.lat < bounds.getSouth()) { bottom++; if (viol) bottomViol = true; }
    });

    function show(id, count, hasViol, arrow) {
      const el = document.getElementById(id);
      if (count > 0) {
        el.textContent = arrow + ' ' + count;
        el.className = 'edge-indicator' + (hasViol ? ' has-flag' : '');
        el.style.display = '';
      } else {
        el.style.display = 'none';
      }
    }
    show('edge-left', left, leftViol, '\u2190');
    show('edge-right', right, rightViol, '\u2192');
    show('edge-top', top, topViol, '\u2191');
    show('edge-bottom', bottom, bottomViol, '\u2193');
  }
  map.on('moveend', updateEdgeIndicators);
  map.on('zoomend', updateEdgeIndicators);
  updateEdgeIndicators();

  // Click edge indicators to pan towards off-screen markers
  ['edge-left', 'edge-right', 'edge-top', 'edge-bottom'].forEach(id => {
    document.getElementById(id).addEventListener('click', () => {
      const bounds = map.getBounds();
      const center = map.getCenter();
      const dx = (bounds.getEast() - bounds.getWest()) * 0.4;
      const dy = (bounds.getNorth() - bounds.getSouth()) * 0.4;
      if (id === 'edge-left') map.panTo([center.lat, center.lng - dx]);
      if (id === 'edge-right') map.panTo([center.lat, center.lng + dx]);
      if (id === 'edge-top') map.panTo([center.lat + dy, center.lng]);
      if (id === 'edge-bottom') map.panTo([center.lat - dy, center.lng]);
    });
  });

  // Navigate to slug from info panel

  window.clickSlug = function(slug) {
    // Update URL hash for shareable links
    history.replaceState(null, '', '#' + slug);

    const m = markerDataBySlug[slug];
    if (m) {
      showAgency(m);
    } else {
      const info = agencyInfo[slug] || {};
      const panel = document.getElementById('info');
      let html = '<h3>' + escapeHtml(info.name || slug) + '</h3>';
      html += '<p class="stat" style="color:#f97316">No map location</p>';
      html += '<p class="stat"><a href="report.html?agency=' + encodeURIComponent(slug) + '" style="color:#2563eb;font-weight:600">View full report \u2192</a>';
      if (info.crawled) {
        html += ' &middot; <a href="https://transparency.flocksafety.com/' + safeSlug(slug) + '" target="_blank" style="color:#2563eb">Transparency portal \u2197</a>';
      }
      html += '</p>';
      if (info.state) html += '<p class="stat">State: ' + escapeHtml(info.state) + '</p>';
      if (info.role) html += '<p class="stat">Role: ' + escapeHtml(info.role) + '</p>';
      if (info.type) html += '<p class="stat">Type: ' + escapeHtml(info.type) + '</p>';
      if (info.type === 'federal') html += '<p class="stat" style="color:#dc2626">Federal entity \u2014 not an \u201cagency of the state\u201d per \u00a71798.90.5(f). AG Bulletin prohibits sharing with federal agencies.</p>';
      else if (info.type === 'fusion_center') html += '<p class="stat" style="color:#dc2626">Re-sharing hub \u2014 data redistributed to many downstream entities; may not qualify as a \u201cpublic agency\u201d under \u00a71798.90.5(f). See notes below.</p>';
      else if (info.type === 'test') html += '<p class="stat" style="color:#dc2626">Test/fixture account \u2014 access controls unknown; no agency of record accountable for queries.</p>';
      else if (info.type === 'decommissioned') html += '<p class="stat" style="color:#dc2626">Inactive / decommissioned \u2014 no current custodian of record; who still holds credentials is unknown.</p>';
      else if (info.type === 'private') {
        const nm = (info.name || '').toLowerCase();
        if (nm.indexOf('university') >= 0 || nm.indexOf('college') >= 0)
          html += '<p class="stat" style="color:#dc2626">Private university PD \u2014 qualification as a \u201cpublic agency\u201d under \u00a71798.90.5(f) is contested.</p>';
        else
          html += '<p class="stat" style="color:#dc2626">Private entity \u2014 not a \u201cpublic agency\u201d under \u00a71798.90.5(f). Sharing likely violates \u00a71798.90.55(b).</p>';
      }
      else if (info.public === true) html += '<p class="stat" style="color:#16a34a">Public agency</p>';
      else if (info.public === false) html += '<p class="stat" style="color:#dc2626">Not a \u201cpublic agency\u201d under \u00a71798.90.5(f). Sharing likely violates \u00a71798.90.55(b).</p>';
      if (info.notes) html += '<p class="stat" style="background:#fef3c7;padding:6px 8px;border-radius:4px;color:#92400e;margin-top:6px">' + info.notes + '</p>';

      const sharedBy = markers.filter(mm => (mm.outbound_slugs || []).includes(slug));
      if (sharedBy.length) {
        html += '<div class="sharing-list"><strong>Receives data from (' + sharedBy.length + '):</strong>';
        sharedBy.forEach(function(mm) {
          const sInfo = agencyInfo[mm.slug] || {};
          const sName = escapeHtml(sInfo.name || mm.slug);
          html += '<div style="cursor:pointer" data-slug="' + escapeHtml(mm.slug) + '">' + sName + '</div>';
        });
        html += '</div>';
      }
      panel.innerHTML = html;
    }
  };

  // Delegated click handler on the #info agency panel. Covers:
  //   - [data-share-url] on the copy-link glyph (prevents navigation,
  //     copies URL to clipboard, flashes "copied!")
  //   - [data-slug] on outbound / inbound / indirect-flag rows
  //     (.closest() picks the innermost match, so clicking the "via X"
  //     sub-span routes to X rather than bubbling to the parent slug)
  // One listener replaces many per-element inline handlers so the
  // page can drop 'unsafe-inline' from script-src.
  const infoPanel = document.getElementById('info');
  infoPanel.addEventListener('click', (e) => {
    const shareEl = e.target.closest('[data-share-url]');
    if (shareEl) {
      e.preventDefault();
      navigator.clipboard.writeText(shareEl.dataset.shareUrl).then(() => {
        shareEl.textContent = 'copied!';
        setTimeout(() => { shareEl.textContent = '\ud83d\udd17'; }, 1500);
      });
      return;
    }
    const slugEl = e.target.closest('[data-slug]');
    if (slugEl) {
      clickSlug(slugEl.dataset.slug);
    }
  });

  // Place off-map flagged entities as markers in the ocean west of California
  document.getElementById('offmap').style.display = 'none';  // hide the old panel

  // Only show entities that RECEIVE data from CA agencies but have no map location
  // (test accounts, decommissioned entries, etc.) — not out-of-state sources
  const offmapEntities = Object.entries(agencyInfo).filter(([slug]) => {
    if (!isFlagged(slug)) return false;
    if (coords[slug]) return false;
    // Must be a recipient — some agency shares outbound to this entity
    return recipientSlugs.has(slug);
  }).sort((a, b) => sortPriority(a[1]) - sortPriority(b[1]));

  if (offmapEntities.length) {
    // Place all off-map flagged entities at a single point in the ocean.
    // MarkerCluster will group them into one clickable cluster.
    const offmapLat = 37.5;
    const offmapLng = -126.0;

    offmapEntities.forEach(([slug]) => {
      const info = agencyInfo[slug] || {};
      let color = '#dc2626';
      if (info.type === 'test' || info.type === 'decommissioned') color = '#f97316';

      const circle = L.circleMarker([offmapLat, offmapLng], {
        radius: 5,
        fillColor: color,
        color: color,
        weight: 1,
        fillOpacity: 0.8,
        slug: slug,
      });
      circle.bindTooltip(slugLabel(slug), { direction: 'right', offset: [8, 0], sticky: true });
      circle.on('click', (e) => { L.DomEvent.stopPropagation(e); window.clickSlug(slug); });
      // Find who shares with this entity
      const inboundSlugs = markers.filter(mm => (mm.outbound_slugs || []).includes(slug)).map(mm => mm.slug);
      const offmapMarkerData = { slug: slug, lat: offmapLat, lng: offmapLng, crawled: false, cameras: 0, outbound_slugs: [], inbound_slugs: inboundSlugs, outbound_count: 0, inbound_count: inboundSlugs.length };
      markers.push(offmapMarkerData);
      markersBySlug[slug] = circle;
      markerLayer.addLayer(circle);
    });
  }

  // ── Search ──
  const searchInput = document.getElementById('search-input');
  const searchResults = document.getElementById('search-results');

  // Build searchable index: name -> slug, with common aliases
  const searchIndex = [];
  markers.forEach(m => {
    const info = agencyInfo[m.slug] || {};
    const name = info.name || m.slug;
    searchIndex.push({ name: name, slug: m.slug, lat: m.lat, lng: m.lng });
    // Add slug as searchable too (e.g. "san-mateo-ca-pd")
    if (m.slug !== name) {
      searchIndex.push({ name: m.slug.replace(/-/g, ' '), slug: m.slug, lat: m.lat, lng: m.lng, alias: true });
    }
  });
  // Also include unmapped agencies from agencyInfo
  Object.entries(agencyInfo).forEach(([slug, info]) => {
    if (!markerDataBySlug[slug]) {
      searchIndex.push({ name: info.name || slug, slug: slug, lat: null, lng: null });
    }
  });

  function doSearch(query) {
    const q = query.trim().toLowerCase();
    if (!q) { searchResults.style.display = 'none'; return; }

    // Check if it looks like a zip code (5 digits)
    if (/^\d{5}$/.test(q)) {
      searchResults.innerHTML = '<div data-zip="' + escapeHtml(q) + '">Zoom to zip code <strong>' + escapeHtml(q) + '</strong></div>';
      searchResults.style.display = 'block';
      return;
    }

    const matches = [];
    searchIndex.forEach(entry => {
      const name = entry.name.toLowerCase();
      const score = name === q ? 100 : name.startsWith(q) ? 50 : name.includes(q) ? 10 : 0;
      if (score > 0 && !entry.alias) matches.push({ ...entry, score });
      else if (score > 0 && entry.alias) matches.push({ ...entry, score: score - 1 });
    });
    // Deduplicate by slug, keep highest score
    const seen = {};
    matches.forEach(m => { if (!seen[m.slug] || m.score > seen[m.slug].score) seen[m.slug] = m; });
    const sorted = Object.values(seen).sort((a, b) => b.score - a.score).slice(0, 15);

    if (sorted.length === 0) {
      // Also offer zip/location search
      searchResults.innerHTML = '<div style="color:#6b7280;cursor:default">No matching agencies</div>';
      searchResults.style.display = 'block';
      return;
    }

    let html = '';
    sorted.forEach(m => {
      const info = agencyInfo[m.slug] || {};
      let tag = '';
      if (info.public === false) tag = ' <span class="sr-tag">[private]</span>';
      else if (info.type === 'federal') tag = ' <span class="sr-tag">[federal]</span>';
      else if (info.type === 'fusion_center') tag = ' <span class="sr-tag">[fusion center]</span>';
      else if (!m.lat) tag = ' <span class="sr-tag">[not mapped]</span>';
      html += '<div data-slug="' + escapeHtml(m.slug) + '">' + escapeHtml(info.name || m.slug) + tag + '</div>';
    });
    searchResults.innerHTML = html;
    searchResults.style.display = 'block';
  }

  // Delegated click handler on #search-results: one listener covers
  // both the agency-suggestion rows ([data-slug]) and the "Zoom to
  // zip code" row ([data-zip]). The per-render listener loop it
  // replaces was redundant — the container is stable.
  searchResults.addEventListener('click', (e) => {
    const zipEl = e.target.closest('[data-zip]');
    if (zipEl) {
      window.searchZip(zipEl.dataset.zip);
      return;
    }
    const slugEl = e.target.closest('[data-slug]');
    if (slugEl) {
      searchResults.style.display = 'none';
      searchInput.value = '';
      clickSlug(slugEl.dataset.slug);
    }
  });

  window.searchZip = function(zip) {
    searchResults.innerHTML = '<div style="color:#6b7280;cursor:default">Looking up zip code...</div>';
    fetch('https://nominatim.openstreetmap.org/search?postalcode=' + encodeURIComponent(zip) + '&country=US&format=json&limit=1', {
      headers: { 'Accept': 'application/json' }
    })
    .then(r => r.json())
    .then(results => {
      if (results.length) {
        const lat = parseFloat(results[0].lat);
        const lng = parseFloat(results[0].lon);
        map.setView([lat, lng], 12);
        searchResults.style.display = 'none';
        searchInput.value = '';
      } else {
        searchResults.innerHTML = '<div style="color:#dc2626;cursor:default">Zip code not found</div>';
      }
    })
    .catch(() => {
      searchResults.innerHTML = '<div style="color:#dc2626;cursor:default">Lookup failed</div>';
    });
  };

  searchInput.addEventListener('input', () => doSearch(searchInput.value));
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const q = searchInput.value.trim();
      if (/^\d{5}$/.test(q)) { window.searchZip(q); return; }
      // Select first result
      const first = searchResults.querySelector('[data-slug]');
      if (first) { first.click(); }
    }
    if (e.key === 'Escape') {
      searchResults.style.display = 'none';
      searchInput.blur();
    }
  });

  // Close search results when clicking elsewhere
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#search-box') && !e.target.closest('#search-results')) {
      searchResults.style.display = 'none';
    }
  });

  // Change-overlay toggle in the legend
  const legendEl = document.querySelector('.legend');
  if (legendEl) {
    const divider = document.createElement('div');
    divider.style.cssText = 'border-top:1px solid #e5e7eb;margin:6px 0 4px';
    legendEl.appendChild(divider);

    const addedItem = document.createElement('div');
    addedItem.className = 'legend-item';
    addedItem.innerHTML = '<div style="width:20px;height:3px;background:#16a34a"></div> Sharing started (last 90d)';
    legendEl.appendChild(addedItem);

    const removedItem = document.createElement('div');
    removedItem.className = 'legend-item';
    removedItem.innerHTML = '<div style="width:20px;height:2px;background:repeating-linear-gradient(90deg,#9ca3af 0,#9ca3af 4px,transparent 4px,transparent 7px)"></div> <span style="color:#dc2626;font-weight:bold;margin:0 2px">\u2716</span> Sharing stopped (last 90d)';
    legendEl.appendChild(removedItem);

    const toggleWrap = document.createElement('label');
    toggleWrap.className = 'legend-item';
    toggleWrap.style.cssText = 'cursor:pointer;user-select:none';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = showChanges;
    cb.style.cssText = 'margin:0 6px 0 0';
    toggleWrap.appendChild(cb);
    toggleWrap.appendChild(document.createTextNode('Show recent changes'));
    cb.addEventListener('change', () => {
      showChanges = cb.checked;
      localStorage.setItem('smalpr-show-changes', String(showChanges));
      if (currentSelectionSlug) {
        const md = markerDataBySlug[currentSelectionSlug];
        if (md) showAgency(md);
      }
    });
    legendEl.appendChild(toggleWrap);

    if (!changelogMeta.window_complete && changelogMeta.tracking_days != null) {
      const note = document.createElement('div');
      note.style.cssText = 'font-size:10px;color:#6b7280;margin-top:2px;font-style:italic;line-height:1.3';
      note.textContent = 'Only ' + changelogMeta.tracking_days + ' days tracked so far — window will grow to 90.';
      legendEl.appendChild(note);
    }
  }

  // Auto-select agency from URL hash (e.g. #san-mateo-ca-pd)
  const hashSlug = decodeURIComponent(window.location.hash.replace('#', ''));
  if (hashSlug && safeSlug(hashSlug)) {
    setTimeout(() => clickSlug(hashSlug), 100);
  }
});
