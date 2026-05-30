#!/usr/bin/env python3
"""Unified FBI Crime Data Explorer fetcher — one tool, a --dataset switch.

The CDE per-ORI fetchers (police-employment / sworn officers, and crime
offenses) share almost everything: target selection from the registry's `ori`
lists, an oldest-first rolling refresh, dated append-only snapshots that store
only what changed vs the joined prior view, rate-limit/retry, resume. Only the
endpoint, parser, storage dir, and snapshot shape differ — so those live in a
small per-dataset SPEC and the shared core (this file) does the rest.

Datasets:
  pe     police employment → data/fbi/sworn/<ORI>/<YYYY-MM-DD>.json {by_year}
  crime  CDE monthly offenses → assets/cde.ucr.cjis.gov/<ORI>/<YYYY-MM-DD>.json
         {offenses, agency_name, max_data_date, last_refresh_date}

Snapshots are append-only and per-ORI, so parallel PRs never conflict and the
full revision history is preserved. Readers join the snapshots (lib.load_sworn
/ fbi_crime.load_crime). A sidecar `<dataset>_scrape_state.json` records when we
last *checked* each ORI (even when nothing changed) to drive oldest-first.

API key: env DATA_GOV_API_KEY or ~/.config/sm-alpr/api_data_gov_key.

Usage:
  uv run python scripts/refresh_fbi.py --dataset pe --slug san-mateo-ca-pd
  uv run python scripts/refresh_fbi.py --dataset pe --all --limit 800
  uv run python scripts/refresh_fbi.py --dataset pe --all --refresh
"""

import argparse
import glob
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_fbi_agencies import api_key  # noqa: E402
from lib import join_sworn_snapshots  # noqa: E402
from fbi_crime import join_snapshots as join_crime_snapshots  # noqa: E402

REGISTRY_PATH = Path("assets/agency_registry.json")
PORTAL_DIR = Path("assets/transparency.flocksafety.com")
GRAPH_PATH = PORTAL_DIR / ".sharing_graph_full.json"


# ── HTTP ─────────────────────────────────────────────────────────

def _get(url, retries=5):
    """GET JSON. Returns ("ok", obj) / ("nodata", None) on 404 /
    ("error", None) after exhausting retries (caller leaves it unchecked)."""
    backoff = 2
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sm-alpr/refresh_fbi"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return "ok", json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "nodata", None
            if e.code == 429:
                time.sleep(60)
                continue
            if attempt == retries - 1:
                return "error", None
            time.sleep(backoff); backoff *= 2
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            if attempt == retries - 1:
                return "error", None
            time.sleep(backoff); backoff *= 2
    return "error", None


# ── pe (police employment / sworn officers) dataset spec ─────────

PE_URL = "https://api.usa.gov/crime/fbi/cde/pe/agency/{ori}?from={y0}&to={y1}&API_KEY={key}"
PE_FROM_YEAR = 2015  # enough recent history for year-accurate per-officer metrics


def _pe_parse(payload):
    """{"<year>": {officers, civilians, total}} for years with officer data."""
    actuals = payload.get("actuals") or {}
    mo = actuals.get("Male Officers") or {}
    fo = actuals.get("Female Officers") or {}
    mc = actuals.get("Male Civilians") or {}
    fc = actuals.get("Female Civilians") or {}
    out = {}
    for y in sorted(mo.keys() | fo.keys()):
        m, f = mo.get(y), fo.get(y)
        if m is None and f is None:
            continue
        officers = (m or 0) + (f or 0)
        civilians = (mc.get(y) or 0) + (fc.get(y) or 0)
        out[y] = {"officers": officers, "civilians": civilians,
                  "total": officers + civilians}
    return out


def _pe_fetch(ori, key, ctx):
    status, payload = _get(PE_URL.format(ori=ori, y0=PE_FROM_YEAR, y1=ctx["to_year"], key=key))
    if status != "ok":
        return status, {}
    return "ok", _pe_parse(payload)


PE_SPEC = {
    "name": "pe",
    "snapshot_dir": Path("data/fbi/sworn"),
    "state_path": Path("data/fbi/sworn_scrape_state.json"),
    "fetch": _pe_fetch,
    "join": join_sworn_snapshots,                       # snaps -> by_year view
    "diff": lambda view, prior: {y: r for y, r in view.items() if prior.get(y) != r},
    "record": lambda ori, d, delta: {"ori": ori, "fetched": d, "by_year": delta},
    "ctx": lambda args: {"to_year": date.today().year},
}


# ── crime (CDE monthly offense counts) dataset spec ──────────────
# Two non-overlapping Part 1 totals; their sub-categories are subsets, so we
# never add them in. Each ORI = two API calls. Output matches the original
# refresh_fbi_crime.py exactly so the existing snapshots + fbi_crime reader
# keep working: assets/cde.ucr.cjis.gov/<ORI>/<date>.json with delta-only
# offense months + agency_name / max_data_date / last_refresh_date.

