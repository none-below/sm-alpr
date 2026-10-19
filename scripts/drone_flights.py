#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Drone flight-log archiver. First backend: Skydio DFR transparency dashboards.

Skydio's public "Drone Flights" pages (cloud.skydio.com/dashboard/<org uuid or
vanity path>, e.g. /dashboard/sanmateopd) are a wrapper around an ArcGIS
Online dashboard. The flights themselves are an anonymous, public ArcGIS
FeatureServer in Skydio's ArcGIS org, one per Skydio organization:

  https://services7.arcgis.com/mnhQTdIYDA7UoY2l/arcgis/rest/services/
      <org-uuid>-production/FeatureServer/0          layer "Operation"

One polyline feature per flight: the 2D track (no altitude) plus flight_id,
takeoff/landing (epoch ms), flight_purpose, external_id (case number),
description, organization_id. The schema also carries user_email ("Pilot"),
vehicle_serial, dock_serial and operation_id, which Skydio leaves null.

Discovery is a listing, not guessing: the org's REST services directory lists
every <org-uuid>-production service ("Feature service for <agency>" in its
serviceDescription), whether or not the agency published a dashboard. Public
dashboards are found by ArcGIS search and tied to an org through the web map
they embed (titled "<org-uuid>-production-web-map").

Flights are an event log, not a page: each run pulls every flight's attributes
(no geometry), diffs them against what's stored by flight_id, and records
additions, edits and removals. Tracks are fixed once a flight lands, so each is
fetched once, hashed, and referenced by sha256; the bytes live outside git.

Commands:
  discover        List services + public dashboards; write/refresh org.json for
                  every service (identity, dashboards, registry link, events).
  crawl           Pull flights for listed orgs whose layer changed (or every
                  --max-age days); record changes; hash new tracks.
  fetch-geometry  Fill the local track store from the sha256s in git, checking
                  each served track still hashes to the recorded value. Never
                  writes git-tracked files.
  sync-artifacts  Pull the CI runs' track artifacts into the local store,
                  verifying each blob against its sha256 name; record progress
                  in the DRONE_TRACKS_SYNCED_THROUGH repo variable.
  artifact-reminder
                  (CI) Open one issue while an unsynced artifact is 60+ days
                  old (they expire at 90); close it once a sync catches up.
  report          Table of orgs with flights (--markdown for PR bodies).
  db              Build a local DuckDB of orgs, flights, changes and (when the
                  local store has them) tracks. Needs duckdb:
                    uv run --with duckdb==1.5.5 python scripts/drone_flights.py db

Layout (times are UTC):
  assets/cloud.skydio.com/
    <org-uuid>/
      org.json                  identity, dashboards, registry link, events
      flights/<YYYY-MM>.ndjson  one line per flight currently served, by takeoff
                                month, ObjectId order: {"attributes": <as
                                served>, "geometry": {sha256, points, parts}}
                                ("geometry": null = not hashed yet)
      changes.ndjson            append-only: baseline | added | modified |
                                removed (with the full prior line) |
                                geometry_changed
    .state.json                 per-org crawl bookkeeping + unresolved dashboards

Track store (local only, never committed; S3 later). CI runs crawl with
--geometry store into a temp dir and uploads it as that run's artifact, so a
track survives even if the flight is deleted before anyone syncs locally —
as long as sync-artifacts runs within the 90-day retention:
  $DRONE_GEOMETRY_DIR or <primary checkout>/.claude/drone_geometry/
    <sha256[:2]>/<sha256>.json.gz   canonical Esri JSON geometry, gzip mtime=0
    mismatches.ndjson               fetch-geometry hash mismatches

The sha256 is over the canonical serialization of the geometry object as
served with f=json: json.dumps(geometry, sort_keys=True, separators=(",", ":")).

Usage:
  uv run python scripts/drone_flights.py discover
  uv run python scripts/drone_flights.py crawl
  uv run python scripts/drone_flights.py crawl 513083d4-ca53-4a0d-a555-03ab785b594c --geometry store
  uv run python scripts/drone_flights.py fetch-geometry --limit 5000
  uv run python scripts/drone_flights.py report --markdown
"""

import functools
print = functools.partial(print, flush=True)

import argparse
import collections
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import load_json, load_registry, save_json  # noqa: E402

DATA_DIR = Path("assets/cloud.skydio.com")
STATE_FILE = ".state.json"
ORG_FILE = "org.json"
FLIGHTS_DIR = "flights"
CHANGES_FILE = "changes.ndjson"
USER_AGENT = "sm-alpr drone_flights (public-records research; github.com/none-below/sm-alpr)"

ARCGIS_ORG = "mnhQTdIYDA7UoY2l"
SERVICES_URL = f"https://services7.arcgis.com/{ARCGIS_ORG}/arcgis/rest/services"
SEARCH_URL = "https://www.arcgis.com/sharing/rest/search"
ITEMS_URL = "https://www.arcgis.com/sharing/rest/content/items"
DASHBOARD_APP_URL = "https://www.arcgis.com/apps/dashboards/"
SERVICE_SUFFIX = "-production"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
UUID_RE = re.compile(rf"^{UUID}$")
WEBMAP_RE = re.compile(rf"^({UUID})-production-web-map$")
ITEM_ID_RE = re.compile(r"[0-9a-f]{32}")
DESC_PREFIX = "Feature service for "

REPO = "none-below/sm-alpr"
# CI keeps each run's new track blobs as an Actions artifact named
# drone-tracks-<run id>-<attempt> (90-day retention). sync-artifacts pulls them
# into the local store and records how far it got in this repo variable, which
# the workflow's artifact-reminder step reads.
ARTIFACT_PREFIX = "drone-tracks-"
SYNC_VARIABLE = "DRONE_TRACKS_SYNCED_THROUGH"
SYNC_LEDGER = "synced_artifacts.json"
ARTIFACT_RETENTION_DAYS = 90
ARTIFACT_WARN_DAYS = 60
REMINDER_TITLE = "Drone tracks: sync the local store before CI artifacts expire"
BLOB_ENTRY_RE = re.compile(r"^([0-9a-f]{2})/([0-9a-f]{64})\.json\.gz$")

PAGE = 2000          # the layers' maxRecordCount
GEOM_BATCH = 50      # tracks per geometry request (a track can be ~9k points)
DEFAULT_DELAY = 0.2
DEFAULT_MAX_AGE = 7
GEOMETRY_MODES = ("hash", "store", "skip")
# Reassigned if Skydio reloads a layer; flight_id is the identity.
VOLATILE_FIELDS = ("ObjectId",)
# A candidate agency must sit inside the flights' extent, padded by this many
# degrees. Extents are clamped to valid lon/lat first: some layers carry
# garbage GPS points (lon -205, lat 688) that would otherwise defeat the check.
LINK_PAD_DEG = 0.25


class RateLimited(Exception):
    """The host answered 429 — stop the run rather than push through."""


class ArcGISError(Exception):
    """ArcGIS answered 200 with an {"error": ...} body (e.g. 499 token required)."""


class IncompleteFetch(Exception):
    """Fewer records came back than the layer's count; nothing is written."""


