#!/usr/bin/env python3
"""
Build the canonical agency registry from crawled portal data.

Generates assets/agency_registry.json — the single source of truth for
agency identity and classification. This file is meant to be reviewed
and hand-corrected.

After initial generation, re-running will merge new agencies into the
existing registry without overwriting manual edits.

Fields per entry:
  agency_id          Stable unique identifier (UUID, never changes once assigned)
  slug               Flock transparency portal URL slug (backward compat)
  flock_active_slug  Current active Flock portal URL slug
  flock_slugs        All known Flock portal URL slugs [active, ...old]
  flock_names        All observed Flock display names [primary, ...variants]
  display_name       Curated display name (null = use flock_names[-1], the most recent)
  lat           Latitude (null if unknown)
  lng           Longitude (null if unknown)
  state         Two-letter state code (null if unknown)
  agency_role   police|sheriff|da|fire|parks|campus_safety|corrections|
                highway_patrol|state_parks|intelligence|fish_wildlife|
                tribal|hoa|school|business|test|decommissioned|other
  agency_type   city|county|state|federal|university|tribal|private|
                community|test|other
  website       Agency website URL (null if unknown)
  tags          Classification tags: public|private|federal|needs-review|ag-lawsuit

Derived at runtime (not stored):
  crawled       lib.crawl_status() checks flock_slugs directories
  crawled_date  Latest JSON filename in the crawled directory

Usage:
  uv run python scripts/build_agency_registry.py           # initial build
  uv run python scripts/build_agency_registry.py --merge    # add new, keep edits
"""

import argparse
import json
import re
import sys
import uuid
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
# Each rule returns (agency_role, agency_type, tags)

KNOWN_PRIVATE = [
    "University of the Pacific", "Stanford University",
    "University of San Francisco", "Santa Clara University",
    "Cornerstone Community School",
]

TEST_NAMES = [
    "Demo", "Delete", "DNU", "DUPLICATE", "Test Demo",
    "Decommissioned", "Jaime LE Training",
]


def detect_flags(name):
    """Extract display flags from an agency name.

    These become a list on the registry entry and are shown as UI badges
    (alongside dynamic flags like out-of-state). Unlike tags, these are
    curated status indicators — not classification.
    """
    flags = []
    n_lower = name.lower()
    if "[inactive]" in n_lower:
        flags.append("inactive")
    if "[deactivated]" in n_lower or "deactivated-org" in n_lower:
        flags.append("deactivated")
    if "duplicate" in n_lower:
        flags.append("duplicate")
    if re.search(r"^DNU\b", name):
        flags.append("dnu")
    if re.search(r"\[decommissioned\]", n_lower) or n_lower == "decommissioned org":
        flags.append("decommissioned")
    return flags


