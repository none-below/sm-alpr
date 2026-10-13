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
  crawl    Re-check known tenants (published daily, the rest weekly), oldest-
           checked first; archive the transparency JSON when published.
  probe    Discover tenants from explicit hosts, or from candidates generated
           for every agency in a state (FBI ORI directory + agency registry);
           register hits and crawl them.
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
  uv run python scripts/axon_transparency.py probe --state CA --limit 40
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
DEFAULT_UNPUBLISHED_EVERY = 7
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


def new_tenant(host, today, match="manual"):
    return {
        "host": host,
        # Confirmed link: registry agency_id and/or FBI ORIs, plus the agency
        # name it was matched from. A rule without the state code can't be
        # pinned to one agency, so its hit goes in `candidate` instead.
        "agency_id": None,
        "ori": [],
        "name": None,
        "candidate": None,
        "match": match,
        "first_seen": today,
        "published": None,
        "axon_agency": None,
        "events": [{"date": today, "event": "tenant_found"}],
    }


def is_linked(rec):
    return bool(rec.get("agency_id") or rec.get("ori"))


def apply_link(rec, link, match):
    """Attach an agency link. A confirmed link fills an unlinked record (and
    replaces a candidate); an unconfirmed one only fills an empty candidate
    slot. Existing links are never overwritten — fix those by hand. Returns
    True if the record changed."""
    if not link:
        return False
    fields = {"agency_id": link.get("agency_id"), "ori": sorted(link.get("ori") or []),
              "name": link.get("name")}
    if link.get("confirmed"):
        if is_linked(rec):
            return False
        rec.update(fields, candidate=None, match=match)
        return True
    if is_linked(rec) or rec.get("candidate"):
        return False
    rec.update(candidate=fields, match=match)
    return True


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


def crawl_due(rec, last_checked, today, unpublished_every):
    """Published tenants are checked daily; the rest every
    `unpublished_every` days, enough to catch a page being turned on."""
    if not last_checked:
        return True
    age = (date.fromisoformat(today) - date.fromisoformat(last_checked)).days
    return age >= (1 if rec.get("published") else unpublished_every)


def run_crawl(session, data_dir, hosts=None, limit=0, max_age=DEFAULT_MAX_AGE,
              delay=DEFAULT_DELAY, today=None, unpublished_every=DEFAULT_UNPUBLISHED_EVERY):
    today = today or utc_now().date().isoformat()
    tenants = known_tenants(data_dir)
    state = load_state(data_dir)
    if hosts:
        missing = [h for h in hosts if h not in tenants]
        if missing:
            raise SystemExit(f"not registered (run probe first): {', '.join(missing)}")
        todo = list(hosts)
    else:
        # Due tenants, oldest-checked first; never-checked ("") before everything.
        todo = sorted((h for h, d in tenants.items()
                       if crawl_due(load_tenant(d), state["tenants"].get(h), today, unpublished_every)),
                      key=lambda h: (state["tenants"].get(h, ""), h))
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
#
# Targets are agencies to look for: every agency in the FBI ORI directory
# (data/fbi/agencies.tsv) for the state, plus agency-registry police/sheriff
# entries that have no ORI there. Each target yields candidate tenant names
# from RULES, tried most-likely first; a probe stops at a target's first hit.
#
# A rule containing {st} pins the hit to one state, so it links as confirmed.
# Rules without it (initials like sfpd/lasd, bare city names like
# mountainview, campus abbreviations like ucsf) could be another state's
# agency or a non-police tenant of the same city, so their hits link only as
# `candidate` until someone confirms them (`probe <host> --ori/--agency-id`).

FBI_AGENCIES = Path("data/fbi/agencies.tsv")

RULES = {
    "police": ("{b}pd{st}", "{b}police{st}", "{b}pd", "{i}pd", "{b}"),
    "dps": ("{b}dps{st}", "{b}pd{st}", "{b}dps", "{b}pd", "{i}pd", "{b}"),
    "sheriff": ("{b}so{st}", "{b}countyso{st}", "{b}sheriff{st}", "{b}cso{st}", "{b}so",
                "{b}sheriff", "{b}countysheriff", "{i}so", "{i}sd", "{i}cso", "{i}sheriff"),
    "campus": ("{p}{i}pd", "{p}{b}pd", "{p}{i}", "{p}{b}"),
    "other": ("{b}pd{st}", "{b}pd", "{i}pd"),
}

