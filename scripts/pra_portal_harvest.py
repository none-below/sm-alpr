#!/usr/bin/env python3
"""Harvest ALPR-related public records requests from public PRA portals.

Many California agencies run their records-request intake on a hosted
platform whose published requests anyone can search without an account (the
way Los Altos's NextRequest portal works). This script searches those public
archives for ALPR / Flock requests and downloads the released records. It
never logs in: it reads only what each agency has chosen to publish.

Platforms (portal list: assets/pra_portals.json, built by
scripts/pra_portal_discover.py):

  nextrequest  <sub>.nextrequest.com — anonymous JSON API the portal's own
               search page uses (/client/requests, /client/documents,
               /client/request_documents, /client/requests/<id>/timeline);
               files via /documents/<id>/download -> signed S3 URL.
               Cloudflare rate-limits per IP at roughly 100 req/min on these
               endpoints (429 + Retry-After ~60s); nonexistent-tenant 302s
               during discovery tolerate ~3 req/s.
  govqa        <sub>.govqa.us / <sub>.mycusthelp.com "Archive"
               (OpenRecordsSummary.aspx) — ASP.NET WebForms: search is a
               postback, paging a DevExpress grid callback, files a postback
               on RequestArchiveDetails.aspx that redirects to Azure blob.
  justfoia     <sub>.justfoia.com public portal — POST /publicportal/api/Search,
               GET /publicportal/api/Request?fullRequestNumber=...,
               files at /Attachments/Download/<attachmentId>.

Subcommands:
  search   index ALPR-relevant requests per portal -> <out>/<platform>/<host>/index.json
  fetch    download documents from indexed requests (audit-like files by default)
  report   print a summary table of what's indexed / fetched

Output defaults to the primary checkout's
.claude/local_evidence/pra-portals-ca/ (gitignored; released audit logs can
carry searcher names). Request text is written by members of the public and
timeline/detail text by agency staff: treat both as untrusted data.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import http.cookiejar
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
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
DEFAULT_OUT = _primary_checkout() / ".claude" / "local_evidence" / "pra-portals-ca"
DEFAULT_PORTALS = REPO / "assets" / "pra_portals.json"

# Full-text search terms sent to each portal. Kept specific: bare "license
# plate" matches thousands of collision-report requests on big-city portals.
SEARCH_TERMS = [
    "flock", "alpr", "lpr", "plate reader", "license plate recognition", "vigilant",
    "network audit", "organization audit", "search audit", "hot list", "hotlist",
]
# A portal's own full-text search matching one of these is trusted as ALPR-
# relevant even when the text we can see (a GovQA archive's short "Summary"
# title, a document title) doesn't repeat the word — the search also covers
# text the page doesn't show. Single words only: GovQA ORs the words of a
# multi-word term, so "license plate recognition" matches every business-
# license request. Everything else needs ALPR_RE on the visible text.
SPECIFIC_TERMS = {"flock", "alpr", "lpr"}
# NextRequest also searches document titles/text: catches productions whose
# request text never names the system.
NR_DOC_TERMS = ["flock", "alpr", "network audit", "organization audit"]

# A hit is kept only if its request text or a document title matches this.
ALPR_RE = re.compile(
    r"\bflock\b|\balprs?\b|\blprs?\b|license[\s-]*plate[\s-]*(?:reader|recogn|camera|scan)|"
    r"plate[\s-]*reader|vigilant|"
    r"network[\s_-]*audit|org(?:anization)?[\s_-]*audit|hot[\s-]*list|falcon camera|"
    r"motorola.*(?:plate|lpr)|rekor|autovu",
    re.I,
)
# Documents downloaded by default: audit logs, sharing lists, hot lists, and
# the tabular exports ALPR audits come as.
AUDIT_DOC_RE = re.compile(
    r"audit|network|shar(?:e|ing)|search|organi[sz]ation|hot[\s-]*list|flock|alpr|\blpr\b|"
    r"plate|query|queries|\blogs?\b|transparency",
    re.I,
)
TABULAR_EXT = {"xlsx", "xls", "xlsm", "csv", "tsv", "ods", "zip", "7z", "json", "txt"}
MEDIA_EXT = {"mp4", "mov", "avi", "wmv", "mp3", "wav", "m4a", "m4v", "mkv", "webm", "mpg", "mpeg"}
URL_RE = re.compile(r"https?://[^\s\"'<>\\)]+", re.I)


def _words(s: str | None) -> str:
    # File names join words with "_", which is a regex word character, so
    # r"\bflock\b" never matches "Flock_Safety_INV..." without this.
    return (s or "").replace("_", " ")


def is_alpr(*texts: str | None) -> bool:
    return any(ALPR_RE.search(_words(t)) for t in texts)


def alpr_context(text: str, width: int = 300) -> str:
    """Up to `width` chars of text around its first ALPR match (or its head)."""
    m = ALPR_RE.search(text)
    start = max(0, m.start() - width // 3) if m else 0
    return text[start:start + width]


def strip_html(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def safe_name(s: str, limit: int = 150) -> str:
    s = re.sub(r"[^\w.\- ()&,+#]+", "_", s).strip(" ._")
    return s[:limit] or "file"


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------------ transport


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Throttle:
    """One shared minimum interval per platform, plus a global pause on 429.
    Each 429 also widens the interval by 25% for the rest of the run, so a
    limit we underestimated stops being tripped instead of tripped repeatedly."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, pause: float = 0.0) -> None:
        with self._lock:
            now = time.time()
            if pause:
                self._next = max(self._next, now + pause)
                self.interval *= 1.25
            delay = self._next - now
            self._next = max(self._next, now) + self.interval
        if delay > 0:
            time.sleep(delay)


