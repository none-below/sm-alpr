#!/usr/bin/env python3
"""Tests for the drone flight-log archiver (drone_flights.py).

A fake ArcGIS server stands in for Skydio's org; everything lands in tmp_path.
Run with: uv run pytest tests/test_drone_flights.py
"""

import argparse
import gzip
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import drone_flights as dl  # noqa: E402

ORG = "11111111-2222-3333-4444-555555555555"
ORG2 = "66666666-7777-8888-9999-000000000000"
WEBMAP = "a" * 32
DASH = "b" * 32
DASH_ORPHAN = "c" * 32
FIELDS = ["ObjectId", "flight_id", "vehicle_serial", "dock_serial", "user_email", "takeoff", "landing",
          "external_id", "description", "flight_purpose", "operation_id", "organization_id", "Shape__Length"]
T0 = 1789000000000  # 2026-09-09T...Z
NOW = datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc)


# ── fakes ────────────────────────────────────────────────────────

class Resp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


def flight(oid, fid, minute=0, desc="", length=0.01, purpose="Criminal Investigation", month_offset_ms=0):
    t = T0 + month_offset_ms + minute * 60000
    return {
        "attributes": {"ObjectId": oid, "flight_id": fid, "vehicle_serial": None, "dock_serial": None,
                       "user_email": None, "takeoff": t, "landing": t + 600000, "external_id": "",
                       "description": desc, "flight_purpose": purpose, "operation_id": None,
                       "organization_id": ORG, "Shape__Length": length},
        "geometry": {"paths": [[[-122.3 + oid / 1000, 37.5], [-122.31, 37.51 + oid / 1000]]]},
    }


class FakeArcGIS:
    """Routes GETs by URL; unknown URLs fail the test."""

    def __init__(self):
        self.orgs = {ORG: {"description": "Feature service for Testville Police Department (CA)",
                           "features": [flight(1, "f-1"), flight(2, "f-2", 30)], "edit": 100}}
        self.dashboards = [{"id": DASH, "title": "Testville PD Drone Flights", "access": "public",
                            "created": T0, "modified": T0}]
        self.calls = []
        self.short_page = False  # drop a record from paged results

    def get(self, url, params=None, timeout=None):
        p = params or {}
        self.calls.append((url, dict(p)))
        if url == dl.SERVICES_URL:
            return Resp({"services": [{"name": f"{o}-production", "type": "FeatureServer"} for o in self.orgs]
                         + [{"name": "Airports", "type": "FeatureServer"}]})
        if url == dl.SEARCH_URL:
            if '"Web Map"' in p["q"]:
                res = [{"id": WEBMAP, "title": f"{ORG}-production-web-map"}, {"id": "d" * 32, "title": "basemap"}]
            else:
                res = self.dashboards
            return Resp({"results": res, "nextStart": -1})
        if url.startswith(dl.ITEMS_URL):
            item = url.split("/")[-2]
            return Resp(f'{{"widgets":[{{"itemId":"{WEBMAP}"}}]}}' if item == DASH else "")
        for org_id, o in self.orgs.items():
            if url == dl.service_url(org_id):
                return Resp({"serviceDescription": o["description"]})
            if url == dl.layer_url(org_id):
                return Resp({"fields": [{"name": f} for f in FIELDS],
                             "editingInfo": {"dataLastEditDate": o["edit"], "schemaLastEditDate": 1}})
            if url == dl.layer_url(org_id) + "/query":
                feats = sorted(o["features"], key=lambda f: f["attributes"]["ObjectId"])
                if p.get("returnCountOnly"):
                    return Resp({"count": len(feats),
                                 "extent": {"xmin": -122.32, "ymin": 37.5, "xmax": -122.30, "ymax": 37.52}})
                if "objectIds" in p:
                    want = {int(x) for x in p["objectIds"].split(",")}
                    return Resp({"features": [{"attributes": {"ObjectId": f["attributes"]["ObjectId"],
                                                              "flight_id": f["attributes"]["flight_id"]},
                                               "geometry": f["geometry"]}
                                              for f in feats if f["attributes"]["ObjectId"] in want]})
                if self.short_page:
                    feats = feats[:-1]
                off, n = int(p["resultOffset"]), int(p["resultRecordCount"])
                page = feats[off:off + n]
                return Resp({"features": [{"attributes": f["attributes"]} for f in page],
                             "exceededTransferLimit": off + n < len(feats)})
        raise AssertionError(f"unexpected GET {url} {p}")

    def queries(self, kind):
        return [c for c in self.calls if c[0].endswith("/query") and kind(c[1])]


