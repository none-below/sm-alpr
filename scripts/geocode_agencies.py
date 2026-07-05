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
    # Other single-campus universities (main campus is in a single named
    # city). Add here when a university entry shows up missing coordinates
    # in scripts/check_recipient_coords.py.
    ("App State University", "NC"): "Boone",
    ("Augusta University", "GA"): "Augusta",
    ("Ball State University", "IN"): "Muncie",
    ("Buffalo State University", "NY"): "Buffalo",
    ("Case Western Reserve University", "OH"): "Cleveland",
    ("Cornell University", "NY"): "Ithaca",
    ("Elon University", "NC"): "Elon",
    ("Fairmont State University", "WV"): "Fairmont",
    ("Hampton University", "VA"): "Hampton",
    ("Indiana University", "IN"): "Bloomington",
    ("Lewis University", "IL"): "Romeoville",
    ("Lincoln Land Community College", "IL"): "Springfield",
    ("Lincoln University", "MO"): "Jefferson City",
    ("Ohio State University", "OH"): "Columbus",
    ("Parkland College", "IL"): "Champaign",
    ("Purdue University", "IN"): "West Lafayette",
    ("Rowan University", "NJ"): "Glassboro",
    ("Temple University", "PA"): "Philadelphia",
    ("Vincennes University", "IN"): "Vincennes",
    ("Virginia Tech University", "VA"): "Blacksburg",
    ("Western Michigan University", "MI"): "Kalamazoo",
    # Indianapolis is a consolidated city-county; the bare "Indianapolis"
    # lookup doesn't match the Census name. Pre-resolve to the balance entry.
    ("University of Indianapolis", "IN"): "Indianapolis city (balance)",
    ("University of Toledo", "OH"): "Toledo",
    # Consolidated city-county PD names where the bare city is the Census place.
    ("Charlotte Mecklenburg", "NC"): "Charlotte",

    # Multi-city / colloquial city names.
    ("Lakeside Park-Crestview Hills", "KY"): "Lakeside Park",  # adjacent KY cities; pick larger
    ("Middleboro", "MA"): "Middleborough",  # cousub; falls through to lookup_cousub
    ("Marta", "GA"): "Atlanta",  # MARTA is the Atlanta transit authority
    ("Wyandotte Nation", "OK"): "Wyandotte",
    ("Port of Seattle", "WA"): "Seattle",

    # Single-campus colleges/universities → campus city (issue #447).
    ("ASU", "AZ"): "Tempe",
    ("Central Connecticut State University", "CT"): "New Britain",
    ("Guilford Technical Community College", "NC"): "Jamestown",
    ("Houston Christian University", "TX"): "Houston",
    ("Ithaca College", "NY"): "Ithaca",
    ("Kellogg Community College", "MI"): "Battle Creek",
    ("Lamar University", "TX"): "Beaumont",
    ("Northern Arizona University", "AZ"): "Flagstaff",
    ("College of Mount St. Joseph", "OH"): "Cincinnati",
    ("Oklahoma City Community College", "OK"): "Oklahoma City",
    ("Prince George's Community College", "MD"): "Largo",
    ("Rockhurst University", "MO"): "Kansas City",
    ("SUNY Binghamton", "NY"): "Binghamton",
    ("SUNY-Onondaga Community College", "NY"): "Syracuse",
    ("SUNY Old Westbury", "NY"): "Old Westbury",
    ("Southwestern Illinois College", "IL"): "Belleville",
    ("Springfield College", "MA"): "Springfield",
    ("Alvin College", "TX"): "Alvin",
    ("Lee College", "TX"): "Baytown",
    ("Tarleton State University", "TX"): "Stephenville",
    ("Texas A&M International University", "TX"): "Laredo",
    ("Texas Christian University", "TX"): "Fort Worth",
    ("University Circle", "OH"): "Cleveland",
    ("University of Arizona", "AZ"): "Tucson",
    ("University of Delaware", "DE"): "Newark",
    ("University of Houston Clear Lake", "TX"): "Houston",
    ("University of Houston Downtown", "TX"): "Houston",
    ("University of Michigan Ann Arbor", "MI"): "Ann Arbor",
    ("University of North Carolina Chapel Hill", "NC"): "Chapel Hill",
    ("University of North Carolina Charlotte", "NC"): "Charlotte",
    ("University of Notre Dame", "IN"): "South Bend",
    ("University of Texas Permian Basin", "TX"): "Odessa",
    ("George Mason University", "VA"): "Fairfax",
    ("Weatherford College", "TX"): "Weatherford",

    # City-name spelling / suffix variants (issue #447). Census place
    # names differ from the spelling Flock carries.
    ("Desoto", "MO"): "De Soto",
    ("Laporte", "IN"): "La Porte",
    ("Gun Barrel", "TX"): "Gun Barrel City",
    ("Horizon", "TX"): "Horizon City",
    ("Town & Country", "MO"): "Town and Country",
    ("Snowflake-Taylor", "AZ"): "Snowflake",   # twin towns; pick Snowflake
    ("Hobart-Lawrence", "WI"): "Hobart",       # twin villages; pick Hobart
    ("Mechanicsburg/Buffalo", "IL"): "Mechanicsburg",
    ("Mentor on the Lake", "OH"): "Mentor-on-the-Lake",
    ("Moorland Hills", "OH"): "Moreland Hills",
    ("LaGrange", "KY"): "La Grange",
    ("LaGrange Park", "IL"): "La Grange Park",
    ("North Attleboro", "MA"): "North Attleborough",
    ("Bloomfield", "MI"): "Bloomfield Hills",
    ("The City of The Village", "OK"): "The Village",
    ("Inc Village of Lake Success", "NY"): "Lake Success",
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
# down (e.g. "OK - 477", "Elm Ridge TX PD", "Alabama Drug Enforcement
# Task Force Region G", multi-county TX precincts without a county
# hint) are deliberately omitted — they stay flagged in the CI
# recipient-coords check until someone narrows them down.
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
    # GA - MCS/OCU/Intelligence — Marietta-Cobb-Smyrna Narcotics Unit;
    # joint task force led by Marietta PD in Cobb County, GA.
    "132123cb-eb61-5dc5-be29-ab9fd39fefde": {
        "kind": "manual", "name": "Marietta", "state": "GA",
        "lat": 33.9526, "lng": -84.5499,
    },
    # UCPAO AR — Union County Prosecuting Attorney's Office (13th
    # Judicial District), 307 American Road, El Dorado, AR.
    "de190cd9-abf6-542a-94c1-eea708ddd05e": {
        "kind": "manual", "name": "El Dorado", "state": "AR",
        "lat": 33.2076, "lng": -92.6663,
    },
    # SE Missouri State University — Cape Girardeau, MO. Manual override
    # because the geocoder's _STATE_TOKEN strips the "SE" prefix as a
    # 2-letter state code, collapsing this to "Missouri State University"
    # (a different school in Springfield, MO).
    "b29798ee-748e-5b57-9e49-7f87fcc992d8": {
        "kind": "manual", "name": "Cape Girardeau", "state": "MO",
        "lat": 37.3059, "lng": -89.5181,
    },
    # Dallas/Fort Worth International Airport PD — the airport straddles
    # Irving/Grapevine/Euless; no single Census place centroid fits, so
    # pin to the airport itself (issue #447).
    "95421760-604f-57cd-88e7-40055f0e1529": {
        "kind": "manual", "name": "DFW Airport", "state": "TX",
        "lat": 32.8998, "lng": -97.0403,
    },
    # Northern Regional PD (Allegheny County PA) — HQ in Wexford, a CDP
    # the Census places gazetteer doesn't index; serves the northern
    # Allegheny suburbs (issue #447).
    "08440ba2-9d22-5fef-8f08-bec451284e95": {
        "kind": "manual", "name": "Wexford", "state": "PA",
        "lat": 40.6470, "lng": -80.0590,
    },
    # Montgomery County Constable Pct 4 — office at 21130 US-59,
    # New Caney, TX (unincorporated; not a census place). The account
    # name is a comma-split fragment of "Montgomery County Constable,
    # Precinct 4 (TX)" (issue #636).
    "c555da83-0c47-5b2b-8988-5a09bf3ef980": {
        "kind": "manual", "name": "New Caney", "state": "TX",
        "lat": 30.1395, "lng": -95.2171,
    },
}


