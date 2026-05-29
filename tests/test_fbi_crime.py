#!/usr/bin/env python3
"""Unit tests for the searches-per-reported-crime resolver (fbi_crime.py).

Pure-function tests with inline fixtures — no API, no build, no disk.
Run with: uv run pytest tests/test_fbi_crime.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from fbi_crime import (  # noqa: E402
    audit_month_coverage,
    crime_monthly,
    latest_full_month,
    month_label,
    searches_per_crime,
)


def _crime(ori, violent, property_, max_data="04/2026"):
    """One ORI's crime record: {iso-ish MM-YYYY: count} per offense."""
    return {ori: {"agency_name": "Test PD", "max_data_date": max_data,
                  "offenses": {"violent-crime": violent, "property-crime": property_}}}


def _rows(*dates):
    """Audit rows from ISO date strings (one search each)."""
    return [{"id": f"r{i}", "searchDate": f"{d}T12:00:00Z"} for i, d in enumerate(dates)]


# ── crime_monthly / latest_full_month ──

def test_crime_monthly_sums_violent_and_property():
    crime = _crime("CA0411600", {"04-2026": 7, "03-2026": 15}, {"04-2026": 105, "03-2026": 100})
    months, max_data = crime_monthly(["CA0411600"], crime)
    assert months["2026-04"] == {"violent": 7, "property": 105, "total": 112}
    assert latest_full_month(months) == "2026-04"
    assert max_data == "2026-04"


def test_crime_monthly_drops_half_reported_month():
    # property has May but violent doesn't -> May excluded (no real Part 1 total)
    crime = _crime("X", {"04-2026": 7}, {"04-2026": 105, "05-2026": 110})
    months, _ = crime_monthly(["X"], crime)
    assert "2026-05" not in months
    assert latest_full_month(months) == "2026-04"


def test_crime_monthly_sums_across_ori_list():
    crime = {
        "A": {"offenses": {"violent-crime": {"04-2026": 3}, "property-crime": {"04-2026": 10}}},
        "B": {"offenses": {"violent-crime": {"04-2026": 4}, "property-crime": {"04-2026": 20}}},
    }
    months, _ = crime_monthly(["A", "B"], crime)
    assert months["2026-04"] == {"violent": 7, "property": 30, "total": 37}


def test_crime_monthly_keeps_explicit_zero():
    crime = _crime("X", {"04-2026": 0}, {"04-2026": 5})
    months, _ = crime_monthly(["X"], crime)
    assert months["2026-04"]["violent"] == 0
    assert months["2026-04"]["total"] == 5


def test_month_label():
    assert month_label("2026-04") == "April 2026"


# ── audit_month_coverage ──

def test_coverage_full_month():
    rows = _rows("2026-04-01", "2026-04-15", "2026-04-30")
    cov = audit_month_coverage(rows, "2026-04")
    assert cov["count"] == 3
    assert cov["covers_full_month"] is True
    assert cov["days_in_month"] == 30


def test_coverage_partial_month():
    # SMPD shape: covered Apr 11-30 only (first ~10 days missing)
    rows = _rows("2026-04-11", "2026-04-20", "2026-04-30")
    cov = audit_month_coverage(rows, "2026-04")
    assert cov["count"] == 3
    assert cov["covers_full_month"] is False
    assert cov["first"] == "2026-04-11"
    assert cov["last"] == "2026-04-30"


def test_coverage_no_rows_in_month():
    cov = audit_month_coverage(_rows("2026-05-10"), "2026-04")
    assert cov["count"] == 0
    assert cov["covers_full_month"] is False


# ── searches_per_crime resolution ──

def test_spc_audit_full_month_is_real_count():
    crime = _crime("X", {"04-2026": 7}, {"04-2026": 105})
    rows = _rows("2026-04-01", "2026-04-15", "2026-04-30")
    b = searches_per_crime(["X"], crime, rows, None)
    assert b["searches_source"] == "audit_month"
    assert b["searches"] == 3
    assert b["estimated"] is False
    assert b["ratio"] == round(3 / 112, 1)


def test_spc_partial_month_prorates_and_flags_estimate():
    # 20 searches over Apr 11-30 (20-day span) -> scaled to 30 days = 30
    crime = _crime("X", {"04-2026": 7}, {"04-2026": 105})
    rows = _rows(*[f"2026-04-{d:02d}" for d in range(11, 31)])  # 20 rows, Apr 11-30
    b = searches_per_crime(["X"], crime, rows, None)
    assert b["searches_source"] == "audit_month_prorated"
    assert b["estimated"] is True
    assert b["month_count"] == 20
    assert b["month_covered_days"] == 20
    assert b["month_prorated"] == 30  # 20 * 30/20
    assert b["searches"] == 30
    assert b["ratio"] == round(30 / 112, 1)


def test_spc_trailing_30d_when_month_uncovered():
    # FBI month is April, but audit data only exists in May -> trailing-30d
    crime = _crime("X", {"04-2026": 7}, {"04-2026": 105})
    rows = _rows("2026-05-10", "2026-05-20")
    b = searches_per_crime(["X"], crime, rows, None,
                           audit_30d={"count": 624, "window_start": "2026-04-27",
                                      "window_end": "2026-05-26"})
    assert b["searches_source"] == "audit_trailing_30d"
    assert b["searches"] == 624
    assert b["partial"] is True


def test_spc_portal_30d_fallback():
    crime = _crime("X", {"04-2026": 7}, {"04-2026": 105})
    b = searches_per_crime(["X"], crime, [], 200)
    assert b["searches_source"] == "portal_30d"
    assert b["searches"] == 200
    assert b["ratio"] == round(200 / 112, 1)


def test_spc_none_without_ori():
    assert searches_per_crime([], _crime("X", {"04-2026": 1}, {"04-2026": 1}), [], 5) is None


def test_spc_none_without_fbi_month():
    assert searches_per_crime(["X"], {}, _rows("2026-04-10"), 5) is None


def test_spc_ratio_none_when_zero_crime():
    crime = _crime("X", {"04-2026": 0}, {"04-2026": 0})
    b = searches_per_crime(["X"], crime, _rows("2026-04-01", "2026-04-30"), None)
    assert b["crime_total"] == 0
    assert b["ratio"] is None  # undefined, not infinity


def test_spc_prorated_beats_raw_partial_for_smpd_shape():
    # Real SMPD April: 509 searches over Apr 11-30, 112 crimes.
    # Raw partial would be 509/112=4.5 (understates); prorated ~764/112=6.8.
    crime = _crime("CA0411600", {"04-2026": 7}, {"04-2026": 105})
    rows = []
    # 509 rows spread across Apr 11-30 (exact per-day split doesn't matter,
    # only the span and count do for proration)
    for d in range(11, 31):
        for k in range(25 if d < 30 else 34):  # 19*25 + 34 = 509
            rows.append({"id": f"{d}-{k}", "searchDate": f"2026-04-{d:02d}T09:00:00Z"})
    assert len(rows) == 509
    b = searches_per_crime(["CA0411600"], crime, rows, None)
    assert b["month_count"] == 509
    assert b["searches"] == round(509 * 30 / 20)  # 764
    assert b["ratio"] == 6.8
