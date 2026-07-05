#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 zero-below
/* Parity check: the browser-side aggregation in
   docs/js/justifications_agg.js must reproduce, at the full date window,
   exactly what scripts/build_justifications.py wrote into
   docs/data/justifications.json. This protects the date-window slider
   (which re-aggregates client-side) from silently diverging from the
   canonical build.

   For every own-audit agency, it reads the raw rows from
   docs/data/audit/<slug>.json, runs JustAgg.aggregate over ALL rows,
   and deep-compares the result against the published per-agency entry.

   The check FAILS CLOSED on schema drift: rather than listing the
   window-dependent fields (a list that would silently go stale when
   build_justifications.py grows a field), it keeps one allowlist of
   window-INDEPENDENT keys and requires every other key of each build
   entry to be reproduced by aggregate() — and every aggregate() key to
   exist in the build entry. A new per-agency field therefore breaks
   this check until it is either ported to justifications_agg.js or
   consciously added to WINDOW_INDEPENDENT below (in which case the
   page's windowed view keeps showing its full-window value).

   Integer/string fields must match exactly; the two percent fields are
   allowed a 0.1 tolerance (Python round() is half-to-even, JS
   Math.round is half-up). JustAgg.fullPhraseSeries — the series the
   expand panel actually renders once raw rows load, which is not part
   of aggregate()'s output — is additionally cross-checked against the
   parity-gated phrase_details series for every published phrase.

   Usage:  node scripts/verify_justifications_agg.js
   Exit 0 on full parity, 1 on any mismatch. */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const JUST = path.join(ROOT, 'docs', 'data', 'justifications.json');
const AUDIT_DIR = path.join(ROOT, 'docs', 'data', 'audit');
const Agg = require(path.join(ROOT, 'docs', 'js', 'justifications_agg.js'));

// Keys of a build entry that do NOT depend on the selected date window:
// identity/registry fields, portal policy text, the cross-agency
// external aggregate (partner rows live in other agencies' audits), the
// audit CSV schema, and the build's UTC-sliced date bounds. Everything
// else must be reproduced by JustAgg.aggregate().
const WINDOW_INDEPENDENT = new Set([
  'has_own_audit',
  'slug',
  'display_name',
  'acceptable_use_policy',
  'prohibited_uses',
  'external_aggregated',
  'audit_schema',
  'has_justification_column',
  'search_date_min',
  'search_date_max',
]);

// Python round() is half-to-even, JS Math.round half-up — allow 0.1.
const PCT_FIELDS = new Set(['top1_share_pct', 'top3_share_pct']);

// Canonical JSON with object keys sorted recursively, so we compare
// values rather than key insertion order (the Python build emits
// phrase_details keys in set-hash order; the JS port uses insertion
// order — same data, different order).
function canon(v) {
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
  if (v && typeof v === 'object') {
    return '{' + Object.keys(v).sort().map(function (k) {
      return JSON.stringify(k) + ':' + canon(v[k]);
    }).join(',') + '}';
  }
  return JSON.stringify(v);
}

// The series fields of a phrase_details entry / fullPhraseSeries
// result, for the cross-check between the two.
function seriesView(d) {
  if (!d || !d.from) return null;
  return {
    from: d.from, to: d.to, span_days: d.span_days,
    daily: d.daily, daily_unit: d.daily_unit,
  };
}

function main() {
  if (!fs.existsSync(JUST)) {
    console.error('missing ' + JUST + ' — run build_justifications.py first');
    process.exit(2);
  }
  const data = JSON.parse(fs.readFileSync(JUST, 'utf8'));
  const agencies = data.agencies || {};
  let checked = 0;
  let skipped = 0;
  const problems = [];

  for (const slug of Object.keys(agencies)) {
    const a = agencies[slug];
    if (a.has_own_audit === false) { skipped++; continue; }
    const auditPath = path.join(AUDIT_DIR, slug + '.json');
    if (!fs.existsSync(auditPath)) { skipped++; continue; }
    const audit = JSON.parse(fs.readFileSync(auditPath, 'utf8'));
    const rows = audit.rows || [];
    const pre = Agg.precompute(rows);
    const got = Agg.aggregate(pre);

    for (const f of Object.keys(a)) {
      if (WINDOW_INDEPENDENT.has(f)) continue;
      if (!(f in got)) {
        problems.push({
          slug,
          field: f + ' (in build output but not reproduced by aggregate() — ' +
            'port it to justifications_agg.js or add it to WINDOW_INDEPENDENT)',
        });
        continue;
      }
      if (PCT_FIELDS.has(f)) {
        if (Math.abs((got[f] || 0) - (a[f] || 0)) > 0.1) {
          problems.push({ slug, field: f + ' (got ' + got[f] + ' want ' + a[f] + ')' });
        }
      } else if (canon(got[f]) !== canon(a[f])) {
        problems.push({ slug, field: f });
      }
    }
    for (const f of Object.keys(got)) {
      if (!(f in a) && !WINDOW_INDEPENDENT.has(f)) {
        problems.push({
          slug,
          field: f + ' (returned by aggregate() but absent from the build output)',
        });
      }
    }

    // fullPhraseSeries must agree with the parity-checked phrase_details
    // series at the full window for every published phrase.
    const details = a.phrase_details || {};
    for (const phrase of Object.keys(details)) {
      const want = seriesView(details[phrase]);
      const fs2 = Agg.fullPhraseSeries(phrase, pre);
      if (canon(seriesView(fs2)) !== canon(want)) {
        problems.push({ slug, field: 'fullPhraseSeries(' + phrase + ')' });
      }
    }
    checked++;
  }

  if (problems.length) {
    console.error('PARITY FAILURES (' + problems.length + '):');
    const byField = {};
    for (const p of problems) {
      byField[p.field] = byField[p.field] || [];
      byField[p.field].push(p.slug);
    }
    for (const field of Object.keys(byField)) {
      const slugs = byField[field];
      console.error('  ' + field + ': ' + slugs.length + ' agencies — ' +
        slugs.slice(0, 5).join(', ') + (slugs.length > 5 ? ', …' : ''));
    }
    console.error('checked ' + checked + ' own-audit agencies, skipped ' + skipped);
    process.exit(1);
  }

  console.log('parity OK: ' + checked + ' own-audit agencies match the build (skipped ' + skipped + ')');
  process.exit(0);
}

main();
