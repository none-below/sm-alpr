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
    r"Sheriff'?s? Department|Sheriff|"
    r"Prosecutor'?s? Office|Prosecutor|"
    r"District Attorney|Fire Department|"
    r"Department of Public Safety|Public Safety Dept\.?|"
    r"Dept\.? of Pub(?:lic)? Safety|"
    r"Public Safety|Parks Department|Marshal'?s? Office|"
    r"Constable|"
    r"Independent School District|"
    r"ISD|School District(?:\s+\d+)?|"
    r"International Airport|Regional Airport|Intl Airport|Airport|"
    r"Campus)\b\s*",
    re.IGNORECASE,
)

# Trailing decorative tags left after suffix stripping. "Inactive" tags
# deactivated agencies; "Insight" is a Flock product label appended to
# some customer names ("Troy University - Insight").
_TRAILING_DECORATIVE = re.compile(
    r"\s+(Inactive|Insight|Decommissioned)\s*$", re.IGNORECASE,
)

# Lone trailing "Dept" / "Department" left over after _AGENCY_SUFFIXES has
# already stripped the contextual phrase (e.g. "Cottage Grove Public Safety
# Dept" → "Cottage Grove Dept" → "Cottage Grove"). End-of-string only, to
# avoid stripping mid-name uses.
_TRAILING_DEPT = re.compile(r"\s+(Dept\.?|Department)\s*$", re.IGNORECASE)

_STATE_TOKEN = re.compile(r"\s*\b[A-Z]{2}\b\s*")  # Strip " CA " etc.
_PARENS = re.compile(r"\s*\[[^\]]*\]\s*|\s*\([^)]*\)\s*")  # Strip "(CA)" and "[Inactive]"
_CITY_OF = re.compile(r"^(City|Town|Village|Borough)\s+of\s+", re.IGNORECASE)
# Leading "CA - Wasco PD" / "KS - Meade County SO" naming artifact.
# Main's version uses a simple ASCII hyphen; accepts an en-dash too
# for robustness.
_STATE_PREFIX = re.compile(r"^[A-Z]{2}\s*[-\u2013]\s*", re.IGNORECASE)
# "Oxford PD - OH", "Harrah OK PD - original", "LaSalle Co. IL SO - New"
_DASH_SUFFIX = re.compile(r"\s*-\s*(original|new|old|[A-Z]{2})$", re.IGNORECASE)
# "Co"/"Co." → "County" only when followed by another 2-letter token *and*
# at least one more token after that. This distinguishes the County
# abbreviation ("Schuyler Co. IL SO", "Gasconade Co MO SO" — "Co" then
# state code then role) from the Colorado state code ("Aurora CO PD" —
# "CO" then role with nothing after).
_ABBREV_CO = re.compile(r"\bCo\.?(?=\s+[A-Z]{2}\b\s+\S)", re.IGNORECASE)
_ABBREV_INTL = re.compile(r"\bIntl\b", re.IGNORECASE)  # "Nashville Intl Airport"
# "Saint" → "St." anywhere in the name (Census uses "St."; Flock data
# sometimes spells it out, e.g. "South Saint Paul MN PD").
_SAINT_VARIANTS = re.compile(r"\bSaint\s+", re.IGNORECASE)
_MOUNT_VARIANTS = re.compile(r"\bMt\.\s+", re.IGNORECASE)  # "Mt. Zion" → "Mount Zion"

# State-level agency patterns (state police, highway patrol, DMV, etc.)
_STATE_AGENCY_PATTERN = re.compile(
    r"\b(State Patrol|State Police|State\s+(?:[A-Z]{2}\s+)?PD|Highway Patrol|"
    r"Department of Public Safety|Department of Motor Vehicles|"
    r"State Highway Patrol|Department of Conservation|"
    r"Crime Analysis Center|"
    r"(?:Department|Dept\.?) of Corrections|"
    r"(?:Department|Dept\.?) of Revenue|"
    r"(?:Department|Dept\.?) of Natural Resources|"
    r"Bureau of Investigation|"
    r"Information Analysis Center|"
    r"Financial Crimes Intelligence Center|"
    r"Division of Criminal Investigation|"
    r"Law Enforcement Agency|Law Enforcement Division|"
    r"Attorney General)\b",
    re.IGNORECASE,
)
# State-level agency abbreviations. "X Bureau of Investigation" is often
# carried in the registry only as the abbreviation (KBI, GBI, etc.).
_STATE_AGENCY_ABBREV = re.compile(
    r"\b(KBI|FDLE|TBI|GBI|SBI|CBI|BCI|MIAC|ALEA|SLED)\b"
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
    # "Co." or "Co" -> "County"
    name = _ABBREV_CO.sub("County", name)
    # "Intl" -> "International" (will be stripped by other patterns)
    name = _ABBREV_INTL.sub("International", name)
    # "Saint" -> "St." anywhere; "Mt." -> "Mount" (Census uses these forms)
    name = _SAINT_VARIANTS.sub("St. ", name)
    name = _MOUNT_VARIANTS.sub("Mount ", name)
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
    "Depue": "De Pue",               # Census place is "De Pue village"
}

