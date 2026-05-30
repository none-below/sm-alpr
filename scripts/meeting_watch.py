#!/usr/bin/env python3
"""meeting_watch.py — flag/log city-council meetings that mention ALPR/Flock.

A forward-looking "watcher" over the agenda portals mapped in
assets/meeting_portals.json. For each government it pulls upcoming + recent
meetings from the portal's feed, checks the agenda text for ALPR/Flock
keywords, and writes one JSON finding per flagged meeting under
assets/meetings/<gov_id>/<platform>-<meeting_id>.json.

Why a watcher: California's Brown Act (Gov. Code 54954.2) requires a regular
meeting's agenda — the document that would name "ALPR" — to be posted at least
72 hours ahead (24h for special meetings). So polling these feeds daily catches
an ALPR agenda item ~3 days before the vote, with enough lead to act. Past
meetings are kept as a durable record.

Findings carry meeting_date / meeting_datetime so a viewer can show "upcoming"
and "past N days" windows (see the `report` mode).

PoC scope: the three JSON-API platforms (primegov, legistar, civicclerk) =
10 of the 21 San Mateo County governments in the portal map. The RSS and
PDF/OCR platforms are mapped there too and get their own adapters later.

Security: raw agenda HTML/PDF is fetched in-memory only and never written to
disk — we persist just the structured finding (matched term + short snippet +
link), mirroring the article_registry "safe parsed view" convention. Matching
is plain regex (no model reads the raw agenda), so injection in scraped agenda
text can't act on anything.

Usage:
  python3 scripts/meeting_watch.py                      # watch: fetch, write findings, print report
  python3 scripts/meeting_watch.py --past-days 30       # narrow the past window
  python3 scripts/meeting_watch.py --platforms legistar # one platform
  python3 scripts/meeting_watch.py --gov san-mateo      # one government (id substring)
  python3 scripts/meeting_watch.py --dry-run            # detect + report, write nothing
  python3 scripts/meeting_watch.py report --past-days 90  # just print existing findings, no fetch
"""

from datetime import datetime, timezone, timedelta, date
from html import unescape
from pathlib import Path
from urllib.parse import quote, urljoin
import argparse
import json
import random
import re
import subprocess
import sys
import time

import requests

try:
    import feedparser
except ImportError:
    feedparser = None

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

PORTALS_FILE = Path("assets/meeting_portals.json")
MEETINGS_DIR = Path("assets/meetings")
JSON_PLATFORMS = {"primegov", "legistar", "civicclerk"}

# America/Los_Angeles for all San Mateo County governments.
try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    PACIFIC = timezone(timedelta(hours=-8))

# Unambiguous terms — always count when present.
STRONG_PATTERNS = [
    ("flock safety",                re.compile(r"flock\s+safety", re.I)),
    ("alpr",                        re.compile(r"\bALPRs?\b|\bA\.L\.P\.R\.?", re.I)),
    ("automated license plate",     re.compile(r"automat(?:ed|ic)\s+license[\s-]+plate", re.I)),
    ("license plate reader",        re.compile(r"license[\s-]+plate\s+readers?", re.I)),
    ("license plate recognition",   re.compile(r"license[\s-]+plate\s+recognition", re.I)),
    ("vigilant solutions",          re.compile(r"vigilant\s+solutions", re.I)),
]
# Ambiguous tokens — only count if a surveillance-context word sits within
# CONTEXT_WINDOW chars. Stops 'Little Flock Church' and a library 'LPR room'
# from registering as ALPR hits.
CONTEXTUAL_PATTERNS = [
    ("flock", re.compile(r"\bFlock\b|\bFLOCK\b")),
    ("lpr",   re.compile(r"\bLPR\b")),
]
CONTEXT_RE = re.compile(
    r"camera|surveillanc|license\s+plate|automat|\breaders?\b|police|public\s+safety|"
    r"\bvehicle|privacy|data\s+shar|motorola|vigilant|\bpd\b|law\s+enforcement",
    re.I,
)
CONTEXT_WINDOW = 80
MAX_MATCHES = 6
SNIPPET_PAD = 90