# ── time helpers ────────────────────────────────────────────────

def utc_now():
    return datetime.now(timezone.utc)


def ms_iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms_iso_full(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def month_of(attrs):
    ms = attrs.get("takeoff")
    if ms is None:
        return "unknown"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m")


# ── HTTP ────────────────────────────────────────────────────────

class Client:
    def __init__(self, session=None, delay=DEFAULT_DELAY, backoff=5.0, retries=4):
        if session is None:
            import requests
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
        self.session = session
        self.delay = delay
        self.backoff = backoff
        self.retries = retries

    def _get(self, url, params):
        err = None
        for attempt in range(self.retries):
            if self.delay:
                time.sleep(self.delay)
            try:
                r = self.session.get(url, params=params, timeout=120)
            except Exception as e:  # connection reset, timeout, TLS
                err = e
            else:
                if r.status_code == 429:
                    raise RateLimited(url)
                if r.status_code < 500:
                    if r.status_code >= 400:
                        raise ArcGISError(f"HTTP {r.status_code} for {url}")
                    return r
                err = ArcGISError(f"HTTP {r.status_code} for {url}")
            time.sleep(self.backoff * (attempt + 1))
        raise err

    def get_json(self, url, **params):
        d = self._get(url, params).json()
        if isinstance(d, dict) and "error" in d:
            raise ArcGISError(f"{url}: {d['error']}")
        return d

    def get_text(self, url, **params):
        return self._get(url, params).text


def service_url(org_id):
    return f"{SERVICES_URL}/{org_id}{SERVICE_SUFFIX}/FeatureServer"


def layer_url(org_id):
    return f"{service_url(org_id)}/0"


# ── discovery ───────────────────────────────────────────────────

def list_services(client):
    """Org uuids of every <uuid>-production service in the directory."""
    d = client.get_json(SERVICES_URL, f="json")
    out = []
    for s in d.get("services", []):
        name = s.get("name", "")
        if name.endswith(SERVICE_SUFFIX) and UUID_RE.match(name[: -len(SERVICE_SUFFIX)]):
            out.append(name[: -len(SERVICE_SUFFIX)])
    return sorted(set(out))


def search_items(client, q):
    out, start = [], 1
    while start and start > 0:
        d = client.get_json(SEARCH_URL, q=q, num=100, start=start, f="json")
        out += d.get("results", [])
        start = d.get("nextStart", -1)
    return out


def find_dashboards(client):
    """({org_uuid: [dashboard]}, [unresolved dashboard]) for the public ArcGIS
    dashboards in Skydio's org. A dashboard belongs to the org whose
    production web map its definition references."""
    maps = {}
    for m in search_items(client, f'orgid:{ARCGIS_ORG} type:"Web Map"'):
        mo = WEBMAP_RE.match(m.get("title") or "")
        if mo:
            maps[m["id"]] = mo.group(1)
    by_org, unresolved = {}, []
    for d in search_items(client, f'orgid:{ARCGIS_ORG} type:"Dashboard"'):
        # Some definitions come back as an empty body; that's "unresolved".
        text = client.get_text(f"{ITEMS_URL}/{d['id']}/data", f="json")
        orgs = sorted({maps[i] for i in ITEM_ID_RE.findall(text) if i in maps})
        summary = {
            "id": d["id"],
            "title": d.get("title"),
            "access": d.get("access"),
            "created": ms_iso(d.get("created")),
            "url": DASHBOARD_APP_URL + d["id"],
        }
        if not orgs:
            unresolved.append(summary)
        for o in orgs:
            by_org.setdefault(o, []).append(summary)
    for v in by_org.values():
        v.sort(key=lambda s: s["id"])
    unresolved.sort(key=lambda s: s["id"])
    return by_org, unresolved


def agency_name_from(description):
    d = (description or "").strip()
    if d.startswith(DESC_PREFIX):
        d = d[len(DESC_PREFIX):]
    return re.sub(r"\s+", " ", d).strip() or None


def new_org(org_id, today):
    return {
        "org_id": org_id,
        "service_url": service_url(org_id),
        "agency_name": None,
        "description": None,
        "dashboards": [],
        "agency_id": None,
        "ori": [],
        "match": None,
        "first_seen": today,
        "listed": True,
        "layer": {"fields": []},
        "flights": {"count": 0, "first_takeoff": None, "last_takeoff": None, "unhashed": 0},
        "events": [],
    }


def apply_discovery(org, *, today, listed, description=None, dashboards=None):
    """Fold one discovery observation into org.json; returns event dicts added."""
    events = []
    if listed != org.get("listed", True):
        events.append({"date": today, "event": "relisted" if listed else "unlisted"})
        org["listed"] = listed
    if description is not None and description != org.get("description"):
        if org.get("description") is not None:
            events.append({"date": today, "event": "description_changed",
                           "old": org["description"], "new": description})
        org["description"] = description
        org["agency_name"] = agency_name_from(description)
    if dashboards is not None:
        old = {d["id"]: d for d in org.get("dashboards", [])}
        new = {d["id"]: d for d in dashboards}
        for i in sorted(new.keys() - old.keys()):
            events.append({"date": today, "event": "dashboard_published", "dashboard": i,
                           "title": new[i]["title"]})
        for i in sorted(old.keys() - new.keys()):
            events.append({"date": today, "event": "dashboard_removed", "dashboard": i,
                           "title": old[i]["title"]})
        for i in sorted(new.keys() & old.keys()):
            for k in ("title", "access"):
                if new[i].get(k) != old[i].get(k):
                    events.append({"date": today, "event": f"dashboard_{k}_changed", "dashboard": i,
                                   "old": old[i].get(k), "new": new[i].get(k)})
        org["dashboards"] = [new[i] for i in sorted(new)]
    org.setdefault("events", []).extend(events)
    return events


def cmd_discover(args, client=None, data_dir=DATA_DIR, today=None):
    client = client or Client(delay=args.delay)
    today = today or utc_now().date().isoformat()
    services = list_services(client)
    print(f"{len(services)} services listed")
    dashboards, unresolved = find_dashboards(client)
    print(f"{sum(len(v) for v in dashboards.values()) + len(unresolved)} public dashboards "
          f"({len(unresolved)} not tied to an org)")
    state = load_json(data_dir / STATE_FILE)
    state["unresolved_dashboards"] = unresolved
    state["discovered"] = today
    known = {p.parent.name for p in data_dir.glob(f"*/{ORG_FILE}")}
    for org_id in sorted(set(services) | known):
        path = data_dir / org_id / ORG_FILE
        org = load_json(path) or new_org(org_id, today)
        is_new = not path.exists()
        listed = org_id in services
        description = None
        if listed:
            try:
                description = client.get_json(service_url(org_id), f="json").get("serviceDescription")
            except (ArcGISError, IncompleteFetch) as e:
                print(f"ERROR {org_id}: service metadata: {e}")
        events = apply_discovery(org, today=today, listed=listed, description=description,
                                 dashboards=dashboards.get(org_id, []))
        name = org.get("agency_name") or org_id
        if is_new:
            org["events"].insert(0, {"date": today, "event": "listed"})
            print(f"NEW SERVICE {name} ({org_id})")
        for e in events:
            print(f"{e['event'].upper().replace('_', ' ')} {name}: "
                  + json.dumps({k: v for k, v in e.items() if k not in ("date", "event")}))
        save_json(path, org)
    save_json(data_dir / STATE_FILE, state)
    return 0


# ── registry link ───────────────────────────────────────────────

_ROLE_PATTERNS = (
    (r"police department|police dept\.?|police services?|police", "police"),
    (r"sheriff'?s? (?:office|department|dept\.?)|sheriff", "sheriff"),
)
_STATE_TAIL = re.compile(r"\s*(?:\(([A-Z]{2})\)|[-,]\s*([A-Z]{2}))\s*$")
_PREFIX_TAG = re.compile(r"^\s*(?:\[[^\]]*\]\s*|[A-Za-z]+ - (?=.*(?:police|sheriff)))", re.I)


def parse_agency_name(name):
    """'South San Francisco Police Department (CA)' → ('South San Francisco', 'police', 'CA', 'place').
    Returns None when no police/sheriff role word is found."""
    if not name:
        return None
    n = _PREFIX_TAG.sub("", name).strip()
    n = re.sub(r"\s*-\s*trial\b.*$", "", n, flags=re.I).strip()
    state = None
    m = _STATE_TAIL.search(n)
    if m:
        state = m.group(1) or m.group(2)
        n = n[: m.start()].strip()
    for pat, role in _ROLE_PATTERNS:
        m = re.search(rf"\s+(?:{pat})\s*$", n, flags=re.I)
        if m:
            place = n[: m.start()].strip()
            kind = "place"
            if re.search(r"\s+county$", place, flags=re.I):
                place, kind = re.sub(r"\s+county$", "", place, flags=re.I).strip(), "county"
            elif role == "sheriff":
                kind = "county"
            return (place, role, state, kind) if place else None
    return None


def _inside(geo, extent):
    xmin, xmax = max(extent["xmin"], -180) - LINK_PAD_DEG, min(extent["xmax"], 180) + LINK_PAD_DEG
    ymin, ymax = max(extent["ymin"], -90) - LINK_PAD_DEG, min(extent["ymax"], 90) + LINK_PAD_DEG
    return xmin <= geo["lng"] <= xmax and ymin <= geo["lat"] <= ymax


def link_agency(name, extent, registry=None):
    """Registry entry for a Skydio org name, or (None, reason). Matches role +
    geo name (+ state when the name carries one), then requires the agency to
    sit inside the flights' extent — that tells Belmont CA from Belmont NC when
    the name has no state. An extent isn't a centre: one Belmont flight is in
    Southern California, so the box spans the state."""
    parsed = parse_agency_name(name)
    if not parsed:
        return None, "unparsed"
    place, role, state, kind = parsed
    registry = load_registry() if registry is None else registry
    cands = []
    for e in registry:
        geo = e.get("geo") or {}
        if e.get("agency_role") != role or (geo.get("name") or "").lower() != place.lower():
            continue
        if geo.get("kind") and geo.get("kind") != kind:
            continue
        if state and geo.get("state") != state:
            continue
        cands.append(e)
    if extent:
        cands = [e for e in cands if (e.get("geo") or {}).get("lat") is not None and _inside(e["geo"], extent)]
    elif not state:
        return None, f"no extent, no state ({len(cands)} candidates)"
    if len(cands) == 1:
        return cands[0], "name+role" + ("+state" if state else "") + ("+extent" if extent else "")
    return None, f"{len(cands)} candidates"


def apply_link(org, extent, today, registry=None):
    """Set agency_id/ori from the registry unless a human set them (match=manual)."""
    if org.get("match") == "manual":
        return None
    entry, basis = link_agency(org.get("agency_name"), extent, registry)
    if entry is None:
        if org.get("agency_id"):
            return None  # keep an earlier unique link rather than flapping on a sparse extent
        org["match"] = None
        return None
    if entry["agency_id"] != org.get("agency_id"):
        org["agency_id"] = entry["agency_id"]
        org["ori"] = list(entry.get("ori") or [])
        org["match"] = basis
        ev = {"date": today, "event": "linked", "agency_id": entry["agency_id"], "basis": basis}
        org.setdefault("events", []).append(ev)
        return ev
    return None


# ── flights store ───────────────────────────────────────────────

def key_lines(lines, attrs=lambda x: x):
    """{key: item} keyed by flight_id; a repeated flight_id (seen only if a
    layer is double-loaded) gets '#2', '#3' by ObjectId order."""
    seen = collections.Counter()
    out = {}
    for item in sorted(lines, key=lambda x: (attrs(x).get("ObjectId") is None, attrs(x).get("ObjectId") or 0)):
        a = attrs(item)
        fid = a.get("flight_id") or f"oid-{a.get('ObjectId')}"
        seen[fid] += 1
        out[fid if seen[fid] == 1 else f"{fid}#{seen[fid]}"] = item
    return out


def material(attrs):
    return {k: v for k, v in attrs.items() if k not in VOLATILE_FIELDS}


def read_flights(org_dir):
    lines = []
    for p in sorted((org_dir / FLIGHTS_DIR).glob("*.ndjson")):
        for raw in p.read_text().splitlines():
            if raw.strip():
                lines.append(json.loads(raw))
    return key_lines(lines, attrs=lambda x: x["attributes"])


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def dump_line(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def write_flights(org_dir, keyed):
    """Rewrite month files whose content changed; drop months left empty."""
    months = collections.defaultdict(list)
    for line in keyed.values():
        months[month_of(line["attributes"])].append(line)
    fdir = org_dir / FLIGHTS_DIR
    wanted = set()
    for month, lines in months.items():
        lines.sort(key=lambda x: (x["attributes"].get("ObjectId") is None, x["attributes"].get("ObjectId") or 0))
        body = "".join(dump_line(x) + "\n" for x in lines).encode()
        path = fdir / f"{month}.ndjson"
        wanted.add(path.name)
        if not path.exists() or path.read_bytes() != body:
            _atomic_write(path, body)
    if fdir.exists():
        for p in fdir.glob("*.ndjson"):
            if p.name not in wanted:
                p.unlink()


def append_changes(org_dir, entries):
    if not entries:
        return
    path = org_dir / CHANGES_FILE
    with path.open("a") as f:
        for e in entries:
            f.write(dump_line(e) + "\n")


# ── geometry ────────────────────────────────────────────────────

def canonical_geometry(geom):
    return json.dumps(geom, sort_keys=True, separators=(",", ":")).encode()


def geometry_meta(geom):
    if not geom or not geom.get("paths"):
        return {"sha256": None, "points": 0, "parts": 0}
    return {
        "sha256": hashlib.sha256(canonical_geometry(geom)).hexdigest(),
        "points": sum(len(p) for p in geom["paths"]),
        "parts": len(geom["paths"]),
    }


def primary_checkout():
    r = subprocess.run(["git", "-C", str(Path(__file__).parent), "rev-parse", "--path-format=absolute",
                        "--git-common-dir"], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"can't find the primary checkout: {r.stderr.strip()}")
    return Path(r.stdout.strip()).parent


def geometry_dir():
    if os.environ.get("DRONE_GEOMETRY_DIR"):
        return Path(os.environ["DRONE_GEOMETRY_DIR"])
    return primary_checkout() / ".claude" / "drone_geometry"


def blob_path(store, sha):
    return store / sha[:2] / f"{sha}.json.gz"


def store_blob(store, geom):
    data = canonical_geometry(geom)
    sha = hashlib.sha256(data).hexdigest()
    path = blob_path(store, sha)
    if not path.exists():
        _atomic_write(path, gzip.compress(data, mtime=0))
    return sha


def read_blob(store, sha):
    path = blob_path(store, sha)
    if not path.exists():
        return None
    return json.loads(gzip.decompress(path.read_bytes()))


def fetch_geometries(client, org_id, object_ids):
    """{ObjectId: (flight_id, geometry)} for the given ObjectIds."""
    out = {}
    ids = list(object_ids)
    for i in range(0, len(ids), GEOM_BATCH):
        batch = ids[i:i + GEOM_BATCH]
        d = client.get_json(f"{layer_url(org_id)}/query", objectIds=",".join(map(str, batch)),
                            outFields="ObjectId,flight_id", returnGeometry="true", outSR=4326, f="json")
        for f in d.get("features", []):
            a = f.get("attributes") or {}
            out[a.get("ObjectId")] = (a.get("flight_id"), f.get("geometry"))
    return out


# ── crawl ───────────────────────────────────────────────────────

def fetch_count(client, org_id):
    """(count, extent) — a returnCountOnly query also carries the data extent."""
    d = client.get_json(f"{layer_url(org_id)}/query", where="1=1", returnCountOnly="true",
                        returnExtentOnly="true", f="json")
    return d.get("count"), d.get("extent")


def fetch_attributes(client, org_id, expected):
    out, offset = [], 0
    while True:
        d = client.get_json(f"{layer_url(org_id)}/query", where="1=1", outFields="*", returnGeometry="false",
                            orderByFields="ObjectId", resultOffset=offset, resultRecordCount=PAGE, f="json")
        feats = d.get("features", [])
        if not feats:
            break
        out += [f["attributes"] for f in feats]
        offset += len(feats)
        if not d.get("exceededTransferLimit") and len(out) >= expected:
            break
    if len(out) != expected:
        raise IncompleteFetch(f"{org_id}: got {len(out)} records, layer count {expected}")
    return out


def crawl_org(client, data_dir, org_id, state, *, now, geometry="hash", store=None,
              geometry_budget=None, force=False, max_age=DEFAULT_MAX_AGE, registry=None):
    """Pull one org; returns a summary dict. Writes nothing on a fetch error."""
    today = now.date().isoformat()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    org_dir = data_dir / org_id
    org = load_json(org_dir / ORG_FILE) or new_org(org_id, today)
    st = state.setdefault("orgs", {}).setdefault(org_id, {})
    name = org.get("agency_name") or org_id
    summary = {"org_id": org_id, "name": name, "status": "unchanged", "baseline": 0, "added": 0,
               "modified": 0, "removed": 0, "geometry_changed": 0, "hashed": 0}

    layer = client.get_json(layer_url(org_id), f="json")
    info = layer.get("editingInfo") or {}
    data_edit, schema_edit = info.get("dataLastEditDate"), info.get("schemaLastEditDate")
    fields = [f["name"] for f in layer.get("fields", [])]

    current = read_flights(org_dir)
    unhashed = [k for k, v in current.items() if v.get("geometry") is None]
    pulled = st.get("pulled")
    fresh = pulled and (now.date() - datetime.fromisoformat(pulled).date()).days < max_age
    want_geom = geometry != "skip" and unhashed
    if not force and fresh and st.get("data_last_edit") == data_edit and fields == org["layer"]["fields"] and not want_geom:
        st["checked"] = today
        return summary

    events, changes = [], []
    if fields != org["layer"].get("fields"):
        if org["layer"].get("fields"):
            old = set(org["layer"]["fields"])
            events.append({"date": today, "event": "schema_changed",
                           "added": sorted(set(fields) - old), "removed": sorted(old - set(fields))})
        org["layer"]["fields"] = fields

    count, extent = fetch_count(client, org_id)
    served = key_lines(fetch_attributes(client, org_id, count))

    added = sorted(served.keys() - current.keys())
    removed = sorted(current.keys() - served.keys())
    keyed = {}
    oid_moves = 0
    for k, attrs in served.items():
        prev = current.get(k)
        if prev is None:
            keyed[k] = {"attributes": attrs, "geometry": None}
            continue
        old = prev["attributes"]
        if old.get("ObjectId") != attrs.get("ObjectId"):
            oid_moves += 1
        diff = {f: [old.get(f), attrs.get(f)] for f in sorted(set(material(old)) | set(material(attrs)))
                if old.get(f) != attrs.get(f)}
        geom = prev.get("geometry")
        if diff:
            changes.append({"seen": stamp, "change": "modified", "key": k,
                            "flight_id": attrs.get("flight_id"), "fields": diff})
            summary["modified"] += 1
            if "Shape__Length" in diff:
                changes[-1]["old_geometry"] = geom  # survives even if this run's budget can't re-hash it
                geom = None  # track edited: re-hash it, compare below
        keyed[k] = {"attributes": attrs, "geometry": geom, "_prev_geometry": prev.get("geometry")}
    for k in removed:
        changes.append({"seen": stamp, "change": "removed", "key": k,
                        "flight_id": current[k]["attributes"].get("flight_id"), "record": current[k]})
    summary["added"], summary["removed"] = len(added), len(removed)
    if oid_moves:
        events.append({"date": today, "event": "objectids_reassigned", "count": oid_moves})

    # Tracks: hash what's new or edited, within the run's budget; the rest stay
    # null for next run. Store mode also keeps those bytes (CI keeps them as a
    # run artifact). Filling the store with older tracks is fetch-geometry's job.
    if geometry != "skip":
        need = [k for k, v in keyed.items() if v["geometry"] is None]
        if geometry_budget is not None:
            need = need[: max(geometry_budget["left"], 0)]
        by_oid = {keyed[k]["attributes"]["ObjectId"]: k for k in need}
        got = fetch_geometries(client, org_id, by_oid) if by_oid else {}
        for oid, (fid, geom) in got.items():
            k = by_oid.get(oid)
            if k is None or fid != keyed[k]["attributes"].get("flight_id"):
                continue  # ObjectIds moved between requests; pick it up next run
            meta = geometry_meta(geom)
            if store is not None and geometry == "store" and meta["sha256"]:
                store_blob(store, geom)
            prev = keyed[k].get("_prev_geometry") or keyed[k]["geometry"]
            if prev and prev.get("sha256") and prev.get("sha256") != meta["sha256"]:
                changes.append({"seen": stamp, "change": "geometry_changed", "key": k,
                                "flight_id": fid, "old": prev, "new": meta})
                summary["geometry_changed"] += 1
            keyed[k]["geometry"] = meta
            summary["hashed"] += 1
        if geometry_budget is not None:
            geometry_budget["left"] -= summary["hashed"]
    for v in keyed.values():
        v.pop("_prev_geometry", None)

    first_pull = not pulled and not current and not (org_dir / CHANGES_FILE).exists()
    if first_pull:
        changes = [c for c in changes if c["change"] != "modified"]
        if served:
            changes.insert(0, {"seen": stamp, "change": "baseline", "count": len(served)})
        summary["baseline"], summary["added"] = len(served), 0
    else:
        changes += [{"seen": stamp, "change": "added", "key": k, "flight_id": served[k].get("flight_id"),
                     "takeoff": served[k].get("takeoff")} for k in added]

    takeoffs = [a.get("takeoff") for a in served.values() if a.get("takeoff") is not None]
    org["flights"] = {
        "count": len(served),
        "first_takeoff": ms_iso(min(takeoffs)) if takeoffs else None,
        "last_takeoff": ms_iso(max(takeoffs)) if takeoffs else None,
        "unhashed": sum(1 for v in keyed.values() if v["geometry"] is None),
        # Extra copies of a flight_id the layer serves (seen: up to 5 copies of
        # one flight). Counts on the public dashboard include them.
        "duplicates": sum(1 for k in served if "#" in k),
    }
    org.setdefault("events", []).extend(events)
    apply_link(org, extent if count else None, today, registry)

    if served or current:
        write_flights(org_dir, keyed)
    append_changes(org_dir, changes)
    save_json(org_dir / ORG_FILE, org)
    st.update({"checked": today, "pulled": today, "data_last_edit": data_edit, "schema_last_edit": schema_edit})
    summary["status"] = "pulled"
    summary["name"] = org.get("agency_name") or org_id
    return summary


def crawl_order(data_dir, state, only=None):
    orgs = []
    for p in sorted(data_dir.glob(f"*/{ORG_FILE}")):
        org = load_json(p)
        if only and org["org_id"] not in only:
            continue
        if not only and not org.get("listed", True):
            continue
        orgs.append(org["org_id"])
    checked = (state.get("orgs") or {})
    return sorted(orgs, key=lambda o: ((checked.get(o) or {}).get("checked") or "", o))


def cmd_crawl(args, client=None, data_dir=DATA_DIR, now=None):
    client = client or Client(delay=args.delay)
    now = now or utc_now()
    store = geometry_dir() if args.geometry == "store" else None
    state = load_json(data_dir / STATE_FILE)
    orgs = crawl_order(data_dir, state, set(args.orgs) if args.orgs else None)
    if args.orgs:
        unknown = set(args.orgs) - set(orgs)
        for o in sorted(unknown):
            if not UUID_RE.match(o):
                print(f"ERROR {o}: not an org uuid")
                continue
            orgs.append(o)  # crawl an org discover hasn't written yet
    if args.limit:
        orgs = orgs[: args.limit]
    budget = {"left": args.max_geometry} if args.max_geometry is not None else None
    totals = collections.Counter()
    errors = 0
    for org_id in orgs:
        try:
            s = crawl_org(client, data_dir, org_id, state, now=now, geometry=args.geometry, store=store,
                          geometry_budget=budget, force=args.force, max_age=args.max_age)
        except RateLimited as e:
            print(f"RATE LIMITED {e}; stopping")
            errors += 1
            break
        except (ArcGISError, IncompleteFetch) as e:
            print(f"ERROR {org_id}: {e}")
            errors += 1
            continue
        totals.update({k: s[k] for k in ("baseline", "added", "modified", "removed", "geometry_changed", "hashed")})
        totals[s["status"]] += 1
        for k in ("baseline", "added", "modified", "removed", "geometry_changed"):
            if s[k]:
                print(f"{k.upper().replace('_', ' ')} {s['name']} ({org_id}): {s[k]}")
        save_json(data_dir / STATE_FILE, state)  # after each org, so a crash keeps progress
    print("crawl: " + ", ".join(f"{k}={v}" for k, v in sorted(totals.items())) + f", errors={errors}")
    return 1 if errors and errors == len(orgs) else 0


# ── fetch-geometry (local store) ────────────────────────────────

def cmd_fetch_geometry(args, client=None, data_dir=DATA_DIR, store=None):
    client = client or Client(delay=args.delay)
    store = store or geometry_dir()
    left = args.limit
    stats = collections.Counter()
    for p in sorted(data_dir.glob(f"*/{ORG_FILE}")):
        org_id = p.parent.name
        if args.orgs and org_id not in args.orgs:
            continue
        lines = read_flights(p.parent)
        need = {v["attributes"]["ObjectId"]: (k, v) for k, v in lines.items()
                if (v.get("geometry") or {}).get("sha256") and not blob_path(store, v["geometry"]["sha256"]).exists()}
        if not need:
            continue
        if left is not None:
            need = dict(list(need.items())[: max(left, 0)])
        try:
            got = fetch_geometries(client, org_id, need)
        except RateLimited as e:
            print(f"RATE LIMITED {e}; stopping")
            stats["errors"] += 1
            break
        except ArcGISError as e:
            print(f"ERROR {org_id}: {e}")
            stats["errors"] += 1
            continue
        for oid, (fid, geom) in got.items():
            k, line = need[oid]
            if fid != line["attributes"].get("flight_id"):
                stats["moved"] += 1
                continue
            sha = store_blob(store, geom) if geom and geom.get("paths") else None
            if sha == line["geometry"]["sha256"]:
                stats["stored"] += 1
            else:
                stats["mismatch"] += 1
                print(f"GEOMETRY MISMATCH {org_id} {k}: recorded {line['geometry']['sha256']} served {sha}")
                with (store / "mismatches.ndjson").open("a") as f:
                    f.write(dump_line({"seen": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"), "org_id": org_id,
                                       "key": k, "recorded": line["geometry"], "served_sha256": sha}) + "\n")
        if left is not None:
            left -= len(need)
            if left <= 0:
                break
    print("fetch-geometry: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) + f" (store {store})")
    return 0


# ── CI artifacts: sync + reminder ───────────────────────────────

def run_gh(args, binary=False):
    r = subprocess.run(["gh", *args], capture_output=True, check=False)
    if r.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])}…: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout if binary else r.stdout.decode()


