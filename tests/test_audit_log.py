#!/usr/bin/env python3
"""Tests for the audit log union tool."""

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "build_audit_log",
    Path(__file__).parent.parent / "scripts" / "build_audit_log.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

normalize_row = _mod.normalize_row
load_portal_rows = _mod.load_portal_rows
write_log = _mod.write_log


def _write_scrape(scrape_dir, portal, date, rows):
    """Write a minimal scrape JSON with the given audit rows."""
    portal_dir = scrape_dir / portal
    portal_dir.mkdir(parents=True, exist_ok=True)
    payload = {"search_audit_csv": rows}
    (portal_dir / f"{date}.json").write_text(json.dumps(payload))


@pytest.fixture
def scrape_dir(tmp_path, monkeypatch):
    d = tmp_path / "scrapes"
    d.mkdir()
    monkeypatch.setattr(_mod, "SCRAPE_DIR", d)
    return d


class TestNormalizeRow:
    def test_column_order(self):
        raw = {
            "reason": "211",
            "id": "abc",
            "networkCount": "50",
            "searchDate": "2026-04-01T00:00:00Z",
            "userId": "***",
        }
        out = normalize_row(raw, first_seen="2026-04-05")
        assert list(out.keys()) == [
            "id",
            "userId",
            "searchDate",
            "networkCount",
            "reason",
            "first_seen",
        ]

    def test_omits_empty_optional_fields(self):
        raw = {
            "id": "abc",
            "userId": "***",
            "searchDate": "2026-04-01T00:00:00Z",
            "networkCount": "5",
            "reason": "",
            "caseNumber": "   ",
        }
        out = normalize_row(raw, first_seen="2026-04-05")
        assert "reason" not in out
        assert "caseNumber" not in out
        assert out["id"] == "abc"
        assert out["first_seen"] == "2026-04-05"

    def test_preserves_nonempty_optional_fields(self):
        raw = {
            "id": "abc",
            "userId": "***",
            "searchDate": "2026-04-01T00:00:00Z",
            "networkCount": "5",
            "reason": "211",
            "caseNumber": "CN-1",
            "offenseType": "Robbery",
        }
        out = normalize_row(raw, first_seen="2026-04-05")
        assert out["reason"] == "211"
        assert out["caseNumber"] == "CN-1"
        assert out["offenseType"] == "Robbery"

    def test_required_fields_kept_when_blank(self):
        # Required columns (id/userId/searchDate/networkCount) kept as-is,
        # even if blank — only reason/caseNumber/offenseType are filtered.
        raw = {"id": "abc", "userId": "", "searchDate": "", "networkCount": ""}
        out = normalize_row(raw, first_seen="2026-04-05")
        assert out["userId"] == ""
        assert out["searchDate"] == ""


