#!/usr/bin/env python3
"""Fetch full-time sworn-officer (and civilian) counts per agency from the FBI.

For every ORI in the registry, pull the FBI Crime Data Explorer "Police
Employment" (PE) series and record the most recent year with real data. The
result is a flat JSON keyed by ORI — the reference file the sharing report
joins against to estimate how many people have access to a city's ALPR data.

Source: https://api.usa.gov/crime/fbi/cde/pe/agency/{ori}?from={y}&to={y}
  actuals → {Male Officers, Female Officers, Male Civilians, Female Civilians}
  Sworn officers = Male Officers + Female Officers (the count we report;
  civilians are stored too, since dispatchers/analysts may also query ALPR).

Output (flat, keyed by ORI):
  data/fbi/sworn_officers.json   { "<ORI>": {year, officers, civilians, total}, ... }
  data/fbi/sworn_meta.json       { source, vintage range, counts }

Resumable: ORIs already present in the output are skipped unless --refresh.
This matters because the api.data.gov rate limit is 1,000 req/hour and the
registry has ~4k ORIs — a fresh full run spans several hours, so the GitHub
Action can run it repeatedly and converge.

API key: ~/.config/sm-alpr/api_data_gov_key or DATA_GOV_API_KEY (see
refresh_fbi_agencies.py).

Usage:
  uv run python scripts/refresh_fbi_sworn.py --slug san-mateo-ca-pd  # one city's recipients
  uv run python scripts/refresh_fbi_sworn.py --all                   # every registry ORI
  uv run python scripts/refresh_fbi_sworn.py --all --refresh         # re-fetch everything
"""

import argparse
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_fbi_agencies import api_key  # noqa: E402

REGISTRY_PATH = Path("assets/agency_registry.json")
PORTAL_DIR = Path("assets/transparency.flocksafety.com")
SWORN_OUT = Path("data/fbi/sworn_officers.json")
META_OUT = Path("data/fbi/sworn_meta.json")

PE_URL = "https://api.usa.gov/crime/fbi/cde/pe/agency/{ori}?from={y0}&to={y1}"

# Pull a window and pick the most recent populated year. FBI PE typically lags
# ~1 year, so 2024 may be present while 2025 is null.
FROM_YEAR = 2018
TO_YEAR = 2025


def parse_pe(payload):
    """Return {year, officers, civilians, total} for the most recent year with
    real officer data, or None if the agency has no usable PE record."""
    actuals = payload.get("actuals") or {}
    mo = actuals.get("Male Officers") or {}
    fo = actuals.get("Female Officers") or {}
    mc = actuals.get("Male Civilians") or {}
    fc = actuals.get("Female Civilians") or {}
    years = sorted({int(y) for y in (mo.keys() | fo.keys())}, reverse=True)
    for y in years:
        sy = str(y)
        m, f = mo.get(sy), fo.get(sy)
        if m is None and f is None:
            continue
        officers = (m or 0) + (f or 0)
        civilians = (mc.get(sy) or 0) + (fc.get(sy) or 0)
        return {"year": y, "officers": officers,
                "civilians": civilians, "total": officers + civilians}
    return None


def fetch_pe(ori, key, retries=3):
    url = PE_URL.format(ori=ori, y0=FROM_YEAR, y1=TO_YEAR) + f"&API_KEY={key}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sm-alpr/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return "ok", json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "nodata", None  # agency has no PE series — cache as null
            if e.code == 429:
                print("  rate-limited (429) — sleeping 60s", file=sys.stderr)
                time.sleep(60)
                continue
            time.sleep(2 ** attempt)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            print(f"  {ori}: {e}", file=sys.stderr)
            time.sleep(2 ** attempt)
    # Exhausted retries on a transient failure — do NOT cache as null (that
    # would permanently undercount); leave uncached so the next run retries.
    return "error", None