class Http:
    def __init__(self, throttle: Throttle, cookies: bool = False, follow: bool = False):
        handlers = []
        if cookies:
            handlers.append(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        if not follow:
            handlers.append(_NoRedirect())
        self.opener = urllib.request.build_opener(*handlers)
        self.throttle = throttle

    def request(self, url: str, data: bytes | None = None, headers: dict | None = None,
                timeout: int = 90):
        """Returns (status, final_url, headers, body). Redirects are returned, not
        followed, unless the Http was built with follow=True."""
        err = None
        for attempt in range(8):
            self.throttle.wait()
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
            try:
                with self.opener.open(req, timeout=timeout) as r:
                    return r.status, r.geturl(), r.headers, r.read()
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    return e.code, url, e.headers, b""
                if e.code == 429:
                    ra = int(e.headers.get("Retry-After") or 60) + 3
                    self.throttle.wait(pause=ra)
                    print(f"    429 from {urllib.parse.urlsplit(url).hostname}: pausing {ra}s, "
                          f"interval now {self.throttle.interval:.2f}s", file=sys.stderr, flush=True)
                    continue
                if e.code in (502, 503, 504):
                    time.sleep(5 * (attempt + 1))
                    continue
                return e.code, url, e.headers, e.read() if e.fp else b""
            except Exception as e:  # timeouts, resets
                err = e
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"giving up on {url}: {err!r}")

    def json(self, url: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        hdrs = {"Accept": "application/json"}
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        status, _, _, body = self.request(url, data=data, headers=hdrs)
        if status != 200:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None


def stream_to(url: str, dest: Path, headers: dict | None = None) -> tuple[str, int]:
    """Download url to dest (atomic rename); returns (sha256, bytes)."""
    tmp = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    n = 0
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=900) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
            h.update(chunk)
            n += len(chunk)
    tmp.rename(dest)
    return h.hexdigest(), n


# ------------------------------------------------------------------ platforms
#
# Each adapter yields request records shaped like:
#   {"id", "url", "matched", "request_date", "state", "departments",
#    "text_excerpt", "documents": [{"id", "title", "ext", "size", ...}],
#    "offsite_links"}
# and knows how to download one of its documents.


