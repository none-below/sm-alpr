#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Snapshot a public CitizenRIMS corpus (cases + incidents) for case-ID
hydration. Semi-automatic by design: run by hand, never from CI.

The API (api.v1.citizenrims.com) has no lookup-by-case-number endpoint, so
hydration is bulk-then-join: one full /Case pull (small) per agency, plus
one /Incident pull per month (dense periods vary by agency). Requests are
spaced with a sleep; a whole run for one agency is ~2 + #months requests
regardless of how many case IDs we hold.

CitizenRIMS is a Sun Ridge Systems product used by many CA agencies, not
just SMPD. AGENCIES below is the set confirmed (2026-09-14) to have public
incident (CAD) data enabled among San Mateo County law-enforcement agencies
-- i.e. Flock-sharing partners in this project's scope. Two SMC agencies
were probed and excluded: San Mateo County Sheriff (smcsheriff, 349 --
incidentsEnabled=False, case-only) and Daly City PD (dalycitypd, 178 --
neither incidents nor cases enabled).

Output: an append-only snapshot under assets/citizenrims/<prefix>/<YYYY-MM-DD>/
(local date):

  config.json           agency config as returned (marker groups = required
                        `types` param values)
  cases.json.gz         full /Case corpus, response bytes as returned
  incidents/<YYYY-MM>.json.gz   one per month pulled
  manifest.json         params, per-file row counts, fetch timestamps

Responses are stored verbatim (gzipped, mtime=0); all filtering and joining
happens downstream. Number formats (do not confuse; format varies by agency
-- confirmed for SMPD, check before assuming elsewhere):
  incidentNumber = YYYYMMDDNNNN — audit-log CAD numbers with century prefix
  caseNumber     = YYMMDDNNN    — separate report sequence, not derivable

Usage:
  python3 scripts/refresh_citizenrims.py --agency sanmateopd
  python3 scripts/refresh_citizenrims.py --agency menlopark --start 2026-07 --end 2026-08
  python3 scripts/refresh_citizenrims.py --all              # every AGENCIES entry
  python3 scripts/refresh_citizenrims.py --agency redwoodcity --cases-only
