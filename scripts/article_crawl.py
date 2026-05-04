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
  uv run python scripts/article_crawl.py                 # process up to 3
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
QUEUE_PATH = DATA_DIR / "queue.jsonl"
PRIORITY_PATH = DATA_DIR / "queue_priority.jsonl"
STATE_PATH = DATA_DIR / ".crawl_state.json"
FAILED_PATH = DATA_DIR / ".failed_urls.json"
CHECK_INJECTION = ROOT / "scripts" / "check_injection.py"
SOURCES_PATH = ROOT / "assets" / "sources.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; sm-alpr-research/1.0; "
    "+https://github.com/none-below/sm-alpr)"
)
TIMEOUT = 30
MAX_RETRIES_PER_URL = 3


# ───────────────────────── data plumbing ─────────────────────────


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"WARN: skipping malformed queue line: {e}", file=sys.stderr)
    return out


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


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


def render_pdf(url: str, pdf_path: Path) -> dict:
    """Render `url` to PDF via headless Chromium. Save to pdf_path.

    Returns: pdf_status ("rendered" | "skipped" | "failed"),
             pdf_byte_size, pdf_error (or None).
    """
    out = {"pdf_status": None, "pdf_byte_size": None, "pdf_error": None}
    if not _PLAYWRIGHT_AVAILABLE:
        out["pdf_status"] = "skipped"
        out["pdf_error"] = "playwright not available"
        return out
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
                # `domcontentloaded` is faster than `networkidle` and good
                # enough for static news pages; some sites (advocacy
                # blogs with embedded media) never reach networkidle.
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.pdf(
                    path=str(pdf_path),
                    format="Letter",
                    margin={"top": "0.5in", "bottom": "0.5in",
                            "left": "0.5in", "right": "0.5in"},
                    print_background=False,
                )
            finally:
                browser.close()
    except Exception as e:
        out["pdf_status"] = "failed"
        out["pdf_error"] = f"{type(e).__name__}: {str(e)[:300]}"
        return out
    if pdf_path.exists():
        out["pdf_status"] = "rendered"
        out["pdf_byte_size"] = pdf_path.stat().st_size
    else:
        out["pdf_status"] = "failed"
        out["pdf_error"] = "pdf not written"
    return out


# ───────────────────────── fetch ─────────────────────────


def fetch_one(url: str) -> tuple[int, str, str]:
    """Return (status_code, html_text, final_url)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"}
    r = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r.status_code, r.text, r.url


# ───────────────────────── main loop ─────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def merge_queue() -> tuple[list[dict], list[dict]]:
    """Load (priority, auto). Drain priority first."""
    return load_jsonl(PRIORITY_PATH), load_jsonl(QUEUE_PATH)


def remove_from_queue(url: str) -> None:
    """Remove a URL from whichever queue file holds it. Idempotent."""
    for path in (PRIORITY_PATH, QUEUE_PATH):
        entries = load_jsonl(path)
        kept = [e for e in entries if e.get("url") != url]
        if len(kept) != len(entries):
            write_jsonl(path, kept)


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

    try:
        status, body, final_url = fetch_one(url)
    except requests.RequestException as e:
        record["status"] = "fetch-failed"
        record["error"] = f"{type(e).__name__}: {e}"
        return record

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

    # Wayback snapshot — query first, save only if missing. The final_url
    # (post-redirects) is what we want to archive, since that's the
    # canonical address of the content we actually fetched.
    if skip_wayback:
        record["wayback_source"] = "skipped"
    else:
        wayback = wayback_lookup_or_save(final_url)
        record["wayback_url"] = wayback.get("wayback_url")
        record["wayback_timestamp"] = wayback.get("wayback_timestamp")
        record["wayback_source"] = wayback.get("wayback_source")
        if wayback.get("wayback_error"):
            record["wayback_error"] = wayback["wayback_error"]

    # Local PDF render via Playwright — separate path, never gated by
    # check_untrusted_read.sh (PDFs are in the trusted-format whitelist
    # alongside parsed JSON).
    if skip_pdf or not _PLAYWRIGHT_AVAILABLE:
        record["pdf_status"] = "skipped"
    else:
        pdf_result = render_pdf(final_url, pdf_path)
        record["pdf_status"] = pdf_result.get("pdf_status")
        record["pdf_byte_size"] = pdf_result.get("pdf_byte_size")
        if pdf_result.get("pdf_error"):
            record["pdf_error"] = pdf_result["pdf_error"]
        if pdf_path.exists():
            record["paths"]["pdf"] = str(pdf_path.relative_to(ROOT))

    save_json(meta_path, record)
    return record


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=3,
                   help="max URLs to process this run (default: 3)")
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

    if args.url:
        candidates: list[dict] = [{"url": u, "discovered_by": "force"} for u in args.url]
    else:
        priority, auto = merge_queue()
        candidates = priority + auto

    fetched_urls = set(state.get("fetched", []))
    selected: list[dict] = []
    for entry in candidates:
        url = entry.get("url")
        if not url:
            continue
        if url in fetched_urls:
            continue
        if failed.get(url, {}).get("attempts", 0) >= MAX_RETRIES_PER_URL:
            continue
        selected.append(entry)
        if len(selected) >= args.limit:
            break

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
