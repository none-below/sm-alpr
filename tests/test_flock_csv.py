#!/usr/bin/env python3
"""Tests for CSV extraction from Flock transparency HTML."""

import importlib.util
import urllib.parse
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "flock_transparency",
    Path(__file__).parent.parent / "scripts" / "flock_transparency.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
extract_csvs_from_html = _mod.extract_csvs_from_html


def _make_csv_link(filename, csv_text):
    """Build an <a> tag with an embedded data-URI CSV."""
    encoded = urllib.parse.quote(csv_text, safe="")
    return (
        f'<a download="{filename}" '
        f'href="data:text/csv;charset=utf-8,{encoded}" '
        f'target="_blank" rel="noreferrer noopener">Download</a>'
    )


def _wrap_html(body):
    return f"<html><body>{body}</body></html>"


class TestExtractCsvs:
    def test_no_csv_links(self):
        html = _wrap_html("<p>No CSV here</p>")
        assert extract_csvs_from_html(html) == []

    def test_single_csv(self):
        csv_text = '"id","userId","searchDate"\n"abc","***","2026-04-01T00:00:00Z"\n'
        html = _wrap_html(_make_csv_link("search_audit.csv", csv_text))
        results = extract_csvs_from_html(html)
        assert len(results) == 1
        filename, rows = results[0]
        assert filename == "search_audit.csv"
        assert len(rows) == 1
        assert rows[0]["id"] == "abc"
        assert rows[0]["userId"] == "***"
        assert rows[0]["searchDate"] == "2026-04-01T00:00:00Z"

    def test_csv_with_case_number_schema(self):
        csv_text = (
            '"id","userId","searchDate","networkCount","caseNumber","offenseType"\n'
            '"row1","***","2026-04-01T00:00:00Z","5","CN-001","Theft"\n'
            '"row2","***","2026-04-02T00:00:00Z","10","","Assault/Battery Offenses"\n'
        )
        html = _wrap_html(_make_csv_link("search_audit.csv", csv_text))
        _, rows = extract_csvs_from_html(html)[0]
        assert len(rows) == 2
        assert rows[0]["caseNumber"] == "CN-001"
        assert rows[0]["offenseType"] == "Theft"
        assert rows[1]["caseNumber"] == ""

    def test_csv_with_reason_schema(self):
        csv_text = (
            '"id","userId","searchDate","networkCount","reason"\n'
            '"row1","***","2026-04-01T00:00:00Z","3","Stolen Vehicle"\n'
        )
        html = _wrap_html(_make_csv_link("search_audit.csv", csv_text))
        _, rows = extract_csvs_from_html(html)[0]
        assert rows[0]["reason"] == "Stolen Vehicle"
        assert "caseNumber" not in rows[0]

    def test_empty_csv_header_only(self):
        csv_text = '"id","userId","searchDate"\n'
        html = _wrap_html(_make_csv_link("search_audit.csv", csv_text))
        _, rows = extract_csvs_from_html(html)[0]
        assert rows == []

    def test_non_csv_link_ignored(self):
        html = _wrap_html(
            '<a download="report.pdf" href="data:application/pdf;base64,abc">PDF</a>'
        )
        assert extract_csvs_from_html(html) == []

    def test_csv_surrounded_by_other_content(self):
        csv_text = '"col1"\n"val1"\n'
        html = _wrap_html(
            "<h2>Download CSV</h2>"
            "<p>Some description</p>"
            + _make_csv_link("search_audit.csv", csv_text)
            + "<h2>Additional Info</h2>"
            "<p>More stuff</p>"
        )
        results = extract_csvs_from_html(html)
        assert len(results) == 1
        assert results[0][1][0]["col1"] == "val1"

    def test_special_characters_in_csv(self):
        csv_text = (
            '"id","offenseType"\n'
            '"1","Assault/Battery (Domestic)"\n'
            '"2","Wanted Person — Arrest Warrant"\n'
        )
        html = _wrap_html(_make_csv_link("search_audit.csv", csv_text))
        _, rows = extract_csvs_from_html(html)[0]
        assert rows[0]["offenseType"] == "Assault/Battery (Domestic)"
        assert rows[1]["offenseType"] == "Wanted Person — Arrest Warrant"

    def test_field_name_derivation(self):
        """The caller derives the JSON field from the filename; verify the
        filename comes through correctly for both known names."""
        for name in ("search_audit.csv", "other-report.csv"):
            csv_text = '"a"\n"1"\n'
            html = _wrap_html(_make_csv_link(name, csv_text))
            filename, _ = extract_csvs_from_html(html)[0]
            assert filename == name
