#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Convert Flock "Network Audit" .xlsx exports into a PRA-import audit source
(`pra-<id>.json`) under assets/transparency.flocksafety.com/<slug>/, matching
the shape scripts/import_pra_audit.py writes. build_audit_log.py then unions it
into docs/data/audit/<slug>.json, which feeds the justifications page — so an
agency whose audit arrived as spreadsheets (not the usual scraped-portal CSV or
PRA PDF) flows through the same pipeline and deploys with the site.

The committed source carries only what the audit pipeline needs — id, searchDate,
networkCount, reason. The searcher is redacted to "***" by default (matching the
SMPD import; no downstream consumer uses it, and it avoids publishing individual
officers' names in a derived build artifact); pass --keep-user to retain it.

A network audit lists every search that touched the target agency's ALPR
network — run by the agency AND its sharing partners. `--org` filters to a
single searching agency so the output represents that agency's OWN searches
(the per-agency semantic the justifications page assumes). `id` is synthesized
(sha1 of the stable row content) since the reason-bearing releases carry no
export UUID; rows dedupe by that id and sort by (searchDate, id).

The raw .xlsx are NOT needed at build time — only this committed json is. Re-run
this when a new PRA release arrives; CI reads the committed json.

Usage:
    uv run python scripts/xlsx_to_audit_rows.py \
        --slug redwood-city-ca-pd --pra-id 26-217 --org "Redwood City CA PD" \
        assets/redwood-city-pras/"PRA 26-217 1st Release.xlsx" ...
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

PORTAL_ROOT = Path("assets/transparency.flocksafety.com")
BLANK = {"", "***"}
MAIN_HINTS = {"Search Time", "Org Name", "Search Type"}


def parse_iso(v):
    if not isinstance(v, str):
        return None
    s = v.strip()
    for fmt in ("%m/%d/%Y, %I:%M:%S %p UTC", "%m/%d/%Y, %I:%M:%S %p", "%m/%d/%Y %I:%M:%S %p UTC"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


def clean(v):
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s in BLANK else s


def find_header(ws):
    for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
        hdr = [str(c).strip() if c is not None else "" for c in row]
        if MAIN_HINTS & set(hdr):
            return {h: i for i, h in enumerate(hdr)}
    return None


def convert(files, org_filter, reason_required, keep_user):
    by_id = {}
    contributing = 0
    org_lower = org_filter.lower() if org_filter else None
    for fp in files:
        wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        idx = find_header(ws)
        if not idx or "Search Time" not in idx:
            print(f"  skip {fp.name}: no recognizable header", file=sys.stderr)
            wb.close()
            continue
        oi = idx.get("Org Name"); ni = idx.get("Name"); ti = idx["Search Time"]
        nci = idx.get("Total Networks Searched"); ri = idx.get("Reason"); ci = idx.get("Case #")
        added = 0
        rows = ws.iter_rows(values_only=True)
        next(rows, None)
        for r in rows:
            if org_lower is not None:
                org = clean(r[oi]) if oi is not None and oi < len(r) else ""
                if org_lower not in org.lower():
                    continue
            reason = clean(r[ri]) if ri is not None and ri < len(r) else ""
            if reason_required and not reason:
                continue
            search_date = parse_iso(r[ti]) if ti < len(r) else None
            if not search_date:
                continue
            user = clean(r[ni]) if ni is not None and ni < len(r) else ""
            nc = clean(r[nci]) if nci is not None and nci < len(r) else ""
            case = clean(r[ci]) if ci is not None and ci < len(r) else ""
            rid = hashlib.sha1("|".join([user, search_date, nc, reason, case]).encode("utf-8")).hexdigest()[:16]
            if rid in by_id:
                continue
            row = {
                "id": rid,
                "userId": user if (keep_user and user) else "***",
                "searchDate": search_date,
                "networkCount": nc or "0",
            }
            if reason:
                row["reason"] = reason
            if case:
                row["caseNumber"] = case
            by_id[rid] = row
            added += 1
        if added:
            contributing += 1
        print(f"  {fp.name}: +{added} rows", file=sys.stderr)
        wb.close()
    return by_id, contributing


def main():
    ap = argparse.ArgumentParser(description="Convert Flock network-audit xlsx to a PRA-import audit source")
    ap.add_argument("files", nargs="+", help="xlsx files to read")
    ap.add_argument("--slug", required=True, help="agency slug under assets/transparency.flocksafety.com/")
    ap.add_argument("--pra-id", required=True, help="PRA request id (output file is pra-<id>.json)")
    ap.add_argument("--org", default=None, help="filter to rows whose Org Name contains this (the searching agency)")
    ap.add_argument("--reason-required", action="store_true",
                    help="keep only rows with a non-blank reason (recommended for justifications)")
    ap.add_argument("--keep-user", action="store_true",
                    help="keep the searcher name instead of redacting it to '***'")
    ap.add_argument("--out", default=None, help="output path (default the portal folder's pra-<id>.json)")
    args = ap.parse_args()

    files = [Path(f) for f in args.files]
    missing = [f for f in files if not f.exists()]
    if missing:
        print(f"missing files: {missing}", file=sys.stderr)
        return 1

    portal_dir = PORTAL_ROOT / args.slug
    if args.out is None and not portal_dir.is_dir():
        print(f"portal folder does not exist: {portal_dir}", file=sys.stderr)
        return 1

    by_id, contributing = convert(files, args.org, args.reason_required, args.keep_user)
    if not by_id:
        print("no rows produced", file=sys.stderr)
        return 1

    rows = sorted(by_id.values(), key=lambda r: (r["searchDate"], r["id"]))
    dates = [r["searchDate"][:10] for r in rows]
    payload = {
        "source": f"Flock network-audit xlsx, PRA #{args.pra_id}, converted by scripts/xlsx_to_audit_rows.py",
        "integrity": {
            "org_filter": args.org,
            "source_files": contributing,
            "row_count": len(rows),
            "search_date_min": min(dates),
            "search_date_max": max(dates),
        },
        "search_audit_csv": rows,
    }
    out = Path(args.out) if args.out else portal_dir / f"pra-{args.pra_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {out} ({len(rows)} rows, {min(dates)}..{max(dates)})", file=sys.stderr)
    print("rebuild:  uv run python scripts/build_audit_log.py --portal " + args.slug, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