def list_track_artifacts(gh, repo):
    """drone-tracks-* artifacts, oldest first: {id, name, created_at, expired, size, run_id}."""
    out = gh(["api", "--paginate", f"repos/{repo}/actions/artifacts?per_page=100", "--jq",
              f'.artifacts[] | select(.name | startswith("{ARTIFACT_PREFIX}")) | '
              '{id, name, created_at, expired, size: .size_in_bytes, run_id: .workflow_run.id}'])
    arts = [json.loads(x) for x in out.splitlines() if x.strip()]
    return sorted(arts, key=lambda a: (a["created_at"], a["id"]))


def store_zip(store, data):
    """Unpack an artifact zip into the store. Every entry must be
    <aa>/<sha256>.json.gz whose gunzipped bytes hash to <sha256>; anything else
    is reported, never stored. Returns {stored, present, bad: [(name, why)]}."""
    import io
    import zipfile
    res = {"stored": 0, "present": 0, "bad": []}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            m = BLOB_ENTRY_RE.match(info.filename)
            if not m or m.group(2)[:2] != m.group(1):
                res["bad"].append((info.filename, "not a track blob"))
                continue
            sha = m.group(2)
            raw = z.read(info)
            try:
                ok = hashlib.sha256(gzip.decompress(raw)).hexdigest() == sha
            except OSError:
                ok = False
            if not ok:
                res["bad"].append((info.filename, "hash mismatch"))
                continue
            path = blob_path(store, sha)
            if path.exists():
                res["present"] += 1
            else:
                _atomic_write(path, raw)
                res["stored"] += 1
    return res


