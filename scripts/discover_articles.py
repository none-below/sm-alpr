#!/usr/bin/env python3
"""Discover ALPR/Flock articles via RSS feeds and Bing News search.

Two discovery modes run by default:

RSS mode
  Iterates assets/sources.json for entries with a ``feed_url``, fetches
  each feed, filters items by ALPR/Flock-related keywords in title or
  summary, and passes matching URLs through scripts/article_queue_add.py.

Search mode (--no-search to disable)
  Queries Bing News RSS for a set of ALPR-related search terms. Bing
  wraps article URLs in a click-tracker; the real URL is extracted from
  the ``url=`` query parameter. Results are filtered by the same keyword
  list (title only) before being passed to article_queue_add.py, which
  applies the domain allowlist from assets/sources.json and deduplicates
  against the queue and registry.

State-free: relies on article_queue_add's URL-level dedup. Re-running
over the same feeds/queries produces zero new queue entries.

Usage:
  uv run python scripts/discover_articles.py                # RSS + search
  uv run python scripts/discover_articles.py --no-search    # RSS only
  uv run python scripts/discover_articles.py --source eff.org
  uv run python scripts/discover_articles.py --dry-run
  uv run python scripts/discover_articles.py --keywords kw1,kw2
"""

import argparse
import functools
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import feedparser
import requests

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "assets" / "sources.json"
QUEUE_ADD = ROOT / "scripts" / "article_queue_add.py"

# Identified research bot — used for regular RSS feeds.
USER_AGENT = (
    "Mozilla/5.0 (compatible; sm-alpr-research/1.0; "
    "+https://github.com/none-below/sm-alpr)"
)

# Bing News RSS requires a browser-like User-Agent; the research bot UA
# returns zero results.
USER_AGENT_BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Substrings we look for (case-insensitive) in feed-item title or
# summary. Keep "flock" out of the bare list — it's too generic
# (covers flocks of birds, demographic flocking, etc.). Require a
# disambiguating second word.
KEYWORDS = [
    "alpr",
    "license plate reader",
    "license-plate reader",
    "license plate recognition",
    "license-plate recognition",
    "license plate camera",
    "license-plate camera",
    "automated license plate",
    "flock safety",
    "flock camera",
    "flock contract",
    "flock alpr",
    "flock surveillance",
    "vigilant solutions",
    "axon alpr",
    # Municipal-program / real-time-crime-center framings. Local outlets
    # often describe ALPR/Flock deployments euphemistically ("street
    # camera program", "public safety cameras") rather than naming the
    # vendor, so the headline alone carries no ALPR term. These broaden
    # recall for that framing; "fusus"/"real-time crime center" cover the
    # adjacent surveillance stack (see Axon Fusus). Modest false-positive
    # cost is absorbed by the two PR-review gates before crawl/merge.
    "real-time crime center",
    "real time crime center",
    "fusus",
    "street camera",
    "public safety camera",
]

# Bing News RSS search queries. Bing doesn't support quoted OR (e.g.
# "A" OR "B" returns 0 results), so each query is a separate request.
# Each tuple is (search_query, discovered_by_label).
# Results are filtered by KEYWORDS (title only) before queuing, then
# domain-validated by article_queue_add.py against assets/sources.json.
BING_NEWS_RSS = "https://www.bing.com/news/search?q={q}&format=RSS&count=20&mkt=en-US"
SEARCH_QUERIES = [
    ('"Flock Safety"',              "bing:flock"),
    ('"license plate reader"',      "bing:lpr"),
    ("ALPR",                        "bing:alpr"),
    ('"Vigilant Solutions"',        "bing:vigilant"),
    ('"Motorola ALPR"',             "bing:motorola"),
]


def matches_keywords(text: str, kws: list[str]) -> str | None:
    """Return the first matching keyword (lowercased), or None."""
    if not text:
        return None
    low = text.lower()
    for kw in kws:
        if kw in low:
            return kw
    return None


