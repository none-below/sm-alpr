# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: zero below
"""The fetch chain descends from most browser-shaped to least, and stops early.

Anti-bot walls key on looking like an automated *browser*, so each tier sheds
browser signal rather than adding it: Playwright drives real Chrome with stealth
patches, curl_cffi replays Chrome's TLS/HTTP2 fingerprint without JS, and plain
requests is a bare GET. rwcpulse.com is the case that forced the third tier —
it 403s the first two and serves the third, so every one of its 15 URLs failed
a full crawl while `curl` fetched them by hand.

The invariants worth pinning, because a regression in any of them is silent
(you just get fewer articles, and the failure looks like the site's fault):

  - A fingerprint-shaped failure walks DOWN the chain until something returns a
    body, and the winning tier is recorded in fetch_path.
  - Tiers below the winner are never called. A 403 wall that clears at tier 2
    must not spend a third request.
  - Non-fingerprint failures (404, timeouts) burn no fallback at all.
  - When every tier fails, the error chains all three so the .meta.json shows
    what was tried — a bare "HTTP 403" can't distinguish "we tried once" from
    "we exhausted the chain".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import article_crawl as ac


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakePage:
    """Playwright Page stand-in. On a >=400 status, fetch_and_render returns
    right after goto() — content()/pdf() are never reached."""

    def __init__(self, status, body="<html>ok</html>"):
        self._status = status
        self._body = body
        self.url = "https://example.test/article"

    def goto(self, url, wait_until=None, timeout=None):
        if self._status is None:
            raise RuntimeError("net::ERR_HTTP2_PROTOCOL_ERROR")
        return _FakeResponse(self._status)

    def content(self):
        return self._body


class _FakeCtx:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    def new_context(self, **kw):
        return _FakeCtx(self._page)

    def close(self):
        pass


class _FakeChromium:
    def __init__(self, page):
        self._page = page

    def launch(self, **kw):
        return _FakeBrowser(self._page)


class _FakePlaywright:
    def __init__(self, page):
        self.chromium = _FakeChromium(page)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def chain(monkeypatch):
    """Wire a fake Playwright and record which fallback tiers get called."""

    calls = []

    def install(page_status, curl_result, requests_result):
        page = _FakePage(page_status)
        monkeypatch.setattr(ac, "_PLAYWRIGHT_AVAILABLE", True)
        monkeypatch.setattr(ac, "sync_playwright",
                            lambda: _FakePlaywright(page), raising=False)
        monkeypatch.setattr(ac, "stealth_sync", lambda p: None, raising=False)

        def fake_curl(url):
            calls.append("curl_cffi")
            return curl_result

        def fake_requests(url):
            calls.append("requests")
            return requests_result

        monkeypatch.setattr(ac, "_fetch_curl_cffi", fake_curl)
        monkeypatch.setattr(ac, "_fetch_requests", fake_requests)
        return calls

    return install


def _blocked(err="HTTP 403"):
    return {"fetch_error": err, "status": 403, "body": None,
            "final_url": None, "pdf_status": "skipped-curl-fallback",
            "pdf_byte_size": None, "pdf_error": None}


def _served(body="<html>from fallback</html>", pdf_status="skipped-curl-fallback"):
    return {"fetch_error": None, "status": 200, "body": body,
            "final_url": "https://example.test/article",
            "pdf_status": pdf_status, "pdf_byte_size": None, "pdf_error": None}


def test_403_that_clears_at_curl_cffi_does_not_reach_requests(chain):
    """Tier 2 wins, so tier 3 is never spent. The rwcpulse fix must not turn
    every 403 into three requests."""
    calls = chain(403, _served(), _served())
    out = ac.fetch_and_render("https://example.test/article", None, skip_pdf=True)

    assert calls == ["curl_cffi"], calls
    assert out["fetch_path"] == "curl_cffi"
    assert out["body"] == "<html>from fallback</html>"
    assert out["fetch_error"] is None


def test_403_through_both_browser_tiers_is_served_by_plain_requests(chain):
    """The rwcpulse case: both browser-shaped tiers 403, the bare GET succeeds,
    and its body is promoted with fetch_path='requests'."""
    calls = chain(403, _blocked(), _served("<html>rwcpulse</html>",
                                           "skipped-requests-fallback"))
    out = ac.fetch_and_render("https://example.test/article", None, skip_pdf=True)

    assert calls == ["curl_cffi", "requests"], calls
    assert out["fetch_path"] == "requests"
    assert out["body"] == "<html>rwcpulse</html>"
    assert out["status"] == 200
    assert out["fetch_error"] is None
    # No browser reached the page, so there is no PDF to claim.
    assert out["pdf_status"] == "skipped-requests-fallback"


def test_all_three_failing_chains_every_error(chain):
    """A .meta.json that only says "HTTP 403" can't distinguish one attempt
    from an exhausted chain, so the error names all three engines."""
    calls = chain(403, _blocked("HTTP 403"), _blocked("HTTP 403"))
    out = ac.fetch_and_render("https://example.test/article", None, skip_pdf=True)

    assert calls == ["curl_cffi", "requests"], calls
    assert out["body"] is None
    err = out["fetch_error"]
    assert "playwright:" in err and "curl_cffi:" in err and "requests:" in err, err


@pytest.mark.parametrize("status", [404, 429, 500])
def test_non_fingerprint_failures_burn_no_fallback(chain, status):
    """Only 403 and the HTTP/2 handshake error benefit from switching engines.
    A 404 is a 404 on every tier — spending two more requests on it is waste."""
    calls = chain(status, _served(), _served())
    out = ac.fetch_and_render("https://example.test/article", None, skip_pdf=True)

    assert calls == [], calls
    assert out["fetch_path"] == "playwright-chrome"
    assert out["fetch_error"] == f"HTTP {status}"


def test_success_on_playwright_never_touches_the_fallbacks(chain):
    """The common path stays one request."""
    calls = chain(200, _served(), _served())
    out = ac.fetch_and_render("https://example.test/article", None, skip_pdf=True)

    assert calls == [], calls
    assert out["fetch_path"] == "playwright-chrome"
    assert out["body"] == "<html>ok</html>"


def test_fetch_requests_reports_http_error_without_a_body(monkeypatch):
    """The tier-3 helper's own contract: a >=400 response is an error, not a
    body, so a served error page never gets curated as article text."""

    class _R:
        status_code = 403
        url = "https://example.test/article"
        text = "<html>go away</html>"

    monkeypatch.setattr(ac.requests, "get", lambda *a, **k: _R())
    out = ac._fetch_requests("https://example.test/article")

    assert out["body"] is None
    assert out["fetch_error"] == "HTTP 403"
    assert out["pdf_status"] == "skipped-requests-fallback"
