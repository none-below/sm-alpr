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

    def test_public_field_is_tristate(self):
        """public should be True, False, or None — never just a boolean for unknown."""
        for slug, info in self.data["agencyInfo"].items():
            assert info.get("public") in (True, False, None), (
                f"{slug}: public={info.get('public')!r}, expected True/False/None"
            )

    def test_ncric_exists(self):
        ncric = self.data["agencyInfo"].get("ncric", {})
        assert ncric.get("crawled") is True

    def test_indirect_flags_computed(self):
        assert "indirectFlags" in self.data
        assert len(self.data["indirectFlags"]) > 50

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
        assert "private" in flock.get("tags", [])
        assert flock["agency_role"] == "vendor"

    def test_uop_is_private(self):
        uop = self.by_slug.get("university-of-the-pacific-ca", {})
        assert "private" in uop.get("tags", [])

    def test_no_duplicate_slugs(self):
        # slug may be null for registry entries that represent an agency
        # we track without a verified Flock portal (non-Flock vendor, or
        # a slug-guesser hasn't confirmed the real slug yet). Only check
        # uniqueness among entries that actually carry a slug.
        slugs = [e["slug"] for e in self.registry if e.get("slug")]
        assert len(slugs) == len(set(slugs)), "Duplicate slugs found"

    def test_no_duplicate_agency_ids(self):
        ids = [e["agency_id"] for e in self.registry]
        assert len(ids) == len(set(ids)), "Duplicate agency_ids found"

    def test_agency_id_is_uuid(self):
        """agency_id should be a valid UUID."""
        import uuid
        for e in self.registry:
            try:
                uuid.UUID(e["agency_id"])
            except ValueError:
                assert False, f"{e['slug']} has invalid UUID: {e['agency_id']}"

    def test_has_flock_slugs(self):
        # Every entry carries the keys, but the arrays are only required to
        # be non-empty for agencies with an actively-crawled Flock portal.
        # Non-Flock agencies (used by the contract-map feature) have
        # flock_active_slug=None and empty slug/name lists.
        for e in self.registry:
            assert "flock_slugs" in e, f"{e['agency_id']} missing flock_slugs"
            assert isinstance(e["flock_slugs"], list)
            assert "flock_active_slug" in e
            if e["flock_active_slug"]:
                assert e["flock_active_slug"] in e["flock_slugs"], (
                    f"{e['agency_id']}: flock_active_slug not in flock_slugs"
                )

    def test_has_flock_names(self):
        # Same relaxation as test_has_flock_slugs: non-Flock registry entries
        # have an empty flock_names list.
        for e in self.registry:
            assert "flock_names" in e, f"{e['agency_id']} missing flock_names"
            assert isinstance(e["flock_names"], list)

    def test_no_crawled_fields_in_registry(self):
        """crawled/crawled_date are derived at runtime, not stored."""
        for e in self.registry:
            assert "crawled" not in e, f"{e['agency_id']} has stale 'crawled' field"
            assert "crawled_date" not in e, f"{e['agency_id']} has stale 'crawled_date' field"

    def test_no_stale_boolean_fields(self):
        """Old boolean fields should not exist in registry entries."""
        stale = {"public", "federal", "needs_review", "ag_lawsuit", "crawled", "crawled_date",
                 "also_known_as", "also_known_as_names", "flock_name", "human_name"}
        for e in self.registry:
            found = stale & set(e.keys())
            assert not found, f"{e['agency_id']} has stale fields: {found}"

    def test_tags_no_conflicts(self):
        """No entry should have both public and private tags."""
        for e in self.registry:
            tags = e.get("tags", [])
            assert not ("public" in tags and "private" in tags), (
                f"{e['agency_id']} has both public and private tags"
            )

    def test_known_types_have_public_or_private_tag(self):
        """Entities with known types should be tagged public or private."""
        known_types = {"city", "county", "state", "federal", "fusion_center", "university", "private", "tribal"}
        for e in self.registry:
            if e.get("agency_type") in known_types:
                tags = e.get("tags", [])
                assert "public" in tags or "private" in tags, (
                    f"{e['agency_id']} type={e['agency_type']} but no public/private tag"
                )


