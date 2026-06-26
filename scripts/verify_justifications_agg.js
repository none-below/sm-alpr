#!/usr/bin/env node
/* Parity check: the browser-side aggregation in
   docs/js/justifications_agg.js must reproduce, at the full date window,
   exactly what scripts/build_justifications.py wrote into
   docs/data/justifications.json. This protects the date-window slider
   (which re-aggregates client-side) from silently diverging from the
   canonical build.

   For every own-audit agency, it reads the raw rows from
   docs/data/audit/<slug>.json, runs JustAgg.aggregate over ALL rows,
   and deep-compares each window-dependent field against the published
   per-agency entry. Integer/string fields must match exactly; the two
   percent fields are allowed a 0.1 tolerance (Python round() is
   half-to-even, JS Math.round is half-up).

   Usage:  node scripts/verify_justifications_agg.js
   Exit 0 on full parity, 1 on any mismatch. */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const JUST = path.join(ROOT, 'docs', 'data', 'justifications.json');
const AUDIT_DIR = path.join(ROOT, 'docs', 'data', 'audit');
const Agg = require(path.join(ROOT, 'docs', 'js', 'justifications_agg.js'));

// Window-dependent fields the build emits that aggregate() reproduces.
const EXACT_SCALARS = [
  'row_count', 'blank_reasons', 'unique_reasons',
  'median_length_chars', 'verbatim_shown', 'tokens_shown', 'timed_rows'
];
const PCT_FIELDS = ['top1_share_pct', 'top3_share_pct'];
const DEEP_FIELDS = [
  'verbatim', 'tokens', 'penal_codes', 'hour_dow',
  'long_active_cases', 'phrase_details'
];

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

function diffField(slug, name, got, want, problems) {
  if (canon(got) !== canon(want)) {
    problems.push({ slug, field: name });
  }
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

    for (const f of EXACT_SCALARS) diffField(slug, f, got[f], a[f], problems);
    for (const f of PCT_FIELDS) {
      if (Math.abs((got[f] || 0) - (a[f] || 0)) > 0.1) {
        problems.push({ slug, field: f + ' (got ' + got[f] + ' want ' + a[f] + ')' });
      }
    }
    for (const f of DEEP_FIELDS) diffField(slug, f, got[f], a[f], problems);
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
