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

CA_RE = re.compile(r"California|Cal Fire|Cal State|NCRIC|Cal Poly", re.IGNORECASE)


def detect_state(name):
    # Prefer an explicit 2-letter state code at a word boundary. In Flock's
    # canonical "<place> <state> <type>" naming, the 2-letter code is the
    # authoritative state — even when the place name itself contains a
    # state-name word (e.g. "City of California MO PD" is Missouri, not CA).
    for code in sorted(ALL_STATES):
        if re.search(r"(?<![A-Za-z])" + code + r"(?![A-Za-z])", name):
            return code
    # Fallback for CA-only word forms ("California", "Cal Fire", "NCRIC",
    # "Cal Poly", "Cal State") — no 2-letter code present, but the name
    # implies California.
    if CA_RE.search(name):
        return "CA"
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
    "Decommissioned", "Jaime LE Training", "Do Not Use",
    "Machine Learning Test",
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



def confirmed_slugs(data_dir, probe_state=None):
    """Return the set of slugs we have evidence are real Flock portals.

    A slug is confirmed when:
      - there's a parsed `.json` capture under `<data_dir>/<slug>/`
        (main crawler archived it cleanly), OR
      - there's a `.txt` under `<data_dir>/<slug>/` (the crawler hit
        a real portal page; the parser may have failed downstream
        but the slug itself is real — Flock served a 200), OR
      - slug_probe declared it `found` in its state file.

    Anything else is speculative — a `name_to_slug` guess that nobody
    has actually verified.
    """
    confirmed = set()
    if data_dir.is_dir():
        from lib import portal_jsons, portal_txts
        for slug_dir in data_dir.iterdir():
            if not slug_dir.is_dir() or slug_dir.name.startswith("."):
                continue
            if portal_jsons(slug_dir) or portal_txts(slug_dir):
                confirmed.add(slug_dir.name)
    if probe_state:
        for ag in probe_state.get("agencies", {}).values():
            found = ag.get("found")
            if found:
                confirmed.add(found)
    return confirmed


def prune_speculative_slugs(registry, confirmed):
    """Mutate `registry` in place: set `flock_active_slug` to None and
    trim `flock_slugs` for entries whose slugs aren't in `confirmed`.

    Returns a dict of stats: {pruned_active, pruned_slugs, kept}.
    """
    pruned_active = 0
    pruned_slugs_total = 0
    kept = 0
    for e in registry:
        original_slugs = list(e.get("flock_slugs", []))
        confirmed_for_entry = [s for s in original_slugs if s in confirmed]
        active = e.get("flock_active_slug")
        if active in confirmed:
            new_active = active
        elif confirmed_for_entry:
            # Active wasn't confirmed but some other historical slug is —
            # promote that one. Rare but possible (e.g. Flock renamed
            # the slug, we captured under the new one but the registry
            # still pointed at the old).
            new_active = confirmed_for_entry[0]
        else:
            new_active = None
        if new_active != active:
            pruned_active += 1
        pruned_slugs_total += len(original_slugs) - len(confirmed_for_entry)
        e["flock_active_slug"] = new_active
        e["flock_slugs"] = confirmed_for_entry
        if confirmed_for_entry:
            kept += 1
    return {
        "pruned_active": pruned_active,
        "pruned_slugs": pruned_slugs_total,
        "kept": kept,
    }