def classify(name):
    """Auto-classify an agency. Returns dict with agency_role, agency_type, tags."""
    n = name

    # DNU (Do Not Use) entries — deactivated device/org, but underlying agency may be real
    if n.startswith("DNU"):
        is_private = any(p.lower() in n.lower() for p in KNOWN_PRIVATE)
        return {"agency_role": "decommissioned", "agency_type": "decommissioned",
                "tags": ["private"] if is_private else ["public"]}

    # Test / garbage entries
    if any(t.lower() in n.lower() for t in TEST_NAMES):
        return {"agency_role": "test", "agency_type": "test", "tags": ["private"]}

    # Known private institutions
    if any(p.lower() in n.lower() for p in KNOWN_PRIVATE):
        role = "campus_safety" if re.search(r"Campus|PD|Police", n, re.I) else "other"
        return {"agency_role": role, "agency_type": "private", "tags": ["private"]}

    # HOA / community / business patterns
    if re.search(r"HOA|Association|Neighborhood Watch|Estates|Village\b(?!.*PD)", n, re.I):
        return {"agency_role": "hoa", "agency_type": "community", "tags": ["private"]}
    if re.search(r"Corporation|Inc\.|Home Depot|Ulta|FedEx|Great Wolf|Total Wine|Mercury Insurance|Simon Property|Lewis Group|Autobody|Towing|Foster Farms", n, re.I):
        return {"agency_role": "business", "agency_type": "private", "tags": ["private"]}
    if re.search(r"\bSchool\b(?!.*PD)", n, re.I):
        return {"agency_role": "school", "agency_type": "other", "tags": ["needs-review"]}

    # Federal
    if re.search(r"\bFBI\b|Federal\b|US Marshal|DEA\b|ATF\b|ICE\b|CBP\b|Secret Service", n, re.I):
        return {"agency_role": "police", "agency_type": "federal", "tags": ["public", "federal"]}

    # State agencies
    if re.search(r"California Highway Patrol|CHP\b", n, re.I):
        return {"agency_role": "highway_patrol", "agency_type": "state", "tags": ["public"]}
    if re.search(r"California State Parks|State Parks\b", n, re.I):
        return {"agency_role": "state_parks", "agency_type": "state", "tags": ["public"]}
    if re.search(r"Cal Fire\b", n, re.I):
        return {"agency_role": "fire", "agency_type": "state", "tags": ["public"]}
    if re.search(r"Department of Corrections", n, re.I):
        return {"agency_role": "corrections", "agency_type": "state", "tags": ["public"]}
    if re.search(r"Fish.*Wildlife", n, re.I):
        return {"agency_role": "fish_wildlife", "agency_type": "state", "tags": ["public"]}
    if re.search(r"NCRIC|Fusion Center|Intelligence Center", n, re.I):
        return {"agency_role": "intelligence", "agency_type": "state", "tags": ["public"]}
    if re.search(r"Department of Insurance", n, re.I):
        return {"agency_role": "other", "agency_type": "state", "tags": ["public", "needs-review"]}

    # University / college
    if re.search(r"University|College|Campus", n, re.I):
        return {"agency_role": "campus_safety", "agency_type": "university", "tags": ["public", "needs-review"]}

    # Tribal
    if re.search(r"Tribal|Rancheria|Reservation|Nation of", n, re.I):
        return {"agency_role": "tribal", "agency_type": "tribal", "tags": ["public"]}

    # Fire authority
    if re.search(r"\bFire\b.*Authority|\bFire\b.*District|\bFD\b|\bFire\b.*Department", n, re.I):
        return {"agency_role": "fire", "agency_type": "county", "tags": ["public"]}

    # DA offices
    if re.search(r"\bDA\b|District Attorney|Attorney.s Office", n, re.I):
        return {"agency_role": "da", "agency_type": "county", "tags": ["public"]}

    # Sheriff
    if re.search(r"Sheriff|County.*SO\b|\bSO\b|\bSD\b", n, re.I):
        return {"agency_role": "sheriff", "agency_type": "county", "tags": ["public"]}

    # County-level (non-sheriff, non-DA)
    if re.search(r"County\b", n, re.I):
        role = "police" if re.search(r"PD|Police", n, re.I) else "other"
        tags = ["public"] + (["needs-review"] if role == "other" else [])
        return {"agency_role": role, "agency_type": "county", "tags": tags}

    # City police (most common)
    if re.search(r"\bPD\b|Police\b|Public Safety\b", n, re.I):
        return {"agency_role": "police", "agency_type": "city", "tags": ["public"]}

    # Port police / parks
    if re.search(r"Port Police|Parks.*PD|Parks.*Department", n, re.I):
        return {"agency_role": "parks", "agency_type": "other", "tags": ["public"]}

    # City of X (without PD)
    if re.search(r"^City of|^Town of", n, re.I):
        return {"agency_role": "other", "agency_type": "city", "tags": ["public", "needs-review"]}

    # Fallback — unknown public status
    return {"agency_role": "other", "agency_type": "other", "tags": ["needs-review"]}