def synced_through(artifacts, ledger):
    """created_at of the newest live artifact such that every live artifact up
    to it has been synced; None if the oldest live one hasn't."""
    out = None
    for a in artifacts:
        if a.get("expired"):
            continue
        if str(a["id"]) not in ledger:
            break
        out = a["created_at"]
    return out


def cmd_sync_artifacts(args, gh=run_gh, store=None, now=None):
    store = store or geometry_dir()
    now = now or utc_now()
    ledger_path = store / SYNC_LEDGER
    ledger = load_json(ledger_path)
    arts = list_track_artifacts(gh, args.repo)
    totals = collections.Counter()
    for a in arts:
        key = str(a["id"])
        if key in ledger:
            continue
        if a.get("expired"):
            print(f"LOST {a['name']} (created {a['created_at']}) expired before it was synced")
            totals["lost"] += 1
            continue
        res = store_zip(store, gh(["api", f"repos/{args.repo}/actions/artifacts/{a['id']}/zip"], binary=True))
        for name, why in res["bad"]:
            print(f"BAD {a['name']} {name}: {why}")
        ledger[key] = {"name": a["name"], "run_id": a.get("run_id"), "created_at": a["created_at"],
                       "synced_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "stored": res["stored"],
                       "present": res["present"], "bad": [list(b) for b in res["bad"]]}
        save_json(ledger_path, ledger)  # per artifact, so an interrupted sync resumes
        totals.update(artifacts=1, stored=res["stored"], present=res["present"], bad=len(res["bad"]))
    through = synced_through(arts, ledger)
    print("sync-artifacts: " + ", ".join(f"{k}={v}" for k, v in sorted(totals.items()))
          + f"; synced through {through or '(none)'} (store {store})")
    if through and not args.no_variable:
        try:
            gh(["variable", "set", SYNC_VARIABLE, "--body", through, "--repo", args.repo])
        except RuntimeError as e:
            print(f"WARNING couldn't set {SYNC_VARIABLE} (the CI reminder will lag): {e}")
            return 1
    return 0


