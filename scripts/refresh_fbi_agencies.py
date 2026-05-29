#!/usr/bin/env python3
"""Refresh the FBI agency master list (ORI directory) from the Crime Data Explorer.

Downloads every agency the FBI knows about, per state, and saves them as a
TSV keyed by ORI (Originating Agency Identifier). The ORI is a stable
9-character federal code that never changes for a given agency — unlike
Flock portal slugs or display names, it's a durable join key.

This file is the reference table for `match_ori.py`, which attaches an
`ori` field to each row of assets/agency_registry.json. Once an agency has
an ORI, any FBI/BJS publication (sworn-officer counts, UCR crime data,
LEMAS) joins exactly on ORI with no further name-matching.

Source: https://api.usa.gov/crime/fbi/cde/agency/byStateAbbr/{state}
  Returns an object keyed by county name; each value is a list of agencies:
    {ori, agency_name, agency_type_name, counties, state_abbr,
     latitude, longitude, is_nibrs, nibrs_start_date}

API key: read from ~/.config/sm-alpr/api_data_gov_key (a free api.data.gov
key), or the DATA_GOV_API_KEY environment variable. The standard key rate
limit is 1,000 requests/hour — this script makes ~51 (one per state), well
under it.

Usage:
  uv run python scripts/refresh_fbi_agencies.py            # all states
  uv run python scripts/refresh_fbi_agencies.py --state CA  # one state (debug)
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path("data/fbi")
AGENCIES_OUT = DATA_DIR / "agencies.tsv"
META_OUT = DATA_DIR / "agencies_meta.json"

BASE = "https://api.usa.gov/crime/fbi/cde/agency/byStateAbbr/{state}"

CONFIG_KEY_PATH = Path.home() / ".config" / "sm-alpr" / "api_data_gov_key"

# 50 states + DC. The CDE has no agencies for the territories via this
# endpoint, so we don't query them.
STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
]

COLUMNS = ["ori", "agency_name", "agency_type_name", "state",
           "county", "lat", "lng", "is_nibrs"]


def api_key():
    key = os.environ.get("DATA_GOV_API_KEY")
    if key:
        return key.strip()
    if CONFIG_KEY_PATH.exists():
        return CONFIG_KEY_PATH.read_text().strip()
    print(f"ERROR: no API key. Set DATA_GOV_API_KEY or write {CONFIG_KEY_PATH}",
          file=sys.stderr)
    sys.exit(1)


def fetch_state(state, key, retries=3):
    """Return the list of agency dicts for one state, flattened from the
    county-keyed response object. Empty list on persistent failure."""
    url = BASE.format(state=state) + f"?API_KEY={key}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sm-alpr/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode())
            break
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            wait = 2 ** attempt
            print(f"  {state}: {e} — retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    else:
        print(f"  {state}: FAILED after {retries} attempts", file=sys.stderr)
        return []

    # Response is {county_name: [agency, ...], ...}. Flatten.
    agencies = []
    if isinstance(payload, dict):
        for county_agencies in payload.values():
            if isinstance(county_agencies, list):
                agencies.extend(county_agencies)
    return agencies


def to_row(ag):
    lat = ag.get("latitude")
    lng = ag.get("longitude")
    return [
        ag.get("ori", ""),
        (ag.get("agency_name") or "").replace("\t", " ").strip(),
        (ag.get("agency_type_name") or "").strip(),
        (ag.get("state_abbr") or "").strip(),
        (ag.get("counties") or "").replace("\t", " ").strip(),
        f"{lat}" if lat is not None else "",
        f"{lng}" if lng is not None else "",
        "1" if ag.get("is_nibrs") else "0",
    ]


def main():
    parser = argparse.ArgumentParser(description="Refresh FBI agency master (ORI directory)")
    parser.add_argument("--state", help="Fetch only this state (debug)")
    parser.add_argument("--delay", type=float, default=0.3,
                        help="Seconds between state requests (politeness)")
    args = parser.parse_args()

    key = api_key()
    states = [args.state.upper()] if args.state else STATES

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    all_rows = []
    seen_ori = set()
    dupes = 0
    per_state = {}
    for st in states:
        agencies = fetch_state(st, key)
        kept = 0
        for ag in agencies:
            ori = ag.get("ori")
            if not ori:
                continue
            # An agency can appear under multiple counties (multi-county
            # jurisdictions); ORI is the dedup key.
            if ori in seen_ori:
                dupes += 1
                continue
            seen_ori.add(ori)
            all_rows.append(to_row(ag))
            kept += 1
        per_state[st] = kept
        print(f"  {st}: {kept} agencies")
        if not args.state:
            time.sleep(args.delay)

    all_rows.sort(key=lambda r: r[0])  # by ORI

    with AGENCIES_OUT.open("w") as f:
        f.write("\t".join(COLUMNS) + "\n")
        for row in all_rows:
            f.write("\t".join(row) + "\n")

    META_OUT.write_text(json.dumps({
        "source": "FBI Crime Data Explorer — agency/byStateAbbr",
        "endpoint": BASE,
        "states_fetched": states if args.state else "all",
        "total_agencies": len(all_rows),
        "duplicate_ori_skipped": dupes,
        "per_state": per_state,
    }, indent=2) + "\n")

    print(f"\nWrote {len(all_rows)} agencies -> {AGENCIES_OUT}")
    if dupes:
        print(f"  ({dupes} multi-county duplicate ORIs collapsed)")


if __name__ == "__main__":
    main()
