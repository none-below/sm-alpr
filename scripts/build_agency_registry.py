#!/usr/bin/env python3
"""
Build the canonical agency registry from crawled portal data.

Generates assets/agency_registry.json — the single source of truth for
agency identity and classification. This file is meant to be reviewed
and hand-corrected.

After initial generation, re-running will merge new agencies into the
existing registry without overwriting manual edits.

Fields per entry:
  slug          Flock transparency portal URL slug
  flock_name    Display name on Flock's transparency portal
  human_name    Cleaned human-readable name
  lat           Latitude (null if unknown)
  lng           Longitude (null if unknown)
  public        true/false — is this a public agency?
  federal       true/false — is this a federal agency?
  state         Two-letter state code (null if unknown)
  agency_role   police|sheriff|da|fire|parks|campus_safety|corrections|
                highway_patrol|state_parks|intelligence|fish_wildlife|
                tribal|hoa|school|business|test|decommissioned|other
  agency_type   city|county|state|federal|university|tribal|private|
                community|test|other
  website       Agency website URL (null if unknown)
  crawled       Whether we have Flock portal data for this agency
  needs_review  true if auto-classified with low confidence

Usage:
  uv run python scripts/build_agency_registry.py           # initial build
  uv run python scripts/build_agency_registry.py --merge    # add new, keep edits
"""

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")
REGISTRY_PATH = Path("assets/agency_registry.json")

# ── State detection ──

ALL_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

CA_RE = re.compile(r"\bCA\b|California|Cal Fire|Cal State|NCRIC|Cal Poly", re.IGNORECASE)


def detect_state(name):
    if CA_RE.search(name):
        return "CA"
    for code in ALL_STATES - {"CA"}:
        if re.search(r"(?<![A-Za-z])" + code + r"(?![A-Za-z])", name):
            if not CA_RE.search(name):
                return code
    return None


# ── Classification rules ──
# Each rule returns (agency_role, agency_type, public, federal, needs_review)

KNOWN_PRIVATE = [
    "University of the Pacific", "Stanford University",
    "University of San Francisco", "Santa Clara University",
    "Cornerstone Community School",
]

TEST_NAMES = [
    "Demo", "Delete", "DNU", "DUPLICATE", "Test Demo",
    "Decommissioned", "Jaime LE Training",
]


