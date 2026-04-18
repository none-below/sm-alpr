#!/usr/bin/env python3
"""
Tests for the shared meeting-banner module (docs/js/meeting_banners.js).

Validates both the banner data shape (field types, date formats) and the
integration points on pages that consume it.
"""

import http.server
import json
import re
import threading
from datetime import date, datetime
from pathlib import Path

import pytest

DOCS_DIR = Path("docs")
BANNER_JS = DOCS_DIR / "js" / "meeting_banners.js"


def _parse_banners():
    """Extract MEETING_BANNERS from the JS source into a Python list.

    We purposely parse the literal JS object rather than shelling out to a
    JS runtime — the data is a pure literal and keeps the test dependency-
    free. If this ever grows dynamic logic, switch to playwright-based
    evaluation (see `TestBrowser` below).
    """
    src = BANNER_JS.read_text()
    m = re.search(r"const MEETING_BANNERS\s*=\s*(\[.*?\n  \]);", src, re.DOTALL)
    assert m, "Could not find MEETING_BANNERS array in meeting_banners.js"
    body = m.group(1)
    # Strip line comments (`// ...`) but not `://` in URLs. JS block
    # comments (`/* ... */`) aren't used in the banner data currently.
    body = re.sub(r"(?<!:)//[^\n]*", "", body)
    # Strip trailing commas.
    body = re.sub(r",(\s*[\]}])", r"\1", body)
    # Quote unquoted keys (match/meeting/when/expires/links/label/url).
    body = re.sub(r"([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:", r'\1"\2":', body)
    return json.loads(body)


class TestBannerData:
    """Validate the shape and contents of MEETING_BANNERS entries."""

    @pytest.fixture(autouse=True)
    def load(self):
        assert BANNER_JS.exists(), f"{BANNER_JS} missing"
        self.banners = _parse_banners()

    def test_is_list(self):
        assert isinstance(self.banners, list)

    def test_entries_have_required_fields(self):
        for i, b in enumerate(self.banners):
            for field in ("match", "meeting", "when", "expires", "links"):
                assert field in b, f"banner[{i}] missing {field}: {b}"

    def test_match_is_nonempty_string_list(self):
        for i, b in enumerate(self.banners):
            assert isinstance(b["match"], list) and b["match"], f"banner[{i}] match must be non-empty list"
            for m in b["match"]:
                assert isinstance(m, str) and m, f"banner[{i}] match entries must be non-empty strings"

    def test_expires_is_iso_date(self):
        for i, b in enumerate(self.banners):
            try:
                datetime.strptime(b["expires"], "%Y-%m-%d")
            except ValueError:
                pytest.fail(f"banner[{i}] expires {b['expires']!r} is not YYYY-MM-DD")

    def test_links_have_label_and_url(self):
        for i, b in enumerate(self.banners):
            assert isinstance(b["links"], list) and b["links"], f"banner[{i}] needs at least one link"
            for j, lnk in enumerate(b["links"]):
                assert lnk.get("label"), f"banner[{i}].links[{j}] missing label"
                url = lnk.get("url", "")
                assert url.startswith(("http://", "https://")), (
                    f"banner[{i}].links[{j}] url must be absolute http(s): {url!r}"
                )

    def test_matches_registry_identifiers(self):
        """Each banner should match a real registry entry by slug/agency_id/name.

        Catches typos in identifiers before they ship as dead banners.
        """
        reg = json.loads(Path("assets/agency_registry.json").read_text())
        known = set()
        for e in reg:
            if e.get("slug"):
                known.add(e["slug"])
            if e.get("agency_id"):
                known.add(e["agency_id"])
            for s in e.get("flock_slugs", []):
                known.add(s)
            for n in e.get("flock_names", []):
                known.add(n)
            if e.get("display_name"):
                known.add(e["display_name"])
        for i, b in enumerate(self.banners):
            hits = [m for m in b["match"] if m in known]
            assert hits, (
                f"banner[{i}] ({b['meeting']}) has no identifiers matching the registry: "
                f"{b['match']}"
            )


