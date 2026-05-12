#!/usr/bin/env python3
"""Probe domains for working RSS/Atom feeds.

Two discovery strategies, tried in order:
  1. Autodiscovery — fetch the homepage, look for
     <link rel="alternate" type="application/rss+xml" ...> tags.
  2. Pattern fallback — try common feed paths (/feed/, /rss.xml, etc.)

A "working" feed is one that returns a feedparser-parseable response
with at least 1 entry. Prints a JSON-ish summary; the caller decides
which URLs to wire into assets/sources.json.

Usage:
  uv run python scripts/feed_probe.py domain1.com domain2.com ...
  uv run python scripts/feed_probe.py --from-sources-unknown
"""

import argparse
import functools
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "assets" / "sources.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; sm-alpr-research/1.0; "
    "+https://github.com/none-below/sm-alpr)"
)
USER_AGENT_BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

PATTERN_PATHS = [
    "/feed/",
    "/feed",
    "/rss",
    "/rss.xml",
    "/index.xml",
    "/atom.xml",
    "/feeds/all.atom.xml",
    "/?feed=rss2",
    "/arc/outboundfeeds/rss/?outputType=xml",
    "/feed/rss/",
    "/news/feed/",
    "/local/feed/",
]


class _AltLinkExtractor(HTMLParser):
    """Pull <link rel="alternate" type="application/(rss|atom)+xml"> hrefs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.feeds: list[tuple[str, str]] = []  # (href, type)

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = dict(attrs)
        rel = (a.get("rel") or "").lower()
        typ = (a.get("type") or "").lower()
        href = a.get("href") or ""
        if "alternate" in rel and href and (
            "rss" in typ or "atom" in typ or "xml" in typ
        ):
            self.feeds.append((href, typ))


def fetch(url: str, *, ua: str = USER_AGENT_BROWSER, timeout: int = 15
          ) -> tuple[bytes | None, int | None, str | None]:
    try:
        r = requests.get(url, headers={"User-Agent": ua},
                         timeout=timeout, allow_redirects=True)
        return r.content, r.status_code, None
    except requests.RequestException as e:
        return None, None, f"{type(e).__name__}: {e}"


def is_feed(content: bytes) -> tuple[bool, int]:
    """Return (looks_like_a_feed, entry_count)."""
    if not content:
        return False, 0
    # Fast reject: very short or HTML start
    head = content[:200].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return False, 0
    parsed = feedparser.parse(content)
    n = len(parsed.entries)
    if parsed.bozo and n == 0:
        return False, 0
    return n > 0, n


def autodiscover(domain: str) -> list[str]:
    """Fetch https://{domain}/, return candidate feed URLs from <link>."""
    home = f"https://{domain}/"
    body, status, err = fetch(home)
    if err or not body or status and status >= 400:
        return []
    p = _AltLinkExtractor()
    try:
        p.feed(body.decode("utf-8", errors="replace"))
    except Exception:
        return []
    out = []
    for href, _typ in p.feeds:
        full = urljoin(home, href)
        if full not in out:
            out.append(full)
    return out


def probe_domain(domain: str, *, delay: float) -> dict:
    """Find first working feed for domain. Returns result dict."""
    result = {"domain": domain, "feed_url": None, "entries": 0,
              "method": None, "tried": []}

    # 1. Autodiscovery
    candidates = autodiscover(domain)
    for url in candidates:
        time.sleep(delay)
        body, status, err = fetch(url)
        result["tried"].append({"url": url, "status": status, "err": err})
        if err or not body:
            continue
        ok, n = is_feed(body)
        if ok:
            result["feed_url"] = url
            result["entries"] = n
            result["method"] = "autodiscover"
            return result

    # 2. Pattern fallback
    for path in PATTERN_PATHS:
        url = f"https://{domain}{path}"
        time.sleep(delay)
        body, status, err = fetch(url)
        result["tried"].append({"url": url, "status": status, "err": err})
        if err or not body:
            continue
        ok, n = is_feed(body)
        if ok:
            result["feed_url"] = url
            result["entries"] = n
            result["method"] = "pattern"
            return result

    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("domains", nargs="*", help="domains to probe")
    p.add_argument("--from-sources-unknown", action="store_true",
                   help="probe every source in sources.json without a feed_url")
    p.add_argument("--delay", type=float, default=1.0,
                   help="seconds between requests (default: 1.0)")
    p.add_argument("--verbose", action="store_true",
                   help="show every URL tried, not just the winner")
    args = p.parse_args()

    domains = list(args.domains)
    if args.from_sources_unknown:
        data = json.loads(SOURCES_PATH.read_text())
        for s in data.get("sources", []):
            if not s.get("feed_url"):
                domains.append(s["domain"])

    if not domains:
        p.error("no domains specified")

    results = []
    for d in domains:
        r = probe_domain(d, delay=args.delay)
        results.append(r)
        if r["feed_url"]:
            print(f"OK   {d}  ({r['entries']} entries via {r['method']})")
            print(f"     {r['feed_url']}")
        else:
            print(f"MISS {d}")
            if args.verbose:
                for t in r["tried"]:
                    print(f"     - {t['url']}  status={t['status']} err={t['err']}")

    hits = sum(1 for r in results if r["feed_url"])
    print(f"\n{hits}/{len(results)} feeds discovered")

    # Machine-readable summary at the end for piping/parsing.
    print("\n=== JSON ===")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
