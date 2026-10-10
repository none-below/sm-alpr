#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Axon ALPR "Policy and Usage" transparency page archiver.

Axon Evidence tenants can opt in to a public ALPR dashboard at
https://<tenant>.evidence.com/alpr/public/policy. That page is a JS shell; the
data is one JSON blob the app fetches from the same host:

  https://<host>/alpr/public/v1/transparency/file/<first-host-label>.json

  200            published: agencyInfo, policy, metric, transparencySetting,
                 policyStatements, deviceBreakdown, categoryBreakdown and, when
                 the agency shares, sharedAgencies [{agencyId, agencyName,
                 state, sharedSince}] ("Sharing LPR Data With")
  404 BlobNotFound
                 not published, OR no such tenant (the response is identical)

Tenant existence is a separate one-request check: GET /api/oauth2/login without
following redirects. A real tenant 303s to the identity host with
`domain=<host>` in the query; a nonexistent one 303s to a generic
`login?return_url=/axon/dashboard`.

A tenant existing is NOT evidence of ALPR use. Axon Evidence (body-worn camera
storage) is near-universal among police departments; "has a tenant, publishes
nothing" only means something where ALPR use is established independently.

Commands:
  crawl    Re-check every known tenant, oldest-checked first; archive the
           transparency JSON when published.
  probe    Discover tenants from explicit hosts or candidates generated from
           the agency registry; register hits and crawl them.
  report   Table of known tenants (--markdown for PR bodies).

Layout (dates are UTC):
  assets/evidence.com/
    <key>/                      key = hostname minus ".evidence.com"
                                (lewisvillepdtx, hps.ca)
      tenant.json               identity + publish-state transitions only
      <YYYY-MM-DD>.json         raw transparency JSON, byte-for-byte as served
      <YYYY-MM-DD>.meta.json    fetch provenance: url, time, sha256, headers
    .state.json                 last-checked date per tenant + probed candidates

A new dated capture is written when anything except the rolling counters
(retained read/hit counts, per-category hit counts, updatedAt) changed, or when
the newest capture is --max-age days old. Policy, sharing-list and device
changes are archived the day they're seen; counters land weekly.

Usage:
  uv run python scripts/axon_transparency.py crawl
  uv run python scripts/axon_transparency.py probe lewisvillepdtx hps.ca
  uv run python scripts/axon_transparency.py probe --registry --state CA --limit 40
  uv run python scripts/axon_transparency.py report --markdown
"""

import functools
print = functools.partial(print, flush=True)

import argparse
import base64
import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import agency_display_name, load_json, load_registry, save_json  # noqa: E402

DATA_DIR = Path("assets/evidence.com")
STATE_FILE = ".state.json"
TENANT_FILE = "tenant.json"
DOMAIN = "evidence.com"
USER_AGENT = "sm-alpr axon_transparency (public-records research; github.com/none-below/sm-alpr)"

DEFAULT_DELAY = 1.5
DEFAULT_MAX_AGE = 7
DEFAULT_RECHECK_DAYS = 90

# Rolling counters that move every day without anything about the program
# changing. Everything else in the file (retention, hotlist categories, policy
# text, sharing list, device counts, vehicle count, display toggles) is
# "material" and triggers a capture as soon as it's seen.
VOLATILE_PATHS = (
    ("updatedAt",),
    ("metric", "readsRetained"),
    ("metric", "hitsRetained"),
    ("categoryBreakdown",),
)

_DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


class RateLimited(Exception):
    """The host answered 429 — stop the run rather than push through."""


# ── hosts ────────────────────────────────────────────────────────

def normalize_host(text):
    """Accept a key ("lewisvillepdtx", "hps.ca"), a hostname, or a URL and
    return the full hostname ("lewisvillepdtx.evidence.com")."""
    t = text.strip().lower()
    if "://" in t:
        t = urllib.parse.urlsplit(t).hostname or ""
    t = t.strip("/").split("/")[0]
    if not t:
        raise ValueError(f"empty host in {text!r}")
    if t == DOMAIN or not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*", t):
        raise ValueError(f"not a tenant host: {text!r}")
    return t if t.endswith("." + DOMAIN) else f"{t}.{DOMAIN}"


def host_key(host):
    return host[: -len("." + DOMAIN)]


def file_url(host):
    # The app names the blob after the first hostname label, so a regional
    # tenant (hps.ca.evidence.com) reads .../file/hps.json.
    return f"https://{host}/alpr/public/v1/transparency/file/{host.split('.')[0]}.json"


def login_url(host):
    return f"https://{host}/api/oauth2/login"


def classify_login_redirect(status, location, host):
    """'tenant' / 'no_tenant' / 'unknown' from the login endpoint's redirect."""
    if not (300 <= status < 400) or not location:
        return "unknown"
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    if host in q.get("domain", []):
        return "tenant"
    if "return_url" in q and "domain" not in q:
        return "no_tenant"
    return "unknown"


