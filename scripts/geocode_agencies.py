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
from gazetteer import lookup_place, lookup_county, lookup_cousub, lookup_state

REGISTRY_PATH = Path("assets/agency_registry.json")


# Patterns to strip from an agency name to get a place/county candidate
_AGENCY_SUFFIXES = re.compile(
    r"\s*\b(PD|SO|SD|DA|FD|DPS|Police Department|Police Services|"
    r"Division of Police|Sheriff'?s? Office|"
    r"Sheriff'?s? Department|Prosecutor'?s? Office|"
    r"District Attorney|Fire Department|"
    r"Department of Public Safety|"
    r"Public Safety|Parks Department|Marshal'?s? Office)\b\s*",
    re.IGNORECASE,
)

_STATE_TOKEN = re.compile(r"\s*\b[A-Z]{2}\b\s*")  # Strip " CA " etc.
_PARENS = re.compile(r"\s*\[[^\]]*\]\s*|\s*\([^)]*\)\s*")  # Strip "(CA)" and "[Inactive]"
_CITY_OF = re.compile(r"^(City|Town|Village|Borough)\s+of\s+", re.IGNORECASE)
# Leading "CA - Wasco PD" / "KS - Meade County SO" naming artifact.
# Main's version uses a simple ASCII hyphen; accepts an en-dash too
# for robustness.
_STATE_PREFIX = re.compile(r"^[A-Z]{2}\s*[-\u2013]\s*", re.IGNORECASE)
_DASH_SUFFIX = re.compile(r"\s*-\s*(original|[A-Z]{2})$", re.IGNORECASE)  # "Oxford PD - OH", "Harrah OK PD - original"
_ABBREV_CO = re.compile(r"\bCo\.", re.IGNORECASE)  # "Schuyler Co. IL SO"
_ABBREV_INTL = re.compile(r"\bIntl\b", re.IGNORECASE)  # "Nashville Intl Airport"
_SAINT_VARIANTS = re.compile(r"^Saint\s+", re.IGNORECASE)  # "Saint Johns AZ" -> "St. Johns AZ"

# State-level agency patterns (state police, highway patrol, DMV, etc.)
_STATE_AGENCY_PATTERN = re.compile(
    r"\b(State Patrol|State Police|Highway Patrol|Department of Public Safety|"
    r"Department of Motor Vehicles|State Highway Patrol|"
    r"Department of Conservation|Crime Analysis Center)\b",
    re.IGNORECASE,
)

# State names to USPS codes (for "Colorado State Patrol" etc.)
_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
    "nys": "NY",  # "NYS Crime Analysis Center"
}