# State-aware aliases. Disambiguates names that need different remappings
# depending on the state (e.g. "Lexington" exists in many states but only
# the KY one is the consolidated "Lexington-Fayette" gov).
#
# University-campus entries map a normalized university token to the
# city where the main campus sits — extract_place_candidate runs the
# alias *after* role/state stripping, so the bare institution name is
# what matches here.
STATE_CITY_ALIASES = {
    ("Lexington", "KY"): "Lexington-Fayette",
    ("Metropolitan Washington", "DC"): "Washington",

    # Consolidated city-county Census names that don't accept the bare
    # form on lookup.
    ("Indianapolis", "IN"): "Indianapolis city (balance)",
    ("Athens", "GA"): "Athens-Clarke County unified government (balance)",

    # University / college campuses (single-campus institutions whose
    # name doesn't contain the campus city). Targets are Census place
    # names, except where noted as a county subdivision (cousub) — those
    # rely on the cousub fallback in geocode_entry.
    ("University of Georgia", "GA"): "Athens-Clarke County unified government (balance)",
    ("University of Kentucky", "KY"): "Lexington-Fayette",
    ("University of Louisville", "KY"): "Louisville",
    ("University of Memphis", "TN"): "Memphis",
    ("University of Tennessee", "TN"): "Knoxville",
    ("University of Tennessee Health Science Center Memphis", "TN"): "Memphis",
    ("University of South Florida", "FL"): "Tampa",
    ("University of West Georgia", "GA"): "Carrollton",
    ("University of North Georgia", "GA"): "Dahlonega",
    ("University of Central Arkansas", "AR"): "Conway",
    ("University of Oklahoma Health Science Center", "OK"): "Oklahoma City",
    ("Emory University", "GA"): "Atlanta",
    ("Florida State University", "FL"): "Tallahassee",
    ("Wichita State University", "KS"): "Wichita",
    ("Midwestern State University", "TX"): "Wichita Falls",
    ("Southern Illinois University", "IL"): "Carbondale",
    ("Georgia Southwestern State University", "GA"): "Americus",
    ("Georgia Piedmont Technical College", "GA"): "Clarkston",
    ("Itawamba Community College", "MS"): "Fulton",
    ("Walters State Community College", "TN"): "Morristown",
    ("Prairie View A & M University", "TX"): "Prairie View",
    ("Troy University", "AL"): "Troy",
    ("UNC Asheville", "NC"): "Asheville",
    # UMMC normalize_agency_name strips parens, leaving "UMMC MS PD" — so
    # the candidate after suffix-strip is "UMMC".
    ("UMMC", "MS"): "Jackson",

    # Multi-city / colloquial city names.
    ("Lakeside Park-Crestview Hills", "KY"): "Lakeside Park",  # adjacent KY cities; pick larger
    ("Middleboro", "MA"): "Middleborough",  # cousub; falls through to lookup_cousub
    ("Marta", "GA"): "Atlanta",  # MARTA is the Atlanta transit authority
    ("Wyandotte Nation", "OK"): "Wyandotte",
    ("Port of Seattle", "WA"): "Seattle",
}


