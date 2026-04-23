#!/usr/bin/env python3
"""
Build a deduplicated per-portal audit log from scraped Flock transparency data.

Flock transparency portals that publish a Public Search Audit embed the CSV as
a data-URI in their HTML; the scraper extracts it into the `search_audit_csv`
field of each scrape JSON. Flock's CSV is a rolling ~30-day window, so rows
drop off over time. This tool unions rows by `id` across every scrape we have,
preserving the earliest-seen scrape date, and writes a stable per-portal JSON
log that can extend beyond Flock's rolling cutoff.

Output: docs/data/audit/<portal-slug>.json (one file per portal that has
ever published an audit CSV). Rebuilt from scratch on every run; the
committed scrape archive is the source of truth. The output dir is
gitignored and published via GitHub Pages in scripts/publish_docs.sh.

Rows sorted by (searchDate, id) for deterministic output. Columns preserved
as-present: id, userId, searchDate, networkCount, reason, caseNumber,
offenseType.

Usage:
  uv run python scripts/build_audit_log.py
  uv run python scripts/build_audit_log.py --portal east-palo-alto-ca-pd
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import portal_jsons

SCRAPE_DIR = Path("assets/transparency.flocksafety.com")
OUT_DIR = Path("docs/data/audit")

# Preferred column order. Rows only include columns that have data.
COLUMN_ORDER = [
    "id",
    "userId",
    "searchDate",
    "networkCount",
    "reason",
    "caseNumber",
    "offenseType",
    "first_seen",
]


def normalize_row(raw, first_seen):
    """Return a row dict with ordered keys, omitting empty optional fields."""
    out = {}
    for col in COLUMN_ORDER:
        if col == "first_seen":
            out[col] = first_seen
            continue
        val = raw.get(col)
        if val is None:
            continue
        if col in ("reason", "caseNumber", "offenseType") and not str(val).strip():
            continue
        out[col] = val
    return out


def scrape_files_for_portal(portal):
    portal_dir = SCRAPE_DIR / portal
    if not portal_dir.is_dir():
        return []
    return portal_jsons(portal_dir)


def load_portal_rows(portal):
    """Union audit rows across all scrapes for a portal.

    Returns (rows, meta) where rows is sorted and meta summarizes the union.
    Returns (None, None) if the portal never published an audit CSV.
    """
    by_id = {}
    first_seen_by_id = {}
    scrape_dates = []
    schema_seen = set()

    for path in scrape_files_for_portal(portal):
        scrape_date = path.stem
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        rows = data.get("search_audit_csv")
        if not rows:
            continue
        scrape_dates.append(scrape_date)
        for r in rows:
            rid = r.get("id", "")
            if not rid:
                continue
            schema_seen.update(r.keys())
            if rid not in first_seen_by_id:
                first_seen_by_id[rid] = scrape_date
                by_id[rid] = r

    if not by_id:
        return None, None

    normalized = [normalize_row(by_id[rid], first_seen_by_id[rid]) for rid in by_id]
    normalized.sort(key=lambda r: (r.get("searchDate", ""), r.get("id", "")))

    search_dates = [r.get("searchDate", "")[:10] for r in normalized if r.get("searchDate")]
    meta = {
        "portal": portal,
        "schema_seen": sorted(schema_seen),
        "scrape_count": len(scrape_dates),
        "first_scrape": min(scrape_dates) if scrape_dates else None,
        "last_scrape": max(scrape_dates) if scrape_dates else None,
        "search_date_min": min(search_dates) if search_dates else None,
        "search_date_max": max(search_dates) if search_dates else None,
        "row_count": len(normalized),
    }
    return normalized, meta


def write_log(portal, rows, meta):
    out_path = OUT_DIR / f"{portal}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(meta)
    payload["rows"] = rows
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
        f.write("\n")
    return out_path


def discover_portals():
    """Return portal slugs whose most recent scrape has any audit-csv history."""
    portals = set()
    for portal_dir in sorted(SCRAPE_DIR.iterdir()):
        if not portal_dir.is_dir():
            continue
        for path in portal_jsons(portal_dir):
            try:
                with open(path) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("search_audit_csv"):
                portals.add(portal_dir.name)
                break
    return sorted(portals)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--portal",
        action="append",
        help="Only process this portal slug. Repeatable.",
    )
    args = ap.parse_args()

    portals = args.portal if args.portal else discover_portals()
    if not portals:
        print("no portals with audit CSV data found", file=sys.stderr)
        return 1

    written = 0
    skipped = 0
    for portal in portals:
        rows, meta = load_portal_rows(portal)
        if rows is None:
            skipped += 1
            print(f"  {portal}: no audit data, skipping")
            continue
        path = write_log(portal, rows, meta)
        print(
            f"  {portal}: {meta['row_count']} rows, "
            f"{meta['search_date_min']}..{meta['search_date_max']} "
            f"({meta['scrape_count']} scrape{'s' if meta['scrape_count'] != 1 else ''}) "
            f"-> {path}"
        )
        written += 1

    print(f"\nwrote {written} portal log{'s' if written != 1 else ''}, skipped {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
