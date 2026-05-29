#!/usr/bin/env python3
"""Unit tests for the searches-per-reported-crime resolver (fbi_crime.py).

Pure-function tests with inline fixtures — no API, no build, no disk.
Run with: uv run pytest tests/test_fbi_crime.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from fbi_crime import (  # noqa: E402
    audit_month_coverage,
    crime_monthly,
    join_snapshots,
    latest_full_month,
    load_crime,
    month_label,
    searches_per_crime,
)


def _snap(violent, property_, **meta):
    """A single dated snapshot for one ORI (offenses already null-stripped)."""
    return {"offenses": {"violent-crime": violent, "property-crime": property_}, **meta}


def _crime(ori, violent, property_, max_data="05/2026"):
    """One ORI's crime record: {iso-ish MM-YYYY: count} per offense.

    Default frontier (max_data) is May so April data is "settled" and
    usable by searches_per_crime (which excludes the frontier month).
    """
    return {ori: {"agency_name": "Test PD", "max_data_date": max_data,
                  "offenses": {"violent-crime": violent, "property-crime": property_}}}


def _rows(*dates):
    """Audit rows from ISO date strings (one search each)."""
    return [{"id": f"r{i}", "searchDate": f"{d}T12:00:00Z"} for i, d in enumerate(dates)]


# ── crime_monthly / latest_full_month ──

def test_crime_monthly_sums_violent_and_property():
    crime = _crime("CA0411600", {"04-2026": 7, "03-2026": 15}, {"04-2026": 105, "03-2026": 100},
                   max_data="04/2026")
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


# ── join_snapshots / load_crime (historical snapshots → current view) ──

def test_join_newer_snapshot_wins_per_month():
    # April revised 112->118 in a later pull; newest non-null wins.
    older = _snap({"04-2026": 7}, {"04-2026": 105}, agency_name="PD", max_data_date="04/2026")
    newer = _snap({"04-2026": 9}, {"04-2026": 109}, agency_name="PD", max_data_date="05/2026")
    merged = join_snapshots([older, newer])  # oldest -> newest
    assert merged["offenses"]["violent-crime"]["04-2026"] == 9
    assert merged["offenses"]["property-crime"]["04-2026"] == 109
    assert merged["max_data_date"] == "05/2026"


def test_join_retains_month_absent_from_later_snapshot():
    # March present in the old pull but dropped (null) in the new one -> retained.
    older = _snap({"03-2026": 15, "04-2026": 7}, {"03-2026": 100, "04-2026": 105})
    newer = _snap({"04-2026": 7}, {"04-2026": 105})  # March absent (transient null)
    merged = join_snapshots([older, newer])
    assert merged["offenses"]["violent-crime"]["03-2026"] == 15
    assert merged["offenses"]["violent-crime"]["04-2026"] == 7


def test_join_fills_late_arriving_month():
    older = _snap({"03-2026": 15}, {"03-2026": 100})
    newer = _snap({"03-2026": 15, "04-2026": 7}, {"03-2026": 100, "04-2026": 105})
    merged = join_snapshots([older, newer])
    assert merged["offenses"]["violent-crime"]["04-2026"] == 7


def test_load_crime_reads_and_joins_snapshot_dir(tmp_path):
    root = tmp_path / "cde"
    d = root / "CA0411600"
    d.mkdir(parents=True)
    (d / "2026-04-16.json").write_text(json.dumps(
        _snap({"03-2026": 15}, {"03-2026": 100}, agency_name="San Mateo PD")))
    (d / "2026-05-16.json").write_text(json.dumps(
        _snap({"03-2026": 15, "04-2026": 7}, {"03-2026": 100, "04-2026": 105})))
    crime = load_crime(root)
    assert set(crime) == {"CA0411600"}
    months, _ = crime_monthly(["CA0411600"], crime)
    assert latest_full_month(months) == "2026-04"
    assert months["2026-04"]["total"] == 112


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


def test_spc_excludes_partial_frontier_month():
    # May is the frontier (max_data_date) and only partially reported (low
    # undercount). The metric must use settled April, not partial May —
    # otherwise the denominator shrinks and the ratio inflates.
    crime = {"X": {"max_data_date": "05/2026",
                   "offenses": {"violent-crime": {"04-2026": 7, "05-2026": 2},
                                "property-crime": {"04-2026": 105, "05-2026": 20}}}}
    b = searches_per_crime(["X"], crime, _rows("2026-04-01", "2026-04-30"), None)
    assert b["month"] == "2026-04"
    assert b["crime_total"] == 112  # April, not May's partial 22


def test_spc_uses_month_once_frontier_moves_past_it():
    # Once June is the frontier, May has settled and becomes usable.
    crime = {"X": {"max_data_date": "06/2026",
                   "offenses": {"violent-crime": {"05-2026": 9},
                                "property-crime": {"05-2026": 120}}}}
    b = searches_per_crime(["X"], crime, _rows("2026-05-01", "2026-05-31"), None)
    assert b["month"] == "2026-05"
    assert b["crime_total"] == 129


def test_spc_none_when_only_frontier_month_present():
    # A brand-new agency whose only data is the unsettled frontier month.
    crime = {"X": {"max_data_date": "05/2026",
                   "offenses": {"violent-crime": {"05-2026": 9},
                                "property-crime": {"05-2026": 120}}}}
    assert searches_per_crime(["X"], crime, _rows("2026-05-10"), None) is None


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
