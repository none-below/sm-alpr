#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Two approximations built on top of scripts/hydrate_case_ids.py's output,
local-only (reads assets/case-id-hydration/ + assets/citizenrims/, no
network access):

1. Report-approval latency: occurrence1Date -> approvalDate for every
   CitizenRIMS case, per agency. NOT a case-closure/duration metric --
   verified median is 4-5 days, 90th percentile under 2 months across every
   agency checked. That's an administrative report-review sign-off, not
   "the investigation concluded" (real investigations routinely continue
   long after a report is administratively approved). Reported honestly as
   report-approval latency, not case-open duration -- CitizenRIMS exposes
   no case-closure date at all, and per-agency `disposition` codes (single
   letters/digits, e.g. SMPD's "C"/"A"/"4" vs Redwood City's "6"/"A"/"X" --
   different vocabularies, no decode table available) are not interpreted
   here to avoid asserting a meaning we can't verify.

2. ALPR-search activity window: for each cad_event ID that escalated to a
   real case (scripts/hydrate_case_ids.py's escalated_to_case), the case is
   treated as "under active ALPR attention" from its first citing search to
   its last -- a proxy for "open" built entirely from data we can trust
   (no RMS disposition guesswork), answering the like-for-like question:
   on a given day, how many escalated cases had an active search window,
   and how many searches happened that day. This is a lower bound on how
   long a case was genuinely under investigation (searches could stop
   before a case really closes) and an approximation, not the department's
   own case-open/close record.

Output: assets/case-id-hydration/case_activity.json
  approval_latency: per-agency + pooled histogram (day buckets) and
                    percentiles, from occurrence1Date -> approvalDate
  daily_activity:   per-agency + pooled date -> {active_cases, searches}
                    for every day with either an active case-search window
                    or at least one search

Usage: python3 scripts/analyze_case_activity.py
"""

import gzip
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CITIZENRIMS_DIR = REPO_ROOT / "assets/citizenrims"
HYDRATION_DIR = REPO_ROOT / "assets/case-id-hydration"

AGENCIES = sorted(p.name for p in HYDRATION_DIR.iterdir()
                  if p.is_dir() and (p / "case_ids.json").exists())

BUCKETS = [(0, 1), (1, 3), (3, 7), (7, 14), (14, 30), (30, 90), (90, 365), (365, None)]


def bucket_label(days):
    for lo, hi in BUCKETS:
        if hi is None or days < hi:
            if days >= lo:
                return f"{lo}-{hi}d" if hi is not None else f"{lo}d+"
    return "unknown"


def latest_snapshot(prefix):
    agency_dir = CITIZENRIMS_DIR / prefix
    dates = sorted(p.name for p in agency_dir.iterdir() if p.is_dir())
    return agency_dir / dates[-1]


def approval_latency(prefix):
    f = latest_snapshot(prefix) / "cases.json.gz"
    cases = json.loads(gzip.open(f, "rt").read())
    days_list = []
    for c in cases:
        if not c.get("approved"):
            continue
        occ, app = c.get("occurrence1Date"), c.get("approvalDate")
        if not (occ and app):
            continue
        try:
            d = (datetime.fromisoformat(app) - datetime.fromisoformat(occ)).days
        except ValueError:
            continue
        if d >= 0:
            days_list.append(d)
    days_list.sort()
    n = len(days_list)
    hist = Counter(bucket_label(d) for d in days_list)
    return {
        "n": n,
        "median_days": days_list[n // 2] if n else None,
        "p10_days": days_list[int(n * 0.1)] if n else None,
        "p90_days": days_list[int(n * 0.9)] if n else None,
        "histogram": {label: hist.get(label, 0) for label, _ in
                     [(bucket_label(lo), None) for lo, _ in BUCKETS]},
    }, days_list


def case_activity_windows(prefix):
    """escalated cad_event id -> (first_search date, last_search date)."""
    d = json.loads((HYDRATION_DIR / prefix / "case_ids.json").read_text())
    windows = {}
    for e in d["case_ids"]:
        if e["kind"] != "cad_event":
            continue
        hyd = e.get("hydration")
        if not hyd or not hyd.get("matched") or not hyd.get("escalated_to_case"):
            continue
        if not (e.get("first_search") and e.get("last_search")):
            continue
        windows[e["id"]] = (e["first_search"][:10], e["last_search"][:10])
    return windows


def daily_activity(prefix, windows):
    """date -> {active_cases, searches}. A case is "active" on any day its
    citing-search window spans; a search counts on the day it happened, for
    any search row whose extracted case_ids include an escalated id."""
    if not windows:
        return {}
    active_ids_by_id = windows
    all_dates = sorted({d for lo, hi in windows.values() for d in (lo, hi)})
    start, end = all_dates[0], all_dates[-1]

    active_cases = defaultdict(set)
    cur = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    while cur <= end_dt:
        day = cur.strftime("%Y-%m-%d")
        for cid, (lo, hi) in active_ids_by_id.items():
            if lo <= day <= hi:
                active_cases[day].add(cid)
        cur += timedelta(days=1)

    searches = Counter()
    escalated_ids = set(windows)
    with gzip.open(HYDRATION_DIR / prefix / "search_rows.ndjson.gz", "rt") as fh:
        for line in fh:
            r = json.loads(line)
            if not r.get("search_time"):
                continue
            ids = {c["id"] for c in r["case_ids"] if c["kind"] == "cad_event"}
            if ids & escalated_ids:
                searches[r["search_time"][:10]] += 1

    days = sorted(set(active_cases) | set(searches))
    return {d: {"active_cases": len(active_cases.get(d, ())), "searches": searches.get(d, 0)}
            for d in days}


def main():
    result = {"approval_latency": {}, "daily_activity": {}}
    pooled_days = []
    pooled_daily = defaultdict(lambda: {"active_cases": 0, "searches": 0})

    for prefix in AGENCIES:
        print(f"{prefix}...", file=sys.stderr)
        stats, days_list = approval_latency(prefix)
        result["approval_latency"][prefix] = stats
        pooled_days.extend(days_list)

        windows = case_activity_windows(prefix)
        daily = daily_activity(prefix, windows)
        result["daily_activity"][prefix] = daily
        for d, v in daily.items():
            pooled_daily[d]["active_cases"] += v["active_cases"]
            pooled_daily[d]["searches"] += v["searches"]

    pooled_days.sort()
    n = len(pooled_days)
    hist = Counter(bucket_label(d) for d in pooled_days)
    result["approval_latency"]["pooled"] = {
        "n": n,
        "median_days": pooled_days[n // 2] if n else None,
        "p10_days": pooled_days[int(n * 0.1)] if n else None,
        "p90_days": pooled_days[int(n * 0.9)] if n else None,
        "histogram": {label: hist.get(label, 0) for label, _ in
                     [(bucket_label(lo), None) for lo, _ in BUCKETS]},
    }
    result["daily_activity"]["pooled"] = dict(sorted(pooled_daily.items()))

    out = HYDRATION_DIR / "case_activity.json"
    out.write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
    print(f"written to {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