CRIME_URL = ("https://api.usa.gov/crime/fbi/cde/summarized/agency/{ori}/{offense}"
             "?from={f}&to={t}&API_KEY={key}")
CRIME_OFFENSES = ["violent-crime", "property-crime"]
CRIME_FROM = "01-2019"


def _crime_month():
    t = date.today()
    return f"{t.month:02d}-{t.year}"


def _crime_fetch(ori, key, ctx):
    """Pull both offenses for one ORI. Returns ("ok", view) with view =
    {offenses:{off:{MM-YYYY:n}}, agency_name, max_data_date, last_refresh_date},
    ("nodata", {}) if neither offense reports, ("error", None) on hard failure."""
    view = {"offenses": {}, "agency_name": None,
            "max_data_date": None, "last_refresh_date": None}
    for i, offense in enumerate(CRIME_OFFENSES):
        if i:
            time.sleep(ctx["delay"])  # throttle between the two calls
        status, payload = _get(CRIME_URL.format(
            ori=ori, offense=offense, f=ctx["date_from"], t=ctx["date_to"], key=key))
        if status == "error":
            return "error", None
        if status == "nodata" or payload is None:
            continue
        actuals = (payload.get("offenses") or {}).get("actuals") or {}
        # The agency's own series is the "<Agency> Offenses" key (not the
        # statewide "California Offenses" rate series).
        series = name = None
        for k, v in actuals.items():
            if "Offenses" in k and not k.startswith("California"):
                series, name = v, k[: -len(" Offenses")].strip()
                break
        if series is None:
            continue
        view["offenses"][offense] = {m: n for m, n in series.items() if n is not None}
        if name:
            view["agency_name"] = name
        props = payload.get("cde_properties") or {}
        ucr = (props.get("max_data_date") or {}).get("UCR")
        if ucr:
            view["max_data_date"] = ucr
        ref = (props.get("last_refresh_date") or {}).get("UCR")
        if ref:
            view["last_refresh_date"] = ref
    return ("ok", view) if view["offenses"] else ("nodata", {})


def _crime_diff(view, prior):
    """Months that are new or revised vs the joined prior view, per offense.
    Empty (falsy) when nothing changed."""
    prior_off = prior.get("offenses") or {}
    offenses_delta = {}
    for offense, series in (view.get("offenses") or {}).items():
        base = prior_off.get(offense) or {}
        d = {m: n for m, n in series.items() if base.get(m) != n}
        if d:
            offenses_delta[offense] = d
    if not offenses_delta:
        return {}
    return {"offenses": offenses_delta,
            "agency_name": view.get("agency_name") or prior.get("agency_name"),
            "max_data_date": view.get("max_data_date"),
            "last_refresh_date": view.get("last_refresh_date")}


CRIME_SPEC = {
    "name": "crime",
    "snapshot_dir": Path("assets/cde.ucr.cjis.gov"),
    "state_path": Path("data/fbi/crime_scrape_state.json"),
    "fetch": _crime_fetch,
    "join": join_crime_snapshots,
    "diff": _crime_diff,
    "record": lambda ori, d, delta: {
        "ori": ori, "fetched": d,
        "agency_name": delta.get("agency_name"),
        "max_data_date": delta.get("max_data_date"),
        "last_refresh_date": delta.get("last_refresh_date"),
        "offenses": delta["offenses"],
    },
    "ctx": lambda args: {"date_from": args.date_from or CRIME_FROM,
                         "date_to": args.date_to or _crime_month(),
                         "delay": args.delay},
}

SPECS = {"pe": PE_SPEC, "crime": CRIME_SPEC}


# ── target selection (shared) ────────────────────────────────────

def _by_name(registry):
    return {n: e for e in registry for n in e.get("flock_names", [])}


def responsive_oris(registry):
    """ORIs of agencies that are some portal's OUTBOUND recipient."""
    by_name = _by_name(registry)
    oris = set()
    for d in sorted(PORTAL_DIR.glob("*/")):
        scrapes = sorted(glob.glob(str(d / "2*.json")))
        if not scrapes:
            continue
        data = json.loads(Path(scrapes[-1]).read_text())
        for n in (data.get("sharing_outbound") or data.get("shared_org_names") or []):
            e = by_name.get(n)
            if e:
                oris.update(e.get("ori") or [])
    return oris


