#!/usr/bin/env python3
"""Append URLs to the article-fetch queue.

Validates each URL against assets/sources.json (publisher allowlist) and
deduplicates against existing queue files and the article registry. Designed
for two callers:

  - Claude (or a human) running searches, batching URLs into queue.jsonl
  - A human deliberately prioritizing a URL into queue_priority.jsonl

Usage:
  scripts/article_queue_add.py URL [URL ...]
  scripts/article_queue_add.py --priority URL [URL ...]
  scripts/article_queue_add.py --discovered-by 'search: flock alpr' URL...
  echo URL | scripts/article_queue_add.py --stdin

Exit codes:
  0  appended (or all already-known, nothing to do)
  1  one or more URLs rejected (unknown domain, malformed, etc.)
  3  I/O / config error
"""

import argparse
import functools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "assets" / "sources.json"
REGISTRY_PATH = ROOT / "assets" / "article_registry.json"
QUEUE_PATH = ROOT / "assets" / "articles" / "queue.jsonl"
PRIORITY_PATH = ROOT / "assets" / "articles" / "queue_priority.jsonl"


def load_allowed_domains() -> set[str]:
    if not SOURCES_PATH.exists():
        print(f"ERROR: {SOURCES_PATH} not found", file=sys.stderr)
        sys.exit(3)
    data = json.loads(SOURCES_PATH.read_text())
    return {s["domain"].lower() for s in data.get("sources", [])}


def normalize_domain(host: str) -> str:
    """Strip leading 'www.' and lowercase. Keep subdomains otherwise."""
    h = host.lower()
    if h.startswith("www."):
        h = h[4:]
    return h


def domain_matches(host: str, allowed: set[str]) -> str | None:
    """Return the matching allowlist entry, or None.

    Matches a host against its apex and any one-level subdomain stripping.
    e.g. 'www.eff.org' -> 'eff.org'; 'blog.flocksafety.com' -> 'flocksafety.com'.
    """
    h = normalize_domain(host)
    if h in allowed:
        return h
    parts = h.split(".")
    for i in range(1, len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in allowed:
            return candidate
    return None


def load_known_urls() -> set[str]:
    """All URLs that are already queued or in the registry."""
    seen: set[str] = set()
    for p in (QUEUE_PATH, PRIORITY_PATH):
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    seen.add(json.loads(line)["url"])
                except (json.JSONDecodeError, KeyError):
                    pass
    if REGISTRY_PATH.exists():
        for entry in json.loads(REGISTRY_PATH.read_text() or "[]"):
            url = entry.get("url")
            if url:
                seen.add(url)
    return seen


def validate_url(url: str) -> tuple[str, str | None]:
    """Return (canonical_url, error). error is None on success."""
    url = url.strip()
    if not url:
        return url, "empty"
    if url.startswith("//"):
        return url, "scheme-relative URL not allowed"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return url, f"scheme must be http(s), got {parsed.scheme!r}"
    if not parsed.netloc:
        return url, "no host"
    # Strip fragment (always); keep query (often material for article URLs).
    canonical = parsed._replace(fragment="").geturl()
    return canonical, None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("urls", nargs="*", help="URLs to enqueue")
    p.add_argument("--stdin", action="store_true",
                   help="read additional URLs from stdin (one per line)")
    p.add_argument("--priority", action="store_true",
                   help="append to queue_priority.jsonl instead of queue.jsonl")
    p.add_argument("--discovered-by", default="manual",
                   help="provenance string stored on each entry "
                        "(e.g. 'search: foo', 'activist-network: bar')")
    p.add_argument("--tags-hint", action="append", default=[],
                   help="optional tag hint for the curator (repeatable)")
    args = p.parse_args()

    urls = list(args.urls)
    if args.stdin:
        urls.extend(line.strip() for line in sys.stdin if line.strip())
    if not urls:
        p.error("provide at least one URL (positional or --stdin)")

    allowed = load_allowed_domains()
    known = load_known_urls()
    target = PRIORITY_PATH if args.priority else QUEUE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    appended = 0
    rejected = 0
    skipped_dup = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with target.open("a") as fh:
        for raw in urls:
            url, err = validate_url(raw)
            if err:
                print(f"REJECT {raw}: {err}", file=sys.stderr)
                rejected += 1
                continue
            host = urlparse(url).netloc
            matched = domain_matches(host, allowed)
            if matched is None:
                print(
                    f"REJECT {url}: domain {normalize_domain(host)!r} not in "
                    f"sources.json. Add the domain (with tier/stance) and re-run.",
                    file=sys.stderr,
                )
                rejected += 1
                continue
            if url in known:
                skipped_dup += 1
                continue
            entry = {
                "url": url,
                "source_domain": matched,
                "discovered_at": now,
                "discovered_by": args.discovered_by,
            }
            if args.tags_hint:
                entry["tags_hint"] = args.tags_hint
            fh.write(json.dumps(entry) + "\n")
            known.add(url)
            appended += 1

    where = "queue_priority.jsonl" if args.priority else "queue.jsonl"
    print(
        f"appended={appended} duplicate={skipped_dup} rejected={rejected} "
        f"-> {where}"
    )
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
