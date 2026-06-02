"""A Cloudflare 403 is a cold-IP edge bot-challenge, not a block.

Flock's edge challenges the FIRST request from a cold (datacenter) IP with a
403, then waves it through once the IP is warm. So a lone-agency dispatch
(e.g. crawling only `san-mateo-ca-pd`) is forever the cold first request and
403s every time, while multi-agency runs sacrifice agency #1 and ingest the
rest. The fix: archive_agency reports 403 as the transient "forbidden" sentinel,
and run_crawl_batch retries it in-session (the warmed retry clears) without
ever quarantining the slug.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import flock_transparency as ft


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakePage:
    """Minimal Playwright Page stand-in. The 403 path in archive_agency
    returns right after goto(), before any inner_text/content/CDP work."""

    def __init__(self, status):
        self._status = status

    def goto(self, url, wait_until=None, timeout=None):
        return _FakeResponse(self._status)

    def wait_for_timeout(self, ms):
        pass


def test_archive_agency_403_is_forbidden_not_a_failure(tmp_path):
    """A 403 must map to the transient "forbidden" sentinel, not a
    ("failed", "http_403") tuple that would flow toward quarantine."""
    status, discovered = ft.archive_agency(_FakePage(403), "anytown-ca-pd", tmp_path)
    assert status == "forbidden", status
    assert discovered == []


def test_run_crawl_batch_retries_forbidden_then_succeeds(tmp_path, monkeypatch):
    """The cold first request 403s; the warmed in-session retry clears. The
    slug ends up captured, and is never added to failed_slugs."""
    monkeypatch.setattr(ft.time, "sleep", lambda *_: None)  # no real backoff wait

    calls = {"n": 0}

    def fake_archive(page, slug, data_dir, force=False, hashes=None, progress=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return "forbidden", []   # cold first request
        return "unchanged", []       # warmed retry clears

    monkeypatch.setattr(ft, "archive_agency", fake_archive)

    failed = {}
    results, _ = ft.run_crawl_batch(
        MagicMock(), ["san-mateo-ca-pd"], tmp_path,
        force=False, delay=0, hashes={}, failed_slugs=failed,
    )

    assert calls["n"] == 2, "should have retried exactly once after the cold 403"
    assert results == [("san-mateo-ca-pd", "unchanged")], results
    assert "san-mateo-ca-pd" not in failed, "a recovered 403 must not be quarantined"


def test_run_crawl_batch_forbidden_exhausted_is_not_quarantined(tmp_path, monkeypatch):
    """If every retry still 403s, skip for this run but DON'T quarantine —
    the slug must regenerate next run, not be permanently excluded."""
    monkeypatch.setattr(ft.time, "sleep", lambda *_: None)

    calls = {"n": 0}

    def always_forbidden(page, slug, data_dir, force=False, hashes=None, progress=""):
        calls["n"] += 1
        return "forbidden", []

    monkeypatch.setattr(ft, "archive_agency", always_forbidden)

    failed = {}
    results, _ = ft.run_crawl_batch(
        MagicMock(), ["san-mateo-ca-pd"], tmp_path,
        force=False, delay=0, hashes={}, failed_slugs=failed,
    )

    # initial attempt + FORBIDDEN_RETRIES in-session retries
    assert calls["n"] == 1 + ft.FORBIDDEN_RETRIES, calls["n"]
    assert results == [("san-mateo-ca-pd", None)], results
    assert "san-mateo-ca-pd" not in failed, "exhausted 403 must not be quarantined"
