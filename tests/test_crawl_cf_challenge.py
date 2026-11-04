"""Cloudflare's managed challenge, and the stealth headers that broke it.

Since 2026-10-28 every crawl met a 403 with `cf-mitigated: challenge`. The
challenge clears by itself in about a second if the browser is left alone on
its page: its script sets cf_clearance and POSTs back, and the answer to that
POST is the portal. Two things stopped that. The crawler returned on the 403
instead of waiting, and tf-playwright-stealth forced a random fake header
profile onto every request, under which the challenge's POST died inside
Chrome. (About 40% of those profiles also carry an Accept-Encoding with "*",
which Chrome fails with net::ERR_INVALID_ARGUMENT before sending anything.)

So launch_browser clears the stealth headers, and archive_agency waits the
challenge out without touching the page. A human check (Turnstile) is never
attempted: the run stops there.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import flock_transparency as ft
import slug_probe


class _Response:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}


CHALLENGE = _Response(403, {"cf-mitigated": "challenge", "server": "cloudflare"})


class _Frame:
    def __init__(self, url):
        self.url = url


class _ChallengePage:
    """A page sitting on Cloudflare's challenge. `landings` scripts what the
    document's navigation entry reports on each poll: 403 while the challenge
    page is up, then the status of the page it lands on. An Exception entry
    stands for a poll that hits a navigation in progress."""

    def __init__(self, landings=(), frames=(), body=""):
        self._landings = list(landings)
        self.frames = [_Frame("https://transparency.flocksafety.com/x")] + [
            _Frame(u) for u in frames]
        self._body = body
        self.polls = 0
        self.navigations = 0

    def goto(self, url, wait_until=None, timeout=None):
        self.navigations += 1
        return CHALLENGE

    def wait_for_timeout(self, ms):
        self.polls += 1

    def evaluate(self, script):
        nxt = self._landings.pop(0) if self._landings else 403
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def inner_text(self, selector):
        return self._body


def test_is_cloudflare_challenge_needs_the_header():
    assert ft.is_cloudflare_challenge(CHALLENGE)
    assert not ft.is_cloudflare_challenge(_Response(403))
    assert not ft.is_cloudflare_challenge(_Response(200, {"cf-mitigated": "challenge"}))
    assert not ft.is_cloudflare_challenge(None)


def test_wait_returns_the_status_the_challenge_lands_on():
    page = _ChallengePage(landings=[403, RuntimeError("mid-navigation"), 0, 200])
    assert ft.wait_out_challenge(page) == 200
    assert page.navigations == 0, "waiting must never reload"


def test_wait_reports_a_human_check_instead_of_attempting_it():
    """A Turnstile frame that never clears is a human check, reported only
    once the wait runs out, never acted on."""
    page = _ChallengePage(frames=["https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile"])
    assert ft.wait_out_challenge(page, timeout_ms=1000) == "interactive"
    assert page.polls == 1000 // ft.CHALLENGE_POLL_MS
    assert page.navigations == 0, "waiting must never reload"


def test_wait_lets_turnstile_clear_by_itself(capsys):
    """Turnstile can finish without a click: the frame comes up and the page
    then lands on the portal. That is a pass, not a human check."""
    page = _ChallengePage(landings=[403, 403, 403, 200],
                          frames=["https://challenges.cloudflare.com/x"])
    assert ft.wait_out_challenge(page) == 200
    assert page.navigations == 0
    out = capsys.readouterr().out
    assert "Turnstile appeared after 0.2s" in out
    assert "Turnstile cleared by itself after 1.0s" in out


def test_wait_gives_up_after_the_timeout():
    page = _ChallengePage()
    assert ft.wait_out_challenge(page, timeout_ms=1000) is None
    assert page.polls == 1000 // ft.CHALLENGE_POLL_MS


def test_archive_agency_human_check_is_challenged(tmp_path):
    page = _ChallengePage(frames=["https://challenges.cloudflare.com/x"])
    assert ft.archive_agency(page, "anytown-ca-pd", tmp_path) == ("challenged", [])


def test_archive_agency_unfinished_challenge_is_forbidden(tmp_path, monkeypatch):
    monkeypatch.setattr(ft, "CHALLENGE_WAIT_MS", 1000)
    assert ft.archive_agency(_ChallengePage(), "anytown-ca-pd", tmp_path) == ("forbidden", [])


def test_archive_agency_judges_the_page_the_challenge_lands_on(tmp_path):
    """A slug that doesn't exist answers 404 behind the challenge. That's a
    real answer about the slug, not a block."""
    page = _ChallengePage(landings=[404])
    assert ft.archive_agency(page, "nowhere-ca-pd", tmp_path) == (("failed", "http_404"), [])


def test_human_check_ends_the_run_without_the_warmup_retry(tmp_path, monkeypatch):
    attempted = []

    def challenged(page, slug, data_dir, force=False, hashes=None, progress=""):
        attempted.append(slug)
        return "challenged", []

    monkeypatch.setattr(ft, "archive_agency", challenged)
    monkeypatch.setattr(ft.time, "sleep", lambda *_: None)
    failed = {}
    results, _, stop = ft.run_crawl_batch(
        MagicMock(), ["one-ca-pd", "two-ca-pd"], tmp_path,
        force=False, delay=0, hashes={}, failed_slugs=failed,
    )

    assert attempted == ["one-ca-pd"], attempted
    assert stop is True
    assert results == [("one-ca-pd", None)]
    assert failed == {}


def test_probe_waits_out_the_challenge(monkeypatch):
    page = _ChallengePage(landings=[200], body="Provided by Flock Safety")
    monkeypatch.setattr(slug_probe, "PORTAL_MARKERS", ["Provided by Flock Safety"])
    assert slug_probe.probe(page, "anytown-ca-pd") == ("hit", "http_200")


def test_probe_human_check_is_challenged():
    page = _ChallengePage(frames=["https://challenges.cloudflare.com/x"])
    assert slug_probe.probe(page, "anytown-ca-pd") == ("challenged", "cf_interactive")


class _Page:
    def __init__(self):
        self.extra_headers = []

    def set_extra_http_headers(self, headers):
        self.extra_headers.append(dict(headers))


class _Browser:
    version = "156.0.8078.74"

    def __init__(self):
        self.page = _Page()

    def new_context(self, **kw):
        ctx = MagicMock()
        ctx.new_page.return_value = self.page
        return ctx


@pytest.mark.parametrize("stealth_raises", [False, True])
def test_launch_browser_clears_the_stealth_header_profile(monkeypatch, stealth_raises):
    def fake_stealth(page):
        # What tf-playwright-stealth 1.2.0 does before its JS patches.
        page.set_extra_http_headers({"accept-encoding": "gzip,*", "referer": "http://wink.com"})
        if stealth_raises:
            raise RuntimeError("init script failed")

    monkeypatch.setattr(ft, "stealth_sync", fake_stealth)
    p = MagicMock()
    p.chromium.launch.return_value = _Browser()

    _, page = ft.launch_browser(p)

    assert page.extra_headers[-1] == {}, page.extra_headers