"""

import argparse
import datetime as dt
import gzip
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.v1.citizenrims.com"
UA = "sm-alpr research (public records research; contact: brian@zerobelow.org)"

# prefix -> (agencyId, display name). San Mateo County agencies confirmed
# 2026-09-14 with incidentsEnabled=True (public CAD data).
AGENCIES = {
    "sanmateopd": (362, "San Mateo PD"),
    "southsanfranciscopd": (63, "South San Francisco PD"),
    "menlopark": (797, "Menlo Park PD"),
    "redwoodcity": (717, "Redwood City PD"),
    "belmont": (1035, "Belmont PD"),
    "sanbruno": (199, "San Bruno PD"),
    "burlingame": (198, "Burlingame PD"),
    "pacifica": (186, "Pacifica PD"),
    "hillsborough": (1107, "Hillsborough PD"),
    "brisbane": (638, "Brisbane PD"),
    "atherton": (192, "Atherton PD"),
}


def request(path, params=None, token=None, method="GET"):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", UA)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if method == "POST":
        req.add_header("Content-Length", "0")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def write_gz(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            gz.write(data)


def months(start, end):
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def month_bounds(ym):
    y, m = map(int, ym.split("-"))
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def snapshot_agency(prefix, agency_id, token, args, out_root):
    today = dt.date.today()
    out = out_root / prefix / f"{today:%Y-%m-%d}"
    if out.exists():
        print(f"skip {prefix}: {out} already exists (snapshots are append-only, "
              "remove it yourself to re-fetch today)")
        return
    out.mkdir(parents=True)
    manifest = {
        "fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "base": BASE, "prefix": prefix, "agency_id": agency_id,
        "params": {"start": args.start, "end": args.end, "cases_only": args.cases_only},
        "files": {},
    }

    time.sleep(args.sleep)
    cfg_bytes = request("/api/v1/AgencyConfig/AgencyConfigGetByUrlPrefix",
                        {"citizenRimsUrlPrefix": prefix}, token)
    (out / "config.json").write_bytes(cfg_bytes)
    cfg = json.loads(cfg_bytes)
    itypes = ",".join(g["groupFieldName"] for g in cfg["incidentMarkerGroups"])
    ctypes = ",".join(g["groupFieldName"] for g in cfg["caseMarkerGroups"])
    manifest["files"]["config.json"] = {"bytes": len(cfg_bytes)}
    print(f"{prefix}: config ok ({len(cfg['incidentMarkerGroups'])} incident / "
          f"{len(cfg['caseMarkerGroups'])} case groups)")

    # Some agencies are hosted under a different tenant's primaryAgencyId in
    # Sun Ridge's system (e.g. San Bruno's data lives under Burlingame's
    # primaryAgencyId) -- trust the config's own value, not our agencyId guess.
    primary_agency_id = cfg.get("primaryAgencyId", agency_id)
    if primary_agency_id != agency_id:
        print(f"{prefix}: primaryAgencyId {primary_agency_id} != agencyId {agency_id} "
              "(hosted under another tenant)")
        manifest["primary_agency_id"] = primary_agency_id
    common = {"agencyId": agency_id, "primaryAgencyId": primary_agency_id,
              "circleLatitude": 0, "circleLongitude": 0, "circleRadius": 0}

    time.sleep(args.sleep)
    cases = request("/api/v1/Case", common | {
        "startDate": "2019-01-01", "endDate": f"{today:%Y-%m-%d}", "types": ctypes}, token)
    write_gz(out / "cases.json.gz", cases)
    n = len(json.loads(cases))
    manifest["files"]["cases.json.gz"] = {"rows": n, "raw_bytes": len(cases)}
    print(f"{prefix}: cases: {n} rows")

    if not args.cases_only:
        for ym in months(args.start, args.end):
            time.sleep(args.sleep)
            s, e = month_bounds(ym)
            data = request("/api/v1/Incident", common | {
                "startDate": s, "endDate": e, "types": itypes}, token)
            write_gz(out / "incidents" / f"{ym}.json.gz", data)
            n = len(json.loads(data))
            manifest["files"][f"incidents/{ym}.json.gz"] = {"rows": n, "raw_bytes": len(data)}
            print(f"{prefix}: incidents {ym}: {n} rows")

    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"{prefix}: snapshot written to {out}")


def main():
    today = dt.date.today()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agency", action="append", choices=sorted(AGENCIES),
                    help="agency prefix to snapshot; repeatable (default: all)")
    ap.add_argument("--all", action="store_true", help="snapshot every known agency")
    ap.add_argument("--start", default="2025-01", help="first incident month (YYYY-MM)")
    ap.add_argument("--end", default=f"{today:%Y-%m}", help="last incident month (YYYY-MM)")
    ap.add_argument("--sleep", type=float, default=5.0, help="seconds between requests")
    ap.add_argument("--cases-only", action="store_true", help="skip incident pulls")
    ap.add_argument("--out-root", type=Path,
                    default=Path(__file__).resolve().parent.parent / "assets/citizenrims")
    args = ap.parse_args()

    if args.all:
        prefixes = sorted(AGENCIES)
    elif args.agency:
        prefixes = args.agency
    else:
        sys.exit("specify --agency <prefix> (repeatable) or --all")

    token = json.loads(request("/api/v1/auth/citizen", method="POST"))["token"]
    print("auth ok")

    for prefix in prefixes:
        agency_id, name = AGENCIES[prefix]
        print(f"--- {name} ({prefix}, agencyId {agency_id}) ---")
        snapshot_agency(prefix, agency_id, token, args, args.out_root)


if __name__ == "__main__":
    main()