class TestIntegration:
    """Verify pages that use banners include the shared script."""

    def test_report_html_includes_banner_script(self):
        html = (DOCS_DIR / "report.html").read_text()
        assert "js/meeting_banners.js" in html, "report.html must include meeting_banners.js"

    def test_sharing_map_includes_banner_script(self):
        html = (DOCS_DIR / "sharing_map.html").read_text()
        assert "js/meeting_banners.js" in html, "sharing_map.html must include meeting_banners.js"

    def test_report_js_delegates_to_shared_helper(self):
        js = (DOCS_DIR / "js" / "report.js").read_text()
        assert "window.renderMeetingBannerHtml" in js, (
            "report.js should call the shared window.renderMeetingBannerHtml helper"
        )

    def test_map_js_delegates_to_shared_helper(self):
        js = (DOCS_DIR / "js" / "map.js").read_text()
        assert "renderMeetingBannerHtml" in js, (
            "map.js should call the shared renderMeetingBannerHtml helper"
        )


def _serve_docs():
    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    server.timeout = 0.5
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


class TestBrowser:
    """End-to-end: load the banner module in a real browser and exercise
    activeMeetingBanner / renderMeetingBannerHtml with fixed 'today' dates.

    Skips cleanly if playwright isn't installed (keeps the non-browser
    tests useful on minimal CI).
    """

    @pytest.fixture(autouse=True, scope="class")
    def serve(self, request):
        import os
        os.chdir(DOCS_DIR)
        server, port = _serve_docs()
        request.cls.port = port
        yield
        server.shutdown()
        os.chdir(Path(__file__).parent.parent)

    @pytest.fixture(scope="class")
    def browser(self):
        pytest.importorskip("playwright.sync_api")
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        b = pw.chromium.launch()
        yield b
        b.close()
        pw.stop()

    def _load(self, browser, today_iso):
        """Load a minimal page that imports meeting_banners.js with Date frozen."""
        page = browser.new_page()
        # Freeze Date so expiration logic is deterministic. Override before
        # the module loads so `new Date()` inside it sees the frozen clock.
        page.add_init_script(f"""
          (function() {{
            const fixed = new Date('{today_iso}T12:00:00Z');
            const OrigDate = Date;
            function MockDate(...args) {{
              if (args.length === 0) return new OrigDate(fixed);
              return new OrigDate(...args);
            }}
            MockDate.prototype = OrigDate.prototype;
            MockDate.now = () => fixed.getTime();
            MockDate.parse = OrigDate.parse;
            MockDate.UTC = OrigDate.UTC;
            window.Date = MockDate;
          }})();
        """)
        page.goto(f"http://127.0.0.1:{self.port}/report.html?agency=_banner_test",
                  wait_until="domcontentloaded")
        # Wait until the banner helper is defined.
        page.wait_for_function("typeof window.renderMeetingBannerHtml === 'function'")
        return page

    def test_sunnyvale_banner_shows_before_expiry(self, browser):
        page = self._load(browser, "2026-04-20")
        html = page.evaluate("window.renderMeetingBannerHtml(['sunnyvale-ca-pd'])")
        assert "Sunnyvale City Council" in html
        assert "no-flock-in-sunnyvale" in html
        page.close()

    def test_banner_hides_after_expiry(self, browser):
        page = self._load(browser, "2026-04-22")
        html = page.evaluate("window.renderMeetingBannerHtml(['sunnyvale-ca-pd'])")
        assert html == ""
        page.close()

    def test_banner_matches_by_agency_id(self, browser):
        page = self._load(browser, "2026-04-20")
        html = page.evaluate(
            "window.renderMeetingBannerHtml(['7ef62d29-d50a-5f12-85fe-6061de259c8d'])"
        )
        assert "Sunnyvale" in html
        page.close()

    def test_banner_matches_by_display_name(self, browser):
        page = self._load(browser, "2026-04-20")
        html = page.evaluate("window.renderMeetingBannerHtml(['El Cerrito CA PD'])")
        assert "El Cerrito" in html
        page.close()

    def test_unknown_identifiers_return_empty(self, browser):
        page = self._load(browser, "2026-04-20")
        html = page.evaluate("window.renderMeetingBannerHtml(['not-a-real-slug'])")
        assert html == ""
        page.close()

    def test_null_and_empty_idents_are_tolerated(self, browser):
        page = self._load(browser, "2026-04-20")
        html = page.evaluate("window.renderMeetingBannerHtml([null, '', undefined])")
        assert html == ""
        page.close()