class NextRequest:
    platform = "nextrequest"
    # Cloudflare's rule on the /client API trips below 100 req/min per IP
    # (2 req/s drew a 429 every minute or so, 80/min every few minutes).
    throttle = Throttle(1.0)

    def __init__(self):
        self.http = Http(self.throttle)

    def _paged(self, host: str, path: str, key: str, max_pages: int = 200, **params):
        seen = 0
        for page in range(1, max_pages + 1):
            q = urllib.parse.urlencode({**params, "page_number": page})
            d = self.http.json(f"https://{host}{path}?{q}")
            if not d or not d.get(key):
                return
            yield from d[key]
            seen += len(d[key])
            if seen >= (d.get("total_count") or 0):
                return

    def search(self, host: str, rdir_for) -> list[dict]:
        # Relevance is decided from the hit itself (request text / doc title) so
        # only relevant requests cost the three detail calls.
        hits: dict[str, dict] = {}
        for term in SEARCH_TERMS:
            for r in self._paged(host, "/client/requests", "requests", search_term=term):
                h = hits.setdefault(r["id"], {"matched": set(), "relevant": False})
                h["matched"].add(f"request:{term}")
                h["relevant"] |= is_alpr(strip_html(r.get("request_text")))
        for term in NR_DOC_TERMS:
            for d in self._paged(host, "/client/documents", "documents", max_pages=10, search_term=term):
                rid = d.get("pretty_id")
                if not rid:
                    continue
                h = hits.setdefault(rid, {"matched": set(), "relevant": False})
                h["matched"].add(f"doc:{term}")
                # "flock"/"alpr" are specific enough to trust a body-text hit; the
                # generic audit terms need an ALPR word in the title (IT network audits).
                h["relevant"] |= term in ("flock", "alpr") or is_alpr(d.get("title"))
        out = []
        for rid, h in sorted(hits.items()):
            if not h["relevant"]:
                continue
            q = urllib.parse.quote(rid)
            detail = self.http.json(f"https://{host}/client/requests/{q}") or {}
            docs = self.http.json(f"https://{host}/client/request_documents?request_id={q}") or {}
            tl = self.http.json(f"https://{host}/client/requests/{q}/timeline") or {}
            rdir = rdir_for(rid)
            (rdir / "request.json").write_text(json.dumps(detail, indent=1))
            (rdir / "documents.json").write_text(json.dumps(docs, indent=1))
            (rdir / "timeline.json").write_text(json.dumps(tl, indent=1))
            links = sorted({u.rstrip(".,;") for ev in tl.get("timeline") or []
                            for u in URL_RE.findall(ev.get("timeline_display_text") or "")
                            if "nextrequest.com" not in u})
            out.append({
                "id": rid,
                "url": f"https://{host}/requests/{rid}",
                "matched": sorted(h["matched"]),
                "request_date": detail.get("request_date"),
                "state": detail.get("request_state"),
                "departments": detail.get("department_names"),
                "text_excerpt": strip_html(detail.get("request_text"))[:300],
                "documents": [{
                    "id": d["id"], "title": d.get("title"), "link": bool(d.get("link")),
                    "ext": (d.get("file_extension") or "").lower(), "size": None,
                    "uploaded": (d.get("document_scan") or {}).get("upload_date"),
                    "folder": d.get("folder_name") or None,
                } for d in docs.get("documents") or []],
                "offsite_links": links,
            })
        return out

    def download(self, host: str, req: dict, doc: dict, dest: Path) -> tuple[str, int]:
        status, _, headers, _ = self.http.request(f"https://{host}/documents/{doc['id']}/download")
        loc = headers.get("Location") if headers else None
        if status not in (301, 302, 303, 307) or not loc:
            raise RuntimeError(f"no download redirect (HTTP {status})")
        return stream_to(loc, dest)