# ═══════════════════════════════════════════════════════════
# HTTP
# ═══════════════════════════════════════════════════════════

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


def _get(url, accept=None, timeout=30, retries=3):
    headers = {"Accept": accept} if accept else {}
    for attempt in range(retries):
        try:
            r = SESSION.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return None


def get_json(url):
    r = _get(url, accept="application/json")
    if not r:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def get_text(url):
    r = _get(url)
    return r.text if r else None


def _deunderscore_s3(url):
    """Granicus 302-redirects agenda docs to virtual-hosted S3 buckets whose
    names contain underscores (e.g. granicus_production_attachments.s3.amazonaws.com),
    which urllib3 rejects as invalid hostnames. Rewrite to path-style on the
    valid s3.amazonaws.com host so requests can fetch them (curl tolerates it)."""
    m = re.match(r"(https?)://([^/]*_[^/]*)\.s3(?:[.-][^/.]+)?\.amazonaws\.com/(.+)", url)
    return f"{m.group(1)}://s3.amazonaws.com/{m.group(2)}/{m.group(3)}" if m else url


def get_doc(url, max_bytes=30_000_000):
    """Fetch a binary doc. Follows redirects manually so we can rewrite
    underscore-bucket S3 redirects (Granicus), and streams with a hard size cap
    (PrimeGov 'Packet' PDFs run 90-140 MB; we only ever want the ~2 MB 'Agenda')."""
    for _ in range(8):
        url = _deunderscore_s3(url)
        try:
            r = SESSION.get(url, timeout=90, stream=True, allow_redirects=False)
        except requests.RequestException:
            time.sleep(2)
            continue
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location", "")
            r.close()
            if not loc:
                return "", b""
            url = loc if loc.startswith("http") else urljoin(url, loc)
            continue
        if r.status_code == 200:
            cl = r.headers.get("Content-Length")
            ct = r.headers.get("Content-Type", "")
            if cl and int(cl) > max_bytes:
                r.close()
                return ct, b""
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > max_bytes:
                    break
            r.close()
            return ct, bytes(buf)
        if r.status_code in (429, 500, 502, 503, 504):
            r.close()
            time.sleep(2)
            continue
        r.close()
        return "", b""
    return "", b""


def polite():
    time.sleep(random.uniform(0.4, 0.9))


# ═══════════════════════════════════════════════════════════
# Text helpers
# ═══════════════════════════════════════════════════════════

def strip_html(s):
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p>\s*<p[^>]*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    return s


def pdf_to_text(raw):
    if not raw:
        return ""
    try:
        p = subprocess.run(
            ["pdftotext", "-q", "-", "-"],
            input=raw, stdout=subprocess.PIPE, timeout=90,
        )
        return p.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""


def _snippet(text, m):
    start = max(0, m.start() - SNIPPET_PAD)
    end = min(len(text), m.end() + SNIPPET_PAD)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def find_matches(text):
    """Return up to MAX_MATCHES distinct {term, snippet} hits. Strong terms
    always count; ambiguous ones only with a nearby surveillance context word."""
    out, seen = [], set()
    for label, pat in STRONG_PATTERNS:
        m = pat.search(text)
        if m and label not in seen:
            seen.add(label)
            out.append({"term": label, "snippet": _snippet(text, m)})
    for label, pat in CONTEXTUAL_PATTERNS:
        if label in seen:
            continue
        for m in pat.finditer(text):
            lo = max(0, m.start() - CONTEXT_WINDOW)
            hi = min(len(text), m.end() + CONTEXT_WINDOW)
            if CONTEXT_RE.search(text[lo:hi]):
                seen.add(label)
                out.append({"term": label, "snippet": _snippet(text, m)})
                break
    return out[:MAX_MATCHES]


def parse_naive_pacific(s):
    """PrimeGov 'YYYY-MM-DDTHH:MM:SS' (naive local) -> aware Pacific datetime."""
    if not s:
        return None
    s = s.strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=PACIFIC)
        except ValueError:
            continue
    return None


