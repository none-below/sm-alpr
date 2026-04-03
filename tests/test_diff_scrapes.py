#!/usr/bin/env python3
"""Tests for diff_scrapes.py."""

import importlib.util
import json
from pathlib import Path

import pytest

# Load diff_scrapes as a module from the scripts directory
_spec = importlib.util.spec_from_file_location(
    "diff_scrapes", Path(__file__).parent.parent / "scripts" / "diff_scrapes.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
diff_agency = _mod.diff_agency


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class TestDiffAgency:
    def test_single_scrape_no_diff(self, tmp_path):
        slug_dir = tmp_path / "test-agency"
        _write_json(slug_dir / "2026-03-27.json", {"slug": "test-agency", "camera_count": 10})
        assert diff_agency(slug_dir) == []

    def test_no_changes(self, tmp_path):
        slug_dir = tmp_path / "test-agency"
        data = {"slug": "test-agency", "archived_date": "2026-03-27", "camera_count": 10}
        _write_json(slug_dir / "2026-03-27.json", data)
        data2 = {**data, "archived_date": "2026-04-03"}
        _write_json(slug_dir / "2026-04-03.json", data2)
        assert diff_agency(slug_dir) == []

    def test_numeric_change(self, tmp_path):
        slug_dir = tmp_path / "test-agency"
        _write_json(slug_dir / "2026-03-27.json", {
            "slug": "test-agency", "archived_date": "2026-03-27", "camera_count": 10,
        })
        _write_json(slug_dir / "2026-04-03.json", {
            "slug": "test-agency", "archived_date": "2026-04-03", "camera_count": 15,
        })
        result = diff_agency(slug_dir)
        assert any("camera_count: 10 -> 15" in line for line in result)

    def test_string_change(self, tmp_path):
        slug_dir = tmp_path / "test-agency"
        _write_json(slug_dir / "2026-03-27.json", {
            "slug": "test-agency", "archived_date": "2026-03-27",
            "hotlist_policy": "No policy",
        })
        _write_json(slug_dir / "2026-04-03.json", {
            "slug": "test-agency", "archived_date": "2026-04-03",
            "hotlist_policy": "Human verified",
        })
        result = diff_agency(slug_dir)
        assert any("hotlist_policy: No policy -> Human verified" in line for line in result)

    def test_list_added_and_removed(self, tmp_path):
        slug_dir = tmp_path / "test-agency"
        _write_json(slug_dir / "2026-03-27.json", {
            "slug": "test-agency", "archived_date": "2026-03-27",
            "shared_org_names": ["Agency A", "Agency B"],
        })
        _write_json(slug_dir / "2026-04-03.json", {
            "slug": "test-agency", "archived_date": "2026-04-03",
            "shared_org_names": ["Agency B", "Agency C"],
        })
        result = diff_agency(slug_dir)
        assert any("+1" in line and "shared_org_names" in line for line in result)
        assert any("-1" in line and "shared_org_names" in line for line in result)
        assert any("+ Agency C" in line for line in result)
        assert any("- Agency A" in line for line in result)

    def test_header_includes_dates(self, tmp_path):
        slug_dir = tmp_path / "test-agency"
        _write_json(slug_dir / "2026-03-27.json", {
            "slug": "test-agency", "archived_date": "2026-03-27", "camera_count": 10,
        })
        _write_json(slug_dir / "2026-04-03.json", {
            "slug": "test-agency", "archived_date": "2026-04-03", "camera_count": 20,
        })
        result = diff_agency(slug_dir)
        assert result[0] == "test-agency (2026-03-27 -> 2026-04-03):"

    def test_picks_two_most_recent(self, tmp_path):
        slug_dir = tmp_path / "test-agency"
        _write_json(slug_dir / "2026-01-01.json", {
            "slug": "test-agency", "archived_date": "2026-01-01", "camera_count": 5,
        })
        _write_json(slug_dir / "2026-03-27.json", {
            "slug": "test-agency", "archived_date": "2026-03-27", "camera_count": 10,
        })
        _write_json(slug_dir / "2026-04-03.json", {
            "slug": "test-agency", "archived_date": "2026-04-03", "camera_count": 15,
        })
        result = diff_agency(slug_dir)
        assert "2026-03-27 -> 2026-04-03" in result[0]
        assert any("10 -> 15" in line for line in result)

    def test_new_field_added(self, tmp_path):
        slug_dir = tmp_path / "test-agency"
        _write_json(slug_dir / "2026-03-27.json", {
            "slug": "test-agency", "archived_date": "2026-03-27",
        })
        _write_json(slug_dir / "2026-04-03.json", {
            "slug": "test-agency", "archived_date": "2026-04-03", "camera_count": 10,
        })
        result = diff_agency(slug_dir)
        assert any("camera_count" in line for line in result)

    def test_field_removed(self, tmp_path):
        slug_dir = tmp_path / "test-agency"
        _write_json(slug_dir / "2026-03-27.json", {
            "slug": "test-agency", "archived_date": "2026-03-27", "camera_count": 10,
        })
        _write_json(slug_dir / "2026-04-03.json", {
            "slug": "test-agency", "archived_date": "2026-04-03",
        })
        result = diff_agency(slug_dir)
        assert any("camera_count" in line for line in result)

    def test_long_string_shows_changed(self, tmp_path):
        slug_dir = tmp_path / "test-agency"
        long_old = "x" * 100
        long_new = "y" * 100
        _write_json(slug_dir / "2026-03-27.json", {
            "slug": "test-agency", "archived_date": "2026-03-27",
            "acceptable_use_policy": long_old,
        })
        _write_json(slug_dir / "2026-04-03.json", {
            "slug": "test-agency", "archived_date": "2026-04-03",
            "acceptable_use_policy": long_new,
        })
        result = diff_agency(slug_dir)
        assert any("(changed)" in line for line in result)

    def test_empty_dir(self, tmp_path):
        slug_dir = tmp_path / "empty-agency"
        slug_dir.mkdir()
        assert diff_agency(slug_dir) == []
