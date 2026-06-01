#!/usr/bin/env python3
"""Tests for the ingest-integrity fingerprint in scripts/lib.py.

The whole point of the block is to make the as-delivered row order legible from
the JSON we read: a date-sorted CSV (portal) vs. a userId-then-date one (PRA)
must produce visibly different fingerprints.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from lib import audit_integrity, parse_iso_dt, parse_pra_datetime


def _rows(dates, ids=None):
    ids = ids or [f"id{i}" for i in range(len(dates))]
    return [{"id": i, "searchDate": d} for i, d in zip(ids, dates)]


class TestAuditIntegrity:
    def test_date_sorted_portal_has_no_resets(self):
        rows = _rows([
            "2026-04-27T00:27:25Z",
            "2026-04-27T00:28:42Z",
            "2026-05-01T10:00:00Z",
        ])
        r = audit_integrity(rows)
        assert r["date_resets"] == 0
        assert r["date_ordering"] == "ascending"
        assert r["row_count"] == 3
        assert r["date_span"]["min"] == r["date_span"]["first"]
        assert r["date_span"]["max"] == r["date_span"]["last"]

    def test_userid_grouped_pra_shows_resets(self):
        # three user blocks, each internally date-ascending; the two block
        # boundaries step backwards -> 2 resets, and ordering is "grouped".
        rows = _rows([
            "2023-01-01T01:00:00Z", "2023-01-01T02:00:00Z", "2023-01-01T03:00:00Z",
            "2023-01-01T01:30:00Z", "2023-01-01T02:30:00Z",
            "2023-01-01T00:30:00Z", "2023-01-01T05:00:00Z",
        ])
        r = audit_integrity(rows)
        assert r["date_resets"] == 2
        assert r["date_ordering"] == "grouped"

    def test_descending(self):
        rows = _rows([
            "2026-05-03T00:00:00Z", "2026-05-02T00:00:00Z", "2026-05-01T00:00:00Z",
        ])
        r = audit_integrity(rows)
        assert r["date_ordering"] == "descending"
        assert r["date_resets"] == 2

    def test_duplicate_ids(self):
        rows = _rows(
            ["2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", "2026-05-03T00:00:00Z"],
            ids=["a", "a", "b"],
        )
        assert audit_integrity(rows)["duplicate_ids"] == 1

    def test_unparsed_dates_counted_not_fatal(self):
        rows = _rows(["2026-05-01T00:00:00Z", "not-a-date", "2026-05-03T00:00:00Z"])
        r = audit_integrity(rows)
        assert r["unparsed_dates"] == 1
        assert r["row_count"] == 3

    def test_empty_is_constant(self):
        r = audit_integrity([])
        assert r["row_count"] == 0
        assert r["date_resets"] == 0
        assert r["date_ordering"] == "constant"
        assert r["date_span"] is None

    def test_custom_to_dt_for_pra_format(self):
        rows = [
            {"id": "a", "searchDate": "01/01/2023, 01:00:00 AM UTC"},
            {"id": "b", "searchDate": "01/01/2023, 02:00:00 AM UTC"},
            {"id": "c", "searchDate": "01/01/2023, 01:30:00 AM UTC"},  # reset
        ]
        r = audit_integrity(rows, to_dt=parse_pra_datetime)
        assert r["date_resets"] == 1
        assert "unparsed_dates" not in r  # all parsed cleanly


class TestDateParsers:
    def test_iso_with_millis_and_z(self):
        assert parse_iso_dt("2026-04-27T00:27:25.717Z") == datetime.fromisoformat(
            "2026-04-27T00:27:25.717+00:00")

    def test_iso_bad_and_empty(self):
        assert parse_iso_dt("") is None
        assert parse_iso_dt(None) is None
        assert parse_iso_dt("nope") is None

    def test_pra_pm_am_noon_midnight(self):
        assert parse_pra_datetime("01/23/2023, 06:15:22 PM UTC") == datetime(2023, 1, 23, 18, 15, 22)
        assert parse_pra_datetime("12/01/2023, 12:05:00 AM UTC") == datetime(2023, 12, 1, 0, 5, 0)
        assert parse_pra_datetime("12/01/2023, 12:05:00 PM UTC") == datetime(2023, 12, 1, 12, 5, 0)

    def test_pra_bad(self):
        assert parse_pra_datetime("2023-01-01T00:00:00Z") is None
        assert parse_pra_datetime(None) is None