def parse_legistar_dt(date_s, time_s):
    """Legistar EventDate (date) + EventTime ('7:00 PM' string) -> aware Pacific."""
    if not date_s:
        return None
    try:
        d = datetime.strptime(date_s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    t = None
    if time_s:
        for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
            try:
                t = datetime.strptime(time_s.strip().upper(), fmt.upper()).time()
                break
            except ValueError:
                continue
    dt = datetime.combine(d, t) if t else datetime(d.year, d.month, d.day)
    return dt.replace(tzinfo=PACIFIC)


# ═══════════════════════════════════════════════════════════
# Platform adapters — each returns a list of normalized meetings:
#   {meeting_id, body, meeting_date (YYYY-MM-DD), meeting_datetime (ISO+tz),
#    agenda_url, agenda_text}
# ═══════════════════════════════════════════════════════════

def primegov_meetings(portal, since_date):
    client = portal["client"]
    base = f"https://{client}.primegov.com"
    api = f"{base}/api/v2/PublicPortal"
    raw = list(get_json(f"{api}/ListUpcomingMeetings") or [])
    for year in sorted({since_date.year, date.today().year}):
        raw += list(get_json(f"{api}/ListArchivedMeetings?year={year}") or [])

    out, seen = [], set()
    for m in raw:
        mid = m.get("id")
        if mid in seen:
            continue
        seen.add(mid)
        dt = parse_naive_pacific(m.get("dateTime") or m.get("date"))
        if dt is None or dt.date() < since_date:
            continue
        agenda_url, text = _primegov_agenda(base, m)
        out.append({
            "meeting_id": mid,
            "body": (m.get("title") or "").strip(),
            "meeting_date": dt.date().isoformat(),
            "meeting_datetime": dt.isoformat(),
            "agenda_url": agenda_url or base,
            "agenda_text": text,
        })
        if text:
            polite()
    return out


def _primegov_agenda(base, meeting):
    """Return (agenda_url, text). PrimeGov keeps the lightweight 'HTML Agenda'
    only for recent/upcoming meetings; for archived ones it returns a
    'Document Not Found' stub, so we fall through to the durable 'Agenda' PDF.
    The 'Packet' is never fetched (it's 90-140 MB)."""
    docs = meeting.get("documentList") or []
    doc = next((d for d in docs if d.get("templateName") == "HTML Agenda"), None)
    if doc:
        url = f"{base}/Public/CompiledDocument?id={doc['id']}"
        body = get_text(url)
        if body:
            text = strip_html(body)
            if len(text) > 400 and "Document Not Found" not in text:
                return url, text
    doc = next((d for d in docs if d.get("templateName") == "Agenda"), None)
    if doc:
        url = f"{base}/Public/CompiledDocument?id={doc['id']}"
        ct, raw = get_doc(url)
        if "pdf" in ct.lower():
            return url, pdf_to_text(raw)
        if raw:
            return url, strip_html(raw.decode("utf-8", "ignore"))
    return None, ""


def legistar_meetings(portal, since_date):
    client = portal["client"]
    api = f"https://webapi.legistar.com/v1/{client}"
    filt = quote(f"EventDate ge datetime'{since_date.isoformat()}'")
    events = get_json(f"{api}/events?$filter={filt}&$orderby=EventDate") or []
    out = []
    for e in events:
        eid = e.get("EventId")
        dt = parse_legistar_dt(e.get("EventDate"), e.get("EventTime"))
        if dt is None or dt.date() < since_date:
            continue
        items = get_json(f"{api}/events/{eid}/eventitems") or []
        parts = []
        for it in items:
            for key in ("EventItemTitle", "EventItemMatterName"):
                val = it.get(key)
                if val:
                    parts.append(str(val))
        out.append({
            "meeting_id": eid,
            "body": (e.get("EventBodyName") or "").strip(),
            "meeting_date": dt.date().isoformat(),
            "meeting_datetime": dt.isoformat(),
            "agenda_url": e.get("EventInSiteURL") or f"https://{client}.legistar.com/Calendar.aspx",
            "agenda_text": "\n".join(parts),
        })
        polite()
    return out


def civicclerk_meetings(portal, since_date):
    client = portal["client"]
    api = f"https://{client}.api.civicclerk.com/v1"
    portal_url = f"https://{client}.portal.civicclerk.com"
    filt = quote(f"startDateTime ge {since_date.isoformat()}T00:00:00Z")
    data = get_json(f"{api}/Events?$filter={filt}&$orderby=startDateTime") or {}
    events = data.get("value") if isinstance(data, dict) else data
    out = []
    for e in events or []:
        eid = e.get("id")
        ds = (e.get("startDateTime") or e.get("eventDate") or "")[:19]
        if len(ds) < 10:
            continue
        date_part = ds[:10]
        if date_part < since_date.isoformat():
            continue
        # CivicClerk stamps local wall-clock with a Z; treat as Pacific local.
        dt = parse_naive_pacific(ds)
        text = _civicclerk_agenda(api, e)
        out.append({
            "meeting_id": eid,
            "body": (e.get("eventName") or "").strip(),
            "meeting_date": date_part,
            "meeting_datetime": dt.isoformat() if dt else None,
            "agenda_url": portal_url,
            "agenda_text": text,
        })
        if text:
            polite()
    return out


def _civicclerk_agenda(api, event):
    files = event.get("publishedFiles") or []
    fid = next((f.get("id") or f.get("fileId") for f in files
                if (f.get("type") or "").lower() == "agenda"), None)
    if fid is None and files:
        fid = files[0].get("id") or files[0].get("fileId")
    if fid is None:
        return ""
    txt = get_text(f"{api}/Meetings/GetMeetingFileStream(fileId={fid},plainText=true)")
    if txt and txt.strip():
        return txt
    ct, raw = get_doc(f"{api}/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)")
    return pdf_to_text(raw) if raw else ""


def _granicus_id(link, d):
    m = re.search(r"(?:clip_id|event_id)=(\d+)", link or "")
    return m.group(1) if m else d.isoformat()


def granicus_meetings(portal, since_date):
    """Granicus ViewPublisher RSS carries ALL of a city's bodies in one feed;
    each item is '{Body} - {Date}' with an AgendaViewer link (usually a PDF).
    One fetch catches e.g. Belmont's Public Safety Committee (an ALPR oversight
    body) alongside the Council. Needs portal['view_id']."""
    host, view_id = portal.get("client"), portal.get("view_id")
    if not host or not view_id or feedparser is None:
        return []
    raw = get_text(f"https://{host}.granicus.com/ViewPublisherRSS.php?view_id={view_id}&mode=agendas")
    if not raw:
        return []
    out = []
    DATE_SUFFIX = r"\s*-\s*([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s*$"
    for e in feedparser.parse(raw).entries:
        title = e.get("title", "")
        # Prefer the meeting date in the title ('... - Feb 24, 2025'); the RSS
        # pubDate is when the agenda was posted, which can differ by days.
        d = None
        dm = re.search(DATE_SUFFIX, title)
        if dm:
            try:
                d = datetime.strptime(dm.group(1), "%b %d, %Y").date()
            except ValueError:
                d = None
        if d is None:
            tm = e.get("published_parsed")
            if not tm:
                continue
            d = date(tm.tm_year, tm.tm_mon, tm.tm_mday)
        if d < since_date:
            continue
        body = re.sub(DATE_SUFFIX, "", title).strip() or title
        link = e.get("link", "")
        text = ""
        if link:
            ct, doc = get_doc(link)
            text = pdf_to_text(doc) if "pdf" in ct.lower() else (strip_html(doc.decode("utf-8", "ignore")) if doc else "")
        out.append({
            "meeting_id": _granicus_id(link, d),
            "body": body,
            "meeting_date": d.isoformat(),
            "meeting_datetime": datetime(d.year, d.month, d.day, tzinfo=PACIFIC).isoformat(),
            "agenda_url": link,
            "agenda_text": text,
        })
        if text:
            polite()
    return out


def _playwright_html(url):
    """Render a WAF-protected listing page with headless Chromium (the agenda
    LISTING for sites like redwoodcity.org that 403 plain requests)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        from playwright_stealth import stealth_sync
    except Exception:
        stealth_sync = None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_context(user_agent=USER_AGENT).new_page()
            if stealth_sync:
                try:
                    stealth_sync(pg)
                except Exception:
                    pass
            try:
                pg.goto(url, wait_until="networkidle", timeout=60000)
            except Exception:
                pass
            pg.wait_for_timeout(2500)
            html = pg.content()
            b.close()
            return html
    except Exception:
        return None


def selfhosted_pdf_meetings(portal, since_date):
    """Config-driven scraper for self-hosted agenda-PDF sites — e.g. the Redwood
    City Police Advisory Committee, whose agendas are off the main portal on a
    separate host. Reads `listing_urls` (via Playwright when WAF-blocked),
    extracts agenda PDF links, derives the meeting date from the filename
    (`date_regex`), and reads each PDF. `pdf_force_https` rewrites http→https
    for hosts that only answer on 443."""
    listings = portal.get("listing_urls") or ([portal["listing_url"]] if portal.get("listing_url") else [])
    if not listings:
        return []
    link_text = portal.get("link_text", "agenda")
    date_rx = re.compile(portal["date_regex"]) if portal.get("date_regex") else None
    out, seen = [], set()
    for listing in listings:
        html = _playwright_html(listing) if portal.get("listing_fetch") == "playwright" else get_text(listing)
        if not html:
            continue
        for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
            href, text = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
            if ".pdf" not in href.lower() or not re.search(link_text, text, re.I):
                continue
            url = href if href.startswith("http") else portal.get("base_url", "") + href
            if portal.get("pdf_force_https"):
                url = url.replace("http://", "https://")
            if url in seen:
                continue
            seen.add(url)
            d = None
            if date_rx:
                mm = date_rx.search(url)
                if mm:
                    s = mm.group(1)
                    try:
                        d = date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
                    except ValueError:
                        d = None
            if d and d < since_date:
                continue
            ct, doc = get_doc(url)
            doc_text = pdf_to_text(doc) if "pdf" in ct.lower() else (strip_html(doc.decode("utf-8", "ignore")) if doc else "")
            out.append({
                "meeting_id": re.sub(r"\.pdf.*$", "", url.rsplit("/", 1)[-1]),
                "body": portal.get("body", ""),
                "meeting_date": d.isoformat() if d else "",
                "meeting_datetime": datetime(d.year, d.month, d.day, tzinfo=PACIFIC).isoformat() if d else None,
                "agenda_url": url,
                "agenda_text": doc_text,
            })
            polite()
    return out


ADAPTERS = {
    "primegov": primegov_meetings,
    "legistar": legistar_meetings,
    "civicclerk": civicclerk_meetings,
    "granicus-viewpublisher": granicus_meetings,
    "selfhosted-webapps": selfhosted_pdf_meetings,
    "selfhosted-pdf": selfhosted_pdf_meetings,
}


# ═══════════════════════════════════════════════════════════
# Findings I/O
# ═══════════════════════════════════════════════════════════

def finding_path(gov_id, platform, meeting_id):
    return MEETINGS_DIR / gov_id / f"{platform}-{meeting_id}.json"


def write_finding(portal, meeting, matches, source, dry_run):
    path = finding_path(portal["id"], portal["platform"], meeting["meeting_id"])
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    first_seen = now_iso
    if path.exists():
        try:
            first_seen = json.loads(path.read_text()).get("first_seen", now_iso)
        except Exception:
            pass
    rec = {
        "gov_id": portal["id"],
        "gov_name": portal["city"],
        "state": portal.get("state"),
        "county": portal.get("county"),
        "platform": portal["platform"],
        "meeting_id": str(meeting["meeting_id"]),
        "body": meeting["body"],
        "meeting_date": meeting["meeting_date"],
        "meeting_datetime": meeting["meeting_datetime"],
        "agenda_url": meeting["agenda_url"],
        "matched_terms": [m["term"] for m in matches],
        "matches": matches,
        "source": source,
        "first_seen": first_seen,
        "last_checked": now_iso,
    }
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, indent=2) + "\n")
    return rec


def load_findings():
    if not MEETINGS_DIR.exists():
        return []
    out = []
    for p in sorted(MEETINGS_DIR.glob("*/*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            pass
    return out


# ═══════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════

def print_report(findings, past_days):
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=past_days)).isoformat()
    upcoming = sorted((f for f in findings if f["meeting_date"] >= today),
                      key=lambda f: f["meeting_date"])
    past = sorted((f for f in findings if cutoff <= f["meeting_date"] < today),
                  key=lambda f: f["meeting_date"], reverse=True)

    def row(f):
        terms = ", ".join(f.get("matched_terms") or [])
        return f"  {f['meeting_date']}  {f['gov_name']:<22} {f['body'][:42]:<42} [{terms}]"

    print(f"\n┏━ UPCOMING flagged meetings ({len(upcoming)}) "
          f"— act before the vote ━━━━━━━━━━━━━━━━")
    print("\n".join(row(f) for f in upcoming) if upcoming else "  (none)")
    print(f"\n┏━ PAST {past_days}d flagged meetings ({len(past)}) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("\n".join(row(f) for f in past) if past else "  (none)")
    print()


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════

def load_portals():
    data = json.loads(PORTALS_FILE.read_text())
    return data.get("portals", [])


def run_watch(args):
    since_date = date.today() - timedelta(days=args.past_days)
    enabled = set(args.platforms.split(",")) if args.platforms else set(ADAPTERS)
    portals = [p for p in load_portals()
               if p.get("id") and p.get("platform") in enabled and p["platform"] in ADAPTERS
               and (not args.gov or args.gov in p["id"])]

    print(f"Watching {len(portals)} government/body(s) | platforms={sorted(enabled & set(ADAPTERS))} "
          f"| window: {since_date.isoformat()} → future"
          + ("  [DRY RUN]" if args.dry_run else ""))

    findings, hits, scanned = [], 0, 0
    for p in portals:
        adapter = ADAPTERS.get(p["platform"])
        if not adapter:
            continue
        try:
            meetings = adapter(p, since_date)
        except Exception as e:
            print(f"  ! {p['id']}: adapter error: {e}")
            continue
        flagged_here = 0
        for m in meetings:
            scanned += 1
            matches = find_matches(m["agenda_text"]) if m["agenda_text"] else []
            if not matches:
                continue
            rec = write_finding(p, m, matches, f"{p['platform']}:watch", args.dry_run)
            findings.append(rec)
            hits += 1
            flagged_here += 1
        print(f"  {p['id']:<24} {len(meetings):>3} meetings in window, {flagged_here} flagged")

    print(f"\nScanned {scanned} meetings across {len(portals)} governments → {hits} flagged.")
    # Report uses freshly written findings plus anything already on disk.
    all_findings = load_findings() if not args.dry_run else findings
    print_report(all_findings, args.past_days)


def run_report(args):
    print_report(load_findings(), args.past_days)


def main():
    parser = argparse.ArgumentParser(description="Flag/log council meetings mentioning ALPR/Flock.")
    parser.add_argument("mode", nargs="?", default="watch", choices=["watch", "report"],
                        help="watch = fetch + detect + write findings (default); report = print existing findings")
    parser.add_argument("--past-days", type=int, default=90, help="how many days back to include (default 90)")
    parser.add_argument("--platforms", default="", help="comma list to limit (primegov,legistar,civicclerk)")
    parser.add_argument("--gov", default="", help="limit to gov ids containing this substring")
    parser.add_argument("--dry-run", action="store_true", help="detect + report but write no files")
    args = parser.parse_args()

    if not PORTALS_FILE.exists():
        sys.exit(f"missing {PORTALS_FILE} — run from the repo root")

    if args.mode == "report":
        run_report(args)
    else:
        run_watch(args)


if __name__ == "__main__":
    main()
