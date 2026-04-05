#!/usr/bin/env python3
"""
Smoke tests for the sharing map.

Builds the map, serves it locally, and verifies key functionality
using Playwright. Run with: uv run pytest tests/test_map_smoke.py
"""

import http.server
import json
import subprocess
import threading
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
        assert flock["inbound_count"] >= 1

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
        known_types = {"city", "county", "state", "federal", "fusion_center", "university", "private", "tribal"}
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

    def test_has_search_box(self):
        assert 'id="search-input"' in self.html

    def test_has_cache_bust_on_js(self):
        assert "map.js?v=" in self.html

    def test_has_cache_bust_on_data(self):
        js = (DOCS_DIR / "js" / "map.js").read_text()
        assert "map_data.json?v=" in js


# ── Browser layout tests (Playwright) ──

def _serve_docs():
    """Start a local HTTP server for the docs directory, return (server, port)."""
    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    server.timeout = 0.5
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _boxes_overlap(a, b):
    """Return True if two bounding boxes overlap."""
    return not (
        a["x"] + a["width"] <= b["x"] or
        b["x"] + b["width"] <= a["x"] or
        a["y"] + a["height"] <= b["y"] or
        b["y"] + b["height"] <= a["y"]
    )


# All positioned UI elements — checked pairwise for overlap.
# Add new elements here as they're created.
_UI_ELEMENTS = [
    "#search-box",
    "#violation-banner",
    ".info-panel",
    ".legend",
]

# Build all unique pairs
_NO_OVERLAP_PAIRS = [
    (a, b) for i, a in enumerate(_UI_ELEMENTS) for b in _UI_ELEMENTS[i + 1:]
]

VIEWPORTS = [
    {"name": "desktop", "width": 1440, "height": 900},
    {"name": "laptop", "width": 1280, "height": 720},
    {"name": "tablet", "width": 768, "height": 1024},
    {"name": "mobile", "width": 375, "height": 812},
]


