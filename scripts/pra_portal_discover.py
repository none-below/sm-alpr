#!/usr/bin/env python3
"""Find California agencies' public records portals on hosted PRA platforms.

The three platforms that dominate California intake all host tenants as
subdomains under wildcard DNS and a wildcard TLS certificate
(*.nextrequest.com, *.govqa.us, *.mycusthelp.com, *.justfoia.com), so
neither DNS nor certificate-transparency logs enumerate them. The only way
to find a tenant is to ask each platform about a candidate name. Candidates
come from every active incorporated CA place and every CA county in
data/census/ crossed with the naming patterns agencies actually use
(cityof<name>ca, <name>ca, <name>, <name>countyca, ...), plus a hand list of
special districts, transit, campuses, and sheriffs.

Existence tests, one cheap request per candidate where possible:
  nextrequest  GET /client/requests?page_number=1 -> JSON 200 (total_count =
               published requests); unknown tenants 302 to civicplus.com.
  govqa        GET /WEBAPP/_rs/supporthome.aspx -> 302 either way, but real
               tenants' 302 carries their configured X-Frame-Options/CSP
               headers and unknown ones only the gateway default. (Following
               the redirect for an unknown tenant hangs ~22s server-side, so
               don't.) Hits get two more requests: the home page's <title>,
               and the public Archive (OpenRecordsSummary.aspx) itself, since
               many tenants publish one without linking it (San Mateo,
               Carlsbad, Riverside).
  justfoia     GET /publicportal/home/search -> 200 vs 404; hits read
               /publicportal/api/configuration for organization + isSearchEnabled.

Stages:
  probe  --platform P   probe candidates, resumable (<cache>/<P>_probed.txt,
                        <cache>/<P>_found.json)
  build                 merge caches into assets/pra_portals.json

NextRequest's Cloudflare rate limit trips somewhere above ~3 req/s from one
IP (HTTP 429, error 1015, Retry-After ~60s); every platform gets a shared
throttle and backs off on 429. A full NextRequest pass is ~10k candidates,
about an hour.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
def _primary_checkout() -> Path:
    """The main checkout, even when run from a worktree: .claude/ is gitignored
    and only populated there, so local evidence always lives under it."""
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
        return Path(common).parent
    except (OSError, subprocess.CalledProcessError):
        return REPO

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
DEFAULT_CACHE = _primary_checkout() / ".claude" / "local_evidence" / "pra-portals-ca" / "discovery"
REGISTRY = REPO / "assets" / "pra_portals.json"

# Special districts, transit, campuses, sheriffs/DAs, and county shorthands
# that the place/county patterns can't generate.
EXTRA = """bart samtrans caltrain vta actransit lametro metro octa sfmta sfgov sf sfpd ebrpd ebparks ucla
ucsd ucsf ucberkeley berkeley ucop ucdavis uci ucsb ucsc ucr ucmerced universityofcalifornia calstate csu
sjsu sdsu sfsu csulb csuf csun cpp calpoly stanford lausd sfusd ousd portofoakland portofla
portoflosangeles portofsandiego sfport flysfo lawa sdcda cadoj oag chp caltrans cdcr smcgov sccgov
lacounty lacountyrrcc lacountysheriff lasd ocgov ocsheriff sdcounty sdsheriff sbcounty acgov acsheriff
cccounty cocosheriff marincounty sonomacounty sonomasheriff scc-sheriffca sccsheriff smcsheriff smcso
ncric sacsheriff saccounty sacramentocounty cityofsacramento sacpd lapd lacity sanjose sjpd sanjoseca
oaklandca oaklandpd fresnosheriff kernsheriff kerncounty rivcosheriff riversidecounty rivco sbsheriff
santabarbaracounty slocounty ventura venturacounty vcsheriff placercounty placersheriff edcgov
contracosta alamedacounty napacounty solanocounty monterey montereycounty santacruzcounty sccounty
imperialcounty sandiegocounty sdpd sandiego sfdistrictattorney sfda alcoda lada cpuc calfire dmv
cahighwaypatrol cityofla laport lacityclerk lacoe smcoe sccoe metrolink sandag mts nctd sjrtd
goldengate ggbhtd bayareametro mtc abag cityofsanmateo sanmateo sanmateocounty smcounty redwoodcity
cityofredwoodcity burlingame fostercity cityofbelmont sanbruno southsanfrancisco ssf dalycity pacifica
millbrae hillsborough atherton menlopark portolavalley woodside eastpaloalto halfmoonbay colma brisbane
sancarlos""".split()

# Portals already known from MuckRock "records posted off-site" notes.
SEEDS = {
    "nextrequest": """carmelbythesea cathedralcityca city-of-salinas-ca cityofanaheimcapd cityofberkeleyca
        cityofelcentroca cityoffolsomca cityoffremontca cityofmarina cityofmountainviewca cityofoceansideca
        cityoforangeca cityofperrisca cityofpetalumaca cityofrichmondca cityofsantaanaca cityofventura-ca
        cityofwatsonvilleca culvercity groverbeachca lacity lacountyrrcc marincountyca oaklandca
        sanbernardinocounty sandiego santaclara scc-sheriffca unioncity ventura losaltosca""".split(),
    "govqa": ["sanmateoca", "fullerton", "cathedralcityca"],
    "justfoia": ["palmspringsca"],
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’]", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _names():
    """(kind, display name, normalized variants) for every CA place and county."""
    for row in csv.DictReader(open(REPO / "data/census/places.tsv"), delimiter="\t"):
        row = {k.strip(): v.strip() for k, v in row.items()}
        if row["USPS"] == "CA" and row["FUNCSTAT"] == "A":
            nm = re.sub(r"\s+(city|town)$", "", row["NAME"])
            variants = {norm(nm), norm(re.sub(r"\(.*?\)", "", nm))} | {norm(x) for x in re.findall(r"\((.*?)\)", nm)}
            yield "place", nm, {v for v in variants if v}
    for row in csv.DictReader(open(REPO / "data/census/counties.tsv"), delimiter="\t"):
        row = {k.strip(): v.strip() for k, v in row.items()}
        if row["USPS"] == "CA":
            yield "county", row["NAME"], {norm(re.sub(r"\s+County$", "", row["NAME"]))}


PATTERNS = {
    # NextRequest names vary the most; cityof<j>ca is the most common by far.
    "nextrequest": {
        "place": ["{j}", "{j}ca", "{j}-ca", "cityof{j}", "cityof{j}ca", "cityof{j}-ca", "city-of-{j}-ca",
                  "{j}city", "townof{j}", "townof{j}ca", "{j}pd", "{j}capd", "cityof{j}capd"],
        "county": ["{j}county", "{j}countyca", "{j}county-ca", "{j}co", "{j}sheriff", "{j}countysheriff",
                   "{j}sheriffca", "{j}countysheriffca", "{j}so", "{j}countyda", "{j}da", "{j}-sheriffca",
                   "{j}countygov", "countyof{j}", "countyof{j}ca"],
        "hyphenate": True,
    },
    "govqa": {
        "place": ["{j}", "{j}ca", "cityof{j}", "cityof{j}ca", "{j}pd", "{j}cityca"],
        "county": ["{j}county", "{j}countyca", "countyof{j}", "{j}countysheriff", "{j}sheriff",
                   "{j}countyda", "{j}co", "{j}countysheriffca"],
        "hyphenate": False,
    },
    "justfoia": {
        "place": ["{j}ca", "{j}", "cityof{j}ca", "cityof{j}", "{j}pdca"],
        "county": ["{j}countyca", "{j}county", "{j}sheriffca", "{j}countysheriffca", "countyof{j}ca", "{j}coca"],
        "hyphenate": False,
    },
}


def candidates(platform: str) -> dict[str, set[str]]:
    pats = PATTERNS[platform]
    out: dict[str, set[str]] = {}
    for kind, name, variants in _names():
        for v in variants:
            bases = {v.replace(" ", "")} | ({v.replace(" ", "-")} if pats["hyphenate"] else set())
            for b in bases:
                for p in pats[kind]:
                    out.setdefault(p.format(j=b), set()).add(f"{kind}:{name}")
    for e in EXTRA:
        out.setdefault(e, set()).add("extra")
    for s in SEEDS[platform]:
        out.setdefault(s, set()).add("seed")
    return {k: v for k, v in out.items() if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", k)}


# ------------------------------------------------------------------ probing


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class Throttle:
    def __init__(self, interval: float):
        self.interval, self._lock, self._next = interval, threading.Lock(), 0.0

    def wait(self, pause: float = 0.0) -> None:
        with self._lock:
            now = time.time()
            if pause:
                self._next = max(self._next, now + pause)
            delay = self._next - now
            self._next = max(self._next, now) + self.interval
        if delay > 0:
            time.sleep(delay)


def _get(throttle: Throttle, url: str):
    """(status, headers, body); redirects are returned, not followed."""
    err = None
    for attempt in range(6):
        throttle.wait()
        try:
            with _opener.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=25) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                throttle.wait(pause=int(e.headers.get("Retry-After") or 60) + 3)
                continue
            if e.code in (500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            return e.code, e.headers, b""
        except Exception as e:
            err = e
            time.sleep(3 * (attempt + 1))
    return "err", None, repr(err).encode()


THROTTLES = {"nextrequest": Throttle(0.33), "govqa.us": Throttle(0.34),
             "mycusthelp.com": Throttle(0.34), "justfoia": Throttle(0.4)}


def probe_nextrequest(sub: str) -> tuple[str, dict]:
    host = f"{sub}.nextrequest.com"
    status, _, body = _get(THROTTLES["nextrequest"], f"https://{host}/client/requests?page_number=1")
    if status == 200 and b"total_count" in body[:200]:
        d = json.loads(body)
        reqs = d.get("requests") or []
        return host, {"status": 200, "total_count": d.get("total_count"),
                      "departments": sorted({x.get("department_names") or "" for x in reqs})[:12]}
    return host, {"status": status}


def probe_govqa(sub: str, domain: str) -> tuple[str, dict]:
    host = f"{sub}.{domain}"
    t = THROTTLES[domain]
    status, headers, _ = _get(t, f"https://{host}/WEBAPP/_rs/supporthome.aspx")
    if status != 302 or not headers or not headers.get("X-Frame-Options"):
        return host, {"status": status if status != 302 else 302}
    loc = headers.get("Location") or ""
    if loc.startswith("/"):
        loc = f"https://{host}{loc}"
    s2, _, body = _get(t, loc)
    page = body.decode("utf-8", "replace")
    title = re.findall(r"<title[^>]*>\s*(.{0,150}?)\s*</title>", page, re.S)
    # The public Archive often exists without a link from the home page (San
    # Mateo, Carlsbad, Riverside...), so ask for it directly and count rows.
    # The session id is in the path, so the archive URL is a sibling of loc.
    s3, _, abody = _get(t, loc.rsplit("/", 1)[0] + "/OpenRecordsSummary.aspx")
    apage = abody.decode("utf-8", "replace") if s3 == 200 else ""
    items = re.findall(r"Page \d+ of \d+ \((\d+) items?\)", apage)
    rows = len(re.findall(r'<tr id="gridView_DXDataRow\d+"', apage))
    return host, {"status": s2, "title": title[0].strip() if title else None,
                  "archive": rows > 0, "archive_items": int(items[0]) if items else rows,
                  "archive_linked": "OpenRecordsSummary" in page}


def probe_justfoia(sub: str) -> tuple[str, dict]:
    host = f"{sub}.justfoia.com"
    t = THROTTLES["justfoia"]
    status, _, _ = _get(t, f"https://{host}/publicportal/home/search")
    if status != 200:
        return host, {"status": status}
    _, _, body = _get(t, f"https://{host}/publicportal/api/configuration")
    try:
        cfg = json.loads(body)["configurationResults"]
    except (ValueError, KeyError, TypeError):
        cfg = {}
    return host, {"status": 200, "organization": cfg.get("organization"),
                  "search_enabled": cfg.get("isSearchEnabled")}


def cmd_probe(args) -> None:
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    platform = args.platform
    cands = candidates(platform)
    jobs = ([(s, d) for s in cands for d in ("govqa.us", "mycusthelp.com")] if platform == "govqa"
            else [(s, None) for s in cands])
    probed_path, found_path = cache / f"{platform}_probed.txt", cache / f"{platform}_found.json"
    probed = set(probed_path.read_text().split()) if probed_path.exists() else set()
    found = json.loads(found_path.read_text()) if found_path.exists() else {}
    host_of = {"nextrequest": lambda s, d: f"{s}.nextrequest.com", "govqa": lambda s, d: f"{s}.{d}",
               "justfoia": lambda s, d: f"{s}.justfoia.com"}[platform]
    if args.recheck_found:
        # Re-probe only known tenants (e.g. after changing what a probe records).
        todo = [(s, d) for s, d in jobs if host_of(s, d) in found]
    else:
        todo = [(s, d) for s, d in jobs if host_of(s, d) not in probed]
    print(f"{platform}: {len(cands)} candidates, {len(todo)} to probe", flush=True)
    fn = {"nextrequest": lambda s, d: probe_nextrequest(s), "govqa": probe_govqa,
          "justfoia": lambda s, d: probe_justfoia(s)}[platform]
    miss = {"nextrequest": {302}, "govqa": {302}, "justfoia": {404}}[platform]
    with open(probed_path, "a") as pf, ThreadPoolExecutor(args.threads) as ex:
        futs = {ex.submit(fn, s, d): s for s, d in todo}
        for n, f in enumerate(as_completed(futs), 1):
            host, r = f.result()
            if r["status"] != "err":
                pf.write(host + "\n")
                pf.flush()
            if r["status"] not in miss and r["status"] != "err":
                r["sources"] = sorted(cands[futs[f]])
                found[host] = r
                print(f"  {host} {json.dumps(r)[:200]}", flush=True)
            if n % 500 == 0:
                print(f"  ...{n}/{len(todo)}", flush=True)
                found_path.write_text(json.dumps(found, indent=1))
    found_path.write_text(json.dumps(found, indent=1))
    print(f"{platform}: {len(found)} tenants found", flush=True)


# ------------------------------------------------------------------- build

# Tenants whose name collides with a CA place but belong elsewhere.
NOT_CA = re.compile(r"\b(?:university|college)\b|,\s*(?!CA\b|California\b)[A-Z]{2}\b", re.I)
CA_RE = re.compile(r"\b(?:CA|California)\b")


def agency_from_sources(sources: list[str]) -> str | None:
    named = [s.split(":", 1)[1] for s in sources if ":" in s]
    return named[0] if len(set(named)) == 1 else (" / ".join(sorted(set(named))) or None)


def cmd_build(args) -> None:
    cache = Path(args.cache)
    portals = []
    for platform in ("nextrequest", "govqa", "justfoia"):
        path = cache / f"{platform}_found.json"
        if not path.exists():
            continue
        for host, r in sorted(json.loads(path.read_text()).items()):
            if r.get("status") != 200:
                continue
            label = r.get("title") or r.get("organization")
            ca = None
            if label:
                # A CA/California mention wins ("University of California, Irvine");
                # otherwise another state or a non-CA university rules it out.
                ca = True if CA_RE.search(label) else (False if NOT_CA.search(label) else None)
            elif re.search(r"(?:ca|-ca|california|capd)$", host.split(".")[0]):
                ca = True
            if ca is False and not args.keep_non_ca:
                continue
            entry = {
                "platform": platform,
                "host": host,
                "agency": agency_from_sources(r.get("sources", [])),
                "label": label,
                "ca_confirmed": ca,
                "searchable": {"nextrequest": bool(r.get("total_count")),
                               "govqa": bool(r.get("archive")),
                               "justfoia": bool(r.get("search_enabled"))}[platform],
                "sources": r.get("sources", []),
            }
            if platform == "nextrequest":
                entry["published_requests"] = r.get("total_count")
            elif platform == "govqa" and "archive_items" in r:
                entry["published_requests"] = r["archive_items"]
            portals.append(entry)
    REGISTRY.write_text(json.dumps({
        "_comment": ("Public records portals of California agencies on hosted PRA platforms, "
                     "found by scripts/pra_portal_discover.py. 'searchable' = the portal lets "
                     "anyone search published requests without an account. 'ca_confirmed' is "
                     "null when only the subdomain name ties the tenant to CA."),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "portals": portals}, indent=1) + "\n")
    by = {}
    for p in portals:
        k = (p["platform"], p["searchable"])
        by[k] = by.get(k, 0) + 1
    print(f"wrote {REGISTRY.relative_to(REPO)}: {len(portals)} portals; "
          + ", ".join(f"{pl} {'searchable' if s else 'closed'}={n}" for (pl, s), n in sorted(by.items())))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--platform", required=True, choices=sorted(PATTERNS))
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--recheck-found", action="store_true", help="re-probe only tenants already found")
    p.set_defaults(func=cmd_probe)
    b = sub.add_parser("build")
    b.add_argument("--keep-non-ca", action="store_true")
    b.set_defaults(func=cmd_build)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
