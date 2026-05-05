#!/usr/bin/env python3
"""Append URLs to the article-fetch queue.

Validates each URL against assets/sources.json (publisher allowlist) and
deduplicates against existing queued URLs and the article registry.

The queue is a directory of one-file-per-URL, named ``<urlhash>.json``
where urlhash is the first 8 hex chars of sha256(url):

  assets/articles/queue/
    <urlhash>.json              # auto queue
    priority/
      <urlhash>.json            # priority queue, drained first by crawler

This layout sidesteps the merge-conflict class that broke us when the
queue lived in a single jsonl file: different URLs are different files,
so concurrent add/remove operations on different URLs commute trivially
through git's 3-way merge.

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
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "assets" / "sources.json"
REGISTRY_PATH = ROOT / "assets" / "article_registry.json"
QUEUE_DIR = ROOT / "assets" / "articles" / "queue"
PRIORITY_DIR = QUEUE_DIR / "priority"


def url_filename(url: str) -> str:
    """Per-URL queue file name. First 8 hex chars of sha256(url) — short
    enough to read, wide enough that 4B URLs would only collide ~once."""
    return hashlib.sha256(url.encode()).hexdigest()[:8] + ".json"


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
    """All URLs that are already queued or in the registry.

    For the queue: presence of the per-URL file in QUEUE_DIR or
    PRIORITY_DIR is the dedup signal. We still load the JSON content to
    get the canonical URL string (the filename hash is opaque), but we
    can short-circuit later by checking file existence first.
    """
    seen: set[str] = set()
    for d in (QUEUE_DIR, PRIORITY_DIR):
        if d.is_dir():
            for f in d.glob("*.json"):
                try:
                    seen.add(json.loads(f.read_text())["url"])
                except (json.JSONDecodeError, KeyError, OSError):
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
                   help="write into queue/priority/ instead of queue/ "
                        "(crawler drains priority first)")
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
    target_dir = PRIORITY_DIR if args.priority else QUEUE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    appended = 0
    rejected = 0
    skipped_dup = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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
        # Write atomically: write to a tempfile then rename, so a partial
        # file never appears under the queue directory (the crawler treats
        # any *.json file as a queued URL).
        target = target_dir / url_filename(url)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entry, indent=2) + "\n")
        tmp.replace(target)
        known.add(url)
        appended += 1

    where = "queue/priority/" if args.priority else "queue/"
    print(
        f"appended={appended} duplicate={skipped_dup} rejected={rejected} "
        f"-> {where}"
    )
    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
