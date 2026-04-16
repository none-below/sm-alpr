#!/usr/bin/env python3
"""Refresh Census gazetteer TSVs.

Downloads the latest annual gazetteer files from the Census Bureau and
saves them to data/census/. Run periodically (annually) to keep the
FIPS lookups in sync with the latest Census data.

Source: https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html

Usage:
  uv run python scripts/refresh_census_gazetteer.py              # current year
  uv run python scripts/refresh_census_gazetteer.py --year 2023  # specific year
"""

import argparse
import io
import sys
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

DATA_DIR = Path("data/census")
BASE_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{year}_Gazetteer"

FILES = [
    # (census file suffix, local name)
    ("places", "places.tsv"),
    ("counties", "counties.tsv"),
    ("cousubs", "cousubs.tsv"),
]


def download_file(year, suffix, out_path):
    url = f"{BASE_URL.format(year=year)}/{year}_Gaz_{suffix}_national.zip"
    print(f"  fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "sm-alpr/1.0"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        # There's one .txt file inside
        txt_name = next(n for n in zf.namelist() if n.endswith(".txt"))
        with zf.open(txt_name) as f:
            content = f.read()
    out_path.write_bytes(content)
    print(f"  saved {out_path} ({len(content):,} bytes)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=date.today().year - 1,
                        help="Gazetteer vintage year (default: previous calendar year)")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for suffix, local_name in FILES:
        out_path = DATA_DIR / local_name
        try:
            download_file(args.year, suffix, out_path)
        except Exception as ex:
            print(f"  ERROR fetching {suffix}: {ex}", file=sys.stderr)
            sys.exit(1)

    print(f"\nRefreshed to {args.year} gazetteer. Run tests/test_geo_cache.py to validate.")


if __name__ == "__main__":
    main()