REGISTRY = [
    {"agency_id": "ag-testville", "agency_role": "police", "ori": ["CA0000001"],
     "geo": {"kind": "place", "name": "Testville", "state": "CA", "lat": 37.51, "lng": -122.31}},
    {"agency_id": "ag-belmont-ca", "agency_role": "police", "ori": ["CA0410200"],
     "geo": {"kind": "place", "name": "Belmont", "state": "CA", "lat": 37.52, "lng": -122.28}},
    {"agency_id": "ag-belmont-nc", "agency_role": "police", "ori": ["NC0360100"],
     "geo": {"kind": "place", "name": "Belmont", "state": "NC", "lat": 35.24, "lng": -81.04}},
    {"agency_id": "ag-orange-so", "agency_role": "sheriff", "ori": [],
     "geo": {"kind": "county", "name": "Orange", "state": "CA", "lat": 33.7, "lng": -117.8}},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakeArcGIS()
    client = dl.Client(session=fake, delay=0, backoff=0)
    store = tmp_path / "store"
    monkeypatch.setenv("DRONE_GEOMETRY_DIR", str(store))
    monkeypatch.setattr(dl, "load_registry", lambda: REGISTRY)
    monkeypatch.setattr(dl, "PAGE", 1)  # exercise paging with two flights
    return fake, client, tmp_path / "data", store


def discover(client, data):
    return dl.cmd_discover(argparse.Namespace(delay=0), client=client, data_dir=data, today="2026-10-18")


def crawl(client, data, *orgs, geometry="hash", max_geometry=None, force=False, now=NOW):
    ns = argparse.Namespace(orgs=list(orgs), geometry=geometry, max_geometry=max_geometry, limit=None,
                            max_age=7, force=force, delay=0)
    return dl.cmd_crawl(ns, client=client, data_dir=data, now=now)


def ledger(data, org=ORG):
    p = data / org / dl.CHANGES_FILE
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def lines(data, org=ORG):
    return dl.read_flights(data / org)


# ── parsing / linking ────────────────────────────────────────────

@pytest.mark.parametrize("name,want", [
    ("San Mateo Police Department", ("San Mateo", "police", None, "place")),
    ("South San Francisco Police Department (CA)", ("South San Francisco", "police", "CA", "place")),
    ("LOS Altos Police Department (CA)", ("LOS Altos", "police", "CA", "place")),
    ("El Centro Police Department - CA", ("El Centro", "police", "CA", "place")),
    ("Glendale Police Department (CA) - Trial", ("Glendale", "police", "CA", "place")),
    ("Orange County Sheriff's Department (CA)", ("Orange", "sheriff", "CA", "county")),
    ("[Paraverse] Modesto Police Department (CA)", ("Modesto", "police", "CA", "place")),
    ("Paraverse - Menifee Police Department (CA)", ("Menifee", "police", "CA", "place")),
    ("University of California - San Diego (CA)", None),
    ("Sunbelt Rentals", None),
])
def test_parse_agency_name(name, want):
    assert dl.parse_agency_name(name) == want


def test_link_uses_extent_to_pick_between_same_named_agencies():
    sm_extent = {"xmin": -122.30, "ymin": 37.50, "xmax": -122.26, "ymax": 37.54}
    entry, basis = dl.link_agency("Belmont Police Department", sm_extent, REGISTRY)
    assert entry["agency_id"] == "ag-belmont-ca" and basis == "name+role+extent"
    entry, basis = dl.link_agency("Belmont Police Department", None, REGISTRY)
    assert entry is None and "no extent" in basis
    entry, _ = dl.link_agency("Orange County Sheriff's Department (CA)", None, REGISTRY)
    assert entry["agency_id"] == "ag-orange-so"
    # one stray flight 600 km away still leaves the home agency inside the box
    statewide = {"xmin": -122.50, "ymin": 33.70, "xmax": -117.93, "ymax": 37.69}
    assert dl.link_agency("Belmont Police Department", statewide, REGISTRY)[0]["agency_id"] == "ag-belmont-ca"
    # garbage GPS points (lon -205, lat 688) are clamped, not trusted
    garbage = {"xmin": -205, "ymin": -188, "xmax": 364, "ymax": 688}
    assert dl.link_agency("Testville Police Department (CA)", garbage, REGISTRY)[0]["agency_id"] == "ag-testville"
    # flights nowhere near the only same-named agency: no link
    far = {"xmin": -80.0, "ymin": 35.0, "xmax": -79.9, "ymax": 35.1}
    assert dl.link_agency("Testville Police Department", far, REGISTRY)[0] is None


def test_manual_link_is_never_overwritten():
    org = {"agency_name": "Belmont Police Department", "agency_id": "x", "ori": [], "match": "manual", "events": []}
    dl.apply_link(org, {"xmin": -122.3, "ymin": 37.5, "xmax": -122.26, "ymax": 37.54}, "2026-10-18", REGISTRY)
    assert org["agency_id"] == "x"


# ── discover ─────────────────────────────────────────────────────

def test_discover_writes_org_with_dashboard_and_tracks_changes(env, capsys):
    fake, client, data, _ = env
    discover(client, data)
    org = json.loads((data / ORG / dl.ORG_FILE).read_text())
    assert org["agency_name"] == "Testville Police Department (CA)"
    assert [d["id"] for d in org["dashboards"]] == [DASH]
    assert org["dashboards"][0]["url"] == dl.DASHBOARD_APP_URL + DASH
    assert [e["event"] for e in org["events"]] == ["listed", "dashboard_published"]
    assert "NEW SERVICE" in capsys.readouterr().out

    # orphan dashboard → state; the dashboard comes down; a second org appears
    fake.dashboards = [{"id": DASH_ORPHAN, "title": "Orphan", "access": "public", "created": T0}]
    fake.orgs[ORG2] = {"description": "Feature service for Nowhere PD", "features": [], "edit": 1}
    discover(client, data)
    org = json.loads((data / ORG / dl.ORG_FILE).read_text())
    assert org["dashboards"] == [] and org["events"][-1]["event"] == "dashboard_removed"
    state = json.loads((data / dl.STATE_FILE).read_text())
    assert [d["id"] for d in state["unresolved_dashboards"]] == [DASH_ORPHAN]
    assert (data / ORG2 / dl.ORG_FILE).exists()

    # the service leaves the directory
    del fake.orgs[ORG]
    discover(client, data)
    org = json.loads((data / ORG / dl.ORG_FILE).read_text())
    assert org["listed"] is False and org["events"][-1]["event"] == "unlisted"


# ── crawl ────────────────────────────────────────────────────────

def test_first_crawl_writes_months_baseline_hashes_and_link(env):
    fake, client, data, _ = env
    discover(client, data)
    assert crawl(client, data) == 0
    got = lines(data)
    assert set(got) == {"f-1", "f-2"}
    for k, line in got.items():
        geom = next(f["geometry"] for f in fake.orgs[ORG]["features"] if f["attributes"]["flight_id"] == k)
        assert line["geometry"]["sha256"] == hashlib.sha256(dl.canonical_geometry(geom)).hexdigest()
        assert line["geometry"]["points"] == 2 and line["geometry"]["parts"] == 1
    assert [p.name for p in (data / ORG / "flights").iterdir()] == ["2026-09.ndjson"]
    assert [c["change"] for c in ledger(data)] == ["baseline"] and ledger(data)[0]["count"] == 2
    org = json.loads((data / ORG / dl.ORG_FILE).read_text())
    assert org["flights"]["count"] == 2 and org["flights"]["unhashed"] == 0
    assert org["agency_id"] == "ag-testville" and org["match"] == "name+role+state+extent"


def test_unchanged_layer_is_not_re_pulled(env):
    fake, client, data, _ = env
    discover(client, data)
    crawl(client, data)
    before = len(fake.calls)
    crawl(client, data)
    assert [c[0] for c in fake.calls[before:]] == [dl.layer_url(ORG)]  # metadata only


def test_changes_are_recorded_added_modified_removed(env):
    fake, client, data, _ = env
    discover(client, data)
    crawl(client, data)
    feats = fake.orgs[ORG]["features"]
    feats[0]["attributes"]["description"] = "edited later"
    feats[0]["attributes"]["external_id"] = "2610170057"
    removed = feats.pop(1)
    feats.append(flight(3, "f-3", 90))
    fake.orgs[ORG]["edit"] = 200
    crawl(client, data)
    by = {c["change"]: c for c in ledger(data)}
    assert by["modified"]["fields"] == {"description": ["", "edited later"], "external_id": ["", "2610170057"]}
    assert by["removed"]["flight_id"] == "f-2"
    assert by["removed"]["record"]["attributes"] == removed["attributes"]  # full prior line kept
    assert by["added"]["flight_id"] == "f-3"
    assert set(lines(data)) == {"f-1", "f-3"}
    assert lines(data)["f-3"]["geometry"]["sha256"]


def test_removing_every_flight_drops_month_files(env):
    fake, client, data, _ = env
    discover(client, data)
    crawl(client, data)
    fake.orgs[ORG]["features"] = []
    fake.orgs[ORG]["edit"] = 300
    crawl(client, data)
    assert list((data / ORG / "flights").glob("*.ndjson")) == []
    assert sum(1 for c in ledger(data) if c["change"] == "removed") == 2


def test_incomplete_fetch_writes_nothing(env, capsys):
    fake, client, data, _ = env
    discover(client, data)
    fake.short_page = True
    assert crawl(client, data) == 1  # the only org errored
    assert "ERROR" in capsys.readouterr().out
    assert not (data / ORG / "flights").exists() and not ledger(data)
    state = json.loads((data / dl.STATE_FILE).read_text())
    assert "pulled" not in state.get("orgs", {}).get(ORG, {})


def test_objectid_reassignment_is_not_a_modification(env):
    fake, client, data, _ = env
    discover(client, data)
    crawl(client, data)
    for f in fake.orgs[ORG]["features"]:
        f["attributes"]["ObjectId"] += 100
    fake.orgs[ORG]["edit"] = 400
    crawl(client, data)
    assert [c["change"] for c in ledger(data)] == ["baseline"]
    org = json.loads((data / ORG / dl.ORG_FILE).read_text())
    assert org["events"][-1] == {"date": "2026-10-18", "event": "objectids_reassigned", "count": 2}
    assert lines(data)["f-1"]["attributes"]["ObjectId"] == 101


def test_duplicate_flight_ids_are_kept_as_separate_lines(env):
    fake, client, data, _ = env
    fake.orgs[ORG]["features"].append(flight(3, "f-1", 45))
    discover(client, data)
    crawl(client, data)
    assert set(lines(data)) == {"f-1", "f-1#2", "f-2"}
    org = json.loads((data / ORG / dl.ORG_FILE).read_text())
    assert org["flights"]["count"] == 3 and org["flights"]["duplicates"] == 1
    assert sorted(r["dup_rank"] for r in dl.iter_flight_rows(data)) == [1, 1, 2]


def test_track_edit_records_geometry_changed_with_old_and_new(env):
    fake, client, data, _ = env
    discover(client, data)
    crawl(client, data)
    old = lines(data)["f-1"]["geometry"]
    f = fake.orgs[ORG]["features"][0]
    f["geometry"] = {"paths": [[[-122.0, 37.0], [-122.1, 37.1], [-122.2, 37.2]]]}
    f["attributes"]["Shape__Length"] = 0.5
    fake.orgs[ORG]["edit"] = 500
    crawl(client, data)
    by = {c["change"]: c for c in ledger(data)}
    assert by["modified"]["old_geometry"] == old
    assert by["geometry_changed"]["old"] == old and by["geometry_changed"]["new"]["points"] == 3


def test_geometry_budget_leaves_rest_unhashed_until_next_run(env):
    fake, client, data, _ = env
    discover(client, data)
    crawl(client, data, max_geometry=1)
    assert sorted(v["geometry"] is None for v in lines(data).values()) == [False, True]
    org = json.loads((data / ORG / dl.ORG_FILE).read_text())
    assert org["flights"]["unhashed"] == 1
    crawl(client, data)  # layer unchanged, but an unhashed track forces a pass
    assert all(v["geometry"] for v in lines(data).values())


def test_store_mode_writes_canonical_gzip_blobs(env):
    fake, client, data, store = env
    discover(client, data)
    crawl(client, data, geometry="store")
    for line in lines(data).values():
        sha = line["geometry"]["sha256"]
        raw = gzip.decompress(dl.blob_path(store, sha).read_bytes())
        assert hashlib.sha256(raw).hexdigest() == sha


def test_fetch_geometry_fills_store_and_flags_mismatch(env, capsys):
    fake, client, data, store = env
    discover(client, data)
    crawl(client, data)  # hash only: nothing in the store yet
    assert not store.exists()
    fake.orgs[ORG]["features"][1]["geometry"] = {"paths": [[[0.0, 0.0], [1.0, 1.0]]]}  # silently re-drawn
    dl.cmd_fetch_geometry(argparse.Namespace(orgs=[], limit=None, delay=0), client=client, data_dir=data, store=store)
    got = lines(data)
    assert dl.blob_path(store, got["f-1"]["geometry"]["sha256"]).exists()
    assert not dl.blob_path(store, got["f-2"]["geometry"]["sha256"]).exists()
    assert "GEOMETRY MISMATCH" in capsys.readouterr().out
    mm = [json.loads(x) for x in (store / "mismatches.ndjson").read_text().splitlines()]
    assert mm[0]["key"] == "f-2"
    # git-tracked files untouched
    assert lines(data) == got


def test_canonical_geometry_ignores_key_order():
    a = {"paths": [[[1.5, 2.25]]], "spatialReference": {"wkid": 4326}}
    b = {"spatialReference": {"wkid": 4326}, "paths": [[[1.5, 2.25]]]}
    assert dl.geometry_meta(a) == dl.geometry_meta(b)
    assert dl.geometry_meta(None) == {"sha256": None, "points": 0, "parts": 0}


def test_report_markdown(env, capsys):
    fake, client, data, _ = env
    discover(client, data)
    crawl(client, data)
    capsys.readouterr()
    dl.cmd_report(argparse.Namespace(markdown=True, top=10), data_dir=data)
    out = capsys.readouterr().out
    assert "1 with flights; 2 flights" in out and "| Testville Police Department (CA) | 2 |" in out


def test_flight_rows_normalize_times_and_blanks(env):
    fake, client, data, _ = env
    discover(client, data)
    crawl(client, data)
    rows = list(dl.iter_flight_rows(data))
    r = next(x for x in rows if x["flight_id"] == "f-1")
    assert r["duration_s"] == 600 and r["external_id"] is None and r["description"] is None
    assert r["takeoff"].endswith("Z") and "." in r["takeoff"]
    assert r["agency_id"] == "ag-testville"


def test_store_mode_never_refetches_already_hashed_tracks(env):
    # CI runs store mode on an empty temp store every day; only tracks hashed
    # in that run may be fetched, or every run would re-download everything.
    fake, client, data, store = env
    discover(client, data)
    crawl(client, data)  # hash only
    before = len(fake.queries(lambda p: "objectIds" in p))
    crawl(client, data, geometry="store", force=True)
    assert len(fake.queries(lambda p: "objectIds" in p)) == before
    assert not store.exists()


# ── CI artifacts ─────────────────────────────────────────────────

def blob(geom):
    data = dl.canonical_geometry(geom)
    sha = hashlib.sha256(data).hexdigest()
    return f"{sha[:2]}/{sha}.json.gz", gzip.compress(data, mtime=0), sha


def artifact_zip(entries):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


class FakeGH:
    def __init__(self, artifacts, zips=None, issues=None, fail_variable=False):
        self.artifacts, self.zips, self.issues = artifacts, zips or {}, issues or []
        self.fail_variable = fail_variable
        self.calls = []

    def __call__(self, args, binary=False):
        self.calls.append(args)
        if args[:2] == ["api", "--paginate"]:
            return "".join(json.dumps(a) + "\n" for a in self.artifacts)
        if args[0] == "api" and args[1].endswith("/zip"):
            return self.zips[int(args[1].split("/")[-2])]
        if args[:2] == ["variable", "set"]:
            if self.fail_variable:
                raise RuntimeError("HTTP 403")
            return ""
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.issues)
        if args[0] == "issue":
            return ""
        raise AssertionError(args)

    def did(self, *prefix):
        return [c for c in self.calls if c[: len(prefix)] == list(prefix)]