# Gazetteer-resolved overrides for agencies whose physical location is a
# Census place/county/cousub but whose *name* can't be auto-extracted
# (hospital systems, park districts, transit authorities, ISDs, regional
# task forces, state-agency HQ cities, etc.). Keyed by agency_id; resolved
# against the gazetteer at geocode time so the coords stay in sync (and are
# validated by tests/test_geo_cache.py) rather than being frozen here.
# Value is (kind, name, state) or (kind, name, state, county_hint) where
# kind is "place" | "county" | "cousub". Entries whose location genuinely
# can't be pinned down (multi-county task forces with no base, bare TX
# constable precincts, cryptic portal names) are deliberately omitted —
# they stay flagged by scripts/check_recipient_coords.py.
GAZ_OVERRIDES = {
    # place overrides (issue #447)
    "bb9193bd-a05e-5a85-8442-0500a92e5345": ("place", "Johnson City", "TN"),  # 1st Judicial District DTF
    "10319f36-e697-550c-8201-d4b1aa641813": ("place", "Livingston", "TX"),  # Alabama-Coushatta Tribe
    "04cea6da-d564-5393-9c2b-e6954ab126d7": ("place", "Boone", "NC"),  # Appalachian Regional Healthcare System
    "578144cb-7f6b-5893-8d43-0fd67b01543b": ("place", "Baldwinsville", "NY"),  # Baldwinsville Central School District
    "fcdbe24d-aac3-5056-8085-e6ee55c61448": ("place", "Chicago Heights", "IL"),  # Bloom Township HS District 206
    "a94e8041-ab2e-58e7-90e8-09218338b9dd": ("place", "Syracuse", "NY"),  # CAC - Central NY
    "d9b3e9f0-3081-5459-b234-39e4bc23ee0d": ("place", "Buffalo", "NY"),  # CAC - Erie
    "d803558a-de4c-56cd-8e59-ea5e06e7bc93": ("place", "Goshen", "NY"),  # CAC - Hudson Valley
    "2f033355-e355-5db0-b779-0f1f2b45cc2b": ("place", "Utica", "NY"),  # CAC - Mohawk Valley
    "af4247e1-9428-5fff-bbeb-de37252f7e13": ("place", "Rochester", "NY"),  # CAC - Monroe
    "e790abe7-04a0-5480-9b1f-2a6f75d4ad65": ("place", "Niagara Falls", "NY"),  # CAC - Niagara
    "e5a4e334-e976-5d15-a5ca-5ddfb0d324ff": ("place", "Malone", "NY"),  # CAC - North Country
    "7e6433bc-b57a-54b6-b375-fb838605e473": ("place", "Binghamton", "NY"),  # CAC - Southern Tier
    "1fd26e6a-2b09-53ac-8f8e-fe4226969049": ("place", "Homewood", "IL"),  # CN Railroad PD (US HQ)
    "1e65fcf9-b399-5e54-9cd4-6f68c9824fa8": ("place", "Rock Hill", "SC"),  # Catawba Nation
    "112cf1f2-b628-5a03-b02a-63169d752db1": ("place", "Cleveland", "OH"),  # Cleveland Clinic
    "2f781e64-cdac-50f8-87b5-907c9049ed95": ("place", "Cleveland", "OH"),  # Cleveland Metro Schools
    "4f49b561-9ed3-5576-9576-145702229258": ("place", "Cleveland", "OH"),  # Cleveland Metroparks
    "fb1b3ad0-6321-5316-8995-1309ed1f2e3d": ("place", "Beeville", "TX"),  # Coastal Bend College
    "74a9fbc7-1d82-5628-aee5-e2938510d86c": ("place", "West Columbia", "SC"),  # Columbia Metro Airport
    "14068524-23ad-5f61-be7a-ed7de5135595": ("place", "Columbus", "OH"),  # Columbus Regional Airport Authority
    "7b710523-06a2-5f2e-948e-41a18aa58fec": ("place", "Indianapolis city (balance)", "IN"),  # Community Health Network
    "27bbf971-facc-5637-8b79-7d2d16893da1": ("place", "Coulee Dam", "WA"),  # Coulee Dam PD
    "76010eb9-473b-5f1d-84fe-5329196b72ba": ("place", "Dallas", "TX"),  # Dallas Area Rapid Transit
    "bd9b53d4-d38f-50b9-90d1-15684578c915": ("place", "Greenville", "NC"),  # ECU Health
    "28bf3b02-2dcd-5e12-98e0-0a5f3ea066a1": ("place", "Homewood", "IL"),  # ECom911 (south Cook County dispatch)
    "7071c666-10c3-5626-9d40-7899bbaad3df": ("place", "Tyler", "TX"),  # East Texas Auto Theft Task Force
    "54d1bc80-a170-513b-861c-79400c9cb8c1": ("place", "New Oxford", "PA"),  # Eastern Adams Regional PD
    "9dc99f88-1f3e-572a-8f92-0c12d47f9b8f": ("place", "Piqua", "OH"),  # Edison State Community College
    "16899400-f5d1-5e53-b38c-9890dc1d9048": ("place", "Dayton", "OH"),  # Five Rivers MetroParks
    "f740adf6-7787-5a57-959c-83eabad5d032": ("place", "East Peoria", "IL"),  # Fon du Lac Park District
    "efc5c6ce-a8ae-5855-8764-bb4ef22e6e05": ("place", "Cleveland", "OH"),  # Greater Cleveland RTA
    "994cafa6-360c-5767-b9bb-1778b2182bc7": ("place", "Midland", "TX"),  # Greenwood ISD
    "0bacfde0-4cc1-5aa2-8959-c24fc15e98c6": ("place", "Harrisburg", "PA"),  # Harrisburg Bureau of Police
    "46448aa2-3292-5837-af4a-55a94ad6b52a": ("place", "Hermitage", "PA"),  # Hermitage PD
    "cdf53878-f007-5349-82c3-c198d69e4737": ("place", "Houston", "TX"),  # Houston METRO transit
    "0eee95cd-e37a-5dfc-b370-3745ed279b97": ("place", "Thornton", "IL"),  # Illinois Statewide Auto Theft TF
    "799ad8b7-438d-5086-a14c-d396754a5644": ("place", "Lima", "OH"),  # Johnny Appleseed Metro Park District
    "c5cde259-f76e-50ed-87c9-6b4dc48f9cf3": ("place", "Frankfort", "KY"),  # Kentucky AG
    "85363fab-8619-55e4-aa45-15949bb864d2": ("place", "Painesville", "OH"),  # Lake Metroparks Rangers
    "01b34cf7-422f-5ddf-b5c3-95e400e0c4b8": ("place", "Lakeway", "TX"),  # Lake Travis ISD
    "04cf4606-6e6f-59ce-b518-1c9cdde0c183": ("place", "West Columbia", "SC"),  # Lexington Medical Center
    "954e73b9-7cf7-5c4f-b490-a65a1a9cc357": ("place", "Milford", "MA"),  # Massachusetts Dept of Correction
    "e6113b72-79ed-5300-834b-f749e617f7be": ("place", "Newtown", "PA"),  # MAGLOCLEN RISS center
    "710baca5-ca63-52f4-925d-9cdfa9917c61": ("place", "Youngstown", "OH"),  # Mahoning Valley LE Task Force
    "95c199f9-534a-585e-a7e9-4983399855bb": ("place", "Hedwig Village", "TX"),  # Memorial Villages PD
    "c0667123-d485-5710-923a-dbf817b0f99c": ("place", "Midlothian", "TX"),  # Methodist Midlothian Medical Center
    "ca1760c3-29de-5a02-be23-502ff58b205b": ("place", "Toledo", "OH"),  # Metroparks Toledo
    "6f7dca30-e826-53d5-bb7a-33a8e21a85ef": ("place", "New York", "NY"),  # NYC Dept of Environmental Protection
    "7ea3e352-7c88-5b18-88cb-964542ce0796": ("place", "Dallas", "TX"),  # North TX Anti-Gang Center
    "d1c45457-4bc4-5858-a874-158dcc960ef1": ("place", "North Tonawanda", "NY"),  # North Tonawanda City PD
    "a203ad54-323b-54d9-9a8a-94c1cbe609ee": ("place", "Leesport", "PA"),  # Northern Berks Regional PD
    "0be79128-e1ed-5a3c-a57c-75cfd1ee2406": ("place", "Chillicothe", "OH"),  # U.S. 23 Major Crimes Task Force
    "2d246354-acd9-5314-a529-cdb88d36e50b": ("place", "Oklahoma City", "OK"),  # Oklahoma State Bureau of Investigation
    "01d3d28e-4e7f-5b42-8e1e-d68741684c46": ("place", "London", "OH"),  # Ohio Bureau of Criminal Investigation
    "fe8cfc6d-73db-56ef-bb9d-4d1a4432be35": ("place", "Columbus", "OH"),  # Ohio Bureau of Motor Vehicles
    "350b3dc3-f357-59ea-9ce1-91f5c4fbb9e8": ("place", "Columbus", "OH"),  # Ohio DPS
    "a483e698-31f9-5d06-8f8e-973537e839a6": ("place", "Moreland Hills", "OH"),  # Ohio Drug TF (Moreland Hills)
    "c33881fc-332e-50fa-b582-a7174f0c5637": ("place", "Reynoldsburg", "OH"),  # Ohio State Fire Marshal
    "3ae3ebc6-f651-5660-bd47-d18c0e38330b": ("place", "Columbus", "OH"),  # Ohio Bureau of Workers' Compensation
    "42353055-bd16-5954-87dd-ce3ce3f5d572": ("place", "Red Rock", "OK"),  # Otoe-Missouria Tribe
    "6600f2e5-4aa7-57ba-bfc6-94434b7e40b8": ("place", "Sunbury", "OH"),  # Preservation Parks of Delaware County
    "05f3a58d-3072-5fa9-aac2-85fefe863065": ("place", "Toledo", "OH"),  # ProMedica
    "127b98cf-5af4-5ae9-a955-db6f2d5cade8": ("place", "Rockford", "IL"),  # Rockford Park District
    "ea85dbdd-5957-5508-9a72-bf380ecd103d": ("place", "Sisseton", "SD"),  # Lake Traverse Reservation
    "00404003-7cbf-5393-b423-b14d38d6fce5": ("place", "Strongsville", "OH"),  # Southwest Emergency Dispatch Center
    "0ef3b226-05d1-577d-a97b-3b9f48bf6a19": ("place", "Sedona", "AZ"),  # Sedona PD (deactivated)
    "0a271d12-f706-5482-bf9a-1324a3150525": ("place", "New Castle", "PA"),  # Shenango Township PD (Lawrence Co.)
    "82ae4281-d565-5965-ae53-06f2ec921757": ("place", "Society Hill", "SC"),  # Society Hill PD
    "df21aae3-5581-58b8-b5d2-3b81c0d2b668": ("place", "Lancaster", "OH"),  # South Central OH Major Crimes Unit
    "f4cde793-b9ca-5de9-a379-27026fb407fd": ("place", "Lubbock", "TX"),  # South Plains Auto Theft Task Force
    "16fd9939-edcc-5adc-8dcf-939d662158fc": ("place", "Canton", "OH"),  # Stark Parks Public Safety
    "0b1f7152-0bf0-57e7-a020-bf9794a5bed0": ("place", "Bowler", "WI"),  # Stockbridge-Munsee Community
    "6175cc72-e59f-509d-9bac-fc2c762895a5": ("place", "Akron", "OH"),  # Summa Health
    "2fcedc2f-f6e5-579a-846b-8868befbeff2": ("place", "Akron", "OH"),  # Summit Metro Parks
    "fd94ae2d-2dbc-51e9-839d-a05b87c478a3": ("place", "Syracuse", "NY"),  # Syracuse-Hancock Intl Airport
    "f77f3cb5-93ee-52fb-9f54-223682704d1d": ("place", "Austin", "TX"),  # TX Alcoholic Beverage Commission
    "8020a5ed-a254-5ff7-b195-d0f086713829": ("place", "Austin", "TX"),  # Texas Dept of Transportation
    "6370abfb-0b2d-5be3-8a5d-28be2f4d14de": ("place", "Fort Worth", "TX"),  # Tarrant Regional Water District
    "11aab840-0087-5abc-b328-35a2619958bc": ("place", "Houston", "TX"),  # Texas Medical Center
    "77d0e5dd-585e-5feb-9574-ca7c4c3dc66f": ("place", "Payson", "AZ"),  # Tonto Apache Tribe
    "eab53746-9866-59f2-b0a5-313f2dd081f0": ("place", "Dallas", "TX"),  # UT Southwestern Medical Center
    "5a2c7e66-cd79-5606-8385-a761dcf9dc22": ("place", "Richmond", "VA"),  # VA Dept of Wildlife Resources
    "50246cdc-d525-5adb-857b-642bb801df47": ("place", "Raleigh", "NC"),  # WakeMed
    "818b8252-e20f-5986-bb60-32d66972f28d": ("place", "North Riverside", "IL"),  # West Central Consolidated Comms
    "1b63402e-4767-5115-a837-f6c148290248": ("place", "Kansas City", "MO"),  # Westport Regional Business League
    # county overrides (issue #447)
    "8e84238f-abb9-5a87-a619-52905c0a2821": ("county", "Clarke", "GA"),  # Athens-Clarke County PD
    "fde92938-d9fc-5d64-ad1a-cf0239b2ee13": ("county", "Fort Bend", "TX"),  # Fort Bend County Constable Pct 1
    "4c590b32-1aff-5185-a6fb-c6f583505349": ("county", "Fort Bend", "TX"),  # Fort Bend County Constable Pct 4
    "5855a3bb-f4b8-5104-92f6-512f92a16c42": ("county", "Fort Bend", "TX"),  # Fort Bend County Environmental Health
    "c680b273-977e-5d06-a26c-c635701a4510": ("county", "Kaufman", "TX"),  # Kaufman County Constable Pct 2
    "8d9679c1-25c2-5625-afd2-7a014384dc6f": ("county", "Rio Blanco", "CO"),  # Rio Blanco County SO
    "4e3f6084-585f-58ac-b00a-3b21f8b5d200": ("county", "Stark", "OH"),  # Stark County SO
    # county-subdivision (cousub) overrides (issue #447)
    "fdf3939a-6ec5-5236-9186-f86cc9e627e7": ("cousub", "De Witt", "NY"),  # DeWitt town (Onondaga Co.)
    "592bf6b8-db27-5a54-8802-34d5b841b2ba": ("cousub", "East Hanover", "NJ"),  # East Hanover Twp (Morris Co.)
    "c639c3ab-f1e6-504c-baed-b5ed04d33181": ("cousub", "Monroe", "NJ", "34023"),  # Monroe Twp (Middlesex Co.)
    "ec54b02b-0101-59bc-a7a0-27cc6f96accc": ("cousub", "Lisbon", "CT"),  # Town of Lisbon (resident troopers)
    # issue #636 — recipients missing coordinates (researched + verified
    # per-entity; see the issue for the triage record)
    "5095ae77-a4ff-57bd-956a-5d09630dff5f": ("place", "Hemphill", "TX"),  # 1st Judicial District
    "4a90ab8e-1b8b-5a79-994e-49dd35818740": ("place", "Jacksonville", "AL"),  # AL - Jacksonville State University Campus PD
    "c753700e-a641-5f4b-a85c-14b657a14822": ("county", "Randolph", "AL"),  # Alabama Drug Enforcement Task Force Region G
    "e82c9df2-d04b-5dda-bb3a-b99e2ef82931": ("place", "Buford", "GA"),  # Buford City Schools GA
    "a5f6beb7-35d3-51a8-aff2-d8d009f96609": ("place", "Pittsburgh", "PA"),  # Carnegie Mellon University PA PD
    "0e88d56f-0f2c-5484-ae48-b6a011079bda": ("place", "Cumming", "GA"),  # City of Cumming - City Center GA
    "8576dbae-204e-50a2-8aea-28833c64442d": ("place", "Orangeburg", "SC"),  # Claflin University SC PD
    "b24639ba-80bc-59af-9c86-da976dd41a34": ("place", "Atlanta", "GA"),  # Clark Atlanta University GA PD
    "5b1daea9-ac4e-50ac-bd09-ccbbb9716c62": ("place", "Corinth", "TX"),  # Corinth City Marshal [inactive]
    "595c59c6-1d8b-513a-aa22-fc0e4c1b59f9": ("place", "Swainsboro", "GA"),  # East Georgia State College Campus PD (GA)
    "4d11f85a-c572-5307-a191-01fccb09acff": ("place", "Johnson City", "TN"),  # East Tennessee State University TN Campus PD
    "238ad3de-f4cd-5cbf-9aaf-121bb1243444": ("place", "Savannah", "TX"),  # Elm Ridge TX PD
    "727f66e6-bff5-5b50-b250-6af9200d9453": ("place", "Memphis", "TN"),  # FedEx Air Carrier PD
    "5ebd965e-7d8f-5c61-8e8e-4979011f1bb9": ("place", "Travelers Rest", "SC"),  # Furman University SC PD
    "513e2c7e-7c22-5e33-b341-28b85fa3eb45": ("place", "Glenview", "IL"),  # GPSDC (Glenview Dispatch)
    "36b4faf1-d039-55ef-9dc3-40b8e556404c": ("cousub", "Flint", "MI", "26049"),  # Genesee Co. MI 911 Dispatch Authority
    "29f8e61a-a729-5dc2-a83e-6890367c2e4a": ("place", "Lawrenceville", "GA"),  # Georgia Gwinnett College PD
    "d919e136-cd32-54f0-8f04-a6e97894a26d": ("place", "Statesboro", "GA"),  # Georgia Southern University PD (GA)
    "0a0db7cb-160e-5aed-823e-f11ba091e7cb": ("place", "Atlanta", "GA"),  # Georgia State University GA PD
    "32851fd9-6d22-589d-b7cd-0ff8df78611a": ("place", "Atlanta", "GA"),  # Georgia Tech GA PD
    "db24fdeb-80e4-51c2-9a78-cb56b57b70bd": ("place", "Griffin", "GA"),  # Griffin Judicial Circuit
    "e587d408-7d98-542c-b982-0293da8e129b": ("place", "Hampden-Sydney", "VA"),  # Hampden-Sydney College Campus VA PD
    "0d679b98-fa65-50aa-a172-8347bf2fe7b8": ("place", "Hillsboro", "MO"),  # Jefferson College Campus MO PD
    "cc3b2418-9795-5de1-99a4-3cb8cab8cecb": ("place", "Knoxville", "TN"),  # Knoxville Fire Dept TN
    "01b34cf7-422f-5ddf-b5c3-95e400e0c4b8": ("place", "Lakeway", "TX"),  # Lake Travis ISD TX PD [Inactive]
    "05b77c43-33f5-5f4f-809d-a76385765016": ("place", "Knoxville", "TN"),  # Lakeshore Park Conservancy (TN)
    "3aa99c45-2421-51dd-b623-9de3f2f5787c": ("place", "Lakeway", "TX"),  # Lakeway & Bee Cave TX PD(Disp)
    "2c89a44e-a806-5053-8523-6a998f0055ea": ("place", "Greenwood", "SC"),  # Lander University SC PD
    "322365a4-00c4-5999-a24b-6fdc1ccb8b35": ("place", "Louisville", "KY"),  # Louisville Metro KY Alcoholic Beverage Control
    "4923d2ca-c927-5c91-9ede-c6968d133c84": ("place", "Louisville", "KY"),  # Louisville Metro KY Arson Bureau
    "537b9143-dd76-5e15-a652-f974af6d962b": ("place", "Raleigh", "NC"),  # Meredith College Campus PD (NC)
    "b86094fb-9d4f-5e4b-a0b6-2f10f32ee624": ("place", "Bloomington", "IL"),  # Metcom Disptach Center IL
    "9cb6fc81-0706-5a2d-ae5a-3c44be5274b6": ("place", "Macon-Bibb County", "GA"),  # Middle Georgia State University Campus PD
    "2db4d130-ca61-5205-80d1-186063c14d83": ("place", "Verona", "VA"),  # Middle River VA Jail
    "4000b55a-faea-5357-860e-702733e8f126": ("place", "Jefferson City", "MO"),  # Missouri Capitol MO PD
    "062ed728-9122-528d-b281-2a9099be966f": ("place", "Atlanta", "GA"),  # Morehouse College GA PD
    "0dcdadc8-a6d0-57cf-9f43-300ac3acab55": ("place", "Norfolk", "VA"),  # Norfolk State University VA PD
    "1fc2cc4a-2531-5a63-aa4e-64eeb60f1020": ("place", "Norfolk", "VA"),  # Norfolk VA Housing Authority
    "5792ba00-373f-5e22-a66d-ccfc634a29c3": ("place", "Greensboro", "NC"),  # North Carolina A&T University NC PD
    "08d63ca5-d642-5cc7-a47d-d2ec7c700530": ("place", "Blountville", "TN"),  # Northeast State Community College TN
    "598aca59-bd7e-5d67-80df-e93bcbdef150": ("place", "Oklahoma City", "OK"),  # OK - 477
    "0facc144-81b9-5951-9e3d-be23cb11656a": ("place", "Oak Lawn", "IL"),  # OLREC IL Dispatch
    "03e33852-8173-5c02-acb9-f1acace99988": ("place", "Oakland", "CA"),  # Port Of Oakland (CA)
    "cf916c0a-c36c-52b7-9bd3-b82703c502bb": ("place", "The Woodlands", "TX"),  # Precinct 3 (TX)
    "43ff10f6-3ea5-5df1-b594-dc3dd2dfc324": ("place", "Magnolia", "TX"),  # Precinct 5 (TX)
    "65d079be-0bf9-5384-bd97-d01b6c77cd3f": ("place", "Clinton", "SC"),  # Presbyterian College SC PD
    "d38ca136-aa34-59e8-b13c-59cdf8567b88": ("place", "Milan", "IL"),  # QComm911 Dispatch IL
    "d27486bc-f265-5158-a0ae-10038f258e62": ("county", "Williamson", "TX"),  # Regional Vehicle Burglary Suppression Taskforce TX
    "34fafb70-7bc2-5eb9-8057-e18b1a94ff8b": ("place", "Waleska", "GA"),  # Reinhardt University GA PD
    "e57e14ad-2bd8-5df7-a315-f2723972292a": ("place", "Decatur", "IL"),  # Richland Community College Campus IL PD
    "e47473b6-09f4-51d2-af5e-777a47048145": ("place", "Crystal Lake", "IL"),  # SEECOM Dispatch Center IL
    "90b3e5a4-915e-51ac-b486-4018e64a1a0b": ("cousub", "Kochville", "MI"),  # Saginaw Valley State University PD (MI) [Inactive]
    "494a32c2-2763-55c3-8842-27dad589ad32": ("place", "Homewood", "AL"),  # Samford University AL PD
    "14e400d1-5c90-54ae-a2fd-3a659dbc6b33": ("place", "Knoxville", "TN"),  # South College TN PD
    "cdcbf72d-b0ce-59bf-b973-e00dfdc32bde": ("place", "Memphis", "TN"),  # Southwest Tennessee Community College TN PD
    "110d1853-7254-5be2-9520-ca30d756e920": ("place", "Springfield", "IL"),  # Springfield Park District IL PD
    "e8c47b49-e42f-5119-b523-fbbfc7584532": ("place", "Austin", "TX"),  # St. Edward's University (TX)
    "070d9906-8500-55b2-8ff6-1a6bbc3981d6": ("place", "Hartford", "CT"),  # State Capitol CT PD
    "343ba750-f243-5c12-b481-0ebf04c57414": ("place", "Memphis", "TN"),  # TN - Loeb Properties Inc - Security
    "6a4187fd-6b1a-5bc9-acfb-ca201fc7204d": ("place", "Austin", "TX"),  # Texas Parks & Wildlife Department
    "34865368-fd21-5ffa-a764-a0033c556f9a": ("place", "Town and Country", "MO"),  # The West Central Dispatch Center (WCDC)
    "62b9849e-9459-5c94-9126-0606536f1432": ("place", "White Bluff", "TN"),  # Town of Whitebluff TN PD
    "18271f3b-d3be-5d62-a8ce-4474d4da86f1": ("place", "Tuscaloosa", "AL"),  # Tuscaloosa Academy (AL)
    "b885c35d-d975-5e7c-8dc1-16da3a5ba133": ("place", "Tuscaloosa", "AL"),  # University of Alabama AL PD
    "33a31834-8b55-5873-8508-38830443168a": ("place", "Berkeley", "CA"),  # University of California, Berkeley PD
    "3d7326eb-a86e-5074-a75e-5d9d5a2f9019": ("place", "Columbia", "MO"),  # University of Missouri Campus MO PD
    "e200e57b-8244-52ac-ae67-b4551a015b2c": ("place", "West Haven", "CT"),  # University of New Haven CT PD
    "91defbf0-3fdb-5aec-a570-b6fed9fc7d37": ("place", "West Hartford", "CT"),  # University of Saint Joseph CT Public Safety
    "757e5e7d-4ff4-58f9-8be5-aa9ff29ccdce": ("place", "Richmond", "VA"),  # Virginia Commonwealth University VA PD
    "0e406815-56da-5b2e-81bc-cb1cf63f4a40": ("place", "Waco", "GA"),  # West Georgia Technical College (GA)
    "a4f56a5f-9099-5866-9dd0-dc1df44cf4ce": ("county", "Winston", "MS"),  # Winston Count MS CO
    "68af1aa4-7d22-522e-8c81-16a59402cc63": ("place", "Winston-Salem", "NC"),  # Winston Salem State University NC PD
    "8de1aa86-5746-54ab-b18f-f7fd35e8fdb1": ("place", "Rock Hill", "SC"),  # Winthrop University SC PD
}


