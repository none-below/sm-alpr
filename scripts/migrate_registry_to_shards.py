#!/usr/bin/env python3
"""One-time: split assets/article_registry.json into per-article shards.

Reads the legacy single-file array and writes one
assets/article_registry/<article_id>.json per entry via article_store,
so the on-disk format matches everything written from here on. After
running this, `git rm assets/article_registry.json` and gitignore it.

Idempotent — re-running only rewrites shards whose content changed.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import article_store

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ROOT / "assets" / "article_registry.json"


def main() -> int:
    if not LEGACY.exists():
        print(f"nothing to migrate: {LEGACY} not found")
        return 0
    entries = json.loads(LEGACY.read_text())
    written = article_store.save_registry(entries)
    print(f"migrated {len(entries)} entries -> {article_store.REGISTRY_DIR} "
          f"({written} shards written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