# ── Lib accessor tests (unit tests, no data dependencies) ──

class TestLibAccessors:
    """Verify lib.py accessor functions handle edge cases."""

    @pytest.fixture(autouse=True, scope="class")
    def add_scripts_to_path(self):
        import sys
        scripts_dir = str(Path(__file__).parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)

    def test_has_tag(self):
        from lib import has_tag
        assert has_tag({"tags": ["public", "federal"]}, "public") is True
        assert has_tag({"tags": ["public"]}, "private") is False
        assert has_tag({}, "public") is False
        assert has_tag({"tags": []}, "public") is False

    def test_agency_display_name(self):
        from lib import agency_display_name
        assert agency_display_name({"display_name": "Custom", "flock_names": ["Flock"]}) == "Custom"
        assert agency_display_name({"flock_names": ["Flock Name"]}) == "Flock Name"
        assert agency_display_name({}, "fallback") == "fallback"
        assert agency_display_name({}) == "?"

    def test_agency_active_slug(self):
        from lib import agency_active_slug
        assert agency_active_slug({"flock_active_slug": "active", "flock_slugs": ["active", "old"]}) == "active"
        assert agency_active_slug({"flock_slugs": ["first", "second"]}) == "first"
        assert agency_active_slug({}, "fallback") == "fallback"
        assert agency_active_slug({}) == "?"

    def test_resolve_agency_by_slug(self):
        from lib import resolve_agency
        entry = resolve_agency(slug="san-mateo-ca-pd")
        assert entry is not None
        assert "san-mateo-ca-pd" in entry["flock_slugs"]

    def test_resolve_agency_by_name(self):
        from lib import resolve_agency
        entry = resolve_agency(name="San Mateo CA PD")
        assert entry is not None
        assert "San Mateo CA PD" in entry["flock_names"]

    def test_resolve_agency_not_found(self):
        from lib import resolve_agency
        assert resolve_agency(slug="nonexistent-agency-xyz") is None
        assert resolve_agency(name="Nonexistent Agency XYZ") is None

    def test_resolve_returns_full_entry(self):
        """resolve_agency should return a complete registry entry."""
        from lib import resolve_agency
        entry = resolve_agency(slug="san-mateo-ca-pd")
        assert entry is not None
        assert "agency_id" in entry
        assert "flock_active_slug" in entry
        assert "tags" in entry


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

    def test_has_flag_banner(self):
        assert 'id="flag-banner"' in self.html

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
    "#flag-banner",
    ".info-panel",
    ".legend",
    ".back-link",
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
        hidden_by_default = {"#flag-banner"}
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
    def test_flag_banner_after_click(self, browser, vp):
        """After selecting an agency with flags, banner should not overlap search and be fully visible."""
        page = browser.new_page(viewport={"width": vp["width"], "height": vp["height"]})
        page.goto(f"http://127.0.0.1:{self.port}/sharing_map.html#san-mateo-ca-pd", wait_until="networkidle")
        page.wait_for_selector("#map", state="visible", timeout=10000)
        # Give JS time to process the hash
        page.wait_for_timeout(500)

        search = page.query_selector("#search-box")
        banner = page.query_selector("#flag-banner")

        # Banner must be visible when an agency with flags is selected
        assert banner is not None, "Flag banner element not found"
        assert banner.is_visible(), f"Flag banner not visible at {vp['name']} after selecting agency with flags"

        box_b = banner.bounding_box()
        assert box_b is not None, f"Flag banner has no bounding box at {vp['name']}"

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
                    f"Search box overlaps flag banner at {vp['name']}: "
                    f"search={box_s}, banner={box_b}"
                )
        page.close()
