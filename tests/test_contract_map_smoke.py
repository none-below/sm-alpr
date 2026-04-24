#!/usr/bin/env python3
"""
Smoke test for the contracts map: verifies required UI elements exist in
docs/contracts.html, build_contract_map.py runs against a fixture backed by
the real registry, and the generated bundle has the expected top-level shape.

No browser involved — grepping the HTML for required selectors is enough to
catch regressions at this stage.

Run with: uv run pytest tests/test_contract_map_smoke.py
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "docs" / "contracts.html"
JS_PATH = ROOT / "docs" / "js" / "contracts.js"
BUILD_SCRIPT = ROOT / "scripts" / "build_contract_map.py"

# Required UI elements — keep in sync with docs/contracts.html / contracts.js.
# If you add a new top-level UI overlay, add its selector here.
_UI_ELEMENTS = [
    'id="map"',
    'id="info"',
    'id="info-close"',
    'id="search-box"',
    'id="search-results"',
    'id="reason-legend"',
    'id="gate-overlay"',
    'id="draft-banner"',
    'class="legend"',
    'class="back-link"',
    'class="page-title"',
    'src="js/contracts.js"',
    "data/contract_map_data.json",
]


def test_html_exists():
    assert HTML_PATH.is_file(), f"missing {HTML_PATH}"


def test_js_exists():
    assert JS_PATH.is_file(), f"missing {JS_PATH}"


def test_html_has_required_ui_elements():
    html = HTML_PATH.read_text()
    joint = html + "\n" + JS_PATH.read_text()
    missing = [sel for sel in _UI_ELEMENTS if sel not in joint]
    assert not missing, f"missing UI elements: {missing}"


def test_html_has_leaflet_include():
    html = HTML_PATH.read_text()
    assert "leaflet@1.9.4" in html
    assert "integrity=" in html  # SRI on CDN assets


def test_html_has_strict_csp():
    html = HTML_PATH.read_text()
    assert "Content-Security-Policy" in html
    assert "default-src 'self'" in html


def _pick_seeded_agency_id():
    """Return a registry agency_id known to have coords — used to validate
    the build against a working fixture without inventing a fake UUID."""
    reg = json.loads((ROOT / "assets" / "agency_registry.json").read_text())
    for e in reg:
        if e.get("slug") == "santa-cruz-ca-pd":
            return e["agency_id"]
    # Fallback: any entry with a valid place geo
    for e in reg:
        geo = e.get("geo") or {}
        if geo.get("lat") and geo.get("lng"):
            return e["agency_id"]
    raise RuntimeError("no suitable registry entry for fixture")


def test_build_runs_against_fixture(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    agency_id = _pick_seeded_agency_id()
    articles = [{
        "id": "fixture-news-2025-01-01-cancel",
        "url": "https://example.com/story",
        "title": "Fixture City cancels ALPR contract",
        "outlet": "Fixture Tribune",
        "author": None,
        "published_date": "2025-01-01",
        "summary": None,
        "tags": [],
    }]
    events = [{
        "agency_id": agency_id,
        "type": "canceled",
        "date": "2025-01-01",
        "vendor": "flock",
        "cameras_affected": 12,
        "article_ids": ["fixture-news-2025-01-01-cancel"],
        "reasons": ["federal-access", "privacy-general"],
        "notes": "Canceled by council vote.",
    }]
    (src / "events.json").write_text(json.dumps(events))
    (src / "articles.json").write_text(json.dumps(articles))

    out = tmp_path / "bundle.json"
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--src", str(src), "--out", str(out)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    bundle = json.loads(out.read_text())

    assert set(bundle.keys()) >= {"markers", "agencies", "generated_at", "meta"}
    assert len(bundle["markers"]) == 1
    m = bundle["markers"][0]
    assert m["id"] == agency_id
    assert m["status_overall"] == "canceled"
    assert m["event_count"] == 1
    assert m["article_count"] == 1

    payload = bundle["agencies"][agency_id]
    assert payload["status_overall"] == "canceled"
    assert payload["status_by_vendor"]["flock"]["status"] == "canceled"
    assert payload["events"][0]["cameras_affected"] == 12
    assert payload["events"][0]["reasons"] == ["federal-access", "privacy-general"]
    assert payload["articles"][0]["id"] == "fixture-news-2025-01-01-cancel"

    # Meta carries reasons metadata for the UI to render chips
    assert "reasons" in bundle["meta"]
    assert "federal-access" in bundle["meta"]["reasons"]


def test_build_fails_on_unknown_agency_id(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "articles.json").write_text("[]")
    (src / "events.json").write_text(json.dumps([{
        "agency_id": "00000000-0000-0000-0000-000000000000",
        "type": "canceled", "date": None, "vendor": "flock",
        "article_ids": [], "reasons": [], "notes": "",
    }]))
    out = tmp_path / "bundle.json"
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--src", str(src), "--out", str(out)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode != 0
    assert "not in registry" in result.stderr or "unknown agency_id" in result.stderr


def test_build_fails_on_dangling_article_ref(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    agency_id = _pick_seeded_agency_id()
    (src / "articles.json").write_text("[]")
    (src / "events.json").write_text(json.dumps([{
        "agency_id": agency_id,
        "type": "canceled", "date": None, "vendor": "flock",
        "article_ids": ["ghost-article"], "reasons": [], "notes": "",
    }]))
    out = tmp_path / "bundle.json"
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--src", str(src), "--out", str(out)],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode != 0
    assert "ghost-article" in result.stderr