def _age_days(created_at, now):
    return (now - datetime.fromisoformat(created_at.replace("Z", "+00:00"))).total_seconds() / 86400


def cmd_artifact_reminder(args, gh=run_gh, now=None):
    """Open (or refresh in place) one issue while any live drone-tracks artifact
    newer than the sync mark is --warn-days old; close it once that clears."""
    now = now or utc_now()
    mark = args.synced_through or ""
    live = [a for a in list_track_artifacts(gh, args.repo) if not a.get("expired")]
    unsynced = [a for a in live if not mark or a["created_at"] > mark]
    oldest = unsynced[0] if unsynced else None
    age = _age_days(oldest["created_at"], now) if oldest else 0
    overdue = oldest is not None and age >= args.warn_days
    issues = json.loads(gh(["issue", "list", "--repo", args.repo, "--state", "open", "--search",
                            f'in:title "{REMINDER_TITLE}"', "--json", "number,title"]) or "[]")
    issue = next((i["number"] for i in issues if i["title"] == REMINDER_TITLE), None)
    print(f"artifact-reminder: {len(live)} live, {len(unsynced)} unsynced"
          + (f", oldest {age:.0f} days ({oldest['created_at']})" if oldest else "")
          + f"; synced through {mark or '(never)'}")
    if overdue:
        size = sum(a.get("size") or 0 for a in unsynced)
        body = (f"{len(unsynced)} CI artifact(s) of drone track blobs haven't been pulled into the local "
                f"store ({size / 1e6:.0f} MB). The oldest, `{oldest['name']}`, is {age:.0f} days old and "
                f"expires after {ARTIFACT_RETENTION_DAYS} days; its tracks are lost if any flight in it "
                "has since been deleted or edited at the source.\n\n"
                "Run locally (needs `gh` auth with permission to set repo variables):\n\n"
                "```\nuv run python scripts/drone_flights.py sync-artifacts\n```\n\n"
                f"That verifies every blob against its sha256 name, stores it, and sets `{SYNC_VARIABLE}`; "
                "the next workflow run closes this issue.\n\n"
                f"Synced through: {mark or '(never)'}. Updated {now.strftime('%Y-%m-%d')} by "
                "refresh-drone-data.yml.")
        if issue:
            gh(["issue", "edit", str(issue), "--repo", args.repo, "--body", body])
            print(f"REMINDER updated #{issue}")
        else:
            gh(["issue", "create", "--repo", args.repo, "--title", REMINDER_TITLE, "--body", body])
            print("REMINDER opened")
    elif issue:
        gh(["issue", "close", str(issue), "--repo", args.repo, "--comment",
            f"Synced through {mark or '(never)'}; no unsynced artifact is {args.warn_days}+ days old."])
        print(f"REMINDER closed #{issue}")
    return 0


