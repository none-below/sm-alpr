#!/usr/bin/env python3
"""Fetch FBI Crime Data Explorer (CDE) monthly offense counts per ORI.

Companion to refresh_fbi_sworn.py (police-employment / sworn officers).
Both write FLAT JSON keyed by INDIVIDUAL ORI into data/fbi/; the report
build sums each agency's ``ori`` list at read time (see fbi_crime.py).

We pull only the two non-overlapping Part 1 totals — ``violent-crime``
and ``property-crime``. Their sum is total Part 1 (index) crime; the
sub-categories (robbery, burglary, larceny, motor-vehicle-theft, ...)
are subsets and would double-count if added in. Two API calls per ORI.

Endpoint (verified):
  GET /summarized/agency/{ORI}/{offense}?from=MM-YYYY&to=MM-YYYY&API_KEY=...
  -> { offenses: { actuals: { "<Agency> Offenses": {MM-YYYY: n|null}, ... } },
       cde_properties: { max_data_date, last_refresh_date }, ... }

Output — one append-only dated snapshot per ORI per fetch, mirroring the
Flock transparency scrape archive (assets/transparency.flocksafety.com/
<slug>/<date>.json):
  assets/cde.ucr.cjis.gov/<ORI>/<YYYY-MM-DD>.json
    { ori, fetched, agency_name, max_data_date, last_refresh_date,
      offenses: { violent-crime: {MM-YYYY: n}, property-crime: {...} } }

We never overwrite a prior snapshot, so the full revision history is
preserved and parallel PRs adding different ORIs/dates never conflict.
fbi_crime.load_crime() joins the snapshots into the current view.

Resumable: an ORI already snapshotted on the fetch date is skipped unless
--refresh. A full run over the matched registry is ~thousands of calls;
converge across runs.

Usage:
  python scripts/refresh_fbi_crime.py --crawled-only  # portal-having agencies
  python scripts/refresh_fbi_crime.py --ori CA0411600 # one ORI (testing)
  python scripts/refresh_fbi_crime.py --date 2026-05-16 --ori CA0411600  # backfill a date
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import load_registry, save_json  # noqa: E402

API_BASE = "https://api.usa.gov/crime/fbi/cde"
OFFENSES = ["violent-crime", "property-crime"]
SNAPSHOT_DIR = Path("assets/cde.ucr.cjis.gov")
KEY_FILE = Path.home() / ".config" / "sm-alpr" / "api_data_gov_key"

THROTTLE_S = 0.4  # ~well under the 1000 req/hour api.data.gov limit
DEFAULT_FROM = "01-2019"


def api_key():
    """API key from env DATA_GOV_API_KEY or ~/.config/sm-alpr/api_data_gov_key.

    (refresh_fbi_sworn.py exposes the same loader as ``api_key``; consolidate
    into lib once both FBI branches have merged.)
    """
    key = os.environ.get("DATA_GOV_API_KEY")
    if key:
        return key.strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    sys.exit(
        f"No API key. Set DATA_GOV_API_KEY or write one to {KEY_FILE}\n"
        "Get a free key at https://api.data.gov/signup/"
    )


def _current_month():
    t = date.today()
    return f"{t.month:02d}-{t.year}"


def fetch_offense(ori, offense, key, date_from, date_to):
    """Return (series_dict, agency_name, cde_properties) for one offense.

    series_dict maps MM-YYYY -> int for non-null months only (an explicit
    0 is kept; ``null`` = not-yet-reported is dropped). Returns
    (None, None, None) on a 404 (ORI has no series for this offense).
    Raises on unrecoverable errors after retries.
    """
    url = (
        f"{API_BASE}/summarized/agency/{ori}/{offense}"
        f"?from={date_from}&to={date_to}&API_KEY={key}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "sm-alpr/refresh_fbi_crime"})
    backoff = 2
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None, None
            if e.code == 429:
                time.sleep(60)
                continue
            if attempt == 4:
                raise
            time.sleep(backoff)
            backoff *= 2
        except (urllib.error.URLError, TimeoutError):
            if attempt == 4:
                raise
            time.sleep(backoff)
            backoff *= 2
    else:
        raise RuntimeError(f"exhausted retries for {ori}/{offense}")

    actuals = (data.get("offenses") or {}).get("actuals") or {}
    # The agency's own monthly series is the key containing "Offenses"
    # (vs. "Clearances", and vs. the statewide "California Offenses" series
    # which lives under offenses.rates, not actuals).
    series, agency_name = None, None
    for k, v in actuals.items():
        if "Offenses" in k and not k.startswith("California"):
            series, agency_name = v, k[: -len(" Offenses")].strip()
            break
    if series is None:
        return None, None, None
    clean = {m: n for m, n in series.items() if n is not None}
    return clean, agency_name, (data.get("cde_properties") or {})


GRAPH_PATH = Path("assets/transparency.flocksafety.com/.sharing_graph_full.json")


def _crawled_agency_ids():
    """agency_ids the sharing graph marks as crawled (have portal data)."""
    if not GRAPH_PATH.exists():
        return set()
    graph = json.loads(GRAPH_PATH.read_text())
    return {aid for aid, g in (graph.get("agencies") or {}).items() if g.get("crawled")}


def target_oris(args, registry):
    if args.ori:
        out = []
        for chunk in args.ori:
            out.extend(o.strip() for o in chunk.split(",") if o.strip())
        return list(dict.fromkeys(out))
    # --crawled-only: just the agencies with portal data, where the
    # searches-per-crime metric can actually be computed. Keeps the
    # default full-registry fetch (~thousands of ORIs) optional.
    entries = registry
    if args.crawled_only:
        crawled = _crawled_agency_ids()
        entries = [e for e in registry if e.get("agency_id") in crawled]
    # Flatten every agency's ori list. 99% of agencies have one ORI;
    # umbrella agencies (CHP, State Parks) carry many — we still fetch
    # each individual ORI; the reader sums per agency.
    oris = []
    for e in entries:
        for o in e.get("ori") or []:
            oris.append(o)
    return list(dict.fromkeys(oris))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ori", action="append", help="ORI(s) to fetch (repeatable / comma-separated)")
    ap.add_argument("--crawled-only", action="store_true",
                    help="limit to agencies with portal data (where the metric applies)")
    ap.add_argument("--refresh", action="store_true", help="re-pull ORIs already snapshotted on the fetch date")
    ap.add_argument("--date", default=None, help="snapshot date YYYY-MM-DD (default today; for backfills)")
    ap.add_argument("--from", dest="date_from", default=DEFAULT_FROM, help="MM-YYYY start (default 01-2019)")
    ap.add_argument("--to", dest="date_to", default=None, help="MM-YYYY end (default current month)")
    ap.add_argument("--limit", type=int, default=None, help="stop after N ORIs (debugging)")
    args = ap.parse_args()

    date_to = args.date_to or _current_month()
    fetch_date = args.date or date.today().isoformat()
    key = api_key()
    registry = load_registry()
    wanted = target_oris(args, registry)
    if not wanted:
        print("No target ORIs. Registry has no 'ori' fields yet — pass --ori for testing.")
        return

    # Resumability: skip an ORI already snapshotted on this fetch date.
    def snap_path(ori):
        return SNAPSHOT_DIR / ori / f"{fetch_date}.json"

    todo = [o for o in wanted if args.refresh or not snap_path(o).exists()]
    if args.limit:
        todo = todo[: args.limit]
    cached = len(wanted) - len(todo)
    print(f"{len(wanted)} target ORIs; {len(todo)} to snapshot for {fetch_date} ({cached} already done).")

    fetched = 0
    for i, ori in enumerate(todo, 1):
        record = {"ori": ori, "fetched": fetch_date, "agency_name": None,
                  "max_data_date": None, "last_refresh_date": None, "offenses": {}}
        for offense in OFFENSES:
            series, name, props = fetch_offense(ori, offense, key, args.date_from, date_to)
            time.sleep(THROTTLE_S)
            if series is None:
                record["offenses"][offense] = None  # explicit: no series
                continue
            record["offenses"][offense] = series
            if name and not record["agency_name"]:
                record["agency_name"] = name
            ucr = (props.get("max_data_date") or {}).get("UCR")
            if ucr:
                record["max_data_date"] = ucr
            ref = (props.get("last_refresh_date") or {}).get("UCR")
            if ref:
                record["last_refresh_date"] = ref
        save_json(snap_path(ori), record)
        fetched += 1
        flag = "" if record["agency_name"] else "  [no FBI series]"
        print(f"  [{i}/{len(todo)}] {ori}  {record['agency_name'] or '-'}{flag}")

    print(f"Wrote {fetched} snapshot(s) under {SNAPSHOT_DIR}/ for {fetch_date}.")


if __name__ == "__main__":
    main()
