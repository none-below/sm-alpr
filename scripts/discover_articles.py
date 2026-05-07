#!/usr/bin/env python3
"""Discover ALPR/Flock articles via RSS and append to the queue.

Iterates assets/sources.json for entries with a ``feed_url``, fetches
each feed, filters items by ALPR/Flock-related keywords in title or
summary, and passes matching URLs through scripts/article_queue_add.py
(which validates the domain against the allowlist and dedupes against
existing queue + registry entries).

State-free: relies on article_queue_add's URL-level dedup. Re-running
over the same feed produces zero new queue entries — every URL we'd
add was already added on the previous run, so dedup short-circuits it.

Usage:
  uv run python scripts/discover_articles.py                # all feeds
  uv run python scripts/discover_articles.py --source eff.org
  uv run python scripts/discover_articles.py --dry-run
  uv run python scripts/discover_articles.py --keywords kw1,kw2
"""

import argparse
import functools
import json
import subprocess
import sys
from pathlib import Path

import feedparser
import requests

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "assets" / "sources.json"
QUEUE_ADD = ROOT / "scripts" / "article_queue_add.py"

USER_AGENT = (
    "Mozilla/5.0 (compatible; sm-alpr-research/1.0; "
    "+https://github.com/none-below/sm-alpr)"
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


def fetch_feed(url: str) -> tuple[list, str | None]:
    """Fetch + parse a feed. Returns (entries, error_or_None)."""
    try:
        resp = requests.get(url,
                            headers={"User-Agent": USER_AGENT},
                            timeout=30)
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


def process_source(source: dict, *, kws: list[str],
                   dry_run: bool) -> dict:
    """Process one source's feed. Returns stats dict."""
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

    discovered_by = f"rss:{domain}"
    _, qa_out = queue_add_batch(
        [u for u, _ in matches],
        discovered_by=discovered_by,
        dry_run=dry_run,
    )
    # article_queue_add prints "appended=N duplicate=M rejected=K -> ..."
    # We parse that line for stats; failures or malformed output just
    # leave the counters at 0.
    for line in qa_out.splitlines():
        line = line.strip()
        if line.startswith("appended="):
            for token in line.split():
                k, _, v = token.partition("=")
                if v.isdigit() and k in stats:
                    stats[k] = int(v)
            break
    return stats


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

    all_stats = []
    for source in sources:
        if not source.get("feed_url"):
            continue
        stats = process_source(source, kws=kws, dry_run=args.dry_run)
        all_stats.append(stats)

    print("\n=== summary ===")
    print(f"feeds processed:    {len(all_stats)}")
    total_items = sum(s["items"] for s in all_stats)
    total_matched = sum(s["matched"] for s in all_stats)
    total_appended = sum(s["appended"] for s in all_stats)
    total_duplicate = sum(s["duplicate"] for s in all_stats)
    print(f"feed items scanned: {total_items}")
    print(f"keyword matched:    {total_matched}")
    print(f"newly queued:       {total_appended}")
    print(f"already queued:     {total_duplicate}")
    errs = [s for s in all_stats if s["error"] and s["error"] != "no feed_url"]
    if errs:
        print(f"feeds with errors:  {len(errs)}")
        for s in errs:
            print(f"  {s['domain']}: {s['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