def _gaz_override_geo(spec):
    """Resolve a GAZ_OVERRIDES spec into a geo block, or None.

    spec is (kind, name, state) or (kind, name, state, county_hint).
    """
    kind, name, state = spec[0], spec[1], spec[2]
    hint = spec[3] if len(spec) > 3 else None
    if kind == "place":
        p = lookup_place(name, state)
        if p:
            return {"kind": "place", "fips": p["fips"], "name": p["name"],
                    "state": state, "lat": p["lat"], "lng": p["lng"]}
    elif kind == "county":
        c = lookup_county(name, state)
        if c:
            return {"kind": "county", "fips": c["fips"], "name": c["bare_name"],
                    "state": state, "lat": c["lat"], "lng": c["lng"]}
    elif kind == "cousub":
        cs = lookup_cousub(name, state, county_hint=hint)
        if cs:
            return {"kind": "cousub", "fips": cs["fips"], "name": cs["bare_name"],
                    "state": state, "lat": cs["lat"], "lng": cs["lng"]}
    return None


def geocode_entry(entry):
    """Attempt to geocode a single entry. Returns a geo dict or None.

    Uses the entry's display name and state. Skips entries with no state
    or that are tagged as private/federal/test/etc.
    """
    aid = entry.get("agency_id")
    if aid in MANUAL_OVERRIDES:
        return dict(MANUAL_OVERRIDES[aid])
    if aid in GAZ_OVERRIDES:
        geo = _gaz_override_geo(GAZ_OVERRIDES[aid])
        if geo:
            return geo

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
        # New England / NY style "X Town", "X Town/Village", "X Village":
        # the Census indexes these as county subdivisions (towns), not
        # incorporated places. Strip the trailing town-type word and look
        # up the cousub first (e.g. "Amherst Town NY PD" -> Amherst town).
        tm = re.search(r"\s+(?:Town/Village|Town|Village)$", candidate)
        if tm:
            bare = candidate[: tm.start()].strip()
            town = lookup_cousub(bare, state)
            if town:
                return {
                    "kind": "cousub",
                    "fips": town["fips"],
                    "name": town["bare_name"],
                    "state": state,
                    "lat": town["lat"],
                    "lng": town["lng"],
                }
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
