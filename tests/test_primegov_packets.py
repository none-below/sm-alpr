#!/usr/bin/env python3
"""
Tests for the PrimeGov meeting packet fetcher & OCR script.

Run with: uv run pytest tests/test_primegov_packets.py
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import primegov_packets


# ── URL construction ──


class TestURLConstruction:
    def test_primegov_api_default(self):
        assert primegov_packets.primegov_api("sanmateo") == \
            "https://sanmateo.primegov.com/api/v2/PublicPortal"

    def test_primegov_api_other_city(self):
        assert primegov_packets.primegov_api("fostercity") == \
            "https://fostercity.primegov.com/api/v2/PublicPortal"

    def test_primegov_packet_url(self):
        assert primegov_packets.primegov_packet_url("sanbruno") == \
            "https://sanbruno.primegov.com/Public/CompiledDocument"

    def test_primegov_api_arbitrary_subdomain(self):
        url = primegov_packets.primegov_api("losangeles")
        assert url == "https://losangeles.primegov.com/api/v2/PublicPortal"


# ── Data directory mapping ──


class TestDataDir:
    def test_san_mateo_preserves_existing_dir(self):
        assert primegov_packets.data_dir_for_city("sanmateo") == \
            Path("assets/san-mateo/council-packets")

    def test_foster_city(self):
        assert primegov_packets.data_dir_for_city("fostercity") == \
            Path("assets/foster-city/council-packets")

    def test_san_bruno(self):
        assert primegov_packets.data_dir_for_city("sanbruno") == \
            Path("assets/san-bruno/council-packets")

    def test_san_carlos(self):
        assert primegov_packets.data_dir_for_city("cityofsancarlos") == \
            Path("assets/city-of-san-carlos/council-packets")

    def test_redwood_city(self):
        assert primegov_packets.data_dir_for_city("redwoodcity") == \
            Path("assets/redwood-city/council-packets")

    def test_unknown_city_uses_subdomain_as_is(self):
        assert primegov_packets.data_dir_for_city("losangeles") == \
            Path("assets/losangeles/council-packets")

    def test_atherton_uses_subdomain(self):
        """Atherton is in KNOWN_CITIES but not DIR_NAMES — uses raw subdomain."""
        assert primegov_packets.data_dir_for_city("atherton") == \
            Path("assets/atherton/council-packets")


# ── safe_dirname ──


class TestSafeDirname:
    def test_standard_date(self):
        meeting = {"id": 1234, "date": "Aug 21, 2023"}
        assert primegov_packets.safe_dirname(meeting) == "1234_2023-08-21"

    def test_january_date(self):
        meeting = {"id": 99, "date": "Jan 01, 2025"}
        assert primegov_packets.safe_dirname(meeting) == "99_2025-01-01"

    def test_bad_date_falls_back(self):
        meeting = {"id": 5, "date": "not-a-date"}
        result = primegov_packets.safe_dirname(meeting)
        assert result.startswith("5_")
        # Should sanitize non-alphanumeric chars
        assert "/" not in result

    def test_missing_date(self):
        meeting = {"id": 7}
        result = primegov_packets.safe_dirname(meeting)
        assert result == "7_unknown"


# ── find_packet_template_id ──


class TestFindPacketTemplateId:
    def test_finds_packet(self):
        meeting = {
            "documentList": [
                {"templateName": "Agenda", "compileOutputType": 1, "templateId": 100},
                {"templateName": "Packet", "compileOutputType": 1, "templateId": 200},
            ]
        }
        assert primegov_packets.find_packet_template_id(meeting) == 200

    def test_ignores_non_pdf_output(self):
        meeting = {
            "documentList": [
                {"templateName": "Packet", "compileOutputType": 2, "templateId": 200},
            ]
        }
        assert primegov_packets.find_packet_template_id(meeting) is None

    def test_no_documents(self):
        assert primegov_packets.find_packet_template_id({}) is None

    def test_empty_document_list(self):
        assert primegov_packets.find_packet_template_id({"documentList": []}) is None


# ── CLI argument parsing ──


class TestCLIParsing:
    def _parse(self, args):
        """Parse CLI args and return the namespace."""
        import argparse
        # Replicate the parser from main() without calling main()
        parser = argparse.ArgumentParser()
        parser.add_argument("--city", default=primegov_packets.DEFAULT_CITY)
        parser.add_argument("--data-dir", type=Path, default=None)
        sub = parser.add_subparsers(dest="command", required=True)

        p_fetch = sub.add_parser("fetch")
        p_fetch.add_argument("--year", type=int, action="append", dest="years")
        p_fetch.add_argument("--meeting-id", type=int)
        p_fetch.add_argument("--council-only", action="store_true")
        p_fetch.add_argument("--force", action="store_true")
        p_fetch.add_argument("--delay", type=float, default=1)

        p_ocr = sub.add_parser("ocr")
        p_ocr.add_argument("--meeting-id", type=int)
        p_ocr.add_argument("--force", action="store_true")

        sub.add_parser("index")

        parsed = parser.parse_args(args)
        if parsed.data_dir is None:
            parsed.data_dir = primegov_packets.data_dir_for_city(parsed.city)
        return parsed

    def test_default_city(self):
        args = self._parse(["fetch"])
        assert args.city == "sanmateo"
        assert args.data_dir == Path("assets/san-mateo/council-packets")

    def test_custom_city(self):
        args = self._parse(["--city", "fostercity", "fetch"])
        assert args.city == "fostercity"
        assert args.data_dir == Path("assets/foster-city/council-packets")

    def test_explicit_data_dir_overrides_city(self):
        args = self._parse(["--city", "fostercity", "--data-dir", "/tmp/test", "fetch"])
        assert args.data_dir == Path("/tmp/test")

    def test_ocr_command(self):
        args = self._parse(["--city", "sanbruno", "ocr"])
        assert args.command == "ocr"
        assert args.data_dir == Path("assets/san-bruno/council-packets")

    def test_index_command(self):
        args = self._parse(["index"])
        assert args.command == "index"


# ── API functions (mocked HTTP) ──


class TestFetchMeetings:
    def _mock_response(self, data):
        resp = MagicMock()
        resp.read.return_value = json.dumps(data).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @patch("primegov_packets.urllib.request.urlopen")
    def test_fetch_meetings_for_year(self, mock_urlopen):
        meetings = [{"id": 1, "title": "City Council", "date": "Jan 15, 2025"}]
        mock_urlopen.return_value = self._mock_response(meetings)

        result = primegov_packets.fetch_meetings_for_year(2025, "fostercity")
        assert len(result) == 1
        assert result[0]["id"] == 1

        # Verify it called the right URL
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "fostercity.primegov.com" in req.full_url
        assert "year=2025" in req.full_url

    @patch("primegov_packets.urllib.request.urlopen")
    def test_fetch_meetings_handles_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection refused")
        result = primegov_packets.fetch_meetings_for_year(2025, "badcity")
        assert result == []

    @patch("primegov_packets.urllib.request.urlopen")
    def test_fetch_upcoming_meetings(self, mock_urlopen):
        meetings = [{"id": 99, "title": "Special Meeting"}]
        mock_urlopen.return_value = self._mock_response(meetings)

        result = primegov_packets.fetch_upcoming_meetings("sanbruno")
        assert len(result) == 1

        req = mock_urlopen.call_args[0][0]
        assert "sanbruno.primegov.com" in req.full_url
        assert "ListUpcomingMeetings" in req.full_url


# ── Known cities sanity ──


class TestKnownCities:
    def test_default_city_is_in_known(self):
        assert primegov_packets.DEFAULT_CITY in primegov_packets.KNOWN_CITIES

    def test_all_known_cities_have_labels(self):
        for city, label in primegov_packets.KNOWN_CITIES.items():
            assert len(label) > 0
            assert isinstance(label, str)

    def test_known_cities_not_empty(self):
        assert len(primegov_packets.KNOWN_CITIES) >= 5


# ── Script runs without import errors ──


class TestScriptImport:
    def test_help_exits_cleanly(self):
        result = subprocess.run(
            [sys.executable, "scripts/primegov_packets.py", "--help"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "--city" in result.stdout
        assert "sanmateo" in result.stdout

    def test_fetch_help(self):
        result = subprocess.run(
            [sys.executable, "scripts/primegov_packets.py", "fetch", "--help"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "--year" in result.stdout
