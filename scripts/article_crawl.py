#!/usr/bin/env python3
"""Drain the article queue and fetch URLs, slow-staged like slug_probe.

Mirrors scripts/slug_probe.py: bounded N per run, jittered delay between
fetches, state file to dedupe across runs, fail-list with attempt cap,
designed to run from an hourly cron on its own rate budget.

Per article fetched:
  1. HTTP GET with project UA, follow redirects, 30s timeout.
  2. Save raw HTML to assets/articles/<source_domain>/<slug>_<hash>.html.
  3. Extract plain text via stdlib html.parser; save .txt sidecar.
     (Quality is intentionally rough — better extraction is a curation-time
     concern. See _extract_text below for the upgrade path.)
  4. Run scripts/check_injection.py on both .html and .txt; record verdict.
  5. Write .meta.json sidecar with everything machine-knowable
     (url, http_status, content_hash, scanner_score, http_title, fetched_at).
  6. Mark URL as fetched in .crawl_state.json; remove from queue file.

Files raw .html and .txt are blocked from Claude reads by
scripts/check_untrusted_read.sh. The curation step (scripts/article_curate.py)
is the only ingress for downstream summarization.

Usage:
  uv run python scripts/article_crawl.py                 # process up to 6
  uv run python scripts/article_crawl.py --limit 10
  uv run python scripts/article_crawl.py --delay 30
  uv run python scripts/article_crawl.py --dry-run       # list, no HTTP
  uv run python scripts/article_crawl.py --url URL       # force one URL
"""

import argparse
import functools
import hashlib
import html
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests

# Playwright is used for PDF rendering. Heavy import + needs Chromium
# installed at runtime; treat as optional so the rest of the crawler
# works even if Playwright isn't set up locally.
try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "assets" / "articles"
QUEUE_DIR = DATA_DIR / "queue"
PRIORITY_DIR = QUEUE_DIR / "priority"
STATE_PATH = DATA_DIR / ".crawl_state.json"
FAILED_PATH = DATA_DIR / ".failed_urls.json"
CHECK_INJECTION = ROOT / "scripts" / "check_injection.py"
SOURCES_PATH = ROOT / "assets" / "sources.json"

# Real Chrome UA, matching scripts/flock_transparency.py via lib.py.
# Custom bot UAs got 403'd by anti-bot heuristics (kxan.com etc.) and slow-
# walked elsewhere; the Flock portal scraper has been reliable on the
# browser UA so we stick with the same string here.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 30
MAX_RETRIES_PER_URL = 3


# ───────────────────────── data plumbing ─────────────────────────


def url_filename(url: str) -> str:
    """Per-URL queue file name. Matches scripts/article_queue_add.py.
    First 8 hex chars of sha256(url) — same convention article_crawl
    already uses for the slug-uniqueness suffix."""
    return hashlib.sha256(url.encode()).hexdigest()[:8] + ".json"


