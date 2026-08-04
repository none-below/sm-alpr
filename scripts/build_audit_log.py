#!/usr/bin/env python3
"""
Build a deduplicated per-portal audit log from scraped Flock transparency data.

Flock transparency portals that publish a Public Search Audit embed the CSV as
a data-URI in their HTML; the scraper extracts it into the `search_audit_csv`
field of each scrape JSON. Flock's CSV is a rolling ~30-day window, so rows
drop off over time. This tool unions rows by `id` across every scrape we have,
preserving the earliest-seen scrape date, and writes a stable per-portal JSON
log that can extend beyond Flock's rolling cutoff.

Rows for the same `id` are merged column-by-column across sources, not kept
whole from whichever source was seen first. This matters where a portal also
has a PRA-imported export (e.g. SMPD): the live feed publishes `offenseType`
while the PRA export publishes `reason` (the offense category plus the
officer's free-text justification), and the merge keeps both on one row.

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
    # Real portal scrapes (strict YYYY-MM-DD.json) + PRA-imported audit data
    # (pra-<request-id>.json, written by scripts/import_pra_audit.py).
    # `portal_jsons` is intentionally strict so other builders that read full
    # scrape JSON aren't fooled by minimal PRA imports — those imports only
    # carry `search_audit_csv` and would crash builders expecting the rest.
    files = list(portal_jsons(portal_dir))
    files.extend(sorted(portal_dir.glob("pra-*.json")))
    return files


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
            if rid not in by_id:
                by_id[rid] = {}
                first_seen_by_id[rid] = scrape_date
            cur = by_id[rid]
            # Per-column merge across sources for the same search id. The live
            # portal feed and SMPD's PRA-imported export carry *different*
            # columns for the same id (portal -> offenseType; PRA export ->
            # reason, where reason = "<offenseType> - <free-text justification>").
            # Union the columns so a merged row keeps both, instead of letting
            # whichever source we saw first win and silently drop the other's
            # column. First non-empty value wins per key; `reason` prefers the
            # longer string, since the export appends the officer's free-text
            # justification to the offense category.
            for k, v in r.items():
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                if k not in cur:
                    cur[k] = v
                elif k == "reason" and len(str(v)) > len(str(cur[k])):
                    cur[k] = v

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
    """Portal slugs with audit-csv data — from live scrapes or PRA imports.

    Live portals embed the audit CSV in daily YYYY-MM-DD.json scrapes. Agencies
    whose audit arrived only via PRA (their portal publishes no public audit CSV)
    carry it in pra-*.json instead; include those too so PRA-only audit agencies
    build. `portal_jsons` stays strict (daily scrapes only) so other builders
    aren't fooled by minimal PRA imports; here we additionally check pra-*.json.
    """
    portals = set()
    for portal_dir in sorted(SCRAPE_DIR.iterdir()):
        if not portal_dir.is_dir():
            continue
        candidates = list(portal_jsons(portal_dir)) + sorted(portal_dir.glob("pra-*.json"))
        for path in candidates:
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
