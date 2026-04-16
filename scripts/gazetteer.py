"""US Census gazetteer lookup: places and counties with FIPS codes.

FIPS (Federal Information Processing Standards) codes are the canonical
identifier for US states, counties, and incorporated/unincorporated places.
Census publishes gazetteer files with coordinates and populations.

Source: https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html

Files: data/census/places.tsv, data/census/counties.tsv (committed; ~7MB total).
"""

import csv
import re
import unicodedata
from pathlib import Path

PLACES_PATH = Path("data/census/places.tsv")
COUNTIES_PATH = Path("data/census/counties.tsv")
COUSUBS_PATH = Path("data/census/cousubs.tsv")  # county subdivisions (townships, CCDs)
POPULATION_PLACES_PATH = Path("data/census/population_places.tsv")
POPULATION_COUNTIES_PATH = Path("data/census/population_counties.tsv")
POPULATION_META_PATH = Path("data/census/population_meta.json")

# Common suffixes in Census place names to strip for matching
_PLACE_SUFFIXES = re.compile(
    r"\s+(city|town|village|borough|township|CDP|municipality|"
    r"consolidated government|urban county|unified government|metro government|"
    r"metropolitan government)$",
    re.IGNORECASE,
)

_places_cache = None
_counties_cache = None
_cousubs_cache = None
_population_cache = None
_population_meta_cache = None


_COUNTY_SUFFIX = re.compile(
    r"\s+(County|Parish|Borough|Census Area|Municipality)$", re.IGNORECASE
)


def _normalize(name):
    """Normalize a name for matching: lowercase, strip punct and diacritics.

    Does not strip Census suffixes — those are stripped separately when
    building the gazetteer index so the raw place name is preserved.
    (e.g. "Cañon City city" is indexed under key "canon city", not "canon".)
    """
    # Fold unicode accents: Cañon -> Canon, etc.
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower().strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"[.,'\u2019]", "", name)
    return name


def _strip_place_suffix(name):
    """Strip Census LSAD suffixes (city, town, CDP, etc.) from a raw place name."""
    return _PLACE_SUFFIXES.sub("", name).strip()


def _strip_county_suffix(name):
    """Strip Census county suffixes (County, Parish, Borough, etc.)."""
    return _COUNTY_SUFFIX.sub("", name).strip()


def _load_places():
    global _places_cache
    if _places_cache is not None:
        return _places_cache
    places = []
    with PLACES_PATH.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            # Strip whitespace from keys and values (some have trailing spaces)
            row = {k.strip(): (v.strip() if v else v) for k, v in row.items()}
            if not row.get("INTPTLAT") or not row.get("INTPTLONG"):
                continue
            bare = _strip_place_suffix(row["NAME"])
            # Also strip MA-style "Town" that appears before the LSAD
            # (e.g. "Braintree Town city" -> "Braintree Town" -> "Braintree")
            bare_key = re.sub(r"\s+Town$", "", bare, flags=re.IGNORECASE)
            try:
                land_sqmi = float(row.get("ALAND_SQMI") or 0) or None
            except (ValueError, TypeError):
                land_sqmi = None
            places.append({
                "state": row["USPS"],
                "fips": row["GEOID"],
                "raw_name": row["NAME"],
                "name": bare,
                "lat": float(row["INTPTLAT"]),
                "lng": float(row["INTPTLONG"]),
                "land_sqmi": land_sqmi,
                "_key": _normalize(bare_key),
            })
    _places_cache = places
    return places


def _load_counties():
    global _counties_cache
    if _counties_cache is not None:
        return _counties_cache
    counties = []
    with COUNTIES_PATH.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            row = {k.strip(): (v.strip() if v else v) for k, v in row.items()}
            if not row.get("INTPTLAT") or not row.get("INTPTLONG"):
                continue
            bare = _strip_county_suffix(row["NAME"])
            try:
                land_sqmi = float(row.get("ALAND_SQMI") or 0) or None
            except (ValueError, TypeError):
                land_sqmi = None
            counties.append({
                "state": row["USPS"],
                "fips": row["GEOID"],
                "raw_name": row["NAME"],
                "name": row["NAME"],
                "bare_name": bare,
                "lat": float(row["INTPTLAT"]),
                "lng": float(row["INTPTLONG"]),
                "land_sqmi": land_sqmi,
                "_key": _normalize(bare),
            })
    _counties_cache = counties
    return counties


_COUSUB_SUFFIX = re.compile(
    r"\s+(township|town|borough|village|CCD|district|charter township|plantation|"
    r"gore|grant|location|precinct|purchase)$",
    re.IGNORECASE,
)


def _load_cousubs():
    """Load county subdivisions (townships, CCDs, etc.)."""
    global _cousubs_cache
    if _cousubs_cache is not None:
        return _cousubs_cache
    if not COUSUBS_PATH.exists():
        _cousubs_cache = []
        return _cousubs_cache
    cousubs = []
    with COUSUBS_PATH.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            row = {k.strip(): (v.strip() if v else v) for k, v in row.items()}
            if not row.get("INTPTLAT") or not row.get("INTPTLONG"):
                continue
            bare = _COUSUB_SUFFIX.sub("", row["NAME"]).strip()
            cousubs.append({
                "state": row["USPS"],
                "fips": row["GEOID"],
                "raw_name": row["NAME"],
                "bare_name": bare,
                "lat": float(row["INTPTLAT"]),
                "lng": float(row["INTPTLONG"]),
                "_key": _normalize(bare),
            })
    _cousubs_cache = cousubs
    return _cousubs_cache


