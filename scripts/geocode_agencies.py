#!/usr/bin/env python3
"""Geocode agencies in the registry using the US Census gazetteer.

For each ungeocoded agency with a state:
  1. Extract a candidate place/county name from flock_names or display_name.
  2. Look up in the gazetteer by (name, state).
  3. Populate the entry's `geo` block with FIPS code + cached lat/lng/name.

Usage:
  uv run python scripts/geocode_agencies.py              # dry run, print matches
  uv run python scripts/geocode_agencies.py --apply      # write changes to registry
  uv run python scripts/geocode_agencies.py --slug X     # geocode a single agency

The `geo` block structure:
  {
    "kind": "place" | "county" | "state" | "manual",
    "fips": "0668252",     # most specific FIPS; null for manual
    "name": "San Mateo",   # what this location represents
    "state": "CA",         # 2-letter USPS code (first 2 chars of fips)
    "lat": 37.56031,
    "lng": -122.3106
  }

FIPS format is self-describing by length:
  2 digits = state (06 = CA)
  5 digits = county (06081 = San Mateo County)
  7 digits = place (0668252 = San Mateo city)

Cached fields (name, state, lat, lng) must match the gazetteer —
validated by tests/test_geo_cache.py.

Kind == "manual" entries are hand-curated (e.g. university campuses,
HQ addresses) and are not validated against the gazetteer.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import load_registry, agency_display_name, agency_state, has_tag
from gazetteer import lookup_place, lookup_county, lookup_cousub

REGISTRY_PATH = Path("assets/agency_registry.json")


# Patterns to strip from an agency name to get a place/county candidate
_AGENCY_SUFFIXES = re.compile(
    r"\s*\b(PD|SO|SD|DA|FD|DPS|Police Department|Sheriff'?s? Office|"
    r"Sheriff'?s? Department|District Attorney|Fire Department|"
    r"Department of Public Safety|"
    r"Public Safety|Parks Department|Marshal'?s? Office)\b\s*",
    re.IGNORECASE,
)

_STATE_TOKEN = re.compile(r"\s*\b[A-Z]{2}\b\s*")  # Strip " CA " etc.
_PARENS = re.compile(r"\s*\([^)]*\)\s*")  # Strip "(CA)" etc.
_CITY_OF = re.compile(r"^(City|Town|Village|Borough)\s+of\s+", re.IGNORECASE)


def extract_place_candidate(agency_name):
    """Derive a candidate place name from an agency name.

    Examples:
      "San Mateo CA PD" -> "San Mateo"
      "City of Menifee CA PD" -> "Menifee"
      "Foster City CA PD" -> "Foster City"
      "Akron OH PD" -> "Akron"
    """
    name = agency_name
    # Remove parenthetical tags like "(CA)"
    name = _PARENS.sub(" ", name)
    # Strip agency-role suffixes (PD, SO, DPS, etc.)
    name = _AGENCY_SUFFIXES.sub(" ", name)
    # Strip state codes (now standalone)
    name = _STATE_TOKEN.sub(" ", name)
    # Strip "City of" / "Town of" prefix
    name = _CITY_OF.sub("", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def extract_township_candidate(agency_name):
    """Derive a township candidate from an agency name.

    Examples:
      "Jackson Township OH PD (Stark County)" -> "Jackson"
      "Butler Township OH PD" -> "Butler"
      "Mahoning Twp PA PD" -> "Mahoning"
    """
    name = agency_name
    name = _PARENS.sub(" ", name)           # drop "(Stark County)"
    name = _AGENCY_SUFFIXES.sub(" ", name)  # drop "PD"
    name = _STATE_TOKEN.sub(" ", name)      # drop "OH"
    name = re.sub(r"\b(Township|Twp)\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def extract_county_candidate(agency_name):
    """Derive a candidate county name.

    Examples:
      "Alameda County CA SO" -> "Alameda"
      "Butte County CA SO" -> "Butte"
      "San Mateo County CA SO" -> "San Mateo"
    """
    name = agency_name
    name = _PARENS.sub(" ", name)
    # Only match if "County" appears (or Parish etc.)
    if not re.search(r"\b(County|Parish|Borough|Census Area)\b", name, re.IGNORECASE):
        return None
    name = _AGENCY_SUFFIXES.sub(" ", name)
    name = _STATE_TOKEN.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def geocode_entry(entry):
    """Attempt to geocode a single entry. Returns a geo dict or None.

    Uses the entry's display name and state. Skips entries with no state
    or that are tagged as private/federal/test/etc.
    """
    state = agency_state(entry)
    if not state:
        return None

    # Skip entries that shouldn't be geocoded
    if has_tag(entry, "federal"):
        return None
    if entry.get("agency_type") in ("federal", "test", "decommissioned", "private",
                                     "community", "other"):
        # Keep trying for "other" since some are real cities
        if entry.get("agency_type") != "other":
            return None

    name = agency_display_name(entry)
    role = entry.get("agency_role")
    kind = entry.get("agency_type")

    # Township / county-subdivision agencies
    # These are genuinely different from cities — Jackson Township != Jackson city
    if re.search(r"\b(Township|Twp)\b", name, re.IGNORECASE):
        cousub_name = extract_township_candidate(name)
        # Look for a parenthetical county hint in the name
        county_hint = None
        m = re.search(r"\(([^)]*County)\)", name)
        if m:
            c = lookup_county(m.group(1), state)
            if c:
                county_hint = c["fips"]
        if cousub_name:
            cousub = lookup_cousub(cousub_name, state, county_hint=county_hint)
            if cousub:
                return {
                    "kind": "cousub",
                    "fips": cousub["fips"],
                    "name": cousub["bare_name"],
                    "state": state,
                    "lat": cousub["lat"],
                    "lng": cousub["lng"],
                }
        # Don't fall through to place lookup — township != same-named city
        return None

    # County-level agencies (sheriff, DA, etc.) — always try county first
    # Strip role-suffix from name; the remainder is the county name
    if kind == "county" or role in ("sheriff", "da"):
        county_candidate = extract_place_candidate(name)  # strips role/state
        if county_candidate:
            county = lookup_county(county_candidate, state)
            if county:
                return {
                    "kind": "county",
                    "fips": county["fips"],
                    "name": county["bare_name"],
                    "state": state,
                    "lat": county["lat"],
                    "lng": county["lng"],
                }
        # Also try with explicit "County" suffix
        candidate = extract_county_candidate(name)
        if candidate:
            county = lookup_county(candidate, state)
            if county:
                return {
                    "kind": "county",
                    "fips": county["fips"],
                    "name": county["bare_name"],
                    "state": state,
                    "lat": county["lat"],
                    "lng": county["lng"],
                }
        # Don't fall through — county-semantic agency in a same-named place
        # is almost always wrong (e.g. "Sacramento DA" = county DA, not city)
        return None

    # Try as a place (city/town)
    candidate = extract_place_candidate(name)
    if candidate:
        place = lookup_place(candidate, state)
        if place:
            return {
                "kind": "place",
                "fips": place["fips"],
                "name": place["name"],
                "state": state,
                "lat": place["lat"],
                "lng": place["lng"],
            }

    # Fall back to county lookup for non-county-typed agencies (last resort)
    candidate = extract_county_candidate(name)
    if candidate:
        county = lookup_county(candidate, state)
        if county:
            return {
                "kind": "county",
                "fips": county["fips"],
                "name": county["bare_name"],
                "state": state,
                "lat": county["lat"],
                "lng": county["lng"],
            }

    return None


def needs_geocoding(entry):
    """True if entry has no geo block, or only state-only (can be upgraded)."""
    geo = entry.get("geo")
    if not geo:
        return True
    # state-only entries are eligible for upgrade to place/county
    if geo.get("kind") == "state-only":
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Geocode agencies via Census gazetteer")
    parser.add_argument("--apply", action="store_true", help="Write changes to registry")
    parser.add_argument("--slug", help="Geocode a single agency by slug")
    args = parser.parse_args()

    registry = json.loads(REGISTRY_PATH.read_text())

    matched = 0
    missed = 0
    matches = []

    for entry in registry:
        if args.slug and entry.get("slug") != args.slug:
            continue
        if not needs_geocoding(entry):
            continue

        geo = geocode_entry(entry)
        if geo:
            matched += 1
            matches.append((entry, geo))
            entry["geo"] = geo
        else:
            missed += 1

    print(f"Matched: {matched}")
    print(f"Missed:  {missed}")

    if matches:
        print("\nSample matches:")
        for entry, geo in matches[:20]:
            name = agency_display_name(entry)
            fips = geo.get("fips") or "?"
            print(f"  {name:<55} -> {geo['name']:<30} [{fips}] @ {geo['lat']:.4f}, {geo['lng']:.4f}")

    if args.apply and matched > 0:
        REGISTRY_PATH.write_text(json.dumps(registry, indent=2) + "\n")
        print(f"\nUpdated {matched} entries in {REGISTRY_PATH}")
    elif matched > 0:
        print(f"\nDry run. Use --apply to write changes.")


if __name__ == "__main__":
    main()