class GovQA:
    """The public "Archive" of a GovQA portal (OpenRecordsSummary.aspx)."""

    platform = "govqa"
    throttle = Throttle(0.5)

    @staticmethod
    def _hidden(page: str) -> dict:
        return {k: html.unescape(v) for k, v in
                re.findall(r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]*value="([^"]*)"', page)}

    @staticmethod
    def grid_callback_param(command: str, arg: str) -> str:
        """DevExpress ASPxGridView callback parameter, e.g. PAGERONCLICK/PN1 ->
        'c0:GB|20;12|PAGERONCLICK3|PN1;' (length-prefixed command and argument)."""
        inner = f"{len(command)}|{command}{len(arg)}|{arg}"
        return f"c0:GB|{len(inner)};{inner};"

    @staticmethod
    def _rows(page: str) -> list[dict]:
        rows = []
        for tr in re.findall(r'<tr id="gridView_DXDataRow\d+".*?</tr>', page, re.S):
            cells = {k: html.unescape(v) for k, v in re.findall(r'aria-label="([^":]+): ([^"]*)"', tr)}
            rid = re.findall(r"redirectInfo\(&#39;(\d+)&#39;\)", tr)
            if cells.get("Request Number") and rid:
                rows.append({"number": cells["Request Number"], "rid": rid[0],
                             "date": cells.get("Create Date"), "status": cells.get("Request Status"),
                             "summary": cells.get("Summary", "")})
        return rows

    def _session(self, host: str):
        http_ = Http(self.throttle, cookies=True, follow=True)
        _, url, _, _ = http_.request(f"https://{host}/WEBAPP/_rs/supporthome.aspx")
        return http_, url.rsplit("/", 1)[0]

    def _post(self, http_: Http, url: str, form: dict) -> str:
        _, _, _, body = http_.request(url, data=urllib.parse.urlencode(form).encode(),
                                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        return body.decode("utf-8", "replace")

    def search(self, host: str, rdir_for) -> list[dict]:
        http_, base = self._session(host)
        summary_url = base + "/OpenRecordsSummary.aspx"
        _, _, _, body = http_.request(summary_url)
        start = body.decode("utf-8", "replace")
        if "txtSearch" not in start:
            return []
        hits: dict[str, dict] = {}
        for term in SEARCH_TERMS:
            form = self._hidden(start)
            form.update({"txtSearch": term, "filterButton": "Search"})
            page = self._post(http_, summary_url, form)
            m = re.search(r"Page \d+ of (\d+) \((\d+) items?\)", page)
            n_pages = int(m.group(1)) if m else 1
            rows = self._rows(page)
            for i in range(1, min(n_pages, 50)):
                form = self._hidden(page)
                form.update({"txtSearch": term, "__CALLBACKID": "gridView",
                             "__CALLBACKPARAM": self.grid_callback_param("PAGERONCLICK", f"PN{i}")})
                rows += self._rows(self._post(http_, summary_url, form))
            for r in rows:
                h = hits.setdefault(r["rid"], {**r, "matched": set()})
                h["matched"].add(f"request:{term}")
        out = []
        for rid, h in sorted(hits.items()):
            # The grid's "Summary" is often a short title ("Contract, budget
            # documentation.") while the portal's search matched the full request
            # text, so relevance is judged on the detail page.
            detail_url = f"{base}/RequestArchiveDetails.aspx?rid={rid}"
            _, _, _, body = http_.request(detail_url)
            page = body.decode("utf-8", "replace")
            page_text = strip_html(page)
            specific = any(m.split(":", 1)[1] in SPECIFIC_TERMS for m in h["matched"])
            if not (specific or is_alpr(h["summary"]) or is_alpr(page_text)):
                continue
            docs = []
            for target, name in re.findall(
                    r"__doPostBack\(&#39;(rptAttachments\$ctl\d+\$lnkStreamCloud)&#39;.*?>(.*?)</a>", page, re.S):
                title = strip_html(name)
                docs.append({"id": target, "title": title,
                             "ext": title.rsplit(".", 1)[-1].lower() if "." in title else "", "size": None})
            rdir = rdir_for(h["number"])
            (rdir / "row.json").write_text(json.dumps({k: v for k, v in h.items() if k != "matched"}, indent=1))
            (rdir / "documents.json").write_text(json.dumps(docs, indent=1))
            out.append({
                "id": h["number"], "rid": rid,
                "url": f"https://{host}/WEBAPP/_rs/RequestArchiveDetails.aspx?rid={rid}",
                "matched": sorted(h["matched"]), "request_date": h["date"], "state": h["status"],
                "departments": None, "summary": strip_html(h["summary"])[:200],
                "text_excerpt": alpr_context(page_text),
                "documents": docs, "offsite_links": sorted(set(URL_RE.findall(h["summary"]))),
            })
        return out

    def download(self, host: str, req: dict, doc: dict, dest: Path) -> tuple[str, int]:
        http_, base = self._session(host)
        detail_url = f"{base}/RequestArchiveDetails.aspx?rid={req['rid']}"
        _, _, _, body = http_.request(detail_url)
        form = self._hidden(body.decode("utf-8", "replace"))
        form.update({"__EVENTTARGET": doc["id"], "__EVENTARGUMENT": ""})
        # The postback answers with a redirect to a short-lived Azure blob SAS URL.
        nofollow = Http(self.throttle)
        nofollow.opener = urllib.request.build_opener(
            *[h for h in http_.opener.handlers if isinstance(h, urllib.request.HTTPCookieProcessor)],
            _NoRedirect())
        status, _, headers, _ = nofollow.request(
            detail_url, data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        loc = headers.get("Location") if headers else None
        if status not in (301, 302, 303) or not loc:
            raise RuntimeError(f"no blob redirect (HTTP {status})")
        return stream_to(urllib.parse.urljoin(detail_url, loc), dest)


class JustFOIA:
    platform = "justfoia"
    throttle = Throttle(0.5)

    def __init__(self):
        self.http = Http(self.throttle)

    def search(self, host: str, rdir_for) -> list[dict]:
        hits: dict[str, dict] = {}
        for term in SEARCH_TERMS:
            for page in range(1, 50):
                d = self.http.json(f"https://{host}/publicportal/api/Search", {
                    "searchText": term, "languageCode": "en", "page": page, "pageSize": 100,
                    "sortBy": "requestDate", "descending": True})
                res = (d or {}).get("searchResults") or []
                for r in res:
                    h = hits.setdefault(r["requestName"], {**r, "matched": set()})
                    h["matched"].add(f"request:{term}")
                if len(res) < 100:
                    break
        out = []
        for num, h in sorted(hits.items()):
            if not is_alpr(h.get("description")):
                continue
            q = urllib.parse.quote(num)
            resp = self.http.json(f"https://{host}/publicportal/api/Request?fullRequestNumber={q}") or {}
            detail = resp.get("requestResults") or {}
            rdir = rdir_for(num)
            (rdir / "request.json").write_text(json.dumps(resp, indent=1))
            docs = [{"id": a["attachmentId"], "title": a.get("attachmentName"),
                     "ext": (a.get("extension") or "").lstrip(".").lower(), "size": a.get("fileSize"),
                     "uploaded": a.get("uploadDatetime")}
                    for a in detail.get("responseDocAttachments") or [] if not a.get("isNote")]
            out.append({
                "id": num, "url": f"https://{host}/publicportal/requests/{q}/view",
                "matched": sorted(h["matched"]), "request_date": (h.get("dateOfRequest") or "")[:10],
                "state": detail.get("status"), "departments": None,
                "text_excerpt": strip_html(h.get("description"))[:300],
                "documents": docs, "offsite_links": sorted(set(URL_RE.findall(h.get("description") or ""))),
            })
        return out

    def download(self, host: str, req: dict, doc: dict, dest: Path) -> tuple[str, int]:
        status, _, headers, _ = self.http.request(f"https://{host}/Attachments/Download/{doc['id']}")
        loc = headers.get("Location") if headers else None
        if status in (301, 302, 303, 307) and loc:
            return stream_to(urllib.parse.urljoin(f"https://{host}/", loc), dest)
        if status == 200:
            return stream_to(f"https://{host}/Attachments/Download/{doc['id']}", dest)
        raise RuntimeError(f"HTTP {status}")


ADAPTERS = {a.platform: a for a in (NextRequest, GovQA, JustFOIA)}


# ------------------------------------------------------------------- commands


def load_portals(path: Path, platforms: list[str] | None, hosts: list[str] | None) -> list[dict]:
    portals = json.loads(path.read_text())["portals"]
    return [p for p in portals
            if p.get("searchable")
            and (not platforms or p["platform"] in platforms)
            and (not hosts or p["host"] in hosts)]


def search_one(p: dict, out: Path, refresh: bool) -> str | None:
    pdir = out / p["platform"] / p["host"]
    idx_path = pdir / "index.json"
    if idx_path.exists() and not refresh:
        return None
    adapter = ADAPTERS[p["platform"]]()

    def rdir_for(rid):
        d = pdir / safe_name(rid)
        d.mkdir(parents=True, exist_ok=True)
        return d

    t0 = time.time()
    try:
        reqs = adapter.search(p["host"], rdir_for)
    except Exception as e:
        return f"{p['platform']} {p['host']}: FAILED {e!r}"
    pdir.mkdir(parents=True, exist_ok=True)
    tmp = idx_path.with_name("index.json.tmp")  # atomic: fetch may be reading it
    tmp.write_text(json.dumps({
        "platform": p["platform"], "host": p["host"], "agency": p.get("agency"), "label": p.get("label"),
        "searched_at": now_utc(), "terms": SEARCH_TERMS, "requests": reqs}, indent=1))
    tmp.rename(idx_path)
    n_docs = sum(len(r["documents"]) for r in reqs)
    return (f"{p['platform']} {p['host']}: {len(reqs)} ALPR requests, {n_docs} docs "
            f"({time.time() - t0:.0f}s)")


def cmd_search(args) -> None:
    out = Path(args.out)
    portals = load_portals(Path(args.portals), args.platform, args.host)
    # Portals run in parallel; each platform's Throttle is shared across
    # threads, so this only hides round-trip latency, not the request rate.
    with ThreadPoolExecutor(args.workers) as ex:
        futs = [ex.submit(search_one, p, out, args.refresh) for p in portals]
        for i, f in enumerate(as_completed(futs), 1):
            msg = f.result()
            if msg:
                print(f"[{i}/{len(portals)}] {msg}", flush=True)


def is_link(doc: dict) -> bool:
    """A NextRequest "document" that is only a pointer to an outside URL (its
    title is the URL); /download tries to follow it, so there's no file to get."""
    return bool(doc.get("link")) or bool(re.match(r"https?://", doc.get("title") or ""))


def wanted(doc: dict, everything: bool, max_mb: float | None) -> bool:
    if is_link(doc):
        return False
    ext = doc.get("ext") or ""
    if ext in MEDIA_EXT:
        return False
    if max_mb and doc.get("size") and doc["size"] > max_mb * 1e6:
        return False
    return everything or ext in TABULAR_EXT or bool(AUDIT_DOC_RE.search(_words(doc.get("title"))))


def doc_prefix(doc: dict) -> str:
    """Short, stable per-request file prefix: NextRequest's numeric id, GovQA's
    attachment slot (ctl00, ctl01, ...), or the head of JustFOIA's GUID."""
    s = str(doc["id"])
    slot = re.search(r"ctl\d+", s)
    return slot.group(0) if slot else s[:12]


def cmd_fetch(args) -> None:
    out = Path(args.out)
    manifest = out / "MANIFEST.jsonl"
    done = set()
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            m = json.loads(line)
            done.add((m["host"], m["request_id"], str(m["doc_id"])))
    budget = args.max_gb * (1 << 30) if args.max_gb else None
    fetched = 0
    with open(manifest, "a") as mf:
        for idx_path in sorted(out.glob("*/*/index.json")):
            idx = json.loads(idx_path.read_text())
            host, platform = idx["host"], idx["platform"]
            if (args.host and host not in args.host) or (args.platform and platform not in args.platform):
                continue
            adapter = ADAPTERS[platform]()
            for r in idx["requests"]:
                if args.request and r["id"] not in args.request:
                    continue
                for d in r["documents"]:
                    if (host, r["id"], str(d["id"])) in done or not wanted(d, args.all, args.max_file_mb):
                        continue
                    fdir = idx_path.parent / safe_name(r["id"]) / "files"
                    fdir.mkdir(parents=True, exist_ok=True)
                    dest = fdir / safe_name(f"{doc_prefix(d)}__{d['title'] or d['id']}")
                    try:
                        sha, n = adapter.download(host, r, d, dest)
                    except Exception as e:
                        print(f"  FAIL {host} {r['id']} {d['title']}: {e}", flush=True)
                        continue
                    mf.write(json.dumps({
                        "platform": platform, "host": host, "request_id": r["id"], "doc_id": d["id"],
                        "title": d["title"], "path": str(dest.relative_to(out)), "bytes": n,
                        "sha256": sha, "fetched_at": now_utc()}) + "\n")
                    mf.flush()
                    fetched += n
                    print(f"  {host} {r['id']} {d['title']} ({n / 1e6:.1f} MB)", flush=True)
                    if budget and fetched > budget:
                        print(f"stopping: --max-gb {args.max_gb} reached", flush=True)
                        return


def cmd_report(args) -> None:
    out = Path(args.out)
    fetched = {}
    if (out / "MANIFEST.jsonl").exists():
        for line in (out / "MANIFEST.jsonl").read_text().splitlines():
            m = json.loads(line)
            fetched.setdefault(m["host"], []).append(m)
    rows = []
    for idx_path in sorted(out.glob("*/*/index.json")):
        idx = json.loads(idx_path.read_text())
        if not idx["requests"]:
            continue
        audit = sum(1 for r in idx["requests"] for d in r["documents"]
                    if re.search(r"audit", d.get("title") or "", re.I))
        f = fetched.get(idx["host"], [])
        rows.append((idx["platform"], idx["host"], idx.get("agency") or "", len(idx["requests"]),
                     sum(len(r["documents"]) for r in idx["requests"]), audit, len(f),
                     sum(m["bytes"] for m in f) / 1e6))
    print(f"{'platform':11} {'host':42} {'agency':28} {'reqs':>5} {'docs':>5} {'audit':>5} {'got':>4} {'MB':>8}")
    for r in sorted(rows, key=lambda r: -r[5]):
        print(f"{r[0]:11} {r[1][:42]:42} {r[2][:28]:28} {r[3]:5} {r[4]:5} {r[5]:5} {r[6]:4} {r[7]:8.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="index ALPR-related requests on each portal")
    s.add_argument("--portals", default=str(DEFAULT_PORTALS))
    s.add_argument("--platform", action="append", choices=sorted(ADAPTERS))
    s.add_argument("--host", action="append", help="limit to these portal hosts")
    s.add_argument("--refresh", action="store_true", help="re-search portals already indexed")
    s.add_argument("--workers", type=int, default=4, help="portals searched in parallel")
    s.set_defaults(func=cmd_search)
    f = sub.add_parser("fetch", help="download documents from indexed requests")
    f.add_argument("--platform", action="append", choices=sorted(ADAPTERS))
    f.add_argument("--host", action="append")
    f.add_argument("--request", action="append", help="limit to these request ids")
    f.add_argument("--all", action="store_true", help="every non-media document, not just audit-like ones")
    f.add_argument("--max-gb", type=float, help="stop after this many GB this run")
    f.add_argument("--max-file-mb", type=float, help="skip documents whose listed size exceeds this")
    f.set_defaults(func=cmd_fetch)
    r = sub.add_parser("report", help="summarize indexed and fetched requests")
    r.set_defaults(func=cmd_report)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
