/* Renders per-agency search-justification word clouds from the data
   produced by scripts/build_justifications.py.

   The "cloud" here is the simple flow-layout variant — inline-block
   words sized by frequency, wrapping into a paragraph. A spiral-packed
   layout (d3-cloud style) was considered but adds a layout dependency
   and obscures the count alongside each word. */

(function () {
  var DATA_URL = 'data/justifications.json';
  var DEFAULT_SLUG = 'east-palo-alto-ca-pd';

  // Font-size scale for the word cloud, in points. The smallest word
  // (count == MIN_COUNT, currently 3) renders at MIN_PT; the most
  // frequent renders at MAX_PT, with a square-root scale in between
  // so a single dominant token doesn't squash the rest.
  var MIN_PT = 9;
  var MAX_PT = 36;

  function fmt(n) { return n == null ? '—' : n.toLocaleString(); }
  function pct(n) { return n == null ? '—' : n.toFixed(1) + '%'; }

  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function scaleFontSize(count, max) {
    if (max <= 0) return MIN_PT;
    var t = Math.sqrt(count / max);
    return MIN_PT + (MAX_PT - MIN_PT) * t;
  }

  // A phrase is a "code" if it's a bare or section-suffixed penal /
  // vehicle code reference (e.g. "10851", "245(a)(2)pc", "23103cvc").
  // Those render in monospace with a distinct color so the eye can
  // separate prose justifications from code-shaped ones at a glance.
  function isCodeToken(s) {
    return /^[0-9]+(?:\.[0-9]+)?(?:\([^)]*\))*\s?(?:pc|cvc|hsc|wic)?$/.test(s);
  }

  // 6-color palette in CSS; pick a stable index by hashing the phrase
  // string so the same phrase keeps its color across reloads while
  // adjacent (count-sorted) entries land on different colors.
  function colorClass(s) {
    var h = 0;
    for (var i = 0; i < s.length; i++) {
      h = ((h << 5) - h + s.charCodeAt(i)) | 0;
    }
    return 'c' + (Math.abs(h) % 6);
  }

  // Long-active case-number justifications: a single case number used
  // as the entire reason across many searches over many days. Renders
  // as a callout above the cloud when present. The presence of this
  // pattern is what audit review is meant to surface — case numbers
  // identify a single incident, so reuse across weeks raises questions
  // about scope creep, hotlist-style use, or stale flagging.
  function renderLongActiveCases(items, displayName) {
    if (!items || !items.length) return '';
    var html = '<div class="long-active-block">' +
      '<div class="long-active-head">Long-active case-number justifications</div>' +
      '<div class="long-active-sub">Single case numbers reused across many searches and days. ' +
      'A case number identifies one incident, so this pattern is what ALPR audit review ' +
      'is supposed to surface for proportionality review.</div>' +
      '<table class="long-active-table"><thead><tr>' +
      '<th>Case number</th><th class="num">Searches</th>' +
      '<th>Span</th>' +
      '<th class="num">Active days</th>' +
      '<th class="num">Median reach</th>' +
      '<th>Flags</th>' +
      '</tr></thead><tbody>';
    for (var i = 0; i < items.length; i++) {
      var phrase = items[i][0];
      var count = items[i][1];
      var days = items[i][2];
      var dmin = items[i][3];
      var dmax = items[i][4];
      var nc = items[i][5];
      // Compute span here too — same date arithmetic as the build
      // does for phrase_details, but the long-active list is a flat
      // tuple so we recompute. Local-tz parsing not needed since we
      // only care about whole-day deltas.
      var span = '';
      if (dmin && dmax) {
        var fromMs = Date.parse(dmin + 'T12:00:00Z');
        var toMs = Date.parse(dmax + 'T12:00:00Z');
        if (!isNaN(fromMs) && !isNaN(toMs)) {
          var spanDays = Math.round((toMs - fromMs) / 86400000) + 1;
          span = '<strong>' + spanFraming(spanDays) + '</strong>' +
            '<div class="span-dates">' + escapeHTML(dmin) + ' &mdash; ' + escapeHTML(dmax) + '</div>';
        }
      }
      var ncCell = nc == null ? '<span class="muted">—</span>'
        : nc >= 100
          ? '<span class="reach-broad" title="Reach beyond direct sharing partners">' + nc.toLocaleString() + '</span>'
          : nc.toLocaleString();
      var phraseFlags = '';
      if (currentDetails && currentDetails[phrase]) {
        var fset = flagsForDetail(currentDetails[phrase], count, phrase);
        phraseFlags = renderFlags(fset);
      }
      html += '<tr class="bar-row case-row" data-phrase="' + escapeHTML(phrase) + '">' +
        '<td class="case">' + escapeHTML(phrase) +
        ' <span class="expand-hint" aria-hidden="true">&#9662;</span></td>' +
        '<td class="num">' + count.toLocaleString() + '</td>' +
        '<td>' + span + '</td>' +
        '<td class="num">' + days + '</td>' +
        '<td class="num">' + ncCell + '</td>' +
        '<td>' + phraseFlags + '</td>' +
        '</tr>';
    }
    html += '</tbody></table></div>';
    return html;
  }

  // The agency's published "Acceptable Use" / "Prohibited Uses" text,
  // pulled from the same Flock transparency portal that publishes the
  // search audit. Surfacing them above the cloud lets the reader judge
  // whether the cloud's free-text reasons fit the agency's own
  // declared policy. Both strings are short prose set by the agency.
  function renderUses(a) {
    var aup = a.acceptable_use_policy;
    var pu = a.prohibited_uses;
    if (!aup && !pu) return '';
    var portalUrl = 'https://transparency.flocksafety.com/' +
      encodeURIComponent(a.slug);
    var html = '<h2>Stated uses ' +
      '<a class="portal-link" href="' + portalUrl + '"' +
      ' target="_blank" rel="noopener">' +
      'View on Flock transparency portal &rarr;</a></h2>' +
      '<div class="uses-block">';
    if (aup) {
      html += '<div class="uses-row uses-acceptable">' +
        '<div class="uses-label">Acceptable use</div>' +
        '<div class="uses-text">' + escapeHTML(aup) + '</div>' +
        '</div>';
    }
    if (pu) {
      html += '<div class="uses-row uses-prohibited">' +
        '<div class="uses-label">Prohibited uses</div>' +
        '<div class="uses-text">' + escapeHTML(pu) + '</div>' +
        '</div>';
    }
    html += '</div>';
    return html;
  }

  function renderCloud(items) {
    if (!items.length) {
      return '<div class="empty">No tokens to display for this agency.</div>';
    }
    var max = items[0][1];
    var html = '<div class="cloud">';
    // Shuffle for visual variety — strict sort puts huge words at one
    // end of the line which reads as a list more than a cloud. Use a
    // deterministic shuffle so the layout is stable across reloads
    // (per-agency seed = sum of counts mod something).
    var order = items.slice();
    var seed = 0;
    for (var k = 0; k < items.length; k++) seed = (seed + items[k][1]) | 0;
    order.sort(function (a, b) {
      var ha = ((seed * 9301 + a[1] * 49297) >>> 0) % 233280;
      var hb = ((seed * 9301 + b[1] * 49297 + 1) >>> 0) % 233280;
      // Tie-break on count so dominant words still tend to anchor;
      // pure shuffle hides the signal in some agencies.
      return (b[1] - a[1]) * 0.4 + (ha - hb) * 0.0001;
    });
    for (var i = 0; i < order.length; i++) {
      var tok = order[i][0];
      var count = order[i][1];
      var size = scaleFontSize(count, max).toFixed(1);
      var cls = 'word ' + (isCodeToken(tok) ? 'code' : colorClass(tok));
      html += '<span class="' + cls + '" style="font-size:' + size + 'pt"' +
        ' data-phrase="' + escapeHTML(tok) + '"' +
        ' title="Click for detail &mdash; ' + escapeHTML(tok) + ': ' + count + '">' +
        escapeHTML(tok) +
        '<span class="count">' + count + '</span></span>';
    }
    html += '</div>';
    return html;
  }

  // Stable display order for filter pills so the row doesn't reflow
  // depending on which agency you're looking at — concern flags go
  // first (red), warn next (amber), info last (blue), with each
  // group sorted by a fixed key order.
  var FILTER_KIND_ORDER = [
    'long-lived', 'heavy-use', 'broad-reach', 'stale-case',
    'multi-officer', 'narrow-reach',
  ];

  function codeSlug(label) {
    return String(label).toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '');
  }

  // Functional crime categories — readable journalistic buckets with
  // an underlying NIBRS Group A correspondence noted in tooltips. Six
  // top-level groups; an "(uncoded)" pseudo-category catches phrases
  // with no detected penal/vehicle code.
  var CODE_CATEGORY = {
    // Violent (NIBRS 09A, 09B, 11A, 13A, 13B, 120, 100, 200)
    '187': 'violent', '192': 'violent', '211': 'violent', '215': 'violent',
    '220': 'violent', '240': 'violent', '242': 'violent', '243': 'violent',
    '245': 'violent', '246': 'violent', '261': 'violent', '273.5': 'violent',
    '288': 'violent', '207': 'violent', '417': 'violent', '422': 'violent',
    '451': 'violent', '452': 'violent', '314': 'violent',
    // Property (NIBRS 220, 23, 240, 250, 26, 290)
    '459': 'property', '460': 'property', '466': 'property',
    '470': 'property', '484': 'property', '487': 'property', '488': 'property',
    '490.4': 'property', '496': 'property', '496d': 'property',
    '530.5': 'property', '594': 'property', '10851': 'property',
    '1030': 'property',
    // Drug (NIBRS 35A, 35B)
    '11350': 'drug', '11351': 'drug', '11352': 'drug',
    '11377': 'drug', '11378': 'drug', '11379': 'drug',
    // Weapons (NIBRS 520)
    '25400': 'weapons', '25850': 'weapons', '29800': 'weapons',
    // Traffic / DUI (NIBRS 90D plus state-specific hit-run)
    '20001': 'traffic', '20002': 'traffic', '23103': 'traffic',
    '23152': 'traffic', '23153': 'traffic', '2800': 'traffic',
    '14601': 'traffic', '23101': 'traffic',
    // Other / Misc — civil-status, radio codes, juvenile justice
    '415': 'other', '5150': 'other', '300': 'other', '601': 'other',
    '602': 'other', '1065': 'other', '10-65': 'other',
  };

  var CATEGORY_INFO = {
    'violent':  { label: 'Violent',     order: 0, nibrs: 'NIBRS 09, 11A, 13, 100, 120, 200' },
    'property': { label: 'Property',    order: 1, nibrs: 'NIBRS 23, 240, 250, 26, 290 (and burglary 220)' },
    'drug':     { label: 'Drug',        order: 2, nibrs: 'NIBRS 35A, 35B' },
    'weapons':  { label: 'Weapons',     order: 3, nibrs: 'NIBRS 520' },
    'traffic':  { label: 'Traffic/DUI', order: 4, nibrs: 'NIBRS 90D plus state-specific hit-and-run' },
    'case-id':  { label: 'Case IDs',    order: 5, nibrs: 'Custom — phrases shaped like a YY-NNNNN case/report number' },
    'other':    { label: 'Other',       order: 6, nibrs: 'NIBRS Group B and uncategorized state codes' },
  };

  function categoriesForCodes(codes, phrase) {
    var out = [];
    var seen = {};
    if (codes && codes.length) {
      for (var i = 0; i < codes.length; i++) {
        var cat = CODE_CATEGORY[codes[i][0]];
        if (cat && !seen[cat]) {
          seen[cat] = true;
          out.push(cat);
        }
      }
    }
    // Case-number-shaped phrases get a synthetic "case-id" category
    // even when no penal code is detected. A phrase can land in both
    // a real-code category AND case-id (e.g. "26-13287/PC459") —
    // that's correct, it deserves both buckets.
    if (phrase && CASE_NUMBER_RE_JS.test(phrase) && !seen['case-id']) {
      out.push('case-id');
    }
    return out;
  }

  function renderFilterBar(items) {
    // Tally flag-kinds and code-labels across all bar items so the
    // filter UI can offer both dimensions. Code labels collapse
    // variant phrasings ("pc459" vs "459pc") under one filter — the
    // user noted these are the same thing.
    var byKind = {};
    var byCode = {};
    var byCat = {};
    for (var i = 0; i < items.length; i++) {
      var detail = items[i][3];
      var phrase = items[i][0];
      var count = items[i][1];
      var flags = flagsForDetail(detail, count, phrase);
      for (var j = 0; j < flags.length; j++) {
        var f = flags[j];
        if (!f.kind) continue;
        if (!byKind[f.kind]) {
          byKind[f.kind] = { count: 0, tone: f.tone };
        }
        byKind[f.kind].count += 1;
      }
      var codes = items[i][2] || [];
      for (var k = 0; k < codes.length; k++) {
        var label = codes[k][1];
        var slug = codeSlug(label);
        if (!byCode[slug]) byCode[slug] = { count: 0, label: label };
        byCode[slug].count += 1;
      }
      var cats = categoriesForCodes(codes, phrase);
      for (var ck = 0; ck < cats.length; ck++) {
        var cat = cats[ck];
        if (!byCat[cat]) byCat[cat] = { count: 0 };
        byCat[cat].count += 1;
      }
    }
    var kinds = Object.keys(byKind);
    var codes = Object.keys(byCode);
    var cats = Object.keys(byCat);
    if (!kinds.length && !codes.length && !cats.length) return '';

    kinds.sort(function (a, b) {
      var ai = FILTER_KIND_ORDER.indexOf(a);
      var bi = FILTER_KIND_ORDER.indexOf(b);
      if (ai === -1) ai = 99;
      if (bi === -1) bi = 99;
      return ai - bi;
    });
    codes.sort(function (a, b) { return byCode[b].count - byCode[a].count; });
    cats.sort(function (a, b) {
      return (CATEGORY_INFO[a].order || 99) - (CATEGORY_INFO[b].order || 99);
    });

    var html = '<div class="filter-bar">' +
      '<span class="filter-bar-label">Filter:</span> ' +
      '<button class="filter-pill filter-all active" data-filter-type="all">All</button>';
    for (var ki = 0; ki < kinds.length; ki++) {
      var kind = kinds[ki];
      var info = byKind[kind];
      html += '<button class="filter-pill flag-' + info.tone +
        '" data-filter-type="flag" data-filter-value="' + kind + '">' +
        escapeHTML(kind) + ' <span class="filter-count">' +
        info.count + '</span></button>';
    }
    if (cats.length) {
      html += '<span class="filter-divider" aria-hidden="true">·</span>';
      for (var ca = 0; ca < cats.length; ca++) {
        var catKey = cats[ca];
        var catInfo = CATEGORY_INFO[catKey] || { label: catKey, nibrs: '' };
        html += '<button class="filter-pill filter-category" ' +
          'data-filter-type="category" data-filter-value="' + catKey + '" ' +
          'title="Approximated from ' + catInfo.nibrs + '">' +
          escapeHTML(catInfo.label) +
          ' <span class="filter-count">' + byCat[catKey].count + '</span></button>';
      }
    }
    if (codes.length) {
      html += '<span class="filter-divider" aria-hidden="true">·</span>';
      for (var ci = 0; ci < codes.length; ci++) {
        var slug2 = codes[ci];
        var ci2 = byCode[slug2];
        html += '<button class="filter-pill filter-code' +
          '" data-filter-type="code" data-filter-value="' + slug2 + '">' +
          escapeHTML(ci2.label) +
          ' <span class="filter-count">' + ci2.count + '</span></button>';
      }
    }
    html += '</div>';
    return html;
  }

  // Aggregate items into a list of pseudo-rows keyed by code or
  // category. Each output row has the same shape as a phrase row
  // ([phrase, count, codes, detail]) so the renderer can stay generic.
  // Multi-code phrases contribute their full count to each constituent
  // code/category — the totals can exceed search count for that reason.
  function aggregateItems(items, mode) {
    if (mode === 'phrase') return items;
    var byKey = {};
    var totalUncoded = 0;
    var totalBlank = 0;
    for (var i = 0; i < items.length; i++) {
      var phrase = items[i][0];
      var count = items[i][1];
      var codes = items[i][2] || [];
      // (blank) gets its own bucket — it's a meaningfully different
      // category from "phrase had words but none were a known code"
      // (uncoded). Same treatment in both code and category modes.
      if (phrase === '(blank)') {
        totalBlank += count;
        continue;
      }
      if (mode === 'code') {
        if (!codes.length) {
          totalUncoded += count;
          continue;
        }
        for (var j = 0; j < codes.length; j++) {
          var label = codes[j][1];
          var k = codeSlug(label);
          if (!byKey[k]) {
            byKey[k] = {
              display: label, count: 0, codes: [codes[j]],
              drillType: 'code', drillValue: k,
            };
          }
          byKey[k].count += count;
        }
      } else if (mode === 'category') {
        var cats = categoriesForCodes(codes, phrase);
        if (!cats.length) {
          totalUncoded += count;
          continue;
        }
        for (var c = 0; c < cats.length; c++) {
          var cat = cats[c];
          var info = CATEGORY_INFO[cat] || { label: cat };
          if (!byKey[cat]) {
            byKey[cat] = {
              display: info.label, count: 0, codes: [],
              drillType: 'category', drillValue: cat,
            };
          }
          byKey[cat].count += count;
        }
      }
    }
    var keys = Object.keys(byKey);
    keys.sort(function (a, b) { return byKey[b].count - byKey[a].count; });
    var out = [];
    for (var ki = 0; ki < keys.length; ki++) {
      var key = keys[ki];
      var row = byKey[key];
      // Aggregated rows aren't expandable into per-phrase detail —
      // the underlying daily/hourly grids would need a re-aggregation
      // pass. Instead, clicking drills down: switch to Phrase mode
      // with a matching filter applied. drillType/drillValue carry
      // the target filter to the click handler.
      out.push([row.display, row.count, row.codes, null,
        { type: row.drillType, value: row.drillValue }]);
    }
    if (totalUncoded > 0) {
      out.push(['(uncoded)', totalUncoded, [], null, null]);
    }
    if (totalBlank > 0) {
      // Drill-down for (blank) goes back to phrase mode and selects
      // the literal (blank) row. No filter pill matches "(blank)"
      // directly, so we hand off via phrase navigation rather than a
      // pill click.
      out.push(['(blank)', totalBlank, [], null,
        { type: 'phrase', value: '(blank)' }]);
    }
    return out;
  }

  var currentViewMode = 'phrase';

  function renderViewModeToggle() {
    var modes = [
      { key: 'phrase',   label: 'Phrase' },
      { key: 'code',     label: 'By code' },
      { key: 'category', label: 'By category' },
    ];
    var html = '<div class="view-mode-toggle">' +
      '<span class="view-mode-label">Group by:</span>';
    for (var i = 0; i < modes.length; i++) {
      var m = modes[i];
      var cls = 'view-mode-btn' + (m.key === currentViewMode ? ' active' : '');
      html += '<button class="' + cls + '" data-view-mode="' + m.key + '">' +
        m.label + '</button>';
    }
    html += '</div>';
    return html;
  }

  // For the small set of agencies whose audit CSV schema entirely
  // omits any justification field — not even an empty column — we
  // render a structural-transparency callout instead of bars.
  // Listing "(blank)" as the dominant phrase would be misleading:
  // there is no field to be blank.
  function renderNoJustificationCallout(agencyData) {
    var fields = agencyData.audit_schema || [];
    var fieldsHtml = fields.map(function (f) {
      return '<code>' + escapeHTML(f) + '</code>';
    }).join(', ');
    return '<div class="no-justification-callout">' +
      '<div class="no-justification-head">' +
      'The only fields published in this agency&rsquo;s audit are ' + fieldsHtml +
      '</div>' +
      '<div class="no-justification-body">' +
      '<p>Other CA agencies&rsquo; audits include one or more of ' +
      '<code>reason</code> (free-text), <code>offenseType</code> ' +
      '(NIBRS-aligned dropdown), or <code>caseNumber</code> (case ID). ' +
      'This agency&rsquo;s audit publishes none of those, so the ' +
      'audit records that <strong>' + agencyData.row_count.toLocaleString() +
      '</strong> searches occurred but not why any of them was run.</p>' +
      '<p><strong>Officer-level accountability is also unavailable.</strong> ' +
      'The <code>userId</code> field is redacted to <code>***</code> by Flock ' +
      'across every agency&rsquo;s public audit, so the officer who ran each ' +
      'search cannot be identified from this published record alone &mdash; ' +
      'true for all ' + currentAgencyCount + ' audit-publishing agencies, ' +
      'not just this one.</p>' +
      '</div></div>';
  }

  function renderBars(items, total) {
    if (!items.length) {
      return '<div class="empty">No phrases to display for this agency.</div>';
    }
    var displayItems = aggregateItems(items, currentViewMode);
    var filterBarHtml = currentViewMode === 'phrase' ? renderFilterBar(items) : '';
    var hint = currentViewMode === 'phrase'
      ? 'Click any row for per-phrase detail (timing, network reach, daily cadence).'
      : 'Aggregated view &mdash; phrases with multiple codes are counted under each, so totals can exceed search count. ' +
        'Switch back to <em>Phrase</em> grouping for clickable rows with detail.';
    var html = renderViewModeToggle() +
      filterBarHtml +
      '<div class="bar-list-hint">' + hint + '</div>' +
      '<div class="bar-list">';
    items = displayItems;
    for (var i = 0; i < items.length; i++) {
      var phrase = items[i][0];
      var count = items[i][1];
      var codes = items[i][2] || [];
      var detail = items[i][3];
      var sharePct = total > 0 ? (100 * count / total) : 0;
      // Bar fill is the absolute share of total searches — a 12%
      // phrase fills 12% of the track. The cap of 100% naturally
      // applies; min 1% so even tiny entries show a visible nub.
      var w = Math.max(1, Math.round(sharePct));

      // Code-identification pills (orange) sit next to the phrase so
      // the "what is this" answer is right beside the text. Behavior
      // flags (long-lived, heavy use, broad reach, etc.) go in the
      // right column where the reader can scan for notable patterns
      // without re-reading every phrase.
      var flags = flagsForDetail(detail, count, phrase);
      // Pills sit on a second line within each row — code-id pills
      // first (orange, "what is this"), then behavior flags
      // (red/amber/blue, "what's notable"). Single line per kind so
      // the bar list still scans cleanly column-wise.
      var pillsBelow = '';
      if (codes.length || flags.length) {
        pillsBelow = '<div class="pill-row">';
        for (var j = 0; j < codes.length; j++) {
          pillsBelow += renderCodePill(codes[j]);
        }
        if (flags.length) pillsBelow += renderFlags(flags);
        pillsBelow += '</div>';
      }

      var isAggregated = currentViewMode !== 'phrase';
      var isBlank = phrase === '(blank)';
      var rowCls = 'bar-row bar-item' + (isBlank ? ' bar-row-blank' : '') +
        (isAggregated ? ' bar-row-aggregated' : '');
      var flagKinds = '';
      for (var fi = 0; fi < flags.length; fi++) {
        if (flags[fi].kind) flagKinds += ' ' + flags[fi].kind;
      }
      flagKinds = flagKinds.trim();
      var codeLabels = '';
      for (var cli = 0; cli < codes.length; cli++) {
        codeLabels += ' ' + codeSlug(codes[cli][1]);
      }
      codeLabels = codeLabels.trim();
      var rowCats = categoriesForCodes(codes, phrase).join(' ');
      var labelHtml = isBlank
        ? '<span class="blank-label" title="Officer did not enter a reason for this search">' +
          escapeHTML(phrase) + '</span>'
        : escapeHTML(phrase);
      var rowAttrs = ' data-phrase="' + escapeHTML(phrase) + '"' +
        ' data-flag-kinds="' + escapeHTML(flagKinds) + '"' +
        ' data-code-labels="' + escapeHTML(codeLabels) + '"' +
        ' data-categories="' + escapeHTML(rowCats) + '"';
      var drilldown = items[i][4];
      if (isAggregated) {
        rowAttrs += ' data-aggregated="1"';
        if (drilldown && drilldown.type) {
          rowAttrs += ' data-drilldown-type="' + escapeHTML(drilldown.type) + '"' +
            ' data-drilldown-value="' + escapeHTML(drilldown.value) + '"' +
            ' role="button" tabindex="0" aria-label="Drill into ' +
            escapeHTML(phrase) + '"';
        }
      } else {
        rowAttrs += ' role="button" tabindex="0" aria-label="Click to expand details for ' +
          escapeHTML(phrase) + '"';
      }
      var hintHtml;
      if (isAggregated) {
        hintHtml = (drilldown && drilldown.type)
          ? '<span class="expand-hint" aria-hidden="true">Drill in <span class="drill-arrow">&rsaquo;</span></span>'
          : '';
      } else {
        hintHtml = '<span class="expand-hint" aria-hidden="true">Details <span class="expand-arrow">&#9662;</span></span>';
      }
      html += '<div class="' + rowCls + '"' + rowAttrs + '>' +
        '<div class="bar-item-main">' +
        '<span class="bar-track">' +
        '<span class="bar" style="width:' + w + '%"></span>' +
        '<span class="bar-pct">' + sharePct.toFixed(1) + '%</span>' +
        '</span>' +
        '<span class="bar-count">' + count.toLocaleString() + '</span>' +
        '<span class="phrase-text">' + labelHtml + '</span>' +
        hintHtml +
        '</div>' +
        pillsBelow +
        '</div>';
    }
    html += '</div>';
    return html;
  }

  // Per-phrase detail panel: a stat line, a hourly mini-histogram,
  // and a weekly one. Rendered into the detail row when a phrase is
  // expanded. Returns the HTML string, not a node, so it can be
  // injected via innerHTML alongside the rest of the table.
  // ── Flags ──
  // High-level pattern callouts derived from a phrase's detail data
  // and the phrase string itself. Each flag has a tone (info / warn /
  // concern) for color, a label for the badge, and a longer tooltip.
  // The same flag set renders both as small badges on the long-active
  // callout rows and at the top of the expanded detail panel.

  var CASE_NUMBER_RE_JS = /^\d{2,4}-\d{3,8}$/;

  function flagsForDetail(detail, count, phrase) {
    var flags = [];
    if (!detail) return flags;
    var span = detail.span_days || 0;
    var days = detail.days || 0;

    // Span / longevity tiers. Strongest tier wins (don't double-flag).
    if (span >= 90) {
      flags.push({
        label: 'long-lived (3+ mo)',
        kind: 'long-lived',
        tone: 'concern',
        ruleTitle: 'Span ≥ 90 days',
        ruleBody: 'Triggered when the calendar window from first to last search exceeds 90 days &mdash; one case-number justification sustained across a full quarter.',
        observed: 'This phrase: <strong>' + span + '-day span</strong> (first ' +
          (detail.from || '?') + ', last ' + (detail.to || '?') + ').',
      });
    } else if (span >= 30) {
      flags.push({
        label: 'long-lived',
        kind: 'long-lived',
        tone: 'warn',
        ruleTitle: 'Span ≥ 30 days',
        ruleBody: 'Triggered when the calendar window from first to last search exceeds 30 days. A single case-number justification reused that long warrants proportionality review.',
        observed: 'This phrase: <strong>' + span + '-day span</strong> (first ' +
          (detail.from || '?') + ', last ' + (detail.to || '?') + ').',
      });
    }

    // Heavy use: high absolute count AND a meaningful per-active-day
    // rate. Either alone isn't striking; both together suggest a
    // sustained operational dependence on one justification text.
    if (count >= 50) {
      var perActiveDay = days > 0 ? count / days : 0;
      if (perActiveDay >= 3) {
        flags.push({
          label: 'heavy use',
          kind: 'heavy-use',
          tone: 'concern',
          ruleTitle: 'Searches ≥ 50 and ≥ 3 per active day',
          ruleBody: 'Triggered when the phrase has at least 50 total searches AND averages 3+ searches on each active day. Either alone isn’t striking; both together suggest a sustained operational dependence on one justification text.',
          observed: 'This phrase: <strong>' + count.toLocaleString() + ' searches</strong> over ' +
            days + ' active days (<strong>' + perActiveDay.toFixed(1) + ' per active day</strong>).',
        });
      }
    }

    // Multi-officer pattern: round-the-clock + most days. Hard to
    // produce single-officer; consistent with shift coverage / RTIC
    // dispatch / hotlist alerting rather than one investigator's work.
    var hourActive = 0;
    if (detail.hourly) {
      for (var i = 0; i < 24; i++) if (detail.hourly[i] > 0) hourActive++;
    }
    var dayActive = 0;
    if (detail.weekly) {
      for (var i2 = 0; i2 < 7; i2++) if (detail.weekly[i2] > 0) dayActive++;
    }
    if (hourActive >= 16 && dayActive >= 6 && count >= 20) {
      flags.push({
        label: 'multi-officer pattern',
        kind: 'multi-officer',
        tone: 'info',
        ruleTitle: '≥ 16 hours of day and ≥ 6 days of week active',
        ruleBody: 'Triggered when activity reaches at least 16 distinct hours of day AND at least 6 days of the week (with ≥ 20 total searches). Hard to produce from a single officer; consistent with multiple officers, shift coverage, or automated alerting rather than one investigator.',
        observed: 'This phrase: <strong>' + hourActive + ' hours of day</strong>, <strong>' +
          dayActive + ' days of week</strong>.',
      });
    }

    // Network reach. nc_med exposure to "above direct partners" is the
    // disclosure question; flagged as concern. nc_max <= 10 means the
    // search stayed within a small partner set; that's expected and
    // gets a quieter info tag.
    if (detail.nc_med != null) {
      if (detail.nc_med >= 100) {
        flags.push({
          label: 'broad reach',
          kind: 'broad-reach',
          tone: 'concern',
          ruleTitle: 'Median networkCount ≥ 100',
          ruleBody: 'Triggered when the median search reaches at least 100 networks. No California agency publishes 100+ direct sharing partners, so this routinely exceeds the agency’s declared bilateral sharing.',
          observed: 'This phrase: <strong>median ' + detail.nc_med.toLocaleString() +
            ' networks</strong> per search (range ' + detail.nc_min.toLocaleString() +
            '&ndash;' + detail.nc_max.toLocaleString() + ').',
        });
      } else if (detail.nc_max != null && detail.nc_max <= 10) {
        flags.push({
          label: 'narrow reach',
          kind: 'narrow-reach',
          tone: 'info',
          ruleTitle: 'Max networkCount ≤ 10',
          ruleBody: 'Triggered when even the broadest search for this phrase stays within 10 networks &mdash; consistent with use limited to direct sharing partners.',
          observed: 'This phrase: <strong>max ' + detail.nc_max.toLocaleString() +
            ' networks</strong> (median ' + detail.nc_med.toLocaleString() + ').',
        });
      }
    }

    // Case-number staleness (PR-prefixed). YY-NNNNN: 2-digit prefix,
    // assume 2000s. YYYY-NNNNN: take the year directly. We're only
    // confident for case numbers where the prefix is plausibly a year
    // (<= current year + 1).
    if (CASE_NUMBER_RE_JS.test(phrase)) {
      var m = phrase.match(/^(\d{2,4})-/);
      if (m) {
        var prefix = parseInt(m[1], 10);
        var currentYear = new Date().getFullYear();
        var yearGuess = null;
        if (prefix >= 1900 && prefix <= currentYear + 1) {
          yearGuess = prefix;
        } else if (prefix < 100) {
          yearGuess = prefix < 50 ? 2000 + prefix : 1900 + prefix;
        }
        if (yearGuess && yearGuess >= 1990 && yearGuess <= currentYear) {
          var ageYears = currentYear - yearGuess;
          if (ageYears >= 2) {
            flags.push({
              label: 'stale case (' + ageYears + 'y old)',
              kind: 'stale-case',
              tone: 'concern',
              ruleTitle: 'Case-number prefix ≥ 2 years before audit window',
              ruleBody: 'Triggered when the case-number prefix &mdash; commonly the year a case was opened &mdash; is interpreted as a year at least 2 years older than the current audit window. Heuristic: many agencies but not all use YY- or YYYY- prefixes for the case year.',
              observed: 'Prefix <code>' + escapeHTML(m[1]) + '</code> &rarr; year <strong>' +
                yearGuess + '</strong>, <strong>' + ageYears + ' years old</strong> as of today.',
            });
          }
        }
      }
    }

    return flags;
  }

  // Map a CA code-book abbreviation to its full title. The label
  // already has the abbreviation in parens (e.g. "Burglary (PC)") —
  // we expand it for the tooltip so a non-LE-savvy reader can see
  // what the suffix means without having to look it up.
  var CODE_BOOK_NAMES = {
    PC: 'California Penal Code',
    CVC: 'California Vehicle Code',
    HSC: 'California Health & Safety Code',
    WIC: 'California Welfare & Institutions Code',
  };

  function codeBookFromLabel(label) {
    // Match the trailing "(PC)" / "(CVC)" / etc. — preserve the
    // raw match so we can show "(historic)" qualifiers verbatim.
    var m = String(label).match(/\(([A-Z]+)(?:,[^)]*)?\)\s*$/);
    if (!m) return null;
    return m[1];
  }

  function renderCodePill(codeRow) {
    var code = codeRow[0];
    var label = codeRow[1];
    var book = codeBookFromLabel(label);
    var bookFull = book ? (CODE_BOOK_NAMES[book] || book) : '';
    var ruleLine = bookFull ? bookFull + ' &sect; ' + escapeHTML(code)
                            : 'Section ' + escapeHTML(code);
    var bodyLine = 'Detected by matching the bare section number in the ' +
      'phrase against a static lookup of common ALPR-audit penal, vehicle, ' +
      'health-and-safety, and welfare-and-institutions code references. ' +
      'The label is what the lookup table assigns to that section.';
    var observedLine = 'Phrase resolved to code <code>' + escapeHTML(code) +
      '</code> &rarr; "' + escapeHTML(label) + '".';
    return '<span class="code-pill" tabindex="0">' +
      escapeHTML(label) +
      '<span class="flag-tip" role="tooltip">' +
      '<span class="flag-tip-rule">' + ruleLine + '</span>' +
      '<span class="flag-tip-body">' + bodyLine + '</span>' +
      '<span class="flag-tip-observed">' + observedLine + '</span>' +
      '</span>' +
      '</span>';
  }

  function renderFlags(flags) {
    if (!flags || !flags.length) return '';
    var html = '<span class="flag-tags">';
    for (var i = 0; i < flags.length; i++) {
      var f = flags[i];
      html += '<span class="flag-tag flag-' + f.tone + '" tabindex="0">' +
        escapeHTML(f.label) +
        '<span class="flag-tip" role="tooltip">' +
        '<span class="flag-tip-rule">' + f.ruleTitle + '</span>' +
        '<span class="flag-tip-body">' + f.ruleBody + '</span>' +
        '<span class="flag-tip-observed">' + f.observed + '</span>' +
        '</span>' +
        '</span>';
    }
    html += '</span>';
    return html;
  }

  // ── Pattern-summary helpers ──
  // Convert a weekday distribution (Mon=0..Sun=6) and an hour-of-day
  // distribution (0..23) into prose. Keeps the per-phrase detail panel
  // legible without three competing tiny charts.

  var DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

  function listToProse(arr) {
    if (arr.length === 0) return '';
    if (arr.length === 1) return arr[0];
    if (arr.length === 2) return arr[0] + ' and ' + arr[1];
    return arr.slice(0, -1).join(', ') + ', and ' + arr[arr.length - 1];
  }

  function formatHour(h) {
    h = ((h % 24) + 24) % 24;
    if (h === 0) return 'midnight';
    if (h === 12) return 'noon';
    if (h < 12) return h + 'am';
    return (h - 12) + 'pm';
  }

  function describeWeekday(weekly) {
    if (!weekly) return '';
    var total = 0;
    for (var i = 0; i < 7; i++) total += weekly[i];
    if (total === 0) return '';

    // Coefficient-of-variation as a uniformity gauge: a roughly even
    // distribution gets the "spread evenly" framing rather than a
    // forced peak-day call-out.
    var mean = total / 7;
    var variance = 0;
    for (var i2 = 0; i2 < 7; i2++) {
      variance += (weekly[i2] - mean) * (weekly[i2] - mean);
    }
    variance /= 7;
    var cv = mean > 0 ? Math.sqrt(variance) / mean : 0;

    if (cv < 0.45) {
      return 'Searches are spread fairly evenly across all days of the week.';
    }

    // Days with at least half the mean's worth of activity count as
    // "active." Cap a small floor so a single search on Sunday in a
    // 200-search dataset doesn't muddy the call-out.
    var threshold = Math.max(2, mean * 0.5);
    var active = [];
    for (var i3 = 0; i3 < 7; i3++) {
      if (weekly[i3] >= threshold) active.push(i3);
    }
    if (active.length === 0 || active.length === 7) {
      return 'Searches occur across the whole week.';
    }
    if (active.length === 1) {
      return 'Searches are concentrated on ' + DAY_NAMES[active[0]] + '.';
    }

    // Try contiguous-range framing in both linear and cyclic order
    // (so e.g. Sun–Thu — wrap-around — phrases naturally).
    var sortedActive = active.slice().sort(function (a, b) { return a - b; });
    var contig = true;
    for (var k = 1; k < sortedActive.length; k++) {
      if (sortedActive[k] - sortedActive[k - 1] !== 1) { contig = false; break; }
    }
    if (contig) {
      return 'The bulk of searches happen ' +
        DAY_NAMES[sortedActive[0]] + ' through ' +
        DAY_NAMES[sortedActive[sortedActive.length - 1]] + '.';
    }
    // Cyclic contiguous (e.g. Sun, Mon, Tue, Wed, Thu)
    var rotated = sortedActive.map(function (d) { return (d + 1) % 7; }).sort(function (a, b) { return a - b; });
    var contigCyc = true;
    for (var k2 = 1; k2 < rotated.length; k2++) {
      if (rotated[k2] - rotated[k2 - 1] !== 1) { contigCyc = false; break; }
    }
    if (contigCyc) {
      var firstRot = rotated[0] === 0 ? 6 : rotated[0] - 1;
      var lastRot = rotated[rotated.length - 1] === 0 ? 6 : rotated[rotated.length - 1] - 1;
      return 'The bulk of searches happen ' +
        DAY_NAMES[firstRot] + ' through ' +
        DAY_NAMES[lastRot] + '.';
    }
    return 'Searches concentrate on ' +
      listToProse(active.map(function (d) { return DAY_NAMES[d]; })) + '.';
  }

  function describeHour(hourly) {
    if (!hourly) return '';
    var total = 0;
    for (var i = 0; i < 24; i++) total += hourly[i];
    if (total === 0) return '';

    // Smallest cyclic window containing 80% of activity. Cyclic so
    // a graveyard pattern (10pm–6am) reads as one contiguous block,
    // not as "before 6am" + "after 10pm."
    var target = total * 0.8;
    var bestStart = 0, bestWidth = 24;
    for (var start = 0; start < 24; start++) {
      var sum = 0;
      for (var len = 1; len <= 24; len++) {
        sum += hourly[(start + len - 1) % 24];
        if (sum >= target) {
          if (len < bestWidth) {
            bestWidth = len;
            bestStart = start;
          }
          break;
        }
      }
    }

    if (bestWidth >= 22) return 'Searches occur round the clock.';
    if (bestWidth >= 18) return 'Searches are spread broadly across the day.';
    var endHour = (bestStart + bestWidth) % 24;
    return 'The bulk of searches happen between ' +
      formatHour(bestStart) + ' and ' + formatHour(endHour) + '.';
  }

  function describeCalendar(daily, days, span) {
    if (!daily || !daily.length || !span) return '';
    // Longest run of zero days bracketed by activity.
    var maxGap = 0, gap = 0, started = false;
    for (var i = 0; i < daily.length; i++) {
      if (daily[i] === 0) {
        if (started) gap++;
      } else {
        started = true;
        if (gap > maxGap) maxGap = gap;
        gap = 0;
      }
    }
    if (maxGap >= 14) {
      return 'Activity is intermittent &mdash; there is a stretch of ' +
        maxGap + ' consecutive days with no searches at all.';
    }
    var density = days / span;
    if (density >= 0.7) {
      return 'Searches happen on most days within the span.';
    }
    if (density <= 0.3) {
      return 'Activity is sparse &mdash; only ' + Math.round(100 * density) +
        '% of days within the span had any search activity.';
    }
    return '';
  }

  // Translate a span in days to a human framing the reader can grasp
  // at a glance — "53 days" lands less than "8 weeks" or "1 month",
  // and the whole point of the long-active callout is the span.
  function spanFraming(spanDays) {
    if (!spanDays || spanDays < 2) return '';
    if (spanDays < 14) return spanDays + '-day span';
    if (spanDays < 60) {
      var w = Math.round(spanDays / 7);
      return spanDays + '-day span (~' + w + ' week' + (w === 1 ? '' : 's') + ')';
    }
    if (spanDays < 365) {
      var m = Math.round(spanDays / 30.4);
      return spanDays + '-day span (~' + m + ' month' + (m === 1 ? '' : 's') + ')';
    }
    var y = (spanDays / 365.25).toFixed(1);
    return spanDays + '-day span (~' + y + ' years)';
  }

  function renderPhraseDetail(detail, phrase, count) {
    if (!detail) {
      return '<div class="empty">No timestamped rows for this phrase.</div>';
    }
    var dateLine = '';
    if (detail.from && detail.to) {
      var span = spanFraming(detail.span_days);
      dateLine = (span ? '<strong>' + span + '</strong>: ' : '') +
        detail.from + ' through ' + detail.to +
        ' &middot; active on ' + detail.days + ' of ' +
        (detail.span_days || detail.days) + ' day' +
        ((detail.span_days || detail.days) === 1 ? '' : 's');
    }
    var ncLine = '';
    if (detail.nc_med != null) {
      ncLine = '<strong>Network reach</strong>: median ' +
        detail.nc_med.toLocaleString() +
        ' (range ' + detail.nc_min.toLocaleString() +
        '&ndash;' + detail.nc_max.toLocaleString() + ')';
      if (detail.nc_over_100) {
        ncLine += ' &middot; <span class="reach-broad">' + detail.nc_over_100.toLocaleString() +
          ' search' + (detail.nc_over_100 === 1 ? '' : 'es') +
          ' reached &ge;100 networks</span>';
      }
    }

    var summarySentences = [];
    var s1 = describeWeekday(detail.weekly);
    if (s1) summarySentences.push(s1);
    var s2 = describeHour(detail.hourly);
    if (s2) summarySentences.push(s2);
    var s3 = describeCalendar(detail.daily, detail.days, detail.span_days);
    if (s3) summarySentences.push(s3);
    var summaryHtml = summarySentences.length
      ? '<div class="phrase-detail-row phrase-detail-summary">' +
        summarySentences.join(' ') + '</div>'
      : '';

    var dailyHtml = '';
    if (detail.daily && detail.daily.length) {
      var unit = detail.daily_unit === 'week' ? 'week' : 'day';
      dailyHtml = '<div class="phrase-detail-row">' +
        '<div class="phrase-detail-chart phrase-detail-chart-full">' +
        '<div class="phrase-detail-label">Searches per ' + unit +
        ' (' + detail.daily.length + ' ' + unit + 's)</div>' +
        renderDailySpark(detail.daily, detail.from, detail.daily_unit) +
        '</div></div>';
    }

    var flags = flagsForDetail(detail, count, phrase);
    var flagsHtml = flags.length ? renderFlags(flags) : '';
    var codes = currentCodes[phrase] || [];
    var codePillsHtml = '';
    if (codes.length) {
      codePillsHtml = '<div class="phrase-detail-codes">';
      for (var ci = 0; ci < codes.length; ci++) {
        codePillsHtml += renderCodePill(codes[ci]);
      }
      codePillsHtml += '</div>';
    }

    return '<div class="phrase-detail">' +
      '<div class="phrase-detail-head">' +
      '<strong>' + count.toLocaleString() + ' searches</strong>' +
      (dateLine ? ' &middot; ' + dateLine : '') +
      codePillsHtml +
      (flagsHtml ? '<div class="phrase-detail-flags">' + flagsHtml + '</div>' : '') +
      '</div>' +
      (ncLine ? '<div class="phrase-detail-row">' + ncLine + '</div>' : '') +
      summaryHtml +
      dailyHtml +
      '</div>';
  }

  // Daily-or-weekly density sparkline: one cell per period, height
  // proportional to count. Empty cells stay visible (faint baseline)
  // so gaps in activity don't visually compress the span.
  function renderDailySpark(series, fromIso, unit) {
    if (!series || !series.length) return '';
    var max = 1;
    for (var i = 0; i < series.length; i++) if (series[i] > max) max = series[i];
    var fromDate = fromIso ? new Date(fromIso + 'T12:00:00Z') : null;
    var step = unit === 'week' ? 7 : 1;
    var html = '<div class="daily-spark">';
    for (var i2 = 0; i2 < series.length; i2++) {
      var v = series[i2];
      var ph = max > 0 ? Math.round(100 * v / max) : 0;
      var label = '';
      if (fromDate) {
        var d = new Date(fromDate.getTime() + i2 * step * 86400000);
        label = d.toISOString().slice(0, 10);
      }
      var titleText = (label ? label + ': ' : '') + v +
        ' search' + (v === 1 ? '' : 'es');
      var cls = 'spark-bar' + (v === 0 ? ' empty' : '');
      html += '<div class="' + cls + '" title="' + titleText + '">' +
        '<div class="spark-fill" style="height:' + ph + '%"></div>' +
        '</div>';
    }
    html += '</div>';
    return html;
  }

  // Click-handler for bar rows / cloud words / case-callout rows.
  // Toggles a sibling detail row containing renderPhraseDetail output
  // for the clicked phrase. Single delegated listener attached at
  // init time.
  function handleExpandClick(ev) {
    // View-mode toggle buttons — change mode and re-render.
    var modeBtn = ev.target.closest('.view-mode-btn');
    if (modeBtn) {
      var mode = modeBtn.getAttribute('data-view-mode');
      if (mode && mode !== currentViewMode && currentAgencyData) {
        currentViewMode = mode;
        document.getElementById('page').innerHTML =
          renderAgency(currentAgencyData, currentSlug);
      }
      return;
    }
    // Filter-pill clicks next — they aren't expandable rows.
    var pill = ev.target.closest('.filter-pill');
    if (pill) {
      handleFilterClick(pill);
      return;
    }
    var target = ev.target.closest('[data-phrase]');
    if (!target) return;
    // Aggregated rows drill down rather than expand: switch to Phrase
    // mode and activate the matching filter pill (or for (blank),
    // scroll to the row in the unfiltered phrase list).
    if (target.getAttribute('data-aggregated') === '1') {
      var drillType = target.getAttribute('data-drilldown-type');
      var drillValue = target.getAttribute('data-drilldown-value');
      if (!drillType || !drillValue || !currentAgencyData) return;
      currentViewMode = 'phrase';
      document.getElementById('page').innerHTML =
        renderAgency(currentAgencyData, currentSlug);
      if (drillType === 'phrase') {
        var row = document.querySelector(
          '.bar-item[data-phrase="' + drillValue.replace(/"/g, '\\"') + '"]'
        );
        if (row) {
          row.scrollIntoView({ behavior: 'smooth', block: 'center' });
          row.classList.add('expanded');
          window.setTimeout(function () { row.classList.remove('expanded'); }, 1500);
        }
        return;
      }
      var pill = document.querySelector(
        '.filter-pill[data-filter-type="' + drillType +
        '"][data-filter-value="' + drillValue + '"]'
      );
      if (pill) handleFilterClick(pill);
      return;
    }
    var phrase = target.getAttribute('data-phrase');
    if (!phrase) return;
    var details = currentDetails;
    if (!details) return;
    var detail = details[phrase];
    var count = currentCounts[phrase] || 0;

    // Cloud word click: scroll to the matching bar row and toggle its
    // expansion. Keeps a single source of truth for the expanded state.
    if (target.classList.contains('word')) {
      var barRow = document.querySelector(
        '.bar-row[data-phrase="' + phraseAttr(phrase) + '"]'
      );
      if (barRow) {
        barRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
        toggleRowExpansion(barRow, detail, phrase, count);
      }
      return;
    }
    // Bar list uses div rows; long-active callout still uses table rows.
    var row = target.closest('.bar-item, tr.bar-row');
    if (row) {
      toggleRowExpansion(row, detail, phrase, count);
    }
  }

  function phraseAttr(s) {
    // Match the escapeHTML output for use inside a CSS attribute selector.
    return s.replace(/"/g, '&quot;');
  }

  // Filter-pill click: dim non-matching rows in the bars table.
  // Clicking the same kind again (or "All") clears the filter.
  function handleFilterClick(pill) {
    var type = pill.getAttribute('data-filter-type') || '';
    var value = pill.getAttribute('data-filter-value') || '';
    var bar = pill.parentNode;
    if (!bar) return;
    var list = null;
    var sib = bar.nextElementSibling;
    while (sib) {
      if (sib.classList && sib.classList.contains('bar-list')) {
        list = sib;
        break;
      }
      sib = sib.nextElementSibling;
    }
    if (!list) return;

    var alreadyActive = pill.classList.contains('active');
    var allPills = bar.querySelectorAll('.filter-pill');
    for (var i = 0; i < allPills.length; i++) {
      allPills[i].classList.remove('active');
    }

    var clearing = (alreadyActive || type === 'all');
    if (clearing) {
      var allPill = bar.querySelector('.filter-all');
      if (allPill) allPill.classList.add('active');
    } else {
      pill.classList.add('active');
    }

    // Imperatively hide non-matching rows. CSS attribute selectors
    // can't enumerate code labels or category sets at build time (the
    // sets are dynamic per agency), so the JS approach handles all
    // filter types uniformly.
    var attrName = type === 'code' ? 'data-code-labels'
      : type === 'category' ? 'data-categories'
      : 'data-flag-kinds';
    var rows = list.querySelectorAll('.bar-item');
    for (var r = 0; r < rows.length; r++) {
      if (clearing) {
        rows[r].style.display = '';
        continue;
      }
      var attr = rows[r].getAttribute(attrName) || '';
      var match = (' ' + attr + ' ').indexOf(' ' + value + ' ') !== -1;
      rows[r].style.display = match ? '' : 'none';
    }

    // Close any expansion in the filtered set so a hidden row
    // doesn't carry along an orphan detail row.
    var details = list.querySelectorAll('.detail-row');
    for (var d = 0; d < details.length; d++) {
      if (details[d].previousElementSibling) {
        details[d].previousElementSibling.classList.remove('expanded');
      }
      details[d].parentNode.removeChild(details[d]);
    }
  }

  function toggleRowExpansion(row, detail, phrase, count) {
    var next = row.nextElementSibling;
    if (next && next.classList.contains('detail-row')) {
      next.parentNode.removeChild(next);
      row.classList.remove('expanded');
      return;
    }
    // Close any other expansion in the same container first so only
    // one is open at a time per container — keeps the page tidy.
    var container = row.parentNode;
    var existing = container.querySelectorAll('.detail-row');
    for (var i = 0; i < existing.length; i++) {
      var er = existing[i];
      if (er.previousElementSibling) er.previousElementSibling.classList.remove('expanded');
      er.parentNode.removeChild(er);
    }
    var detailHtml = renderPhraseDetail(detail, phrase, count);
    var dr;
    // Bars list uses div-based rows; long-active callout still uses
    // a real table. Detail row shape needs to match the parent so
    // the markup stays valid.
    if (row.tagName === 'TR') {
      dr = document.createElement('tr');
      dr.className = 'detail-row';
      var colspan = row.children.length;
      dr.innerHTML = '<td colspan="' + colspan + '">' + detailHtml + '</td>';
    } else {
      dr = document.createElement('div');
      dr.className = 'detail-row';
      dr.innerHTML = detailHtml;
    }
    container.insertBefore(dr, row.nextSibling);
    row.classList.add('expanded');
  }

  var currentDetails = null;
  var currentCounts = {};
  var currentCodes = {};
  var currentAgencyData = null;
  var currentSlug = '';
  var currentAgencyCount = 0;

  // Populate the per-phrase context maps that handleExpandClick reads.
  // Counts come from verbatim AND long-active-cases so all three
  // entry points resolve their phrase to a count, even if the phrase
  // didn't make the verbatim top-50.
  function installPhraseContext(agencyData) {
    currentAgencyData = agencyData || null;
    currentDetails = (agencyData && agencyData.phrase_details) || {};
    currentCounts = {};
    currentCodes = {};
    if (agencyData && agencyData.verbatim) {
      for (var i = 0; i < agencyData.verbatim.length; i++) {
        var v = agencyData.verbatim[i];
        currentCounts[v[0]] = v[1];
        if (v[2]) currentCodes[v[0]] = v[2];
      }
    }
    if (agencyData && agencyData.long_active_cases) {
      for (var j = 0; j < agencyData.long_active_cases.length; j++) {
        var lac = agencyData.long_active_cases[j];
        currentCounts[lac[0]] = lac[1];
      }
    }
  }

  // 7-day x 24-hour heatmap. Hours are in PT (build-side conversion).
  // Cell intensity is sqrt-scaled to keep low-but-nonzero cells visible
  // alongside a dominant peak (linear scale would wash out off-peak
  // activity for agencies with one busy hour). Empty cells stay
  // visibly empty (light gray) so gaps in coverage are legible.
  function renderHeatmap(grid, timedRows) {
    if (!grid || !grid.length || !timedRows) {
      return '<div class="empty">No timestamped searches available for this agency.</div>';
    }
    var max = 0;
    for (var d = 0; d < 7; d++) {
      for (var h = 0; h < 24; h++) {
        if (grid[d][h] > max) max = grid[d][h];
      }
    }
    var days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    var html = '<div class="heatmap-wrap">' +
      '<table class="heatmap"><thead><tr><th></th>';
    for (var h = 0; h < 24; h++) {
      // Show every 3 hours to avoid header crowding.
      html += '<th>' + (h % 3 === 0 ? (h < 10 ? '0' + h : h) : '') + '</th>';
    }
    html += '</tr></thead><tbody>';
    for (var d2 = 0; d2 < 7; d2++) {
      html += '<tr><th class="day-label">' + days[d2] + '</th>';
      for (var h2 = 0; h2 < 24; h2++) {
        var c = grid[d2][h2] || 0;
        var t = max > 0 ? Math.sqrt(c / max) : 0;
        // Light gray for zero, blue ramp for nonzero so the eye can
        // pick out absent cells from low-but-present ones.
        var bg = c === 0
          ? '#f1f5f9'
          : 'rgba(37, 99, 235, ' + (0.12 + 0.82 * t).toFixed(3) + ')';
        var fg = t > 0.55 ? '#fff' : '#1e293b';
        html += '<td title="' + days[d2] + ' ' + h2 + ':00 — ' + c +
          ' search' + (c === 1 ? '' : 'es') + '"' +
          ' style="background:' + bg + ';color:' + fg + '">' +
          (c >= 1 ? c : '') +
          '</td>';
      }
      html += '</tr>';
    }
    html += '</tbody></table></div>' +
      '<div class="heatmap-caption">Day × hour of day, Pacific Time. ' +
      timedRows.toLocaleString() + ' search' +
      (timedRows === 1 ? '' : 'es') + ' with timestamps.</div>';
    return html;
  }

  function renderCodes(items) {
    if (!items.length) {
      return '<div class="empty">No California penal/vehicle codes detected in the justification text for this agency.</div>';
    }
    var html = '<table class="codes-table">' +
      '<thead><tr><th>Code</th><th>Description</th><th class="count">Searches</th></tr></thead>' +
      '<tbody>';
    for (var i = 0; i < items.length; i++) {
      html += '<tr>' +
        '<td class="code">' + escapeHTML(items[i][0]) + '</td>' +
        '<td>' + escapeHTML(items[i][1]) + '</td>' +
        '<td class="count">' + items[i][2] + '</td>' +
        '</tr>';
    }
    html += '</tbody></table>';
    return html;
  }

  function templatingCommentary(top1Pct, unique, total) {
    // Translate the top-1 share + unique-reason ratio into a one-line
    // qualitative read: highly templated, mixed, or freeform.
    if (!total) return '';
    var ratio = unique / total;
    if (top1Pct >= 30 || unique <= 30) {
      return 'High templating: a single phrase covers ' + top1Pct.toFixed(0) +
        '% of searches, suggesting a small dropdown or copy-paste set rather than case-specific narratives.';
    }
    if (top1Pct >= 15 || ratio < 0.05) {
      return 'Mixed: a small set of phrases dominates, but with meaningful long-tail variation.';
    }
    return 'Predominantly free-text: many distinct reasons, no single phrase carries the bulk of searches.';
  }

  function renderAgency(agencyData, slug) {
    currentSlug = slug || '';
    if (!agencyData) {
      return '<h1>Search Justifications</h1>' +
        '<p class="subtitle">No data for <code>' + escapeHTML(slug) + '</code>.</p>';
    }
    var dateRange = '';
    if (agencyData.search_date_min && agencyData.search_date_max) {
      dateRange = ' (' + agencyData.search_date_min + ' through ' + agencyData.search_date_max + ')';
    }
    var commentary = templatingCommentary(
      agencyData.top1_share_pct,
      agencyData.unique_reasons,
      agencyData.row_count
    );
    var windowBanner = '';
    if (agencyData.search_date_min && agencyData.search_date_max) {
      var dms = Date.parse(agencyData.search_date_min + 'T12:00:00Z');
      var dms2 = Date.parse(agencyData.search_date_max + 'T12:00:00Z');
      var spanD = (!isNaN(dms) && !isNaN(dms2))
        ? Math.round((dms2 - dms) / 86400000) + 1
        : null;
      var rawLink = 'data/audit/' + encodeURIComponent(agencyData.slug) + '.json';
      windowBanner = '<div class="audit-window">' +
        '<span class="audit-window-label">Data window</span>' +
        '<strong>' + escapeHTML(agencyData.search_date_min) +
        ' &rarr; ' + escapeHTML(agencyData.search_date_max) + '</strong>' +
        (spanD ? ' <span class="audit-window-span">(' + spanD +
          ' day' + (spanD === 1 ? '' : 's') + ')</span>' : '') +
        ' <a class="audit-window-raw" href="' + rawLink +
        '" target="_blank" rel="noopener">View raw audit data &rarr;</a>' +
        '<span class="audit-window-note">' +
        'Built from every scrape of this agency&rsquo;s public search audit on file. ' +
        'Flock exposes a rolling ~30-day window; older rows persist here only ' +
        'because they were captured before they aged out.' +
        '</span></div>';
    }
    var blurb =
      '<p class="blurb">Aggregated free-text reasons that ' +
      escapeHTML(agencyData.display_name) +
      ' attached to ALPR plate searches in the data window above. ' +
      'Reasons are entered by the searching officer and are the agency’s own justification record. ' +
      commentary +
      '</p>';

    var shown = agencyData.verbatim_shown || 0;
    var unique = agencyData.unique_reasons;
    var stats =
      '<div class="stats-grid">' +
      stat('Searches', fmt(agencyData.row_count), 'in audit window') +
      stat('Unique reasons', fmt(unique),
        shown < unique
          ? 'top ' + fmt(shown) + ' shown below'
          : 'all shown below') +
      stat('Top phrase share', pct(agencyData.top1_share_pct),
        'top 3: ' + pct(agencyData.top3_share_pct),
        agencyData.top1_share_pct >= 30 ? 'warn' : '') +
      stat('Blank reasons', fmt(agencyData.blank_reasons),
        agencyData.row_count > 0
          ? pct(100 * agencyData.blank_reasons / agencyData.row_count) + ' of total'
          : '') +
      '</div>';

    return '<h1>' + escapeHTML(agencyData.display_name) + '</h1>' +
      '<p class="subtitle">ALPR search justifications</p>' +
      windowBanner +
      blurb +
      stats +
      renderUses(agencyData) +
      renderLongActiveCases(agencyData.long_active_cases, agencyData.display_name) +
      '<h2>Top reasons by count</h2>' +
      (agencyData.has_justification_column === false
        ? renderNoJustificationCallout(agencyData)
        : '<p class="bars-disclaimer">' +
          'These reasons are typed in (or chosen from a dropdown) by the searching officer. ' +
          'Nothing in the audit system validates that the entered reason aligns with the actual ' +
          'reason for the search, or that the search was authorized.' +
          '</p>' +
          renderBars(agencyData.verbatim, agencyData.row_count)) +
      '<h2>Search timing (day &times; hour, Pacific Time)</h2>' +
      renderHeatmap(agencyData.hour_dow, agencyData.timed_rows) +
      '<h2>Detected California penal / vehicle codes</h2>' +
      renderCodes(agencyData.penal_codes) +
      '<div class="footnote">' +
      'Source: this agency’s Public Search Audit CSV, embedded in its Flock Safety transparency portal. ' +
      'Rows are deduplicated by ID across every scrape we have, so the audit window can extend ' +
      'beyond Flock’s ~30-day rolling cutoff. Cloud and bars show the top 50 reasons by count. ' +
      'See <a href="https://github.com/none-below/sm-alpr/blob/main/scripts/build_justifications.py">' +
      'build_justifications.py</a> for processing details.' +
      '</div>';
  }

  function stat(label, value, sub, cls) {
    cls = cls || '';
    return '<div class="stat ' + cls + '">' +
      '<div class="label">' + escapeHTML(label) + '</div>' +
      '<div class="value">' + value + '</div>' +
      (sub ? '<div class="sub">' + escapeHTML(sub) + '</div>' : '') +
      '</div>';
  }

  function populateSelect(agencies, currentSlug) {
    var sel = document.getElementById('agency-select');
    sel.innerHTML = '';
    var slugs = Object.keys(agencies);
    slugs.sort(function (a, b) {
      return agencies[a].display_name.localeCompare(agencies[b].display_name);
    });
    for (var i = 0; i < slugs.length; i++) {
      var s = slugs[i];
      var opt = document.createElement('option');
      opt.value = s;
      opt.textContent = agencies[s].display_name +
        ' (' + agencies[s].row_count.toLocaleString() + ' searches)';
      if (s === currentSlug) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.addEventListener('change', function () {
      var newSlug = sel.value;
      // Use replaceState rather than pushState so back-button doesn't
      // cycle through every agency the user previewed.
      history.replaceState(null, '', '?agency=' + encodeURIComponent(newSlug));
      var data = agencies[newSlug];
      installPhraseContext(data);
      document.getElementById('page').innerHTML =
        renderAgency(data, newSlug);
    });
  }

  function getInitialSlug(agencies) {
    var params = new URLSearchParams(window.location.search);
    var slug = params.get('agency');
    if (slug && agencies[slug]) return slug;
    if (agencies[DEFAULT_SLUG]) return DEFAULT_SLUG;
    var keys = Object.keys(agencies);
    return keys.length ? keys[0] : null;
  }

  function init() {
    fetch(DATA_URL)
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        var agencies = data.agencies || {};
        currentAgencyCount = data.agency_count || Object.keys(agencies).length;
        var slug = getInitialSlug(agencies);
        populateSelect(agencies, slug);
        installPhraseContext(slug ? agencies[slug] : null);
        document.getElementById('page').innerHTML =
          renderAgency(slug ? agencies[slug] : null, slug || '');
        document.getElementById('page').addEventListener('click', handleExpandClick);
        document.getElementById('page').addEventListener('keydown', function (ev) {
          if (ev.key !== 'Enter' && ev.key !== ' ') return;
          var row = ev.target.closest('.bar-item, tr.bar-row');
          if (!row) return;
          ev.preventDefault();
          handleExpandClick({ target: row });
        });
      })
      .catch(function (err) {
        document.getElementById('page').innerHTML =
          '<h1>Search Justifications</h1>' +
          '<p class="subtitle">Failed to load data: ' + escapeHTML(err.message) + '</p>' +
          '<p>Run <code>uv run python scripts/build_justifications.py</code> to generate ' +
          '<code>docs/data/justifications.json</code>, then reload.</p>';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
