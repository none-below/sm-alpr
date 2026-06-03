#!/usr/bin/env python3
"""Tests for the ALPR/Flock meeting-agenda watcher (scripts/meeting_watch.py).

Covers the pure logic — keyword matching (incl. the context gating that stops
known false positives), the Granicus underscore-bucket S3 rewrite, and the
date/id parsers. No network.

Run with: uv run pytest tests/test_meeting_watch.py
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import meeting_watch as mw


# ── keyword matching (context-gated) ──


class TestFindMatches:
    def test_clear_alpr_contract_flags_strong_terms(self):
        text = ("Authorizing the Mayor to Execute an Agreement with Flock Safety, "
                "LLC for the Procurement of Eight Additional Automatic License "
                "Plate Readers (ALPRs) for the City")
        terms = {m["term"] for m in mw.find_matches(text)}
        assert "flock safety" in terms
        assert "license plate reader" in terms
        assert "alpr" in terms                       # matches 'ALPRs'
        assert "automated license plate" in terms    # matches 'Automatic License Plate'

    def test_spelled_out_without_acronym(self):
        # Belmont PSC item — names the tech in full, no 'ALPR' acronym.
        text = "Automated License Plate Readers and Unarmed Aerial Systems Update"
        terms = {m["term"] for m in mw.find_matches(text)}
        assert "license plate reader" in terms
        assert "automated license plate" in terms

    def test_little_flock_church_not_flagged(self):
        # Bare 'Flock' as a proper noun, no surveillance context -> must not flag.
        text = ("Ratification of a resolution honoring and commending Little Flock "
                "Church of God in Christ on its 70th Anniversary.")
        assert mw.find_matches(text) == []

    def test_library_lpr_room_not_flagged(self):
        # 'LPR' as a library room code, no surveillance context -> must not flag.
        text = "Discussion and Motion to Approve the LPR Mother's & Lactation Room Policy"
        assert mw.find_matches(text) == []

    def test_routine_business_not_flagged(self):
        assert mw.find_matches("Roll call, approval of minutes, and consent calendar") == []

    def test_lpr_with_camera_context_flags(self):
        terms = {m["term"] for m in mw.find_matches(
            "Award contract for LPR camera installation at three intersections")}
        assert "lpr" in terms

    def test_flock_with_surveillance_context_flags(self):
        terms = {m["term"] for m in mw.find_matches(
            "Receive an update on the Flock surveillance camera program")}
        assert "flock" in terms

    def test_match_carries_a_snippet(self):
        text = "x" * 200 + " Flock Safety automated license plate readers " + "y" * 200
        matches = mw.find_matches(text)
        assert matches
        assert matches[0]["snippet"]
        assert len(matches[0]["snippet"]) < 300


# ── Granicus underscore-bucket S3 redirect rewrite (the requests/urllib3 bug) ──


class TestDeunderscoreS3:
    def test_rewrites_underscore_bucket_to_path_style(self):
        url = "https://granicus_production_attachments.s3.amazonaws.com/belmont-ca/abc.pdf"
        assert mw._deunderscore_s3(url) == (
            "https://s3.amazonaws.com/granicus_production_attachments/belmont-ca/abc.pdf")

    def test_rewrites_regional_underscore_bucket(self):
        url = "https://granicus_prod.s3.us-west-2.amazonaws.com/x/y.pdf"
        assert mw._deunderscore_s3(url) == "https://s3.amazonaws.com/granicus_prod/x/y.pdf"

    def test_leaves_granicus_viewer_url_untouched(self):
        url = "https://belmont-ca.granicus.com/AgendaViewer.php?view_id=1&clip_id=1139"
        assert mw._deunderscore_s3(url) == url

    def test_leaves_valid_bucket_without_underscore_untouched(self):
        url = "https://my-valid-bucket.s3.amazonaws.com/x.pdf"
        assert mw._deunderscore_s3(url) == url


# ── id + date parsing ──


class TestGranicusId:
    def test_clip_id(self):
        assert mw._granicus_id(
            "https://x/AgendaViewer.php?view_id=1&clip_id=1139", date(2025, 2, 24)) == "1139"

    def test_event_id_for_upcoming(self):
        assert mw._granicus_id(
            "https://x/AgendaViewer.php?view_id=1&event_id=1788", date(2026, 6, 2)) == "1788"

    def test_falls_back_to_date(self):
        assert mw._granicus_id("https://x/no-id-here", date(2025, 2, 24)) == "2025-02-24"


class TestDateParsing:
    def test_legistar_date_plus_time(self):
        dt = mw.parse_legistar_dt("2026-05-27T00:00:00", "7:00 PM")
        assert dt.date() == date(2026, 5, 27)
        assert dt.hour == 19

    def test_legistar_date_only(self):
        dt = mw.parse_legistar_dt("2026-05-27T00:00:00", None)
        assert dt.date() == date(2026, 5, 27)

    def test_legistar_bad_date_returns_none(self):
        assert mw.parse_legistar_dt("", "7:00 PM") is None

    def test_naive_pacific_datetime(self):
        dt = mw.parse_naive_pacific("2026-06-01T19:00:00")
        assert dt.date() == date(2026, 6, 1)
        assert dt.hour == 19

    def test_naive_pacific_bad_input(self):
        assert mw.parse_naive_pacific("") is None


def test_strip_html_drops_tags_and_unescapes():
    out = mw.strip_html("<p>Flock&nbsp;Safety</p><br><script>x</script>")
    assert "Flock" in out and "Safety" in out
    assert "<" not in out and "script" not in out
