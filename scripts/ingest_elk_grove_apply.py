#!/usr/bin/env python3
"""One-shot registry edits + file moves to ingest the Elk Grove self-hosted
ALPR transparency page.

Why this exists: Elk Grove publishes its ALPR program info on elkgrove.gov
instead of using a Flock transparency portal. We've already parsed two
snapshots (2025-12-31, 2026-05-12) into Flock-shaped JSON. This script:

  1. Moves snapshot files into the existing transparency.flocksafety.com
     slug directory so the graph builder picks them up without code change.
  2. Adds aliases on existing registry entries for Elk Grove's spacing-typo
     variants of agency names (Dec snapshot has several).
  3. Adds 5 new registry entries: Millbrae CA PD (real city, not previously
     scraped) and 4 private operators (HOA + 3 businesses) seen in EG's
     Dec inbound list.
  4. Sets website on existing Elk Grove CA PD entry.

Idempotent on re-run: existing aliases / entries are not duplicated.

Usage:  uv run python scripts/ingest_elk_grove_apply.py
"""
from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gazetteer

REGISTRY = Path("assets/agency_registry.json")
SRC_DIR = Path("assets/transparency.elkgrove.gov/elk-grove-ca-pd")
DST_DIR = Path("assets/transparency.flocksafety.com/elk-grove-ca-pd")

EG_URL = "https://elkgrove.gov/traffic/automated-license-plate-readers-alpr"


# ── (existing entry, alias_to_add) — alias EG's typed text → registry agency ──
ALIASES = [
    # spacing typos for "X CA PD" written as "X PD - CA" etc.
    ("suisun-city-ca-pd", "Suisun City PD - CA"),
    ("suisun-city-ca-pd", "Suisun City PD- CA"),
    ("sacramento-ca-pd", "Sacramento PD - CA"),
    ("sacramento-county-ca-so", "Sacramento SO - CA"),
    ("citrus-heights-ca-pd", "Citrus Heights PD- CA"),
    ("grass-valley-ca-pd", "Grass Valley PD -CA"),
    ("yuba-county-ca-so", "Yuba County CA SO"),
    ("anderson-ca-pd", "Anderson City CA PD"),
    # Flex feeds are the same agency, mobile-camera channel
    ("napa-county-ca-so", "Napa County CA SO (Flex)"),
    ("vallejo-ca-pd", "Vallejo CA PD (Flex)"),
]


def _id_for(slug: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "flock-registry:" + slug))


def _new_private(slug: str, flock_name: str, role: str, atype: str, state: str = "CA") -> dict:
    return {
        "agency_id": _id_for(slug),
        "slug": slug,
        "flock_active_slug": None,
        "flock_slugs": [],
        "flock_names": [flock_name],
        "display_name": None,
        "agency_role": role,
        "agency_type": atype,
        "website": None,
        "tags": ["private"],
        "geo": {"kind": "state-only", "state": state},
        "flags": [],
    }


def build_new_entries() -> list[dict]:
    millbrae_geo = gazetteer.lookup_place("Millbrae", "CA")
    millbrae = {
        "agency_id": _id_for("millbrae-ca-pd"),
        "slug": "millbrae-ca-pd",
        "flock_active_slug": None,
        "flock_slugs": [],
        "flock_names": ["Millbrae CA PD"],
        "display_name": None,
        "agency_role": "police",
        "agency_type": "city",
        "website": None,
        "tags": ["public"],
        "geo": {
            "kind": "place",
            "fips": millbrae_geo["fips"],
            "name": millbrae_geo["name"],
            "state": millbrae_geo["state"],
            "lat": millbrae_geo["lat"],
            "lng": millbrae_geo["lng"],
        },
        "flags": [],
    }
    return [
        millbrae,
        _new_private("chezimme-estates-hoa", "CheZimme Estates HOA",
                     role="hoa", atype="community"),
        _new_private("eskanos-family-lp-ca", "Eskanos Family LP (CA)",
                     role="business", atype="private"),
        _new_private("elk-grove-auto-mall-ca", "Elk Grove Auto Mall (CA)",
                     role="business", atype="private"),
        _new_private("stones-gambling-hall-ca", "Stones Gambling Hall (CA)",
                     role="business", atype="private"),
    ]


def apply_registry_edits():
    reg = json.loads(REGISTRY.read_text())
    by_slug = {e["slug"]: e for e in reg}

    changed = 0

    # Set website on Elk Grove CA PD
    eg = by_slug.get("elk-grove-ca-pd")
    if eg and eg.get("website") != EG_URL:
        eg["website"] = EG_URL
        changed += 1
        print(f"  set website on elk-grove-ca-pd")

    # Append aliases
    for slug, alias in ALIASES:
        entry = by_slug.get(slug)
        if not entry:
            print(f"  WARN: no entry for {slug} (alias {alias!r} skipped)")
            continue
        aliases = entry.get("aliases") or []
        if alias in aliases:
            continue
        aliases.append(alias)
        entry["aliases"] = aliases
        changed += 1
        print(f"  + alias {alias!r} → {slug}")

    # Add new entries
    for new in build_new_entries():
        if new["slug"] in by_slug:
            continue
        reg.append(new)
        by_slug[new["slug"]] = new
        changed += 1
        print(f"  + new entry {new['slug']} ({new['flock_names'][0]})")

    REGISTRY.write_text(json.dumps(reg, indent=2) + "\n")
    print(f"registry: {changed} change(s)")


def move_files():
    if not SRC_DIR.exists():
        print("  (snapshot dir already moved or absent)")
        return
    DST_DIR.mkdir(parents=True, exist_ok=True)
    for src in sorted(SRC_DIR.iterdir()):
        dst = DST_DIR / src.name
        if dst.exists():
            print(f"  skip (already at dst): {src.name}")
            continue
        shutil.move(str(src), str(dst))
        print(f"  moved {src.name}")
    # Remove parent dirs if empty
    try:
        SRC_DIR.rmdir()
        SRC_DIR.parent.rmdir()
        print("  removed empty assets/transparency.elkgrove.gov/")
    except OSError:
        pass


def main():
    print("=== move snapshot files ===")
    move_files()
    print("=== apply registry edits ===")
    apply_registry_edits()


if __name__ == "__main__":
    main()