# ── report ──────────────────────────────────────────────────────

def load_orgs(data_dir=DATA_DIR):
    return [load_json(p) for p in sorted(data_dir.glob(f"*/{ORG_FILE}"))]


def cmd_report(args, data_dir=DATA_DIR):
    orgs = [o for o in load_orgs(data_dir) if o["flights"]["count"]]
    orgs.sort(key=lambda o: -o["flights"]["count"])
    total = sum(o["flights"]["count"] for o in orgs)
    listed = sum(1 for o in load_orgs(data_dir) if o.get("listed", True))
    head = f"{listed} listed services; {len(orgs)} with flights; {total:,} flights"
    rows = [(o.get("agency_name") or o["org_id"], f"{o['flights']['count']:,}",
             (o["flights"]["first_takeoff"] or "")[:10], (o["flights"]["last_takeoff"] or "")[:10],
             "yes" if o.get("dashboards") else "no", "yes" if o.get("agency_id") else "",
             str(o["flights"].get("unhashed") or "")) for o in orgs[: args.top]]
    cols = ("Agency", "Flights", "First", "Last", "Dashboard", "Linked", "Unhashed")
    if args.markdown:
        print(head + "\n")
        print("| " + " | ".join(cols) + " |")
        print("|" + "---|" * len(cols))
        for r in rows:
            print("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
        if len(orgs) > args.top:
            print(f"\n…and {len(orgs) - args.top} more.")
    else:
        print(head)
        for r in rows:
            print(f"{r[1]:>8}  {r[2]} → {r[3]}  dash={r[4]:<3} {r[0]}")
    return 0


# ── db (ingest into DuckDB) ─────────────────────────────────────

def iter_flight_rows(data_dir=DATA_DIR):
    """Normalized flight rows across all orgs (the ingest view)."""
    for org in load_orgs(data_dir):
        for key, line in read_flights(data_dir / org["org_id"]).items():
            a, g = line["attributes"], line.get("geometry") or {}
            t0, t1 = a.get("takeoff"), a.get("landing")
            yield {
                "org_id": org["org_id"], "agency_name": org.get("agency_name"), "agency_id": org.get("agency_id"),
                "key": key, "dup_rank": int(key.rsplit("#", 1)[1]) if "#" in key else 1,
                "flight_id": a.get("flight_id"), "object_id": a.get("ObjectId"),
                "takeoff": ms_iso_full(t0), "landing": ms_iso_full(t1),
                "duration_s": (t1 - t0) / 1000 if t0 is not None and t1 is not None else None,
                "flight_purpose": a.get("flight_purpose") or None, "external_id": a.get("external_id") or None,
                "description": a.get("description") or None, "shape_length": a.get("Shape__Length"),
                "geometry_sha256": g.get("sha256"), "points": g.get("points"), "parts": g.get("parts"),
            }


def cmd_db(args, data_dir=DATA_DIR):
    import duckdb
    out = Path(args.out) if args.out else primary_checkout() / ".claude" / "drone_db" / "flights.duckdb"
    out.parent.mkdir(parents=True, exist_ok=True)
    store = geometry_dir()
    tmp = Path(tempfile.mkdtemp(prefix="drone_db."))
    try:
        orgs = load_orgs(data_dir)
        with (tmp / "orgs.ndjson").open("w") as f:
            for o in orgs:
                f.write(dump_line({"org_id": o["org_id"], "agency_name": o.get("agency_name"),
                                   "agency_id": o.get("agency_id"), "ori": o.get("ori") or [],
                                   "listed": o.get("listed", True), "first_seen": o.get("first_seen"),
                                   "dashboards": len(o.get("dashboards") or []),
                                   "flights": o["flights"]["count"]}) + "\n")
        shas = set()
        with (tmp / "flights.ndjson").open("w") as f:
            for r in iter_flight_rows(data_dir):
                f.write(dump_line(r) + "\n")
                if r["geometry_sha256"]:
                    shas.add(r["geometry_sha256"])
        with (tmp / "changes.ndjson").open("w") as f:
            for o in orgs:
                p = data_dir / o["org_id"] / CHANGES_FILE
                if p.exists():
                    for raw in p.read_text().splitlines():
                        c = json.loads(raw)
                        f.write(dump_line({"org_id": o["org_id"], "seen": c["seen"], "change": c["change"],
                                           "key": c.get("key"), "flight_id": c.get("flight_id"),
                                           "detail": dump_line({k: v for k, v in c.items()
                                                                if k not in ("seen", "change", "key", "flight_id")})}) + "\n")
        tracks = 0
        with (tmp / "tracks.ndjson").open("w") as f:
            for sha in sorted(shas):
                g = read_blob(store, sha)
                if g is None:
                    continue
                paths = g["paths"]
                f.write(dump_line({"geometry_sha256": sha, "esri_json": dump_line(g),
                                   "start_lon": paths[0][0][0], "start_lat": paths[0][0][1],
                                   "end_lon": paths[-1][-1][0], "end_lat": paths[-1][-1][1]}) + "\n")
                tracks += 1
        if out.exists():
            out.unlink()
        con = duckdb.connect(str(out))
        con.execute(f"""CREATE TABLE orgs AS SELECT * FROM read_json('{tmp / "orgs.ndjson"}', format='newline_delimited',
            columns={{org_id:'VARCHAR', agency_name:'VARCHAR', agency_id:'VARCHAR', ori:'VARCHAR[]', listed:'BOOLEAN',
                     first_seen:'DATE', dashboards:'INTEGER', flights:'INTEGER'}})""")
        con.execute(f"""CREATE TABLE flights AS SELECT * FROM read_json('{tmp / "flights.ndjson"}', format='newline_delimited',
            columns={{org_id:'VARCHAR', agency_name:'VARCHAR', agency_id:'VARCHAR', key:'VARCHAR', dup_rank:'INTEGER',
                     flight_id:'VARCHAR',
                     object_id:'BIGINT', takeoff:'TIMESTAMPTZ', landing:'TIMESTAMPTZ', duration_s:'DOUBLE',
                     flight_purpose:'VARCHAR', external_id:'VARCHAR', description:'VARCHAR', shape_length:'DOUBLE',
                     geometry_sha256:'VARCHAR', points:'INTEGER', parts:'INTEGER'}})""")
        con.execute(f"""CREATE TABLE changes AS SELECT * FROM read_json('{tmp / "changes.ndjson"}', format='newline_delimited',
            columns={{org_id:'VARCHAR', seen:'TIMESTAMPTZ', change:'VARCHAR', key:'VARCHAR', flight_id:'VARCHAR', detail:'JSON'}})""")
        con.execute(f"""CREATE TABLE tracks AS SELECT * FROM read_json('{tmp / "tracks.ndjson"}', format='newline_delimited',
            columns={{geometry_sha256:'VARCHAR', esri_json:'JSON', start_lon:'DOUBLE', start_lat:'DOUBLE',
                     end_lon:'DOUBLE', end_lat:'DOUBLE'}})""")
        # `flights` is every served row; layers repeat some flight_ids verbatim,
        # so count and analyse over flights_unique.
        con.execute("CREATE VIEW flights_unique AS SELECT * FROM flights WHERE dup_rank = 1")
        # Minimum fleet in the air: the most flights airborne at once, per org.
        con.execute("""CREATE VIEW org_max_concurrent AS
            WITH f AS (SELECT * FROM flights_unique WHERE takeoff IS NOT NULL AND landing IS NOT NULL),
                 ev AS (SELECT org_id, takeoff AS t, 1 AS d FROM f UNION ALL SELECT org_id, landing, -1 FROM f),
                 run AS (SELECT org_id, SUM(d) OVER (PARTITION BY org_id ORDER BY t, d ROWS UNBOUNDED PRECEDING) AS airborne FROM ev)
            SELECT org_id, MAX(airborne) AS max_concurrent FROM run GROUP BY org_id""")
        n = con.execute("SELECT COUNT(*) FROM flights").fetchone()[0]
        con.close()
    finally:
        for p in tmp.glob("*"):
            p.unlink()
        tmp.rmdir()
    print(f"db: {len(orgs)} orgs, {n:,} flights, {tracks:,} tracks from the local store → {out}")
    return 0


# ── CLI ─────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("discover", help="list services + dashboards, refresh org.json")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    p = sub.add_parser("crawl", help="pull flights for orgs whose layer changed")
    p.add_argument("orgs", nargs="*", help="org uuids (default: every listed org, oldest-checked first)")
    p.add_argument("--geometry", choices=GEOMETRY_MODES, default="hash",
                   help="hash: record sha256 only (CI); store: also keep the bytes locally; skip: no tracks")
    p.add_argument("--max-geometry", type=int, default=None, help="tracks to fetch this run, across orgs")
    p.add_argument("--limit", type=int, default=None, help="orgs to check this run")
    p.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE,
                   help="re-pull an unchanged layer after this many days")
    p.add_argument("--force", action="store_true")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    p = sub.add_parser("fetch-geometry", help="fill the local track store from git sha256s")
    p.add_argument("orgs", nargs="*")
    p.add_argument("--limit", type=int, default=None, help="tracks to fetch this run")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    p = sub.add_parser("sync-artifacts", help="pull CI track artifacts into the local store")
    p.add_argument("--repo", default=REPO)
    p.add_argument("--no-variable", action="store_true", help=f"don't update the {SYNC_VARIABLE} repo variable")

    p = sub.add_parser("artifact-reminder", help="(CI) open/close the sync-reminder issue")
    p.add_argument("--repo", default=REPO)
    p.add_argument("--synced-through", default="", help=f"the {SYNC_VARIABLE} repo variable")
    p.add_argument("--warn-days", type=int, default=ARTIFACT_WARN_DAYS)

    p = sub.add_parser("report", help="orgs with flights")
    p.add_argument("--markdown", action="store_true")
    p.add_argument("--top", type=int, default=60)

    p = sub.add_parser("db", help="build a local DuckDB (needs duckdb)")
    p.add_argument("--out", default=None)

    args = ap.parse_args(argv)
    return {
        "discover": cmd_discover, "crawl": cmd_crawl, "fetch-geometry": cmd_fetch_geometry,
        "sync-artifacts": cmd_sync_artifacts, "artifact-reminder": cmd_artifact_reminder,
        "report": cmd_report, "db": cmd_db,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
