"""A 403 from Flock's edge is two different things, and position tells them apart.

The first request from a cold datacenter IP is challenged, and one retry on the
same session clears it. Every 403 after that is the rate limit — Flock allows
roughly 3 requests/hour/IP and puts an over-budget IP in a cooldown that lasts
hours, which no retry can clear. So the crawler spends exactly ONE warm-up retry
per session on the handshake, and ends the run on the next 403: the slugs behind
it keep their budget for the next scheduled run, which gets a fresh runner IP.

What this replaced: three retries on every 403, of every slug. Measured on the
30-min rotation, that spent ~10 requests per run to capture exactly 1 — the
retries on slug #1 pushed slugs #2 and #3 past the cliff, where their four
requests each could only fail. Nothing rate-limited is ever quarantined.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import flock_transparency as ft


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakePage:
    """Minimal Playwright Page stand-in. The 403/429 paths in archive_agency
    return right after goto(), before any inner_text/content/CDP work."""

    def __init__(self, status):
        self._status = status

    def goto(self, url, wait_until=None, timeout=None):
        return _FakeResponse(self._status)

    def wait_for_timeout(self, ms):
        pass


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(ft.time, "sleep", lambda *_: None)


@pytest.mark.parametrize("status,expected", [(403, "forbidden"), (429, "rate_limited")])
def test_archive_agency_rate_limit_sentinels(tmp_path, status, expected):
    """Both codes map to their transient sentinel, not a ("failed", "http_403")
    tuple that would flow toward quarantine."""
    got, discovered = ft.archive_agency(_FakePage(status), "anytown-ca-pd", tmp_path)
    assert got == expected, got
    assert discovered == []


@pytest.mark.parametrize("sentinel", ["forbidden", "rate_limited"])
def test_one_warmup_retry_then_the_run_ends(tmp_path, monkeypatch, sentinel):
    """A 403 that outlives the warm-up is the rate limit: stop the run rather
    than spend the remaining slugs' budget on requests that cannot succeed.

    Two requests total (the attempt plus the one warm-up), not the twelve the
    retry-everything crawler made for the same three slugs.
    """
    attempted = []

    def limited(page, slug, data_dir, force=False, hashes=None, progress=""):
        attempted.append(slug)
        return sentinel, []

    monkeypatch.setattr(ft, "archive_agency", limited)

    failed = {}
    results, _, rate_limited = ft.run_crawl_batch(
        MagicMock(), ["one-ca-pd", "two-ca-pd", "three-ca-pd"], tmp_path,
        force=False, delay=0, hashes={}, failed_slugs=failed,
    )

    assert attempted == ["one-ca-pd", "one-ca-pd"], attempted
    assert rate_limited is True
    assert results == [("one-ca-pd", None)], results
    assert failed == {}, "a rate limit must never quarantine a slug"


def test_cold_ip_handshake_clears_on_the_warmup(tmp_path, monkeypatch):
    """The observed production shape: slug #1 403s, the warm-up retry clears
    it, slug #2 then 403s for real. Yield is the same one capture the old
    crawler got — at three requests instead of ten."""
    attempted = []

    def handshake_then_limit(page, slug, data_dir, force=False, hashes=None, progress=""):
        attempted.append(slug)
        if len(attempted) == 1:
            return "forbidden", []       # cold-IP handshake
        if len(attempted) == 2:
            return "unchanged", []       # warmed retry clears
        return "forbidden", []           # budget now spent

    monkeypatch.setattr(ft, "archive_agency", handshake_then_limit)

    results, _, rate_limited = ft.run_crawl_batch(
        MagicMock(), ["one-ca-pd", "two-ca-pd", "three-ca-pd"], tmp_path,
        force=False, delay=0, hashes={}, failed_slugs={},
    )

    assert attempted == ["one-ca-pd", "one-ca-pd", "two-ca-pd"], attempted
    assert rate_limited is True
    assert results == [("one-ca-pd", "unchanged"), ("two-ca-pd", None)], results


def test_warmup_is_once_per_session_not_once_per_batch(tmp_path, monkeypatch):
    """cmd_crawl calls run_crawl_batch once per BFS level against the same
    page. The handshake happens once for that session, so a later level must
    not get a second warm-up — by then a 403 is unambiguously the rate limit."""
    attempted = []

    def clears_then_limited(page, slug, data_dir, force=False, hashes=None, progress=""):
        attempted.append(slug)
        if len(attempted) == 1:
            return "forbidden", []
        if len(attempted) == 2:
            return "unchanged", []
        return "forbidden", []

    monkeypatch.setattr(ft, "archive_agency", clears_then_limited)
    session = ft.new_crawl_session()
    common = dict(force=False, delay=0, hashes={}, failed_slugs={}, session=session)

    ft.run_crawl_batch(MagicMock(), ["level0-ca-pd"], tmp_path, **common)
    _, _, rate_limited = ft.run_crawl_batch(MagicMock(), ["level1-ca-pd"], tmp_path, **common)

    assert attempted == ["level0-ca-pd", "level0-ca-pd", "level1-ca-pd"], attempted
    assert rate_limited is True


def test_rate_limited_slug_records_an_attempt(tmp_path, monkeypatch):
    """The slug actually requested must stamp .attempts.json so the
    stalest-first rotation advances past it — otherwise the same slug is
    re-picked every run and the other ~250 agencies never get a turn. The
    untouched remainder must NOT be stamped: they were never requested."""
    monkeypatch.setattr(ft, "archive_agency", lambda *a, **k: ("forbidden", []))

    ft.run_crawl_batch(
        MagicMock(), ["one-ca-pd", "two-ca-pd"], tmp_path,
        force=False, delay=0, hashes={}, failed_slugs={},
    )

    attempts = ft.load_json(tmp_path / ft.ATTEMPT_FILE)
    assert "one-ca-pd" in attempts, attempts
    assert "two-ca-pd" not in attempts, attempts


def test_variation_probe_does_not_read_a_403_as_a_working_slug(tmp_path, monkeypatch):
    """--try-variations walks alternate spellings after a 404. A 403 mid-walk
    is a rate limit, not evidence the variation exists, so it must not be
    adopted as the slug — doing so would rename the agency in the rotation on
    the strength of a request that never reached a portal."""
    seen = []

    def four_oh_four_then_limited(page, slug, data_dir, force=False, hashes=None, progress=""):
        seen.append(slug)
        if len(seen) == 1:
            return ("failed", "http_404"), []
        return "forbidden", []

    monkeypatch.setattr(ft, "archive_agency", four_oh_four_then_limited)

    results, _, rate_limited = ft.run_crawl_batch(
        MagicMock(), ["anytown-ca-pd"], tmp_path,
        force=False, delay=0, hashes={}, failed_slugs={},
        try_variations=True,
    )

    assert rate_limited is True
    assert results == [("anytown-ca-pd", None)], results
    # original 404, one variation, then the warm-up against the ORIGINAL slug
    assert seen == ["anytown-ca-pd", "anytown-pd", "anytown-ca-pd"], seen
