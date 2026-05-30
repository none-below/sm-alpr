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

  (crime will be added as a second spec when its action is migrated off the
   standalone refresh_fbi_crime.py — same core, a CrimeSpec with the
   /summarized endpoint and offense-by-month snapshots.)

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

REGISTRY_PATH = Path("assets/agency_registry.json")
PORTAL_DIR = Path("assets/transparency.flocksafety.com")


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
    "ctx": lambda: {"to_year": date.today().year},
}

SPECS = {"pe": PE_SPEC}


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
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    fetch_date = date.today().isoformat()
    ctx = spec["ctx"]()

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
    g.add_argument("--slug", help="Only the ORIs a single portal shares outbound to")
    ap.add_argument("--refresh", action="store_true", help="Re-check even ORIs checked today")
    ap.add_argument("--delay", type=float, default=0.4, help="Seconds between calls")
    ap.add_argument("--limit", type=int, help="Stop after N fetches (oldest-first budget)")
    args = ap.parse_args()

    spec = SPECS[args.dataset]
    key = api_key()
    registry = json.loads(REGISTRY_PATH.read_text())
    if args.slug:
        targets = slug_recipient_oris(registry, args.slug)
    elif args.all:
        targets = {o for e in registry for o in (e.get("ori") or [])}
    else:
        targets = responsive_oris(registry)
    run(spec, targets, key, args)


if __name__ == "__main__":
    main()
