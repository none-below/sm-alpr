#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Smoke tests for the backlog dashboard.

Data-shape only (no browser): asserts build_dashboard.py emits the schema
docs/js/dashboard.js consumes, and that dashboard.html carries the CSP +
analytics + shared-asset wiring every docs page needs.
"""
import json
from pathlib import Path

import pytest

DOCS = Path("docs")
DASH = DOCS / "data" / "dashboard.json"
HTML = DOCS / "dashboard.html"
JS = DOCS / "js" / "dashboard.js"


class TestDashboardData:
    """dashboard.json structure (run `make build` / build_dashboard.py first)."""

    @pytest.fixture(autouse=True)
    def load(self):
        assert DASH.exists(), "Run build_dashboard.py first"
        self.data = json.loads(DASH.read_text())

    def test_top_level_keys(self):
        assert {"generated_at", "crawl", "articles"} <= set(self.data)

    def test_crawl_config_matches_crawler(self):
        # These thresholds mirror flock_transparency.STALE_DAYS and the
        # refresh workflow's --max-age default; dashboard.js renders them.
        c = self.data["crawl"]
        assert c["stale_days"] == 14
        assert c["refresh_max_age_days"] == 7

    def test_crawl_totals(self):
        c = self.data["crawl"]
        assert c["total_agencies"] > 100
        assert len(c["agencies"]) == c["total_agencies"]
        assert c["total_captures"] >= c["total_agencies"]

    def test_agency_shape(self):
        for a in self.data["crawl"]["agencies"]:
            assert {"slug", "name", "state", "last_capture",
                    "last_attempt", "captures"} <= set(a)
        assert any(a["last_attempt"] for a in self.data["crawl"]["agencies"])

    def test_agencies_sorted_stalest_first(self):
        attempts = [a["last_attempt"] for a in self.data["crawl"]["agencies"]
                    if a["last_attempt"]]
        assert attempts == sorted(attempts)

    def test_san_mateo_present(self):
        slugs = {a["slug"] for a in self.data["crawl"]["agencies"]}
        assert "san-mateo-ca-pd" in slugs

    def test_throughput_shape(self):
        t = self.data["crawl"]["throughput"]
        assert {"per_day", "mean_7d", "mean_14d", "effective_cycle_days"} <= set(t)
        # Dense, oldest→today series the sparkline renders.
        assert len(t["per_day"]) == 14
        for pt in t["per_day"]:
            assert {"date", "captures"} <= set(pt)
        assert t["per_day"] == sorted(t["per_day"], key=lambda p: p["date"])
        assert isinstance(t["mean_7d"], (int, float))

    def test_quarantined_shape(self):
        # Always a list; entries (if any) are previously-captured agencies that
        # fell out of rotation — never the never-real probe-guess 404s.
        q = self.data["crawl"]["quarantined"]
        assert isinstance(q, list)
        captured = {a["slug"] for a in self.data["crawl"]["agencies"]}
        for item in q:
            assert {"slug", "name", "reason", "since", "last_capture"} <= set(item)
            assert item["slug"] in captured

    def test_articles_shape(self):
        a = self.data["articles"]
        assert {"total", "enriched", "mechanical", "needs_review",
                "failed_urls", "fetched", "last_crawl_run"} <= set(a)
        # Status buckets never exceed the registry total (an unbucketed
        # "unknown" status is possible, so this is <= not ==).
        assert a["total"] >= a["enriched"] + a["mechanical"] + a["needs_review"]
        assert a["total"] > 0


class TestDashboardPage:
    """dashboard.html / dashboard.js wiring required of every docs page."""

    @pytest.fixture(autouse=True)
    def load(self):
        assert HTML.exists()
        self.html = HTML.read_text()

    def test_csp_allows_local_fetch(self):
        assert "Content-Security-Policy" in self.html
        assert "connect-src 'self'" in self.html

    def test_goatcounter_present(self):
        # Memory: every docs page needs the analytics tag + its CSP entry.
        assert "gc.zgo.at/count.js" in self.html
        assert "none-below.goatcounter.com" in self.html

    def test_shared_assets_loaded(self):
        assert "css/shared.css" in self.html
        assert "js/utils.js" in self.html
        assert "js/dashboard.js" in self.html

    def test_js_fetches_dashboard_data(self):
        assert "data/dashboard.json" in JS.read_text()
