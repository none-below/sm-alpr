#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Derive an agency's *former* sharing partners from its Flock network-audit logs.

A network audit lists every search that touched the agency's ALPR network, and
who ran it. Agencies that appear for a while and then stop are partners whose
access ended — a sharing relationship that existed and was withdrawn.

Why this exists separately from the portal changelog: `scripts/build_history.py`
infers removals by diffing consecutive portal scrapes, so it can only see
changes that happened *after* we started scraping, and only reports them for
`RECENT_WINDOW_DAYS`. Los Altos' access cut-offs happened in March 2025 and
December 2025, long before our first scrape of its portal, and are invisible to
that mechanism. The audit logs record them directly and with a real date, so
these entries carry an exact `removed_on` rather than the changelog's
"on/around" scrape-boundary estimate.

`removed_on` is the first day of the next month the corpus actually *covers*
after the partner's last observed search — the earliest month in which the
partner provably had no access. It follows covered months rather than the
calendar because productions have holes: Redwood City's releases skip
2026-03-10..2026-05-09, and treating the calendar-next month as proof of absence
would invent departures for every partner whose last search fell before a gap.
When the next covered month is not the calendar-next one, the entry carries
`coverage_gap_before_removal: true` so a consumer can present the date as
bounded rather than exact. `last_search_month` is kept alongside so a consumer
can show the underlying observation instead of the derived date.

A partner still searching in either of the last two covered months is treated as
active, not departed — the final month is usually partial (Los Altos' 2026
workbook ends July 6), so a partner absent only from that stub has not
necessarily left.

New audit corpora are picked up automatically: each `assets/<agency>-pras/json/`
declares itself in a `_corpus.json` (`portal` plus the globs naming its
*network* audits), and `--all` regenerates every declared corpus. Dropping in a
new production means adding its NDJSON and, for a new agency, one small
`_corpus.json` — no edit to this script or to the build.

Usage:
  uv run python scripts/audit_departures.py --all
  uv run python scripts/audit_departures.py --slug los-altos-ca-pd <files...>
