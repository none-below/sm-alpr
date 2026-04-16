#!/usr/bin/env python3
"""Refresh Census population + household-vehicle estimates for places and counties.

Downloads the latest ACS 5-year estimates from the Census API and saves
them as TSVs keyed by FIPS code. Run periodically (annually) to keep
the per-capita / per-vehicle calculations current.

Source: https://api.census.gov/data/{year}/acs/acs5

  - B01003_001E = total population
  - B25046_001E = aggregate number of vehicles available across all
    households in the geography. Counts vehicles kept at home for
    household use (ACS survey estimate); excludes commercial/fleet
    vehicles that aren't assigned to a household. It's the closest
    per-city vehicle count available from Census — DMV doesn't
    publish registrations at the place level.

Usage:
  uv run python scripts/refresh_census_population.py              # most-recent ACS vintage
  uv run python scripts/refresh_census_population.py --year 2023  # specific vintage
"""

import argparse
import csv
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

DATA_DIR = Path("data/census")
PLACES_OUT = DATA_DIR / "population_places.tsv"
COUNTIES_OUT = DATA_DIR / "population_counties.tsv"
META_OUT = DATA_DIR / "population_meta.json"

BASE = "https://api.census.gov/data/{year}/acs/acs5"

# ACS variables we fetch alongside population.
#   population: B01003_001E — total population
#   vehicles:   B25046_001E — aggregate household vehicles available
VARIABLES = "NAME,B01003_001E,B25046_001E"


def fetch_places(year):
    """Fetch place-level population + vehicles for every state, concatenated.

    The Census API caps some queries at the state level; places must be
    fetched per-state because `for=place:*&in=state:*` returns a single
    state per query. We iterate through all 56 USPS codes.
    """
    # State FIPS: 01..56 skipping non-existent ones. Easier to just
    # enumerate the known state FIPS codes from our gazetteer.
    state_fips = _state_fips_list()
    rows = []
    for sf in state_fips:
        url = (f"{BASE.format(year=year)}?get={VARIABLES}"
               f"&for=place:*&in=state:{sf}")
        data = _fetch_json(url)
        if not data:
            continue
        header = data[0]
        name_idx = header.index("NAME")
        pop_idx = header.index("B01003_001E")
        veh_idx = header.index("B25046_001E")
        state_idx = header.index("state")
        place_idx = header.index("place")
        for row in data[1:]:
            fips = row[state_idx] + row[place_idx]
            try:
                pop = int(row[pop_idx])
            except (ValueError, TypeError):
                continue
            try:
                veh = int(row[veh_idx])
            except (ValueError, TypeError):
                veh = ""
            rows.append((fips, row[name_idx], pop, veh))
        print(f"  state FIPS {sf}: {len(data) - 1} places")
    return rows


def fetch_counties(year):
    """Fetch county-level population + vehicles (one request — all counties fit)."""
    url = (f"{BASE.format(year=year)}?get={VARIABLES}"
           f"&for=county:*&in=state:*")
    data = _fetch_json(url)
    if not data:
        return []
    header = data[0]
    name_idx = header.index("NAME")
    pop_idx = header.index("B01003_001E")
    veh_idx = header.index("B25046_001E")
    state_idx = header.index("state")
    county_idx = header.index("county")
    rows = []
    for row in data[1:]:
        fips = row[state_idx] + row[county_idx]
        try:
            pop = int(row[pop_idx])
        except (ValueError, TypeError):
            continue
        try:
            veh = int(row[veh_idx])
        except (ValueError, TypeError):
            veh = ""
        rows.append((fips, row[name_idx], pop, veh))
    return rows


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "sm-alpr/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except Exception as ex:
        print(f"  ERROR fetching {url}: {ex}", file=sys.stderr)
        return None


def _state_fips_list():
    """Derive the list of valid state FIPS codes from the gazetteer counties file."""
    counties_path = DATA_DIR / "counties.tsv"
    if not counties_path.exists():
        print(f"  ERROR: {counties_path} not found. Run refresh_census_gazetteer.py first.",
              file=sys.stderr)
        sys.exit(1)
    seen = set()
    with counties_path.open() as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            row = {k.strip(): (v.strip() if v else v) for k, v in row.items()}
            fips = row.get("GEOID")
            if fips and len(fips) >= 2:
                seen.add(fips[:2])
    return sorted(seen)


def write_tsv(path, rows, header):
    with path.open("w") as f:
        f.write("\t".join(header) + "\n")
        for row in rows:
            f.write("\t".join(str(v) for v in row) + "\n")
    print(f"  wrote {path} ({len(rows):,} rows)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=date.today().year - 2,
                        help="ACS 5-year vintage (default: two calendar years prior; ACS is released with a lag)")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Fetching ACS {args.year} 5-year population estimates...")

    print("Places:")
    place_rows = fetch_places(args.year)
    place_rows.sort(key=lambda r: r[0])
    write_tsv(PLACES_OUT, place_rows, ["fips", "name", "population", "vehicles"])

    print("Counties:")
    county_rows = fetch_counties(args.year)
    county_rows.sort(key=lambda r: r[0])
    write_tsv(COUNTIES_OUT, county_rows, ["fips", "name", "population", "vehicles"])

    META_OUT.write_text(json.dumps({
        "vintage": args.year,
        "source": f"ACS 5-Year Estimates ({args.year}), U.S. Census Bureau",
        "fetched": date.today().isoformat(),
    }, indent=2) + "\n")
    print(f"  wrote {META_OUT}")

    print(f"\nRefreshed to ACS {args.year} population estimates.")


if __name__ == "__main__":
    main()
