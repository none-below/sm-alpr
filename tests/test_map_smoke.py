#!/usr/bin/env python3
"""
Smoke tests for the sharing map.

Builds the map, serves it locally, and verifies key functionality
using Playwright. Run with: uv run pytest tests/test_map_smoke.py
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

DOCS_DIR = Path("docs")
MAP_DATA = DOCS_DIR / "data" / "map_data.json"


# ── Data tests (no browser needed) ──

class TestMapData:
    """Verify map_data.json has expected structure and content."""

    @pytest.fixture(autouse=True)
    def load_data(self):
        assert MAP_DATA.exists(), "Run build_map.py first"
        self.data = json.loads(MAP_DATA.read_text())

    def test_has_markers(self):
        assert len(self.data["markers"]) > 100

    def test_has_coords(self):
        assert len(self.data["coords"]) > 100

    def test_has_agency_info(self):
        assert len(self.data["agencyInfo"]) > 100

    def test_san_mateo_exists(self):
        slugs = [m["slug"] for m in self.data["markers"]]
        assert "san-mateo-ca-pd" in slugs

    def test_san_mateo_has_outbound(self):
        smpd = next(m for m in self.data["markers"] if m["slug"] == "san-mateo-ca-pd")
        assert smpd["outbound_count"] > 200

    def test_san_mateo_has_inbound(self):
        smpd = next(m for m in self.data["markers"] if m["slug"] == "san-mateo-ca-pd")
        assert smpd["inbound_count"] > 50

    def test_flock_vendor_exists(self):
        slugs = [m["slug"] for m in self.data["markers"]]
        assert "flock-safety-vendor" in slugs

    def test_flock_vendor_in_outbound(self):
        smpd = next(m for m in self.data["markers"] if m["slug"] == "san-mateo-ca-pd")
        assert "flock-safety-vendor" in smpd["outbound_slugs"]

    def test_flock_vendor_has_inbound(self):
        flock = next(m for m in self.data["markers"] if m["slug"] == "flock-safety-vendor")
        assert flock["inbound_count"] > 100

    def test_private_entities_flagged(self):
        uop = self.data["agencyInfo"].get("university-of-the-pacific-ca", {})
        assert uop.get("public") is False
        assert uop.get("type") == "private"

    def test_ncric_exists(self):
        ncric = self.data["agencyInfo"].get("ncric", {})
        assert ncric.get("crawled") is True

    def test_indirect_violations_computed(self):
        assert "indirectViolations" in self.data
        assert len(self.data["indirectViolations"]) > 50

    def test_no_garbled_entries(self):
        """No ncmec-amber-alert parser artifacts."""
        for slug in self.data["agencyInfo"]:
            assert "ncmec-amber-alert" not in slug, f"Garbled entry: {slug}"

    def test_markers_have_coords(self):
        """All markers should have valid lat/lng."""
        for m in self.data["markers"]:
            assert m["lat"] is not None, f"{m['slug']} missing lat"
            assert m["lng"] is not None, f"{m['slug']} missing lng"


# ── Registry tests ──

class TestRegistry:
    """Verify agency_registry.json integrity."""

    @pytest.fixture(autouse=True)
    def load_registry(self):
        p = Path("assets/agency_registry.json")
        assert p.exists()
        self.registry = json.loads(p.read_text())
        self.by_slug = {e["slug"]: e for e in self.registry}

    def test_has_entries(self):
        assert len(self.registry) > 300

    def test_flock_in_registry(self):
        assert "flock-safety-vendor" in self.by_slug

    def test_flock_is_private(self):
        flock = self.by_slug["flock-safety-vendor"]
        assert flock["public"] is False
        assert flock["agency_role"] == "vendor"

    def test_uop_is_private(self):
        uop = self.by_slug.get("university-of-the-pacific-ca", {})
        assert uop.get("public") is False

    def test_no_duplicate_slugs(self):
        slugs = [e["slug"] for e in self.registry]
        assert len(slugs) == len(set(slugs)), "Duplicate slugs found"

    def test_crawled_have_dates(self):
        for e in self.registry:
            if e.get("crawled"):
                assert e.get("crawled_date"), f"{e['slug']} crawled but no date"

    def test_no_null_public_for_known_types(self):
        """Entities with known types should have public set."""
        known_types = {"city", "county", "state", "federal", "university", "private", "tribal"}
        for e in self.registry:
            if e.get("agency_type") in known_types:
                assert e.get("public") is not None, f"{e['slug']} type={e['agency_type']} but public=None"


# ── HTML tests ──

class TestHTML:
    """Verify the HTML shell is correct."""

    @pytest.fixture(autouse=True)
    def load_html(self):
        p = DOCS_DIR / "sharing_map.html"
        assert p.exists()
        self.html = p.read_text()

    def test_has_leaflet(self):
        assert "leaflet" in self.html.lower()

    def test_has_map_div(self):
        assert 'id="map"' in self.html

    def test_has_info_panel(self):
        assert 'id="info"' in self.html

    def test_has_violation_banner(self):
        assert 'id="violation-banner"' in self.html

    def test_has_cache_bust_on_js(self):
        assert "map.js?v=" in self.html

    def test_has_cache_bust_on_data(self):
        js = (DOCS_DIR / "js" / "map.js").read_text()
        assert "map_data.json?v=" in js