def classify(name):
    """Auto-classify an agency. Returns dict of classification fields."""
    n = name

    # DNU (Do Not Use) entries — deactivated device/org, but underlying agency may be real
    if n.startswith("DNU"):
        # Check if the underlying entity is a known private institution
        is_private = any(p.lower() in n.lower() for p in KNOWN_PRIVATE)
        return {
            "agency_role": "decommissioned",
            "agency_type": "decommissioned",
            "public": not is_private,
            "federal": False,
            "needs_review": False,
        }

    # Test / garbage entries
    if any(t.lower() in n.lower() for t in TEST_NAMES):
        return {
            "agency_role": "test",
            "agency_type": "test",
            "public": False,
            "federal": False,
            "needs_review": False,
        }

    # Known private institutions
    if any(p.lower() in n.lower() for p in KNOWN_PRIVATE):
        role = "campus_safety" if re.search(r"Campus|PD|Police", n, re.I) else "other"
        return {
            "agency_role": role,
            "agency_type": "private",
            "public": False,
            "federal": False,
            "needs_review": False,
        }

    # HOA / community / business patterns
    if re.search(r"HOA|Association|Neighborhood Watch|Estates|Village\b(?!.*PD)", n, re.I):
        return {"agency_role": "hoa", "agency_type": "community", "public": False, "federal": False, "needs_review": False}
    if re.search(r"Corporation|Inc\.|Home Depot|Ulta|FedEx|Great Wolf|Total Wine|Mercury Insurance|Simon Property|Lewis Group|Autobody|Towing|Foster Farms", n, re.I):
        return {"agency_role": "business", "agency_type": "private", "public": False, "federal": False, "needs_review": False}
    if re.search(r"\bSchool\b(?!.*PD)", n, re.I):
        return {"agency_role": "school", "agency_type": "other", "public": None, "federal": False, "needs_review": True}

    # Federal
    if re.search(r"\bFBI\b|Federal\b|US Marshal|DEA\b|ATF\b|ICE\b|CBP\b|Secret Service", n, re.I):
        return {"agency_role": "police", "agency_type": "federal", "public": True, "federal": True, "needs_review": False}

    # State agencies
    if re.search(r"California Highway Patrol|CHP\b", n, re.I):
        return {"agency_role": "highway_patrol", "agency_type": "state", "public": True, "federal": False, "needs_review": False}
    if re.search(r"California State Parks|State Parks\b", n, re.I):
        return {"agency_role": "state_parks", "agency_type": "state", "public": True, "federal": False, "needs_review": False}
    if re.search(r"Cal Fire\b", n, re.I):
        return {"agency_role": "fire", "agency_type": "state", "public": True, "federal": False, "needs_review": False}
    if re.search(r"Department of Corrections", n, re.I):
        return {"agency_role": "corrections", "agency_type": "state", "public": True, "federal": False, "needs_review": False}
    if re.search(r"Fish.*Wildlife", n, re.I):
        return {"agency_role": "fish_wildlife", "agency_type": "state", "public": True, "federal": False, "needs_review": False}
    if re.search(r"NCRIC|Fusion Center|Intelligence Center", n, re.I):
        return {"agency_role": "intelligence", "agency_type": "state", "public": True, "federal": False, "needs_review": False}
    if re.search(r"Department of Insurance", n, re.I):
        return {"agency_role": "other", "agency_type": "state", "public": True, "federal": False, "needs_review": True}

    # University / college
    if re.search(r"University|College|Campus", n, re.I):
        return {"agency_role": "campus_safety", "agency_type": "university", "public": True, "federal": False, "needs_review": True}

    # Tribal
    if re.search(r"Tribal|Rancheria|Reservation|Nation of", n, re.I):
        return {"agency_role": "tribal", "agency_type": "tribal", "public": True, "federal": False, "needs_review": False}

    # Fire authority
    if re.search(r"\bFire\b.*Authority|\bFire\b.*District|\bFD\b|\bFire\b.*Department", n, re.I):
        return {"agency_role": "fire", "agency_type": "county", "public": True, "federal": False, "needs_review": False}

    # DA offices
    if re.search(r"\bDA\b|District Attorney|Attorney.s Office", n, re.I):
        return {"agency_role": "da", "agency_type": "county", "public": True, "federal": False, "needs_review": False}

    # Sheriff
    if re.search(r"Sheriff|County.*SO\b|\bSO\b|\bSD\b", n, re.I):
        return {"agency_role": "sheriff", "agency_type": "county", "public": True, "federal": False, "needs_review": False}

    # County-level (non-sheriff, non-DA)
    if re.search(r"County\b", n, re.I):
        role = "police" if re.search(r"PD|Police", n, re.I) else "other"
        return {"agency_role": role, "agency_type": "county", "public": True, "federal": False, "needs_review": role == "other"}

    # City police (most common)
    if re.search(r"\bPD\b|Police\b|Public Safety\b", n, re.I):
        return {"agency_role": "police", "agency_type": "city", "public": True, "federal": False, "needs_review": False}

    # Port police / parks
    if re.search(r"Port Police|Parks.*PD|Parks.*Department", n, re.I):
        return {"agency_role": "parks", "agency_type": "other", "public": True, "federal": False, "needs_review": False}

    # City of X (without PD)
    if re.search(r"^City of|^Town of", n, re.I):
        return {"agency_role": "other", "agency_type": "city", "public": True, "federal": False, "needs_review": True}

    # Fallback
    return {"agency_role": "other", "agency_type": "other", "public": None, "federal": False, "needs_review": True}