class TestLoadPortalRows:
    def test_returns_none_when_no_scrapes(self, scrape_dir):
        rows, meta = load_portal_rows("nonexistent-agency")
        assert rows is None
        assert meta is None

    def test_returns_none_when_scrapes_have_no_audit_csv(self, scrape_dir):
        portal_dir = scrape_dir / "agency-x"
        portal_dir.mkdir()
        (portal_dir / "2026-04-01.json").write_text(json.dumps({}))
        rows, meta = load_portal_rows("agency-x")
        assert rows is None

    def test_dedupes_by_id_across_scrapes(self, scrape_dir):
        shared = {
            "id": "row1",
            "userId": "***",
            "searchDate": "2026-03-15T12:00:00Z",
            "networkCount": "10",
            "reason": "211",
        }
        _write_scrape(scrape_dir, "agency-x", "2026-04-01", [shared])
        _write_scrape(scrape_dir, "agency-x", "2026-04-08", [shared])
        rows, meta = load_portal_rows("agency-x")
        assert meta["row_count"] == 1
        assert rows[0]["first_seen"] == "2026-04-01"

    def test_preserves_earliest_first_seen(self, scrape_dir):
        # id present in both scrapes — first_seen should be the earlier one
        row = {
            "id": "shared",
            "userId": "***",
            "searchDate": "2026-03-15T12:00:00Z",
            "networkCount": "10",
            "reason": "211",
        }
        _write_scrape(scrape_dir, "agency-x", "2026-04-08", [row])
        _write_scrape(scrape_dir, "agency-x", "2026-04-01", [row])
        rows, _ = load_portal_rows("agency-x")
        assert rows[0]["first_seen"] == "2026-04-01"

    def test_sorted_by_search_date_ascending(self, scrape_dir):
        rows_in = [
            {"id": "c", "userId": "***", "searchDate": "2026-03-20T00:00:00Z", "networkCount": "5"},
            {"id": "a", "userId": "***", "searchDate": "2026-03-10T00:00:00Z", "networkCount": "5"},
            {"id": "b", "userId": "***", "searchDate": "2026-03-15T00:00:00Z", "networkCount": "5"},
        ]
        _write_scrape(scrape_dir, "agency-x", "2026-04-01", rows_in)
        rows, _ = load_portal_rows("agency-x")
        assert [r["id"] for r in rows] == ["a", "b", "c"]

    def test_handles_mixed_schema_variants(self, scrape_dir):
        # One scrape with reason, another with caseNumber+offenseType
        _write_scrape(
            scrape_dir,
            "agency-x",
            "2026-04-01",
            [{"id": "r1", "userId": "***", "searchDate": "2026-03-10T00:00:00Z", "networkCount": "5", "reason": "211"}],
        )
        _write_scrape(
            scrape_dir,
            "agency-x",
            "2026-04-08",
            [{"id": "r2", "userId": "***", "searchDate": "2026-03-11T00:00:00Z", "networkCount": "5", "caseNumber": "CN-9", "offenseType": "Theft"}],
        )
        rows, meta = load_portal_rows("agency-x")
        assert meta["row_count"] == 2
        assert rows[0]["reason"] == "211"
        assert "caseNumber" not in rows[0]
        assert rows[1]["caseNumber"] == "CN-9"
        assert rows[1]["offenseType"] == "Theft"
        assert set(meta["schema_seen"]) >= {"reason", "caseNumber", "offenseType"}

    def test_skips_rows_without_id(self, scrape_dir):
        _write_scrape(
            scrape_dir,
            "agency-x",
            "2026-04-01",
            [
                {"id": "", "userId": "***", "searchDate": "2026-03-10T00:00:00Z", "networkCount": "5"},
                {"id": "good", "userId": "***", "searchDate": "2026-03-11T00:00:00Z", "networkCount": "5"},
            ],
        )
        rows, meta = load_portal_rows("agency-x")
        assert meta["row_count"] == 1
        assert rows[0]["id"] == "good"

    def test_meta_reports_scrape_and_search_date_ranges(self, scrape_dir):
        _write_scrape(
            scrape_dir,
            "agency-x",
            "2026-04-01",
            [{"id": "r1", "userId": "***", "searchDate": "2026-03-10T00:00:00Z", "networkCount": "5"}],
        )
        _write_scrape(
            scrape_dir,
            "agency-x",
            "2026-04-15",
            [{"id": "r2", "userId": "***", "searchDate": "2026-04-14T00:00:00Z", "networkCount": "5"}],
        )
        _, meta = load_portal_rows("agency-x")
        assert meta["first_scrape"] == "2026-04-01"
        assert meta["last_scrape"] == "2026-04-15"
        assert meta["search_date_min"] == "2026-03-10"
        assert meta["search_date_max"] == "2026-04-14"
        assert meta["scrape_count"] == 2


class TestIdempotent:
    def test_rerun_produces_identical_output(self, scrape_dir, tmp_path, monkeypatch):
        out_dir = tmp_path / "out"
        monkeypatch.setattr(_mod, "OUT_DIR", out_dir)
        _write_scrape(
            scrape_dir,
            "agency-x",
            "2026-04-01",
            [{"id": "r1", "userId": "***", "searchDate": "2026-03-10T00:00:00Z", "networkCount": "5", "reason": "211"}],
        )
        rows, meta = load_portal_rows("agency-x")
        write_log("agency-x", rows, meta)
        first = (out_dir / "agency-x.json").read_text()

        rows2, meta2 = load_portal_rows("agency-x")
        write_log("agency-x", rows2, meta2)
        second = (out_dir / "agency-x.json").read_text()
        assert first == second
