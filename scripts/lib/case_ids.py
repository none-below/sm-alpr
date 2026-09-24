#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Shared case-ID extraction/classification logic for ALPR search-reason text.
Pulled out of scripts/harvest_case_ids.py so scripts/hydrate_case_ids.py can
reuse the exact same parsing without drifting. Pure functions, no I/O.

Extracted identifier kinds:

  cad_event   YYMMDD + 3- or 4-digit event number (e.g. 2602190261 =
              2026-02-19 event 0261). Self-dating; the embedded date is
              validated and emitted as cad_date. The only kind with a
              cross-agency-consistent target format (CitizenRIMS
              incidentNumber is always YYYYMMDDNNNN on every agency
              probed) -- see scripts/hydrate_case_ids.py.
  agency_rms  Two-letter agency prefix + YY + 9 digits, e.g. SH25000089394.
  court_case  County criminal-case style, e.g. 25SM013446A.
  hyphen_case Classic RMS style YY-NNNNN(N). Target-format varies wildly by
              agency (confirmed by sampling all 11 SMC CitizenRIMS corpora:
              Menlo Park "19-10", Redwood City "R19-01-0254", San Bruno
              "SNB19-00240", San Mateo "190202020", ...) -- not safely
              joinable against caseNumber without per-agency parsing.
"""

import re
from datetime import datetime, timezone

REDACTION_MARKERS = {"***", "REDACTED", ""}

# YYMMDD + 3- or 4-digit event number. Year restricted to 20-26 so stray
# phone-number-like digit runs mostly fail the date check anyway.
CAD_RE = re.compile(r"(?<![0-9])(2[0-6])(\d{2})(\d{2})(\d{3,4})(?![0-9])")
# Officer-typed variants seen in the corpora: full-year 20YYMMDDNNN(N),
# YY-MMDDNNN(N), YY-MMDD-NNN(N), and 8-digit YYMMDD + 2-digit event.
# All canonicalize to the digits-only YYMMDD+event form.
CAD_FULLYEAR_RE = re.compile(r"(?<![0-9])20(2[0-6])(\d{2})(\d{2})(\d{3,4})(?![0-9])")
CAD_HYPHEN_RE = re.compile(r"(?<![0-9])(2[0-6])-(\d{2})(\d{2})-?(\d{3,4})(?![0-9])")
CAD_SHORT_RE = re.compile(r"(?<![0-9])(2[0-6])(\d{2})(\d{2})(\d{2})(?![0-9])")
# Neighboring agencies' RMS/CAD numbers as typed by officers, e.g.
# SH25000089394 (SMCSO), EP26000012731 (East Palo Alto), DP26000028528
# (Daly City), GT26000000405. Two-letter agency prefix + YY + 9 digits.
AGENCY_RMS_RE = re.compile(r"(?<![0-9A-Za-z])([A-Z]{2})(2[0-6])(\d{9})(?![0-9])")
COURT_RE = re.compile(r"(?<![0-9A-Za-z])(\d{2})-?([A-Z]{2})-?(\d{5,7})([A-Z]?)(?![0-9A-Za-z])")
HYPHEN_RE = re.compile(r"(?<![0-9])(2[0-6])-(\d{4,6})(?![0-9])")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LONGNUM_RE = re.compile(r"(?<![0-9])\d{7,12}(?![0-9])")

TIME_RE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4}),\s+(\d{1,2}):(\d{2}):(\d{2})\s+(AM|PM)\s+UTC"
)


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


def clean(v):
    """Normalize a produced cell: stringify, treat redaction markers as None."""
    if v is None:
        return None
    s = str(v).strip()
    return None if s in REDACTION_MARKERS else s