def load_queue_dir(d: Path) -> list[dict]:
    """Read every *.json file directly under d (non-recursive). Sorted
    by mtime ascending so older URLs get processed first (FIFO)."""
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.suffix == ".json" and p.is_file()]
    files.sort(key=lambda p: (p.stat().st_mtime, p.name))
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARN: skipping malformed queue file {f.name}: {e}",
                  file=sys.stderr)
    return out


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text() or json.dumps(default))


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_source_for(domain: str) -> dict | None:
    """Return the sources.json entry whose 'domain' matches, after the same
    normalize-and-strip logic article_queue_add.py uses."""
    if not SOURCES_PATH.exists():
        return None
    sources = json.loads(SOURCES_PATH.read_text()).get("sources", [])
    by_domain = {s["domain"].lower(): s for s in sources}
    h = domain.lower()
    if h.startswith("www."):
        h = h[4:]
    if h in by_domain:
        return by_domain[h]
    parts = h.split(".")
    for i in range(1, len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in by_domain:
            return by_domain[cand]
    return None


# ───────────────────────── slug / paths ─────────────────────────


_SLUG_PUNCT = re.compile(r"[^a-z0-9]+")


def url_to_slug(url: str) -> str:
    """Filename-safe slug derived from URL path + 8-char hash for uniqueness.

    Example:
      https://www.eff.org/deeplinks/2024/09/flock-cameras-policy
        -> 'deeplinks-2024-09-flock-cameras-policy_a1b2c3d4'

    The hash bit guarantees uniqueness even when two URLs share a path
    (e.g. tracking-param-only differences after fragment stripping).
    """
    parsed = urlparse(url)
    raw = (parsed.path or "/").strip("/")
    raw = _SLUG_PUNCT.sub("-", raw.lower()).strip("-")
    if not raw:
        raw = "index"
    if len(raw) > 80:
        raw = raw[:80].rstrip("-")
    digest = hashlib.sha256(url.encode()).hexdigest()[:8]
    return f"{raw}_{digest}"


def article_dir(source_domain: str) -> Path:
    return DATA_DIR / source_domain


# ───────────────────────── extraction ─────────────────────────


_DROP_TAGS = {"script", "style", "noscript", "template", "iframe", "svg"}


class _TextExtractor(HTMLParser):
    """Strip tags, drop script/style content, concatenate text.

    Crude but stdlib-only. Curation re-extracts more carefully if needed.
    Does NOT preserve structure — paragraphs lose their boundaries beyond
    whitespace. Good enough for scanner input and quote-spotting.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._drop_depth = 0
        self._title: str | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self._drop_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS and self._drop_depth > 0:
            self._drop_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._drop_depth > 0:
            return
        if self._in_title and self._title is None:
            self._title = data.strip()
        self._chunks.append(data)

    @property
    def text(self) -> str:
        body = " ".join(c.strip() for c in self._chunks if c and c.strip())
        body = re.sub(r"\s+", " ", body)
        return body.strip()

    @property
    def title(self) -> str | None:
        return self._title


def extract_text_and_title(html_text: str) -> tuple[str, str | None]:
    p = _TextExtractor()
    try:
        p.feed(html_text)
    except Exception as e:
        # Malformed HTML shouldn't kill the crawl — fall back to a regex strip.
        print(f"WARN: html.parser failed ({e}); using regex fallback", file=sys.stderr)
        body = re.sub(r"<script\b.*?</script>", " ", html_text, flags=re.I | re.S)
        body = re.sub(r"<style\b.*?</style>", " ", body, flags=re.I | re.S)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", html.unescape(body)).strip()
        m = re.search(r"<title[^>]*>([^<]+)</title>", html_text, flags=re.I)
        return body, (m.group(1).strip() if m else None)
    return p.text, p.title


# ───────────────────────── scanner ─────────────────────────


def run_scanner(paths: list[Path]) -> dict:
    """Invoke check_injection.py --json on the given paths. Return parsed JSON.

    Failure modes are converted into a synthetic 'error' verdict so the
    crawler still records something useful in the meta sidecar.
    """
    if not paths:
        return {"verdict": "no-files", "total_score": 0, "files": []}
    # Use the same interpreter we're running under so the scanner picks up
    # the project venv if one is active. Hardcoding `python3` was breaking
    # in CI because the runner's `python3` resolved to a different
    # interpreter than `uv run python ...` and the file path wasn't relative
    # to its working directory.
    cmd = [sys.executable, str(CHECK_INJECTION), "--json", "--files",
           *[str(p) for p in paths]]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                check=False, timeout=120)
    except subprocess.TimeoutExpired:
        return {"verdict": "scanner-timeout", "total_score": 0, "files": []}
    if result.returncode == 3:
        return {"verdict": "scanner-error",
                "stderr": result.stderr.strip()[:1000],
                "total_score": 0, "files": []}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"verdict": "scanner-parse-error",
                "stdout": result.stdout[:1000],
                "stderr": result.stderr[:1000],
                "total_score": 0, "files": []}


# ───────────────────────── archival ─────────────────────────


def _ia_keys() -> tuple[str, str] | None:
    """Return (access, secret) IA SPN credentials, or None if unset.

    With keys we use the async Save Page Now API: submit returns a
    job_id in ~1s, status polled at end of run. Without keys we fall
    back to the slower anonymous Availability + sync save path.
    """
    a = os.environ.get("IA_ACCESS_KEY")
    s = os.environ.get("IA_SECRET_KEY")
    return (a, s) if a and s else None


def wayback_submit(url: str) -> dict:
    """Submit a Save Page Now job. Authenticated path is async; anonymous
    falls back to wayback_lookup_or_save (sync, blocking).

    The authenticated path uses ``if_not_archived_within=86400`` so
    re-runs of the same URL within 24h get the existing snapshot back
    server-side rather than triggering a redundant save — eliminates
    the Availability-API indexing-lag false negative.

    Returns: wayback_url + wayback_timestamp (set if existing snapshot),
    wayback_source ("submitted" | "existing" | "submit-failed"),
    wayback_job_id (set if a poll is needed), wayback_error.
    """
    keys = _ia_keys()
    if not keys:
        return wayback_lookup_or_save(url)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Authorization": f"LOW {keys[0]}:{keys[1]}",
        "Spn-Async": "1",
    }
    data = {
        "url": url,
        "if_not_archived_within": "86400",
    }
    out = {
        "wayback_url": None,
        "wayback_timestamp": None,
        "wayback_source": None,
        "wayback_job_id": None,
        "wayback_error": None,
    }
    try:
        resp = requests.post(
            "https://web.archive.org/save",
            headers=headers, data=data, timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as e:
        out["wayback_source"] = "submit-failed"
        out["wayback_error"] = f"{type(e).__name__}: {e}"
        return out

    if body.get("job_id"):
        out["wayback_job_id"] = body["job_id"]
        out["wayback_source"] = "submitted"
    elif body.get("url"):
        # if_not_archived_within served back an existing snapshot.
        out["wayback_url"] = body["url"]
        out["wayback_source"] = "existing"
        m = re.search(r"/web/(\d{14})/", body["url"])
        if m:
            out["wayback_timestamp"] = m.group(1)
    else:
        out["wayback_source"] = "submit-failed"
        out["wayback_error"] = f"unexpected SPN response: {str(body)[:300]}"
    return out


def wayback_poll(job_id: str, *, deadline: float,
                 poll_interval: float = 3.0) -> dict:
    """Poll SPN status until done or `deadline` (monotonic time.time())
    elapses. Returns wayback_url/timestamp/source/error suitable for
    merging into a record. On timeout, source="pending" — the job is
    still running server-side and a follow-up workflow can finalize it.
    """
    keys = _ia_keys()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if keys:
        headers["Authorization"] = f"LOW {keys[0]}:{keys[1]}"

    out = {
        "wayback_url": None,
        "wayback_timestamp": None,
        "wayback_source": None,
        "wayback_error": None,
    }
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"https://web.archive.org/save/status/{job_id}",
                headers=headers, timeout=10,
            )
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            out["wayback_source"] = "poll-failed"
            out["wayback_error"] = f"{type(e).__name__}: {e}"
            return out

        status = data.get("status")
        if status == "success":
            ts = data.get("timestamp")
            orig = data.get("original_url") or ""
            if ts and orig:
                out["wayback_url"] = f"https://web.archive.org/web/{ts}/{orig}"
                out["wayback_timestamp"] = ts
            out["wayback_source"] = "saved"
            return out
        if status == "error":
            out["wayback_source"] = "save-failed"
            out["wayback_error"] = data.get("message") or "unknown SPN error"
            return out
        # status == "pending" — keep polling
        time.sleep(poll_interval)

    out["wayback_source"] = "pending"
    out["wayback_error"] = "poll deadline elapsed; job may still complete"
    return out


def wayback_poll_pending(pending: list[tuple[dict, Path]], *,
                         total_timeout: int = 90) -> None:
    """Finalize all submitted-but-pending wayback jobs.

    Single shared deadline across all jobs — Wayback runs them all in
    parallel server-side, so sequential polling on one shared budget is
    equivalent in wall-clock time to parallel polling. Updates each
    record in place and rewrites its meta sidecar.
    """
    if not pending:
        return
    print(f"wayback: polling {len(pending)} pending save(s) "
          f"(deadline {total_timeout}s)...")
    deadline = time.time() + total_timeout
    for record, meta_path in pending:
        job_id = record.get("wayback_job_id")
        if not job_id:
            continue
        result = wayback_poll(job_id, deadline=deadline)
        record["wayback_url"] = result.get("wayback_url")
        record["wayback_timestamp"] = result.get("wayback_timestamp")
        record["wayback_source"] = result.get("wayback_source")
        if result.get("wayback_error"):
            record["wayback_error"] = result["wayback_error"]
        else:
            record.pop("wayback_error", None)
        save_json(meta_path, record)
        wb = record.get("wayback_url") or record.get("wayback_source")
        print(f"  {record['url']}: {wb}")


def wayback_lookup_or_save(url: str) -> dict:
    """Get a Wayback Machine snapshot URL for `url`, saving only if needed.

    Always checks the Availability API first
    (https://archive.org/wayback/available?url=...) — most articles from
    EFF/ACLU/NPR etc. are already heavily archived, and there's no reason
    to spam the Save endpoint when a snapshot already exists.

    Only when no snapshot is found do we trigger
    https://web.archive.org/save/<URL>. The save can take 30+ seconds for
    long pages; we cap at 60s and record the partial state if it
    times out — server-side save may still complete.

    Returns a dict suitable for merging into a meta record:
      wayback_url, wayback_timestamp (YYYYMMDDHHMMSS), wayback_source
      ("existing" | "saved" | "save-failed"), wayback_error (or None).
    """
    out = {
        "wayback_url": None,
        "wayback_timestamp": None,
        "wayback_source": None,
        "wayback_error": None,
    }
    headers = {"User-Agent": USER_AGENT}

    try:
        avail = requests.get(
            "https://archive.org/wayback/available",
            params={"url": url},
            headers=headers, timeout=10,
        )
        avail.raise_for_status()
        closest = (avail.json().get("archived_snapshots") or {}).get("closest")
        if closest and closest.get("available"):
            out["wayback_url"] = closest.get("url")
            out["wayback_timestamp"] = closest.get("timestamp")
            out["wayback_source"] = "existing"
            return out
    except (requests.RequestException, ValueError) as e:
        out["wayback_error"] = f"availability check: {type(e).__name__}: {e}"
        # Fall through and still attempt the save.

    try:
        # 90s — fresh saves of never-archived pages can take this long
        # while Wayback fetches and ingests. We accept the cost because
        # the availability check filtered out the common case (already
        # archived); only never-seen URLs reach this branch.
        save_resp = requests.get(
            f"https://web.archive.org/save/{url}",
            headers=headers, timeout=90, allow_redirects=True,
        )
        if save_resp.status_code in (200, 302):
            out["wayback_url"] = save_resp.url
            out["wayback_source"] = "saved"
            m = re.search(r"/web/(\d{14})/", save_resp.url)
            if m:
                out["wayback_timestamp"] = m.group(1)
        else:
            out["wayback_source"] = "save-failed"
            out["wayback_error"] = (out["wayback_error"] or "") + \
                f"; save HTTP {save_resp.status_code}"
    except requests.RequestException as e:
        out["wayback_source"] = "save-failed"
        out["wayback_error"] = (out["wayback_error"] or "") + \
            f"; save: {type(e).__name__}: {e}"
    return out


# ───────────────────────── fetch + render ─────────────────────────


def fetch_and_render(url: str, pdf_path: Path | None,
                     *, skip_pdf: bool = False) -> dict:
    """Navigate to `url` with Playwright (real Chrome UA), capture HTML,
    and optionally render the loaded page to `pdf_path` in the same
    browser session.

    One browser launch per URL covers both the HTML fetch and the PDF
    render — previously these were separate launches with a `requests`
    fetch in between, but bare `requests` got 403'd by anti-bot guards
    on several news sites we needed (kxan.com, kvue.com, etc.) and the
    `requests`-then-Playwright sequence also navigated to the URL twice.

    Returns dict with:
      fetch_error:   str | None — set on navigation failure or HTTP >= 400
      status:        int | None — HTTP status from page.goto response
      body:          str | None — page.content() HTML, or None on error
      final_url:     str | None — page.url after redirects
      pdf_status:    'rendered' | 'skipped' | 'failed' | None
      pdf_byte_size: int | None
      pdf_error:     str | None
    """
    out = {
        "fetch_error": None, "status": None, "body": None, "final_url": None,
        "pdf_status": None, "pdf_byte_size": None, "pdf_error": None,
    }
    if not _PLAYWRIGHT_AVAILABLE:
        out["fetch_error"] = "playwright not available"
        return out

    if pdf_path is not None and not skip_pdf:
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 1024},
                )
                page = ctx.new_page()
                # `domcontentloaded` is faster than `networkidle` and
                # good enough for static news pages; some sites (ad-
                # heavy news, advocacy blogs with embeds) never reach
                # networkidle within timeout.
                response = page.goto(url, wait_until="domcontentloaded",
                                     timeout=45000)
                if response is None:
                    out["fetch_error"] = "navigation returned no response"
                    return out
                out["status"] = response.status
                out["final_url"] = page.url
                if response.status >= 400:
                    out["fetch_error"] = f"HTTP {response.status}"
                    return out
                out["body"] = page.content()

                if skip_pdf or pdf_path is None:
                    out["pdf_status"] = "skipped"
                else:
                    try:
                        page.pdf(
                            path=str(pdf_path),
                            format="Letter",
                            margin={"top": "0.5in", "bottom": "0.5in",
                                    "left": "0.5in", "right": "0.5in"},
                            print_background=False,
                        )
                        if pdf_path.exists():
                            out["pdf_status"] = "rendered"
                            out["pdf_byte_size"] = pdf_path.stat().st_size
                        else:
                            out["pdf_status"] = "failed"
                            out["pdf_error"] = "pdf not written"
                    except Exception as e:
                        out["pdf_status"] = "failed"
                        out["pdf_error"] = f"{type(e).__name__}: {str(e)[:300]}"
            finally:
                browser.close()
    except Exception as e:
        if out["fetch_error"] is None:
            out["fetch_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    return out


# ───────────────────────── main loop ─────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def merge_queue() -> tuple[list[dict], list[dict]]:
    """Load (priority, auto). Drain priority first.

    The queue is a directory of one-file-per-URL: priority lives in
    PRIORITY_DIR, auto in QUEUE_DIR. load_queue_dir lists QUEUE_DIR
    non-recursively so the nested priority/ subdir doesn't
    double-count.
    """
    return load_queue_dir(PRIORITY_DIR), load_queue_dir(QUEUE_DIR)


def remove_from_queue(url: str) -> None:
    """Remove a URL from the queue. Idempotent — silently no-ops if
    the file doesn't exist (e.g. force-processed URL via --url that
    wasn't in the queue)."""
    fn = url_filename(url)
    for d in (PRIORITY_DIR, QUEUE_DIR):
        f = d / fn
        if f.exists():
            try:
                f.unlink()
            except OSError as e:
                print(f"WARN: failed to remove {f}: {e}", file=sys.stderr)


def crawl_once(entry: dict, *, dry_run: bool,
               skip_wayback: bool = False,
               skip_pdf: bool = False) -> dict:
    url = entry["url"]
    domain = entry.get("source_domain") or urlparse(url).netloc
    source = load_source_for(domain)
    slug = url_to_slug(url)
    out_dir = article_dir(domain)
    html_path = out_dir / f"{slug}.html"
    txt_path = out_dir / f"{slug}.txt"
    pdf_path = out_dir / f"{slug}.pdf"
    meta_path = out_dir / f"{slug}.meta.json"

    record = {
        "url": url,
        "source_domain": domain,
        "tier": (source or {}).get("tier"),
        "stance": (source or {}).get("stance"),
        "discovered_by": entry.get("discovered_by"),
        "queued_at": entry.get("discovered_at"),
        "slug": slug,
        "paths": {
            "html": str(html_path.relative_to(ROOT)),
            "txt": str(txt_path.relative_to(ROOT)),
            "meta": str(meta_path.relative_to(ROOT)),
        },
    }

    if dry_run:
        record["status"] = "dry-run"
        return record

    out_dir.mkdir(parents=True, exist_ok=True)

    fetch_result = fetch_and_render(url, pdf_path, skip_pdf=skip_pdf)
    if fetch_result["fetch_error"] is not None:
        record["status"] = "fetch-failed"
        record["error"] = fetch_result["fetch_error"]
        if fetch_result["status"] is not None:
            record["http_status"] = fetch_result["status"]
        return record

    status = fetch_result["status"]
    body = fetch_result["body"]
    final_url = fetch_result["final_url"]

    html_path.write_text(body, encoding="utf-8")
    text, title = extract_text_and_title(body)
    txt_path.write_text(text, encoding="utf-8")

    scanner = run_scanner([html_path, txt_path])

    record.update({
        "status": "fetched",
        "http_status": status,
        "final_url": final_url,
        "fetched_at": now_iso(),
        "content_hash": hashlib.sha256(body.encode()).hexdigest(),
        "byte_size": len(body.encode()),
        "extracted_chars": len(text),
        "http_title": title,
        "scanner_verdict": scanner.get("verdict"),
        "scanner_score": scanner.get("total_score", 0),
    })

    # Wayback snapshot — final_url (post-redirects) is what we want to
    # archive, since that's the canonical address of the content we
    # actually fetched. With IA credentials this submits an async SPN
    # job and returns a job_id in ~1s; status polling happens after all
    # crawls complete. Without credentials it falls back to the slower
    # sync availability+save path.
    if skip_wayback:
        record["wayback_source"] = "skipped"
    else:
        wayback = wayback_submit(final_url)
        record["wayback_url"] = wayback.get("wayback_url")
        record["wayback_timestamp"] = wayback.get("wayback_timestamp")
        record["wayback_source"] = wayback.get("wayback_source")
        if wayback.get("wayback_job_id"):
            record["wayback_job_id"] = wayback["wayback_job_id"]
        if wayback.get("wayback_error"):
            record["wayback_error"] = wayback["wayback_error"]

    # PDF render was performed in the same Playwright session as the
    # fetch (see fetch_and_render). PDFs are in the trusted-format
    # whitelist alongside parsed JSON, so they aren't gated by
    # check_untrusted_read.sh.
    record["pdf_status"] = fetch_result.get("pdf_status")
    record["pdf_byte_size"] = fetch_result.get("pdf_byte_size")
    if fetch_result.get("pdf_error"):
        record["pdf_error"] = fetch_result["pdf_error"]
    if pdf_path.exists():
        record["paths"]["pdf"] = str(pdf_path.relative_to(ROOT))

    save_json(meta_path, record)
    return record


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=6,
                   help="max URLs to process this run (default: 6)")
    p.add_argument("--delay", type=int, default=5,
                   help="base seconds between fetches; ±25%% jitter (default: 5). "
                        "The hourly cron schedule is the real rate limit; this "
                        "delay is just so back-to-back fetches don't burst.")
    p.add_argument("--dry-run", action="store_true",
                   help="list candidates, no HTTP")
    p.add_argument("--skip-wayback", action="store_true",
                   help="skip Wayback Machine archival lookup/save")
    p.add_argument("--skip-pdf", action="store_true",
                   help="skip Playwright PDF render (e.g. local runs without Chromium)")
    p.add_argument("--url", action="append", default=[],
                   help="force-process a specific URL "
                        "(repeatable; bypasses queue, still recorded)")
    args = p.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    state = load_json(STATE_PATH, {"fetched": [], "last_run": None})
    failed = load_json(FAILED_PATH, {})

    fetched_urls = set(state.get("fetched", []))

    def is_eligible(entry: dict) -> bool:
        url = entry.get("url")
        if not url or url in fetched_urls:
            return False
        return failed.get(url, {}).get("attempts", 0) < MAX_RETRIES_PER_URL

    def round_robin(pool: list[dict], remaining: int) -> list[dict]:
        # Pick at most one entry per source_domain per pass, walking the pool
        # in its existing order. Spreads a batch across publishers so a
        # backlog from one domain (e.g. 17 EFF URLs) can't monopolize a run.
        out: list[dict] = []
        queue = [e for e in pool if is_eligible(e)]
        while queue and len(out) < remaining:
            seen: set[str] = set()
            leftover: list[dict] = []
            for entry in queue:
                domain = entry.get("source_domain") or urlparse(entry["url"]).netloc
                if domain in seen:
                    leftover.append(entry)
                    continue
                out.append(entry)
                seen.add(domain)
                if len(out) >= remaining:
                    break
            if not leftover or len(out) >= remaining:
                break
            queue = leftover
        return out

    if args.url:
        candidates: list[dict] = [{"url": u, "discovered_by": "force"} for u in args.url]
        selected = [c for c in candidates if is_eligible(c)][: args.limit]
    else:
        priority, auto = merge_queue()
        selected = round_robin(priority, args.limit)
        if len(selected) < args.limit:
            selected.extend(round_robin(auto, args.limit - len(selected)))

    if not selected:
        print("nothing to crawl (queue empty or all already fetched/failed)")
        state["last_run"] = now_iso()
        save_json(STATE_PATH, state)
        return 0

    print(f"crawl plan: {len(selected)} URL(s)"
          f"{' [DRY-RUN]' if args.dry_run else ''}")
    for s in selected:
        print(f"  - {s['url']}")

    results = []
    for i, entry in enumerate(selected):
        if i > 0 and not args.dry_run:
            base = max(1, args.delay)
            sleep_s = random.uniform(base * 0.75, base * 1.25)
            print(f"sleep {sleep_s:.0f}s ...")
            time.sleep(sleep_s)
        rec = crawl_once(entry, dry_run=args.dry_run,
                         skip_wayback=args.skip_wayback,
                         skip_pdf=args.skip_pdf)
        results.append(rec)
        status = rec.get("status")
        line = f"  [{status}] {rec['url']}"
        if rec.get("scanner_verdict"):
            line += f"  scanner={rec['scanner_verdict']} score={rec.get('scanner_score', 0)}"
        if rec.get("error"):
            line += f"  err={rec['error'][:80]}"
        print(line)

        if args.dry_run:
            continue

        url = rec["url"]
        if status == "fetched":
            state.setdefault("fetched", []).append(url)
            failed.pop(url, None)
            remove_from_queue(url)
        elif status == "fetch-failed":
            f = failed.setdefault(url, {"attempts": 0})
            f["attempts"] += 1
            f["last_attempt"] = rec.get("fetched_at") or now_iso()
            f["error"] = rec.get("error")
            if f["attempts"] >= MAX_RETRIES_PER_URL:
                remove_from_queue(url)

    # Wayback async: submit happens inline in crawl_once and returns
    # immediately; poll all pending jobs here at the tail of the run.
    # All jobs are running concurrently server-side so a single shared
    # 90s deadline polls them efficiently.
    if not args.dry_run and not args.skip_wayback:
        pending = []
        for rec in results:
            if rec.get("wayback_job_id") and not rec.get("wayback_url"):
                meta_path = ROOT / rec["paths"]["meta"]
                pending.append((rec, meta_path))
        wayback_poll_pending(pending, total_timeout=90)

    if not args.dry_run:
        state["last_run"] = now_iso()
        save_json(STATE_PATH, state)
        save_json(FAILED_PATH, failed)

    fetched = sum(1 for r in results if r.get("status") == "fetched")
    failed_n = sum(1 for r in results if r.get("status") == "fetch-failed")
    print(f"summary: fetched={fetched} failed={failed_n} "
          f"total={len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