# ── HTTP ─────────────────────────────────────────────────────────

def new_session():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _get(session, url, *, redirects=True, retries=3):
    """GET with backoff on connection errors and 5xx. Raises RateLimited on
    429. Returns the final response, or None after exhausting retries."""
    backoff = 5
    for attempt in range(retries):
        try:
            r = session.get(url, allow_redirects=redirects, timeout=30)
        except Exception as e:  # requests' ConnectionError/Timeout/etc.
            print(f"  ! {url}: {type(e).__name__}: {e}")
            r = None
        if r is not None:
            if r.status_code == 429:
                raise RateLimited(url)
            if r.status_code < 500:
                return r
            print(f"  ! {url}: HTTP {r.status_code}")
        if attempt < retries - 1:
            time.sleep(backoff)
            backoff *= 2
    return None


def check_tenant(session, host):
    r = _get(session, login_url(host), redirects=False)
    if r is None:
        return "unknown"
    return classify_login_redirect(r.status_code, r.headers.get("location", ""), host)


def utc_now():
    return datetime.now(timezone.utc)


def fetch_transparency(session, host, now=None):
    """Fetch one tenant's transparency file.

    Returns {"status": "published"|"not_published"|"error", ...}. A published
    result carries the raw body bytes, the parsed doc, and a provenance dict.
    Anything unexpected is an error and nothing gets written for it.
    """
    now = now or utc_now()
    url = file_url(host)
    r = _get(session, url)
    if r is None:
        return {"status": "error", "error": "no response"}
    body = r.content
    if r.status_code == 404:
        if b"BlobNotFound" in body:
            return {"status": "not_published"}
        return {"status": "error", "error": f"404 without BlobNotFound ({len(body)} bytes)"}
    if r.status_code != 200:
        return {"status": "error", "error": f"HTTP {r.status_code}"}

    md5_header = r.headers.get("content-md5")
    md5_ok = None
    if md5_header:
        md5_ok = base64.b64encode(hashlib.md5(body).digest()).decode() == md5_header
        if not md5_ok:
            return {"status": "error", "error": "Content-MD5 mismatch"}
    try:
        doc = json.loads(body)
    except ValueError as e:
        return {"status": "error", "error": f"body is not JSON: {e}"}
    if not isinstance(doc, dict) or not isinstance(doc.get("agencyInfo"), dict):
        return {"status": "error", "error": "JSON lacks agencyInfo"}

    updated = doc.get("updatedAt")
    meta = {
        "url": url,
        "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "http_status": r.status_code,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "content_md5": md5_header,
        "content_md5_ok": md5_ok,
        "last_modified": r.headers.get("last-modified"),
        "etag": r.headers.get("etag"),
        "x_ms_request_id": r.headers.get("x-ms-request-id"),
        "updated_at": (datetime.fromtimestamp(updated, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                       if isinstance(updated, (int, float)) else None),
    }
    return {"status": "published", "body": body, "doc": doc, "meta": meta}


# ── snapshots ────────────────────────────────────────────────────

def material_view(doc):
    """The doc minus the rolling counters in VOLATILE_PATHS."""
    view = copy.deepcopy(doc)
    for path in VOLATILE_PATHS:
        node = view
        for k in path[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return view


def captures(tenant_dir):
    """Sorted [(YYYY-MM-DD, path)] of a tenant's dated captures."""
    out = []
    if tenant_dir.is_dir():
        for p in tenant_dir.iterdir():
            m = _DATED.match(p.name)
            if m:
                out.append((m.group(1), p))
    return sorted(out)


def latest_capture(tenant_dir):
    """(date, doc) of the newest capture, or (None, None)."""
    caps = captures(tenant_dir)
    if not caps:
        return None, None
    d, p = caps[-1]
    return d, json.loads(p.read_bytes())


def should_capture(prev_date, prev_doc, doc, today, max_age):
    """Why to write a new capture ('first'/'changed'/'stale'), or None."""
    if prev_doc is None:
        return "first"
    if material_view(prev_doc) != material_view(doc):
        return "changed"
    age = (date.fromisoformat(today) - date.fromisoformat(prev_date)).days
    if age >= max_age:
        return "stale"
    return None


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


def write_capture(tenant_dir, today, body, meta):
    """Raw body first, then its provenance — a meta file never points at a
    body that isn't there."""
    _atomic_write(tenant_dir / f"{today}.json", body)
    _atomic_write(tenant_dir / f"{today}.meta.json",
                  (json.dumps(meta, indent=2, sort_keys=True) + "\n").encode())


def _shared_names(doc):
    names = []
    for a in doc.get("sharedAgencies") or []:
        if isinstance(a, dict):
            n = a.get("agencyName") or a.get("agencyId") or "?"
            st = (a.get("state") or "").strip()
            names.append(f"{n}, {st}" if st else n)
    return names


def material_changes(prev, cur):
    """Human-readable lines describing material differences, for logs/PRs."""
    if prev is None:
        return []
    out = []
    pa, ca = set(_shared_names(prev)), set(_shared_names(cur))
    for n in sorted(ca - pa):
        out.append(f"sharing added: {n}")
    for n in sorted(pa - ca):
        out.append(f"sharing removed: {n}")
    pv, cv = material_view(prev), material_view(cur)
    pv.pop("sharedAgencies", None)
    cv.pop("sharedAgencies", None)
    for k in sorted(set(pv) | set(cv)):
        a, b = pv.get(k), cv.get(k)
        if a == b:
            continue
        if isinstance(a, dict) and isinstance(b, dict):
            for sk in sorted(set(a) | set(b)):
                if a.get(sk) != b.get(sk):
                    out.append(f"{k}.{sk}: {_short(a.get(sk))} → {_short(b.get(sk))}")
        else:
            out.append(f"{k}: {_short(a)} → {_short(b)}")
    return out


def _short(v, n=80):
    s = json.dumps(v, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= n else s[: n - 1] + "…"


# ── tenant records ───────────────────────────────────────────────

def load_tenant(tenant_dir):
    p = tenant_dir / TENANT_FILE
    return json.loads(p.read_text()) if p.exists() else None


def save_tenant(tenant_dir, rec):
    save_json(tenant_dir / TENANT_FILE, rec)


def new_tenant(host, today, agency_id=None, match="manual", candidate_agency_id=None):
    return {
        "host": host,
        "agency_id": agency_id,
        "candidate_agency_id": candidate_agency_id,
        "match": match,
        "first_seen": today,
        "published": None,
        "axon_agency": None,
        "events": [{"date": today, "event": "tenant_found"}],
    }


def apply_check(rec, today, result):
    """Fold one crawl result into the tenant record. Returns True if the
    record changed. Only state transitions add events, so the file's diff
    is the history of the page being turned on or off."""
    published = result["status"] == "published"
    changed = False
    if rec.get("published") is not published:
        rec["published"] = published
        rec["events"].append({"date": today, "event": "published" if published else "not_published"})
        changed = True
    if published:
        info = result["doc"].get("agencyInfo") or {}
        axon = {"id": info.get("id"), "name": info.get("name")}
        if rec.get("axon_agency") != axon:
            rec["axon_agency"] = axon
            changed = True
    return changed


def known_tenants(data_dir):
    """{host: tenant_dir} for every registered tenant."""
    out = {}
    if data_dir.is_dir():
        for d in sorted(data_dir.iterdir()):
            rec = load_tenant(d) if d.is_dir() else None
            if rec:
                out[rec["host"]] = d
    return out


def load_state(data_dir):
    st = load_json(data_dir / STATE_FILE)
    st.setdefault("tenants", {})
    st.setdefault("candidates", {})
    return st


def save_state(data_dir, st):
    save_json(data_dir / STATE_FILE, st)


# ── crawl ────────────────────────────────────────────────────────

def crawl_one(session, host, tenant_dir, state, today, max_age, now=None):
    """Check one tenant; archive if warranted. Returns the result status."""
    rec = load_tenant(tenant_dir)
    result = fetch_transparency(session, host, now=now)
    status = result["status"]
    if status == "error":
        print(f"  ERROR {host}: {result['error']}")
        return status

    if apply_check(rec, today, result):
        save_tenant(tenant_dir, rec)
    state["tenants"][host] = today

    if status == "not_published":
        print(f"  {host}: not published")
        return status

    prev_date, prev_doc = latest_capture(tenant_dir)
    why = should_capture(prev_date, prev_doc, result["doc"], today, max_age)
    if why:
        write_capture(tenant_dir, today, result["body"], result["meta"])
        print(f"  {host}: captured {today} ({why})")
        for line in material_changes(prev_doc, result["doc"]):
            print(f"    CHANGED {host}: {line}")
    else:
        print(f"  {host}: published, no material change since {prev_date}")
    return status


def run_crawl(session, data_dir, hosts=None, limit=0, max_age=DEFAULT_MAX_AGE,
              delay=DEFAULT_DELAY, today=None):
    today = today or utc_now().date().isoformat()
    tenants = known_tenants(data_dir)
    state = load_state(data_dir)
    if hosts:
        missing = [h for h in hosts if h not in tenants]
        if missing:
            raise SystemExit(f"not registered (run probe first): {', '.join(missing)}")
        todo = list(hosts)
    else:
        # Oldest-checked first; never-checked ("") before everything.
        todo = sorted(tenants, key=lambda h: (state["tenants"].get(h, ""), h))
    if limit:
        todo = todo[:limit]

    counts = {"published": 0, "not_published": 0, "error": 0}
    try:
        for i, host in enumerate(todo):
            if i:
                time.sleep(delay)
            counts[crawl_one(session, host, tenants[host], state, today, max_age)] += 1
    except RateLimited as e:
        print(f"RATE LIMITED at {e}; stopping this run")
        counts["error"] += 1
    finally:
        save_state(data_dir, state)
    print(f"crawl: {counts['published']} published, {counts['not_published']} not published, "
          f"{counts['error']} errors ({len(todo)} checked)")
    return counts


# ── probe ────────────────────────────────────────────────────────

def _alnum(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


# Ordered most-likely first; a probe stops at an agency's first hit. Patterns
# without the state code come last because a hit on one can't be pinned to a
# state (hillsboroughpd could be Hillsborough NC or NH). Such a hit is stored
# as candidate_agency_id, not agency_id, until someone confirms it (the
# published agencyInfo.name, or a records request) and links it with
# `probe <host> --agency-id`.
POLICE_RULES = ("{b}pd{st}", "{b}police{st}", "{b}pd")
SHERIFF_RULES = ("{b}so{st}", "{b}countyso{st}", "{b}coso{st}", "{b}sheriff{st}",
                 "{b}cso{st}", "{b}so", "{b}sheriff")


def agency_candidates(entry):
    """[(host, rule)] to try for one registry entry, most likely first."""
    geo = entry.get("geo") or {}
    name, st = geo.get("name"), (geo.get("state") or "").lower()
    if not name or not st or geo.get("kind") == "state-only":
        return []
    role = entry.get("agency_role")
    if role == "police":
        rules = POLICE_RULES
    elif role == "sheriff":
        rules = SHERIFF_RULES
        name = re.sub(r"\s+county$", "", name, flags=re.IGNORECASE)
    else:
        return []
    b = _alnum(name)
    if not b:
        return []
    out = []
    for rule in rules:
        host = f"{rule.format(b=b, st=st)}.{DOMAIN}"
        if host not in (h for h, _ in out):
            out.append((host, rule))
    return out


def _distance(entry, near):
    geo = entry.get("geo") or {}
    lat, lng = geo.get("lat"), geo.get("lng")
    if lat is None or lng is None:
        return math.inf
    # Equirectangular is plenty for ordering.
    x = math.radians(lng - near[1]) * math.cos(math.radians((lat + near[0]) / 2))
    y = math.radians(lat - near[0])
    return math.hypot(x, y)


def registry_targets(entries, linked_ids, state=None, near=None):
    """Registry entries worth probing: police/sheriff, in `state` if given,
    not already linked to a tenant. Nearest first when `near` is given."""
    out = [e for e in entries
           if e.get("agency_id") not in linked_ids
           and (state is None or (e.get("geo") or {}).get("state") == state)
           and agency_candidates(e)]
    if near:
        out.sort(key=lambda e: _distance(e, near))
    return out


def _recently_checked(state, host, today, recheck_days):
    c = state["candidates"].get(host)
    if not c:
        return False
    age = (date.fromisoformat(today) - date.fromisoformat(c["checked"])).days
    return age < recheck_days


def state_qualified(rule):
    return rule == "manual" or "{st}" in rule


def register(data_dir, host, today, agency_id=None, rule="manual"):
    """Create or link a tenant record. A registry hit on a rule without the
    state code links only as candidate_agency_id; an explicit (manual)
    --agency-id or a state-qualified rule links agency_id."""
    match = rule if rule == "manual" else f"registry:{rule}"
    confirmed = state_qualified(rule)
    aid = agency_id if confirmed else None
    cand = None if confirmed else agency_id
    tenant_dir = data_dir / host_key(host)
    rec = load_tenant(tenant_dir)
    if rec is None:
        save_tenant(tenant_dir, new_tenant(host, today, aid, match, cand))
        return tenant_dir, True
    if aid and not rec.get("agency_id"):
        rec.update(agency_id=aid, candidate_agency_id=None, match=match)
        save_tenant(tenant_dir, rec)
    elif cand and not rec.get("agency_id") and not rec.get("candidate_agency_id"):
        rec.update(candidate_agency_id=cand, match=match)
        save_tenant(tenant_dir, rec)
    return tenant_dir, False


def run_probe(session, data_dir, hosts=(), entries=(), limit=0, recheck_days=DEFAULT_RECHECK_DAYS,
              delay=DEFAULT_DELAY, max_age=DEFAULT_MAX_AGE, agency_id=None, today=None):
    """Probe explicit hosts, then registry entries (up to `limit` agencies).
    A hit is registered and crawled right away."""
    today = today or utc_now().date().isoformat()
    state = load_state(data_dir)
    tenants = known_tenants(data_dir)
    found, checked = [], 0

    # Explicit hosts carry no candidate list; registry entries try their rules
    # in order and stop at the first hit.
    jobs = [(None, [(h, "manual")]) for h in hosts]
    jobs += [(e, agency_candidates(e)) for e in entries]
    if limit:
        jobs = jobs[: len(hosts) + limit]

    def pause():
        nonlocal checked
        if checked:
            time.sleep(delay)
        checked += 1

    try:
        for entry, cands in jobs:
            aid = entry["agency_id"] if entry else agency_id
            for host, rule in cands:
                if entry is not None and host in tenants:
                    # Already registered (maybe unlinked): link it, no request.
                    register(data_dir, host, today, aid, rule)
                    break
                if entry is not None and _recently_checked(state, host, today, recheck_days):
                    continue
                pause()
                verdict = check_tenant(session, host)
                if verdict == "unknown":
                    # Give up on this agency for the run rather than fall
                    # through to a less specific rule: next run retries in order.
                    print(f"  ? {host}: unrecognized login redirect; will retry next run")
                    break
                state["candidates"][host] = {"checked": today, "result": verdict}
                if verdict == "no_tenant":
                    continue
                tenant_dir, new = register(data_dir, host, today, aid, rule)
                tenants[host] = tenant_dir
                print(f"  TENANT {host}" + (f" (agency {aid}, {rule})" if aid else "") + ("" if new else " [known]"))
                found.append(host)
                pause()
                crawl_one(session, host, tenant_dir, state, today, max_age)
                break
    except RateLimited as e:
        print(f"RATE LIMITED at {e}; stopping this run")
    finally:
        save_state(data_dir, state)
    print(f"probe: {checked} requests, {len(found)} tenants found")
    return found


# ── report ───────────────────────────────────────────────────────

def tenant_rows(data_dir):
    by_id = {e["agency_id"]: e for e in load_registry()}
    rows = []
    for host, d in known_tenants(data_dir).items():
        rec = load_tenant(d)
        cap_date, doc = latest_capture(d)
        doc = doc or {}
        aid = rec.get("agency_id") or rec.get("candidate_agency_id")
        entry = by_id.get(aid) or {}
        registry_name = agency_display_name(entry, fallback="") if entry else None
        if registry_name and not rec.get("agency_id"):
            registry_name += " (unconfirmed)"
        settings = doc.get("transparencySetting") or {}
        if doc.get("sharedAgencies"):
            sharing = len(doc["sharedAgencies"])
        elif settings.get("metric_enable_sharing_disclosure") is False:
            sharing = "hidden"
        else:
            sharing = None
        devices = doc.get("deviceBreakdown") or {}
        policy = doc.get("policy") or {}
        rows.append({
            "host": host,
            "published": rec.get("published"),
            "since": next((ev["date"] for ev in reversed(rec["events"])
                           if ev["event"] in ("published", "not_published")), None),
            "axon_name": (rec.get("axon_agency") or {}).get("name"),
            "registry": registry_name,
            "match": rec.get("match"),
            "capture": cap_date,
            "devices": ", ".join(f"{k} {v}" for k, v in devices.items() if v) or None,
            "retention": " / ".join(
                f"{_days(policy.get(k))} {what}"
                for k, what in (("readRetentionSetting", "reads"), ("hitRetentionSetting", "hits"))
            ) if policy else None,
            "sharing": sharing,
        })
    rows.sort(key=lambda r: (r["published"] is not True, r["host"]))
    return rows


def _days(v):
    return "hidden" if v is None else f"{v}d"


def print_report(rows, markdown=False):
    cols = ["host", "published", "since", "axon_name", "registry", "devices", "retention", "sharing", "capture"]
    fmt = lambda v: "—" if v is None else str(v)  # noqa: E731
    if markdown:
        print("| " + " | ".join(cols) + " |")
        print("|" + "---|" * len(cols))
        for r in rows:
            print("| " + " | ".join(fmt(r[c]).replace("|", "\\|") for c in cols) + " |")
        return
    for r in rows:
        print("  ".join(f"{c}={fmt(r[c])}" for c in cols))


# ── CLI ──────────────────────────────────────────────────────────

def _parse_near(s):
    lat, lng = (float(x) for x in s.split(","))
    return lat, lng


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data-dir", type=Path, default=DATA_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="re-check known tenants")
    c.add_argument("hosts", nargs="*", help="only these tenants (key, host or URL)")
    c.add_argument("--limit", type=int, default=0, help="max tenants this run (oldest-checked first)")
    c.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE, metavar="DAYS",
                   help="write a capture after this long even if only counters changed")
    c.add_argument("--delay", type=float, default=DEFAULT_DELAY, metavar="SECONDS")

    pr = sub.add_parser("probe", help="discover tenants")
    pr.add_argument("hosts", nargs="*", help="explicit tenant keys, hosts or URLs")
    pr.add_argument("--agency-id", help="registry agency_id to link explicit hosts to")
    pr.add_argument("--registry", action="store_true", help="generate candidates from the agency registry")
    pr.add_argument("--state", help="registry candidates only for this 2-letter state")
    pr.add_argument("--near", type=_parse_near, metavar="LAT,LNG", help="probe nearest agencies first")
    pr.add_argument("--limit", type=int, default=0, help="max registry agencies this run")
    pr.add_argument("--recheck-days", type=int, default=DEFAULT_RECHECK_DAYS,
                    help="skip candidates found absent within this many days")
    pr.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE, metavar="DAYS")
    pr.add_argument("--delay", type=float, default=DEFAULT_DELAY, metavar="SECONDS")

    r = sub.add_parser("report", help="table of known tenants")
    r.add_argument("--markdown", action="store_true")
    r.add_argument("--published-only", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "report":
        rows = tenant_rows(args.data_dir)
        if args.published_only:
            rows = [r for r in rows if r["published"]]
        print_report(rows, markdown=args.markdown)
        return 0

    session = new_session()
    if args.cmd == "crawl":
        hosts = [normalize_host(h) for h in args.hosts]
        counts = run_crawl(session, args.data_dir, hosts, args.limit, args.max_age, args.delay)
        checked = sum(counts.values())
        return 1 if checked and counts["error"] == checked else 0

    hosts = [normalize_host(h) for h in args.hosts]
    entries = []
    if args.registry:
        linked = set()
        for d in known_tenants(args.data_dir).values():
            rec = load_tenant(d)
            linked |= {rec.get("agency_id"), rec.get("candidate_agency_id")}
        entries = registry_targets(load_registry(), linked - {None},
                                   state=args.state.upper() if args.state else None, near=args.near)
    elif not hosts:
        p.error("probe needs hosts or --registry")
    run_probe(session, args.data_dir, hosts, entries, args.limit, args.recheck_days,
              args.delay, args.max_age, args.agency_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
