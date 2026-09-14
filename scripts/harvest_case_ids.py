#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Harvest SMPD case-ID candidates from the audit-log corpora already in the repo.

Reads three local sources of San Mateo PD search rows — no network access:

  smpd_audit   assets/transparency.flocksafety.com/san-mateo-ca-pd/
               pra-W012541-041426.json + pra-W012818-053026.json
               (SMPD's own produced search-audit rows; userId redacted)
  la_network   assets/los-altos-pras/json/pra-26-366/
               Network Audit sheets — rows where Org Name == "San Mateo CA PD"
               (Reason released essentially in full by Los Altos)
  rwc_network  assets/redwood-city-pras/json/PRA_26_217_2025_1.ndjson.gz
               (the Jan 1-15 2025 release where Reason survives; carries
               searcher names for SMPD rows)

Scope: searches from 2026, plus the January 2025 slice (the one 2025 window
where Redwood City's production carries Reason + searcher name). RWC's 2026-era
releases carry no Reason for partner rows and are skipped.

Extracted identifier kinds:

  cad_event   YYMMDD + 3- or 4-digit event number (e.g. 2602190261 =
              2026-02-19 event 0261). Self-dating; the embedded date is
              validated and emitted as cad_date.
  court_case  County criminal-case style, e.g. 25SM013446A.
  hyphen_case Classic RMS style YY-NNNNN(N).

Everything else digit-bearing is preserved for review (`unclassified_number`)
rather than silently dropped. Reasons that are a bare UUID are flagged
(`uuid` reason class) — they are Flock artifacts, not officer-entered text.

Outputs (deterministic; no timestamps) under assets/case-id-harvest/:

  search_rows.ndjson.gz  one line per in-scope SMPD search row, with source
                         provenance and any extracted case IDs
  case_ids.json          deduplicated case-ID index aggregated across sources,
                         with per-source search counts, category sets,
                         searcher names (as produced), and sample reasons

Usage: python3 scripts/harvest_case_ids.py [--repo-root PATH]
"""

import argparse
import gzip
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SMPD_ORG = "San Mateo CA PD"
REDACTION_MARKERS = {"***", "REDACTED", ""}

SMPD_AUDIT_FILES = [
    "assets/transparency.flocksafety.com/san-mateo-ca-pd/pra-W012541-041426.json",
    "assets/transparency.flocksafety.com/san-mateo-ca-pd/pra-W012818-053026.json",
]
LA_DIR = "assets/los-altos-pras/json/pra-26-366"
LA_2026_GLOB = "Los_Altos_PD_Network_Audit_through_July_6_2026__*.ndjson.gz"
LA_JAN2025 = "Los_Altos_PD_Network_Audit_2025__JANUARY.ndjson.gz"
RWC_JAN2025 = "assets/redwood-city-pras/json/PRA_26_217_2025_1.ndjson.gz"

OUT_DIR = "assets/case-id-harvest"

# YYMMDD + 3- or 4-digit event number. Year restricted to 20-26 so stray
# phone-number-like digit runs mostly fail the date check anyway.
CAD_RE = re.compile(r"(?<![0-9])(2[0-6])(\d{2})(\d{2})(\d{3,4})(?![0-9])")
# Officer-typed variants seen in the corpora: full-year 20YYMMDDNNN(N),
# YY-MMDDNNN(N), YY-MMDD-NNN(N), and 8-digit YYMMDD + 2-digit event.
# All canonicalize to the digits-only YYMMDD+event form.
CAD_FULLYEAR_RE = re.compile(r"(?<![0-9])20(2[0-6])(\d{2})(\d{2})(\d{3,4})(?![0-9])")
CAD_HYPHEN_RE = re.compile(r"(?<![0-9])(2[0-6])-(\d{2})(\d{2})-?(\d{3,4})(?![0-9])")
CAD_SHORT_RE = re.compile(r"(?<![0-9])(2[0-6])(\d{2})(\d{2})(\d{2})(?![0-9])")
# Neighboring agencies' RMS/CAD numbers as typed by SMPD officers, e.g.
# SH25000089394 (SMCSO), EP26000012731 (East Palo Alto), DP26000028528
# (Daly City), GT26000000405. Two-letter agency prefix + YY + 9 digits.
AGENCY_RMS_RE = re.compile(r"(?<![0-9A-Za-z])([A-Z]{2})(2[0-6])(\d{9})(?![0-9])")
COURT_RE = re.compile(r"(?<![0-9A-Za-z])(\d{2})-?([A-Z]{2})-?(\d{5,7})([A-Z]?)(?![0-9A-Za-z])")
HYPHEN_RE = re.compile(r"(?<![0-9])(2[0-6])-(\d{4,6})(?![0-9])")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LONGNUM_RE = re.compile(r"(?<![0-9])\d{7,12}(?![0-9])")


def valid_date(yy, mm, dd):
    try:
        datetime(2000 + int(yy), int(mm), int(dd))
        return True
    except ValueError:
        return False


def extract_ids(text):
    """Return (ids, leftover_longnums). ids: list of dicts, de-duplicated."""
    ids, seen, consumed = [], set(), []
    for regex in (CAD_FULLYEAR_RE, CAD_RE, CAD_HYPHEN_RE, CAD_SHORT_RE):
        for m in regex.finditer(text):
            if any(s <= m.start() < e for s, e in consumed):
                continue
            yy, mm, dd, ev = m.groups()
            if not valid_date(yy, mm, dd):
                continue
            cid = f"{yy}{mm}{dd}{ev}"
            if cid in seen:
                continue
            seen.add(cid)
            consumed.append(m.span())
            ids.append({
                "id": cid,
                "kind": "cad_event",
                "cad_date": f"20{yy}-{mm}-{dd}",
            })
    for m in AGENCY_RMS_RE.finditer(text):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        cid = m.group(0)
        if cid in seen:
            continue
        seen.add(cid)
        consumed.append(m.span())
        ids.append({"id": cid, "kind": "agency_rms", "agency_prefix": m.group(1)})
    for m in COURT_RE.finditer(text):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        yy, county, num, suffix = m.groups()
        cid = f"{yy}{county}{num}{suffix}"
        if cid in seen:
            continue
        seen.add(cid)
        consumed.append(m.span())
        ids.append({"id": cid, "kind": "court_case", "county": county})
    for m in HYPHEN_RE.finditer(text):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        cid = m.group(0)
        if cid in seen:
            continue
        seen.add(cid)
        consumed.append(m.span())
        ids.append({"id": cid, "kind": "hyphen_case"})
    leftovers = []
    for m in LONGNUM_RE.finditer(text):
        if any(s <= m.start() < e or m.start() <= s < m.end() for s, e in consumed):
            continue
        leftovers.append(m.group(0))
    return ids, leftovers


def classify_reason(reason, ids, leftovers):
    if reason is None or reason == "":
        return "empty"
    if UUID_RE.match(reason.strip().lower()):
        return "uuid"
    if ids:
        return "case_id"
    if leftovers:
        return "unclassified_number"
    if re.search(r"\d", reason):
        return "code_or_other_digits"
    return "no_digits"


def split_category(reason):
    """Post-Dec-2025 reasons are 'Category - detail'. Returns (category, detail)."""
    if " - " in reason:
        cat, detail = reason.split(" - ", 1)
        return cat.strip(), detail.strip()
    return None, reason.strip()


TIME_RE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4}),\s+(\d{1,2}):(\d{2}):(\d{2})\s+(AM|PM)\s+UTC"
)


def parse_flock_time(s):
    """'02/20/2026, 05:17:49 AM UTC' or '1/3/2025, 08:54:25 PM UTC' -> ISO Z."""
    m = TIME_RE.match(s.strip())
    if not m:
        return None
    mm, dd, yyyy, hh, mi, ss, ampm = m.groups()
    hh = int(hh) % 12 + (12 if ampm == "PM" else 0)
    return datetime(
        int(yyyy), int(mm), int(dd), hh, int(mi), int(ss), tzinfo=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_scope(iso):
    """2026, plus the January 2025 slice."""
    if iso is None:
        return None
    if iso >= "2026-01-01":
        return "2026"
    if "2025-01-01" <= iso < "2025-02-01":
        return "2025-01"
    return None


def clean(v):
    """Normalize a produced cell: stringify, treat redaction markers as None."""
    if v is None:
        return None
    s = str(v).strip()
    return None if s in REDACTION_MARKERS else s


def make_row(source, source_file, period, search_time, reason, **extra):
    reason = "" if reason is None else str(reason).strip()
    if UUID_RE.match(reason.lower()):
        ids, leftovers = [], []  # Flock artifact; hex runs are not case numbers
    else:
        ids, leftovers = extract_ids(reason)
    category, detail = split_category(reason)
    row = {
        "source": source,
        "source_file": source_file,
        "period": period,
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
            iso = r.get("searchDate")
            period = in_scope(iso)
            if not period:
                continue
            yield make_row(
                "smpd_audit", path.name, period, iso, r.get("reason"),
                row_id=r.get("id"),
                networks=clean(r.get("networkCount")),
            )


def iter_network_rows(path, source, org_field="Org Name"):
    with gzip.open(path, "rt") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get(org_field) != SMPD_ORG:
                continue
            iso = parse_flock_time(r.get("Search Time", "")) if r.get("Search Time") else None
            period = in_scope(iso)
            if not period:
                continue
            yield make_row(
                source, path.name, period, iso, r.get("Reason"),
                name=clean(r.get("Name")),
                case_field=clean(r.get("Case #")),
                search_type=clean(r.get("Search Type")),
                networks=clean(r.get("Total Networks Searched")),
                devices=clean(r.get("Total Devices Searched")),
                time_frame=clean(r.get("Time Frame")),
            )


def aggregate(rows):
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
                "id": key,
                "kind": c["kind"],
                "n_searches": 0,
                "by_source": Counter(),
                "first_search": row["search_time"],
                "last_search": row["search_time"],
                "categories": set(),
                "searchers": set(),
                "sample_reasons": [],
            })
            for attr in ("cad_date", "county", "agency_prefix"):
                if attr in c:
                    e[attr] = c[attr]
            e["n_searches"] += 1
            e["by_source"][row["source"]] += 1
            e["first_search"] = min(e["first_search"], row["search_time"])
            e["last_search"] = max(e["last_search"], row["search_time"])
            if row["category"]:
                e["categories"].add(row["category"])
            if row.get("name"):
                e["searchers"].add(row["name"])
            if row["reason"] and row["reason"] not in e["sample_reasons"]:
                if len(e["sample_reasons"]) < 3:
                    e["sample_reasons"].append(row["reason"])
    out = []
    for e in sorted(index.values(), key=lambda e: (-e["n_searches"], e["id"])):
        e["by_source"] = dict(sorted(e["by_source"].items()))
        e["categories"] = sorted(e["categories"])
        e["searchers"] = sorted(e["searchers"])
        e["multi_category"] = len(e["categories"]) > 1
        out.append(e)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", type=Path,
                    default=Path(__file__).resolve().parent.parent)
    args = ap.parse_args()
    root = args.repo_root

    rows = list(iter_smpd_audit(root))
    la = root / LA_DIR
    for path in sorted(la.glob(LA_2026_GLOB)) + [la / LA_JAN2025]:
        rows.extend(iter_network_rows(path, "la_network"))
    rows.extend(iter_network_rows(root / RWC_JAN2025, "rwc_network"))

    rows.sort(key=lambda r: (r["search_time"], r["source"], r.get("row_id", ""), r["reason"]))

    out_dir = root / OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # mtime=0 keeps the gzip byte-identical across runs
    with (out_dir / "search_rows.ndjson.gz").open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            for row in rows:
                gz.write((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode())

    case_index = aggregate(rows)

    stats = {
        "rows_total": len(rows),
        "rows_by_source_period": dict(sorted(
            Counter(f'{r["source"]}/{r["period"]}' for r in rows).items()
        )),
        "reason_class": dict(sorted(Counter(r["reason_class"] for r in rows).items())),
        "rows_with_case_id": sum(1 for r in rows if r["case_ids"]),
        "unique_case_ids": len(case_index),
        "case_ids_by_kind": dict(sorted(Counter(e["kind"] for e in case_index).items())),
        "multi_category_case_ids": sum(1 for e in case_index if e["multi_category"]),
    }

    (out_dir / "case_ids.json").write_text(
        json.dumps({"stats": stats, "case_ids": case_index},
                   indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    )

    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    sys.exit(main())