def flock_name_to_human(name):
    """Simple cleanup — don't over-expand, keep recognizable."""
    s = name.strip()
    s = re.sub(r"\s+", " ", s)
    return s


def main():
    parser = argparse.ArgumentParser(description="Build agency registry")
    parser.add_argument("--merge", action="store_true",
                        help="Merge new agencies into existing registry, keeping manual edits")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()

    data_dir = args.data_dir

    # Load existing registry for merge mode
    existing = {}
    if args.merge and REGISTRY_PATH.exists():
        for e in json.loads(REGISTRY_PATH.read_text()):
            existing[e["slug"]] = e

    # Collect all entities from crawled data
    slug_to_flock_name = {}
    crawled_slugs = {}  # slug -> latest crawl date

    for slug_dir in sorted(data_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue
        jsons = sorted(slug_dir.glob("*.json"))
        if not jsons:
            continue
        data = json.loads(jsons[-1].read_text())
        crawled_slugs[slug_dir.name] = jsons[-1].stem  # e.g. "2026-03-27"

        for name, slug in zip(
            data.get("shared_org_names", []),
            data.get("shared_org_slugs", []),
        ):
            slug_to_flock_name[slug] = name

    for slug in crawled_slugs:
        if slug not in slug_to_flock_name:
            slug_to_flock_name[slug] = slug

    # Also pick up entities from the sharing graph (includes inbound, uncrawled refs)
    graph_path = data_dir / ".sharing_graph_full.json"
    if graph_path.exists():
        graph = json.loads(graph_path.read_text())
        for slug in graph.get("agencies", {}):
            if slug not in slug_to_flock_name:
                slug_to_flock_name[slug] = slug

    # Build registry
    registry = []
    new_count = 0
    kept_count = 0

    for slug in sorted(slug_to_flock_name.keys()):
        # In merge mode, keep existing manual edits
        if args.merge and slug in existing:
            entry = existing[slug]
            entry["crawled"] = slug in crawled_slugs
            entry["crawled_date"] = crawled_slugs.get(slug)
            registry.append(entry)
            kept_count += 1
            continue

        flock_name = slug_to_flock_name[slug]
        cls = classify(flock_name)

        entry = {
            "slug": slug,
            "flock_name": flock_name,
            "human_name": flock_name_to_human(flock_name),
            "lat": None,
            "lng": None,
            "public": cls["public"],
            "federal": cls["federal"],
            "state": detect_state(flock_name),
            "agency_role": cls["agency_role"],
            "agency_type": cls["agency_type"],
            "website": None,
            "crawled": slug in crawled_slugs,
            "crawled_date": crawled_slugs.get(slug),
            "needs_review": cls["needs_review"],
        }
        registry.append(entry)
        new_count += 1

    # Save
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2) + "\n")

    # Stats
    total = len(registry)
    review = sum(1 for e in registry if e.get("needs_review"))
    public = sum(1 for e in registry if e.get("public") is True)
    private = sum(1 for e in registry if e.get("public") is False)
    null_public = sum(1 for e in registry if e.get("public") is None)
    geocoded = sum(1 for e in registry if e.get("lat"))

    print(f"Registry: {total} agencies -> {REGISTRY_PATH}")
    if args.merge:
        print(f"  New:          {new_count}")
        print(f"  Kept:         {kept_count}")
    print(f"  Crawled:      {sum(1 for e in registry if e['crawled'])}")
    print(f"  Geocoded:     {geocoded}")
    print(f"  Public:       {public}")
    print(f"  Private:      {private}")
    print(f"  Unknown:      {null_public}")
    print(f"  Needs review: {review}")

    if review:
        print(f"\n  Entries needing review:")
        for e in registry:
            if e.get("needs_review"):
                print(f"    {e['flock_name']}: role={e['agency_role']}, type={e['agency_type']}, public={e['public']}")


if __name__ == "__main__":
    main()
