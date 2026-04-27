// Standalone ALPR agency report.
//
// URL: docs/report.html?agency=<slug>
//
// Loads docs/data/report_data.json and renders a printable per-agency
// report. All compute happens at build time in scripts/build_report_data.py
// — this file just formats it for humans.

(function() {
  "use strict";

  const AGENCY_TYPE_LABELS = {
    city: "city police",
    county: "county sheriff",
    state: "state agency",
    federal: "federal agency",
    university: "university",
    fusion_center: "fusion center",
    private: "private entity",
    transit: "transit police",
    school_district: "school district",
    other: "agency",
    test: "test account",
    decommissioned: "decommissioned",
  };

  // Short singular/plural nouns for rank-pill phrasing. "higher than
  // nearly all peers" reads fine but leaves readers wondering "all
  // peers of what kind?" — the peer pool is type-filtered, so name it.
  const PEER_NOUNS = {
    all:             { sing: "CA agency",       plu: "CA agencies" },
    city:            { sing: "city",             plu: "cities" },
    county:          { sing: "county",           plu: "counties" },
    state:           { sing: "state agency",     plu: "state agencies" },
    federal:         { sing: "federal agency",   plu: "federal agencies" },
    university:      { sing: "university",       plu: "universities" },
    fusion_center:   { sing: "fusion center",    plu: "fusion centers" },
    private:         { sing: "private entity",   plu: "private entities" },
    transit:         { sing: "transit agency",   plu: "transit agencies" },
    school_district: { sing: "school district",  plu: "school districts" },
  };
  function peerNounsFor(peerSample) {
    if (!peerSample || !peerSample.type) return null;
    return PEER_NOUNS[peerSample.type] || null;
  }
  // Full pool name for prose contexts ("among CA counties"). The
  // rank pill uses a shorter form.
  function peerPoolName(peerSample) {
    const nouns = peerNounsFor(peerSample);
    if (!nouns) return "CA agencies";
    return nouns.plu.startsWith("CA ") ? nouns.plu : `CA ${nouns.plu}`;
  }

  // Category labels name the actual concern rather than a blanket "violates SB 34"
  // claim — the problem differs by category. `private` is split at display time
  // into `private_entity` (companies, HOAs, towing) and `private_university`
  // (Stanford, USF, UOP) because their statutory situations differ. See
  // refineKind() below.
  const FLAG_LABELS = {
    private_entity: "PRIVATE ENTITY \u2014 not a public agency",
    private_university: "PRIVATE UNIVERSITY PD \u2014 contested",
    out_of_state: "OUT OF STATE",
    federal: "FEDERAL",
    fusion_center: "RE-SHARING HUB",
    decommissioned: "INACTIVE \u2014 no current custodian",
    test: "TEST/FIXTURE \u2014 access controls unknown",
  };

  const FLAG_EXPLANATIONS = {
    private_entity: "Not a \u201cpublic agency\u201d under CA Civil Code \u00a71798.90.5(f), which limits that term to the state or a city/county/political subdivision. Sharing ALPR data with a private entity likely violates \u00a71798.90.55(b).",
    private_university: "Private university police departments are authorized under CA Education Code \u00a776400, but their qualification as \u201cpublic agencies\u201d under CA Civil Code \u00a71798.90.5(f) is contested \u2014 the statute defines \u201cpublic agency\u201d as the state or a city/county/political subdivision, which does not clearly include private universities.",
    out_of_state: "CA Civil Code \u00a71798.90.55(b) and AG Bulletin 2023-DLE-06 prohibit sharing ALPR data with non-California agencies.",
    federal: "Federal agencies are not \u201cagencies of the state\u201d under \u00a71798.90.5(f). AG Bulletin 2023-DLE-06 prohibits sharing with federal agencies.",
    fusion_center: "Multi-agency re-sharing hub \u2014 data sent here is redistributed to many downstream entities, some of which may not qualify as \u201cpublic agencies.\u201d Whether the hub itself qualifies under \u00a71798.90.5(f) depends on its specific charter, governance, funding, and staffing \u2014 see the per-entity notes below for concerns specific to each one.",
    decommissioned: "Marked decommissioned or do-not-use on Flock\u2019s portal. No current custodian of record \u2014 who still holds credentials to query this data is unknown.",
    test: "Test or demo account. Access controls are unknown and there is no agency of record accountable for queries against this data.",
  };

  function refineKind(kind, name) {
    if (kind !== 'private') return kind;
    const nm = (name || '').toLowerCase();
    return (nm.indexOf('university') >= 0 || nm.indexOf('college') >= 0)
      ? 'private_university' : 'private_entity';
  }

  // Maps a stats-table metric key to the corresponding transparency
  // checklist id. When the agency doesn't publish a given stat, we
  // use this to look up the peer publish-rate and show it as a
  // "X% of peers report this" hint next to "not reported".
  const TRANSPARENCY_CHECK_FOR_METRIC = {
    cameras: "camera_count",
    vehicles_30d: "vehicles_30d",
    hotlist_hits_30d: "hotlist_hits",
    searches_30d: "searches_30d",
    retention_days: "retention",
  };

  const METRIC_LABELS = {
    cameras: "Cameras",
    vehicles_30d: "Vehicles detected (30d)",
    hotlist_hits_30d: "Hotlist hits (30d)",
    searches_30d: "Searches (30d)",
    outbound: "Agencies it shares to",
  };

  // Meeting banner data + helpers live in docs/js/meeting_banners.js
  // (loaded from report.html before this script).

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtInt(n) {
    if (n == null) return '<span class="null">not reported</span>';
    return Number(n).toLocaleString();
  }

  function fmtNum(n, digits) {
    if (n == null) return '<span class="null">&mdash;</span>';
    return Number(n).toLocaleString(undefined, {
      minimumFractionDigits: digits || 0,
      maximumFractionDigits: digits || 0,
    });
  }

  function pct(part, total) {
    if (!total) return "0";
    return Math.round((100 * part) / total).toString();
  }

  function agencyTypeLabel(type) {
    return AGENCY_TYPE_LABELS[type] || (type ? type.replace(/_/g, " ") : "agency");
  }

  function peerGroupDescription(peerType, peerTotal) {
    if (peerType === "all") {
      return `of ${peerTotal} California agencies`;
    }
    return `of ${peerTotal} California ${agencyTypeLabel(peerType)} agencies`;
  }

  // ── Main render ──
  function render(data, slug) {
    const report = (data.reports || {})[slug];
    const meta = data.metadata || {};
    const container = document.getElementById("report");

    if (!report) {
      container.innerHTML = renderNotFound(slug, data);
      return;
    }

    document.title = `${report.name} — ALPR Scorecard`;

    let html = "";
    html += renderMeetingBanner(report);
    html += renderHeader(report);
    html += renderStats(report, meta);
    html += renderSB34Checklist(report, meta);
    html += renderTransparencyChecklist(report, meta);
    html += renderSharing(report);
    html += renderRegional(report, meta);
    html += renderLegalSummary(report, meta);
    html += renderQuestions(report, meta);
    html += renderFooter(report, meta);
    container.innerHTML = html;
  }

  function renderNotFound(slug, data) {
    const agencyList = Object.keys(data.reports || {}).sort().slice(0, 10);
    return `
      <div class="error-box">
        <h1 style="margin-top:0">Agency not found</h1>
        <p>No report available for <code>${escapeHtml(slug)}</code>.</p>
        <p>Use the <a href="sharing_map.html">interactive map</a> to find an agency,
        or append <code>?agency=&lt;slug&gt;</code> to this URL.</p>
        <p class="muted">Example slugs: ${agencyList.map(s => `<a href="?agency=${escapeHtml(s)}">${escapeHtml(s)}</a>`).join(", ")}${agencyList.length >= 10 ? ", ..." : ""}</p>
      </div>
    `;
  }

  // ── Meeting banner (time-limited, per-agency) ──
  function renderMeetingBanner(report) {
    if (typeof window.renderMeetingBannerHtml !== "function") return "";
    return window.renderMeetingBannerHtml([report.agency_id, report.slug, report.name]);
  }

  // ── Header ──
  function renderHeader(report) {
    const roleLabel = report.agency_role ? report.agency_role : "";
    const typeLabel = agencyTypeLabel(report.agency_type);
    const combinedType = [typeLabel, roleLabel].filter(Boolean).join(" — ");
    const geoName = (report.geo && report.geo.name) || "";
    const thisUrlAbs = new URL(`report.html?agency=${report.slug}`, location.href).toString();
    const topMapUrlAbs = new URL(`sharing_map.html#${report.slug}`, location.href).toString();

    // Header layout: name/subtitle on the left, two QRs on the right
    // (live report + interactive sharing map). Both link via wrapping
    // anchor (clickable on screen) and scan from print.
    let html = '<div class="report-header">';
    html += '<div class="report-header-main">';
    html += `<h1>${escapeHtml(report.name)}</h1>`;
    html += `<p class="subtitle">ALPR Scorecard &middot; Flock transparency data &middot; generated ${new Date().toLocaleDateString("en-US", {year: "numeric", month: "long", day: "numeric"})}</p>`;
    html += '</div>';
    html += '<div class="report-header-qr">';
    html += '<div class="report-header-qr-block">';
    html += '<div id="top-qrcode" aria-label="QR code linking to the live online version of this report"></div>';
    html += '<div class="qr-caption">Scan for live version</div>';
    html += '</div>';
    html += '<div class="report-header-qr-block">';
    html += '<div id="top-map-qrcode" aria-label="QR code linking to the interactive sharing map"></div>';
    html += '<div class="qr-caption">Scan for sharing map</div>';
    html += '</div>';
    html += '</div>';
    html += '</div>';
    // Defer QR render until the HTML is inserted.
    setTimeout(function() {
      renderQrCode("top-qrcode", thisUrlAbs, { size: 90 });
      renderQrCode("top-map-qrcode", topMapUrlAbs, { size: 90 });
    }, 0);

    if (!report.crawled) {
      html += `<div class="no-data-box">
        <strong>This agency does not publish a Flock transparency page.</strong>
        The information below is derived from sharing lists on <em>other</em> agencies' transparency pages.
        Cameras, retention policy, and 30-day activity are not knowable without their own transparency page.
      </div>`;
    }

    html += '<div class="meta-grid">';
    const addMeta = (label, value, missing) => {
      if (value == null || value === "") {
        if (missing) {
          html += `<div><span class="label">${label}:</span> <span class="value missing">${missing}</span></div>`;
        }
      } else {
        html += `<div><span class="label">${label}:</span> <span class="value">${escapeHtml(value)}</span></div>`;
      }
    };
    addMeta("Agency type", combinedType);
    addMeta("State", report.state);
    // Only show Location when we have a specific place/county name —
    // otherwise the line becomes ", CA" (blank name + state suffix),
    // which is worse than nothing. The "State" row already covers the
    // state-only case.
    if (geoName) {
      const stateSuffix = report.geo && report.geo.state ? `, ${report.geo.state}` : "";
      addMeta("Location", geoName + stateSuffix);
    }
    // Agency types that don't correspond to a geographic population —
    // per-capita metrics don't apply to these (campus PDs serve students,
    // not residents; vendors, fusion centers, state/federal agencies
    // operate across many populations). Relies on registry's
    // agency_type; mis-classified entries (e.g. a community college
    // district typed as "city") will show "Not available" until the
    // registry is corrected.
    const NON_GEOGRAPHIC_TYPES = new Set([
      "university", "college", "community_college",
      "fusion_center", "federal", "state",
      "transit", "school_district",
      "vendor", "private", "community", "other", "test", "decommissioned",
    ]);
    const isNonGeographic = NON_GEOGRAPHIC_TYPES.has(report.agency_type);
    let popMissingNote = "Not available";
    if (isNonGeographic) {
      popMissingNote = "Not applicable (serves a non-geographic population)";
    }
    addMeta("Population (2023)", report.population ? Number(report.population).toLocaleString() : null, popMissingNote);
    // Land area from Census gazetteer (for per-sq-mi metrics context).
    // Only rendered when known — same reasoning as population.
    if (report.land_sqmi) {
      addMeta("Land area", `${Number(report.land_sqmi).toLocaleString(undefined, { maximumFractionDigits: 1 })} sq mi`);
    }
    // Household vehicles (ACS B25046) — denominator for per-vehicle
    // rates. Caveat: excludes commercial fleets. Still the best
    // per-city vehicle count Census publishes.
    if (report.household_vehicles) {
      addMeta("Household vehicles", Number(report.household_vehicles).toLocaleString());
    }
    // "No transparency portal" is a meaningful compliance signal on its
    // own — color it red so it stands out in the metadata block.
    if (report.crawled) {
      addMeta("Transparency portal", "Yes");
    } else {
      html += `<div><span class="label">Transparency portal:</span> <span class="value" style="color:var(--flag); font-weight: bold">No</span></div>`;
    }
    addMeta("Last crawled", report.crawled_date);

    html += '</div>';

    if (report.notes) {
      html += `<p class="legal-note">${report.notes}</p>`;
    }

    // Agency-specific data concerns: documented discrepancies between
    // what the agency publishes and what other records show. Section-
    // tagged concerns render inline next to the relevant stat/check
    // (with a warning-triangle marker in the stat header). Only
    // untagged/general concerns render here, since they have no other
    // home.
    if (report.data_concerns && report.data_concerns.length) {
      const generalConcerns = report.data_concerns.filter(function(c) {
        return !c.section || c.section === "general";
      });
      if (generalConcerns.length) {
        html += '<div class="data-concerns">';
        generalConcerns.forEach(function(c) {
          html += renderDataConcernBody(c);
        });
        html += '</div>';
      }
    }

    return html;
  }

  // ── Stats ──
  // ── Metric-block layout helpers ────────────────────────────────
  //
  // The 30-Day Activity section used to be a three-column table. With
  // the metric-block layout each metric is a self-contained block with
  // a title, up to two data cells (left/right), optional caveats, and
  // rank pills that embed tiny sparklines. Lets each metric shape its
  // right-side cell differently — e.g. cameras shows a stack of per-X
  // rates, "searches reaching this data" shows a top-searchers bar
  // chart, "agencies it shares to" shows reach stats.

  // Small inline histogram (peer distribution) with a red marker at
  // the agency's value. Narrow enough to embed next to a rank number
  // inside a rank pill.
  function inlinePillSparkSvg(hist, value, markerColor) {
    if (!hist || !hist.bins || !hist.bins.length) return "";
    const bins = hist.bins;
    const mn = hist.min, mx = hist.max;
    const W = 68, H = 16, PAD = 1;
    const barW = (W - 2 * PAD) / bins.length;
    const maxCount = bins.reduce(function(m, c) { return c > m ? c : m; }, 0) || 1;
    const barMaxH = H - 3;
    let svg = `<svg class="pill-spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">`;
    for (let i = 0; i < bins.length; i++) {
      const h = Math.max(1.5, barMaxH * bins[i] / maxCount);
      const x = PAD + i * barW;
      const y = H - 1.5 - h;
      svg += `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${Math.max(1, barW - 0.4).toFixed(2)}" height="${h.toFixed(2)}" fill="currentColor" opacity="0.45"/>`;
    }
    if (value != null && mx > mn) {
      let t = (value - mn) / (mx - mn);
      if (t < 0) t = 0;
      if (t > 1) t = 1;
      const x = PAD + t * (W - 2 * PAD);
      const stroke = markerColor || "#dc2626";
      svg += `<line x1="${x.toFixed(2)}" x2="${x.toFixed(2)}" y1="0" y2="${H}" stroke="${stroke}" stroke-width="1.5"/>`;
    }
    svg += `</svg>`;
    return svg;
  }

  // Renders a rank pill showing a scope tag (STATE / COUNTY / 25 mi),
  // the rank phrase ("higher than most"), percentile, peer median,
  // and an embedded mini sparkline. Color class comes from
  // rankDescription so the whole pill reads as concerning / neutral
  // consistently.
  function rankPillHtml(opts) {
    const { scopeLabel, pctile, median: med, sample, hist, value, markerColor, atMax, peerNouns } = opts;
    if (pctile == null) return "";
    const plu = (peerNouns && peerNouns.plu) || "peers";
    const sing = (peerNouns && peerNouns.sing) || "peer";
    let cls = "";
    let phrase;
    if (atMax) { phrase = `at ${sing} maximum`; cls = "concern strong"; }
    else if (pctile >= 90) { phrase = `higher than nearly all ${plu}`; cls = "concern strong"; }
    else if (pctile >= 75) { phrase = `higher than most ${plu}`; cls = "concern"; }
    else if (pctile >= 60) { phrase = `above typical ${sing}`; cls = "concern-mild"; }
    else if (pctile >= 40) { phrase = `near ${sing} median`; cls = ""; }
    else { phrase = `below ${sing} median`; cls = ""; }
    const spark = inlinePillSparkSvg(hist, value, markerColor);
    const medHtml = med != null ? ` <span class="pill-med">med ${fmtNumSmart(med)}</span>` : "";
    return `<span class="rank-pill ${cls}">` +
      `<span class="scope">${escapeHtml(scopeLabel)}</span> ` +
      `<span class="pill-phrase">${phrase}</span> ` +
      `<span class="pill-pctile">${pctile}${nthSuffix(pctile)}</span>` +
      medHtml +
      (spark ? ` ${spark}` : "") +
      `</span>`;
  }

  // Renders the two rank pills (statewide + local) side by side.
  // Returns an empty string if neither rank is available.
  function rankPillsForMetric(opts) {
    const {
      pctile, med, lpctile, lmed, lsamp, sparkHist, value, agencyType, meta, sparkMetricKey, statePeer,
    } = opts;
    if (pctile == null && lpctile == null) return "";
    const hist = sparkHist || peerHistogramFor(sparkMetricKey, agencyType, meta);
    const stateNouns = peerNounsFor(statePeer);
    let html = '<div class="rank-pill-row">';
    if (pctile != null) {
      // Scope label names the comparison pool concretely — "vs
      // counties" not "vs state" — so readers don't have to wonder
      // "peers of what kind?" and don't assume we're comparing
      // counties against cities.
      const scopeLabel = stateNouns ? `vs ${stateNouns.plu}` : "vs state";
      html += rankPillHtml({
        scopeLabel: scopeLabel,
        pctile: pctile, median: med, hist: hist, value: value,
        peerNouns: stateNouns,
      });
    }
    if (lpctile != null) {
      const scopeLabel = lsamp && lsamp.scope === "county" ? "vs county" : "vs 25 mi";
      html += rankPillHtml({
        scopeLabel: scopeLabel,
        pctile: lpctile, median: lmed, hist: hist, value: value,
      });
    }
    html += "</div>";
    return html;
  }

  // A single stats cell (raw or per-capita or whatever else) — label
  // header, big value, optional extras (caveats, additional rates),
  // and rank pills at the bottom.
  function statsCellHtml(opts) {
    const {
      cellClass = "",
      label,
      value,
      valueSuffix = "",
      valueIsNotReported = false,
      notReportedHint = "",
      extrasHtml = "",
      rankPillsHtml: pillsHtml = "",
      concernClass = "",
    } = opts;

    let html = `<div class="metric-cell ${cellClass} ${concernClass}">`;
    if (label) html += `<div class="cell-label">${escapeHtml(label)}</div>`;
    if (valueIsNotReported) {
      html += `<div class="cell-value not-reported">not reported</div>`;
      if (notReportedHint) html += notReportedHint;
    } else if (value == null) {
      html += `<div class="cell-value cell-na"><span class="null">&mdash;</span></div>`;
    } else {
      const display = typeof value === "number" && value < 10 && value % 1 !== 0
        ? fmtNum(value, 2)
        : (typeof value === "number" ? fmtInt(value) : value);
      html += `<div class="cell-value">${display}${valueSuffix ? `<span class="cell-value-suffix">${valueSuffix}</span>` : ""}</div>`;
    }
    if (extrasHtml) html += extrasHtml;
    if (pillsHtml) html += pillsHtml;
    html += `</div>`;
    return html;
  }

  // Wraps a set of cells in a full metric block with title/subtitle
  // header. `concern` adds a red left bar; `concernMild` adds an
  // amber left bar (used when the metric is misleading rather than
  // worrying — e.g., rank is artificially low because the agency
  // didn't publish a required input).
  function metricBlockHtml(opts) {
    const {
      title,
      subtitle = "",
      titleTooltip = "",
      concern = false,
      concernMild = false,
      cellsHtml,
      caveatHtml: caveat = "",
      inlineConcernHtml: concernHtml = "",
    } = opts;

    const hasConcern = !!concernHtml;
    const concernCls = concern ? " concern" : (concernMild ? " concern-mild" : "");
    let html = `<div class="metric-block${concernCls}${hasConcern ? " has-data-concern" : ""}">`;
    html += `<div class="metric-head">`;
    if (hasConcern) html += `<span class="concern-flag" aria-label="Has documented concern">\u26A0</span> `;
    html += `<span class="metric-title"${titleTooltip ? ` title="${escapeHtml(titleTooltip)}"` : ""}>${escapeHtml(title)}</span>`;
    if (subtitle) html += ` <span class="metric-subtitle">${escapeHtml(subtitle)}</span>`;
    html += `</div>`;
    if (caveat) html += `<div class="metric-caveat">${caveat}</div>`;
    html += `<div class="metric-body">${cellsHtml}</div>`;
    if (concernHtml) html += concernHtml;
    html += `</div>`;
    return html;
  }

  // Build the "not reported" hint string for a metric where the agency
  // doesn't publish but its peers do. Returns empty if there's no
  // corresponding transparency check or the peer rate is 0.
  function notReportedHintFor(report, metric) {
    const checkId = TRANSPARENCY_CHECK_FOR_METRIC[metric];
    if (!checkId) return "";
    const item = (report.checklist_transparency || []).find(function(x) { return x.id === checkId; });
    if (!item || !item.peer_applicable) return "";
    const p = Math.round(100 * item.peer_count / item.peer_applicable);
    const peerGroup = item.peer_type === "all" ? "California agencies" : `California ${agencyTypeLabel(item.peer_type)} agencies`;
    return `<div class="not-reported-hint">${p}% of ${peerGroup} with a transparency portal publish this field.</div>`;
  }

  // Short name for personalising prose — "San Mateo CA PD" → "San
  // Mateo", "San Mateo County CA SO" → "San Mateo County", "NCRIC"
  // stays "NCRIC". Prefers the Census place/county name from the geo
  // block; falls back to stripping agency-role suffixes from the
  // display name.
  function shortAgencyName(report) {
    const geo = report.geo || {};
    if (geo.name) {
      if (geo.kind === "county" && !/\bCounty$/i.test(geo.name)) {
        return geo.name + " County";
      }
      return geo.name;
    }
    let n = report.name || "";
    n = n.replace(/\s+(CA|NV|AZ|OR|WA|ID|UT|NY|TX|FL|NH|MA|CT|NJ|PA|MD|VA|NC|SC|GA|OH|MI|IL|IN|KY|TN|AL|MS|LA|AR|OK|MO|KS|NE|IA|MN|WI|ND|SD|MT|WY|CO|NM|AK|HI|ME|VT|RI|DE|WV|DC)\s+(PD|SO|SD|DA|FD|DPS|Police|Sheriff|Police Department|Sheriff'?s? Office|District Attorney)\b.*$/i, "").trim();
    return n || report.name || "This agency";
  }

  function renderStats(report, meta) {
    if (!report.crawled) return "";

    const stats = report.stats || {};
    const per1k = report.per_1000 || {};
    const perVeh = report.per_1000_vehicles || {};
    const medians = report.medians || {};
    const percentiles = report.percentiles || {};
    const peerSample = report.peer_sample || {};
    const per1kPct = report.percentiles_per_1000 || {};
    const per1kMed = report.medians_per_1000 || {};
    const per1kSample = report.peer_sample_per_1000 || {};
    const localPct = report.percentiles_local || {};
    const localMed = report.medians_local || {};
    const localSample = report.peer_sample_local || {};
    const localPerPct = report.percentiles_per_1000_local || {};
    const localPerMed = report.medians_per_1000_local || {};
    const short = shortAgencyName(report);

    let html = `<h2>30-Day Activity</h2>`;

    // Peer sample note above the blocks — explicitly names who we're
    // comparing to.
    const firstPeer = Object.values(peerSample)[0];
    if (firstPeer) {
      const fallback = firstPeer.fallback;
      const typeLabel = firstPeer.type === "all" ? "California agencies" : `California ${agencyTypeLabel(firstPeer.type)} agencies`;
      html += `<p class="muted" style="font-size:9.5pt">Ranked against <strong>${firstPeer.size} ${typeLabel}</strong> with transparency pages${fallback ? " (too few peers of the same type; all CA agencies)" : ""}. Red-bordered blocks flag above-median metrics; below-median ones stay neutral (a low peer rank doesn\u2019t mean the absolute number is acceptable).</p>`;
    }

    // ── Metric 0: Data retention policy ──────────────────────────
    // Not a 30-day activity stat, but fits the same peer-compare
    // layout. Most agencies report 30 days, so anything higher lands
    // in the top of the distribution — the sparkline marker goes red
    // only for outliers (pctile ≥ 75) and gray otherwise so 30-day
    // baselines don't visually flag.
    {
      const v = stats.retention_days;
      const notReported = v == null;
      const pctile = percentiles.retention_days;
      const med = medians.retention_days;
      const sample = peerSample.retention_days;
      const hist = peerHistogramFor("retention_days", report.agency_type, meta);
      // Tied-at-max detection: the rank-based percentile counts
      // strictly-below values, so an agency tied with others at the
      // top of the distribution gets an 87th-ish pctile even though
      // nobody retains longer. Override the phrase when that happens.
      const atMax = hist && v != null && v === hist.max;
      const isOutlier = atMax || (pctile != null && pctile >= 75);
      const markerColor = isOutlier ? "#dc2626" : "#94a3b8";
      const retNouns = peerNounsFor(sample);
      const pillHtml = pctile != null
        ? `<div class="rank-pill-row">${rankPillHtml({
            scopeLabel: retNouns ? `vs ${retNouns.plu}` : "vs state",
            pctile: pctile, median: med, hist: hist, value: v,
            markerColor: markerColor, atMax: atMax,
            peerNouns: retNouns,
          })}</div>`
        : "";
      const rawCell = statsCellHtml({
        cellClass: "raw",
        concernClass: isOutlier ? "concern" : "",
        label: "Agency raw",
        value: v != null ? `${fmtInt(v)} days` : null,
        valueIsNotReported: notReported,
        notReportedHint: notReported ? notReportedHintFor(report, "retention_days") : "",
        rankPillsHtml: pillHtml,
      });
      html += metricBlockHtml({
        title: `${short}'s data retention policy`,
        subtitle: "how long ALPR data is kept before deletion",
        concern: isOutlier,
        cellsHtml: rawCell,
        inlineConcernHtml: concernsForSection(report, "stat:retention_days"),
      });
    }

    // ── Metric 1: Searches performed ─────────────────────────────
    {
      const v = stats.searches_30d;
      const notReported = v == null;
      const pctile = percentiles.searches_30d;
      const lpctile = localPct.searches_30d;
      const pills = rankPillsForMetric({
        pctile, med: medians.searches_30d,
        lpctile, lmed: localMed.searches_30d,
        lsamp: localSample.searches_30d,
        value: v, agencyType: report.agency_type, meta,
        sparkMetricKey: "searches_30d",
        statePeer: peerSample.searches_30d,
      });
      const rawCell = statsCellHtml({
        cellClass: "raw",
        concernClass: notReported ? "" : (cellClassFor(pctile, "searches_30d")),
        label: "Agency raw",
        value: v,
        valueIsNotReported: notReported,
        notReportedHint: notReported ? notReportedHintFor(report, "searches_30d") : "",
        rankPillsHtml: pills,
      });
      // Per-capita cell
      const per = per1k.searches_30d;
      const pvPills = rankPillsForMetric({
        pctile: per1kPct.searches_30d, med: per1kMed.searches_30d,
        lpctile: localPerPct.searches_30d, lmed: localPerMed.searches_30d,
        lsamp: localSample.searches_30d,
        value: per, agencyType: report.agency_type, meta,
        sparkMetricKey: "searches_30d_per_1000",
        statePeer: per1kSample.searches_30d,
      });
      const pvExtras = perVeh.searches_30d != null
        ? `<div class="per-vehicle"><strong>${fmtNum(perVeh.searches_30d, perVeh.searches_30d < 10 ? 2 : 0)}</strong> <span class="muted">per 1,000 household vehicles</span></div>`
        : "";
      const perCell = statsCellHtml({
        cellClass: "per-capita",
        concernClass: cellClassFor(per1kPct.searches_30d, "searches_30d"),
        label: "Per 1,000 residents",
        value: per,
        valueIsNotReported: notReported,
        extrasHtml: pvExtras,
        rankPillsHtml: pvPills,
      });
      html += metricBlockHtml({
        title: `Searches performed by ${short}`,
        subtitle: "last 30 days",
        concern: !notReported && (pctile >= 60 || lpctile >= 60),
        concernMild: notReported,
        cellsHtml: rawCell + perCell,
        inlineConcernHtml: concernsForSection(report, "stat:searches_30d"),
      });
    }

    // ── Metric 2: Searches reaching this data (downstream) ───────
    {
      const v = report.downstream_total;
      const pctile = report.percentile_downstream;
      const lpctile = report.percentile_downstream_local;
      // Suppress rank pills + sparkline entirely when the agency
      // hasn't published a sharing list — the downstream total is
      // missing recipient contributions, so any rank is a comparison
      // against an incomplete number and would mislead.
      const noSharingList = !report.downstream_searches;
      const pills = noSharingList ? "" : rankPillsForMetric({
        pctile, med: report.median_downstream,
        lpctile, lmed: report.median_downstream_local,
        lsamp: report.peer_sample_downstream_local,
        value: v, agencyType: report.agency_type, meta,
        sparkMetricKey: "downstream",
        statePeer: report.peer_sample_downstream,
      });
      // Coverage caveat
      let coverage = "";
      if (report.downstream_searches) {
        const ds = report.downstream_searches;
        if (ds.recipients_total > 0) {
          const pctStr = pct(ds.recipients_with_data, ds.recipients_total);
          const notCounted = [];
          if (ds.recipients_no_portal > 0) {
            notCounted.push(`${fmtInt(ds.recipients_no_portal)} have no transparency portal`);
          }
          if (ds.recipients_portal_no_search_field > 0) {
            notCounted.push(`${fmtInt(ds.recipients_portal_no_search_field)} have a portal but don\u2019t publish a search count`);
          }
          const parts = [`<strong>Based on ${pctStr}%</strong> of ${fmtInt(ds.recipients_total)} recipients.`];
          if (notCounted.length) parts.push("Not counted: " + notCounted.join("; ") + ".");
          if (ds.self_included === false) parts.push("Agency also doesn\u2019t publish its own.");
          parts.push("<em>Real total likely higher.</em>");
          coverage = `<div class="coverage-tag">${parts.join(" ")}</div>`;
        }
      } else {
        // Agency doesn't publish an outbound sharing list, so no
        // recipients contribute to the downstream total — the rank
        // reflects only their own searches and is not comparable to
        // peers who do publish. Flag this so a low percentile doesn't
        // read as "small network footprint" when it's really
        // "undisclosed network footprint."
        coverage = `<div class="coverage-tag" style="color:var(--flag)"><strong>No published sharing list.</strong> Total reflects only this agency\u2019s own searches; downstream queries from recipients can\u2019t be counted. Rank understates actual reach.</div>`;
      }
      const downstreamDisplay = noSharingList
        ? `<span style="color:#9ca3af">${fmtInt(v)}</span>`
        : v;
      const rawCell = statsCellHtml({
        cellClass: "raw",
        concernClass: noSharingList ? "" : cellClassFor(pctile, "downstream"),
        label: "Combined total",
        value: downstreamDisplay,
        extrasHtml: coverage,
        rankPillsHtml: pills,
      });
      // Right cell: top searchers bar chart
      // topResearchersHtml already renders its own header — don't
      // duplicate it with a cell-label.
      const rightCell = `<div class="metric-cell downstream-researchers">${topResearchersHtml(report.downstream_searches)}</div>`;
      html += metricBlockHtml({
        title: `Searches reaching ${short}'s data`,
        subtitle: `last 30 days · ${short} + its recipients`,
        titleTooltip: report.downstream_searches
          ? `${fmtInt(v || 0)} = searches this agency publishes + searches published by ${report.downstream_searches.recipients_with_data} of its ${report.downstream_searches.recipients_total} recipients.`
          : "",
        concern: !noSharingList && pctile != null && pctile >= 60,
        concernMild: noSharingList,
        cellsHtml: rawCell + rightCell,
        inlineConcernHtml: concernsForSection(report, "stat:downstream"),
      });
    }

    // ── Metric 3: Agencies it shares to ──────────────────────────
    {
      const v = stats.outbound_count;
      const pctile = percentiles.outbound;
      const lpctile = localPct.outbound;
      // 0 outbound is ambiguous: "we share with nobody" (good) or
      // "we don't publish our sharing list" (transparency gap). When
      // the agency hasn't published an outbound list, suppress the
      // peer rank (the "0" isn't comparable) and amber-flag the
      // block so a 0 doesn't read as compliant.
      const noSharingList = !report.downstream_searches;
      const pills = noSharingList ? "" : rankPillsForMetric({
        pctile, med: medians.outbound,
        lpctile, lmed: localMed.outbound,
        lsamp: localSample.outbound,
        value: v, agencyType: report.agency_type, meta,
        sparkMetricKey: "outbound",
        statePeer: peerSample.outbound,
      });
      // Peer-publish rate — how many peers actually publish an
      // outbound sharing list. Gives the reader context for whether
      // "not publishing" is common baseline or an outlier.
      const outList = (report.checklist_transparency || []).find(
        function(x) { return x.id === "outbound_list"; }
      );
      let peerPublishNote = "";
      if (noSharingList && outList && outList.peer_applicable) {
        const p = Math.round(100 * outList.peer_count / outList.peer_applicable);
        const peerGroup = outList.peer_type === "all"
          ? "CA agencies"
          : `CA ${agencyTypeLabel(outList.peer_type)} agencies`;
        peerPublishNote = ` <span class="muted">(${p}% of ${peerGroup} with a transparency portal do publish one.)</span>`;
      }
      const noListNote = noSharingList
        ? `<div class="coverage-tag" style="color:var(--flag)"><strong>No published sharing list.</strong> Count is unknown, not zero. Inferred edges may still appear in the recipient table below.${peerPublishNote}</div>`
        : "";
      // Show "N/A" when the list is unpublished — a bare "0" reads as
      // "shares with nobody" (compliant), which inverts the signal.
      const displayValue = noSharingList
        ? `<span style="color:#9ca3af; font-size:14pt">N/A</span>`
        : v;
      const rawCell = statsCellHtml({
        cellClass: "raw",
        concernClass: noSharingList ? "" : cellClassFor(pctile, "outbound"),
        label: "Agency raw",
        value: displayValue,
        extrasHtml: noListNote,
        rankPillsHtml: pills,
      });
      // Right cell: reach metrics + state/local comparison pills so
      // the reader sees how this agency's geographic reach stacks up.
      const reachPcts = report.reach_percentiles || {};
      const reachMeds = report.reach_medians || {};
      const reachPctsLocal = report.reach_percentiles_local || {};
      const reachMedsLocal = report.reach_medians_local || {};
      const reachSampleLocal = report.reach_peer_samples_local || {};
      function reachPillsFor(kind, value) {
        return rankPillsForMetric({
          pctile: reachPcts[kind],
          med: reachMeds[kind] != null ? kmToMi(reachMeds[kind]) : null,
          lpctile: reachPctsLocal[kind],
          lmed: reachMedsLocal[kind] != null ? kmToMi(reachMedsLocal[kind]) : null,
          lsamp: reachSampleLocal[kind],
          value: value, agencyType: report.agency_type, meta,
          sparkMetricKey: null,
        });
      }
      const bits = [];
      if (report.outbound_avg_km != null) {
        const avgMi = kmToMi(report.outbound_avg_km);
        bits.push(
          `<div><strong>${fmtNum(avgMi, 0)}</strong> <span class="muted">mi &mdash; avg distance to a recipient</span>` +
          reachPillsFor("avg", avgMi) +
          `</div>`
        );
      }
      if (report.farthest_outbound) {
        const farMi = kmToMi(report.farthest_outbound.distance_km);
        const stateSuffix = report.farthest_outbound.state && report.farthest_outbound.state !== report.state
          ? ` (${escapeHtml(report.farthest_outbound.state)})` : "";
        bits.push(
          `<div><strong>${fmtNum(farMi, 0)}</strong> <span class="muted">mi &mdash; farthest:</span> ${escapeHtml(report.farthest_outbound.name)}${stateSuffix}` +
          reachPillsFor("far", farMi) +
          `</div>`
        );
      }
      // When the agency doesn't publish its outbound list, the reach
      // cell becomes meaningless (no distances to measure). Replace it
      // with the count of inferred edges — agencies that NAME this one
      // in their own inbound list. Those inferred edges are our only
      // evidence of actual sharing partners when the agency withholds.
      let reachCell;
      if (noSharingList) {
        const inferredCount = (report.outbound || []).filter(function(e) { return e.inferred; }).length;
        const inferredHtml = inferredCount > 0
          ? `<div><strong>${fmtInt(inferredCount)}</strong> other ${inferredCount === 1 ? "agency claims" : "agencies claim"} to receive data from ${escapeHtml(short)}.</div><div class="muted" style="margin-top:4px">From those agencies\u2019 published inbound lists. Actual count is likely higher — most agencies only publish outbound lists.</div>`
          : `<div class="muted">No agencies\u2019 inbound lists name ${escapeHtml(short)} as a source. Actual sharing partners are unknown.</div>`;
        reachCell = `<div class="metric-cell reach"><div class="cell-label">Inferred edges</div>${inferredHtml}</div>`;
      } else {
        reachCell = `<div class="metric-cell reach"><div class="cell-label">Reach</div><div class="reach-lines">${bits.join("")}</div></div>`;
      }
      html += metricBlockHtml({
        title: `Agencies ${short} shares to`,
        concern: !noSharingList && pctile != null && pctile >= 60,
        concernMild: noSharingList,
        cellsHtml: rawCell + reachCell,
        inlineConcernHtml: concernsForSection(report, "stat:outbound"),
      });
    }

    // ── Metric 4: Cameras ────────────────────────────────────────
    {
      const v = stats.cameras;
      const pctile = percentiles.cameras;
      const lpctile = localPct.cameras;
      const rawPills = rankPillsForMetric({
        pctile, med: medians.cameras,
        lpctile, lmed: localMed.cameras,
        lsamp: localSample.cameras,
        value: v, agencyType: report.agency_type, meta,
        sparkMetricKey: "cameras",
        statePeer: peerSample.cameras,
      });
      const rawCell = statsCellHtml({
        cellClass: "raw",
        concernClass: cellClassFor(pctile, "cameras"),
        label: "Agency raw",
        value: v,
        rankPillsHtml: rawPills,
      });
      // Right cell: density rates (per-capita, per-vehicle, per-sqmi)
      // with rank pills for density (per-sqmi) since we have peer data
      const densityLines = [];
      if (per1k.cameras != null) {
        densityLines.push(`<div><strong>${fmtNum(per1k.cameras, per1k.cameras < 10 ? 2 : 0)}</strong> <span class="muted">per 1,000 residents</span></div>`);
      }
      if (perVeh.cameras != null) {
        densityLines.push(`<div><strong>${fmtNum(perVeh.cameras, perVeh.cameras < 10 ? 2 : 0)}</strong> <span class="muted">per 1,000 household vehicles</span></div>`);
      }
      if (report.cameras_per_sqmi != null) {
        densityLines.push(`<div><strong>${fmtNum(report.cameras_per_sqmi, report.cameras_per_sqmi < 10 ? 2 : 0)}</strong> <span class="muted">per square mile</span></div>`);
      }
      const densityPills = rankPillsForMetric({
        pctile: report.percentile_density, med: report.median_density,
        lpctile: report.percentile_density_local, lmed: report.median_density_local,
        lsamp: report.peer_sample_density_local,
        value: report.cameras_per_sqmi, agencyType: report.agency_type, meta,
        sparkMetricKey: "cameras_per_sqmi",
        statePeer: report.peer_sample_density,
      });
      const densityCell = `<div class="metric-cell density ${cellClassFor(report.percentile_density, "cameras_per_sqmi")}"><div class="cell-label">Density</div><div class="reach-lines">${densityLines.join("")}</div>${densityPills}</div>`;
      html += metricBlockHtml({
        title: `${short}'s cameras`,
        concern: (pctile != null && pctile >= 60) || (report.percentile_density != null && report.percentile_density >= 60),
        cellsHtml: rawCell + densityCell,
        inlineConcernHtml: concernsForSection(report, "stat:cameras"),
      });
    }

    // ── Metric 5: Vehicles detected ──────────────────────────────
    {
      const v = stats.vehicles_30d;
      const pctile = percentiles.vehicles_30d;
      const lpctile = localPct.vehicles_30d;
      const pills = rankPillsForMetric({
        pctile, med: medians.vehicles_30d,
        lpctile, lmed: localMed.vehicles_30d,
        lsamp: localSample.vehicles_30d,
        value: v, agencyType: report.agency_type, meta,
        sparkMetricKey: "vehicles_30d",
        statePeer: peerSample.vehicles_30d,
      });
      const rawCell = statsCellHtml({
        cellClass: "raw",
        concernClass: cellClassFor(pctile, "vehicles_30d"),
        label: "Agency raw",
        value: v,
        rankPillsHtml: pills,
      });
      const per = per1k.vehicles_30d;
      const pvPills = rankPillsForMetric({
        pctile: per1kPct.vehicles_30d, med: per1kMed.vehicles_30d,
        lpctile: localPerPct.vehicles_30d, lmed: localPerMed.vehicles_30d,
        lsamp: localSample.vehicles_30d,
        value: per, agencyType: report.agency_type, meta,
        sparkMetricKey: "vehicles_30d_per_1000",
        statePeer: per1kSample.vehicles_30d,
      });
      const pvExtras = perVeh.vehicles_30d != null
        ? `<div class="per-vehicle"><strong>${fmtNum(perVeh.vehicles_30d, perVeh.vehicles_30d < 10 ? 2 : 0)}</strong> <span class="muted">per 1,000 household vehicles</span></div>`
        : "";
      const perCell = statsCellHtml({
        cellClass: "per-capita",
        concernClass: cellClassFor(per1kPct.vehicles_30d, "vehicles_30d"),
        label: "Per 1,000 residents",
        value: per,
        extrasHtml: pvExtras,
        rankPillsHtml: pvPills,
      });
      html += metricBlockHtml({
        title: `Vehicles detected by ${short}`,
        subtitle: "last 30 days",
        concern: (pctile != null && pctile >= 60) || (per1kPct.vehicles_30d != null && per1kPct.vehicles_30d >= 60),
        cellsHtml: rawCell + perCell,
        inlineConcernHtml: concernsForSection(report, "stat:vehicles_30d"),
      });
    }

    // ── Metric 6: Hotlist hits ───────────────────────────────────
    {
      const v = stats.hotlist_hits_30d;
      const pctile = percentiles.hotlist_hits_30d;
      const lpctile = localPct.hotlist_hits_30d;
      const pills = rankPillsForMetric({
        pctile, med: medians.hotlist_hits_30d,
        lpctile, lmed: localMed.hotlist_hits_30d,
        lsamp: localSample.hotlist_hits_30d,
        value: v, agencyType: report.agency_type, meta,
        sparkMetricKey: "hotlist_hits_30d",
        statePeer: peerSample.hotlist_hits_30d,
      });
      const rawCell = statsCellHtml({
        cellClass: "raw",
        concernClass: cellClassFor(pctile, "hotlist_hits_30d"),
        label: "Agency raw",
        value: v,
        rankPillsHtml: pills,
      });
      const per = per1k.hotlist_hits_30d;
      const pvPills = rankPillsForMetric({
        pctile: per1kPct.hotlist_hits_30d, med: per1kMed.hotlist_hits_30d,
        lpctile: localPerPct.hotlist_hits_30d, lmed: localPerMed.hotlist_hits_30d,
        lsamp: localSample.hotlist_hits_30d,
        value: per, agencyType: report.agency_type, meta,
        sparkMetricKey: "hotlist_hits_30d_per_1000",
        statePeer: per1kSample.hotlist_hits_30d,
      });
      const pvExtras = perVeh.hotlist_hits_30d != null
        ? `<div class="per-vehicle"><strong>${fmtNum(perVeh.hotlist_hits_30d, perVeh.hotlist_hits_30d < 10 ? 2 : 0)}</strong> <span class="muted">per 1,000 household vehicles</span></div>`
        : "";
      const perCell = statsCellHtml({
        cellClass: "per-capita",
        concernClass: cellClassFor(per1kPct.hotlist_hits_30d, "hotlist_hits_30d"),
        label: "Per 1,000 residents",
        value: per,
        extrasHtml: pvExtras,
        rankPillsHtml: pvPills,
      });
      const hotlistCaveat = `A hit is a notification, not a confirmed crime. Agencies subscribe to watchlists — FBI NCIC (stolen vehicles, felony wants, missing persons) or custom lists the operator maintains — and a camera pings when it sees a plate on one of them.`;
      const subscribed = (report.stats && report.stats.hotlists_alerted_on) || [];
      const subscribedHtml = subscribed.length
        ? `<div class="hotlist-subscriptions"><span class="muted">Subscribed to:</span> ${subscribed.map(escapeHtml).join(", ")}</div>`
        : "";
      html += metricBlockHtml({
        title: `${short}'s hotlist hits`,
        subtitle: "last 30 days",
        concern: (pctile != null && pctile >= 60) || (per1kPct.hotlist_hits_30d != null && per1kPct.hotlist_hits_30d >= 60),
        cellsHtml: rawCell + perCell,
        caveatHtml: hotlistCaveat + subscribedHtml,
        inlineConcernHtml: concernsForSection(report, "stat:hotlist_hits_30d"),
      });
    }

    return html;
  }

  // Tiny SVG sparkline of the statewide peer distribution. Each bar
  // represents a histogram bin; a small triangle marks where THIS
  // agency falls on the same axis. Helps the reader see "80th
  // percentile" intuitively without having to interpret the number.
  //
  // hist: {bins: [int,...], min, max} from metadata.sparkline_state
  // value: this agency's raw value (null renders no marker)
  function sparklineSvg(hist, value) {
    if (!hist || !hist.bins || !hist.bins.length) return "";
    const bins = hist.bins;
    const mn = hist.min, mx = hist.max;
    const W = 120, H = 22, PAD = 1;
    const barW = (W - 2 * PAD) / bins.length;
    const maxCount = bins.reduce(function(m, c) { return c > m ? c : m; }, 0) || 1;
    const barMaxH = H - 6;
    let svg = `<svg class="sparkline" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" aria-hidden="true">`;
    // Histogram bars
    for (let i = 0; i < bins.length; i++) {
      const h = Math.max(1, Math.round(barMaxH * bins[i] / maxCount));
      const x = PAD + i * barW;
      const y = H - 3 - h;
      svg += `<rect x="${x.toFixed(2)}" y="${y}" width="${Math.max(1, barW - 0.5).toFixed(2)}" height="${h}" fill="#cbd5e1"/>`;
    }
    // Agency marker: a vertical line + triangle at the top
    if (value != null && mx > mn) {
      let t = (value - mn) / (mx - mn);
      if (t < 0) t = 0;
      if (t > 1) t = 1;
      const x = PAD + t * (W - 2 * PAD);
      svg += `<line x1="${x.toFixed(2)}" x2="${x.toFixed(2)}" y1="0" y2="${H}" stroke="#dc2626" stroke-width="1.5"/>`;
      svg += `<polygon points="${(x-3).toFixed(2)},0 ${(x+3).toFixed(2)},0 ${x.toFixed(2)},4" fill="#dc2626"/>`;
    }
    svg += `</svg>`;
    return svg;
  }

  // Render the body of a single data_concern (title + description +
  // source link). Factored out so both the top-of-page block and the
  // inline anchored callouts use the same markup.
  function renderDataConcernBody(c, anchorId, inlineAgencyLabel) {
    let html = '<div class="data-concern"';
    if (anchorId) html += ` id="${escapeHtml(anchorId)}"`;
    html += '>';
    if (inlineAgencyLabel) {
      html += `<div class="data-concern-scope">${inlineAgencyLabel}-specific finding</div>`;
    }
    if (c.title) html += `<div class="data-concern-title">${escapeHtml(c.title)}</div>`;
    if (c.description) html += `<div class="data-concern-desc">${c.description}</div>`;
    if (c.source_url) {
      html += `<div class="data-concern-source"><a href="${escapeHtml(c.source_url)}" target="_blank" rel="noopener">Source</a></div>`;
    }
    html += '</div>';
    return html;
  }

  // Return the HTML for any data_concerns on `report` whose section
  // tag matches `sectionKey`. Each concern gets an anchor id matching
  // the top-of-page index so "↓ see below" links jump to the right
  // block. Concerns already rendered at the top (section="general" or
  // unset) are skipped.
  function concernsForSection(report, sectionKey) {
    if (!report.data_concerns || !report.data_concerns.length) return "";
    let html = "";
    const agencyLabel = escapeHtml(shortAgencyName(report) || report.name || "this agency");
    report.data_concerns.forEach(function(c, i) {
      if (!c.section || c.section === "general") return;
      if (c.section !== sectionKey) return;
      html += renderDataConcernBody(c, `concern-${i}`, agencyLabel);
    });
    if (!html) return "";
    return `<div class="data-concerns data-concerns-inline">${html}</div>`;
  }

  // Pick the right peer distribution: prefer this agency's type,
  // fall back to the all-CA bucket if the type-specific one is missing.
  function peerHistogramFor(metric, agencyType, meta) {
    const dists = meta && meta.sparkline_state && meta.sparkline_state[metric];
    if (!dists) return null;
    return dists[agencyType] || dists["all"] || null;
  }

  // Renders a single stats-table value cell: the primary number on top,
  // then statewide and local rank lines beneath (each with its peer
  // median). Collapses what used to be three columns (value, rank,
  // per-capita rank) into one cell per metric dimension.
  function valueBlockHtml(value, stateMed, localMed, statePctile, localPctile, localSample) {
    if (value == null) {
      // "not reported" is a transparency gap — the agency publishes
      // other stats but withheld this one. Color it like the other
      // amber warnings (missing-policy, no-portal) so it reads as a
      // signal, not neutral content.
      return '<span class="not-reported">not reported</span>';
    }
    const displayVal = typeof value === "number" && value < 10 && value % 1 !== 0
      ? fmtNum(value, 2)
      : fmtInt(value);
    let html = `<div class="value-primary">${displayVal}</div>`;
    if (statePctile != null) {
      html += `<div class="rank-line"><span class="rank-tag rank-tag-state">statewide:</span> ${rankDescription(statePctile)}`;
      if (stateMed != null) html += ` <span class="paren-median">&middot; median ${fmtNumSmart(stateMed)}</span>`;
      html += `</div>`;
    }
    if (localPctile != null) {
      const scopeLabel = localSample && localSample.scope === "county"
        ? "local (county):"
        : "local (25 miles):";
      html += `<div class="rank-line"><span class="rank-tag rank-tag-local">${scopeLabel}</span> ${rankDescription(localPctile)}`;
      if (localMed != null) html += ` <span class="paren-median">&middot; median ${fmtNumSmart(localMed)}</span>`;
      html += `</div>`;
    }
    return html;
  }

  // Format a number compactly: integers with commas, small decimals
  // with 2 places. Used for peer medians in the stats table so we
  // don't print "median 0" when the real number is 0.67.
  function fmtNumSmart(n) {
    if (n == null) return "";
    if (Math.abs(n) < 10 && n % 1 !== 0) return fmtNum(n, 2);
    return fmtInt(Math.round(n));
  }

  // California state outline, simplified to ~2KB. Sourced from the
  // PublicaMundi/MappingAPI us-states GeoJSON (public domain). Drawn
  // behind the recipient dots in the mini map for geographic
  // orientation — much more legible than a lat/lng grid. If the map
  // zooms out to show multi-state reach (El Cajon → Braintree MA),
  // CA appears as a small shape on the left and still serves as an
  // anchor. A full 50-state outline would be 80+ KB; CA-only is the
  // right tradeoff for the project's CA focus.
  const CA_OUTLINE = [[[-123.233256,42.006186],[-122.378853,42.011663],[-121.037003,41.995232],[-120.001861,41.995232],[-119.996384,40.264519],[-120.001861,38.999346],[-118.71478,38.101128],[-117.498899,37.21934],[-116.540435,36.501861],[-115.85034,35.970598],[-114.634459,35.00118],[-114.634459,34.87521],[-114.470151,34.710902],[-114.333228,34.448009],[-114.136058,34.305608],[-114.256551,34.174162],[-114.415382,34.108438],[-114.535874,33.933176],[-114.497536,33.697668],[-114.524921,33.54979],[-114.727567,33.40739],[-114.661844,33.034958],[-114.524921,33.029481],[-114.470151,32.843265],[-114.524921,32.755634],[-114.72209,32.717295],[-116.04751,32.624187],[-117.126467,32.536556],[-117.24696,32.668003],[-117.252437,32.876127],[-117.329114,33.122589],[-117.471515,33.297851],[-117.7837,33.538836],[-118.183517,33.763391],[-118.260194,33.703145],[-118.413548,33.741483],[-118.391641,33.840068],[-118.566903,34.042715],[-118.802411,33.998899],[-119.218659,34.146777],[-119.278905,34.26727],[-119.558229,34.415147],[-119.875891,34.40967],[-120.138784,34.475393],[-120.472878,34.448009],[-120.64814,34.579455],[-120.609801,34.858779],[-120.670048,34.902595],[-120.631709,35.099764],[-120.894602,35.247642],[-120.905556,35.450289],[-121.004141,35.461243],[-121.168449,35.636505],[-121.283465,35.674843],[-121.332757,35.784382],[-121.716143,36.195153],[-121.896882,36.315645],[-121.935221,36.638785],[-121.858544,36.6114],[-121.787344,36.803093],[-121.929744,36.978355],[-122.105006,36.956447],[-122.335038,37.115279],[-122.417192,37.241248],[-122.400761,37.361741],[-122.515777,37.520572],[-122.515777,37.783465],[-122.329561,37.783465],[-122.406238,38.15042],[-122.488392,38.112082],[-122.504823,37.931343],[-122.701993,37.893004],[-122.937501,38.029928],[-122.97584,38.265436],[-123.129194,38.451652],[-123.331841,38.566668],[-123.44138,38.698114],[-123.737134,38.95553],[-123.687842,39.032208],[-123.824765,39.366301],[-123.764519,39.552517],[-123.85215,39.831841],[-124.109566,40.105688],[-124.361506,40.259042],[-124.410798,40.439781],[-124.158859,40.877937],[-124.109566,41.025814],[-124.158859,41.14083],[-124.065751,41.442061],[-124.147905,41.715908],[-124.257444,41.781632],[-124.213628,42.000709],[-123.233256,42.006186]]];

  // Mini regional map: a lightweight inline SVG showing the agency's
  // outbound sharing footprint. No tiles, no basemap — just dots on
  // an equirectangular projection autofitted to the subject + all
  // geocoded recipients, with a CA state outline for orientation.
  //
  // Design:
  //   - subject: solid cyan dot with ring
  //   - flagged recipients (private/out-of-state/federal/fusion/etc):
  //     red dots, connected to subject with a red line
  //   - clean recipients: small gray dots
  //   - farthest recipient labeled
  //   - a few lat/lng gridlines drawn in very faint gray as scale
  //
  // Size is about 5.5" × 2.5" — fits under the reach-metrics sentence
  // without dominating the page.
  function miniMapHtml(report, subjLat, subjLng, farthest) {
    const recipients = (report.outbound || []).filter(function(r) {
      return r.lat != null && r.lng != null;
    });
    if (!recipients.length) return "";

    const MAX_W = 540, MAX_H = 320;
    const PAD_L = 6, PAD_R = 6, PAD_T = 6, PAD_B = 6;

    // Bounding box — include subject + every geocoded recipient, plus
    // a small cushion so dots at the edge aren't clipped.
    let minLat = subjLat, maxLat = subjLat;
    let minLng = subjLng, maxLng = subjLng;
    recipients.forEach(function(r) {
      if (r.lat < minLat) minLat = r.lat;
      if (r.lat > maxLat) maxLat = r.lat;
      if (r.lng < minLng) minLng = r.lng;
      if (r.lng > maxLng) maxLng = r.lng;
    });
    // Cushion relative to range so edges aren't clipped
    let latRange = Math.max(maxLat - minLat, 0.2);
    let lngRange = Math.max(maxLng - minLng, 0.2);
    minLat -= latRange * 0.05;
    maxLat += latRange * 0.05;
    minLng -= lngRange * 0.05;
    maxLng += lngRange * 0.05;
    latRange = maxLat - minLat;
    lngRange = maxLng - minLng;

    // Equirectangular-style projection with a latitude-dependent
    // longitude correction. Without the cos(mid_lat) scaling, 1° of
    // longitude occupies the same horizontal pixels as 1° of
    // latitude, which stretches east-west distances at mid-latitudes
    // — e.g., Calexico (south-east of San Mateo) ends up visually
    // too far east. Pre-scale lngRange by cos(mid_lat) so the two
    // axes use proportional geographic distance per pixel.
    const midLat = (minLat + maxLat) / 2;
    const lngScale = Math.cos(midLat * Math.PI / 180);
    const effectiveLngRange = lngRange * lngScale;

    // Pick the tighter scale so the data fits inside MAX_W/MAX_H,
    // then size the actual SVG viewport to the used dimensions. This
    // avoids huge left/right whitespace when the data is tall-and-
    // narrow (or top/bottom whitespace when short-and-wide).
    const maxViewW = MAX_W - PAD_L - PAD_R;
    const maxViewH = MAX_H - PAD_T - PAD_B;
    const xScalePerLng = maxViewW / effectiveLngRange;
    const yScalePerLat = maxViewH / latRange;
    const scale = Math.min(xScalePerLng, yScalePerLat);
    const usedW = effectiveLngRange * scale;
    const usedH = latRange * scale;
    const W = Math.round(usedW + PAD_L + PAD_R);
    const H = Math.round(usedH + PAD_T + PAD_B);
    const offX = PAD_L;
    const offY = PAD_T;

    function proj(lat, lng) {
      const x = offX + (lng - minLng) * lngScale * scale;
      const y = offY + (maxLat - lat) * scale;
      return [x, y];
    }

    let svg = `<svg class="mini-map" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Mini map of outbound sharing recipients">`;
    // Background
    svg += `<rect x="0" y="0" width="${W}" height="${H}" fill="#f8fafc" stroke="#e2e8f0"/>`;

    // CA state outline drawn behind everything else as orientation
    // anchor. If the viewport doesn't overlap CA (unusual — this
    // project is CA-focused), the polygon simply clips outside the
    // visible area.
    CA_OUTLINE.forEach(function(ring) {
      const points = ring.map(function(pt) {
        const [x, y] = proj(pt[1], pt[0]);
        return x.toFixed(1) + "," + y.toFixed(1);
      }).join(" ");
      svg += `<polygon points="${points}" fill="#eef2ff" stroke="#c7d2fe" stroke-width="0.8"/>`;
    });

    const [sx, sy] = proj(subjLat, subjLng);

    // Draw lines to flagged recipients first (behind dots) so red
    // connections pop without overlapping the dots.
    recipients.forEach(function(r) {
      if (r.kind) {
        const [rx, ry] = proj(r.lat, r.lng);
        svg += `<line x1="${sx.toFixed(1)}" y1="${sy.toFixed(1)}" x2="${rx.toFixed(1)}" y2="${ry.toFixed(1)}" stroke="#dc2626" stroke-width="0.5" opacity="0.35"/>`;
      }
    });

    // (Previously: a dashed amber line from the subject to the
    // farthest recipient. Removed — the label already calls it out
    // and the line added clutter, especially on big-reach maps like
    // El Cajon where nearly every flagged line runs out of state.)

    // Clean recipients first (underneath), then flagged on top so
    // the red dots aren't obscured. Orange for clean so they stand
    // out against the pale-blue CA fill (gray blended in too much).
    recipients.filter(function(r) { return !r.kind; }).forEach(function(r) {
      const [x, y] = proj(r.lat, r.lng);
      svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2" fill="#f97316" opacity="0.85"/>`;
    });
    recipients.filter(function(r) { return r.kind; }).forEach(function(r) {
      const [x, y] = proj(r.lat, r.lng);
      svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.8" fill="#dc2626"/>`;
    });

    // Subject: larger cyan ring
    svg += `<circle cx="${sx.toFixed(1)}" cy="${sy.toFixed(1)}" r="6" fill="none" stroke="#06b6d4" stroke-width="2"/>`;
    svg += `<circle cx="${sx.toFixed(1)}" cy="${sy.toFixed(1)}" r="3" fill="#06b6d4"/>`;

    svg += `</svg>`;

    return `<div class="mini-map-wrap">${svg}</div>`;
  }

  // Horizontal bar chart of top recipients by search volume, for the
  // right-hand cell of the downstream-searches row. Bar width is
  // scaled to the max in the visible set so the eye lands on the
  // heaviest searcher; the exact count sits in the right gutter.
  function topResearchersHtml(ds) {
    if (!ds || !ds.top_researchers || !ds.top_researchers.length) {
      return '<span class="null">&mdash;</span>';
    }
    const shown = ds.top_researchers.slice(0, 5);
    const max = shown.reduce(function(m, r) { return r.searches > m ? r.searches : m; }, 0) || 1;
    let html = '<div class="researchers-header">Top searchers among recipients</div>';
    html += '<div class="researchers-bars">';
    shown.forEach(function(r) {
      const pct = Math.max(4, Math.round(100 * r.searches / max));
      html += '<div class="rb-row">';
      html += '<div class="rb-name">';
      html += `<span class="rb-bar" style="width:${pct}%"></span>`;
      html += `<a href="?agency=${escapeHtml(r.slug)}">${escapeHtml(r.name)}</a>`;
      html += '</div>';
      html += `<div class="rb-count">${fmtInt(r.searches)}</div>`;
      html += '</div>';
    });
    html += '</div>';
    if (ds.recipients_with_data > 5) {
      html += `<div class="researchers-footer">+ ${ds.recipients_with_data - 5} more</div>`;
    }
    return html;
  }

  // Stacks the state rank and the local rank (if available) in a single
  // table cell. Keeps the stats table to 5 columns while still showing
  // both comparisons, and labels each so the reader knows which is
  // which.
  function rankCellHtml(statePctile, localPctile, localSample) {
    if (statePctile == null && localPctile == null) {
      return '<span class="null">&mdash;</span>';
    }
    let html = "";
    if (statePctile != null) {
      html += `<div class="rank-line"><span class="rank-tag rank-tag-state">statewide:</span> ${rankDescription(statePctile)}</div>`;
    }
    if (localPctile != null) {
      const scopeLabel = localSample && localSample.scope === "county"
        ? "local (county):"
        : "local (25 miles):";
      html += `<div class="rank-line"><span class="rank-tag rank-tag-local">${scopeLabel}</span> ${rankDescription(localPctile)}</div>`;
    }
    return html;
  }

  // Cell coloring policy: the report should raise concerns, not
  // reassure. High percentiles get colored red (concerning); low
  // percentiles get NO color — we don't want councils looking at a
  // green cell and concluding "we're fine." Absolute numbers may
  // still be concerning even when this agency is below its peers.
  function cellClassFor(pctile, metric) {
    if (pctile == null) return "";
    if (pctile >= 75) return "cell-high";       // "higher than most / nearly all"
    if (pctile >= 60) return "cell-mid-high";   // "above average"
    return "";  // median or below — no color, no pat on the back
  }

  // Short plain-English rank label for table cells (paired with a percentile
  // number so the reader sees both the narrative and the raw statistic).
  //
  // Color-codes the phrase to match the cell-background scheme: high
  // percentiles (more cameras / more sharing / more queries) are red,
  // low percentiles green, middle neutral. This lets the reader compare
  // the statewide and local lines at a glance — if one is "statewide:
  // near the median" (neutral) and the other is "local (25 miles):
  // higher than most" (red), the divergence pops out.
  // Short plain-English rank label. High percentiles get red because
  // they're concerning. Below-median cases are described neutrally —
  // no celebratory "lower than most" framing, no green color — so
  // councils reviewing the report don't conclude "we're fine" from a
  // low-percentile metric. The agency may still operate a large
  // surveillance program in absolute terms.
  function rankDescription(pctile) {
    let label;
    let cls = "";
    if (pctile >= 90) { label = "higher than nearly all"; cls = "rank-high-strong"; }
    else if (pctile >= 75) { label = "higher than most"; cls = "rank-high"; }
    else if (pctile >= 60) { label = "above average"; cls = "rank-mid-high"; }
    else if (pctile >= 40) { label = "near the median"; cls = "rank-mid"; }
    else { label = "below the peer median"; cls = "rank-mid"; }
    return `<span class="${cls}">${label}</span> <span class="muted">(${pctile}${nthSuffix(pctile)} percentile)</span>`;
  }

  function nthSuffix(n) {
    const s = ["th", "st", "nd", "rd"];
    const v = n % 100;
    return s[(v - 20) % 10] || s[v] || s[0];
  }

  // Plain-English comparison sentence for a percentile.
  // Always says WHAT is being compared (this agency to its peers) and
  // in WHICH direction (higher/lower).
  function percentileSentence(pctile, peerLabel, peerMedian) {
    let level;
    if (pctile >= 90) level = "<strong>higher than nearly all</strong>";
    else if (pctile >= 75) level = "<strong>higher than most</strong>";
    else if (pctile >= 60) level = "above average compared to";
    else if (pctile >= 40) level = "close to the median for";
    else if (pctile >= 25) level = "below average compared to";
    else if (pctile >= 10) level = "<strong>lower than most</strong>";
    else level = "<strong>lower than nearly all</strong>";
    let s = `This agency is ${level} ${peerLabel} (${pctile}${nthSuffix(pctile)} percentile`;
    if (peerMedian != null) {
      s += `; peer median: ${fmtNum(peerMedian, 0)}`;
    }
    s += `).`;
    return s;
  }

  // ── Checklists ──
  function renderSB34Checklist(report, meta) {
    if (report.state !== "CA") return "";
    const items = report.checklist_sb34 || [];
    if (!items.length) return "";

    let html = `<h2>SB 34 Compliance Concerns</h2>`;
    html += `<p class="muted">Signals tied to California Civil Code &sect;1798.90.51&ndash;.55. Red items indicate potential compliance concerns a council member may want to raise with their agency.</p>`;
    // Pass the current report through so inline data-concern callouts
    // can be attached to specific checklist items.
    // (Opt `report` consumed by renderChecklistItems below.)
    // Substantive vs. surface-signal disclaimer. Passing these checks
    // only means the minimum signal is present on the transparency
    // page — it does NOT mean the agency substantively complies with
    // the law. A posted policy may be stale or incomplete; a documented
    // audit process may not actually be executed; an outbound list
    // without flagged entities may still include unreviewed recipients.
    // Several investigations (including the project's SMPD findings)
    // show agencies that pass every heuristic signal while having
    // substantial compliance gaps.
    html += `<p class="legal-note" style="border-left: 3px solid var(--warn-border); background: var(--warn-bg); color: #78350f; margin: 6px 0 10px 0"><strong>Surface-signal checks only.</strong> A green check means the signal appears on the transparency page &mdash; not that the agency substantively complies. A posted policy may be stale, an &ldquo;audit process&rdquo; may not be executed, a clean sharing list may include unvetted recipients. Starting point for questions, not a compliance certification.</p>`;
    html += renderChecklistItems(items, { report: report });
    return html;
  }

  function renderTransparencyChecklist(report, meta) {
    if (report.state !== "CA") return "";
    const items = report.checklist_transparency || [];
    if (!items.length) return "";

    let html = `<h2>Transparency</h2>`;
    html += `<p class="muted">What this agency publishes on its Flock transparency page, compared to California peers.</p>`;
    html += renderChecklistItems(items, { multiCol: true, compact: true, report: report });
    return html;
  }

  // Pick the label that matches the check's actual state. Avoids the
  // "No private entities" ✗ contradiction where a static positive label
  // reads as an assertion the ✗ negates.
  function labelFor(item) {
    if (item.value === true) return item.label_pass || item.label;
    if (item.value === false) return item.label_fail || item.label_pass || item.label;
    return item.label_unknown || item.label_pass || item.label;
  }

  // Items where ≥ this fraction of verifiable peers also pass are
  // considered "common baseline" and hidden by default unless THIS
  // agency fails them. Keeps the printable report focused on the
  // distinguishing signals rather than listing universal practices
  // that tell the reader nothing.
  const UNIVERSAL_PASS_THRESHOLD = 0.90;

  function isUniversalPass(item) {
    const applicable = item.peer_applicable != null ? item.peer_applicable : item.peer_total;
    if (!applicable) return false;
    return (item.peer_count / applicable) >= UNIVERSAL_PASS_THRESHOLD;
  }

  function renderChecklistItems(items, opts) {
    opts = opts || {};
    const cls = "checklist" + (opts.multiCol ? " multi-col" : "");

    // Split: always-show (fail/unknown OR pass on a distinguishing
    // check) versus common-baseline (pass on a check ≥90% of peers
    // also pass). Common items get a one-line summary at the end.
    const visibleItems = [];
    const hiddenPassItems = [];
    items.forEach(function(item) {
      if (item.value !== true) {
        visibleItems.push(item);
      } else if (isUniversalPass(item)) {
        hiddenPassItems.push(item);
      } else {
        visibleItems.push(item);
      }
    });

    let html = `<ul class="${cls}">`;
    visibleItems.forEach(function(item) {
      const cls = item.value === true ? "yes" : item.value === false ? "no" : "unknown";
      const peerTypeLabel = item.peer_type === "all" ? "CA agencies" : `CA ${agencyTypeLabel(item.peer_type)} agencies`;
      html += `<li class="${cls}">`;
      html += `<span class="label">${escapeHtml(labelFor(item))}</span>`;
      // For sharing-based failures, name the offending entities so the
      // line explains WHY the check failed. Keep the inline list short
      // (up to 3); refer to the Flagged Recipients section for more.
      if (item.value === false && item.failure_entities && item.failure_entities.length) {
        const names = item.failure_entities;
        const shown = names.slice(0, 3).map(escapeHtml).join(", ");
        const more = names.length > 3 ? ` <span class="muted">&mdash; and ${names.length - 3} more (see Flagged Recipients below)</span>` : "";
        html += `<span class="detail"><strong>Shares to:</strong> ${shown}${more}</span>`;
      }
      // Caveat: check technically passes, but a related concern applies.
      // Used for the federal-sharing check when the agency shares with
      // a fusion center that has federal characteristics (e.g., NCRIC).
      // Suppressed when the check is already failing — the failure line
      // already covers it.
      if (item.value === true && item.caveat_entities && item.caveat_entities.length) {
        const names = item.caveat_entities;
        const shown = names.slice(0, 3).map(escapeHtml).join(", ");
        const more = names.length > 3 ? ` <span class="muted">&mdash; and ${names.length - 3} more</span>` : "";
        html += `<span class="detail caveat"><strong>\u26a0 Caveat:</strong> shares with ${shown}${more} &mdash; fusion centers may have federal entanglements (federal funding, staff, or governance). See Flagged Recipients for specifics.</span>`;
      }
      if (item.detail) {
        html += `<span class="detail">${escapeHtml(item.detail)}</span>`;
      }
      html += `<span class="peer-stat">${formatPeerStat(item, peerTypeLabel, opts.compact)}</span>`;
      // Inline data-concern anchored to this check (e.g. SMPD's
      // "Policy posted but not in full" under published_policy).
      if (opts.report) {
        const inline = concernsForSection(opts.report, `check:${item.id}`);
        if (inline) html += inline;
      }
      html += '</li>';
    });
    html += '</ul>';

    // (Previously: a "Also passing — ≥ 90% of peers also pass" blurb
    // listed the items we hid as near-universal baseline. Removed —
    // the checklist already shows only distinguishing signals, so
    // naming the hidden baseline items just added noise.)
    return html;
  }

  // Phrases peer stats concretely, using a per-check action phrase
  // (e.g. "publish an access policy", "avoid out-of-state sharing")
  // so the reader sees the specific behavior instead of an abstract
  // "pass". Falls back to "do this" if no peer_phrase is set.
  // Uncrawled agencies are excluded from the denominator — we can't
  // verify what isn't public. For SB 34 items (non-compact), adds a
  // statewide line when the type-scoped numbers differ.
  function formatPeerStat(item, peerTypeLabel, compact) {
    const applicable = item.peer_applicable != null ? item.peer_applicable : item.peer_total;
    const pass = item.peer_count;
    const phrase = item.peer_phrase || "do this";

    if (applicable === 0) {
      return `No ${peerTypeLabel} publish enough info to evaluate.`;
    }

    const passPct = pct(pass, applicable);

    if (compact) {
      return `${passPct}% of ${peerTypeLabel} ${escapeHtml(phrase)} (${pass}/${applicable}).`;
    }

    let line = `<strong>${passPct}%</strong> of ${applicable} ${peerTypeLabel} ${escapeHtml(phrase)} (${pass}/${applicable}).`;

    if (item.state_applicable != null && item.state_total != null) {
      const sApp = item.state_applicable;
      const sPass = item.state_count;
      if (sApp > applicable) {
        const sPct = pct(sPass, sApp);
        line += ` <span class="muted">Statewide: ${sPct}% (${sPass}/${sApp}).</span>`;
      }
    }
    return line;
  }

  // ── Sharing ──
  function renderSharing(report) {
    let html = `<h2>Data Sharing</h2>`;

    // Outbound
    const flagged = report.flagged_recipients || [];
    const outboundCount = (report.stats && report.stats.outbound_count) || 0;
    const outbound = report.outbound || [];
    const pctile = report.percentiles && report.percentiles.outbound;

    html += `<h3>Shares to (outbound): ${outbound.length} ${outbound.length === 1 ? "agency" : "agencies"}</h3>`;

    // Reach metrics: average distance and farthest recipient.
    // Downstream-search totals moved into the stats table above so
    // they get full state + local peer comparisons; here we just
    // describe how geographically broad this agency's sharing is.
    const farthest = report.farthest_outbound;
    const avgKm = report.outbound_avg_km;
    if (farthest || avgKm != null) {
      html += '<p class="muted" style="font-size:10pt; margin: 2px 0 8px 0">';
      const bits = [];
      if (avgKm != null) {
        bits.push(`Average distance to a recipient: <strong>${fmtNum(kmToMi(avgKm), 0)} miles</strong>`);
      }
      if (farthest) {
        const farMi = fmtNum(kmToMi(farthest.distance_km), 0);
        bits.push(
          `Farthest recipient: <strong>${escapeHtml(farthest.name)}</strong> (${farMi} miles away${farthest.state && farthest.state !== report.state ? `, ${escapeHtml(farthest.state)}` : ""})`
        );
      }
      html += bits.join(" &middot; ") + ".";
      html += '</p>';
    }

    // Mini regional map — shows the agency at the center and every
    // geocoded outbound recipient as a dot. Red dots/lines for
    // flagged recipients, amber dashed for the farthest. Rendered
    // inline so it prints as part of the PDF (a live sharing-map
    // link would be useless on paper). A QR code in the footer
    // already points readers to the interactive version.
    const subjLat = (report.geo && report.geo.lat) || null;
    const subjLng = (report.geo && report.geo.lng) || null;
    if (subjLat != null && subjLng != null) {
      html += miniMapHtml(report, subjLat, subjLng, farthest);
    }



    // The 30-Day Activity stats table above already shows state + local
    // ranks for "Agencies it shares to" with both medians. Don't
    // duplicate that here — just remove the old single-state sentence.

    if (flagged.length) {
      // Group flagged recipients by kind so we can explain each flag
      // type once instead of repeating the explanation per row.
      const kindGroups = {};
      flagged.forEach(function(f) {
        const k = refineKind(f.kind, f.name);
        (kindGroups[k] = kindGroups[k] || []).push(f);
      });

      // Deliberate render order: private entities and contested university PDs
      // first (direct statutory concerns), then out-of-state, federal, then
      // re-sharing hubs (multi-hop concern), then inactive and test accounts.
      // Groups not in this list (shouldn't happen, but be safe) append at the end.
      const KIND_ORDER = ["private_entity", "private_university", "out_of_state", "federal", "fusion_center", "decommissioned", "test"];
      const orderedKinds = KIND_ORDER.filter(function(k) { return kindGroups[k]; });
      Object.keys(kindGroups).forEach(function(k) {
        if (orderedKinds.indexOf(k) < 0) orderedKinds.push(k);
      });

      html += '<div class="flag-section">';
      html += `<strong>${flagged.length} flagged recipient${flagged.length === 1 ? "" : "s"}</strong> &mdash; entities whose inclusion raises compliance questions under CA Civil Code &sect;1798.90.55(b). Each flag type is explained below.`;
      orderedKinds.forEach(function(kind) {
        const group = kindGroups[kind];
        const label = FLAG_LABELS[kind] || kind.toUpperCase();
        const explanation = FLAG_EXPLANATIONS[kind] || "";
        html += `<div style="margin-top:10px">`;
        html += `<div style="font-weight:bold"><span class="flag-tag kind-${escapeHtml(kind)}">${escapeHtml(label)}</span></div>`;
        if (explanation) {
          html += `<div class="detail" style="margin:2px 0 4px 0;color:#555;font-size:9.5pt">${explanation}</div>`;
        }
        // Cap each flag group at a reasonable number of inline entries.
        // AG-lawsuit targets (the most urgent signal) get more room;
        // bulk groups like out-of-state get a shorter list plus a
        // count. Without this, a lawsuit target like El Cajon with 600+
        // out-of-state recipients bloats the PDF by tens of pages.
        const GROUP_LIMIT = 15;
        const visibleGroup = group.slice(0, GROUP_LIMIT);
        html += '<ul style="margin-top:2px">';
        visibleGroup.forEach(function(f) {
          html += `<li>${escapeHtml(f.name)}`;
          if (f.ag_lawsuit) html += ` <span class="flag-tag lawsuit">AG LAWSUIT</span>`;
          if (f.days_since_added != null) {
            const days = f.days_since_added;
            const ago = days === 0 ? "today" : days === 1 ? "1 day ago" : `${days} days ago`;
            html += ` <span class="flag-tag" style="background:#fef3c7;color:#92400e" title="Sharing first appeared on ${escapeHtml(f.added_on)}">NEW &mdash; started ${ago}</span>`;
          }
          // Registry notes are curated HTML (may contain anchor tags);
          // inserted as-is. Editors of the registry must avoid script
          // content — there is no sanitizer.
          if (f.notes) {
            html += `<div class="entity-notes">${f.notes}</div>`;
          }
          html += '</li>';
        });
        if (group.length > GROUP_LIMIT) {
          html += `<li class="muted">&hellip; and ${group.length - GROUP_LIMIT} more ${escapeHtml(label.toLowerCase())} recipient${group.length - GROUP_LIMIT === 1 ? "" : "s"}. See the live report for the full list.</li>`;
        }
        html += '</ul>';
        html += '</div>';
      });
      html += '</div>';
    }

    // Previously-flagged recipients: entities the agency dropped from
    // its sharing list. Surfaced so cleanup is visible (and so the
    // record doesn't disappear when a portal update removes a row).
    const removedFlagged = report.removed_flagged_recipients || [];
    if (removedFlagged.length) {
      html += '<div class="flag-section" style="margin-top:12px;border-left-color:#9ca3af">';
      html += `<strong>${removedFlagged.length} previously flagged recipient${removedFlagged.length === 1 ? "" : "s"} removed</strong> &mdash; entities that were in this agency\'s sharing list at some point during our tracking window and have since been removed.`;
      html += '<ul style="margin-top:6px">';
      removedFlagged.forEach(function(r) {
        const label = FLAG_LABELS[r.kind] || r.kind.toUpperCase();
        html += `<li>${escapeHtml(r.name)} <span class="flag-tag kind-${escapeHtml(r.kind)}">${escapeHtml(label)}</span>`;
        if (r.ag_lawsuit) html += ` <span class="flag-tag lawsuit">AG LAWSUIT</span>`;
        html += ` <span class="muted" style="font-size:9.5pt">&mdash; removed on/around ${escapeHtml(r.removed_on)}</span>`;
        html += '</li>';
      });
      html += '</ul>';
      html += '</div>';
    }

    // Outbound list: flagged entities are always shown inline (the
    // concerning signal). For long tail of non-flagged recipients,
    // cap the printed/expanded list at PRINT_LIST_LIMIT and show a
    // "... + N more" note pointing at the live URL. Prevents El
    // Cajon-style agencies (684 outbound) from ballooning the PDF.
    const PRINT_LIST_LIMIT = 50;
    if (outbound.length === 0) {
      if (report.crawled) {
        html += '<p class="muted">This agency publishes no outbound sharing relationships.</p>';
      } else {
        html += '<div class="no-data-box" style="background:#f3f4f6;border-left-color:#6b7280">Unknown &mdash; outbound sharing requires <em>this</em> agency\'s transparency page to verify. A small number of outbound edges may be inferable when other agencies publish an inbound list that names this one; none were found here.</div>';
      }
    } else if (outbound.length <= 25) {
      html += '<ul>';
      outbound.forEach(function(o) {
        const line = formatRelationship(o)
          + (o.inferred ? ' <span class="inferred">[inferred from their portal]</span>' : "");
        html += '<li>' + line + '</li>';
      });
      html += '</ul>';
    } else {
      const clean = outbound.filter(function(o) { return !o.kind; });
      html += `<details><summary>Show all ${outbound.length} recipients</summary><ul>`;
      const visible = outbound.slice(0, PRINT_LIST_LIMIT);
      visible.forEach(function(o) {
        const line = formatRelationship(o)
          + (o.inferred ? ' <span class="inferred">[inferred from their portal]</span>' : "");
        html += '<li>' + line + '</li>';
      });
      if (outbound.length > PRINT_LIST_LIMIT) {
        const remaining = outbound.length - PRINT_LIST_LIMIT;
        html += `<li class="muted">&hellip; and ${remaining} more. Full list at <a href="?agency=${escapeHtml(report.slug)}#full-outbound">the live report</a>.</li>`;
      }
      html += '</ul></details>';
      if (!flagged.length) {
        html += `<p class="muted">${clean.length} recipients with no flags.</p>`;
      }
    }

    // Print-only block: replaces the collapsed <details> lists above
    // with a short sentence + QR code to the live sharing map for
    // this agency. Screen view keeps the expandable details (for
    // interactivity); print shows the QR so a reader with a printed
    // copy can still reach the full list.
    const outboundCountForPrint = (report.outbound || []).length;
    const inboundCountForPrint = (report.inbound || []).length;
    if (outboundCountForPrint > 0 || inboundCountForPrint > 0) {
      const mapUrlAbs = new URL(
        `sharing_map.html#${report.slug}`,
        location.href
      ).toString();
      html += `<div class="print-only sharing-print-qr">`;
      html += `<div class="sharing-print-qr-text">`;
      html += `Full list of recipients and sources: scan the QR code to open this agency on the live sharing map.`;
      html += `<div class="sharing-print-qr-url">${escapeHtml(mapUrlAbs)}</div>`;
      html += `</div>`;
      html += `<div class="sharing-print-qr-code" id="sharing-print-qr"></div>`;
      html += `</div>`;
      setTimeout(function() { renderQrCode("sharing-print-qr", mapUrlAbs, { size: 90 }); }, 0);
    }

    // Inbound
    const inbound = report.inbound || [];
    // Clear the mini-map's left float so the inbound heading starts
    // on its own line rather than flowing next to the map.
    html += `<div style="clear:both"></div>`;
    html += `<h3>Receives from (inbound): ${inbound.length} ${inbound.length === 1 ? "agency" : "agencies"}</h3>`;
    if (inbound.length === 0) {
      html += '<p class="muted">No inbound sharing relationships found.</p>';
    } else if (inbound.length <= 25) {
      html += '<ul>';
      inbound.forEach(function(i) {
        html += '<li>' + formatRelationship(i);
        if (i.inferred) html += ' <span class="inferred">[inferred from their portal]</span>';
        html += '</li>';
      });
      html += '</ul>';
    } else {
      // Same cap as outbound: don't let a long tail bloat the PDF.
      html += `<details><summary>Show all ${inbound.length} sources</summary><ul>`;
      const visibleIn = inbound.slice(0, PRINT_LIST_LIMIT);
      visibleIn.forEach(function(i) {
        html += '<li>' + formatRelationship(i);
        if (i.inferred) html += ' <span class="inferred">[inferred from their portal]</span>';
        html += '</li>';
      });
      if (inbound.length > PRINT_LIST_LIMIT) {
        const remaining = inbound.length - PRINT_LIST_LIMIT;
        html += `<li class="muted">&hellip; and ${remaining} more. Full list at the live report.</li>`;
      }
      html += '</ul></details>';
    }

    return html;
  }

  function formatRelationship(r) {
    const kind = r.kind;
    let html = escapeHtml(r.name);
    if (kind) {
      const refined = refineKind(kind, r.name);
      const label = FLAG_LABELS[refined] || refined.toUpperCase();
      html += ` <span class="flag-tag kind-${escapeHtml(refined)}">${escapeHtml(label)}</span>`;
    }
    return html;
  }

  // ── Regional ──
  //
  // km → miles conversion factor. The data pipeline stores distance_km
  // (because the Haversine formula gives km directly). We convert at
  // render time so everything the reader sees is in miles.
  const KM_PER_MILE = 1.60934;
  function kmToMi(km) { return km / KM_PER_MILE; }

  function renderRegional(report, meta) {
    const regional = report.regional || [];
    if (!regional.length) return "";

    // Any regional agency has population data? If none, skip the
    // per-capita columns entirely.
    const anyPopulation = regional.some(function(r) { return r.population; });

    const radiusKm = meta.regional_radius_km || 50;
    const radiusMi = Math.round(kmToMi(radiusKm));

    // Cap the visible regional table to the 8 closest agencies (plus
    // the subject agency itself pinned at the top as a comparison
    // row). Most readers only need the immediate neighborhood; for
    // the full list they can click through to any row's agency page.
    const REGIONAL_ROW_LIMIT = 8;

    // Synthesize the subject as a row so the reader can compare it
    // against the neighbors on the same scale. Per-capita rates come
    // from report.per_1000; raw values from report.stats.
    const per1k = report.per_1000 || {};
    const subjStats = report.stats || {};
    const subjectRow = {
      slug: report.slug,
      name: report.name,
      isSubject: true,
      distance_km: 0,
      population: report.population,
      cameras: subjStats.cameras,
      vehicles_30d: subjStats.vehicles_30d,
      searches_30d: subjStats.searches_30d,
      outbound: subjStats.outbound_count || 0,
      cameras_per_1000: per1k.cameras,
      vehicles_per_1000: per1k.vehicles_30d,
      searches_per_1000: per1k.searches_30d,
    };

    let html = `<h2>Regional Context</h2>`;
    html += `<p class="muted">This agency (highlighted) vs. the ${Math.min(REGIONAL_ROW_LIMIT, regional.length)} closest crawled California agencies within ${radiusMi} miles (of ${regional.length} total). Per-capita columns normalize by city population so small towns and big cities can be compared on the same scale.</p>`;
    html += '<table>';
    html += '<tr>';
    html += '<th data-sort-key="name" class="sortable">Agency <span class="sort-arrow"></span></th>';
    html += '<th data-sort-key="distance" class="sortable">Distance <span class="sort-arrow"></span></th>';
    html += '<th data-sort-key="cameras" class="sortable num">Cameras' + (anyPopulation ? '<br><span class="muted-head">(per 1k)</span>' : '') + ' <span class="sort-arrow"></span></th>';
    html += '<th data-sort-key="vehicles" class="sortable num">Vehicles/30d' + (anyPopulation ? '<br><span class="muted-head">(per 1k)</span>' : '') + ' <span class="sort-arrow"></span></th>';
    html += '<th data-sort-key="searches" class="sortable num">Searches/30d' + (anyPopulation ? '<br><span class="muted-head">(per 1k)</span>' : '') + ' <span class="sort-arrow"></span></th>';
    html += '<th data-sort-key="outbound" class="sortable num">Shares to <span class="sort-arrow"></span></th>';
    html += '</tr>';

    function valueWithRate(value, rate, decimals) {
      if (value == null) return '<span class="null">&mdash;</span>';
      const main = fmtNum(value, decimals || 0);
      if (!anyPopulation || rate == null) return main;
      const rateFmt = fmtNum(rate, rate < 10 ? 2 : 0);
      return `${main} <span class="paren-median">(${rateFmt})</span>`;
    }

    const rows = [subjectRow].concat(regional.slice(0, REGIONAL_ROW_LIMIT));
    rows.forEach(function(r) {
      const trCls = r.isSubject ? ' class="subject-row"' : '';
      html += `<tr${trCls}>`;
      const agencyCell = r.isSubject
        ? `<strong>${escapeHtml(r.name)}</strong> <span class="muted">(this agency)</span>`
        : `<a href="?agency=${escapeHtml(r.slug)}">${escapeHtml(r.name)}</a>`;
      html += `<td data-sort-value="${escapeHtml(r.name.toLowerCase())}">${agencyCell}</td>`;
      const distCell = r.isSubject ? '&mdash;' : `${fmtNum(kmToMi(r.distance_km), 1)} mi`;
      html += `<td class="num" data-sort-value="${r.distance_km}">${distCell}</td>`;
      html += `<td class="num" data-sort-value="${r.cameras == null ? '' : r.cameras}" data-sort-rate="${r.cameras_per_1000 == null ? '' : r.cameras_per_1000}">${valueWithRate(r.cameras, r.cameras_per_1000, 0)}</td>`;
      html += `<td class="num" data-sort-value="${r.vehicles_30d == null ? '' : r.vehicles_30d}" data-sort-rate="${r.vehicles_per_1000 == null ? '' : r.vehicles_per_1000}">${valueWithRate(r.vehicles_30d, r.vehicles_per_1000, 0)}</td>`;
      html += `<td class="num" data-sort-value="${r.searches_30d == null ? '' : r.searches_30d}" data-sort-rate="${r.searches_per_1000 == null ? '' : r.searches_per_1000}">${valueWithRate(r.searches_30d, r.searches_per_1000, 0)}</td>`;
      if (r.isSubject || r.outbound > 0) {
        html += `<td class="num" data-sort-value="${r.outbound || 0}">${fmtInt(r.outbound || 0)}</td>`;
      } else {
        html += `<td class="num" data-sort-value=""><span class="null">N/A</span></td>`;
      }
      html += `</tr>`;
    });
    html += '</table>';
    // Click-to-sort: deferred because we need the DOM to exist first.
    // Default sort is distance ascending (already the order we rendered).
    setTimeout(function() { wireRegionalSort(); }, 0);
    return html;
  }

  // Default sort direction for a given column. Name/distance sort
  // ascending (A-Z, nearest first); numeric columns start descending
  // since the interesting signal is big numbers.
  function defaultDirFor(key) {
    return (key === "name" || key === "distance") ? 1 : -1;
  }

  // Attach click-to-sort to each sortable header of the regional
  // context table. For columns where each cell carries a per-1k rate
  // in parentheses, clicks cycle through four states:
  //   primary-asc -> primary-desc -> rate-asc -> rate-desc -> primary-asc
  // Columns without a rate toggle only asc/desc.
  function wireRegionalSort() {
    const tables = document.querySelectorAll("table");
    let table = null;
    tables.forEach(function(t) {
      if (t.querySelector("th.sortable")) table = t;
    });
    if (!table) return;

    const headers = table.querySelectorAll("th.sortable");
    // activeKey: sort-key string; activeMode: "primary" or "rate";
    // activeDir: 1 asc, -1 desc
    let activeKey = null;
    let activeMode = "primary";
    let activeDir = 1;

    headers.forEach(function(th) {
      th.addEventListener("click", function() {
        const key = th.dataset.sortKey;
        // Does this column have a per-1k rate on its cells? Check the
        // first data row's cell at this column's index.
        const colIdx = Array.prototype.indexOf.call(th.parentNode.children, th);
        const sampleRow = table.tBodies[0] ? table.tBodies[0].rows[0] : table.rows[1];
        const hasRate = !!(sampleRow && sampleRow.cells[colIdx]
          && sampleRow.cells[colIdx].dataset.sortRate !== undefined
          && sampleRow.cells[colIdx].dataset.sortRate !== "");

        if (activeKey !== key) {
          // New column: use the column's default direction.
          activeKey = key;
          activeMode = "primary";
          activeDir = defaultDirFor(key);
        } else {
          // Same column: advance through the cycle. For rate-bearing
          // columns: primary-asc → primary-desc → rate-asc → rate-desc
          // → primary-asc. For plain columns: asc → desc → asc.
          if (activeMode === "primary" && activeDir === defaultDirFor(key)) {
            activeDir = -activeDir;  // primary desc (or asc after flip)
          } else if (activeMode === "primary" && hasRate) {
            activeMode = "rate";
            activeDir = -1;  // rate desc first
          } else if (activeMode === "rate" && activeDir === -1) {
            activeDir = 1;   // rate asc
          } else {
            // Back to primary default
            activeMode = "primary";
            activeDir = defaultDirFor(key);
          }
        }
        sortRegionalRows(table, headers, key, activeMode, activeDir);
      });
    });
  }

  function sortRegionalRows(table, headers, key, mode, dir) {
    const tbody = table.tBodies[0] || table;
    const rows = Array.from(tbody.querySelectorAll("tr")).filter(function(row) {
      return row.querySelector("td");
    });

    let colIdx = -1;
    headers.forEach(function(th, i) {
      if (th.dataset.sortKey === key) colIdx = i;
    });
    if (colIdx < 0) return;

    rows.sort(function(a, b) {
      const ac = a.cells[colIdx];
      const bc = b.cells[colIdx];
      const av = mode === "rate" ? (ac.dataset.sortRate || "") : ac.dataset.sortValue;
      const bv = mode === "rate" ? (bc.dataset.sortRate || "") : bc.dataset.sortValue;
      if (av === "" && bv === "") return 0;
      if (av === "") return 1;
      if (bv === "") return -1;
      const an = parseFloat(av);
      const bn = parseFloat(bv);
      if (!isNaN(an) && !isNaN(bn)) return (an - bn) * dir;
      return av.localeCompare(bv) * dir;
    });

    rows.forEach(function(r) { tbody.appendChild(r); });

    // Update arrow indicators. The "rate" mode gets a parenthesized
    // arrow so the reader can tell which sub-value is active.
    headers.forEach(function(th) {
      const arrow = th.querySelector(".sort-arrow");
      if (!arrow) return;
      if (th.dataset.sortKey === key) {
        const base = dir > 0 ? "\u25B2" : "\u25BC";
        arrow.textContent = mode === "rate" ? `(${base})` : base;
      } else {
        arrow.textContent = "";
      }
    });
  }

  // ── Legal summary ──
  // URL for a specific CA Civil Code section. These link to
  // leginfo.legislature.ca.gov which renders stable deep links.
  function calCodeUrl(section) {
    return `https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=${encodeURIComponent(section)}`;
  }

  function renderLegalSummary(report, meta) {
    if (report.state !== "CA") return "";
    // Two subsections, each with its own QR code aligned next to the
    // relevant body text. Previously the two QRs were bundled in a
    // single right-hand column — the AG Bulletin QR ended up far
    // from its mention, which was confusing for readers holding a
    // printed copy.
    const sb34Url = "https://leginfo.legislature.ca.gov/faces/codes_displayText.xhtml?lawCode=CIV&division=3.&title=1.81.23.&part=4.&chapter=&article=";
    const agBulletinUrl = "https://oag.ca.gov/system/files/media/alpr-bulletin.pdf";
    setTimeout(function() {
      renderQrCode("sb34-qr", sb34Url, { size: 70 });
      renderQrCode("ag-bulletin-qr", agBulletinUrl, { size: 70 });
    }, 0);
    return `
      <h2>California ALPR Law &mdash; Key Requirements</h2>
      <div class="legal-with-qr">
        <div class="legal-text">
          <p><strong>CA Civil Code &sect;<a href="${calCodeUrl("1798.90.51")}" target="_blank" rel="noopener">1798.90.51</a>&ndash;<a href="${calCodeUrl("1798.90.55")}" target="_blank" rel="noopener">.55</a> (SB 34)</strong> governs ALPR use by California agencies. Full text at <a href="${sb34Url}" target="_blank" rel="noopener">leginfo.legislature.ca.gov</a>.</p>
          <ul>
            <li><strong>Sharing restricted to public agencies</strong> (<a href="${calCodeUrl("1798.90.55")}" target="_blank" rel="noopener">&sect;1798.90.55(b)</a>): ALPR data may only be shared with public agencies. Private universities, HOAs, and vendors in a sharing list likely violate this provision.</li>
            <li><strong>Usage and privacy policy required</strong> (<a href="${calCodeUrl("1798.90.51")}" target="_blank" rel="noopener">&sect;1798.90.51(a)</a>): operators must conspicuously post a policy covering authorized uses, data access, retention, auditing, and accountability.</li>
            <li><strong>Audit requirements</strong> (<a href="${calCodeUrl("1798.90.51")}" target="_blank" rel="noopener">&sect;1798.90.51(b)(5)</a>): policies must include provisions for auditing access.</li>
            <li><strong>End-user obligations</strong> (<a href="${calCodeUrl("1798.90.53")}" target="_blank" rel="noopener">&sect;1798.90.53</a>): agencies accessing ALPR data must maintain a usage policy, even if they don't operate cameras.</li>
            <li><strong>\u201cAgency\u201d defined</strong> (<a href="${calCodeUrl("1798.90.5")}" target="_blank" rel="noopener">&sect;1798.90.5(f)</a>): \u201cpublic agency\u201d is narrowly defined and does not include federal agencies.</li>
          </ul>
        </div>
        <div class="legal-qr-col">
          <div class="legal-qr-block">
            <div id="sb34-qr" aria-label="QR code linking to SB 34 full text"></div>
            <div class="qr-caption">SB 34 full text</div>
          </div>
        </div>
      </div>
      <div class="legal-with-qr">
        <div class="legal-text">
          <p><strong>AG Bulletin 2023-DLE-06</strong> (<a href="${agBulletinUrl}" target="_blank" rel="noopener">PDF</a>, October 2023) directed all California agencies to:</p>
          <ul>
            <li>Review vendor contracts for SB 34 compliance, particularly provisions allowing non-agency access to ALPR data.</li>
            <li>Conspicuously post ALPR usage and privacy policies.</li>
            <li>Address audit deficiencies identified by the CA State Auditor.</li>
          </ul>
        </div>
        <div class="legal-qr-col">
          <div class="legal-qr-block">
            <div id="ag-bulletin-qr" aria-label="QR code linking to AG Bulletin 2023-DLE-06"></div>
            <div class="qr-caption">AG Bulletin 2023-DLE-06</div>
          </div>
        </div>
      </div>
    `;
  }

  // ── Questions ──
  function renderQuestions(report, meta) {
    if (report.state !== "CA") return "";

    const questions = buildQuestions(report);
    if (!questions.length) return "";

    const nameForHeader = escapeHtml(agencyName(report));
    let html = `<h2>Questions for the ${nameForHeader}</h2>`;
    html += `<p class="muted">These questions have been tailored to the ${nameForHeader}'s specific usage patterns:</p>`;
    html += '<div class="questions"><ul>';
    questions.forEach(function(q) {
      html += `<li>${q}</li>`;
    });
    html += '</ul></div>';
    html += `<p class="legal-note"><em>This is informational context derived from public records, not legal advice.</em></p>`;
    return html;
  }

  // Expand the registry's terse "Belmont CA PD" / "Butte County CA SO"
  // form into prose-friendly "Belmont Police Department" /
  // "Butte County Sheriff's Office". Names render in council-tailored
  // questions so each report reads like it was written for that city,
  // not a canned generic prompt.
  function agencyName(report) {
    let n = (report && report.name) || "this department";
    n = n.replace(/ CA PD$/, " Police Department");
    n = n.replace(/ CA SO$/, " Sheriff's Office");
    n = n.replace(/ CA DA$/, " District Attorney's Office");
    n = n.replace(/ CA SD$/, " Sheriff's Department");
    return n;
  }

  function buildQuestions(report) {
    const qs = [];
    const flagged = report.flagged_recipients || [];
    // Tailored agency-name forms used throughout the questions list.
    // `dept` is mid-sentence ("Will the Belmont PD commit..."), `Dept`
    // is sentence-initial ("The Belmont PD reports...").
    const nameRaw = escapeHtml(agencyName(report));
    const dept = `the ${nameRaw}`;
    const Dept = `The ${nameRaw}`;
    // Short form for second/later references within a single question.
    // City PDs and county sheriffs are "the department" (sheriffs use
    // both "the office" and "the department"; the latter reads
    // universally). Everything else falls back to "the agency".
    const deptShort = (report.agency_type === "city" || report.agency_type === "county")
      ? "the department"
      : "the agency";
    const DeptShort = (report.agency_type === "city" || report.agency_type === "county")
      ? "The department"
      : "The agency";
    // localPlace: use the agency's own jurisdiction name when available
    // (e.g. "San Mateo", "Butte County"). Avoids "this jurisdiction"
    // ambiguity when questions reference remote sharing partners.
    const localPlace = (report.geo && report.geo.name)
      ? escapeHtml(report.geo.name)
      : `${dept}'s jurisdiction`;
    // Question priorities (0-100): higher renders first. Static
    // priorities for always-on or single-trigger questions; dynamic
    // priorities (computed inline) scale with magnitude (e.g. high
    // search count = higher priority). Goal: highest-impact questions
    // surface at the top so a council member skimming the list lands
    // on the most damning items first.
    const add = function(pri, text) { qs.push({pri: pri, text: text}); };
    // Cap at 10 — beyond that the list overwhelms a council member
    // skimming for action items. Sort highest-priority first.
    const QUESTION_CAP = 10;
    const finalize = function(items) {
      return items
        .slice()
        .sort(function(a, b) { return b.pri - a.pri; })
        .slice(0, QUESTION_CAP)
        .map(function(q) { return q.text; });
    };

    if (!report.crawled) {
      // Peer-pressure clause: "X% of California city PDs publish a
      // transparency page; this one does not." Pulled from the
      // has_portal transparency check, which is already evaluated
      // for every CA agency (crawled or not). For non-CA agencies
      // the checklist is empty — fall back to a generic clause.
      const transparency = report.checklist_transparency || [];
      const hasPortal = transparency.find(function(i) { return i.id === "has_portal"; });
      const typeLabel = (hasPortal && hasPortal.peer_total)
        ? agencyTypeLabel(hasPortal.peer_type)
        : "agencies";
      add(95, `Many California ${typeLabel} publish a public Flock transparency page covering ALPR cameras, policy, retention, sharing partners, and audits. Where is ${dept}'s?`);
      // Outbound recipients we know about even without a current
      // crawled portal — either from a past crawl that's still in the
      // sharing graph, or from other agencies' published inbound lists
      // naming this dept as a source. Either way: we can prove sharing
      // happens, which makes "what data is being sent and how is its
      // use governed" a sharp question to ask.
      const outRecips = report.outbound || [];
      if (outRecips.length > 0) {
        const exampleNames = outRecips.slice(0, 3).map(function(o) {
          return `<strong>${escapeHtml(o.name)}</strong>`;
        });
        const remainder = outRecips.length - 3;
        const namesList = remainder > 0
          ? `${exampleNames.join(", ")}, and ${remainder} other${remainder === 1 ? "" : "s"}`
          : exampleNames.join(", ");
        add(90, `Other California agencies' transparency pages indicate that ${dept} shares ALPR data with at least <strong>${outRecips.length}</strong> agencies (including ${namesList}). What data is ${deptShort} sending to these recipients, and how is its use governed once it leaves ${deptShort}'s control?`);
      }
      // Examples of nearby crawled-portal agencies — rendered as a
      // block list beneath the question prose so each link and its
      // QR code stay together (inline-flowing the QRs through prose
      // produces awkward line breaks). Flock transparency URLs are
      // deterministic: https://transparency.flocksafety.com/<slug>
      const regional = report.regional || [];
      const peerQrUrls = [];
      const exampleEntries = regional.slice(0, 3).map(function(r) {
        const url = `https://transparency.flocksafety.com/${r.slug}`;
        const qrId = `peer-qr-${r.slug}`;
        peerQrUrls.push({ id: qrId, url: url });
        return `<span class="peer-example"><span class="peer-qr" id="${qrId}" aria-label="QR code for ${escapeHtml(r.name)} transparency page"></span><a href="${url}" target="_blank" rel="noopener">${escapeHtml(r.name)}</a></span>`;
      });
      const examplesBlock = exampleEntries.length
        ? `<div class="peer-examples-block"><div class="peer-examples-label">Nearby agencies that publish this kind of page:</div><div class="peer-examples-list">${exampleEntries.join("")}</div></div>`
        : "";
      add(75, `If ${dept} doesn't yet publish a public ALPR transparency page, when does ${deptShort} plan to &mdash; and will it cover cameras, policy, retention, audits, and sharing partners?${examplesBlock}`);
      // Defer QR renders until the question HTML is in the DOM.
      if (peerQrUrls.length) {
        setTimeout(function() {
          peerQrUrls.forEach(function(p) {
            renderQrCode(p.id, p.url, { size: 40 });
          });
        }, 0);
      }
      return finalize(qs);
    }

    // Generic flagged-recipient prompt covers kinds that don't have
    // their own specific question below. Private-entity recipients
    // are handled by the §1798.90.55(b) "public agency" question
    // further down — counting them here would double-ask. Same idea
    // will apply as we add specific prompts for other flag kinds.
    const flaggedNonPrivate = flagged.filter(function(r) {
      return r.kind !== "private";
    });
    // Generic flagged-recipient prompt only fires for >= 2 recipients.
    // A single flag (typical case: lone fusion-center sharing with NCRIC,
    // a longstanding relationship) makes the "produce records for each"
    // demand feel pedantic. The lawsuit, private-entity, and recent-
    // additions questions handle the more pointed single-agency cases.
    if (flaggedNonPrivate.length >= 2) {
      const n = flaggedNonPrivate.length;
      const q = `<strong>${n}</strong> of the agencies receiving ${dept}'s ALPR data are flagged below as out-of-state, federal, or fusion-center recipients. For each one, what does ${dept}'s approval process look like &mdash; who reviewed the sharing request, when, and what vetting of the recipient was performed?`;
      // Pri scales with count of flagged non-private recipients.
      const flaggedPri = n >= 10 ? 78 : n >= 5 ? 70 : 55;
      add(flaggedPri, q);
    }

    // Lawsuit-recipient question: CA AG has filed suit against specific
    // agencies over illegal ALPR sharing (e.g., El Cajon — for sharing
    // with out-of-state agencies in violation of §1798.90.55(b)). Every
    // agency that continues to share with the lawsuit defendant is
    // participating in the same activity the AG has sued over.
    const sb34 = report.checklist_sb34 || [];
    const lawsuitItem = sb34.find(function(i) { return i.id === "no_ag_lawsuit_sharing"; });
    if (lawsuitItem && lawsuitItem.value === false && (lawsuitItem.failure_entities || []).length) {
      const lawsuitNames = lawsuitItem.failure_entities.map(escapeHtml);
      let nameList;
      if (lawsuitNames.length === 1) {
        nameList = `<strong>${lawsuitNames[0]}</strong>`;
      } else if (lawsuitNames.length === 2) {
        nameList = `<strong>${lawsuitNames[0]}</strong> and <strong>${lawsuitNames[1]}</strong>`;
      } else {
        const shown = lawsuitNames.slice(0, 3).map(function(n) { return `<strong>${n}</strong>`; }).join(", ");
        const remL = lawsuitNames.length - 3;
        nameList = remL > 0 ? `${shown}, and ${remL} other${remL === 1 ? "" : "s"}` : shown;
      }
      const isOne = lawsuitNames.length === 1;
      add(95,
        `${Dept} shares ALPR data with ${nameList}. The California Attorney General has filed a lawsuit against ${isOne ? "this agency" : "these agencies"} for illegally sharing ALPR data with out-of-state agencies in violation of CA Civil Code &sect;1798.90.55(b). What review has ${deptShort} performed of the lawsuit's allegations, and what was the outcome &mdash; will ${deptShort} suspend sharing pending resolution, or has ${deptShort} concluded continued sharing is appropriate?`
      );
    }

    // Private-entity recipients: name them explicitly and ask the
    // §1798.90.55(b) "public agency" question + records question.
    // Pairs with the UOP/Stockton story in the findings doc §6:
    // when a peer was asked whether it had vetted UOP's status, it
    // confirmed it had not — and that no records of what was shared
    // are retained by either side, since all paperwork lives in
    // Flock's platform and is not subject to the CPRA.
    const privates = (report.flagged_recipients || []).filter(function(r) {
      return r.kind === "private";
    });
    if (privates.length) {
      const names = privates.map(function(r) { return escapeHtml(r.name); });
      let nameList;
      if (names.length === 1) {
        nameList = `<strong>${names[0]}</strong>`;
      } else if (names.length === 2) {
        nameList = `<strong>${names[0]}</strong> and <strong>${names[1]}</strong>`;
      } else {
        const shown = names.slice(0, 3).map(function(n) { return `<strong>${n}</strong>`; }).join(", ");
        const rem = names.length - 3;
        nameList = rem > 0
          ? `${shown}, and ${rem} other${rem === 1 ? "" : "s"}`
          : shown;
      }
      const isOne = privates.length === 1;
      add(92,
        `${Dept} shares ALPR data with ${nameList} &mdash; ` +
        `${isOne ? "an entity" : "entities"} whose status as a "public agency" under CA Civil Code ` +
        `&sect;1798.90.55(b) is not self-evident. Does ${deptShort} consider ` +
        `${isOne ? "this recipient to be a public agency" : "these recipients to be public agencies"}, ` +
        `and what records does ${deptShort} maintain of the ALPR data ` +
        `shared with ${isOne ? "it" : "them"}?`
      );
    }

    // Recent outbound additions: forces a "produce the approval
    // record" question for each new sharing partner. A single
    // addition is enough to fire — the city should be able to
    // produce the approval record for any addition. If recent
    // additions include flagged recipients, we call that out
    // explicitly.
    const recentAdds = report.recent_outbound_additions || [];
    if (recentAdds.length) {
      const flaggedAdds = recentAdds.filter(function(a) { return a.kind; });
      const nonFlaggedAdds = recentAdds.filter(function(a) { return !a.kind; });
      // Surface flagged recent additions by name first — they're the
      // most interesting case and shouldn't get buried in "and N others".
      const orderedAdds = flaggedAdds.concat(nonFlaggedAdds);
      const namesShown = orderedAdds.slice(0, 3).map(function(a) { return `<strong>${escapeHtml(a.name)}</strong>`; }).join(", ");
      const remainder = orderedAdds.length - 3;
      const namesList = remainder > 0
        ? `${namesShown}, and ${remainder} other${remainder === 1 ? "" : "s"}`
        : namesShown;
      let q = `In the last 90 days, ${dept} added <strong>${recentAdds.length}</strong> agenc${recentAdds.length === 1 ? "y" : "ies"} to its ALPR sharing list (${namesList}). For each one, what does ${deptShort}'s approval process look like &mdash; who at ${deptShort} reviewed the request, when, and what review of the recipient was performed?`;
      if (flaggedAdds.length) {
        const isOneFlagged = flaggedAdds.length === 1;
        q += ` <span class="muted">(<strong>${flaggedAdds.length}</strong> of the recent additions ${isOneFlagged ? "is" : "are"} flagged in this report &mdash; see Flagged Recipients section.)</span>`;
      }
      // Recent additions with flagged kinds bump the priority — adding
      // a private/lawsuit-tagged agency in the last 90 days is the
      // sharpest version of this question.
      const addPri = flaggedAdds.length ? 80 : (recentAdds.length >= 5 ? 65 : 50);
      add(addPri, q);
    }

    // Recent outbound removals: implies the agency is now reviewing
    // sharing partners. The implicit follow-up is "if these turned
    // out not to belong, why are the others still on the list?"
    const recentRemovals = report.recent_outbound_removals || [];
    if (recentRemovals.length) {
      // Big housekeeping (Vacaville-style) is the strongest signal —
      // the question "if you just realized N agencies didn't belong,
      // what about the rest of your list?" has real bite.
      const rmPri = recentRemovals.length >= 50 ? 80 : recentRemovals.length >= 5 ? 60 : 40;
      add(rmPri, `In the last 90 days, ${dept} removed <strong>${recentRemovals.length}</strong> agenc${recentRemovals.length === 1 ? "y" : "ies"} from its ALPR sharing list. What review prompted the removals, and what records exist of that review? If those agencies were not appropriate to share with, is the same review now being applied to the agencies still on ${dept}'s sharing list?`);
    }

    // Sharing size
    const out = report.stats && report.stats.outbound_count;
    const pctile = report.percentiles && report.percentiles.outbound;
    if (out && pctile != null && pctile >= 75) {
      const pool = peerPoolName(report.peer_sample && report.peer_sample.outbound);
      const sizePri = pctile >= 95 ? 75 : pctile >= 85 ? 60 : 45;
      add(sizePri, `${Dept} shares ALPR data with <strong>${out}</strong> other agencies &mdash; more than ${pctile}% of comparable California ${pool}. What does ${dept}'s process look like for adding a new sharing partner &mdash; who approves, what review of the recipient is performed, and where are those records kept? <span class="muted">Decisions made inside Flock's platform are not subject to the California Public Records Act; only records the city itself maintains can be produced under a CPRA request.</span>`);
    }

    // Generic transparency-checklist failures: for each transparency
    // item the agency doesn't publish AND a meaningful share of CA
    // peers does, ask "X% of peers publish this; will you?". Skips
    // checks with low peer adoption (where the question would feel
    // like nitpicking) and items that have their own dedicated
    // questions (has_portal, outbound_list, inbound_list).
    const transparency = report.checklist_transparency || [];
    transparency.forEach(function(item) {
      if (item.value !== false) return;
      if (!item.peer_total) return;
      const pctPass = 100 * item.peer_count / item.peer_total;
      if (pctPass < 25) return;
      if (item.id === "has_portal") return;
      if (item.id === "outbound_list" || item.id === "inbound_list") return;
      const pctStr = pct(item.peer_count, item.peer_total);
      const phrase = item.peer_phrase || "publish this";
      // Higher pri when more peers do publish it (gap looks worse).
      const transPri = pctPass >= 60 ? 50 : pctPass >= 40 ? 40 : 30;
      add(transPri, `<strong>${pctStr}%</strong> of California ${agencyTypeLabel(item.peer_type)} agencies ${phrase}; ${dept} does not. Will ${deptShort} commit to adding this to its transparency page?`);
    });

    // Portal exists, but agency publishes no sharing lists at all.
    // Other agencies' published outbound lists indicate sharing
    // relationships exist, suggesting the gap is incomplete disclosure
    // rather than a genuinely empty list. (Oakland is the canonical
    // case: 112 inferred inbound edges, 0 published.)
    const allOut = report.outbound || [];
    const allIn = report.inbound || [];
    const directOutCount = allOut.filter(function(o) { return !o.inferred; }).length;
    const inferredOutCount = allOut.filter(function(o) { return o.inferred; }).length;
    const directInCount = allIn.filter(function(o) { return !o.inferred; }).length;
    const inferredInCount = allIn.filter(function(o) { return o.inferred; }).length;
    // Lead with inferred-inbound count only — that's the reliable
    // signal (most agencies publish outbound, so seeing this dept on
    // their lists yields a meaningful floor). Inferred-outbound is
    // structurally noisy because most agencies don't publish inbound,
    // so it under-counts; quoting a small number there reads as
    // "they barely share" when actually we just can't see most of it.
    if (directOutCount === 0 && directInCount === 0 && inferredInCount >= 5) {
      add(78,
        `${Dept} publishes a transparency page, but it does not list any sharing partners &mdash; ` +
        `neither inbound nor outbound. Other California agencies' published sharing lists report ` +
        `sending and/or receiving data with ${dept}. Why aren't those relationships disclosed ` +
        `on ${deptShort}'s own transparency page?`
      );
    }

    // SB 34 failures
    const sb34Fails = sb34.filter(function(i) { return i.value === false; });
    sb34Fails.forEach(function(item) {
      if (item.id === "documented_audit") {
        add(70, `${Dept}'s transparency page describes no audit process for ALPR access. What does ${dept}'s audit process look like &mdash; how often are audits conducted, what is reviewed (sharing configuration vs. search activity vs. case justifications), and where are the audit records?`);
      } else if (item.id === "downloadable_audit") {
        const pctStr = pct(item.peer_count, item.peer_total);
        add(55, `<strong>${pctStr}%</strong> of California ${agencyTypeLabel(item.peer_type)} agencies publish their ALPR search-audit records publicly; ${dept} does not. Will ${dept} commit to publishing the same &mdash; by a specific date &mdash; or confirm publicly that ${dept} maintains no such audit log?`);
      } else if (item.id === "published_policy") {
        add(75, `${Dept}'s transparency page does not include a posted ALPR policy. State law (Civil Code &sect;1798.90.51(a)) requires the policy to be posted publicly. Will ${dept} post the policy by a specific date, or confirm publicly that no current ALPR policy exists?`);
      }
    });

    // Camera density. When local percentile is available and stronger
    // than statewide, lead with the local number — "more than every
    // nearby agency" lands harder than "more than 85% of state peers"
    // because the council member reads it as a comparison they
    // recognize.
    const cameraPctile = report.percentiles && report.percentiles.cameras;
    const cameraPctileLocal = report.percentiles_local && report.percentiles_local.cameras;
    const localCamSample = report.peer_sample_local && report.peer_sample_local.cameras;
    if (cameraPctile != null && cameraPctile >= 85) {
      const pool = peerPoolName(report.peer_sample && report.peer_sample.cameras);
      const scopeWord = (localCamSample && localCamSample.scope === "county") ? "county" : "area";
      let stat;
      if (cameraPctileLocal === 100 && localCamSample && localCamSample.size) {
        stat = `more than every other agency in the same ${scopeWord} (${localCamSample.size} peers) &mdash; and more than <strong>${cameraPctile}%</strong> of California ${pool}`;
      } else if (cameraPctileLocal != null && cameraPctileLocal > cameraPctile && localCamSample) {
        stat = `more than <strong>${cameraPctileLocal}%</strong> of agencies in the same ${scopeWord} and more than <strong>${cameraPctile}%</strong> of California ${pool}`;
      } else {
        stat = `more than <strong>${cameraPctile}%</strong> of comparable California ${pool}`;
      }
      // Local 100% pctile is the strongest version (every nearby
      // agency has fewer); treat that as a higher pri.
      const camPri = (cameraPctileLocal === 100) ? 85 : (cameraPctile >= 95 ? 75 : 65);
      add(camPri, `${Dept} operates <strong>${report.stats.cameras}</strong> ALPR cameras &mdash; ${stat}. What deployment plan, study, or staff report documented why this number of cameras is appropriate for ${localPlace}, and where is it publicly available?`);
    }

    // Outbound geographic reach: question fires when the average
    // distance to recipients exceeds 150 miles. The farthest recipient
    // is also surfaced for emphasis. Most CA crawled agencies trip
    // this — sharing with statewide / interstate agencies is
    // widespread and rarely justified at the local level. The
    // "produce a record of crimes solved at distance" demand is
    // potent because almost no agency tracks this; the answer is
    // either "we don't track" or specific-and-small numbers.
    const avgKm = report.outbound_avg_km;
    const farthest = report.farthest_outbound;
    if (avgKm != null && avgKm > 241) {
      const avgMi = Math.round(avgKm / 1.60934);
      let farPart = "";
      if (farthest && farthest.distance_km) {
        const farMi = Math.round(farthest.distance_km / 1.60934);
        const farState = farthest.state ? `, ${escapeHtml(farthest.state)},` : "";
        farPart = ` &mdash; the farthest recipient is <strong>${escapeHtml(farthest.name)}</strong>${farState} <strong>${fmtInt(farMi)}</strong> miles away`;
      }
      // Pri scales with avg distance — 300+ mi is striking, 150-200 mi
      // is meaningful but routine for CA agencies sharing into NCRIC etc.
      const distPri = avgMi >= 300 ? 75 : avgMi >= 200 ? 60 : 50;
      add(distPri,
        `${Dept} shares ALPR data with other agencies an average of <strong>${fmtInt(avgMi)}</strong> miles away${farPart}. ` +
        `How does ${deptShort} track when ${localPlace} benefits from data sent to those remote agencies &mdash; ` +
        `i.e., crimes in ${localPlace} that get solved or closed because of that sharing &mdash; ` +
        `and what does that count look like?`
      );
    }

    // No-portal recipients: how many of the agencies receiving this
    // department's data have NO public transparency page of their own.
    // The implication is sharp: ~half of {dept}'s sharing partners
    // don't tell their own residents what they're doing with the data
    // {dept} sends them. Fires when the count is meaningful (>= 10);
    // small numbers don't justify the question.
    const ds = report.downstream_searches || {};
    const noPortal = ds.recipients_no_portal || 0;
    const recipTotal = ds.recipients_total || 0;
    if (noPortal >= 10 && recipTotal > 0) {
      const pctNoPortal = Math.round(100 * noPortal / recipTotal);
      // Pri scales with raw count of opaque recipients.
      const noPortalPri = noPortal >= 100 ? 75 : noPortal >= 50 ? 65 : 50;
      add(noPortalPri,
        `Of the <strong>${fmtInt(recipTotal)}</strong> agencies receiving ${dept}'s ALPR data, ` +
        `<strong>${fmtInt(noPortal)}</strong> (<strong>${pctNoPortal}%</strong>) do not appear to publish a Flock transparency page. ` +
        `How would I verify what those <strong>${fmtInt(noPortal)}</strong> agencies are doing with ${deptShort}'s ALPR data: what they retain, how long, who they re-share it with, and how they use it? ` +
        `What review of each of the <strong>${fmtInt(recipTotal)}</strong> recipient agencies' ALPR practices did ${deptShort} perform before sharing?`
      );
    }

    // High-search-volume recipients: total searches across all
    // recipients that publish search counts. Forces the question
    // "how many of those queries hit data sourced from your cameras?"
    // Almost no agency tracks this — answer is either "we don't" or
    // a specific count, both of which are accountable.
    const dsTotal = ds.total || 0;
    const dsWithData = ds.recipients_with_data || 0;
    const topR = (ds.top_researchers || [])[0];
    if (dsTotal >= 1000 && dsWithData > 0) {
      const recipTotalForRatio = ds.recipients_total || 0;
      // Note: this number is a floor. Most recipients don't publish
      // search counts, so the actual total is higher.
      const floorClause = recipTotalForRatio
        ? ` (this is a floor &mdash; only <strong>${fmtInt(dsWithData)}</strong> of ${fmtInt(recipTotalForRatio)} recipients publish their search counts)`
        : ` (across the <strong>${fmtInt(dsWithData)}</strong> recipients that publish search counts)`;
      // Pri scales with magnitude. Tens of thousands of searches is
      // genuinely striking; a few thousand reads as routine.
      const searchPri = dsTotal >= 50000 ? 88 : dsTotal >= 10000 ? 70 : 50;
      add(searchPri,
        `Agencies receiving ${dept}'s ALPR data collectively performed at least <strong>${fmtInt(dsTotal)}</strong> ALPR searches in the last 30 days${floorClause}. ` +
        `How does ${deptShort} verify that <em>each of</em> those partner-agency searches complies with ${deptShort}'s ALPR usage and privacy policies &mdash; ` +
        `does each search require ${deptShort}'s approval or an approved case number, do partner agencies submit search logs back to ${deptShort} for review, or does ${deptShort} rely on each partner agency's own internal policies? ` +
        `How much staff time does ${deptShort} spend verifying these searches each month?`
      );
    }

    // Audit log shows searches spanning more networks than inbound list.
    // Implies use of Flock's statewide/nationwide lookup features —
    // capabilities not granted by bilateral sharing. The 10% floor
    // filters out a handful of low-signal cases (e.g. one stray
    // statewide query) where the question would lack force.
    // Only fires when the agency publishes BOTH an inbound sharing
    // list AND a search audit on its own transparency portal. The
    // question relies on the agency's own numbers not adding up;
    // cross-portal inference would weaken that claim.
    const avi = report.audit_vs_inbound;
    if (avi && avi.exceeding_inbound_pct >= 10) {
      add(90,
        `${Dept}'s published search audit shows searches reaching more agency ` +
        `networks than ${deptShort}'s transparency portal lists as sharing data inbound &mdash; ` +
        `<strong>${avi.exceeding_inbound_pct}%</strong> of audited searches ` +
        `(${fmtInt(avi.exceeding_inbound_count)} of ${fmtInt(avi.rows_analyzed)}) ` +
        `reached more than the <strong>${fmtInt(avi.inbound_count)}</strong> agencies ` +
        `on the inbound list, with at least one search reaching <strong>${fmtInt(avi.max_network_count)}</strong> networks. ` +
        `Could ${deptShort} help reconcile the two numbers, and update the transparency page so a resident can see the actual scope of ${deptShort}'s sharing relationships?`
      );
    }

    // Downstream-access question: shares-to partners can typically
    // query this agency's data. Pull in the raw count so the question
    // has a concrete number — "shares to 279 other agencies" lands
    // harder than a generic prompt.
    const outCount = report.stats && report.stats.outbound_count;
    if (outCount && outCount > 0) {
      add(50, `${Dept} shares ALPR data with <strong>${outCount}</strong> other agencies. When one of those partner agencies runs a search against ${deptShort}'s ALPR data, how long does it take them to receive the results, and does anyone at ${deptShort} review the request before results are returned? If reviews are required, who handles them after-hours, on weekends, or during shift changes &mdash; where is that on-call coverage documented, and what does it cost the city each month?`);
    }
    // Generic "do your posted policies meet SB 34?" prompt is only
    // worth raising when the agency hasn't already cleared the basic
    // posting bar. If they posted a policy link, the SB 34 check is
    // green and the next-level critique requires actually reading
    // the policy text — that's a council member's job, not a prompt
    // worth canning here.
    const publishedPolicyItem = sb34.find(function(i) { return i.id === "published_policy"; });
    const policyPosted = publishedPolicyItem && publishedPolicyItem.value === true;
    if (!policyPosted) {
      add(40,
        `When ${dept} posts its ALPR policy publicly, the policy itself must address specific items state law calls for. Common gaps in posted ALPR policies include: ` +
      "(a) audits that review search activity, not just sharing configuration; " +
      "(b) a defined process for revoking user accounts when personnel leave or change roles; " +
      "(c) a clear definition of authorized purposes and authorized users; " +
      `(d) policy text that describes the platforms ${dept} actually uses today, not legacy systems. ` +
      `Will ${dept} confirm publicly that its posted policy &mdash; once published &mdash; covers each of these items, and produce the policy text for review?`
      );
    }
    add(30, `How many user accounts currently have access to ${dept}'s Flock platform, and what are the current roles of those users within the city?`);
    add(65,
      `The standard Flock master service agreement (&sect;5.3) lets Flock &mdash; the vendor &mdash; disclose the city's ALPR data to third parties on its own "good faith belief," without ${dept}'s approval or notification. A February 2026 ` +
      `<a href="https://stateofsurveillance.org/news/flock-safety-class-action-lawsuit-california-federal-data-sharing-2026/" target="_blank" rel="noopener">California class action</a> ` +
      `alleges Flock used such authority to let federal and out-of-state agencies search SFPD's database 1.6 million times and Los Altos's over 1 million times without local approval, in alleged violation of SB 34. Is &sect;5.3 in the city's contract, how often does ${deptShort} audit for unauthorized vendor-side access &mdash; and what happens if such a change occurs between audits?`
    );

    return finalize(qs);
  }

  // ── Footer ──
  function renderFooter(report, meta) {
    const popSrc = meta.population_source;
    // Absolute URL so the QR scan works from a printed copy.
    const thisUrlAbs = new URL(
      `report.html?agency=${report.slug}`,
      location.href
    ).toString();
    const mapUrlAbs = new URL(
      `sharing_map.html#${report.slug}`,
      location.href
    ).toString();

    let html = '<div class="footer" style="display:flex;gap:16px;align-items:flex-start">';
    html += '<div style="flex:1">';
    html += `<p><strong>Source:</strong> Flock Safety transparency portals, compiled by the sm-alpr project.</p>`;
    if (popSrc) {
      // Link the ACS product + Census Bureau so readers can click
      // through to source. Print CSS strips underlines so the PDF
      // stays clean — the source text still appears verbatim.
      const vintage = popSrc.vintage || 2023;
      const acsUrl = "https://www.census.gov/programs-surveys/acs";
      const censusUrl = "https://www.census.gov/";
      html += `<p><strong>Population data:</strong> ` +
        `<a href="${acsUrl}" target="_blank" rel="noopener">ACS 5-Year Estimates (${vintage})</a>` +
        `, <a href="${censusUrl}" target="_blank" rel="noopener">U.S. Census Bureau</a>.</p>`;
    }
    html += `<p><strong>Interactive map:</strong> <a href="${escapeHtml(mapUrlAbs)}">${escapeHtml(mapUrlAbs)}</a></p>`;
    html += `<p><strong>This report:</strong> <a href="${escapeHtml(thisUrlAbs)}">${escapeHtml(thisUrlAbs)}</a></p>`;
    html += `<p><strong>Report generated:</strong> ${new Date().toLocaleDateString("en-US", {year: "numeric", month: "long", day: "numeric"})}</p>`;
    const crawlDate = report.crawled_date;
    const crawlPhrase = crawlDate ? `the ${escapeHtml(crawlDate)} portal crawl` : "the last portal crawl";
    html += `<p>This report reflects data from ${crawlPhrase}. Sharing relationships and stats may have changed since.</p>`;
    html += '</div>';
    // QR code block (right column)
    html += '<div style="flex:0 0 auto;text-align:center">';
    html += `<div id="report-qrcode" aria-label="QR code linking to this report"></div>`;
    html += `<p style="font-size:8.5pt;color:#666;margin-top:4px;max-width:110px">Scan to open this report online</p>`;
    html += '</div>';
    html += '</div>';
    // Deferred: render the QR after this HTML is inserted.
    setTimeout(function() { renderQrCode("report-qrcode", thisUrlAbs, { size: 100 }); }, 0);
    return html;
  }

  // Render a QR code into the element with the given id. Wraps the SVG
  // in an anchor so clicking (on screen) opens the URL — when printed,
  // the QR is scannable from paper. opts.size controls the px dimension.
  function renderQrCode(id, url, opts) {
    opts = opts || {};
    const el = document.getElementById(id);
    if (!el || typeof qrcode === "undefined") return;
    try {
      const qr = qrcode(0, "M");
      qr.addData(url);
      qr.make();
      const size = opts.size || 100;
      el.innerHTML = `<a href="${escapeHtml(url)}" target="_blank" rel="noopener" title="${escapeHtml(url)}" style="display:inline-block;line-height:0">${qr.createSvgTag({ cellSize: 3, margin: 2 })}</a>`;
      const svg = el.querySelector("svg");
      if (svg) {
        svg.setAttribute("width", String(size));
        svg.setAttribute("height", String(size));
      }
    } catch (e) {
      console.warn("QR render failed:", e);
    }
  }

  // ── Bootstrap ──
  function init() {
    const printBtn = document.getElementById("print-btn");
    if (printBtn) {
      printBtn.addEventListener("click", function() { window.print(); });
    }

    const params = new URLSearchParams(location.search);
    const slug = params.get("agency");
    if (!slug) {
      document.getElementById("report").innerHTML = `
        <div class="error-box">
          <h1 style="margin-top:0">No agency specified</h1>
          <p>Pass <code>?agency=&lt;slug&gt;</code> in the URL, or pick an agency from the <a href="sharing_map.html">interactive map</a>.</p>
        </div>`;
      return;
    }

    fetch("data/report_data.json?v=" + Date.now())
      .then(function(r) { return r.json(); })
      .then(function(data) {
        render(data, slug);
        wireAgencySearch(data, slug);
      })
      .catch(function(err) {
        document.getElementById("report").innerHTML = `
          <div class="error-box">
            <h1 style="margin-top:0">Failed to load report data</h1>
            <p>${escapeHtml(err.message || String(err))}</p>
          </div>`;
      });
  }

  // Toolbar "Jump to another agency" search. Filters by case-
  // insensitive substring, ranks exact > startsWith > contains, caps
  // at 12 results. Navigating follows report.html?agency=<slug>.
  // Keyboard: ↓/↑ to move highlight, Enter to open, Esc to close.
  function wireAgencySearch(data, currentSlug) {
    const input = document.getElementById("agency-search-input");
    const results = document.getElementById("agency-search-results");
    if (!input || !results) return;
    const index = Object.entries(data.reports || {}).map(function(entry) {
      const [slug, r] = entry;
      return {
        slug: slug,
        name: r.name || slug,
        state: r.state || "",
        _nameLower: (r.name || slug).toLowerCase(),
      };
    });
    let highlightIdx = -1;

    function doSearch(q) {
      q = q.trim().toLowerCase();
      if (!q) { results.classList.remove("open"); results.innerHTML = ""; highlightIdx = -1; return; }
      const matches = [];
      for (let i = 0; i < index.length; i++) {
        const e = index[i];
        if (e.slug === currentSlug) continue;
        const n = e._nameLower;
        let score = 0;
        if (n === q) score = 100;
        else if (n.startsWith(q)) score = 50;
        else if (n.includes(q)) score = 10;
        if (score > 0) matches.push({ e: e, score: score });
      }
      matches.sort(function(a, b) {
        if (b.score !== a.score) return b.score - a.score;
        return a.e._nameLower.localeCompare(b.e._nameLower);
      });
      const shown = matches.slice(0, 12);
      if (!shown.length) {
        results.innerHTML = '<div class="sr-empty">No matching agencies</div>';
      } else {
        results.innerHTML = shown.map(function(m) {
          const state = m.e.state ? `<span class="sr-state">${escapeHtml(m.e.state)}</span>` : "";
          return `<a href="report.html?agency=${encodeURIComponent(m.e.slug)}" data-slug="${escapeHtml(m.e.slug)}">${escapeHtml(m.e.name)}${state}</a>`;
        }).join("");
      }
      results.classList.add("open");
      highlightIdx = -1;
    }

    function updateHighlight() {
      const links = results.querySelectorAll("a");
      links.forEach(function(a, i) { a.classList.toggle("hl", i === highlightIdx); });
      if (highlightIdx >= 0 && links[highlightIdx]) {
        links[highlightIdx].scrollIntoView({ block: "nearest" });
      }
    }

    input.addEventListener("input", function() { doSearch(input.value); });
    input.addEventListener("keydown", function(e) {
      const links = results.querySelectorAll("a");
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (!results.classList.contains("open")) doSearch(input.value);
        if (links.length) {
          highlightIdx = Math.min(highlightIdx + 1, links.length - 1);
          updateHighlight();
        }
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (links.length) {
          highlightIdx = Math.max(highlightIdx - 1, 0);
          updateHighlight();
        }
      } else if (e.key === "Enter") {
        if (highlightIdx >= 0 && links[highlightIdx]) {
          e.preventDefault();
          window.location.href = links[highlightIdx].href;
        } else if (links.length === 1) {
          e.preventDefault();
          window.location.href = links[0].href;
        }
      } else if (e.key === "Escape") {
        results.classList.remove("open");
        input.blur();
      }
    });
    input.addEventListener("focus", function() {
      if (input.value.trim()) doSearch(input.value);
    });
    document.addEventListener("click", function(e) {
      if (!e.target.closest("#agency-search")) {
        results.classList.remove("open");
      }
    });
  }

  // (Previously: opened every <details> on beforeprint so the PDF
  // would include the full sharing list as a wall of text. Removed:
  // the expanded lists are now hidden entirely in print and replaced
  // with a QR code to the live sharing map — see .sharing-print-qr.)

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