def extract_bing_url(raw: str) -> str | None:
    """Extract the real article URL from a Bing News click-tracker link.

    Bing wraps article links as:
      http://www.bing.com/news/apiclick.aspx?...&url=https%3A%2F%2F...
    The ``url`` query parameter holds the canonical article URL. Returns
    None if the URL looks like a tag/topic/section index rather than an
    article.
    """
    url = raw
    try:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
        urls = params.get("url")
        if urls:
            url = urls[0]
    except Exception:
        pass

    # Drop tag/category/section index pages — not individual articles.
    path = urllib.parse.urlparse(url).path.rstrip("/")
    path_parts = [p for p in path.split("/") if p]
    if path_parts and path_parts[-1] in ("tag", "topic", "tags", "category",
                                          "section", "records", "news"):
        return None
    non_article_segments = {"tag", "topic", "tags", "category", "section"}
    if any(p in non_article_segments for p in path_parts[:-1]):
        return None

    return url


def fetch_feed(url: str, *, ua: str = USER_AGENT) -> tuple[list, str | None]:
    """Fetch + parse a feed. Returns (entries, error_or_None)."""
    try:
        resp = requests.get(url, headers={"User-Agent": ua}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        return [], f"{type(e).__name__}: {e}"
    parsed = feedparser.parse(resp.content)
    # feedparser sets bozo=1 for any quirk including non-fatal ones
    # (e.g. unknown XML namespaces). Only treat it as an error when
    # we got zero entries — otherwise the parse is usable.
    if parsed.bozo and not parsed.entries:
        return [], f"bozo parse: {parsed.bozo_exception}"
    return parsed.entries, None


def queue_add_batch(urls: list[str], *, discovered_by: str,
                    dry_run: bool) -> tuple[int, str]:
    """Run article_queue_add.py with the given URLs. Returns (rc, output)."""
    if dry_run or not urls:
        for u in urls:
            print(f"  [dry-run] would queue: {u}")
        return 0, ""
    cmd = [sys.executable, str(QUEUE_ADD),
           "--discovered-by", discovered_by, *urls]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.stdout.strip():
        print(f"  {result.stdout.strip()}")
    if result.stderr.strip():
        # queue_add prints REJECT lines and friendly errors to stderr;
        # echo them so the workflow log captures them.
        print(result.stderr.strip(), file=sys.stderr)
    return result.returncode, result.stdout


def parse_qa_stats(qa_out: str, stats: dict) -> None:
    """Parse appended=/duplicate= counters from queue_add output into stats."""
    for line in qa_out.splitlines():
        line = line.strip()
        if line.startswith("appended="):
            for token in line.split():
                k, _, v = token.partition("=")
                if v.isdigit() and k in stats:
                    stats[k] += int(v)
            break


def process_source(source: dict, *, kws: list[str], dry_run: bool) -> dict:
    """Process one source's RSS feed. Returns stats dict."""
    domain = source["domain"]
    feed_url = source.get("feed_url")
    stats = {
        "domain": domain, "items": 0, "matched": 0,
        "appended": 0, "duplicate": 0, "error": None,
    }
    if not feed_url:
        stats["error"] = "no feed_url"
        return stats

    print(f"\n=== {domain}  feed: {feed_url}")
    entries, err = fetch_feed(feed_url)
    if err:
        stats["error"] = err
        print(f"  ERROR: {err}")
        return stats

    stats["items"] = len(entries)
    matches: list[tuple[str, str]] = []
    for entry in entries:
        url = entry.get("link")
        if not url:
            continue
        title = entry.get("title") or ""
        summary = entry.get("summary") or ""
        kw = matches_keywords(f"{title}\n{summary}", kws)
        if kw:
            matches.append((url, kw))
    stats["matched"] = len(matches)
    if not matches:
        print(f"  {len(entries)} items, 0 matched")
        return stats

    print(f"  {len(entries)} items, {len(matches)} matched:")
    for url, kw in matches:
        print(f"    [{kw}]  {url}")

    _, qa_out = queue_add_batch(
        [u for u, _ in matches],
        discovered_by=f"rss:{domain}",
        dry_run=dry_run,
    )
    parse_qa_stats(qa_out, stats)
    return stats


def process_search_queries(queries: list[tuple[str, str]], *,
                           kws: list[str], dry_run: bool) -> dict:
    """Query Bing News RSS for each search term and queue results.

    Applies keyword filter on title to discard off-topic Bing hits.
    Domain validation in article_queue_add.py rejects unknown sources.
    Returns aggregate stats dict.
    """
    agg = {"items": 0, "matched": 0, "appended": 0, "duplicate": 0, "errors": 0}

    for i, (query, label) in enumerate(queries):
        if i > 0:
            time.sleep(1)  # light pacing between Bing requests

        feed_url = BING_NEWS_RSS.format(q=urllib.parse.quote(query))
        print(f"\n=== search: {query!r}")

        entries, err = fetch_feed(feed_url, ua=USER_AGENT_BROWSER)
        if err:
            print(f"  ERROR: {err}")
            agg["errors"] += 1
            continue

        agg["items"] += len(entries)
        matches: list[tuple[str, str]] = []
        for entry in entries:
            raw = entry.get("link") or ""
            url = extract_bing_url(raw)
            if not url:
                continue
            title = entry.get("title") or ""
            kw = matches_keywords(title, kws)
            if kw:
                matches.append((url, kw))

        agg["matched"] += len(matches)
        if not matches:
            print(f"  {len(entries)} items, 0 keyword-matched")
            continue

        print(f"  {len(entries)} items, {len(matches)} keyword-matched:")
        for url, kw in matches:
            print(f"    [{kw}]  {url}")

        _, qa_out = queue_add_batch(
            [u for u, _ in matches], discovered_by=label, dry_run=dry_run
        )
        parse_qa_stats(qa_out, agg)

    return agg


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--source", default=None,
                   help="process only this source domain (default: all)")
    p.add_argument("--dry-run", action="store_true",
                   help="show would-queue without writing")
    p.add_argument("--keywords", default=None,
                   help="comma-separated keyword list "
                        "(default: built-in ALPR/Flock set)")
    p.add_argument("--no-search", action="store_true",
                   help="skip Bing News search queries; RSS feeds only")
    args = p.parse_args()

    kws = ([k.strip().lower() for k in args.keywords.split(",") if k.strip()]
           if args.keywords else KEYWORDS)

    sources = json.loads(SOURCES_PATH.read_text()).get("sources", [])
    if args.source:
        match = args.source.lower()
        sources = [s for s in sources if s["domain"].lower() == match]
        if not sources:
            print(f"no source named {args.source!r}", file=sys.stderr)
            return 1

    # --- RSS mode ---
    rss_stats: list[dict] = []
    for source in sources:
        if not source.get("feed_url"):
            continue
        stats = process_source(source, kws=kws, dry_run=args.dry_run)
        rss_stats.append(stats)

    # --- Search mode (skipped when --source or --no-search) ---
    search_agg: dict = {}
    if not args.no_search and not args.source:
        search_agg = process_search_queries(
            SEARCH_QUERIES, kws=kws, dry_run=args.dry_run
        )

    # --- Summary ---
    print("\n=== summary ===")
    print(f"feeds processed:     {len(rss_stats)}")
    print(f"feed items scanned:  {sum(s['items'] for s in rss_stats)}")
    print(f"keyword matched:     {sum(s['matched'] for s in rss_stats)}")
    print(f"newly queued (rss):  {sum(s['appended'] for s in rss_stats)}")
    print(f"already queued:      {sum(s['duplicate'] for s in rss_stats)}")
    if search_agg:
        print(f"search queries:      {len(SEARCH_QUERIES)}")
        print(f"search items:        {search_agg.get('items', 0)}")
        print(f"search kw-matched:   {search_agg.get('matched', 0)}")
        print(f"newly queued (srch): {search_agg.get('appended', 0)}")
    errs = [s for s in rss_stats if s["error"] and s["error"] != "no feed_url"]
    if errs:
        print(f"feeds with errors:   {len(errs)}")
        for s in errs:
            print(f"  {s['domain']}: {s['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