def lookup_cousub(name, state, county_hint=None):
    """Find a county subdivision (township, CCD, etc.) by name + state.

    Township names often repeat within a state (many "Jackson townships"
    in Ohio). If ``county_hint`` is provided, filter by 5-digit county
    FIPS prefix.
    """
    key = _normalize(name)
    matches = [c for c in _load_cousubs() if c["state"] == state and c["_key"] == key]
    if county_hint:
        matches = [m for m in matches if m["fips"].startswith(county_hint)]
    if len(matches) == 1:
        return matches[0]
    return None


def lookup_place(name, state):
    """Find a place by name + state code. Returns dict or None.

    Matching is case-insensitive. If the input name ends with a Census
    LSAD suffix (city, town, CDP, etc.), it's stripped before matching.
    """
    # Only strip LSAD suffix at the end if the base name wouldn't match
    # (e.g. "Foster City" needs to match the "Foster City city" entry,
    # but "San Mateo city" input should strip to "San Mateo")
    key = _normalize(name)
    matches = [p for p in _load_places() if p["state"] == state and p["_key"] == key]
    if not matches:
        stripped = _strip_place_suffix(name)
        if stripped != name:
            key = _normalize(stripped)
            matches = [p for p in _load_places() if p["state"] == state and p["_key"] == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Prefer "city" over "CDP" over others when ambiguous
        for suffix in (" city", " town", " CDP", ""):
            for m in matches:
                if m["raw_name"].endswith(suffix):
                    return m
    return None


def lookup_county(name, state):
    """Find a county by name + state code. Accepts 'San Mateo' or 'San Mateo County'."""
    key = _normalize(_strip_county_suffix(name))
    matches = [c for c in _load_counties() if c["state"] == state and c["_key"] == key]
    if len(matches) == 1:
        return matches[0]
    return None


def lookup_state(usps):
    """Return a (state_fips, lat, lng, name) tuple for the state centroid.

    Computed as the centroid of all counties in the state.
    """
    counties = [c for c in _load_counties() if c["state"] == usps]
    if not counties:
        return None
    lat = sum(c["lat"] for c in counties) / len(counties)
    lng = sum(c["lng"] for c in counties) / len(counties)
    state_fips = counties[0]["fips"][:2]
    return {
        "state_fips": state_fips,
        "state": usps,
        "lat": lat,
        "lng": lng,
    }


def lookup_by_place_fips(fips):
    """Look up a place by FIPS code."""
    for p in _load_places():
        if p["fips"] == fips:
            return p
    return None


def lookup_by_county_fips(fips):
    """Look up a county by FIPS code."""
    for c in _load_counties():
        if c["fips"] == fips:
            return c
    return None


_place_to_county_cache = {}


def county_fips_for_place(place_fips):
    """Return the county FIPS that a place is located in, or None.

    Place FIPS codes are state(2) + place(5) — they don't encode county.
    To find the county for a place we compare the place centroid against
    every county in the same state and pick the nearest. Results are
    memoized across calls.

    For non-place FIPS (counties, cousubs), callers can just take the
    first 5 characters directly; this helper is only for places.
    """
    if not place_fips:
        return None
    if place_fips in _place_to_county_cache:
        return _place_to_county_cache[place_fips]

    place = lookup_by_place_fips(place_fips)
    if not place:
        _place_to_county_cache[place_fips] = None
        return None

    state = place["state"]
    p_lat, p_lng = place["lat"], place["lng"]

    # Haversine isn't strictly needed — we're just finding the nearest
    # county centroid within one state. Squared-degree distance is fine
    # and much cheaper.
    best = None
    best_d2 = float("inf")
    for c in _load_counties():
        if c["state"] != state:
            continue
        dlat = c["lat"] - p_lat
        dlng = c["lng"] - p_lng
        d2 = dlat * dlat + dlng * dlng
        if d2 < best_d2:
            best_d2 = d2
            best = c

    result = best["fips"] if best else None
    _place_to_county_cache[place_fips] = result
    return result


def _load_population():
    """Lazy-load the population TSVs into a single fips -> int dict."""
    global _population_cache
    if _population_cache is not None:
        return _population_cache
    pop = {}
    for path in (POPULATION_PLACES_PATH, POPULATION_COUNTIES_PATH):
        if not path.exists():
            continue
        with path.open() as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                fips = (row.get("fips") or "").strip()
                try:
                    pop[fips] = int(row["population"])
                except (ValueError, KeyError, TypeError):
                    continue
    _population_cache = pop
    return pop


def lookup_area_sqmi(fips):
    """Return land area in square miles for a FIPS (place or county), or None.

    Uses the Census gazetteer's ALAND_SQMI. Useful for per-sq-mi rates.
    """
    if not fips:
        return None
    if len(fips) == 7:
        for p in _load_places():
            if p["fips"] == fips:
                return p.get("land_sqmi")
    elif len(fips) == 5:
        for c in _load_counties():
            if c["fips"] == fips:
                return c.get("land_sqmi")
    return None


def lookup_population(fips):
    """Return the population for a FIPS code (place or county), or None.

    Requires data/census/population_places.tsv and
    data/census/population_counties.tsv (produced by
    refresh_census_population.py).
    """
    if not fips:
        return None
    return _load_population().get(fips)


def population_meta():
    """Return population vintage metadata dict, or None if unavailable."""
    global _population_meta_cache
    if _population_meta_cache is not None:
        return _population_meta_cache
    import json as _json
    if not POPULATION_META_PATH.exists():
        return None
    try:
        _population_meta_cache = _json.loads(POPULATION_META_PATH.read_text())
        return _population_meta_cache
    except (OSError, _json.JSONDecodeError):
        return None