def main():
    parser = argparse.ArgumentParser(description="Build agency registry")
    parser.add_argument("--merge", action="store_true",
                        help="Merge new agencies into existing registry, keeping manual edits")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()

    data_dir = args.data_dir

    # Safety: refuse to overwrite an existing registry without --merge
    if not args.merge and REGISTRY_PATH.exists():
        print("ERROR: registry already exists. Use --merge to add new agencies", file=sys.stderr)
        print("       without --merge would destroy UUIDs, tags, and manual edits.", file=sys.stderr)
        sys.exit(1)

    # Load existing registry for merge mode
    existing = {}
    if args.merge and REGISTRY_PATH.exists():
        for e in json.loads(REGISTRY_PATH.read_text()):
            existing[e["agency_id"]] = e
            # Index by slug and flock_slugs so we can find existing entries
            existing.setdefault(f"__slug__{e['slug']}", e["agency_id"])
            for ps in e.get("flock_slugs", []):
                existing.setdefault(f"__slug__{ps}", e["agency_id"])

    # Collect all agency names from crawled data
    all_names = set()    # every agency name we've seen
    crawled_slugs = {}   # directory slug -> latest crawl date

    for slug_dir in sorted(data_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue
        jsons = sorted(slug_dir.glob("*.json"))
        if not jsons:
            continue
        data = json.loads(jsons[-1].read_text())
        crawled_slugs[slug_dir.name] = jsons[-1].stem  # e.g. "2026-03-27"

        # Collect names from both old and new field names
        for key in ("sharing_outbound", "sharing_inbound",
                     "shared_org_names", "orgs_sharing_with_names"):
            all_names.update(data.get(key, []))

    # Build slug_to_flock_name for new entry creation
    # Each crawled directory is an agency; use its registry name or slug
    slug_to_flock_name = {}
    for slug in crawled_slugs:
        entry = existing.get(f"__slug__{slug}")
        if entry and entry in existing:
            e = existing[entry]
            names = e.get("flock_names", [])
            slug_to_flock_name[slug] = names[0] if names else slug
        else:
            slug_to_flock_name[slug] = slug

    # For each discovered name not already in the registry, derive a slug
    from lib import resolve_agency, name_to_slug
    for name in all_names:
        if resolve_agency(name=name):
            continue  # already known
        slug = name_to_slug(name)
        if slug not in slug_to_flock_name:
            slug_to_flock_name[slug] = name

    # Build registry
    registry = []
    seen_ids = set()
    new_count = 0
    kept_count = 0

    for slug in sorted(slug_to_flock_name.keys()):
        flock_name = slug_to_flock_name[slug]

        # In merge mode, check if this slug maps to an existing agency
        if args.merge:
            existing_id = existing.get(f"__slug__{slug}")
            if existing_id and existing_id in existing:
                entry = existing[existing_id]
                # Add new name if we haven't seen it
                if flock_name not in entry.get("flock_names", []):
                    entry.setdefault("flock_names", []).append(flock_name)
                if existing_id not in seen_ids:
                    registry.append(entry)
                    seen_ids.add(existing_id)
                    kept_count += 1
                continue

        cls = classify(flock_name)

        state = detect_state(flock_name)
        flags = detect_flags(flock_name)
        entry = {
            "agency_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "flock-registry:" + slug)),
            "slug": slug,
            "flock_active_slug": slug,
            "flock_slugs": [slug],
            "flock_names": [flock_name],
            "display_name": None,
            "agency_role": cls["agency_role"],
            "agency_type": cls["agency_type"],
            "website": None,
            "tags": sorted(cls["tags"]),
            "flags": flags,
            "geo": {"kind": "state-only", "state": state} if state else None,
        }
        registry.append(entry)
        seen_ids.add(slug)
        new_count += 1

    # In merge mode, preserve manually-added entries not in discovered data
    if args.merge:
        for aid, entry in existing.items():
            if aid.startswith("__slug__"):
                continue
            if aid not in seen_ids:
                registry.append(entry)
                seen_ids.add(aid)
                kept_count += 1

    # Auto-geocode new entries via the Census gazetteer
    from geocode_agencies import geocode_entry, needs_geocoding
    geocoded_new = 0
    for entry in registry:
        if needs_geocoding(entry):
            geo = geocode_entry(entry)
            if geo:
                entry["geo"] = geo
                geocoded_new += 1

    # Save
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2) + "\n")

    # Stats
    total = len(registry)
    review = sum(1 for e in registry if "needs-review" in e.get("tags", []))
    public = sum(1 for e in registry if "public" in e.get("tags", []))
    private = sum(1 for e in registry if "private" in e.get("tags", []))
    null_public = total - public - private
    geocoded = sum(1 for e in registry if (e.get("geo") or {}).get("lat"))

    print(f"Registry: {total} agencies -> {REGISTRY_PATH}")
    if args.merge:
        print(f"  New:          {new_count}")
        print(f"  Kept:         {kept_count}")
    print(f"  New geocoded: {geocoded_new}")
    print(f"  Crawled:      {len(crawled_slugs)}")
    print(f"  Geocoded:     {geocoded}")
    print(f"  Public:       {public}")
    print(f"  Private:      {private}")
    print(f"  Unknown:      {null_public}")
    print(f"  Needs review: {review}")

    if review:
        print(f"\n  Entries needing review:")
        for e in registry:
            if "needs-review" in e.get("tags", []):
                from lib import agency_display_name
                name = agency_display_name(e)
                print(f"    {name}: role={e['agency_role']}, type={e['agency_type']}, tags={e.get('tags', [])}")


if __name__ == "__main__":
    main()