class TestLayout:
    """Verify UI elements don't overlap across screen sizes."""

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
    def browser(self, request):
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        b = pw.chromium.launch()
        request.cls._pw = pw
        request.cls._browser = b
        yield b
        b.close()
        pw.stop()

    def test_ui_elements_and_overlays(self, browser):
        """All UI elements exist, and no unregistered overlays are present."""
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"http://127.0.0.1:{self.port}/sharing_map.html", wait_until="networkidle")
        page.wait_for_selector("#map", state="visible", timeout=10000)

        # Every element in _UI_ELEMENTS must exist
        for sel in _UI_ELEMENTS:
            el = page.query_selector(sel)
            assert el is not None, f"UI element {sel} not found in page"

        # Catch new positioned overlays not yet in _UI_ELEMENTS
        overlays = page.evaluate("""() => {
            const found = [];
            document.querySelectorAll('body > *').forEach(el => {
                const style = getComputedStyle(el);
                if (style.position === 'absolute' || style.position === 'fixed') {
                    const id = el.id ? '#' + el.id : (el.className ? '.' + el.className.split(' ')[0] : el.tagName);
                    found.push(id);
                }
            });
            return found;
        }""")
        known = set(_UI_ELEMENTS)
        # Dynamically shown/hidden elements that don't need overlap checks in default state
        known.update(["#edge-left", "#edge-right", "#edge-top", "#edge-bottom",
                       "#offmap", "#infoToggle", "#search-results"])
        # #info is the .info-panel (already in _UI_ELEMENTS as .info-panel)
        known.add("#info")
        unknown = [o for o in overlays if o not in known and o != "#map"]
        assert not unknown, (
            f"Found positioned overlay(s) not in _UI_ELEMENTS: {unknown}. "
            f"Add them to _UI_ELEMENTS in test_map_smoke.py to include them in overlap checks."
        )
        page.close()

    @pytest.mark.parametrize("vp", VIEWPORTS, ids=lambda v: v["name"])
    def test_layout_at_viewport(self, browser, vp):
        """No overlaps, all elements visible and within viewport bounds."""
        page = browser.new_page(viewport={"width": vp["width"], "height": vp["height"]})
        page.goto(f"http://127.0.0.1:{self.port}/sharing_map.html", wait_until="networkidle")
        page.wait_for_selector("#map", state="visible", timeout=10000)

        # -- No element overlap --
        for sel_a, sel_b in _NO_OVERLAP_PAIRS:
            el_a = page.query_selector(sel_a)
            el_b = page.query_selector(sel_b)
            if not el_a or not el_b:
                continue
            if not el_a.is_visible() or not el_b.is_visible():
                continue
            box_a = el_a.bounding_box()
            box_b = el_b.bounding_box()
            if not box_a or not box_b:
                continue
            assert not _boxes_overlap(box_a, box_b), (
                f"{sel_a} overlaps {sel_b} at {vp['name']} ({vp['width']}x{vp['height']}): "
                f"{sel_a}={box_a}, {sel_b}={box_b}"
            )

        # -- All elements visible and in viewport --
        hidden_by_default = {"#violation-banner"}
        for sel in _UI_ELEMENTS:
            el = page.query_selector(sel)
            if not el:
                continue
            if not el.is_visible():
                if sel == ".legend" and vp["width"] < 769:
                    continue
                if sel in hidden_by_default:
                    continue
                pytest.fail(f"{sel} not visible at {vp['name']} ({vp['width']}x{vp['height']})")
            box = el.bounding_box()
            if not box:
                continue
            assert box["x"] >= 0, f"{sel} clipped on left at {vp['name']}: x={box['x']}"
            assert box["y"] >= 0, f"{sel} clipped on top at {vp['name']}: y={box['y']}"
            assert box["x"] + box["width"] <= vp["width"] + 1, (
                f"{sel} extends past right edge at {vp['name']}: "
                f"right={box['x'] + box['width']}, viewport={vp['width']}"
            )
            assert box["y"] + box["height"] <= vp["height"] + 1, (
                f"{sel} extends past bottom edge at {vp['name']}: "
                f"bottom={box['y'] + box['height']}, viewport={vp['height']}"
            )
        page.close()

    @pytest.mark.parametrize("vp", VIEWPORTS, ids=lambda v: v["name"])
    def test_violation_banner_after_click(self, browser, vp):
        """After selecting an agency with violations, banner should not overlap search and be fully visible."""
        page = browser.new_page(viewport={"width": vp["width"], "height": vp["height"]})
        page.goto(f"http://127.0.0.1:{self.port}/sharing_map.html#san-mateo-ca-pd", wait_until="networkidle")
        page.wait_for_selector("#map", state="visible", timeout=10000)
        # Give JS time to process the hash
        page.wait_for_timeout(500)

        search = page.query_selector("#search-box")
        banner = page.query_selector("#violation-banner")

        # Banner must be visible when an agency with violations is selected
        assert banner is not None, "Violation banner element not found"
        assert banner.is_visible(), f"Violation banner not visible at {vp['name']} after selecting agency with violations"

        box_b = banner.bounding_box()
        assert box_b is not None, f"Violation banner has no bounding box at {vp['name']}"

        # Banner must be fully within viewport
        assert box_b["x"] >= 0, f"Banner clipped on left at {vp['name']}: x={box_b['x']}"
        assert box_b["y"] >= 0, f"Banner clipped on top at {vp['name']}: y={box_b['y']}"
        assert box_b["x"] + box_b["width"] <= vp["width"], (
            f"Banner clipped on right at {vp['name']}: right edge={box_b['x'] + box_b['width']}, viewport={vp['width']}"
        )
        assert box_b["y"] + box_b["height"] <= vp["height"], (
            f"Banner clipped on bottom at {vp['name']}: bottom edge={box_b['y'] + box_b['height']}, viewport={vp['height']}"
        )

        # Must not overlap search box
        if search and search.is_visible():
            box_s = search.bounding_box()
            if box_s:
                assert not _boxes_overlap(box_s, box_b), (
                    f"Search box overlaps violation banner at {vp['name']}: "
                    f"search={box_s}, banner={box_b}"
                )
        page.close()
