#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Tests for the pattern-driven federal-LE query detector.

The detection patterns are the load-bearing part: too loose and benign
reasons get flagged as federal-LE activity; too tight and real references
slip through. These tests lock the calibrated behavior, especially the
false-positive exclusions (Marshall / idea / service-fragment).
"""

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "federal_le_queries",
    Path(__file__).parent.parent / "scripts" / "federal_le_queries.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

classify_reason = _mod.classify_reason
scan_rows = _mod.scan_rows
build_index = _mod.build_index


# ── classify_reason: positive matches (verbatim from observed portals) ──

@pytest.mark.parametrize("reason,expected", [
    ("Usms case", "usms"),
    ("usms cas", "usms"),
    ("U.S. Marshal warrant", "usms"),
    ("US Marshals Service assist", "usms"),
    ("FBI ASSIST: BANK THEFT", "fbi"),
    ("AOA for fbi", "fbi"),
    ("dea inquiry", "dea"),
    ("ATF case 12345", "atf"),
    ("DHS AOD", "dhs"),
    ("HSI request", "dhs"),
    ("CBP lookout", "dhs"),
    ("ICE detainer", "dhs"),
    ("Homeland Security tip", "dhs"),
    ("Border Patrol assist", "dhs"),
    ("POTUS threat. Search for USSS", "usss"),
    ("Secret Service detail", "usss"),
    # Out-of-state assist: non-CA state name beside an LE token.
    ("Assist Talent PD (Oregon) BOL Fraud S-Veh", "oos"),
    ("Oregon State Police BOL", "oos"),
    ("assist for Texas DPS", "oos"),
    ("Clark County Nevada sheriff request", "oos"),  # Nevada (not "Nevada City")
])
def test_classify_positive(reason, expected):
    assert classify_reason(reason) == expected


# ── classify_reason: false-positive exclusions ──
# These are the look-alikes the narrow patterns must NOT match. "Marshall"
# is the surname/place case that the broad \bmarshal pattern over-matched
# (Buena Park PD); the rest guard the short-token patterns.

@pytest.mark.parametrize("reason", [
    "Officer Marshall responding",       # surname, not U.S. Marshals
    "Marshall Street collision",         # place name
    "good idea to check this plate",     # 'idea' must not trip \bdea\b
    "dead body investigation",           # 'dead'
    "narcotics deal in progress",        # 'deal'
    "service of a search warrant",       # 'service' must not trip \bice\b
    "police welfare check",              # 'police'
    "notice to appear follow-up",        # 'notice'
    "stolen vehicle recovery",           # nothing federal
    "187 PC homicide",                   # penal code, historically a false ICE hit
    # Out-of-state guards: a state name with no adjacent LE token, a CA
    # place named after a state, and the "from out of state" origin phrase
    # must all stay unflagged.
    "Washington Blvd collision",         # street, no LE token
    "Virginia St burglary",              # street, no LE token
    "Nevada City PD assist",             # CA agency (Nevada County, CA)
    "Nevada County sheriff request",     # CA agency
    "offender from out of state",        # suspect origin, not a transfer
    "suspect last seen in Oregon",       # travel, no LE token nearby
    "",                                  # empty
    None,                                # missing
])
def test_classify_no_false_positive(reason):
    assert classify_reason(reason) is None


def test_classify_first_match_priority():
    # A reason naming two agencies resolves to the first category in
    # DETECT_CATEGORIES order (usms before fbi; federal before out-of-state).
    assert classify_reason("USMS / FBI joint fugitive task force") == "usms"
    assert classify_reason("FBI assist for Oregon State Police") == "fbi"


# ── scan_rows aggregation ──

def _rows(*specs):
    """specs: (reason, searchDate) tuples → audit-row dicts."""
    return [{"reason": r, "searchDate": d} for r, d in specs]


def test_scan_rows_none_when_empty():
    assert scan_rows([]) is None
    assert scan_rows(_rows(("stolen vehicle", "2026-01-01T00:00:00Z"))) is None


def test_scan_rows_counts_and_categories():
    rows = _rows(
        ("Usms case", "2026-03-04T10:00:00Z"),
        ("Usms request DAI smith", "2026-03-05T10:00:00Z"),
        ("FBI ASSIST: BANK THEFT", "2026-04-01T10:00:00Z"),
        ("FBI safe streets", "2026-04-02T10:00:00Z"),
        ("FBI assist again", "2026-04-03T10:00:00Z"),
        ("stolen vehicle", "2026-04-04T10:00:00Z"),  # ignored
    )
    out = scan_rows(rows, max_samples=2)
    assert out["total"] == 5
    # Categories sorted by count desc: FBI (3) before USMS (2).
    assert [c["key"] for c in out["categories"]] == ["fbi", "usms"]
    assert out["categories"][0] == {"key": "fbi", "label": "FBI", "count": 3}
    assert out["date_min"] == "2026-03-04"
    assert out["date_max"] == "2026-04-03"  # the ignored row's date excluded


def test_scan_rows_prefers_shortest_sample():
    # The shortest distinct reason is surfaced first, keeping a potential
    # subject name ("...smith") out of the headline sample.
    rows = _rows(
        ("Usms request DAI smith", "2026-03-05T10:00:00Z"),
        ("Usms case", "2026-03-04T10:00:00Z"),
    )
    out = scan_rows(rows, max_samples=1)
    assert out["samples"] == ["Usms case"]


def test_scan_rows_sample_truncated():
    long_reason = "USMS " + "x" * 200
    out = scan_rows(_rows((long_reason, "2026-03-04T10:00:00Z")), max_samples=1)
    assert out["samples"][0].endswith("…")
    assert len(out["samples"][0]) <= 80


# ── build_index over a directory of audit JSONs ──

def test_build_index(tmp_path):
    (tmp_path / "stockton-ca-pd.json").write_text(json.dumps({
        "portal": "stockton-ca-pd",
        "rows": _rows(
            ("Usms case", "2026-03-04T10:00:00Z"),
            ("Usms case", "2026-03-05T10:00:00Z"),
            ("traffic stop", "2026-03-06T10:00:00Z"),
        ),
    }))
    (tmp_path / "clean-ca-pd.json").write_text(json.dumps({
        "portal": "clean-ca-pd",
        "rows": _rows(("stolen vehicle", "2026-03-04T10:00:00Z")),
    }))

    # Keyed by slug by default; clean agency omitted.
    by_slug = build_index(tmp_path)
    assert set(by_slug) == {"stockton-ca-pd"}
    assert by_slug["stockton-ca-pd"]["total"] == 2

    # key_by remaps to agency_id and attaches the source slug.
    by_id = build_index(tmp_path, key_by={"stockton-ca-pd": "AID-1"})
    assert set(by_id) == {"AID-1"}
    assert by_id["AID-1"]["slug"] == "stockton-ca-pd"


def test_build_index_missing_dir(tmp_path):
    assert build_index(tmp_path / "does-not-exist") == {}
