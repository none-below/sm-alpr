#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Tripwires for the CARTO basemap API key.

CARTO began watermarking keyless basemap tiles ("API KEY REQUIRED" struck
diagonally across every tile) in September 2026, so every tile URL now has to
carry ?key=. The failure mode is silent: a page that builds its own tile URL,
or that forgets to load map_common.js, still renders — just defaced. Nothing
in the build or the other smoke tests notices.

These assert derivable invariants rather than listing known-good files:
  1. Exactly one place in the shipped JS constructs a cartocdn tile URL, and
     it carries the key.
  2. Any page whose scripts call MapCommon actually loads map_common.js
     (contracts.html did not, which is how the second map page was missed).

Run with: uv run pytest tests/test_basemap_key.py
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
JS_DIR = DOCS / "js"
MAP_COMMON = JS_DIR / "map_common.js"

# docs/js/map.js is generated from scripts/map.js and is gitignored; it is a
# build output, not a source we can fix, so scan the template instead.
_GENERATED = {"map.js"}

_TILE_HOST = "basemaps.cartocdn.com"


def _js_sources():
    """Committed JS that ships to the site, plus the generator templates."""
    for p in sorted(JS_DIR.glob("*.js")):
        if p.name not in _GENERATED:
            yield p
    for p in sorted((ROOT / "scripts").glob("*.js")):
        yield p


def test_only_map_common_builds_a_tile_url():
    """One definition of the tile URL means one place the key can go stale.

    A second literal is how a page silently reverts to watermarked tiles.
    """
    offenders = [
        p.relative_to(ROOT)
        for p in _js_sources()
        if _TILE_HOST in p.read_text(encoding="utf-8") and p != MAP_COMMON
    ]
    assert not offenders, (
        f"These build their own {_TILE_HOST} URL instead of calling "
        f"MapCommon.cartoTileUrl(), so they will not carry the API key "
        f"and will render watermarked tiles: {offenders}"
    )


def test_tile_url_carries_the_key():
    src = MAP_COMMON.read_text(encoding="utf-8")
    assert _TILE_HOST in src, "map_common.js no longer builds the tile URL"
    assert "?key=" in src, (
        "map_common.js builds a cartocdn tile URL without ?key= — CARTO "
        "watermarks keyless tiles"
    )
    # The key is public by design (static site), but it must be a real value,
    # not a placeholder someone forgot to fill in.
    m = re.search(r"CARTO_KEY\s*=\s*'([^']*)'", src)
    assert m, "CARTO_KEY constant not found in map_common.js"
    key = m.group(1)
    assert len(key) > 12 and "YOUR" not in key.upper(), (
        f"CARTO_KEY looks like a placeholder: {key!r}"
    )


def test_key_is_gated_on_the_authorized_host():
    """The key is referrer-restricted, so sending it from an origin CARTO
    does not recognise returns 403 — a map with no basemap, worse than the
    watermark it replaced. Keyless requests still return (watermarked) tiles,
    so off-production must fall back to keyless rather than always keying.

    This guards the gate itself: dropping it would blank the map on
    localhost and in the CI screenshot harness (127.0.0.1).
    """
    src = MAP_COMMON.read_text(encoding="utf-8")
    m = re.search(r"CARTO_KEYED_HOSTS\s*=\s*\[([^\]]*)\]", src)
    assert m, "CARTO_KEYED_HOSTS allowlist not found in map_common.js"
    hosts = re.findall(r"'([^']+)'", m.group(1))
    assert hosts, "CARTO_KEYED_HOSTS is empty — the key would never be sent"
    for bad in ("localhost", "127.0.0.1"):
        assert bad not in hosts, (
            f"{bad!r} is in CARTO_KEYED_HOSTS, but the key is not authorized "
            f"for it — tiles will 403 instead of falling back to keyless"
        )
    # The gate must actually guard the key, not sit unused.
    assert "CARTO_KEYED_HOSTS.indexOf" in src, (
        "CARTO_KEYED_HOSTS is defined but never consulted — the key is "
        "being sent unconditionally"
    )


def test_pages_using_mapcommon_load_it():
    """contracts.html called into MapCommon without loading map_common.js.

    That throws a ReferenceError at init and leaves the map with no basemap
    at all — worse than the watermark it was meant to fix.
    """
    # page -> the local js files it loads
    for html in sorted(DOCS.glob("*.html")):
        text = html.read_text(encoding="utf-8")
        loaded = set(re.findall(r'<script src="js/([A-Za-z0-9_.-]+\.js)', text))
        if not loaded:
            continue
        uses_mapcommon = False
        for name in loaded:
            src_path = JS_DIR / name
            if name in _GENERATED:
                # check the template the output is generated from
                src_path = ROOT / "scripts" / name
            if not src_path.exists():
                continue
            if "MapCommon." in src_path.read_text(encoding="utf-8"):
                uses_mapcommon = True
                break
        if uses_mapcommon:
            assert "map_common.js" in loaded, (
                f"{html.name} loads JS that calls MapCommon.* but never "
                f"loads js/map_common.js — MapCommon will be undefined"
            )