def art(i, created, expired=False):
    return {"id": i, "name": f"drone-tracks-{i}-1", "created_at": created, "expired": expired,
            "size": 1000, "run_id": 900 + i}


def test_store_zip_verifies_every_entry(tmp_path):
    good_name, good, sha = blob({"paths": [[[1.0, 2.0], [3.0, 4.0]]]})
    other_name, _, _ = blob({"paths": [[[9.0, 9.0], [8.0, 8.0]]]})
    data = artifact_zip([(good_name, good), (other_name, good),  # right bytes under the wrong name
                         ("mismatches.ndjson", b"{}"), ("zz/" + sha + ".json.gz", good)])
    res = dl.store_zip(tmp_path, data)
    assert res["stored"] == 1 and dl.blob_path(tmp_path, sha).exists()
    assert sorted(why for _, why in res["bad"]) == ["hash mismatch", "not a track blob", "not a track blob"]
    assert dl.store_zip(tmp_path, artifact_zip([(good_name, good)]))["present"] == 1


def test_synced_through_stops_at_the_first_gap():
    arts = [art(1, "2026-10-01T00:00:00Z", expired=True), art(2, "2026-10-02T00:00:00Z"),
            art(3, "2026-10-03T00:00:00Z"), art(4, "2026-10-04T00:00:00Z")]
    assert dl.synced_through(arts, {"2": {}, "4": {}}) == "2026-10-02T00:00:00Z"
    assert dl.synced_through(arts, {"3": {}}) is None
    assert dl.synced_through(arts, {"2": {}, "3": {}, "4": {}}) == "2026-10-04T00:00:00Z"