def responsive_oris(registry):
    """ORIs of agencies that are some portal's OUTBOUND recipient — the set the
    report actually needs (inbound-only discoveries aren't responsive)."""
    by_name = {}
    for e in registry:
        for n in e.get("flock_names", []):
            by_name.setdefault(n, e)
    oris = set()
    for d in sorted(PORTAL_DIR.glob("*/")):
        scrapes = sorted(glob.glob(str(d / "2*.json")))  # date-named portal scrapes
        if not scrapes:
            continue
        data = json.loads(Path(scrapes[-1]).read_text())
        for n in (data.get("sharing_outbound") or data.get("shared_org_names") or []):
            e = by_name.get(n)
            if e:
                oris.update(e.get("ori") or [])  # ori is a list (umbrellas: many)
    return oris


def slug_recipient_oris(registry, slug):
    """ORIs of the agencies a single portal (by slug) shares OUTBOUND to."""
    by_name = {e_name: e for e in registry for e_name in e.get("flock_names", [])}
    scrapes = sorted(glob.glob(str(PORTAL_DIR / slug / "2*.json")))
    if not scrapes:
        print(f"ERROR: no portal scrape for slug {slug}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(Path(scrapes[-1]).read_text())
    oris = set()
    for n in (data.get("sharing_outbound") or data.get("shared_org_names") or []):
        e = by_name.get(n)
        if e:
            oris.update(e.get("ori") or [])  # ori is a list (umbrellas: many)
    return oris


def main():
    ap = argparse.ArgumentParser(description="Fetch FBI sworn-officer counts per ORI")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="Every distinct ORI in the registry")
    g.add_argument("--slug", help="Only the ORIs a single portal shares outbound to")
    ap.add_argument("--refresh", action="store_true", help="Re-fetch even cached ORIs")
    ap.add_argument("--delay", type=float, default=0.4, help="Seconds between calls")
    ap.add_argument("--limit", type=int, help="Stop after N fetches (debug)")
    args = ap.parse_args()

    key = api_key()
    registry = json.loads(REGISTRY_PATH.read_text())

    if args.slug:
        targets = slug_recipient_oris(registry, args.slug)
    elif args.all:
        targets = {o for e in registry for o in (e.get("ori") or [])}
    else:
        targets = responsive_oris(registry)

    SWORN_OUT.parent.mkdir(parents=True, exist_ok=True)
    cache = json.loads(SWORN_OUT.read_text()) if SWORN_OUT.exists() else {}

    todo = sorted(targets if args.refresh else (targets - set(cache)))
    print(f"Targets: {len(targets)} ORIs | cached: {len(set(targets) & set(cache))} "
          f"| to fetch: {len(todo)}")

    fetched = no_data = errored = 0
    for i, ori in enumerate(todo):
        if args.limit and fetched >= args.limit:
            break
        status, payload = fetch_pe(ori, key)
        if status == "error":
            errored += 1  # leave uncached — next run retries
        else:
            cache[ori] = parse_pe(payload) if payload else None
            if cache[ori]:
                fetched += 1
            else:
                no_data += 1
        if i % 50 == 0:
            SWORN_OUT.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
        time.sleep(args.delay)

    SWORN_OUT.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")

    populated = sum(1 for v in cache.values() if v)
    META_OUT.write_text(json.dumps({
        "source": "FBI Crime Data Explorer — Police Employment (pe/agency)",
        "endpoint": PE_URL,
        "year_window": [FROM_YEAR, TO_YEAR],
        "oris_with_data": populated,
        "oris_no_data": len(cache) - populated,
        "total_oris": len(cache),
        "sworn_definition": "officers = Male Officers + Female Officers (full-time sworn)",
    }, indent=2) + "\n")

    print(f"Fetched {fetched} with data, {no_data} with none, "
          f"{errored} transient errors (left uncached) this run.")
    print(f"Cache now: {populated} populated / {len(cache)} total -> {SWORN_OUT}")


if __name__ == "__main__":
    main()
