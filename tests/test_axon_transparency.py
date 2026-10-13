#!/usr/bin/env python3
"""Tests for the Axon ALPR transparency archiver (axon_transparency.py).

A fake HTTP session stands in for evidence.com; captures land in tmp_path.
Run with: uv run pytest tests/test_axon_transparency.py
"""

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import axon_transparency as ax  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────

class Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code = status
        self.content = body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


class FakeSession:
    """Routes GETs by exact URL; unknown URLs are a test failure."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def get(self, url, allow_redirects=True, timeout=None):
        self.calls.append(url)
        r = self.routes[url]
        return r() if callable(r) else r


BLOB_404 = Resp(404, b'\xef\xbb\xbf<?xml version="1.0"?><Error><Code>BlobNotFound</Code></Error>')


def doc(**over):
    d = {
        "agencyInfo": {"id": "a-1", "name": "Test PD"},
        "transparencySetting": {"general_enable": True},
        "policy": {"readRetentionSetting": 30, "hitRetentionSetting": 365,
                   "offenseCategories": [{"id": 59, "name": "Stolen Vehicle"}]},
        "metric": {"readsRetained": 1000, "hitsRetained": 10, "vehicles": 5},
        "updatedAt": 1791288321,
        "deviceBreakdown": {"fleet": 5, "outpost": 0, "lightpost": 0},
        "categoryBreakdown": {"59": 3},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k] = {**d[k], **v}
        else:
            d[k] = v
    return d


def published(d, md5=True):
    body = json.dumps(d, separators=(",", ":")).encode()
    h = {"Last-Modified": "Tue, 06 Oct 2026 12:05:21 GMT", "ETag": "0x1"}
    if md5:
        h["Content-MD5"] = base64.b64encode(hashlib.md5(body).digest()).decode()
    return Resp(200, body, h)


def tenant_redirect(host):
    return Resp(303, headers={"Location": f"https://id.evidence.com/oauth/authorize?state=x&domain={host}&client_id=y"})


NO_TENANT = Resp(303, headers={"Location": "https://id.evidence.com/login?return_url=%2Faxon%2Fdashboard"})

H = "testpdca.evidence.com"


# ── hosts ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,host", [
    ("springfieldpdca", "springfieldpdca.evidence.com"),
    ("SpringfieldPDCA.evidence.com", "springfieldpdca.evidence.com"),
    ("https://lewisvillepdtx.evidence.com/alpr/public/policy", "lewisvillepdtx.evidence.com"),
    ("hps.ca", "hps.ca.evidence.com"),
])
def test_normalize_host(text, host):
    assert ax.normalize_host(text) == host


@pytest.mark.parametrize("bad", ["", "evidence.com", "foo bar", "https:///x"])
def test_normalize_host_rejects(bad):
    with pytest.raises(ValueError):
        ax.normalize_host(bad)


def test_file_url_uses_first_label_for_regional_hosts():
    assert ax.file_url("hps.ca.evidence.com") == \
        "https://hps.ca.evidence.com/alpr/public/v1/transparency/file/hps.json"
    assert ax.host_key("hps.ca.evidence.com") == "hps.ca"


def test_classify_login_redirect():
    loc_real = "https://id.evidence.com/oauth/authorize?state=x&domain=springfieldpdca.evidence.com&redirect_uri=z"
    assert ax.classify_login_redirect(303, loc_real, "springfieldpdca.evidence.com") == "tenant"
    # Another tenant's domain in the redirect is not a match for this host.
    assert ax.classify_login_redirect(303, loc_real, "shelbyvillepdca.evidence.com") == "unknown"
    assert ax.classify_login_redirect(
        303, "https://id.evidence.com/login?return_url=%2Faxon%2Fdashboard", "x.evidence.com") == "no_tenant"
    assert ax.classify_login_redirect(200, "", "x.evidence.com") == "unknown"
    assert ax.classify_login_redirect(503, loc_real, "springfieldpdca.evidence.com") == "unknown"


# ── fetch ────────────────────────────────────────────────────────

def test_fetch_published_records_provenance():
    s = FakeSession({ax.file_url(H): published(doc())})
    r = ax.fetch_transparency(s, H)
    assert r["status"] == "published"
    assert r["doc"]["agencyInfo"]["name"] == "Test PD"
    m = r["meta"]
    assert m["sha256"] == hashlib.sha256(r["body"]).hexdigest()
    assert m["content_md5_ok"] is True
    assert m["updated_at"] == "2026-10-06T12:05:21Z"
    assert m["fetched_at"].endswith("Z")


def test_fetch_md5_mismatch_is_error():
    bad = published(doc())
    bad.headers["content-md5"] = base64.b64encode(b"0" * 16).decode()
    r = ax.fetch_transparency(FakeSession({ax.file_url(H): bad}), H)
    assert r["status"] == "error" and "MD5" in r["error"]


def test_fetch_without_md5_header_still_published():
    r = ax.fetch_transparency(FakeSession({ax.file_url(H): published(doc(), md5=False)}), H)
    assert r["status"] == "published" and r["meta"]["content_md5_ok"] is None


@pytest.mark.parametrize("resp,status", [
    (BLOB_404, "not_published"),
    (Resp(404, b"<html>nginx 404</html>"), "error"),
    (Resp(403, b"denied"), "error"),
    (Resp(200, b"<html>login</html>"), "error"),
    (Resp(200, b'{"no":"agencyInfo"}'), "error"),
])
def test_fetch_classification(resp, status):
    assert ax.fetch_transparency(FakeSession({ax.file_url(H): resp}), H)["status"] == status


def test_fetch_429_raises():
    with pytest.raises(ax.RateLimited):
        ax.fetch_transparency(FakeSession({ax.file_url(H): Resp(429)}), H)


# ── capture policy ───────────────────────────────────────────────

def test_counters_only_change_is_not_material():
    a = doc()
    b = doc(metric={"readsRetained": 999999, "hitsRetained": 77}, updatedAt=1, categoryBreakdown={"59": 9})
    assert ax.material_view(a) == ax.material_view(b)
    assert ax.should_capture("2026-10-08", a, b, "2026-10-10", 7) is None
    assert ax.should_capture("2026-10-03", a, b, "2026-10-10", 7) == "stale"


@pytest.mark.parametrize("over", [
    {"policy": {"readRetentionSetting": 365}},
    {"deviceBreakdown": {"outpost": 12}},
    {"metric": {"vehicles": 6}},
    {"sharedAgencies": [{"agencyId": "b", "agencyName": "Other PD", "state": "CA", "sharedSince": "2026-01-01"}]},
    {"policyStatements": {"data_access": "new text"}},
])
def test_material_change_captures_immediately(over):
    assert ax.should_capture("2026-10-09", doc(), doc(**over), "2026-10-10", 7) == "changed"


def test_first_capture():
    assert ax.should_capture(None, None, doc(), "2026-10-10", 7) == "first"


def test_material_changes_lines():
    prev = doc(sharedAgencies=[{"agencyId": "1", "agencyName": "Old PD", "state": "CA"}])
    cur = doc(sharedAgencies=[{"agencyId": "2", "agencyName": "New SO", "state": " TX "}],
              deviceBreakdown={"outpost": 12})
    lines = ax.material_changes(prev, cur)
    assert "sharing added: New SO, TX" in lines
    assert "sharing removed: Old PD, CA" in lines
    assert "deviceBreakdown.outpost: 0 → 12" in lines
    assert ax.material_changes(None, cur) == []


# ── crawl (end to end on disk) ───────────────────────────────────

def _register(tmp_path, host=H):
    d, new = ax.register(tmp_path, host, "2026-10-01")
    assert new
    return d


def test_crawl_writes_raw_bytes_meta_and_transition(tmp_path):
    d = _register(tmp_path)
    resp = published(doc())
    ax.run_crawl(FakeSession({ax.file_url(H): resp}), tmp_path, delay=0, today="2026-10-10")

    assert (d / "2026-10-10.json").read_bytes() == resp.content  # byte-for-byte
    meta = json.loads((d / "2026-10-10.meta.json").read_text())
    assert meta["sha256"] == hashlib.sha256(resp.content).hexdigest()
    rec = ax.load_tenant(d)
    assert rec["published"] is True
    assert rec["axon_agency"] == {"id": "a-1", "name": "Test PD"}
    assert [e["event"] for e in rec["events"]] == ["tenant_found", "published"]
    assert ax.load_state(tmp_path)["tenants"][H] == "2026-10-10"


def test_crawl_skips_counter_only_change_but_marks_checked(tmp_path):
    d = _register(tmp_path)
    ax.run_crawl(FakeSession({ax.file_url(H): published(doc())}), tmp_path, delay=0, today="2026-10-10")
    tenant_before = (d / ax.TENANT_FILE).read_text()
    ax.run_crawl(FakeSession({ax.file_url(H): published(doc(metric={"readsRetained": 5}))}),
                 tmp_path, delay=0, today="2026-10-11")
    assert [c for c, _ in ax.captures(d)] == ["2026-10-10"]
    assert (d / ax.TENANT_FILE).read_text() == tenant_before  # no churn
    assert ax.load_state(tmp_path)["tenants"][H] == "2026-10-11"


def test_crawl_unpublish_is_an_event_and_keeps_captures(tmp_path):
    d = _register(tmp_path)
    ax.run_crawl(FakeSession({ax.file_url(H): published(doc())}), tmp_path, delay=0, today="2026-10-10")
    ax.run_crawl(FakeSession({ax.file_url(H): BLOB_404}), tmp_path, delay=0, today="2026-10-12")
    rec = ax.load_tenant(d)
    assert rec["published"] is False
    assert rec["events"][-1] == {"date": "2026-10-12", "event": "not_published"}
    assert [c for c, _ in ax.captures(d)] == ["2026-10-10"]


def test_crawl_error_leaves_tenant_unchecked(tmp_path):
    d = _register(tmp_path)
    counts = ax.run_crawl(FakeSession({ax.file_url(H): Resp(403)}), tmp_path, delay=0, today="2026-10-10")
    assert counts["error"] == 1
    assert ax.load_tenant(d)["published"] is None
    assert H not in ax.load_state(tmp_path)["tenants"]
    assert ax.captures(d) == []


def test_crawl_order_is_oldest_checked_first(tmp_path):
    hosts = ["a.evidence.com", "b.evidence.com", "c.evidence.com"]
    for h in hosts:
        _register(tmp_path, h)
    st = ax.load_state(tmp_path)
    st["tenants"] = {"a.evidence.com": "2026-10-09", "b.evidence.com": "2026-10-01"}
    ax.save_state(tmp_path, st)
    s = FakeSession({ax.file_url(h): BLOB_404 for h in hosts})
    ax.run_crawl(s, tmp_path, limit=2, delay=0, today="2026-10-10")
    assert s.calls == [ax.file_url("c.evidence.com"), ax.file_url("b.evidence.com")]


# ── targets ──────────────────────────────────────────────────────

def entry(aid, name, role, state="CA", kind=None, lat=None, lng=None, ori=None):
    return {"agency_id": aid, "agency_role": role, "ori": ori or [],
            "geo": {"name": name, "state": state, "kind": kind or ("county" if role == "sheriff" else "place"),
                    "lat": lat, "lng": lng}}


def fbi(ori, name, typ, state="CA", lat="", lng=""):
    return {"ori": ori, "agency_name": name, "agency_type_name": typ, "state": state, "lat": lat, "lng": lng}


def hosts_of(t):
    return [h.removesuffix(".evidence.com") for h, _ in ax.candidates(t)]


def test_registry_target_candidates():
    t = ax.target_from_registry(entry("1", "Springfield", "police"))
    assert hosts_of(t) == ["springfieldpdca", "springfieldpoliceca", "springfieldpd", "springfield"]
    so = hosts_of(ax.target_from_registry(entry("2", "Springfield County", "sheriff")))
    assert so[0] == "springfieldsoca" and "springfieldcountysoca" in so
    assert not any(h.startswith("s") and len(h) <= 4 for h in so)  # one word: no initials rules
    assert hosts_of(ax.target_from_registry(entry("3", "La Cañada Flintridge", "police")))[0] == \
        "lacanadaflintridgepdca"
    assert ax.target_from_registry(entry("4", "X", "da")) is None
    assert ax.target_from_registry(entry("5", "California", "police", kind="state-only")) is None
    assert ax.target_from_registry({"agency_id": "6", "agency_role": "police"}) is None


def test_fbi_name_parsing_and_initials_rules():
    city = ax.target_from_fbi(fbi("CA1", "North Haverbrook Police Department", "City"))
    assert hosts_of(city) == ["northhaverbrookpdca", "northhaverbrookpoliceca", "northhaverbrookpd",
                              "nhpd", "northhaverbrook"]
    so = ax.target_from_fbi(fbi("CA2", "Capital City County Sheriff's Office", "County"))
    assert so["kind"] == "sheriff" and "ccsd" in hosts_of(so) and "ccsheriff" in hosts_of(so)
    dps = ax.target_from_fbi(fbi("CA3", "Ogdenville Department of Public Safety", "City"))
    assert hosts_of(dps)[0] == "ogdenvilledpsca"
    uc = ax.target_from_fbi(fbi("CA4", "University of California: West Springfield", "University or College"))
    assert hosts_of(uc) == ["ucwspd", "ucwestspringfieldpd", "ucws", "ucwestspringfield"]
    rail = ax.target_from_fbi(fbi("CA5", "Monorail Transit: Springfield County", "Other"))
    assert hosts_of(rail)[0] == "monorailtransitpdca"
    assert ax.target_from_fbi(fbi("CA6", "Highway Patrol: Springfield Area Office", "State Police")) is None
    assert ax.target_from_fbi(fbi("CA7", "Springfield Fire Marshal", "City")) is None


def test_build_targets_merges_county_rows_links_registry_and_adds_unmatched():
    rows = [fbi("CA1", "Springfield Police Department", "City"),
            fbi("CA2", "Monorail Transit: A County", "Other"),
            fbi("CA3", "Monorail Transit: B County", "Other"),
            fbi("TX1", "Lewisville Police Department", "City", state="TX")]
    reg = [entry("reg-spd", "Springfield", "police", ori=["CA1"]),
           entry("reg-shelby", "Shelbyville", "police"),          # no ORI: added from registry
           entry("reg-dup", "Springfield", "police")]             # same agency, no ORI: same candidates
    ts = ax.build_targets("CA", rows, reg)
    assert [t["name"] for t in ts] == ["Springfield Police Department", "Monorail Transit: A County",
                                       "Shelbyville"]
    assert ts[0]["agency_id"] == "reg-spd" and ts[0]["ori"] == ["CA1"]
    assert ts[1]["ori"] == ["CA2", "CA3"]
    assert ts[2]["agency_id"] == "reg-shelby" and ts[2]["ori"] == []
    assert [t["source"] for t in ts] == ["fbi", "fbi", "registry"]
    assert len(ts) == 3


def test_unlinked_targets_drops_confirmed_and_candidate_links_and_orders():
    ts = [ax.make_target("Far PD", "police", "Far", "CA", ori=["CA1"], lat=36.7, lng=-119.8),
          ax.make_target("Near PD", "police", "Near", "CA", ori=["CA2"], lat=37.58, lng=-122.36),
          ax.make_target("Linked PD", "police", "Linked", "CA", ori=["CA3"], lat=37.56, lng=-122.31),
          ax.make_target("Cand PD", "police", "Cand", "CA", agency_id="cand", lat=37.5, lng=-122.3),
          ax.make_target("Nowhere PD", "police", "Nowhere", "CA", ori=["CA5"])]
    recs = [{"agency_id": None, "ori": ["CA3"]},
            {"agency_id": None, "ori": [], "candidate": {"agency_id": "cand", "ori": []}}]
    got = [t["name"] for t in ax.unlinked_targets(ts, recs, near=(37.56, -122.31))]
    assert got == ["Near PD", "Far PD", "Nowhere PD"]


# ── probe ────────────────────────────────────────────────────────

def target(name="Test", aid="agency-1", ori=("CA1",)):
    return ax.make_target(f"{name} Police Department", "police", name, "CA", agency_id=aid, ori=ori)


def test_probe_stops_at_first_hit_and_crawls(tmp_path):
    s = FakeSession({
        ax.login_url("testpdca.evidence.com"): NO_TENANT,
        ax.login_url("testpoliceca.evidence.com"): tenant_redirect("testpoliceca.evidence.com"),
        ax.file_url("testpoliceca.evidence.com"): BLOB_404,
    })
    found = ax.run_probe(s, tmp_path, targets=[target()], delay=0, today="2026-10-10")
    assert found == ["testpoliceca.evidence.com"]
    assert ax.login_url("testpd.evidence.com") not in s.calls  # stopped at first hit
    rec = ax.load_tenant(tmp_path / "testpoliceca")
    assert (rec["agency_id"], rec["ori"], rec["name"]) == ("agency-1", ["CA1"], "Test Police Department")
    assert rec["candidate"] is None and rec["match"] == "fbi:{b}police{st}"
    assert rec["published"] is False  # crawled right away: "has tenant, publishes nothing"
    st = ax.load_state(tmp_path)
    assert st["candidates"]["testpdca.evidence.com"] == {"checked": "2026-10-10", "result": "no_tenant"}


def test_probe_fbi_only_target_records_source(tmp_path):
    s = FakeSession({ax.login_url("testpdca.evidence.com"): tenant_redirect("testpdca.evidence.com"),
                     ax.file_url("testpdca.evidence.com"): BLOB_404})
    ax.run_probe(s, tmp_path, targets=[target(aid=None)], delay=0, today="2026-10-10")
    rec = ax.load_tenant(tmp_path / "testpdca")
    assert rec["match"] == "fbi:{b}pd{st}" and rec["ori"] == ["CA1"] and rec["agency_id"] is None


def test_probe_skips_recent_negatives_and_rechecks_old_ones(tmp_path):
    st = ax.load_state(tmp_path)
    st["candidates"] = {
        "testpdca.evidence.com": {"checked": "2026-10-01", "result": "no_tenant"},     # recent: skip
        "testpoliceca.evidence.com": {"checked": "2026-01-01", "result": "no_tenant"},  # old: recheck
    }
    ax.save_state(tmp_path, st)
    s = FakeSession({ax.login_url("testpoliceca.evidence.com"): NO_TENANT,
                     ax.login_url("testpd.evidence.com"): NO_TENANT,
                     ax.login_url("test.evidence.com"): NO_TENANT})
    ax.run_probe(s, tmp_path, targets=[target()], delay=0, today="2026-10-10")
    assert s.calls == [ax.login_url("testpoliceca.evidence.com"), ax.login_url("testpd.evidence.com"),
                       ax.login_url("test.evidence.com")]


def test_probe_unknown_redirect_is_not_recorded(tmp_path):
    s = FakeSession({ax.login_url(H): Resp(200)})
    ax.run_probe(s, tmp_path, hosts=[H], delay=0, today="2026-10-10")
    assert H not in ax.load_state(tmp_path)["candidates"]
    assert ax.known_tenants(tmp_path) == {}


def test_probe_links_existing_unlinked_tenant_without_a_request(tmp_path):
    ax.register(tmp_path, "testpdca.evidence.com", "2026-10-01")
    s = FakeSession({})
    ax.run_probe(s, tmp_path, targets=[target()], delay=0, today="2026-10-10")
    assert s.calls == []
    assert ax.load_tenant(tmp_path / "testpdca")["agency_id"] == "agency-1"


def test_probe_rate_limit_stops_and_saves_state(tmp_path):
    ts = [target("Alpha"), target("Beta")]
    s = FakeSession({ax.login_url("alphapdca.evidence.com"): NO_TENANT,
                     ax.login_url("alphapoliceca.evidence.com"): Resp(429)})
    ax.run_probe(s, tmp_path, targets=ts, delay=0, today="2026-10-10")
    assert ax.load_state(tmp_path)["candidates"]["alphapdca.evidence.com"]["result"] == "no_tenant"
    assert not any("beta" in c for c in s.calls)


def test_probe_limit_counts_agencies_not_requests(tmp_path):
    ts = [target("Alpha"), target("Beta")]
    s = FakeSession({ax.login_url(h): NO_TENANT for t in ts for h, _ in ax.candidates(t)})
    ax.run_probe(s, tmp_path, targets=ts, limit=1, delay=0, today="2026-10-10")
    assert len(s.calls) == len(ax.candidates(ts[0])) and all("alpha" in c for c in s.calls)


def test_probe_limit_skips_targets_with_nothing_to_check(tmp_path):
    # Rotation: once Alpha's candidates are all recently checked, a limit-1
    # run must move on to Beta instead of spending its slot on Alpha.
    ts = [target("Alpha"), target("Beta")]
    st = ax.load_state(tmp_path)
    st["candidates"] = {h: {"checked": "2026-10-09", "result": "no_tenant"} for h, _ in ax.candidates(ts[0])}
    ax.save_state(tmp_path, st)
    s = FakeSession({ax.login_url(h): NO_TENANT for h, _ in ax.candidates(ts[1])})
    ax.run_probe(s, tmp_path, targets=ts, limit=1, delay=0, today="2026-10-10")
    assert s.calls and all("beta" in c for c in s.calls)


def test_probe_hit_without_state_code_links_only_as_candidate(tmp_path):
    s = FakeSession({
        ax.login_url("testpdca.evidence.com"): NO_TENANT,
        ax.login_url("testpoliceca.evidence.com"): NO_TENANT,
        ax.login_url("testpd.evidence.com"): tenant_redirect("testpd.evidence.com"),
        ax.file_url("testpd.evidence.com"): BLOB_404,
    })
    ax.run_probe(s, tmp_path, targets=[target()], delay=0, today="2026-10-10")
    rec = ax.load_tenant(tmp_path / "testpd")
    assert rec["agency_id"] is None and rec["ori"] == []
    assert rec["candidate"] == {"agency_id": "agency-1", "ori": ["CA1"], "name": "Test Police Department"}
    assert rec["match"] == "fbi:{b}pd"


def test_explicit_link_confirms_a_candidate(tmp_path):
    ax.register(tmp_path, "testpd.evidence.com", "2026-10-10",
                {"agency_id": "agency-1", "ori": ["CA1"], "name": "Test PD"}, "{b}pd", "registry")
    s = FakeSession({ax.login_url("testpd.evidence.com"): tenant_redirect("testpd.evidence.com"),
                     ax.file_url("testpd.evidence.com"): BLOB_404})
    ax.run_probe(s, tmp_path, hosts=["testpd.evidence.com"], link={"agency_id": None, "ori": ["CA1"], "name": "Test PD"},
                 delay=0, today="2026-10-11")
    rec = ax.load_tenant(tmp_path / "testpd")
    assert rec["ori"] == ["CA1"] and rec["candidate"] is None and rec["match"] == "manual"


def test_confirmed_link_is_never_overwritten(tmp_path):
    ax.register(tmp_path, H, "2026-10-10", {"agency_id": "a", "ori": [], "name": "A"})
    ax.register(tmp_path, H, "2026-10-11", {"agency_id": "b", "ori": [], "name": "B"})
    assert ax.load_tenant(tmp_path / "testpdca")["agency_id"] == "a"


def test_probe_unknown_verdict_does_not_fall_through_to_weaker_rule(tmp_path):
    s = FakeSession({ax.login_url("testpdca.evidence.com"): Resp(200)})  # no redirect: unrecognized
    ax.run_probe(s, tmp_path, targets=[target()], delay=0, today="2026-10-10")
    assert s.calls == [ax.login_url("testpdca.evidence.com")]
    assert ax.load_state(tmp_path)["candidates"] == {}


def test_crawl_checks_published_daily_and_unpublished_weekly(tmp_path):
    pub, unpub_recent, unpub_old = "pub.evidence.com", "recent.evidence.com", "old.evidence.com"
    for h in (pub, unpub_recent, unpub_old):
        _register(tmp_path, h)
    for h, published in ((pub, True), (unpub_recent, False), (unpub_old, False)):
        d = tmp_path / ax.host_key(h)
        rec = ax.load_tenant(d)
        rec["published"] = published
        ax.save_tenant(d, rec)
    st = ax.load_state(tmp_path)
    st["tenants"] = {pub: "2026-10-09", unpub_recent: "2026-10-08", unpub_old: "2026-10-02"}
    ax.save_state(tmp_path, st)
    s = FakeSession({ax.file_url(h): BLOB_404 for h in (pub, unpub_recent, unpub_old)})
    ax.run_crawl(s, tmp_path, delay=0, today="2026-10-10")
    assert s.calls == [ax.file_url(unpub_old), ax.file_url(pub)]
