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

# SEED_KEY is a stable internal identifier used for UUID minting and for
# idempotent dedup when slug is null. Never change a seed_key after an entry
# is written to the registry — agency_ids depend on it.
#
# flock_slug is only set when the Flock transparency portal has been verified
# to exist (user manual confirmation or Playwright probe). Setting it here
# also populates flock_slugs/flock_names in the registry entry.
#
# Agencies we suspect had Flock but whose real slug we haven't verified get
# flock_slug=None. A future slug-guesser tool can upgrade these in place.
SEEDS = [
    # CA — already in registry from the SMPD sharing crawl (seeder skips them).
    {"seed_key": "santa-cruz-ca-pd",     "slug": "santa-cruz-ca-pd",     "display_name": "Santa Cruz Police Department",      "state": "CA", "role": "police",  "type": "city",   "flock_slug": "santa-cruz-ca-pd"},
    {"seed_key": "south-pasadena-ca-pd", "slug": "south-pasadena-ca-pd", "display_name": "South Pasadena Police Department",  "state": "CA", "role": "police",  "type": "city",   "flock_slug": "south-pasadena-ca-pd"},
    {"seed_key": "berkeley-ca-pd",       "slug": "berkeley-ca-pd",       "display_name": "Berkeley Police Department",        "state": "CA", "role": "police",  "type": "city",   "flock_slug": "berkeley-ca-pd"},
    {"seed_key": "alameda-county-ca-so", "slug": "alameda-county-ca-so", "display_name": "Alameda County Sheriff's Office",   "state": "CA", "role": "sheriff", "type": "county", "flock_slug": "alameda-county-ca-so"},
    {"seed_key": "springfield-or-pd",    "slug": "springfield-or-pd",    "display_name": "Springfield Police Department",     "state": "OR", "role": "police",  "type": "city",   "flock_slug": "springfield-or-pd"},
    {"seed_key": "richmond-va-pd",       "slug": "richmond-va-pd",       "display_name": "Richmond Police Department",        "state": "VA", "role": "police",  "type": "city",   "flock_slug": "richmond-va-pd"},

    # Verified portals (user manual + Playwright).
    {"seed_key": "mountain-view-ca-pd",   "slug": "mountain-view-ca-pd",   "display_name": "Mountain View Police Department",   "state": "CA", "role": "police",  "type": "city",   "flock_slug": "mountain-view-ca-pd"},
    {"seed_key": "austin-tx-pd",          "slug": "austin-tx-pd",          "display_name": "Austin Police Department",          "state": "TX", "role": "police",  "type": "city",   "flock_slug": "austin-tx-pd"},
    {"seed_key": "denver-co-pd",          "slug": "denver-co-pd",          "display_name": "Denver Police Department",          "state": "CO", "role": "police",  "type": "city",   "flock_slug": "denver-co-pd"},
    {"seed_key": "charlottesville-va-pd", "slug": "charlottesville-va-pd", "display_name": "Charlottesville Police Department", "state": "VA", "role": "police",  "type": "city",   "flock_slug": "charlottesville-va-pd"},
    {"seed_key": "staunton-va-pd",        "slug": "staunton-va-pd",        "display_name": "Staunton Police Department",        "state": "VA", "role": "police",  "type": "city",   "flock_slug": "staunton-va-pd"},
    {"seed_key": "flagstaff-az-pd",       "slug": "flagstaff-az-pd",       "display_name": "Flagstaff Police Department",       "state": "AZ", "role": "police",  "type": "city",   "flock_slug": "flagstaff-az-pd"},
    {"seed_key": "eugene-or-pd",          "slug": "eugene-or-pd",          "display_name": "Eugene Police Department",          "state": "OR", "role": "police",  "type": "city",   "flock_slug": "eugene-or-pd"},
    {"seed_key": "tompkins-county-ny-so", "slug": "tompkins-county-ny-so", "display_name": "Tompkins County Sheriff's Office",  "state": "NY", "role": "sheriff", "type": "county", "flock_slug": "tompkins-county-ny-so"},
    {"seed_key": "hillsborough-nc-pd",    "slug": "hillsborough-nc-pd",    "display_name": "Hillsborough Police Department",    "state": "NC", "role": "police",  "type": "city",   "flock_slug": "hillsborough-nc-pd"},
    {"seed_key": "lynnwood-wa-pd",        "slug": "lynnwood-wa-pd",        "display_name": "Lynnwood Police Department",        "state": "WA", "role": "police",  "type": "city",   "flock_slug": "lynnwood-wa-pd"},
    {"seed_key": "oshkosh-wi-pd",         "slug": "oshkosh-wi-pd",         "display_name": "Oshkosh Police Department",         "state": "WI", "role": "police",  "type": "city",   "flock_slug": "oshkosh-wi-pd"},
    {"seed_key": "windsor-ct-pd",         "slug": "windsor-ct-pd",         "display_name": "Windsor Police Department",         "state": "CT", "role": "police",  "type": "city",   "flock_slug": "windsor-ct-pd"},

    # Unverified: real slug unknown. Registry entry exists but carries no
    # flock_* data until the slug-guesser tool verifies.
    {"seed_key": "oak-park-il-pd",        "slug": None, "display_name": "Oak Park Police Department",        "state": "IL", "role": "police",  "type": "city",   "flock_slug": None},
    {"seed_key": "evanston-il-pd",        "slug": None, "display_name": "Evanston Police Department",        "state": "IL", "role": "police",  "type": "city",   "flock_slug": None},
    {"seed_key": "cambridge-ma-pd",       "slug": None, "display_name": "Cambridge Police Department",       "state": "MA", "role": "police",  "type": "city",   "flock_slug": None},
    {"seed_key": "ithaca-ny-pd",          "slug": None, "display_name": "Ithaca Police Department",          "state": "NY", "role": "police",  "type": "city",   "flock_slug": None},
    {"seed_key": "coralville-ia-pd",      "slug": None, "display_name": "Coralville Police Department",      "state": "IA", "role": "police",  "type": "city",   "flock_slug": None},
    {"seed_key": "olympia-wa-pd",         "slug": None, "display_name": "Olympia Police Department",         "state": "WA", "role": "police",  "type": "city",   "flock_slug": None},
    {"seed_key": "redmond-wa-pd",         "slug": None, "display_name": "Redmond Police Department",         "state": "WA", "role": "police",  "type": "city",   "flock_slug": None},
    {"seed_key": "columbus-oh-pd",        "slug": None, "display_name": "Columbus Police Department",        "state": "OH", "role": "police",  "type": "city",   "flock_slug": None},
    {"seed_key": "el-paso-tx-pd",         "slug": None, "display_name": "El Paso Police Department",         "state": "TX", "role": "police",  "type": "city",   "flock_slug": None},
    {"seed_key": "bloomington-in-pd",     "slug": None, "display_name": "Bloomington Police Department",     "state": "IN", "role": "police",  "type": "city",   "flock_slug": None},
]


def mint_agency_id(seed_key):
    # Deterministic UUID keyed on seed_key. Same namespace prefix as
    # build_agency_registry.py (flock-registry:) so seeds whose seed_key
    # matches an existing slug in the registry produce the same UUID as
    # the original auto-populated entry — enabling deduplication.
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, "flock-registry:" + seed_key))


def build_entry(seed):
    flock_slug = seed.get("flock_slug")
    entry = {
        "agency_id": mint_agency_id(seed["seed_key"]),
        "slug": seed["slug"],  # may be None for unverified agencies
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
    # Dedup by agency_id (deterministic from seed_key). Works whether or
    # not the slug is populated, which matters for entries whose real
    # Flock slug has not yet been verified.
    by_id = {e.get("agency_id"): e for e in registry}

    added = []
    existed = []
    for seed in SEEDS:
        aid = mint_agency_id(seed["seed_key"])
        if aid in by_id:
            existed.append(seed["seed_key"])
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
        ident = e.get("slug") or e.get("display_name")
        print(f"  add   {ident:40s}  agency_id={e['agency_id']}  "
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
