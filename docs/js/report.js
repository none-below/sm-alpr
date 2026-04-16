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

  const FLAG_LABELS = {
    private: "PRIVATE",
    out_of_state: "OUT OF STATE",
    federal: "FEDERAL",
    fusion_center: "FUSION CENTER",
    decommissioned: "DECOMMISSIONED",
    test: "TEST/DEMO",
  };

  // Short tooltips explaining why each flag kind is a concern. Shown on
  // hover and spelled out in-line where flagged recipients are listed.
  // Fusion centers in particular vary: NCRIC's concerns are federal
  // entanglement, others raise different concerns. Per-entity specifics
  // come from the agency's `notes` field in the registry.
  const FLAG_EXPLANATIONS = {
    private: "Private entities are not \u201cpublic agencies\u201d under CA Civil Code \u00a71798.90.5(f). Sharing ALPR data with them likely violates \u00a71798.90.55(b).",
    out_of_state: "CA Civil Code \u00a71798.90.55(b) and AG Bulletin 2023-DLE-06 prohibit sharing with non-California agencies.",
    federal: "Federal agencies are not \u201cagencies of the state\u201d under \u00a71798.90.5(f). AG Bulletin 2023-DLE-06 prohibits sharing with federal agencies.",
    fusion_center: "Fusion centers are multi-agency information-sharing hubs. Whether they qualify as \u201cpublic agencies\u201d under CA Civil Code \u00a71798.90.5(f) depends on their specific charter, governance, funding, and staffing \u2014 see the per-entity notes below for the concerns specific to each one.",
    decommissioned: "Decommissioned / do-not-use account \u2014 ALPR data should not be going to an inactive entity.",
    test: "Test/demo account, not a real public agency \u2014 ALPR data should not be routed here.",
  };

  // Maps a stats-table metric key to the corresponding transparency
  // checklist id. When the agency doesn't publish a given stat, we
  // use this to look up the peer publish-rate and show it as a
  // "X% of peers report this" hint next to "not reported".
  const TRANSPARENCY_CHECK_FOR_METRIC = {
    cameras: "camera_count",
    vehicles_30d: "vehicles_30d",
    hotlist_hits_30d: "hotlist_hits",
    searches_30d: "searches_30d",
  };

  const METRIC_LABELS = {
    cameras: "Cameras",
    vehicles_30d: "Vehicles detected (30d)",
    hotlist_hits_30d: "Hotlist hits (30d)",
    searches_30d: "Searches (30d)",
    outbound: "Agencies it shares to",
  };

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

  // ── Header ──
  function renderHeader(report) {
    const roleLabel = report.agency_role ? report.agency_role : "";
    const typeLabel = agencyTypeLabel(report.agency_type);
    const combinedType = [typeLabel, roleLabel].filter(Boolean).join(" — ");
    const geoName = (report.geo && report.geo.name) || "";
    const thisUrlAbs = new URL(`report.html?agency=${report.slug}`, location.href).toString();

    // Header layout: name/subtitle on the left, QR on the right. The QR
    // links to the live dynamic report — prints as scannable, clicks as
    // link on screen.
    let html = '<div class="report-header">';
    html += '<div class="report-header-main">';
    html += `<h1>${escapeHtml(report.name)}</h1>`;
    html += `<p class="subtitle">ALPR Scorecard &middot; Flock transparency data &middot; generated ${new Date().toLocaleDateString("en-US", {year: "numeric", month: "long", day: "numeric"})}</p>`;
    html += '</div>';
    html += '<div class="report-header-qr">';
    html += '<div id="top-qrcode" aria-label="QR code linking to the live online version of this report"></div>';
    html += '<div class="qr-caption">Scan for live version</div>';
    html += '</div>';
    html += '</div>';
    // Defer QR render until the HTML is inserted.
    setTimeout(function() { renderQrCode("top-qrcode", thisUrlAbs, { size: 90 }); }, 0);

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
    // what the agency publishes and what other records show (internal
    // dashboards, PRA responses, testimony). Concerns are tagged with
    // a `section` field (e.g. "stat:cameras", "check:documented_audit")
    // so the full body renders near the relevant section; here we show
    // a TL;DR index at the top of the report listing the titles with
    // jump links. Concerns without a section (or with section="general")
    // render fully here.
    if (report.data_concerns && report.data_concerns.length) {
      const generalConcerns = report.data_concerns.filter(function(c) {
        return !c.section || c.section === "general";
      });
      const taggedConcerns = report.data_concerns.filter(function(c) {
        return c.section && c.section !== "general";
      });
      // Header makes the agency-specific nature explicit — these are
      // NOT generic data-quality notes about the transparency program;
      // they're documented discrepancies and gaps specific to THIS
      // agency, pulled from the project's findings for it.
      html += '<div class="data-concerns">';
      html += `<div class="data-concerns-header">\u26a0 Known concerns specific to ${escapeHtml(report.name)}</div>`;
      html += `<div class="data-concerns-sub">Documented discrepancies between what this agency publishes and what other public records (internal dashboards, PRA responses, testimony) show. Each concern below links to the relevant section further down the report.</div>`;
      // Fully-inline rendering for untagged concerns
      generalConcerns.forEach(function(c) {
        html += renderDataConcernBody(c);
      });
      // Tagged: index only (title + "↓ see below" jump link). Anchor
      // id uses the concern's index in the full data_concerns array
      // so the inline renderer (which iterates that same array) can
      // produce matching ids.
      if (taggedConcerns.length) {
        html += '<ul class="data-concerns-index">';
        report.data_concerns.forEach(function(c, i) {
          if (!c.section || c.section === "general") return;
          const anchorId = `concern-${i}`;
          html += `<li><a href="#${anchorId}">${escapeHtml(c.title || "(concern)")}</a> <span class="muted">&mdash; see below</span></li>`;
        });
        html += '</ul>';
      }
      html += '</div>';
    }

    return html;
  }

  // ── Stats ──
  function renderStats(report, meta) {
    if (!report.crawled) return "";

    const stats = report.stats || {};
    const per1k = report.per_1000 || {};
    const medians = report.medians || {};
    const percentiles = report.percentiles || {};
    const peerSample = report.peer_sample || {};

    let html = `<h2>30-Day Activity</h2>`;

    // Row order prioritises "what this agency does with the data"
    // (searches, shared searches, agencies shared to) before "what
    // the agency has" (cameras and derivatives) and the raw activity
    // metrics (vehicles, hotlist hits). Councils asking accountability
    // questions care most about query and distribution behaviour.
    const rows = [
      { metric: "searches_30d", label: "Searches performed", value: stats.searches_30d },
      // Compound metric — this agency's searches plus all its crawled
      // recipients' searches. Shows data-exposure scale even when the
      // agency itself is a light searcher.
      //
      // Coverage is usually far below 100% because many Flock portals
      // don't publish search counts. Surface that as a visible
      // sublabel (not just a tooltip) so councils understand the
      // reported number is a floor, not the full volume.
      // Compound: this agency's own searches plus all crawled
      // recipients' searches. The coverage breakdown lives in the
      // value cell (coverage-tag) rather than as a metric sublabel —
      // keeps the label clean and puts the caveat next to the number
      // it qualifies.
      {
        metric: "downstream",
        label: "Searches reaching this data",
        tooltip: (report.downstream_searches
          ? `${fmtInt(report.downstream_total || 0)} = searches this agency publishes + searches published by ${report.downstream_searches.recipients_with_data} of its ${report.downstream_searches.recipients_total} recipients.`
          : undefined),
        value: report.downstream_total,
        isDownstream: true,
      },
      { metric: "outbound", label: "Agencies it shares to", value: stats.outbound_count },
      { metric: "cameras", label: "Cameras", value: stats.cameras },
      { metric: "vehicles_30d", label: "Vehicles detected", value: stats.vehicles_30d },
      {
        metric: "hotlist_hits_30d",
        label: "Hotlist hits",
        // Per Flock's own description (Flock Blog, May 14, 2025), a
        // Hot List match fires when a detected plate matches either
        // the FBI NCIC feed or a custom list the operator has
        // configured. Known data-quality issues documented in public
        // audits: high NCIC false-positive rates (e.g., 77% wrong-
        // state matches in a Story County, IA audit) and custom
        // lists frequently lacking case numbers or expiration dates
        // (Harris County, TX: > 50% of 800 entries had no case
        // number, 73% never expire). See
        // haveibeenflocked.com/news/hotlist-mess.
        sublabel: "a plate detection matching the FBI NCIC feed or a custom list the operator has configured",
        caveatHtml: (
          "Hotlist hits are <strong>not a vetted crime indicator</strong>. " +
          "NCIC matches include known false positives (e.g., 77% wrong-state matches in one Iowa audit), " +
          "and custom lists are frequently added without case numbers or expirations. " +
          "See <a href=\"https://haveibeenflocked.com/news/hotlist-mess\" target=\"_blank\" rel=\"noopener\">haveibeenflocked.com/news/hotlist-mess</a>."
        ),
        value: stats.hotlist_hits_30d,
      },
    ];

    // Peer sample note above the table — explicitly names who we're comparing to.
    const firstPeer = Object.values(peerSample)[0];
    if (firstPeer) {
      const fallback = firstPeer.fallback;
      const typeLabel = firstPeer.type === "all" ? "California agencies" : `California ${agencyTypeLabel(firstPeer.type)} agencies`;
      html += `<p class="muted" style="font-size:9.5pt">Comparisons below are against <strong>${firstPeer.size} ${typeLabel}</strong> with public transparency pages${fallback ? " (too few peers of the same type; falling back to all CA agencies)" : ""}. The median is the peer at the 50th percentile.</p>`;
    }

    const per1kPct = report.percentiles_per_1000 || {};
    const per1kMed = report.medians_per_1000 || {};
    const localPct = report.percentiles_local || {};
    const localMed = report.medians_local || {};
    const localSample = report.peer_sample_local || {};
    const localPerPct = report.percentiles_per_1000_local || {};
    const localPerMed = report.medians_per_1000_local || {};

    // Add cameras-per-sqmi as its own row, slotted right after cameras.
    // This keeps density in the same table as the other comparisons.
    const rowsWithDensity = [];
    rows.forEach(function(r) {
      rowsWithDensity.push(r);
      if (r.metric === "cameras" && report.cameras_per_sqmi != null) {
        rowsWithDensity.push({
          metric: "cameras_per_sqmi",
          label: "Cameras per square mile",
          value: report.cameras_per_sqmi,
          isDensity: true,
        });
      }
    });

    // Collapsed 3-column layout: Metric | This agency | Per 1,000 residents.
    // Each value cell stacks the number on top and the statewide/local
    // rank lines beneath, so everything about one metric fits on one
    // row without pushing past the page width.
    // Use <thead> so browsers repeat the column headers on each
    // page when the table breaks across pages in print.
    html += '<div class="stats-table-wrap"><table class="stats-table">';
    html += '<thead><tr><th>Metric</th><th class="num">This agency</th>';
    if (report.population) html += '<th class="num">Per 1,000 residents</th>';
    html += '</tr></thead><tbody>';

    rowsWithDensity.forEach(function(r) {
      // Route each row to the right percentile/median fields. Most
      // metrics use the standard "percentiles" / "medians" dicts;
      // density and downstream are top-level scalars on the report.
      let pctile, med, lpctile, lmed, lsamp;
      if (r.isDensity) {
        pctile = report.percentile_density;
        med = report.median_density;
        lpctile = report.percentile_density_local;
        lmed = report.median_density_local;
        lsamp = report.peer_sample_density_local;
      } else if (r.isDownstream) {
        pctile = report.percentile_downstream;
        med = report.median_downstream;
        lpctile = report.percentile_downstream_local;
        lmed = report.median_downstream_local;
        lsamp = report.peer_sample_downstream_local;
      } else {
        pctile = percentiles[r.metric];
        med = medians[r.metric];
        lpctile = localPct[r.metric];
        lmed = localMed[r.metric];
        lsamp = localSample[r.metric];
      }

      const per = per1k[r.metric];
      const perPct = per1kPct[r.metric];
      const perMed = per1kMed[r.metric];
      const lperPct = localPerPct[r.metric];
      const lperMed = localPerMed[r.metric];

      html += '<tr>';
      // Label cell can include a sublabel (small gray clarifier) and
      // a title= tooltip on hover for the composition of a metric.
      // Used for "Searches reaching this data" so the reader doesn't
      // misread it as the agency's own search count. Downstream row
      // gets amber `.warn` styling on the sublabel — its undercount
      // caveat needs to land, not blend in.
      let labelCell = `<td`;
      if (r.tooltip) labelCell += ` title="${escapeHtml(r.tooltip)}"`;
      labelCell += `>${escapeHtml(r.label)}`;
      if (r.sublabel) {
        const subCls = r.isDownstream ? "metric-sublabel warn" : "metric-sublabel";
        labelCell += `<div class="${subCls}">${escapeHtml(r.sublabel)}</div>`;
      }
      // caveatHtml is a trusted-source HTML fragment (code-supplied,
      // not user input) for metrics that need a citation/link the
      // sublabel can't carry inline. Rendered in a distinct amber
      // box below the sublabel so it reads as a caveat, not a
      // definition.
      if (r.caveatHtml) {
        labelCell += `<div class="metric-caveat">${r.caveatHtml}</div>`;
      }
      labelCell += `</td>`;
      html += labelCell;
      // Sparkline: statewide peer distribution for this metric, with
      // a triangle marking where this agency falls on the same axis.
      // Metric key differs between the metadata and the report data
      // (e.g. "cameras_per_sqmi" lives under report.cameras_per_sqmi),
      // so resolve the right (key, value) pair for each row.
      let sparkMetric, sparkValue;
      if (r.isDensity) {
        sparkMetric = "cameras_per_sqmi";
        sparkValue = report.cameras_per_sqmi;
      } else if (r.isDownstream) {
        sparkMetric = "downstream";
        sparkValue = report.downstream_total;
      } else {
        sparkMetric = r.metric;
        sparkValue = r.value;
      }
      const sparkHist = peerHistogramFor(sparkMetric, report.agency_type, meta);
      const sparkHtml = sparkHist ? `<div class="spark-wrap">${sparklineSvg(sparkHist, sparkValue)}</div>` : "";
      // Downstream row: coverage tag explains the floor number AND
      // why it's a floor. Breaks "not counted" into (a) recipients
      // with no transparency portal and (b) recipients with a portal
      // but no published search count, so the reader can see at a
      // glance why the denominator is what it is. Also notes when
      // this agency itself doesn't publish its own search count.
      let coverageHtml = "";
      if (r.isDownstream && report.downstream_searches) {
        const ds = report.downstream_searches;
        if (ds.recipients_total > 0) {
          const pctStr = pct(ds.recipients_with_data, ds.recipients_total);
          let lines = [
            `<strong>Based on ${pctStr}% of recipients</strong> (${fmtInt(ds.recipients_with_data)} of ${fmtInt(ds.recipients_total)}).`,
          ];
          const notCounted = [];
          if (ds.recipients_no_portal > 0) {
            notCounted.push(`${fmtInt(ds.recipients_no_portal)} have no transparency portal`);
          }
          if (ds.recipients_portal_no_search_field > 0) {
            notCounted.push(`${fmtInt(ds.recipients_portal_no_search_field)} have a portal but don\u2019t publish a search count`);
          }
          if (notCounted.length) {
            lines.push("Not counted: " + notCounted.join("; ") + ".");
          }
          if (ds.self_included === false) {
            lines.push("This agency also doesn\u2019t publish its own search count.");
          }
          lines.push("<em>The real total is likely higher.</em>");
          coverageHtml = `<div class="coverage-tag">${lines.join(" ")}</div>`;
        }
      }
      // Inline data-concern for this metric row (e.g. SMPD's 66 vs
      // 80 device discrepancy is tagged section="stat:cameras" and
      // appears attached to the Cameras row).
      const concernSectionKey = r.isDensity
        ? "stat:cameras_per_sqmi"
        : r.isDownstream
        ? "stat:downstream"
        : `stat:${r.metric}`;
      const inlineConcern = concernsForSection(report, concernSectionKey);
      // When the agency doesn't publish this metric, add a hint
      // showing what fraction of peers DO — tells the reader
      // whether the omission is typical or unusual.
      let notReportedHint = "";
      if (r.value == null && r.metric) {
        const checkId = TRANSPARENCY_CHECK_FOR_METRIC[r.metric];
        if (checkId) {
          const item = (report.checklist_transparency || []).find(function(x) { return x.id === checkId; });
          if (item && item.peer_applicable > 0) {
            const p = Math.round(100 * item.peer_count / item.peer_applicable);
            const peerGroup = item.peer_type === "all" ? "California agencies" : `California ${agencyTypeLabel(item.peer_type)} agencies`;
            notReportedHint = `<div class="not-reported-hint">${p}% of ${peerGroup} with a transparency portal publish this field.</div>`;
          }
        }
      }
      html += `<td class="num ${cellClassFor(pctile, r.metric)}">${valueBlockHtml(r.value, med, lmed, pctile, lpctile, lsamp)}${notReportedHint}${coverageHtml}${sparkHtml}${inlineConcern}</td>`;
      if (report.population) {
        // Per-capita doesn't apply to every metric:
        //  - cameras/sqmi: a density is already a ratio
        //  - downstream-searches: shown with a top-searchers list
        //  - outbound (agencies shared to): a count of entities, not
        //    a count of events/people; "agencies shared to per 1,000
        //    residents" isn't a meaningful measure
        // For these, leave the cell blank (no value, no color, no tag).
        if (r.isDownstream) {
          html += `<td class="downstream-researchers">${topResearchersHtml(report.downstream_searches)}</td>`;
        } else if (r.isDensity || r.metric === "outbound") {
          html += `<td class="num"></td>`;
        } else {
          // Per-capita cell: same shape as primary, including a
          // sparkline for the per-1,000 peer distribution.
          const perSparkHist = peerHistogramFor(r.metric + "_per_1000", report.agency_type, meta);
          const perSparkHtml = perSparkHist ? `<div class="spark-wrap">${sparklineSvg(perSparkHist, per)}</div>` : "";
          html += `<td class="num ${cellClassFor(perPct, r.metric)}">${valueBlockHtml(per, perMed, lperMed, perPct, lperPct, lsamp)}${perSparkHtml}</td>`;
        }
      }
      html += '</tr>';
    });

    html += '</tbody></table></div>';

    // Caption: explain what "state" and "local" mean. The local scope
    // can be either "county" or "nearby" (50 miles); pick the scope
    // that's actually being used for this agency.
    let localScopeNote = "";
    const anyLocalSample = Object.values(localSample)[0];
    if (anyLocalSample) {
      if (anyLocalSample.scope === "county") {
        localScopeNote = `Local rank is against other crawled California agencies in the same county (${anyLocalSample.size} peers).`;
      } else if (anyLocalSample.scope === "nearby") {
        localScopeNote = `Local rank is against other crawled California agencies within 50 miles (${anyLocalSample.size} peers). The agency's county doesn't have enough crawled peers for a same-county comparison.`;
      }
    }
    html += `<p class="muted" style="font-size:9.5pt">State rank compares this agency against California ${agencyTypeLabel(report.agency_type)} agencies with transparency pages. ` +
      (localScopeNote ? localScopeNote + " " : "Local rank omitted: too few local peers to compute. ") +
      (report.population
        ? `"Per 1,000 residents" normalizes by city population so small towns and big cities can be compared on the same scale. `
        : ``) +
      `A percentile is read as "X% of peers have a lower value" &mdash; so 80th percentile means this agency is higher than 80% of its peers. Red cells flag percentiles that warrant scrutiny; below-median values are left uncolored because a low rank relative to peers doesn't mean the absolute number is acceptable.</p>`;

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
  function renderDataConcernBody(c, anchorId) {
    let html = '<div class="data-concern"';
    if (anchorId) html += ` id="${escapeHtml(anchorId)}"`;
    html += '>';
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
    report.data_concerns.forEach(function(c, i) {
      if (!c.section || c.section === "general") return;
      if (c.section !== sectionKey) return;
      html += renderDataConcernBody(c, `concern-${i}`);
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

    const W = 540, H = 320;
    const PAD_L = 6, PAD_R = 6, PAD_T = 6, PAD_B = 22;  // extra bottom pad for caption

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

    // Now fit the aspect-correct data into the viewport without
    // squishing either axis: pick the tighter scale, center the
    // shorter axis with padding.
    const viewW = W - PAD_L - PAD_R;
    const viewH = H - PAD_T - PAD_B;
    const xScalePerLng = viewW / effectiveLngRange;
    const yScalePerLat = viewH / latRange;
    const scale = Math.min(xScalePerLng, yScalePerLat);
    const usedW = effectiveLngRange * scale;
    const usedH = latRange * scale;
    const offX = PAD_L + (viewW - usedW) / 2;
    const offY = PAD_T + (viewH - usedH) / 2;

    function proj(lat, lng) {
      const x = offX + (lng - minLng) * lngScale * scale;
      const y = offY + (maxLat - lat) * scale;
      return [x, y];
    }

    let svg = `<svg class="mini-map" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Mini map of outbound sharing recipients">`;
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
    // the red dots aren't obscured
    recipients.filter(function(r) { return !r.kind; }).forEach(function(r) {
      const [x, y] = proj(r.lat, r.lng);
      svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="1.8" fill="#94a3b8" opacity="0.7"/>`;
    });
    recipients.filter(function(r) { return r.kind; }).forEach(function(r) {
      const [x, y] = proj(r.lat, r.lng);
      svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.8" fill="#dc2626"/>`;
    });

    // Subject: larger cyan ring
    svg += `<circle cx="${sx.toFixed(1)}" cy="${sy.toFixed(1)}" r="6" fill="none" stroke="#06b6d4" stroke-width="2"/>`;
    svg += `<circle cx="${sx.toFixed(1)}" cy="${sy.toFixed(1)}" r="3" fill="#06b6d4"/>`;

    // Farthest label
    if (farthest) {
      const fr = recipients.find(function(r) { return r.slug === farthest.slug; });
      if (fr) {
        const [fx, fy] = proj(fr.lat, fr.lng);
        const labelX = fx < W / 2 ? fx + 6 : fx - 6;
        const anchor = fx < W / 2 ? "start" : "end";
        svg += `<text x="${labelX.toFixed(1)}" y="${(fy - 5).toFixed(1)}" font-size="9" fill="#92400e" text-anchor="${anchor}">${escapeHtml(farthest.name)}</text>`;
      }
    }

    // Caption — count + legend. Separate from SVG so text flow is
    // native HTML (easier styling, selectable).
    const flaggedCount = recipients.filter(function(r) { return r.kind; }).length;
    const cleanCount = recipients.length - flaggedCount;
    svg += `<text x="${PAD_L}" y="${H - 6}" font-size="9" fill="#475569">${recipients.length} geocoded recipients (${cleanCount} clean, ${flaggedCount} flagged). Red lines: sharing to flagged entities. Amber dashed: farthest.</text>`;
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
    html += `<p class="muted">Signals tied to California Civil Code &sect;1798.90.51&ndash;.55. Red items indicate potential compliance concerns a council member may want to raise.</p>`;
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
    html += `<p class="legal-note" style="border-left: 3px solid var(--warn-border); background: var(--warn-bg); color: #78350f; margin: 6px 0 10px 0"><strong>Important:</strong> These are surface-signal checks. A green check means the minimum signal appears on the agency's transparency page &mdash; not that the agency substantively complies with the law. A posted policy may be out of date or incomplete; a documented audit process may not actually be executed; a clean sharing list may include recipients never reviewed for eligibility. Use this as a <em>starting point</em> for council questions, not a certification of compliance.</p>`;
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

    // One-line summary of the items we hid because they're near-
    // universal (this agency does them AND > 90% of peers do them).
    // Names the items so the reader knows what was skipped, without
    // reserving full UI real estate for each.
    if (hiddenPassItems.length) {
      const names = hiddenPassItems.map(function(i) { return labelFor(i); }).join(", ");
      html += `<p class="muted baseline-note" style="font-size:9pt; margin:4px 0 0 0">` +
        `<strong>Also passing</strong> (\u2265 90% of peers also pass &mdash; baseline, not distinguishing): ${escapeHtml(names)}.` +
        `</p>`;
    }
    return html;
  }

  // Phrases peer stats concretely, always leading with the "how many
  // agencies pass" number. Uncrawled agencies are excluded from the
  // denominator (we can't verify what isn't public) — that context is
  // included as a separate sentence when relevant. For SB 34 items
  // (non-compact), also renders a statewide line underneath so the
  // reader sees if the concern is type-specific or CA-wide.
  function formatPeerStat(item, peerTypeLabel, compact) {
    const applicable = item.peer_applicable != null ? item.peer_applicable : item.peer_total;
    const total = item.peer_total;
    const pass = item.peer_count;
    const fail = applicable - pass;

    if (applicable === 0) {
      return `No ${peerTypeLabel} publish enough info to evaluate.`;
    }

    const passPct = pct(pass, applicable);
    const failPct = pct(fail, applicable);

    if (compact) {
      // Multi-column cards: keep it one short sentence.
      return `${pass}/${applicable} peers pass (${passPct}%). ${fail} fail (${failPct}%).`;
    }

    let line = `Among ${applicable} ${peerTypeLabel} we can verify: ` +
      `<strong>${pass} (${passPct}%) pass this check</strong>; ` +
      `${fail} (${failPct}%) do not.`;
    if (applicable < total) {
      const unknown = total - applicable;
      line += ` (${unknown} more ${peerTypeLabel} don't publish enough to evaluate.)`;
    }

    // Statewide line: shown when the item carries state-wide counts
    // (added for SB 34). Only include when the state numbers differ
    // meaningfully from the type numbers — otherwise it's redundant.
    if (item.state_applicable != null && item.state_total != null) {
      const sApp = item.state_applicable;
      const sPass = item.state_count;
      if (sApp > applicable) {
        const sPct = pct(sPass, sApp);
        line += `<br><span class="muted">Statewide: ${sPass} of ${sApp} CA agencies (${sPct}%) pass this check.</span>`;
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
        (kindGroups[f.kind] = kindGroups[f.kind] || []).push(f);
      });

      // Deliberate render order: private (direct SB 34 violation) first,
      // then out-of-state, federal, fusion centers (indirect federal
      // concern), then decommissioned/test accounts. Groups not in this
      // list (shouldn't happen, but be safe) append at the end.
      const KIND_ORDER = ["private", "out_of_state", "federal", "fusion_center", "decommissioned", "test"];
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
      const label = FLAG_LABELS[kind] || kind.toUpperCase();
      html += ` <span class="flag-tag kind-${escapeHtml(kind)}">${escapeHtml(label)}</span>`;
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

    // Cap the visible regional table to the 12 closest agencies (was
    // 20). Most readers only need the immediate neighborhood; for the
    // full list they can click through to any row's agency page.
    const REGIONAL_ROW_LIMIT = 12;

    let html = `<h2>Regional Context</h2>`;
    html += `<p class="muted">${Math.min(REGIONAL_ROW_LIMIT, regional.length)} closest crawled California agencies within ${radiusMi} miles (of ${regional.length} total). Per-capita columns normalize by city population so small towns and big cities can be compared on the same scale.</p>`;
    html += '<table>';
    // Collapsed: the per-1,000-residents rates live in parens inside
    // their parent cell ("66 (0.64/1k)"), not as separate columns.
    // Keeps the table narrow enough to fit the page even when sharing
    // plus flagged columns are present.
    //
    // Sortable: each header has data-sort-key; a click handler (wired
    // in after the report renders) reorders the rows in place.
    html += '<tr>';
    html += '<th data-sort-key="name" class="sortable">Agency <span class="sort-arrow"></span></th>';
    html += '<th data-sort-key="distance" class="sortable">Distance <span class="sort-arrow"></span></th>';
    html += '<th data-sort-key="cameras" class="sortable num">Cameras' + (anyPopulation ? '<br><span class="muted-head">(per 1k)</span>' : '') + ' <span class="sort-arrow"></span></th>';
    html += '<th data-sort-key="vehicles" class="sortable num">Vehicles/30d' + (anyPopulation ? '<br><span class="muted-head">(per 1k)</span>' : '') + ' <span class="sort-arrow"></span></th>';
    html += '<th data-sort-key="searches" class="sortable num">Searches/30d' + (anyPopulation ? '<br><span class="muted-head">(per 1k)</span>' : '') + ' <span class="sort-arrow"></span></th>';
    html += '<th data-sort-key="outbound" class="sortable num">Shares to <span class="sort-arrow"></span></th>';
    html += '<th data-sort-key="flagged" class="sortable num">Flagged recipients <span class="sort-arrow"></span></th>';
    html += '</tr>';

    // Helper: value with parenthetical per-1k rate. Em-dash for null
    // value; skip the paren if we don't have a rate for this agency.
    function valueWithRate(value, rate, decimals) {
      if (value == null) return '<span class="null">&mdash;</span>';
      const main = fmtNum(value, decimals || 0);
      if (!anyPopulation || rate == null) return main;
      const rateFmt = fmtNum(rate, rate < 10 ? 2 : 0);
      return `${main} <span class="paren-median">(${rateFmt})</span>`;
    }

    // Each sortable cell carries up to two sort keys:
    //   data-sort-value: the primary number (e.g. cameras = 20)
    //   data-sort-rate: the parenthetical per-1k rate if present
    // Columns with a per-1k rate cycle primary-asc → primary-desc →
    // rate-asc → rate-desc; columns without one toggle only asc/desc.
    // Render up to REGIONAL_ROW_LIMIT rows by default. Sorting (via
    // click-to-sort) then operates within that visible set.
    regional.slice(0, REGIONAL_ROW_LIMIT).forEach(function(r) {
      html += `<tr>`;
      html += `<td data-sort-value="${escapeHtml(r.name.toLowerCase())}"><a href="?agency=${escapeHtml(r.slug)}">${escapeHtml(r.name)}</a></td>`;
      html += `<td class="num" data-sort-value="${r.distance_km}">${fmtNum(kmToMi(r.distance_km), 1)} mi</td>`;
      html += `<td class="num" data-sort-value="${r.cameras == null ? '' : r.cameras}" data-sort-rate="${r.cameras_per_1000 == null ? '' : r.cameras_per_1000}">${valueWithRate(r.cameras, r.cameras_per_1000, 0)}</td>`;
      html += `<td class="num" data-sort-value="${r.vehicles_30d == null ? '' : r.vehicles_30d}" data-sort-rate="${r.vehicles_per_1000 == null ? '' : r.vehicles_per_1000}">${valueWithRate(r.vehicles_30d, r.vehicles_per_1000, 0)}</td>`;
      html += `<td class="num" data-sort-value="${r.searches_30d == null ? '' : r.searches_30d}" data-sort-rate="${r.searches_per_1000 == null ? '' : r.searches_per_1000}">${valueWithRate(r.searches_30d, r.searches_per_1000, 0)}</td>`;
      if (r.outbound > 0) {
        html += `<td class="num" data-sort-value="${r.outbound}">${fmtInt(r.outbound)}</td>`;
        html += `<td class="num" data-sort-value="${r.flagged || 0}">${r.flagged || 0}</td>`;
      } else {
        // N/A entries sort to the end; use an empty sort-value so the
        // sorter handles them consistently with other blanks.
        html += `<td class="num" data-sort-value=""><span class="null">N/A</span></td>`;
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
          // New column: default direction — numeric desc (interesting =
          // big numbers), text/distance asc.
          activeKey = key;
          activeMode = "primary";
          activeDir = (key === "name" || key === "distance") ? 1 : -1;
        } else {
          // Same column: advance through the cycle. For rate-bearing
          // columns: primary-asc → primary-desc → rate-asc → rate-desc
          // → primary-asc. For plain columns: asc → desc → asc.
          if (activeMode === "primary" && activeDir === (key === "name" || key === "distance" ? 1 : -1)) {
            activeDir = -activeDir;  // primary desc (or asc after flip)
          } else if (activeMode === "primary" && hasRate) {
            activeMode = "rate";
            activeDir = -1;  // rate desc first
          } else if (activeMode === "rate" && activeDir === -1) {
            activeDir = 1;   // rate asc
          } else {
            // Back to primary default
            activeMode = "primary";
            activeDir = (key === "name" || key === "distance") ? 1 : -1;
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

    let html = `<h2>Questions for Your Agency</h2>`;
    html += `<p class="muted">Based on the data above, council members may wish to ask:</p>`;
    html += '<div class="questions"><ul>';
    questions.forEach(function(q) {
      html += `<li>${q}</li>`;
    });
    html += '</ul></div>';
    html += `<p class="legal-note"><em>This is informational context derived from public records, not legal advice.</em></p>`;
    return html;
  }

  function buildQuestions(report) {
    const qs = [];
    const flagged = report.flagged_recipients || [];

    if (!report.crawled) {
      qs.push("Does the department operate an ALPR program? How many cameras, and where?");
      qs.push("If so, why doesn't the department publish a public Flock transparency page as its peers do?");
      qs.push("What is the department's data retention policy for ALPR data?");
      qs.push("Does the department conduct audits of ALPR access? Are those records public?");
      qs.push("Will the department commit to publishing a transparency page matching the &sect;1798.90.51(a) requirements?");
      return qs;
    }

    if (flagged.length) {
      // Expand the question with category-specific addenda when the
      // agency hits certain high-signal categories (AG-lawsuit
      // targets, fusion centers). Keeps the base question identical
      // for agencies without those specifics; adds pointed follow-up
      // text when they apply.
      let q = `This report shows <strong>${flagged.length} flagged recipient${flagged.length === 1 ? "" : "s"}</strong> in the sharing list. Has the department conducted an entity-type review of all recipients? Who performs it, and how often?`;
      const sb34 = report.checklist_sb34 || [];
      const failsAgLawsuit = sb34.some(function(i) { return i.id === "no_ag_lawsuit_sharing" && i.value === false; });
      const failsFusion = sb34.some(function(i) { return i.id === "no_fusion_center_sharing" && i.value === false; });
      const addenda = [];
      if (failsAgLawsuit) {
        addenda.push("the list includes <strong>an agency the CA Attorney General has sued</strong> for illegal out-of-state ALPR sharing in violation of SB 34");
      }
      if (failsFusion) {
        addenda.push("the list includes <strong>a fusion center</strong> whose governance includes federal law enforcement agencies (see the Flagged Recipients section for specifics)");
      }
      if (addenda.length) {
        q += " In particular, " + addenda.join("; and ") + ".";
      }
      qs.push(q);
    }

    // Sharing size
    const out = report.stats && report.stats.outbound_count;
    const pctile = report.percentiles && report.percentiles.outbound;
    if (out && pctile != null && pctile >= 75) {
      qs.push(`The department shares with <strong>${out}</strong> agencies (${pctile}${nthSuffix(pctile)} percentile). What criteria determine who is added to this list?`);
    }

    // SB 34 failures
    const sb34 = report.checklist_sb34 || [];
    const sb34Fails = sb34.filter(function(i) { return i.value === false; });
    sb34Fails.forEach(function(item) {
      if (item.id === "documented_audit") {
        qs.push("How often are ALPR access audits conducted, and what do they review (sharing configuration vs. search activity)?");
      } else if (item.id === "downloadable_audit") {
        const pctStr = pct(item.peer_count, item.peer_total);
        qs.push(`Only <strong>${pctStr}%</strong> of CA ${agencyTypeLabel(item.peer_type)} agencies publish downloadable audit records. Will the department commit to publishing one?`);
      } else if (item.id === "published_policy") {
        qs.push("Is the department's ALPR usage and privacy policy conspicuously posted as required by &sect;1798.90.51(a)?");
      }
    });

    // Camera density
    const cameraPctile = report.percentiles && report.percentiles.cameras;
    if (cameraPctile != null && cameraPctile >= 85) {
      qs.push(`The department reports <strong>${report.stats.cameras} cameras</strong> (${cameraPctile}${nthSuffix(cameraPctile)} percentile). How does the department justify this density?`);
    }

    // Downstream-access question: shares-to partners can typically
    // query this agency's data. Pull in the raw count so the question
    // has a concrete number — "shares to 279 other agencies" lands
    // harder than a generic prompt.
    const outCount = report.stats && report.stats.outbound_count;
    if (outCount && outCount > 0) {
      qs.push(`This agency shares ALPR data with <strong>${outCount}</strong> other agencies. What is the process for one of those partnered agencies to perform a search on this data? Does the department need to approve each request, or is access automatic once a sharing relationship is established? Is every such search logged, and is that log accessible to the department?`);
    }
    qs.push(
      "Do the city's posted ALPR policies actually meet the SB 34 requirements outlined above? " +
      "The transparency checks above only verify that a policy URL is posted &mdash; " +
      "they don't read the policy itself. Posted policies commonly fall short in concrete ways, for example: " +
      "(a) end-user audit procedures that apply only to a legacy system and were never extended to cover Flock; " +
      "(b) no defined process for revoking user accounts when personnel leave; " +
      "(c) audits that review sharing configuration but not search activity or case-number compliance; " +
      "(d) retained policy text that describes platforms the department no longer uses. " +
      "A council member should ask to see the <em>text</em> of the policy and compare it item-by-item against &sect;1798.90.51\u2013.55."
    );
    qs.push("How many users currently have access to the city's Flock pages? Are there any inactive users who still have access? What is the process for revoking accounts when personnel leave the department?");
    qs.push("Does the Flock contract contain an independent disclosure clause (such as &sect;5.3 in the standard MSA)?");

    return qs;
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
      html += `<p><strong>Population data:</strong> ${escapeHtml(popSrc.source || "U.S. Census Bureau")}.</p>`;
    }
    html += `<p><strong>Interactive map:</strong> <a href="${escapeHtml(mapUrlAbs)}">${escapeHtml(mapUrlAbs)}</a></p>`;
    html += `<p><strong>This report:</strong> <a href="${escapeHtml(thisUrlAbs)}">${escapeHtml(thisUrlAbs)}</a></p>`;
    html += `<p><strong>Report generated:</strong> ${new Date().toLocaleDateString("en-US", {year: "numeric", month: "long", day: "numeric"})}</p>`;
    html += `<p>This report reflects data at the time of the last portal crawl. Sharing relationships and stats may have changed.</p>`;
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
      .then(function(data) { render(data, slug); })
      .catch(function(err) {
        document.getElementById("report").innerHTML = `
          <div class="error-box">
            <h1 style="margin-top:0">Failed to load report data</h1>
            <p>${escapeHtml(err.message || String(err))}</p>
          </div>`;
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
