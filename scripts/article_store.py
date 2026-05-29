#!/usr/bin/env python3
"""Sharded storage for the curated article registry.

Each article is one JSON file at assets/article_registry/<article_id>.json
(the id is sha256(url)-derived — see article_curate.article_id_for_url —
so the filename is stable and unique). This replaced a single
article_registry.json array: with one file per article, parallel PRs that
add or enrich different articles touch disjoint files, so they can't
produce array-append git conflicts or silent dup-merges. Two PRs that DO
create the same article_id collide as a git add/add conflict on the same
filename — surfaced at merge instead of slipping through to lint.

The directory is the source of truth; nothing assembles a combined
article_registry.json (the web viewer reads the derived
docs/data/articles_data.json built by build_articles_data.py).

Curated JSON is the safe view of scraped content — keep these shards under
assets/article_registry/, distinct from the raw, untrusted html/txt under
assets/articles/<domain>/ (see CLAUDE.md).
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = ROOT / "assets" / "article_registry"


def shard_path(article_id: str, registry_dir: Path = REGISTRY_DIR) -> Path:
    return registry_dir / f"{article_id}.json"


def load_registry(registry_dir: Path = REGISTRY_DIR) -> list[dict]:
    """Every article entry, sorted by article_id for a deterministic order
    (so callers that serialize the list get stable output)."""
    if not registry_dir.is_dir():
        return []
    entries = [json.loads(p.read_text())
               for p in registry_dir.glob("*.json")]
    entries.sort(key=lambda e: e.get("article_id") or "")
    return entries


def save_registry(entries: list[dict], registry_dir: Path = REGISTRY_DIR) -> int:
    """Write each entry to its <article_id>.json shard, but only when the
    serialized content actually changed. This keeps a one-entry edit to a
    one-file diff instead of rewriting every shard. Returns the number of
    shards written. (Entries are never deleted here — curation only adds or
    mutates, it doesn't drop articles.)"""
    registry_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for e in entries:
        p = registry_dir / f"{e['article_id']}.json"
        data = json.dumps(e, indent=2, sort_keys=False) + "\n"
        if not p.exists() or p.read_text() != data:
            p.write_text(data)
            written += 1
    return written
