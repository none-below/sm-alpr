#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Hydrate ALPR search-reason case-ID candidates against public CitizenRIMS CAD
data, across all 11 San Mateo County agencies with both an audit-log source
and a CitizenRIMS snapshot -- not just SMPD (see scripts/harvest_case_ids.py,
which stays SMPD-only/narrow-scoped since its exact counts are already cited
in outside documents). Deterministic and local-only: no network access, reads
only what's already in the repo (assets/{redwood-city-pras,los-altos-pras,
transparency.flocksafety.com}/ for reasons/case numbers, assets/citizenrims/
for CAD ground truth). Safe to wire into `make build` later -- every input is
already committed.

Sources scanned, per agency (Org Name match against ORG_NAMES below):

  smpd_audit    SMPD's own produced audit rows (2 PRAs, W012541 + W012818)
  rwc_network   Every assets/redwood-city-pras/json/*.ndjson.gz -- RWC's own
                portal export, which incidentally carries every partner
                agency's searches too. Reason is populated in 4 early/2023-
                24 releases + the Jan 2025 slice; Case # (no Reason) is
                separately populated in 6 files spanning Mar-Aug 2025 --
                that Case#-only window was not previously harvested.
  la_network    Every assets/los-altos-pras/json/pra-*/*.ndjson.gz filename
                containing "network_audit" (case-insensitive) -- skips the
                separate "org(anizational)_audit" report type, which is
                LA's outbound-only view. The 25-312 and 26-366 productions
                republish byte-identical files for overlapping months
                (verified by content hash); deduplicated by decompressed
                content hash, not filename, since naming conventions differ
                across the two productions.

Only `cad_event`-kind IDs (YYMMDD+event, self-dating) are joined against
CitizenRIMS: it is the one identifier format confirmed structurally
consistent across every agency (CitizenRIMS incidentNumber is always
YYYYMMDDNNNN). `hyphen_case`/`agency_rms`/`court_case` IDs are harvested and
counted but NOT joined -- caseNumber format varies wildly by agency (sampled:
Menlo Park "19-10", Redwood City "R19-01-0254", San Bruno "SNB19-00240", San
Mateo "190202020", ...), so a general cross-agency caseNumber join isn't
attempted here.

A match's confidence is scored by how close the search time lands to the
CAD incident's creation time (incidentDate+incidentTime, confirmed Pacific
local for SMPD in prior analysis -- assumed same platform-wide here, not
independently reverified per agency): near_simultaneous if within 30
minutes, same_day if same Pacific calendar date, else loose.

Output (deterministic, no timestamps in content) under assets/case-id-hydration/:

  <prefix>/search_rows.ndjson.gz  one line per in-scope search row with any
                                  extracted case IDs and their hydration result
  <prefix>/case_ids.json          deduplicated per-agency case-ID index
  summary.json                    cross-agency rollup for the dashboard

Usage:
  python3 scripts/hydrate_case_ids.py               # all 11 agencies
  python3 scripts/hydrate_case_ids.py --agency menlopark
