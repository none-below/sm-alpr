// Shared Leaflet helpers for sm-alpr maps (sharing_map.html, articles.html, …).
// Loaded before per-page JS; exposes window.MapCommon.

window.MapCommon = (function () {
  function createCartoMap(elementId, opts) {
    opts = opts || {};
    var center = opts.center || [37.5, -121.5];
    var zoom = opts.zoom != null ? opts.zoom : 7;
    var theme = opts.theme === 'dark' ? 'dark_all' : 'light_all';
    var map = L.map(elementId, opts.mapOptions || {}).setView(center, zoom);
    L.tileLayer(
      'https://{s}.basemaps.cartocdn.com/' + theme + '/{z}/{x}/{y}@2x.png',
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
    createCartoMap: createCartoMap,
    clusterOptions: clusterOptions,
    attachClusterTooltips: attachClusterTooltips,
    countClusterIcon: countClusterIcon
  };
})();