def test_sync_artifacts_stores_records_and_sets_variable(tmp_path, capsys):
    n1, b1, s1 = blob({"paths": [[[1.0, 2.0], [3.0, 4.0]]]})
    n2, b2, s2 = blob({"paths": [[[5.0, 6.0], [7.0, 8.0]]]})
    gh = FakeGH([art(1, "2026-08-01T00:00:00Z", expired=True), art(2, "2026-10-01T00:00:00Z"),
                 art(3, "2026-10-02T00:00:00Z")],
                zips={2: artifact_zip([(n1, b1)]), 3: artifact_zip([(n2, b2)])})
    ns = argparse.Namespace(repo="o/r", no_variable=False)
    assert dl.cmd_sync_artifacts(ns, gh=gh, store=tmp_path) == 0
    assert dl.blob_path(tmp_path, s1).exists() and dl.blob_path(tmp_path, s2).exists()
    assert "LOST drone-tracks-1-1" in capsys.readouterr().out
    ledger = json.loads((tmp_path / dl.SYNC_LEDGER).read_text())
    assert set(ledger) == {"2", "3"} and ledger["3"]["stored"] == 1
    assert gh.did("variable", "set") == [["variable", "set", dl.SYNC_VARIABLE, "--body", "2026-10-02T00:00:00Z",
                                          "--repo", "o/r"]]
    # a second sync downloads nothing
    gh.calls.clear()
    dl.cmd_sync_artifacts(ns, gh=gh, store=tmp_path)
    assert not [c for c in gh.calls if c[0] == "api" and c[1].endswith("/zip")]