"""

import argparse
import gzip
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.case_ids import (  # noqa: E402
    UUID_RE, classify_reason, clean, extract_ids, parse_flock_time,
    split_category,
)

PACIFIC = ZoneInfo("America/Los_Angeles")

# prefix (matches assets/citizenrims/<prefix>/) -> Flock "Org Name" string
ORG_NAMES = {
    "sanmateopd": "San Mateo CA PD",
    "southsanfranciscopd": "South San Francisco CA PD",
    "menlopark": "Menlo Park CA PD",
    "redwoodcity": "Redwood City CA PD",
    "belmont": "Belmont CA PD",
    "sanbruno": "San Bruno CA PD",
    # Note the double space -- that's the literal string Flock's export
    # uses for this org, confirmed by scanning all Org Name values in the
    # RWC/LA corpora (a single-space match returns zero rows silently).
    "burlingame": "Burlingame  CA PD",
    "pacifica": "Pacifica CA PD",
    "hillsborough": "Hillsborough CA PD",
    "brisbane": "Brisbane CA PD",
    "atherton": "Atherton CA PD",
}
ORG_TO_PREFIX = {v: k for k, v in ORG_NAMES.items()}

SMPD_AUDIT_FILES = [
    "assets/transparency.flocksafety.com/san-mateo-ca-pd/pra-W012541-041426.json",
    "assets/transparency.flocksafety.com/san-mateo-ca-pd/pra-W012818-053026.json",
]
RWC_DIR = "assets/redwood-city-pras/json"
LA_DIR = "assets/los-altos-pras/json"
CITIZENRIMS_DIR = "assets/citizenrims"

OUT_DIR = "assets/case-id-hydration"


def make_row(source, source_file, org, search_time, reason, **extra):
    reason = "" if reason is None else str(reason).strip()
    if UUID_RE.match(reason.lower()):
        ids, leftovers = [], []
    else:
        ids, leftovers = extract_ids(reason)
    category, detail = split_category(reason)
    row = {
        "source": source,
        "source_file": source_file,
        "org": org,
        "search_time": search_time,
        "reason": reason,
        "category": category,
        "detail": detail,
        "reason_class": classify_reason(reason, ids, leftovers),
        "case_ids": ids,
    }
    if leftovers:
        row["unclassified_numbers"] = leftovers
    row.update({k: v for k, v in extra.items() if v is not None})
    return row


def iter_smpd_audit(root):
    for rel in SMPD_AUDIT_FILES:
        path = root / rel
        data = json.loads(path.read_text())
        for r in data["search_audit_csv"]:
            yield make_row(
                "smpd_audit", path.name, "sanmateopd", r.get("searchDate"),
                r.get("reason"), row_id=r.get("id"),
                networks=clean(r.get("networkCount")),
            )


def iter_network_file(path, source):
    """Yield rows for every row whose Org Name matches a tracked agency."""
    with gzip.open(path, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            org_name = r.get("Org Name")
            prefix = ORG_TO_PREFIX.get(org_name)
            if prefix is None:
                continue
            search_time = (
                parse_flock_time(r.get("Search Time", ""))
                if r.get("Search Time") else None
            )
            reason = r.get("Reason")
            case_field = clean(r.get("Case #"))
            # Extract from Reason primarily; if Reason is absent/redacted but
            # Case # is populated, treat Case # as the text to mine too (the
            # Mar-Aug 2025 RWC window: Reason redacted, Case # is not).
            text = reason if clean(reason) else (case_field or "")
            yield make_row(
                source, path.name, prefix, search_time, text,
                name=clean(r.get("Name")),
                case_field=case_field,
                search_type=clean(r.get("Search Type")),
                time_frame=clean(r.get("Time Frame")),
            )


def dedup_network_files(paths):
    """Drop files whose decompressed bytes duplicate an already-seen file
    (the 25-312/26-366 Los Altos productions republish identical months)."""
    seen = {}
    out = []
    for path in paths:
        digest = hashlib.md5(gzip.open(path, "rb").read()).hexdigest()
        if digest in seen:
            continue
        seen[digest] = path
        out.append(path)
    return out


def find_network_files(root):
    rwc = sorted((root / RWC_DIR).glob("*.ndjson.gz"))
    la_all = sorted((root / LA_DIR).glob("pra-*/*.ndjson.gz"))
    la_network = [p for p in la_all if "network_audit" in p.name.lower()]
    return rwc, dedup_network_files(la_network)


def load_citizenrims_index(root, prefix):
    """Latest snapshot's incidents, keyed by incidentNumber. Also returns
    (min_date, max_date) incident coverage for context."""
    agency_dir = root / CITIZENRIMS_DIR / prefix
    if not agency_dir.exists():
        return {}, (None, None)
    dates = sorted(p.name for p in agency_dir.iterdir() if p.is_dir())
    if not dates:
        return {}, (None, None)
    snapshot = agency_dir / dates[-1]
    index = {}
    min_date = max_date = None
    for f in sorted((snapshot / "incidents").glob("*.json.gz")):
        for r in json.loads(gzip.open(f, "rt").read()):
            num = r.get("incidentNumber")
            if num:
                index[num] = r
            d = r.get("incidentDate", "")[:10]
            if d:
                min_date = d if min_date is None else min(min_date, d)
                max_date = d if max_date is None else max(max_date, d)
    return index, (min_date, max_date)


def incident_utc(rec):
    """incidentDate+incidentTime assumed Pacific local (confirmed for SMPD
    via hourly-histogram trough analysis; not independently reverified per
    agency here -- same CitizenRIMS platform, same assumption)."""
    d = rec.get("incidentDate", "")[:10]
    t = rec.get("incidentTime", "")
    if not d or not t:
        return None
    try:
        naive = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=PACIFIC).astimezone(timezone.utc)


def confidence(search_time_iso, incident_rec):
    if not search_time_iso:
        return None, None
    inc_utc = incident_utc(incident_rec)
    if inc_utc is None:
        return None, None
    search_utc = datetime.strptime(search_time_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    gap_seconds = (search_utc - inc_utc).total_seconds()
    abs_gap = abs(gap_seconds)
    if abs_gap <= 1800:
        band = "near_simultaneous"
    elif abs_gap <= 86400:
        band = "same_day"
    else:
        band = "loose"
    return gap_seconds, band


def hydrate_id(cid_entry, incident_index):
    """cid_entry: {"id":..., "kind":..., "cad_date":...} for kind=cad_event.
    Returns a hydration dict or None if not a cad_event / not matchable."""
    if cid_entry["kind"] != "cad_event":
        return None
    # id is YYMMDD(no century)+event; cad_date is 20YY-MM-DD -> reconstruct
    # the CitizenRIMS incidentNumber form (century-prefixed, 4-digit event).
    yymmdd = cid_entry["id"][:6]
    event = cid_entry["id"][6:]
    incident_number = int(f"20{yymmdd}{event.zfill(4)}")
    rec = incident_index.get(incident_number)
    if rec is None:
        return {"matched": False, "incident_number": incident_number}
    return {
        "matched": True,
        "incident_number": incident_number,
        "incident_date": rec.get("incidentDate", "")[:10],
        "call_type": rec.get("callTypeDescription") or rec.get("type"),
        "disposition": rec.get("dispositionDescription"),
        "status": rec.get("status"),
        "city": rec.get("city"),
    }


def aggregate(rows, incident_index):
    index = {}
    for row in rows:
        candidates = list(row["case_ids"])
        cf = row.get("case_field")
        if cf:
            ids, _ = extract_ids(cf)
            known = {c["id"] for c in candidates}
            candidates.extend(c for c in ids if c["id"] not in known)
        for c in candidates:
            key = c["id"]
            e = index.setdefault(key, {
                "id": key, "kind": c["kind"], "n_searches": 0,
                "by_source": Counter(), "first_search": row["search_time"],
                "last_search": row["search_time"], "categories": set(),
                "searchers": set(), "sample_reasons": [], "hydration": None,
            })
            for attr in ("cad_date", "county", "agency_prefix"):
                if attr in c:
                    e[attr] = c[attr]
            e["n_searches"] += 1
            e["by_source"][row["source"]] += 1
            if row["search_time"]:
                e["first_search"] = min(e["first_search"] or row["search_time"], row["search_time"])
                e["last_search"] = max(e["last_search"] or row["search_time"], row["search_time"])
            if row["category"]:
                e["categories"].add(row["category"])
            if row.get("name"):
                e["searchers"].add(row["name"])
            if row["reason"] and row["reason"] not in e["sample_reasons"]:
                if len(e["sample_reasons"]) < 3:
                    e["sample_reasons"].append(row["reason"])
            if e["hydration"] is None:
                hyd = hydrate_id(c, incident_index)
                if hyd is not None:
                    if hyd.get("matched") and row["search_time"]:
                        gap, band = confidence(row["search_time"],
                                               incident_index[hyd["incident_number"]])
                        hyd["gap_seconds"] = gap
                        hyd["confidence"] = band
                    e["hydration"] = hyd
    out = []
    for e in sorted(index.values(), key=lambda e: (-e["n_searches"], e["id"])):
        e["by_source"] = dict(sorted(e["by_source"].items()))
        e["categories"] = sorted(e["categories"])
        e["searchers"] = sorted(e["searchers"])
        e["multi_category"] = len(e["categories"]) > 1
        out.append(e)
    return out


def process_agency(prefix, all_rows, root, out_dir):
    rows = [r for r in all_rows if r["org"] == prefix]
    rows.sort(key=lambda r: (r["search_time"] or "", r["source"],
                             r.get("row_id", ""), r["reason"]))
    incident_index, coverage = load_citizenrims_index(root, prefix)

    agency_dir = out_dir / prefix
    agency_dir.mkdir(parents=True, exist_ok=True)
    with (agency_dir / "search_rows.ndjson.gz").open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            for row in rows:
                gz.write((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode())

    case_index = aggregate(rows, incident_index)
    cad_entries = [e for e in case_index if e["kind"] == "cad_event"]
    matched = [e for e in cad_entries if e["hydration"] and e["hydration"]["matched"]]
    by_confidence = Counter(e["hydration"]["confidence"] for e in matched)

    stats = {
        "org_name": ORG_NAMES[prefix],
        "citizenrims_incident_coverage": {"min_date": coverage[0], "max_date": coverage[1]},
        "citizenrims_incident_count": len(incident_index),
        "rows_total": len(rows),
        "rows_by_source": dict(sorted(Counter(r["source"] for r in rows).items())),
        "reason_class": dict(sorted(Counter(r["reason_class"] for r in rows).items())),
        "rows_with_case_id": sum(1 for r in rows if r["case_ids"]),
        "unique_case_ids": len(case_index),
        "case_ids_by_kind": dict(sorted(Counter(e["kind"] for e in case_index).items())),
        "cad_event_ids": len(cad_entries),
        "cad_event_matched": len(matched),
        "cad_event_match_rate": round(len(matched) / len(cad_entries), 4) if cad_entries else None,
        "cad_event_match_confidence": dict(sorted(by_confidence.items())),
        "multi_category_case_ids": sum(1 for e in case_index if e["multi_category"]),
    }

    (agency_dir / "case_ids.json").write_text(
        json.dumps({"stats": stats, "case_ids": case_index},
                   indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    )
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--agency", action="append", choices=sorted(ORG_NAMES),
                    help="limit to one agency prefix; repeatable (default: all)")
    args = ap.parse_args()
    root = args.repo_root
    prefixes = args.agency or sorted(ORG_NAMES)

    rows = list(iter_smpd_audit(root))
    rwc_files, la_files = find_network_files(root)
    for path in rwc_files:
        rows.extend(iter_network_file(path, "rwc_network"))
    for path in la_files:
        rows.extend(iter_network_file(path, "la_network"))

    out_dir = root / OUT_DIR
    summary = {}
    for prefix in prefixes:
        summary[prefix] = process_agency(prefix, rows, root, out_dir)
        print(f"{prefix}: {json.dumps(summary[prefix], indent=None)}")

    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    )
    print(f"\nsummary written to {out_dir / 'summary.json'}")


if __name__ == "__main__":
    sys.exit(main())