def slug_recipient_oris(registry, slug):
    by_name = _by_name(registry)
    scrapes = sorted(glob.glob(str(PORTAL_DIR / slug / "2*.json")))
    if not scrapes:
        sys.exit(f"ERROR: no portal scrape for slug {slug}")
    data = json.loads(Path(scrapes[-1]).read_text())
    oris = set()
    for n in (data.get("sharing_outbound") or data.get("shared_org_names") or []):
        e = by_name.get(n)
        if e:
            oris.update(e.get("ori") or [])
    return oris


def crawled_oris(registry):
    """ORIs of agencies the sharing graph marks as crawled (have portal data) —
    the set where crime's searches-per-crime metric can be computed."""
    if not GRAPH_PATH.exists():
        return set()
    graph = json.loads(GRAPH_PATH.read_text())
    crawled = {aid for aid, g in (graph.get("agencies") or {}).items() if g.get("crawled")}
    oris = set()
    for e in registry:
        if e.get("agency_id") in crawled:
            oris.update(e.get("ori") or [])
    return oris


# ── shared run loop ──────────────────────────────────────────────

def _prior_view(spec, ori, fetch_date):
    """Joined view from this ORI's snapshots dated strictly before fetch_date
    (so same-day re-runs are idempotent)."""
    d = spec["snapshot_dir"] / ori
    if not d.is_dir():
        return {}
    snaps = []
    for sp in sorted(d.glob("*.json")):
        if sp.stem >= fetch_date:
            continue
        try:
            snaps.append(json.loads(sp.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return spec["join"](snaps)


def run(spec, targets, key, args):
    spec["snapshot_dir"].mkdir(parents=True, exist_ok=True)
    state_path = spec["state_path"]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    fetch_date = args.date or date.today().isoformat()
    ctx = spec["ctx"](args)

    # Oldest-first: unchecked ORIs (no state) sort first, then by last-checked.
    todo = sorted(targets, key=lambda o: state.get(o, ""))
    if not args.refresh:
        todo = [o for o in todo if state.get(o) != fetch_date]
    print(f"[{spec['name']}] targets: {len(targets)} | checked today: "
          f"{len(targets) - len(todo)} | queue: {len(todo)}")

    wrote = unchanged = errored = 0
    for ori in todo:
        if args.limit and (wrote + unchanged) >= args.limit:
            break
        status, view = spec["fetch"](ori, key, ctx)
        if status == "error":
            errored += 1  # don't record a check — retry next run
            time.sleep(args.delay)
            continue
        state[ori] = fetch_date  # record the check even when nothing changed
        delta = spec["diff"](view, _prior_view(spec, ori, fetch_date))
        if delta:
            path = spec["snapshot_dir"] / ori / f"{fetch_date}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(spec["record"](ori, fetch_date, delta),
                                       indent=2, sort_keys=True) + "\n")
            wrote += 1
        else:
            unchanged += 1
        time.sleep(args.delay)

    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    print(f"[{spec['name']}] wrote {wrote} snapshot(s), {unchanged} unchanged/no-data, "
          f"{errored} transient errors -> {spec['snapshot_dir']}/")


def main():
    ap = argparse.ArgumentParser(description="Unified FBI CDE per-ORI fetcher")
    ap.add_argument("--dataset", choices=sorted(SPECS), default="pe")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="Every distinct ORI in the registry")
    g.add_argument("--crawled-only", action="store_true",
                   help="Only ORIs of agencies with portal data (sharing graph)")
    g.add_argument("--slug", help="Only the ORIs a single portal shares outbound to")
    g.add_argument("--ori", action="append",
                   help="Explicit ORI(s) to fetch (repeatable / comma-separated; testing)")
    ap.add_argument("--refresh", action="store_true", help="Re-check even ORIs checked today")
    ap.add_argument("--delay", type=float, default=0.4, help="Seconds between calls")
    ap.add_argument("--limit", type=int, help="Stop after N fetches (oldest-first budget)")
    # crime-only knobs (ignored by pe): MM-YYYY window + snapshot-date override.
    ap.add_argument("--from", dest="date_from", default=None, help="crime: MM-YYYY start")
    ap.add_argument("--to", dest="date_to", default=None, help="crime: MM-YYYY end")
    ap.add_argument("--date", default=None, help="snapshot date YYYY-MM-DD (default today)")
    args = ap.parse_args()

    spec = SPECS[args.dataset]
    key = api_key()
    registry = json.loads(REGISTRY_PATH.read_text())
    if args.ori:
        targets = set()
        for chunk in args.ori:
            targets.update(o.strip() for o in chunk.split(",") if o.strip())
    elif args.slug:
        targets = slug_recipient_oris(registry, args.slug)
    elif args.crawled_only:
        targets = crawled_oris(registry)
    elif args.all:
        targets = {o for e in registry for o in (e.get("ori") or [])}
    else:
        targets = responsive_oris(registry)
    run(spec, targets, key, args)


if __name__ == "__main__":
    main()