def extract_place_candidate(agency_name, state=None):
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
      "Lexington KY PD" -> "Lexington-Fayette" (via STATE_CITY_ALIASES, when state=KY)
    """
    name = normalize_agency_name(agency_name)
    # Strip agency-role suffixes (PD, SO, DPS, etc.)
    name = _AGENCY_SUFFIXES.sub(" ", name)
    # Strip trailing decorative tags ("Inactive", "Insight") so they
    # don't bleed into the place candidate.
    name = _TRAILING_DECORATIVE.sub("", name)
    # Strip state codes (now standalone)
    name = _STATE_TOKEN.sub(" ", name)
    # Strip "City of" / "Town of" prefix
    name = _CITY_OF.sub("", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    # Strip a lone trailing "Dept" / "Department" left over from compound
    # phrases like "X Public Safety Dept" where _AGENCY_SUFFIXES took the
    # contextual part but left the standalone word.
    name = _TRAILING_DEPT.sub("", name).strip()
    # Re-strip in case the candidate ends with a decorative tag that was
    # only exposed after the agency suffix came off.
    name = _TRAILING_DECORATIVE.sub("", name).strip()
    # Strip dashes that have whitespace on at least one side, or that hang
    # off the start/end of the string. These are artifacts from messy names
    # like "Windsor- IL- PD" or "X - " separators left after suffix stripping.
    # Internal hyphens like "Carmel-by-the-Sea" have no adjacent whitespace
    # and are preserved.
    name = re.sub(r"\s+-\s*|\s*-\s+|^-+\s*|\s*-+$", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Apply targeted alias mapping last, so the cleaned candidate is
    # what we alias against (not the raw agency name).
    if state and (name, state) in STATE_CITY_ALIASES:
        name = STATE_CITY_ALIASES[(name, state)]
    elif name in CITY_ALIASES:
        name = CITY_ALIASES[name]
    return name


# Match a single capital-word immediately before County/Parish/etc.
# Multi-word counties (St. Louis, Salt Lake) usually appear adjacent to
# a county-suffix agency name and are handled by the primary path.
_COUNTY_WINDOW = re.compile(
    r"\b([A-Z][\w.'\-]+)\s+(County|Parish|Borough|Census Area)\b",
)


def extract_county_window(agency_name):
    """Find an "X County" / "X Parish" substring anywhere in the name.

    Useful for special-purpose county-level agencies where the county
    name is buried in the middle of the agency name:

      "Forest Preserve District Will County PD (IL)" -> "Will County"
      "Lake County Forest Preserves IL PD"           -> "Lake County"
      "Grundy County IL 911"                          -> "Grundy County"
      "Whitley County IN Prosector"                   -> "Whitley County"
      "Racine County WI Communications Center"        -> "Racine County"
    """
    norm = normalize_agency_name(agency_name)
    norm = _STATE_TOKEN.sub(" ", norm)
    m = _COUNTY_WINDOW.search(norm)
    if not m:
        return None
    return f"{m.group(1).strip()} {m.group(2)}"


def extract_university_campus(agency_name):
    """Extract a campus city from a multi-campus university name.

    Splits on " - " / " at " / "- " and returns the rightmost segment
    after the "University of …" stem. Returns None for single-campus
    universities, where no separator is present.

      "University of Wisconsin - Madison WI PD"        -> "Madison"
      "University of Illinois - Chicago IL PD"          -> "Chicago"
      "University of Illinois at Urbana-Champaign IL PD"-> "Urbana-Champaign"
      "University of Illinois- Springfield"             -> "Springfield"
      "University of Minnesota MN PD (Twin Cities)"     -> None  (no separator)
      "Grand Valley State University MI"                -> None  (not "University of …")
    """
    name = normalize_agency_name(agency_name)
    name = _AGENCY_SUFFIXES.sub(" ", name)
    name = _STATE_TOKEN.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not re.match(r"^University of\b", name, re.IGNORECASE):
        return None
    for sep_re in (r"\s+at\s+", r"\s*[-–]\s*"):
        parts = re.split(sep_re, name, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            campus = parts[1].strip()
            if campus and not re.match(r"^University\b", campus, re.IGNORECASE):
                return campus
    return None


def is_state_level_agency(name):
    """Does this name describe a state-level agency (state patrol, etc.)?"""
    return bool(_STATE_AGENCY_PATTERN.search(name)) or bool(_STATE_AGENCY_ABBREV.search(name))


# Match "<Word> CO <STATE>" where CO is the County abbreviation and the
# trailing 2-letter token is the actual state. Some registry rows
# ("Caldwell CO TX SO", "Reeves CO TX SO") have this shape and ended up
# stored with state="CO" because the slug parser took CO as Colorado.
# Used to override the entry's state during geocoding.
_CO_COUNTY_STATE = re.compile(r"\b[A-Z][\w]+\s+CO\s+([A-Z]{2})\b")


_ROLE_SUFFIX_TOKENS = frozenset(
    {"PD", "SO", "DA", "DPS", "FD", "EMS", "OEM", "OES"}
)


def detect_county_co_state(name):
    """If name has 'X CO YY' pattern (CO=County), return YY.
    Returns None when no match, when YY is itself 'CO', or when YY is a
    role suffix (e.g. 'Aurora CO PD' is Aurora, Colorado — not a county)."""
    m = _CO_COUNTY_STATE.search(name)
    if not m:
        return None
    code = m.group(1)
    if code == "CO" or code in _ROLE_SUFFIX_TOKENS:
        return None
    return code


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


# Hand-curated geo blocks for entries that the Census-based geocoder
# can't reasonably resolve (tribal HQs, regional drug task forces,
# private hospital systems, airports without an inferable state, TX
# CDPs the Census places gazetteer doesn't index, etc.). Keyed by
# agency_id; the geocoder applies these before falling back to the
# Census pipeline. Entries whose location can't be confidently pinned
# down (e.g. "OK - 477", "Elm Ridge TX PD", multi-county precincts
# without a county hint) are deliberately omitted — they stay flagged
# in the CI recipient-coords check until someone narrows them down.
MANUAL_OVERRIDES = {
    # 18th Judicial District DTF — Sumner County, TN; based in Gallatin.
    "c9811955-3e45-5ff9-bd62-25a4f54afa6a": {
        "kind": "manual", "name": "Gallatin", "state": "TN",
        "lat": 36.388, "lng": -86.450,
    },
    # Baylor Scott & White Health PD — Dallas-Fort Worth HQ.
    "bc36858b-ad3b-59d8-84ec-719a6c34de87": {
        "kind": "manual", "name": "Dallas", "state": "TX",
        "lat": 32.7767, "lng": -96.7970,
    },
    # CSX Railroad PD — corporate HQ Jacksonville, FL.
    "ddad36ac-3cf9-58aa-b0c4-ec087280450b": {
        "kind": "manual", "name": "Jacksonville", "state": "FL",
        "lat": 30.3322, "lng": -81.6557,
    },
    # Chatham-Savannah Counter Narcotics Team — Savannah/Chatham County, GA.
    "fec2b9a0-ee16-5d0a-948b-0b58ed4d1c48": {
        "kind": "manual", "name": "Savannah", "state": "GA",
        "lat": 32.0809, "lng": -81.0912,
    },
    # Chehalis Tribe Law Enforcement — Confederated Tribes of the
    # Chehalis Reservation HQ in Oakville, WA.
    "a2c33b5c-16a8-52e3-8c8a-5be75694d3ed": {
        "kind": "manual", "name": "Oakville", "state": "WA",
        "lat": 46.8482, "lng": -123.2293,
    },
    # Columbia River DTF — covers Cowlitz County WA; based in Longview.
    "8661b49d-944f-56c2-924b-decfbca1a0ef": {
        "kind": "manual", "name": "Longview", "state": "WA",
        "lat": 46.1382, "lng": -122.9382,
    },
    # Cypress-Fairbanks ISD PD — Cypress, TX (NW Harris County). Census
    # places gazetteer doesn't index the Cypress CDP.
    "ff35f6c5-112e-5970-b59f-47dcef6947e5": {
        "kind": "manual", "name": "Cypress", "state": "TX",
        "lat": 29.9691, "lng": -95.6972,
    },
    # Huffman ISD PD — Huffman, TX (NE Harris County CDP, not in
    # Census places gazetteer).
    "fe2ade2a-15d6-5074-8593-c2f5c8f2c590": {
        "kind": "manual", "name": "Huffman", "state": "TX",
        "lat": 30.0152, "lng": -95.0919,
    },
    # Gwinnett County School PD — Gwinnett County, GA. Registry has no
    # state set so the standard county branch can't fire.
    "4c431642-2f5c-5c7c-9c69-6587fff1becf": {
        "kind": "manual", "name": "Gwinnett County", "state": "GA",
        "lat": 33.9588, "lng": -84.0257,
    },
    # Louisville Muhammad Ali International Airport PD — Louisville KY.
    "8383db21-74c6-5054-a618-f23bc75390ac": {
        "kind": "manual", "name": "Louisville", "state": "KY",
        "lat": 38.1744, "lng": -85.7361,
    },
    # Memphis International Airport (MEM) — Memphis, TN.
    "d5100c71-2e81-53cc-9c6c-d96e89abd32d": {
        "kind": "manual", "name": "Memphis", "state": "TN",
        "lat": 35.0424, "lng": -89.9767,
    },
    # Methodist Health System PD — Dallas, TX (system HQ).
    "1d8cb468-cbbb-535f-a315-a79da4dd4f16": {
        "kind": "manual", "name": "Dallas", "state": "TX",
        "lat": 32.7457, "lng": -96.8285,
    },
    # Mineral Area Drug Task Force — covers St. Francois / Iron / etc.;
    # based in Park Hills, MO.
    "28893c8b-2b9b-5d85-a0a8-6a404cdab8b0": {
        "kind": "manual", "name": "Park Hills", "state": "MO",
        "lat": 37.8553, "lng": -90.5178,
    },
    # Montgomery County Constable — Montgomery County, TX (Conroe).
    # The registry has no state; multiple states have a Montgomery
    # County, but in context (Cabot AR PD shares with TX agencies and
    # the Constable role is a Texas-specific institution) TX is the
    # safe call.
    "a876b7c8-45b2-53a9-a8a6-460d19bdfc51": {
        "kind": "manual", "name": "Conroe", "state": "TX",
        "lat": 30.3119, "lng": -95.4561,
    },
    # Northwest Arkansas Regional Airport (XNA) — Highfill, AR (serves
    # Bentonville/Fayetteville).
    "ee18a416-bc7f-5307-83e7-cfc14adac657": {
        "kind": "manual", "name": "Bentonville", "state": "AR",
        "lat": 36.2819, "lng": -94.3068,
    },
    # Orleans Levee District PD — New Orleans, LA.
    "bf9a43ef-94e7-5ca0-84b3-ca2e1d64d8a0": {
        "kind": "manual", "name": "New Orleans", "state": "LA",
        "lat": 29.9511, "lng": -90.0715,
    },
    # Ozarks Drug Enforcement Team — Greene County, MO; Springfield.
    "b6e5f1da-fdb5-5b48-95b9-18c3137f2545": {
        "kind": "manual", "name": "Springfield", "state": "MO",
        "lat": 37.2090, "lng": -93.2923,
    },
    # Port Fourchon Harbor PD — Port Fourchon, LA.
    "2be084dc-fee8-51fe-b1f4-5acbd9d6da03": {
        "kind": "manual", "name": "Port Fourchon", "state": "LA",
        "lat": 29.1067, "lng": -90.1928,
    },
    # SEMO Drug Task Force — Southeast Missouri; based in Cape Girardeau.
    "23e43879-4b61-5c96-8abd-488f90536244": {
        "kind": "manual", "name": "Cape Girardeau", "state": "MO",
        "lat": 37.3059, "lng": -89.5181,
    },
    # South Central Missouri DTF — Texas County, MO; HQ in Houston, MO.
    "13052a9b-73e7-54ac-8289-6818f7a23de0": {
        "kind": "manual", "name": "Houston", "state": "MO",
        "lat": 37.3239, "lng": -91.9557,
    },
    # Southern Regional PD — York County PA, based in Shrewsbury.
    "07ccfe8f-7915-59fd-b090-280305bdee32": {
        "kind": "manual", "name": "Shrewsbury", "state": "PA",
        "lat": 39.7666, "lng": -76.6797,
    },
    # Spokane Tribal PD — Spokane Indian Reservation HQ in Wellpinit, WA.
    "3fb2e1f2-434a-5a2b-b79e-64c927bc461e": {
        "kind": "manual", "name": "Wellpinit", "state": "WA",
        "lat": 47.8907, "lng": -118.0651,
    },
    # Tennessee Regional Organized Crime Information Center — Nashville HQ.
    "c745e651-a0bd-543e-a69c-2a9741b766de": {
        "kind": "manual", "name": "Nashville", "state": "TN",
        "lat": 36.1627, "lng": -86.7816,
    },
    # Tupelo Regional Airport (TUP) — Tupelo, MS.
    "e0905266-5997-5468-a68a-55abc5e49065": {
        "kind": "manual", "name": "Tupelo", "state": "MS",
        "lat": 34.2581, "lng": -88.7037,
    },
}


def geocode_entry(entry):
    """Attempt to geocode a single entry. Returns a geo dict or None.

    Uses the entry's display name and state. Skips entries with no state
    or that are tagged as private/federal/test/etc.
    """
    aid = entry.get("agency_id")
    if aid in MANUAL_OVERRIDES:
        return dict(MANUAL_OVERRIDES[aid])

    name = agency_display_name(entry)
    state = agency_state(entry) or infer_state_from_name(name)
    # "X CO YY" naming (CO=County abbrev) overrides any state inferred
    # from the entry, since the registry sometimes mistakenly records
    # CO as Colorado for these.
    co_override = detect_county_co_state(name)
    if co_override:
        state = co_override
    if not state:
        return None

    # Skip entries that shouldn't be geocoded
    if has_tag(entry, "federal"):
        return None
    if entry.get("agency_type") in ("federal", "test", "decommissioned", "private"):
        return None
    # type=community covers HOAs but also some misclassified incorporated
    # places (e.g. "MI - Village of Middleville" tagged community/hoa).
    # Allow them to try place lookup; if no match, return None below.

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
        county_candidate = extract_place_candidate(name, state)  # strips role/state
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
        # Window extraction: pull "X County" out of the middle of the name.
        # Handles special-purpose county agencies like "Forest Preserve
        # District Will County PD" or "Grundy County 911".
        windowed = extract_county_window(name)
        if windowed:
            county = lookup_county(windowed, state)
            if county:
                return {
                    "kind": "county",
                    "fips": county["fips"],
                    "name": county["bare_name"],
                    "state": state,
                    "lat": county["lat"],
                    "lng": county["lng"],
                }
        # Mistyped city PDs: a few entries are tagged type=county/role=sheriff
        # but the agency name explicitly says " PD" (e.g. "Sioux Falls SD PD").
        # In those cases fall through to a place lookup. Restricted to names
        # containing " PD" to avoid breaking e.g. "Sacramento DA" cases where
        # the city/county name collision would mis-map a county DA to a city.
        if kind == "county" and role == "sheriff" and re.search(r"\bPD\b", name):
            place_candidate = extract_place_candidate(name, state)
            if place_candidate:
                place = lookup_place(place_candidate, state)
                if place:
                    return {
                        "kind": "place",
                        "fips": place["fips"],
                        "name": place["name"],
                        "state": state,
                        "lat": place["lat"],
                        "lng": place["lng"],
                    }
        return None

    # University with named campus — extract the campus city before
    # falling through to the generic place candidate (which would just
    # see "University of Wisconsin - Madison" and miss).
    if kind == "university":
        campus = extract_university_campus(name)
        if campus:
            place = lookup_place(campus, state)
            if place:
                return {
                    "kind": "place",
                    "fips": place["fips"],
                    "name": place["name"],
                    "state": state,
                    "lat": place["lat"],
                    "lng": place["lng"],
                }
            # Two-city campuses ("Urbana-Champaign", "Tri-Cities") often
            # appear as a hyphenated pair. Try the first half.
            if "-" in campus:
                first = campus.split("-", 1)[0].strip()
                place = lookup_place(first, state)
                if place:
                    return {
                        "kind": "place",
                        "fips": place["fips"],
                        "name": place["name"],
                        "state": state,
                        "lat": place["lat"],
                        "lng": place["lng"],
                    }

    # Try as a place (city/town)
    candidate = extract_place_candidate(name, state)
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

    # State agencies (Department of Corrections, Information Analysis Center,
    # etc.) with no specific city — point at the state centroid as a final
    # fallback. The named-pattern check at the top handles most of these,
    # but type=state catches the rest by classification rather than name.
    if kind == "state":
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
                # manual_coords=True tells downstream code (and the
                # geo-cache tests) that the coords here intentionally
                # diverge from the FIPS centroid; don't try to
                # validate them against the gazetteer.
                geo_soft = dict(geo)
                geo_soft["lat"] = current_geo["lat"]
                geo_soft["lng"] = current_geo["lng"]
                geo_soft["manual_coords"] = True
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
