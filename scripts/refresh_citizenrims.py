#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Snapshot SMPD's public CitizenRIMS corpus (cases + incidents) for case-ID
hydration. Semi-automatic by design: run by hand, never from CI.

The API (api.v1.citizenrims.com, agencyId 362) has no lookup-by-case-number
endpoint, so hydration is bulk-then-join: one full /Case pull (small), plus
one /Incident pull per month (dense only from ~March 2025). Requests are
spaced with a sleep; a whole run is ~2 + #months requests regardless of how
many case IDs we hold.

Output: an append-only snapshot under assets/citizenrims/<YYYY-MM-DD>/
(local date):

  config.json           agency config as returned (marker groups = required
                        `types` param values)
  cases.json.gz         full /Case corpus, response bytes as returned
  incidents/<YYYY-MM>.json.gz   one per month pulled
  manifest.json         params, per-file row counts, fetch timestamps

Responses are stored verbatim (gzipped, mtime=0); all filtering and joining
happens downstream. Number formats (do not confuse):
  incidentNumber = YYYYMMDDNNNN — audit-log CAD numbers with century prefix
  caseNumber     = YYMMDDNNN    — separate report sequence, not derivable

Usage:
  python3 scripts/refresh_citizenrims.py                # 2025-01 .. current month
  python3 scripts/refresh_citizenrims.py --start 2026-07 --end 2026-08
  python3 scripts/refresh_citizenrims.py --cases-only
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
PREFIX = "sanmateopd"
AGENCY = 362
UA = "sm-alpr research (public records research; contact: brian@zerobelow.org)"


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


def main():
    today = dt.date.today()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2025-01", help="first incident month (YYYY-MM)")
    ap.add_argument("--end", default=f"{today:%Y-%m}", help="last incident month (YYYY-MM)")
    ap.add_argument("--sleep", type=float, default=5.0, help="seconds between requests")
    ap.add_argument("--cases-only", action="store_true", help="skip incident pulls")
    ap.add_argument("--out-root", type=Path,
                    default=Path(__file__).resolve().parent.parent / "assets/citizenrims")
    args = ap.parse_args()

    out = args.out_root / f"{today:%Y-%m-%d}"
    if out.exists():
        sys.exit(f"snapshot dir {out} already exists; snapshots are append-only "
                 "(remove it yourself to re-fetch today)")
    out.mkdir(parents=True)
    manifest = {
        "fetched_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "base": BASE, "agency_id": AGENCY, "params": vars(args) | {"out_root": str(args.out_root)},
        "files": {},
    }

    token = json.loads(request("/api/v1/auth/citizen", method="POST"))["token"]
    print("auth ok")
    time.sleep(args.sleep)

    cfg_bytes = request("/api/v1/AgencyConfig/AgencyConfigGetByUrlPrefix",
                        {"citizenRimsUrlPrefix": PREFIX}, token)
    (out / "config.json").write_bytes(cfg_bytes)
    cfg = json.loads(cfg_bytes)
    itypes = ",".join(g["groupFieldName"] for g in cfg["incidentMarkerGroups"])
    ctypes = ",".join(g["groupFieldName"] for g in cfg["caseMarkerGroups"])
    manifest["files"]["config.json"] = {"bytes": len(cfg_bytes)}
    print(f"config ok ({len(cfg['incidentMarkerGroups'])} incident / "
          f"{len(cfg['caseMarkerGroups'])} case groups)")
    time.sleep(args.sleep)

    common = {"agencyId": AGENCY, "primaryAgencyId": AGENCY,
              "circleLatitude": 0, "circleLongitude": 0, "circleRadius": 0}

    cases = request("/api/v1/Case", common | {
        "startDate": "2019-01-01", "endDate": f"{today:%Y-%m-%d}", "types": ctypes}, token)
    write_gz(out / "cases.json.gz", cases)
    n = len(json.loads(cases))
    manifest["files"]["cases.json.gz"] = {"rows": n, "raw_bytes": len(cases)}
    print(f"cases: {n} rows")

    if not args.cases_only:
        for ym in months(args.start, args.end):
            time.sleep(args.sleep)
            s, e = month_bounds(ym)
            data = request("/api/v1/Incident", common | {
                "startDate": s, "endDate": e, "types": itypes}, token)
            write_gz(out / "incidents" / f"{ym}.json.gz", data)
            n = len(json.loads(data))
            manifest["files"][f"incidents/{ym}.json.gz"] = {"rows": n, "raw_bytes": len(data)}
            print(f"incidents {ym}: {n} rows")

    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"snapshot written to {out}")


if __name__ == "__main__":
    main()