def test_sync_artifacts_reports_a_variable_it_cannot_set(tmp_path, capsys):
    n1, b1, _ = blob({"paths": [[[1.0, 2.0], [3.0, 4.0]]]})
    gh = FakeGH([art(2, "2026-10-01T00:00:00Z")], zips={2: artifact_zip([(n1, b1)])}, fail_variable=True)
    assert dl.cmd_sync_artifacts(argparse.Namespace(repo="o/r", no_variable=False), gh=gh, store=tmp_path) == 1
    assert "WARNING" in capsys.readouterr().out
    assert (tmp_path / dl.SYNC_LEDGER).exists()  # the sync itself still counts


REMINDER_NOW = datetime(2026, 12, 15, tzinfo=timezone.utc)


def reminder(gh, synced=""):
    ns = argparse.Namespace(repo="o/r", synced_through=synced, warn_days=60)
    return dl.cmd_artifact_reminder(ns, gh=gh, now=REMINDER_NOW)


def test_reminder_opens_one_issue_when_an_unsynced_artifact_is_60_days_old():
    gh = FakeGH([art(1, "2026-10-10T00:00:00Z"), art(2, "2026-12-01T00:00:00Z")])
    reminder(gh)
    (create,) = gh.did("issue", "create")
    assert dl.REMINDER_TITLE in create and "drone-tracks-1-1" in create[create.index("--body") + 1]


def test_reminder_edits_the_open_issue_instead_of_commenting():
    gh = FakeGH([art(1, "2026-10-10T00:00:00Z")], issues=[{"number": 7, "title": dl.REMINDER_TITLE}])
    reminder(gh)
    assert gh.did("issue", "edit", "7") and not gh.did("issue", "create") and not gh.did("issue", "comment")


def test_reminder_closes_once_synced_and_stays_quiet_otherwise():
    arts = [art(1, "2026-10-10T00:00:00Z"), art(2, "2026-12-01T00:00:00Z")]
    gh = FakeGH(arts, issues=[{"number": 7, "title": dl.REMINDER_TITLE}])
    reminder(gh, synced="2026-10-10T00:00:00Z")  # only the 14-day-old one is unsynced
    assert gh.did("issue", "close", "7")
    gh = FakeGH(arts)
    reminder(gh, synced="2026-10-10T00:00:00Z")
    assert not [c for c in gh.calls if c[0] == "issue" and c[1] != "list"]