_STOPWORDS = {"of", "the", "and", "at"}
_CITY = re.compile(r"^(.*?)\s+(Police Department|Police|Department of Public Safety|Public Safety)$")
_COUNTY = re.compile(r"^(.*?)\s+County\s+Sheriff'?s?\s+(?:Office|Department)$")
_CAMPUS = re.compile(r"^(University of California|California State (?:Polytechnic )?University):\s*(.+)$")


def _alnum(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _words(s):
    return [w for w in (_alnum(x) for x in re.split(r"[\s/-]+", s)) if w and w not in _STOPWORDS]


def make_target(name, kind, base, st, *, agency_id=None, ori=(), prefix="", lat=None, lng=None,
                source="fbi"):
    """`source` names the list `name`/`base` came from (recorded in a hit's
    `match`); agency_id/ori are links regardless of source."""
    return {"name": name, "kind": kind, "words": _words(base), "prefix": prefix, "source": source,
            "st": st.lower(), "agency_id": agency_id, "ori": list(ori), "lat": lat, "lng": lng}


def target_from_fbi(row, ori_to_agency=None):
    """Target for one agencies.tsv row, or None if it isn't a probe-able kind."""
    name, typ, st = row["agency_name"].strip(), row["agency_type_name"], row["state"]
    ori = row["ori"]
    kw = {"agency_id": (ori_to_agency or {}).get(ori), "ori": [ori],
          "lat": _float(row.get("lat")), "lng": _float(row.get("lng"))}
    if typ == "City":
        m = _CITY.match(name)
        if m:
            return make_target(name, "dps" if "Safety" in m.group(2) else "police", m.group(1), st, **kw)
        return None
    if typ == "County":
        m = _COUNTY.match(name)
        return make_target(name, "sheriff", m.group(1), st, **kw) if m else None
    if typ in ("University or College", "Other", "Tribal"):
        m = _CAMPUS.match(name)
        if m:
            prefix = "uc" if m.group(1).startswith("University") else "csu"
            return make_target(name, "campus", m.group(2), st, prefix=prefix, **kw)
        # "Union Pacific Railroad: Alameda County" — one agency per county row.
        return make_target(name, "other", name.split(":")[0], st, **kw)
    return None  # State Police (CHP offices), Other State Agency


def target_from_registry(entry):
    geo = entry.get("geo") or {}
    name, st = geo.get("name"), geo.get("state")
    if not name or not st or geo.get("kind") == "state-only":
        return None
    role = entry.get("agency_role")
    if role not in ("police", "sheriff"):
        return None
    if role == "sheriff":
        name = re.sub(r"\s+county$", "", name, flags=re.IGNORECASE)
    return make_target(agency_display_name(entry, fallback=name), role, name, st,
                       agency_id=entry["agency_id"], ori=entry.get("ori") or [],
                       lat=geo.get("lat"), lng=geo.get("lng"), source="registry")


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def candidates(target):
    """[(host, rule)] to try for one target, most likely first."""
    w, st, prefix = target["words"], target["st"], target["prefix"]
    if not w:
        return []
    b = "".join(w)
    i = "".join(x[0] for x in w) if len(w) >= 2 else None
    out = []
    for rule in RULES[target["kind"]]:
        if "{i}" in rule and not i:
            continue
        host = f"{rule.format(b=b, i=i, st=st, p=prefix)}.{DOMAIN}"
        if host not in (h for h, _ in out):
            out.append((host, rule))
    return out


def state_qualified(rule):
    return rule == "manual" or "{st}" in rule


def load_fbi_rows(path=FBI_AGENCIES):
    import csv
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def build_targets(state, fbi_rows, registry):
    """FBI agencies for `state`, then registry police/sheriff with no FBI
    match. Targets yielding the same candidate list (an agency listed once
    per county, like a railroad or transit police) merge their ORIs."""
    ori_to_agency = {o: e["agency_id"] for e in registry for o in (e.get("ori") or [])}
    targets = [t for t in (target_from_fbi(r, ori_to_agency) for r in fbi_rows if r.get("state") == state) if t]
    covered = {t["agency_id"] for t in targets if t["agency_id"]}
    covered_ori = {o for t in targets for o in t["ori"]}
    for e in registry:
        if (e.get("geo") or {}).get("state") != state or e["agency_id"] in covered:
            continue
        if set(e.get("ori") or []) & covered_ori:
            continue
        t = target_from_registry(e)
        if t:
            targets.append(t)
    merged, by_cands = [], {}
    for t in targets:
        key = tuple(h for h, _ in candidates(t))
        if not key:
            continue
        if key in by_cands:
            by_cands[key]["ori"] = sorted(set(by_cands[key]["ori"]) | set(t["ori"]))
            continue
        by_cands[key] = t
        merged.append(t)
    return merged


def _distance(target, near):
    lat, lng = target.get("lat"), target.get("lng")
    if lat is None or lng is None:
        return math.inf
    # Equirectangular is plenty for ordering.
    x = math.radians(lng - near[1]) * math.cos(math.radians((lat + near[0]) / 2))
    y = math.radians(lat - near[0])
    return math.hypot(x, y)


def unlinked_targets(targets, tenant_recs, near=None):
    """Drop targets already linked (confirmed or candidate) to a tenant;
    nearest first when `near` is given."""
    ids, oris = set(), set()
    for rec in tenant_recs:
        for link in (rec, rec.get("candidate") or {}):
            if link.get("agency_id"):
                ids.add(link["agency_id"])
            oris.update(link.get("ori") or [])
    out = [t for t in targets if t["agency_id"] not in ids and not set(t["ori"]) & oris]
    if near:
        out.sort(key=lambda t: _distance(t, near))
    return out


def _recently_checked(state, host, today, recheck_days):
    c = state["candidates"].get(host)
    if not c:
        return False
    age = (date.fromisoformat(today) - date.fromisoformat(c["checked"])).days
    return age < recheck_days


def register(data_dir, host, today, link=None, rule="manual", source="manual"):
    """Create the tenant record if new and attach `link` (confirmed only for
    state-qualified rules and manual links). Returns (tenant_dir, is_new)."""
    match = "manual" if rule == "manual" else f"{source}:{rule}"
    if link is not None:
        link = {**link, "confirmed": state_qualified(rule)}
    tenant_dir = data_dir / host_key(host)
    rec = load_tenant(tenant_dir)
    is_new = rec is None
    if is_new:
        rec = new_tenant(host, today, match)
    if apply_link(rec, link, match) or is_new:
        save_tenant(tenant_dir, rec)
    return tenant_dir, is_new


def run_probe(session, data_dir, hosts=(), targets=(), limit=0, recheck_days=DEFAULT_RECHECK_DAYS,
              delay=DEFAULT_DELAY, max_age=DEFAULT_MAX_AGE, link=None, today=None):
    """Probe explicit hosts (linked to `link` if given), then targets. `limit`
    caps targets that needed a request this run, so targets whose every
    candidate was checked recently cost nothing and the rotation moves on.
    A hit is registered and crawled right away."""
    today = today or utc_now().date().isoformat()
    state = load_state(data_dir)
    tenants = known_tenants(data_dir)
    found, requests_made, probed = [], 0, 0

    jobs = [(None, [(h, "manual")]) for h in hosts]
    jobs += [(t, candidates(t)) for t in targets]

    def pause():
        nonlocal requests_made
        if requests_made:
            time.sleep(delay)
        requests_made += 1

    try:
        for target, cands in jobs:
            if target is not None and limit and probed >= limit:
                break
            if target is not None:
                tlink = {k: target[k] for k in ("agency_id", "ori", "name")}
                source = target["source"]
            else:
                tlink, source = link, "manual"
            before = requests_made
            for host, rule in cands:
                if target is not None and host in tenants:
                    # Already registered (maybe unlinked): link it, no request.
                    register(data_dir, host, today, tlink, rule, source)
                    break
                if target is not None and _recently_checked(state, host, today, recheck_days):
                    continue
                pause()
                verdict = check_tenant(session, host)
                if verdict == "unknown":
                    # Give up on this target for the run rather than fall
                    # through to a less specific rule: next run retries in order.
                    print(f"  ? {host}: unrecognized login redirect; will retry next run")
                    break
                state["candidates"][host] = {"checked": today, "result": verdict}
                if verdict == "no_tenant":
                    continue
                tenant_dir, new = register(data_dir, host, today, tlink, rule, source)
                tenants[host] = tenant_dir
                who = f" ← {tlink['name'] or tlink['agency_id'] or tlink['ori']}" if tlink else ""
                print(f"  TENANT {host}{who} [{rule}]" + ("" if new else " (known)"))
                found.append(host)
                pause()
                crawl_one(session, host, tenant_dir, state, today, max_age)
                break
            if target is not None and requests_made > before:
                probed += 1
    except RateLimited as e:
        print(f"RATE LIMITED at {e}; stopping this run")
    finally:
        save_state(data_dir, state)
    print(f"probe: {requests_made} requests, {probed} agencies probed, {len(found)} tenants found")
    return found


# ── report ───────────────────────────────────────────────────────

def tenant_rows(data_dir):
    by_id = {e["agency_id"]: e for e in load_registry()}
    rows = []
    for host, d in known_tenants(data_dir).items():
        rec = load_tenant(d)
        cap_date, doc = latest_capture(d)
        doc = doc or {}
        cand = rec.get("candidate") or {}
        link = rec if is_linked(rec) else cand
        entry = by_id.get(link.get("agency_id")) or {}
        agency = (agency_display_name(entry, fallback="") if entry else None) or link.get("name")
        if agency and not is_linked(rec):
            agency += " (unconfirmed)"
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
            "agency": agency,
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
    cols = ["host", "published", "since", "axon_name", "agency", "devices", "retention", "sharing", "capture"]
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
    c.add_argument("--unpublished-every", type=int, default=DEFAULT_UNPUBLISHED_EVERY, metavar="DAYS",
                   help="re-check tenants without a published page this often (published: daily)")

    pr = sub.add_parser("probe", help="discover tenants")
    pr.add_argument("hosts", nargs="*", help="explicit tenant keys, hosts or URLs")
    pr.add_argument("--agency-id", help="registry agency_id to link explicit hosts to (confirmed)")
    pr.add_argument("--ori", action="append", default=[], help="FBI ORI to link explicit hosts to (repeatable)")
    pr.add_argument("--name", help="agency name to record with --agency-id/--ori")
    pr.add_argument("--state", help="probe every agency in this state (FBI ORI directory + registry)")
    pr.add_argument("--near", type=_parse_near, metavar="LAT,LNG", help="probe nearest agencies first")
    pr.add_argument("--limit", type=int, default=0, help="max agencies needing a request this run")
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
        counts = run_crawl(session, args.data_dir, hosts, args.limit, args.max_age, args.delay,
                           unpublished_every=args.unpublished_every)
        checked = sum(counts.values())
        return 1 if checked and counts["error"] == checked else 0

    hosts = [normalize_host(h) for h in args.hosts]
    if not hosts and not args.state:
        p.error("probe needs hosts or --state")
    link = None
    if args.agency_id or args.ori:
        link = {"agency_id": args.agency_id, "ori": args.ori, "name": args.name}
    targets = []
    if args.state:
        recs = [load_tenant(d) for d in known_tenants(args.data_dir).values()]
        targets = unlinked_targets(build_targets(args.state.upper(), load_fbi_rows(), load_registry()),
                                   recs, near=args.near)
        print(f"probe: {len(targets)} unlinked {args.state.upper()} agencies")
    run_probe(session, args.data_dir, hosts, targets, args.limit, args.recheck_days,
              args.delay, args.max_age, link)
    return 0


if __name__ == "__main__":
    sys.exit(main())
