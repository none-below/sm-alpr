#!/usr/bin/env python3
"""
Idempotently add the seed agencies used by the contract-status map to
assets/agency_registry.json. Agencies that already exist in the registry
(by slug) are left untouched; new entries are appended and geocoded.

Non-Flock agencies get `flock_slugs: []` and `flock_active_slug: null` —
the registry is the central identity DB regardless of ALPR vendor.

Usage:
  uv run python scripts/seed_contract_registry.py
  uv run python scripts/seed_contract_registry.py --dry-run
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from geocode_agencies import geocode_entry  # noqa: E402

REGISTRY_PATH = Path("assets/agency_registry.json")

# Each seed entry below becomes a registry row. Slugs are stable and
# deterministic — re-running produces the same UUIDs. For agencies already
# tracked in the registry (CA Flock portal crawls), this script is a no-op
# because the slug already exists.
#
# Fields:
#   slug          — hand-chosen stable slug (idempotency key)
#   display_name  — how the agency should be rendered
#   state         — 2-letter state code
#   role          — "police" | "sheriff" | "da" | ...
#   type          — "city" | "county" | "state" | "federal" | "university"
#   website       — optional official website URL
#   flock_slug    — set ONLY if the agency has a known Flock transparency
#                   portal (so the contract map can pull live snapshots).
#                   Leave None for agencies that never used Flock or have
#                   no crawlable portal.

SEEDS = [
    # CA — most already in registry from the SMPD sharing crawl, but we
    # include them here so slug → agency_id mapping is explicit and
    # any missing ones get auto-added.
    {"slug": "santa-cruz-ca-pd",     "display_name": "Santa Cruz Police Department",      "state": "CA", "role": "police",  "type": "city",   "flock_slug": "santa-cruz-ca-pd"},
    {"slug": "mountain-view-ca-pd",  "display_name": "Mountain View Police Department",   "state": "CA", "role": "police",  "type": "city",   "flock_slug": "mountain-view-ca-pd"},
    {"slug": "south-pasadena-ca-pd", "display_name": "South Pasadena Police Department",  "state": "CA", "role": "police",  "type": "city",   "flock_slug": "south-pasadena-ca-pd"},
    {"slug": "berkeley-ca-pd",       "display_name": "Berkeley Police Department",        "state": "CA", "role": "police",  "type": "city",   "flock_slug": "berkeley-ca-pd"},
    {"slug": "alameda-county-ca-so", "display_name": "Alameda County Sheriff's Office",   "state": "CA", "role": "sheriff", "type": "county", "flock_slug": "alameda-county-ca-so"},

    # Non-CA / non-Flock-crawled — registry entry with flock_slug=None
    {"slug": "oak-park-il-pd",       "display_name": "Oak Park Police Department",        "state": "IL", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "evanston-il-pd",       "display_name": "Evanston Police Department",        "state": "IL", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "austin-tx-pd",         "display_name": "Austin Police Department",          "state": "TX", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "denver-co-pd",         "display_name": "Denver Police Department",          "state": "CO", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "charlottesville-va-pd","display_name": "Charlottesville Police Department", "state": "VA", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "staunton-va-pd",       "display_name": "Staunton Police Department",        "state": "VA", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "flagstaff-az-pd",      "display_name": "Flagstaff Police Department",       "state": "AZ", "role": "police",  "type": "city",       "flock_slug": "flagstaff-az-pd-inactive"},
    {"slug": "cambridge-ma-pd",      "display_name": "Cambridge Police Department",       "state": "MA", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "eugene-or-pd",         "display_name": "Eugene Police Department",          "state": "OR", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "springfield-or-pd",    "display_name": "Springfield Police Department",     "state": "OR", "role": "police",  "type": "city",       "flock_slug": "springfield-or-pd"},
    {"slug": "ithaca-ny-pd",         "display_name": "Ithaca Police Department",          "state": "NY", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "tompkins-county-ny-so","display_name": "Tompkins County Sheriff's Office",  "state": "NY", "role": "sheriff", "type": "county",     "flock_slug": None},
    {"slug": "hillsborough-nc-pd",   "display_name": "Hillsborough Police Department",    "state": "NC", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "coralville-ia-pd",     "display_name": "Coralville Police Department",      "state": "IA", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "lynnwood-wa-pd",       "display_name": "Lynnwood Police Department",        "state": "WA", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "olympia-wa-pd",        "display_name": "Olympia Police Department",         "state": "WA", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "redmond-wa-pd",        "display_name": "Redmond Police Department",         "state": "WA", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "oshkosh-wi-pd",        "display_name": "Oshkosh Police Department",         "state": "WI", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "windsor-ct-pd",        "display_name": "Windsor Police Department",         "state": "CT", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "columbus-oh-pd",       "display_name": "Columbus Police Department",        "state": "OH", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "el-paso-tx-pd",        "display_name": "El Paso Police Department",         "state": "TX", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "bloomington-in-pd",    "display_name": "Bloomington Police Department",     "state": "IN", "role": "police",  "type": "city",       "flock_slug": None},
    {"slug": "richmond-va-pd",       "display_name": "Richmond Police Department",        "state": "VA", "role": "police",  "type": "city",       "flock_slug": "richmond-va-pd"},
]


def mint_agency_id(slug):
    # Same namespace as build_agency_registry.py so slugs → UUIDs stay stable.
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "flock-registry:" + slug))


def build_entry(seed):
    slug = seed["slug"]
    flock_slug = seed.get("flock_slug")
    entry = {
        "agency_id": mint_agency_id(slug),
        "slug": slug,
        "flock_active_slug": flock_slug,
        "flock_slugs": [flock_slug] if flock_slug else [],
        "flock_names": [seed["display_name"]] if flock_slug else [],
        "display_name": seed["display_name"],
        "agency_role": seed["role"],
        "agency_type": seed["type"],
        "website": seed.get("website"),
        "notes": None,
        "tags": ["public"],
        "flags": [],
        "geo": {"kind": "state-only", "state": seed["state"]},
    }
    return entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change, don't write")
    args = ap.parse_args()

    registry = json.loads(REGISTRY_PATH.read_text())
    by_slug = {e.get("slug"): e for e in registry}

    added = []
    existed = []
    for seed in SEEDS:
        if seed["slug"] in by_slug:
            existed.append(seed["slug"])
            continue
        entry = build_entry(seed)
        # Geocode to replace the "state-only" stub with precise coords
        geo = geocode_entry(entry)
        if geo:
            entry["geo"] = geo
        added.append(entry)

    print(f"existed in registry: {len(existed)}")
    for s in existed:
        print(f"  skip  {s}")
    print(f"new to be added:     {len(added)}")
    for e in added:
        g = e.get("geo") or {}
        print(f"  add   {e['slug']:30s}  agency_id={e['agency_id']}  "
              f"geo={g.get('kind')} lat={g.get('lat')} lng={g.get('lng')}")

    if args.dry_run:
        print("\n(dry run — no changes written)")
        return

    if not added:
        print("nothing to write")
        return

    registry.extend(added)
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2) + "\n")
    print(f"\nwrote {REGISTRY_PATH}: {len(added)} new entries, "
          f"{len(registry)} total")


if __name__ == "__main__":
    main()
