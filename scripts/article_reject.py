#!/usr/bin/env python3
"""Flip article registry entries to `needs_review` after human rejection.

Companion to scripts/review_mechanical.py: when the review UI emits a
`reject: art_X art_Y …` line, this script marks those entries so they
stop showing up in future reviews. Existing consumers already skip
`needs_review`, so no other code needs to change.

Usage:
  scripts/article_reject.py --ids "art_111 art_222"
  scripts/article_reject.py --ids art_111,art_222 --reason "off-topic"
  scripts/article_reject.py --ids art_111 --dry-run
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import article_store


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", required=True,
                    help="comma- or space-separated article_ids to reject")
    ap.add_argument("--reason", default="rejected in review",
                    help="reason recorded in curation_error (default: %(default)r)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ids = {t for t in re.split(r"[,\s]+", args.ids) if t}
    if not ids:
        print("no ids given", file=sys.stderr)
        return 1

    registry = article_store.load_registry()
    found, flipped, skipped = 0, 0, 0
    for e in registry:
        if e.get("article_id") not in ids:
            continue
        found += 1
        status = e.get("curation_status")
        if status == "enriched":
            print(f"skip {e['article_id']}: already enriched (use a different "
                  f"flow to demote enriched entries)", file=sys.stderr)
            skipped += 1
            continue
        e["curation_status"] = "needs_review"
        e["curation_error"] = f"rejected: {args.reason}"
        e["curated_at"] = now_iso()
        flipped += 1
        print(f"{'would flip' if args.dry_run else 'flipped'} {e['article_id']} "
              f"({status} → needs_review)")

    missing = ids - {e["article_id"] for e in registry if e.get("article_id") in ids}
    for m in sorted(missing):
        print(f"NOT FOUND: {m}", file=sys.stderr)

    if not args.dry_run and flipped:
        article_store.save_registry(registry)
    print(f"summary: found={found} flipped={flipped} skipped={skipped} "
          f"not_found={len(missing)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