def main():
    parser = argparse.ArgumentParser(description="Build agency registry")
    parser.add_argument("--merge", action="store_true",
                        help="Merge new agencies into existing registry, keeping manual edits")
    parser.add_argument("--prune", action="store_true",
                        help="Nullify speculative flock_active_slug / flock_slugs (only confirmed slugs survive). Mutually exclusive with --merge.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args()

    if args.prune:
        if args.merge:
            print("ERROR: --prune and --merge are mutually exclusive", file=sys.stderr)
            sys.exit(1)
        if not REGISTRY_PATH.exists():
            print("ERROR: registry doesn't exist; nothing to prune", file=sys.stderr)
            sys.exit(1)
        registry = json.loads(REGISTRY_PATH.read_text())
        # Probe state contributes confirmed slugs even when no .json is
        # on disk yet — happens when probe just hit but hasn't run
        # archive_agency, or when the captured artifact wasn't committed.
        probe_state_path = args.data_dir / ".slug_probe_state.json"
        probe_state = (
            json.loads(probe_state_path.read_text())
            if probe_state_path.exists() else None
        )
        confirmed = confirmed_slugs(args.data_dir, probe_state)
        print(f"Confirmed slugs (have .txt/.json or probe-found): {len(confirmed)}")
        stats = prune_speculative_slugs(registry, confirmed)
        print(f"Pruned flock_active_slug on {stats['pruned_active']} entries")
        print(f"Removed {stats['pruned_slugs']} speculative entries from flock_slugs")
        print(f"Entries with at least one confirmed slug: {stats['kept']}")
        REGISTRY_PATH.write_text(json.dumps(registry, indent=2) + "\n")
        return

    data_dir = args.data_dir

    # Safety: refuse to overwrite an existing registry without --merge
    if not args.merge and REGISTRY_PATH.exists():
        print("ERROR: registry already exists. Use --merge to add new agencies", file=sys.stderr)
        print("       without --merge would destroy UUIDs, tags, and manual edits.", file=sys.stderr)
        sys.exit(1)

    # Load existing registry for merge mode.
    #   existing    — agency_id -> entry
    #   slug_index  — slug -> agency_id, covers active slug + historical flock_slugs.
    #                 Falsy slugs are skipped (seed entries with slug=None).
    existing = {}
    slug_index = {}
    if args.merge and REGISTRY_PATH.exists():
        for e in json.loads(REGISTRY_PATH.read_text()):
            existing[e["agency_id"]] = e
            if e.get("slug"):
                slug_index.setdefault(e["slug"], e["agency_id"])
            for ps in e.get("flock_slugs", []):
                if ps:
                    slug_index.setdefault(ps, e["agency_id"])

    # Collect all agency names from crawled data
    all_names = set()    # every agency name we've seen
    crawled_slugs = {}   # directory slug -> latest crawl date

    from lib import portal_jsons
    for slug_dir in sorted(data_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue
        jsons = portal_jsons(slug_dir)
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
        aid = slug_index.get(slug)
        if aid and aid in existing:
            names = existing[aid].get("flock_names", [])
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
        # agency_id is uuid5("flock-registry:" + slug). seed_contract_registry.py
        # uses the same scheme keyed on its seed_key, so seeded null-slug entries
        # whose seed_key matches a discovered slug share an agency_id with what
        # we'd otherwise mint here. Detect that collision before creating a
        # duplicate row.
        candidate_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, "flock-registry:" + slug))

        if args.merge:
            existing_id = slug_index.get(slug)
            if not existing_id and candidate_id in existing:
                existing_id = candidate_id

            if existing_id and existing_id in existing:
                entry = existing[existing_id]
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
        # New entries from peer-outbound names start with no confirmed
        # Flock slug — `slug` is a stable internal key derived from the
        # name (used for dedup), but flock_active_slug / flock_slugs only
        # get populated by a successful capture or a slug_probe hit.
        # Speculative guesses don't claim to be slugs.
        entry = {
            "agency_id": candidate_id,
            "slug": slug,
            "flock_active_slug": None,
            "flock_slugs": [],
            "flock_names": [flock_name],
            "display_name": None,
            "agency_role": cls["agency_role"],
            "agency_type": cls["agency_type"],
            "website": None,
            "tags": sorted(cls["tags"]),
            "flags": flags,
            "geo": {"kind": "state-only", "state": state} if state else None,
        }
        # If `slug` (the internal key) happens to match a directory we
        # captured, the slug IS confirmed and should populate the flock
        # fields. The discovery side of build_agency_registry already
        # iterates crawled_slugs upstream; this is for the case where
        # the resolved-from-name slug coincidentally matches.
        if slug in crawled_slugs:
            entry["flock_active_slug"] = slug
            entry["flock_slugs"] = [slug]
        registry.append(entry)
        seen_ids.add(candidate_id)
        new_count += 1

    # In merge mode, preserve manually-added entries not in discovered data
    if args.merge:
        for aid, entry in existing.items():
            if aid not in seen_ids:
                registry.append(entry)
                seen_ids.add(aid)
                kept_count += 1

    # Invariant: agency_id must be unique. The merge logic above tries hard
    # to dedupe, but if a future edge case slips through we want a loud
    # failure with the offending IDs rather than a silently-broken registry.
    ids = [e["agency_id"] for e in registry]
    if len(ids) != len(set(ids)):
        from collections import Counter
        dups = [aid for aid, count in Counter(ids).items() if count > 1]
        print(f"ERROR: duplicate agency_ids in built registry: {dups}", file=sys.stderr)
        for aid in dups:
            for i, e in enumerate(registry):
                if e["agency_id"] == aid:
                    print(f"  [{i}] slug={e.get('slug')!r} "
                          f"flock_slugs={e.get('flock_slugs')} "
                          f"display_name={e.get('display_name')!r}", file=sys.stderr)
        sys.exit(1)

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
