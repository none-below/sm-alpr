#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Select suspect case IDs from the harvest for the future case-lookup step.

Reads assets/case-id-harvest/case_ids.json and writes suspect_case_ids.json
next to it. Pool: cad_event IDs dated 2025-03-01 or later (CitizenRIMS is
dense from March 2025; earlier IDs, court cases, and other agencies' RMS
numbers cannot be resolved there and are listed separately when notable).

Suspicion flags (an ID is a suspect if it carries at least one):

  multi_category      searched under 2+ Flock crime categories; divergence
                      is "adjacent" when all categories fall in one of the
                      documented clusters below, else "divergent"
  high_volume         >= 40 searches (top ~3% of the pool)
  long_span           >= 30 days between first and last search
  stale_start         first search >= 60 days after the embedded CAD date
  traffic_infraction  any category is "Traffic Infraction" (Policy 463
                      purpose-limit angle)

Usage: python3 scripts/select_suspect_case_ids.py [--repo-root PATH]
"""

import argparse
import json
from datetime import date
from pathlib import Path

HARVEST = "assets/case-id-harvest/case_ids.json"
OUT = "assets/case-id-harvest/suspect_case_ids.json"

LOOKUP_MIN_CAD_DATE = "2025-03-01"
HIGH_VOLUME = 40
LONG_SPAN_DAYS = 30
STALE_START_DAYS = 60

# Category pairs that plausibly describe one incident. Multi-category IDs
# whose categories all fall inside a single cluster are "adjacent"; anything
# else is "divergent" and a stronger suspect.
ADJACENT_CLUSTERS = [
    {"Motor Vehicle Theft/Stolen", "Stolen Property Offenses",
     "Larceny/Theft Offenses"},
    {"Assault/Battery Offenses", "Assault/Battery Offenses (Domestic)"},
    {"Criminal Motor Vehicle Offense (incl. Road Rage/Reckless)",
     "Obstructing the Police (Fleeing/Eluding)",
     "Driving Under the Influence (DUI/DWI/OWI/OVI)",
     "Alcohol Offenses (Non-DUI)"},
    {"Welfare Check", "Missing/Endangered Person/Runaway"},
    {"Disorderly Conduct/Disturbance", "Disturbing Public Peace/Riot"},
]


def iso_day(s):
    return date.fromisoformat(s[:10])


def flag(entry):
    flags = {}
    cats = entry["categories"]
    if len(cats) > 1:
        adjacent = any(set(cats) <= cluster for cluster in ADJACENT_CLUSTERS)
        flags["multi_category"] = "adjacent" if adjacent else "divergent"
    if entry["n_searches"] >= HIGH_VOLUME:
        flags["high_volume"] = entry["n_searches"]
    span = (iso_day(entry["last_search"]) - iso_day(entry["first_search"])).days
    if span >= LONG_SPAN_DAYS:
        flags["long_span_days"] = span
    if "cad_date" in entry:
        lag = (iso_day(entry["first_search"]) - iso_day(entry["cad_date"])).days
        if lag >= STALE_START_DAYS:
            flags["stale_start_days"] = lag
    if "Traffic Infraction" in cats:
        flags["traffic_infraction"] = True
    return flags


def why_excluded(entry):
    if entry["kind"] == "court_case":
        return "court_case: resolve via superior-court records, not CitizenRIMS"
    if entry["kind"] == "agency_rms":
        return "agency_rms: another agency's RMS number"
    if entry["kind"] == "hyphen_case":
        return "hyphen_case: format ambiguous / often another agency's case"
    return f"cad_date {entry.get('cad_date', '?')} predates dense CitizenRIMS coverage ({LOOKUP_MIN_CAD_DATE})"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", type=Path,
                    default=Path(__file__).resolve().parent.parent)
    args = ap.parse_args()
    root = args.repo_root

    index = json.loads((root / HARVEST).read_text())["case_ids"]

    suspects, excluded_notable = [], []
    for e in index:
        flags = flag(e)
        lookupable = e["kind"] == "cad_event" and e.get("cad_date", "") >= LOOKUP_MIN_CAD_DATE
        record = {
            "id": e["id"],
            "kind": e["kind"],
            **({"cad_date": e["cad_date"]} if "cad_date" in e else {}),
            "n_searches": e["n_searches"],
            "by_source": e["by_source"],
            "first_search": e["first_search"],
            "last_search": e["last_search"],
            "categories": e["categories"],
            "flags": flags,
            "sample_reasons": e["sample_reasons"],
        }
        if lookupable and flags:
            suspects.append(record)
        elif flags and (len(e["categories"]) > 1 or e["n_searches"] >= HIGH_VOLUME
                        or "long_span_days" in flags):
            record["why_excluded"] = why_excluded(e)
            excluded_notable.append(record)

    def rank(r):
        f = r["flags"]
        return (
            -(len(f) + (1 if f.get("multi_category") == "divergent" else 0)),
            -r["n_searches"],
            r["id"],
        )

    suspects.sort(key=rank)
    excluded_notable.sort(key=lambda r: (-r["n_searches"], r["id"]))

    out = {
        "criteria": {
            "pool": f"cad_event IDs with cad_date >= {LOOKUP_MIN_CAD_DATE}",
            "high_volume_min_searches": HIGH_VOLUME,
            "long_span_min_days": LONG_SPAN_DAYS,
            "stale_start_min_days": STALE_START_DAYS,
            "note": "n_searches counts produced rows across sources, not distinct search events",
        },
        "counts": {
            "suspects": len(suspects),
            "by_flag": {
                k: sum(1 for s in suspects if k in s["flags"])
                for k in ("multi_category", "high_volume", "long_span_days",
                          "stale_start_days", "traffic_infraction")
            },
            "multi_category_divergent": sum(
                1 for s in suspects if s["flags"].get("multi_category") == "divergent"),
            "excluded_notable": len(excluded_notable),
        },
        "suspects": suspects,
        "excluded_notable": excluded_notable,
    }
    (root / OUT).write_text(
        json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(out["counts"], indent=2))


if __name__ == "__main__":
    main()