def normalize_agency_name(name):
    """Normalize an agency name before extraction — strip brackets,
    state prefixes/suffixes, abbreviations."""
    # Strip [Inactive], [DEACTIVATED], (NFTA), etc.
    name = _PARENS.sub(" ", name)
    # Strip "XX - " state prefix at start
    name = _STATE_PREFIX.sub("", name)
    # Strip "- OH" or "- original" suffix
    name = _DASH_SUFFIX.sub(" ", name)
    # "Co." -> "County"
    name = _ABBREV_CO.sub("County", name)
    # "Intl" -> "International" (will be stripped by other patterns)
    name = _ABBREV_INTL.sub("International", name)
    # "Saint" -> "St." for gazetteer matching
    name = _SAINT_VARIANTS.sub("St. ", name)
    # Strip "Metro" suffix (Louisville Metro, Cumberland Metro) — usually denotes
    # a consolidated gov or metro area but the bare name is what we want
    name = re.sub(r"\s+Metro\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    return name

# Known name mismatches between common usage and Census place names.
# After the usual cleanup, if the extracted candidate matches a key
# here, the value gets used for the gazetteer lookup. Keep this list
# short and targeted — the registry also has a flock_names alias
# mechanism that can be used for trickier cases.
CITY_ALIASES = {
    "Carmel": "Carmel-by-the-Sea",  # Census has the hyphenated form
    "Angels Camp": "Angels",         # Census place is "Angels city"
}


def extract_place_candidate(agency_name):
    """Derive a candidate place name from an agency name.

    Examples:
      "San Mateo CA PD" -> "San Mateo"
      "City of Menifee CA PD" -> "Menifee"
      "Foster City CA PD" -> "Foster City"
      "Akron OH PD" -> "Akron"
      "Oxford PD - OH" -> "Oxford"
      "KS - Meade County SO" -> "Meade County"
      "CA - Wasco PD" -> "Wasco"
      "Carmel CA PD" -> "Carmel-by-the-Sea" (via CITY_ALIASES)
    """
    name = normalize_agency_name(agency_name)
    # Strip agency-role suffixes (PD, SO, DPS, etc.)
    name = _AGENCY_SUFFIXES.sub(" ", name)
    # Strip state codes (now standalone)
    name = _STATE_TOKEN.sub(" ", name)
    # Strip "City of" / "Town of" prefix
    name = _CITY_OF.sub("", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    # Apply targeted alias mapping last, so the cleaned candidate is
    # what we alias against (not the raw agency name).
    if name in CITY_ALIASES:
        name = CITY_ALIASES[name]
    return name


def is_state_level_agency(name):
    """Does this name describe a state-level agency (state patrol, etc.)?"""
    return bool(_STATE_AGENCY_PATTERN.search(name))


def infer_state_from_name(name):
    """Extract state USPS code from a name like 'Colorado State Patrol' or 'NYS ...'."""
    for word, usps in _STATE_NAMES.items():
        if re.search(rf"\b{re.escape(word)}\b", name, re.IGNORECASE):
            return usps
    return None


def extract_township_candidate(agency_name):
    """Derive a township candidate from an agency name.

    Examples:
      "Jackson Township OH PD (Stark County)" -> "Jackson"
      "Butler Township OH PD" -> "Butler"
      "Mahoning Twp PA PD" -> "Mahoning"
    """
    name = normalize_agency_name(agency_name)
    name = _AGENCY_SUFFIXES.sub(" ", name)
    name = _STATE_TOKEN.sub(" ", name)
    name = re.sub(r"\b(Township|Twp)\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def extract_county_candidate(agency_name):
    """Derive a candidate county name.

    Examples:
      "Alameda County CA SO" -> "Alameda"
      "Butte County CA SO" -> "Butte"
      "San Mateo County CA SO" -> "San Mateo"
      "Schuyler Co. IL SO" -> "Schuyler County"
      "St. John Parish LA SO" -> "St. John Parish"
    """
    name = normalize_agency_name(agency_name)
    # Only match if "County" / "Parish" / etc. appears
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
    name = agency_display_name(entry)
    state = agency_state(entry) or infer_state_from_name(name)
    if not state:
        return None

    # Skip entries that shouldn't be geocoded
    if has_tag(entry, "federal"):
        return None
    if entry.get("agency_type") in ("federal", "test", "decommissioned", "private",
                                     "community"):
        return None

    # State-level agencies (state patrol, highway patrol, etc.)
    # → point to state centroid
    if is_state_level_agency(name):
        s = lookup_state(state)
        if s:
            return {
                "kind": "state",
                "fips": s["state_fips"],
                "name": state,
                "state": state,
                "lat": s["lat"],
                "lng": s["lng"],
            }

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
        # Ambiguous township (multiple in state, no county hint) — fall back
        # to state centroid but mark as "ambiguous" to signal this is a local
        # agency whose specific location couldn't be determined.
        s = lookup_state(state)
        if s:
            return {
                "kind": "ambiguous",
                "fips": s["state_fips"],
                "name": state,
                "state": state,
                "lat": s["lat"],
                "lng": s["lng"],
                "note": "township name matches multiple county subdivisions in state",
            }
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
        # Fall back to county subdivisions — handles cases like
        # Scarborough ME (a town), Chippewa PA (a township) where the
        # agency doesn't explicitly say "Township" but the place is a cousub.
        cousub = lookup_cousub(candidate, state)
        if cousub:
            return {
                "kind": "cousub",
                "fips": cousub["fips"],
                "name": cousub["bare_name"],
                "state": state,
                "lat": cousub["lat"],
                "lng": cousub["lng"],
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


def _haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance between two points, in kilometers."""
    import math
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def main():
    parser = argparse.ArgumentParser(description="Geocode agencies via Census gazetteer")
    parser.add_argument("--apply", action="store_true", help="Write changes to registry")
    parser.add_argument("--slug", help="Geocode a single agency by slug")
    parser.add_argument(
        "--upgrade-manual",
        action="store_true",
        help=(
            "Also try to upgrade entries with geo.kind='manual' to place/county if "
            "the Census match falls within --manual-threshold-km of the existing "
            "manual lat/lng. Protects deliberate manual placements (federal "
            "buildings, colleges, tribal HQ, etc.) by requiring the match to be "
            "geographically close to the curated point."
        ),
    )
    parser.add_argument(
        "--manual-threshold-km",
        type=float,
        default=16.0,  # ~10 miles
        help=(
            "Full-upgrade threshold: match within this distance of the curated "
            "manual lat/lng fully replaces the entry's coords with the Census "
            "centroid."
        ),
    )
    parser.add_argument(
        "--manual-soft-threshold-km",
        type=float,
        default=150.0,  # county centroids can be ~130 km from county seat; tighter than that catches name collisions
        help=(
            "Soft-upgrade threshold: if the match is between --manual-threshold-km "
            "and this value, adopt the match's FIPS/kind/name but KEEP the curated "
            "manual lat/lng. Catches cases like San Francisco where the Census "
            "centroid sits offshore (Farallon Islands) and the curated point is "
            "the real HQ location. Matches beyond this distance are rejected as "
            "probable name collisions."
        ),
    )
    args = parser.parse_args()

    registry = json.loads(REGISTRY_PATH.read_text())

    matched = 0
    soft_matched = 0
    missed = 0
    matches = []
    soft_matches = []  # (entry, geo_with_preserved_coords, distance_km)
    # In --upgrade-manual mode we also track rejections where the
    # gazetteer DID match something but it was too far from the
    # curated manual coords. Useful for reviewing what we're choosing
    # not to auto-upgrade.
    manual_kept = []  # (entry, geo_match, distance_km)

    for entry in registry:
        if args.slug and entry.get("slug") != args.slug:
            continue

        current_geo = entry.get("geo") or {}
        is_manual_upgrade_candidate = (
            args.upgrade_manual and current_geo.get("kind") == "manual"
            and current_geo.get("lat") is not None and current_geo.get("lng") is not None
        )
        if not needs_geocoding(entry) and not is_manual_upgrade_candidate:
            continue

        geo = geocode_entry(entry)
        if not geo:
            missed += 1
            continue

        # For manual upgrades, apply one of three rules based on how
        # far the Census match is from the curated point:
        #   - < threshold_km            : FULL upgrade (use Census coords)
        #   - < soft_threshold_km       : SOFT upgrade (keep curated
        #                                 coords, adopt FIPS/kind/name)
        #   - >= soft_threshold_km      : SKIP (likely a name collision)
        if is_manual_upgrade_candidate:
            d = _haversine_km(
                current_geo["lat"], current_geo["lng"],
                geo["lat"], geo["lng"],
            )
            if d > args.manual_threshold_km and d <= args.manual_soft_threshold_km:
                # Soft upgrade — replace fips/kind/name but preserve
                # the curated lat/lng. Catches cases like SF where the
                # Census centroid is offshore (Farallon Islands).
                geo_soft = dict(geo)
                geo_soft["lat"] = current_geo["lat"]
                geo_soft["lng"] = current_geo["lng"]
                entry["geo"] = geo_soft
                soft_matches.append((entry, geo_soft, d))
                soft_matched += 1
                continue
            if d > args.manual_soft_threshold_km:
                manual_kept.append((entry, geo, d))
                missed += 1
                continue

        matched += 1
        matches.append((entry, geo))
        entry["geo"] = geo

    print(f"Matched: {matched}")
    if args.upgrade_manual:
        print(f"Soft-upgraded (kept curated coords): {soft_matched}")
    print(f"Missed:  {missed}")

    if matches:
        print("\nSample matches (full upgrade — replaced lat/lng with Census centroid):")
        for entry, geo in matches[:25]:
            name = agency_display_name(entry)
            fips = geo.get("fips") or "?"
            print(f"  {name:<55} -> {geo['name']:<30} [{fips}] @ {geo['lat']:.4f}, {geo['lng']:.4f}")

    if soft_matches:
        print("\nSoft-upgraded (adopted FIPS/kind/name; kept curated lat/lng):")
        for entry, geo, d in soft_matches[:25]:
            name = agency_display_name(entry)
            fips = geo.get("fips") or "?"
            print(f"  {name:<55} -> {geo['name']:<30} [{fips}] (Census centroid was {d:.1f} km off)")

    if args.upgrade_manual and manual_kept:
        print(f"\nSkipped {len(manual_kept)} manual entries (match was > {args.manual_soft_threshold_km} km — likely name collision):")
        for entry, geo, d in manual_kept[:25]:
            name = agency_display_name(entry)
            print(f"  {name:<55} (would match {geo['name']}, {d:.1f} km away)")

    total_changed = matched + soft_matched
    if args.apply and total_changed > 0:
        REGISTRY_PATH.write_text(json.dumps(registry, indent=2) + "\n")
        print(f"\nUpdated {total_changed} entries ({matched} full, {soft_matched} soft) in {REGISTRY_PATH}")
    elif total_changed > 0:
        print(f"\nDry run. Use --apply to write changes.")


if __name__ == "__main__":
    main()
