// Shared Leaflet helpers for sm-alpr maps (sharing_map.html, articles.html, …).
// Loaded before per-page JS; exposes window.MapCommon.

window.MapCommon = (function () {
  // CARTO began watermarking keyless basemap tiles ("API KEY REQUIRED"
  // struck diagonally across every tile) in September 2026. The free tier
  // covers this project — 5M tile requests/month, no card, non-commercial
  // and research use — so the fix is just to send a key.
  //
  // The key is public on purpose: it ships in a static site's JS, so there
  // is nowhere to hide it. What makes that safe is the referrer restriction
  // set on it in the CARTO dashboard — a request whose Referer is not an
  // allowed origin gets 403, so a copied key is useless against our quota.
  // Manage restrictions at dashboard.basemaps.carto.com.
  //
  // Keep the attribution below visible — required by CARTO's basemap terms.
  var CARTO_KEY = 'cb1_3hyb_1_cb36f4622b90f341bca25d25';

  // Only send the key from origins it is actually authorized for. Sending it
  // anywhere else — a dev server on localhost, the CI screenshot harness on
  // 127.0.0.1 — returns 403 and leaves the map with NO basemap at all, which
  // is worse than the watermark. A KEYLESS request still returns real tiles
  // (200, just watermarked), so omitting the key off-production degrades to
  // "ugly but working" instead of "blank".
  //
  // Allowlist, not a localhost denylist, on purpose: an unknown origin (a
  // fork's Pages site, a mirror) should land in the watermarked-but-working
  // case too. If the site moves to a new domain, add it to the CARTO key
  // restrictions FIRST, then add it here.
  var CARTO_KEYED_HOSTS = ['none-below.github.io'];

  // Single source of truth for the tile URL. contracts.js builds its own
  // L.tileLayer (different subdomains/maxZoom) but must not carry a second
  // copy of this logic — one page silently reverting to watermarked tiles
  // in production is exactly the kind of drift that goes unnoticed.
  function cartoTileUrl(theme) {
    var url = 'https://{s}.basemaps.cartocdn.com/'
      + (theme === 'dark' ? 'dark_all' : 'light_all')
      + '/{z}/{x}/{y}@2x.png';
    var host = (window.location && window.location.hostname) || '';
    if (CARTO_KEYED_HOSTS.indexOf(host) !== -1) {
      url += '?key=' + CARTO_KEY;
    }
    return url;
  }

  function createCartoMap(elementId, opts) {
    opts = opts || {};
    var center = opts.center || [37.5, -121.5];
    var zoom = opts.zoom != null ? opts.zoom : 7;
    var map = L.map(elementId, opts.mapOptions || {}).setView(center, zoom);
    L.tileLayer(
      cartoTileUrl(opts.theme),
      {
        attribution: '&copy; OpenStreetMap, &copy; CARTO',
        maxZoom: 18
      }
    ).addTo(map);
    return map;
  }

  function clusterOptions(extra) {
    var base = {
      maxClusterRadius: 50,
      spiderfyOnMaxZoom: true,
      showCoverageOnHover: false,
      zoomToBoundsOnClick: true
    };
    if (extra) {
      for (var k in extra) base[k] = extra[k];
    }
    return base;
  }

  // Wire mouseover tooltips that show member names (or just the count when
  // the cluster has more than `overflowAt` members).
  function attachClusterTooltips(clusterLayer, getNameFn, opts) {
    opts = opts || {};
    var overflowAt = opts.overflowAt || 15;
    var label = opts.overflowLabel || 'agencies';
    clusterLayer.on('clustermouseover', function (e) {
      var children = e.layer.getAllChildMarkers();
      if (children.length > overflowAt) {
        e.layer.bindTooltip(children.length + ' ' + label).openTooltip();
        return;
      }
      var names = children.map(getNameFn).filter(Boolean).sort();
      e.layer.bindTooltip(names.join('<br>'), { direction: 'top' }).openTooltip();
    });
    clusterLayer.on('clustermouseout', function (e) {
      e.layer.unbindTooltip();
    });
  }

  // Simple "blue circle with member count" cluster icon.
  function countClusterIcon(opts) {
    opts = opts || {};
    var fill = opts.fill || '#60a5fa';
    var border = opts.border || '#1e3a8a';
    var textColor = opts.textColor || '#0f172a';
    return function (cluster) {
      var children = cluster.getAllChildMarkers();
      var size = Math.min(44, 22 + children.length * 2);
      var r = size / 2;
      var svg = '<svg width="' + size + '" height="' + size
        + '" xmlns="http://www.w3.org/2000/svg">'
        + '<circle cx="' + r + '" cy="' + r + '" r="' + (r - 1)
          + '" fill="' + fill + '" fill-opacity="0.85" stroke="'
          + border + '" stroke-width="1"/>'
        + '<text x="' + r + '" y="' + (r + 4)
          + '" text-anchor="middle" font-size="11" font-weight="bold" fill="'
          + textColor + '">'
          + children.length + '</text>'
        + '</svg>';
      return L.divIcon({ html: svg, className: '', iconSize: [size, size] });
    };
  }

  return {
    cartoTileUrl: cartoTileUrl,
    createCartoMap: createCartoMap,
    clusterOptions: clusterOptions,
    attachClusterTooltips: attachClusterTooltips,
    countClusterIcon: countClusterIcon
  };
})();