"""
import argparse
import gzip
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

REGISTRY = Path("assets/agency_registry.json")
OUT_ROOT = Path("assets/audit_departures")
# Month/day are not reliably zero-padded: Los Altos exports "01/10/2025, ...",
# Redwood City's "1/10/2025, ...". Requiring two digits silently drops every
# single-digit month, which reads downstream as a coverage gap and invents
# departures for everyone whose last search fell in one.
MONTH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4}),")


def search_ymd(v):
    """'02/28/2025, 04:37:29 PM UTC' or '2/28/2025, ...' -> ('2025-02', 28)."""
    m = MONTH_RE.match(str(v).strip()) if v is not None else None
    return (f"{m.group(3)}-{int(m.group(1)):02d}", int(m.group(2))) if m else None


# A month only proves a partner's absence if the production actually covered the
# whole month. Productions routinely stop mid-month (Redwood City's 26-217 ends
# March 9 2026; Los Altos' 2026 workbook ends July 6), and treating a nine-day
# stub as a full month marks every partner who simply did not search that week
# as departed.
FULL_MONTH_SPAN_DAYS = 20


def solid_months(span):
    """Covered months whose observed day range is wide enough to prove absence."""
    return sorted(m for m, (lo, hi) in span.items()
                  if hi - lo + 1 >= FULL_MONTH_SPAN_DAYS)


def next_month(ym):
    y, m = (int(x) for x in ym.split("-"))
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


def load_corpora():
    """Every `assets/<agency>-pras/json/_corpus.json` that declares a portal."""
    out = []
    for decl in sorted(Path("assets").glob("*-pras/json/_corpus.json")):
        spec = json.loads(decl.read_text())
        portal = spec.get("portal")
        globs = spec.get("network_audit_globs") or []
        if not portal or not globs:
            print(f"  skip {decl}: needs 'portal' and 'network_audit_globs'", file=sys.stderr)
            continue
        root = decl.parent
        files = sorted({p for g in globs for p in root.glob(g)})
        if not files:
            print(f"  skip {portal}: no files matched {globs}", file=sys.stderr)
            continue
        out.append((portal, files))
    return out


def load_registry():
    """Map every Flock display name to its registry identity."""
    by_name = {}
    for a in json.loads(REGISTRY.read_text()):
        for n in a.get("flock_names") or []:
            by_name[n.strip()] = a
    return by_name


def scan(files):
    """Per searching org: first/last month seen and searches, plus per-month
    day span so partially-covered months can be told from complete ones."""
    first, last, count = {}, {}, defaultdict(int)
    span = {}   # month -> [min_day, max_day]
    for fp in files:
        with gzip.open(fp, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                org = (rec.get("Org Name") or "").strip()
                parsed = search_ymd(rec.get("Search Time"))
                if not parsed:
                    continue
                ym, day = parsed
                lo_hi = span.get(ym)
                if lo_hi is None:
                    span[ym] = [day, day]
                else:
                    if day < lo_hi[0]:
                        lo_hi[0] = day
                    if day > lo_hi[1]:
                        lo_hi[1] = day
                if not org:
                    continue
                count[org] += 1
                if org not in first or ym < first[org]:
                    first[org] = ym
                if org not in last or ym > last[org]:
                    last[org] = ym
    return first, last, count, span


def run_one(slug, files, registry, out_path=None):
    first, last, count, span = scan(files)
    if not last:
        print(f"  {slug}: no rows parsed", file=sys.stderr)
        return None

    covered = sorted(span)
    solid = solid_months(span)
    partial = [m for m in covered if m not in set(solid)]
    corpus_end = covered[-1]

    departed, active, unresolved = [], 0, 0
    for org, lm in sorted(last.items()):
        # Departed only if a fully-covered month follows the last search with no
        # activity in it; absence from a partial month proves nothing.
        nxt = next((m for m in solid if m > lm), None)
        if nxt is None:
            active += 1
            continue
        a = registry.get(org)
        if a is None:
            unresolved += 1
        departed.append({
            "name": org,
            "agency_id": (a or {}).get("agency_id"),
            "slug": (a or {}).get("slug"),
            "mappable": bool((a or {}).get("geo")),
            "first_search_month": first[org],
            "last_search_month": lm,
            "removed_on": nxt + "-01",
            "coverage_gap_before_removal": nxt != next_month(lm),
            "searches": count[org],
        })

    departed.sort(key=lambda d: (d["removed_on"], -d["searches"], d["name"]))
    by_month = defaultdict(int)
    for d in departed:
        by_month[d["removed_on"][:7]] += 1

    gaps = [f"{covered[i]}..{covered[i + 1]}" for i in range(len(covered) - 1)
            if covered[i + 1] != next_month(covered[i])]
    payload = {
        "source": "Flock network-audit logs (PRA productions), "
                  "derived by scripts/audit_departures.py",
        "portal": slug,
        "note": "Former searchers of this agency's ALPR network. removed_on is the "
                "first fully-covered month with no observed search, derived from "
                "monthly audit logs, not from a portal-scrape diff.",
        "corpus_first_month": covered[0],
        "corpus_last_month": corpus_end,
        "corpus_coverage_gaps": gaps,
        "partial_months_ignored": partial,
        "counts": {
            "departed": len(departed),
            "departed_mappable": sum(1 for d in departed if d["mappable"]),
            "departed_unresolved": unresolved,
            "still_active": active,
        },
        "departed_by_month": dict(sorted(by_month.items())),
        "departed": departed,
    }
    out = Path(out_path) if out_path else OUT_ROOT / f"{slug}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"  {slug}: {len(departed)} departed "
          f"({payload['counts']['departed_mappable']} mappable, {unresolved} "
          f"not in registry), {active} active, {covered[0]}..{corpus_end}"
          + (f", GAPS {gaps}" if gaps else ""), file=sys.stderr)
    return payload


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="*", help="network-audit *.ndjson.gz")
    ap.add_argument("--all", action="store_true",
                    help="regenerate every corpus declaring assets/*-pras/json/_corpus.json")
    ap.add_argument("--slug", default=None, help="slug of the audited agency")
    ap.add_argument("--out", default=None, help="output path (default assets/audit_departures/<slug>.json)")
    args = ap.parse_args()

    registry = load_registry()
    if args.all:
        corpora = load_corpora()
        if not corpora:
            print("no corpora declared", file=sys.stderr)
            return 1
        for slug, files in corpora:
            run_one(slug, files, registry)
        return 0

    if not args.slug or not args.files:
        ap.error("give --all, or --slug plus files")
    files = [Path(f) for f in args.files]
    missing = [f for f in files if not f.exists()]
    if missing:
        print(f"missing: {missing}", file=sys.stderr)
        return 1
    return 0 if run_one(args.slug, files, registry, args.out) else 1


if __name__ == "__main__":
    sys.exit(main())
