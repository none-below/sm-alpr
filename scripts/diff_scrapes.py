#!/usr/bin/env python3
"""Compare the two most recent JSON scrapes for each agency and report changes."""

import json
import sys
from pathlib import Path

DATA_DIR = Path("assets/transparency.flocksafety.com")

# Fields to skip in diffs (always change between scrapes)
SKIP_FIELDS = {"archived_date"}

# Fields where we show set diffs instead of full values
LIST_FIELDS = {"shared_org_names", "shared_org_slugs",
               "orgs_sharing_with_names", "orgs_sharing_with_slugs"}


def diff_agency(slug_dir: Path) -> list[str]:
    jsons = sorted(slug_dir.glob("*.json"))
    if len(jsons) < 2:
        return []

    old = json.loads(jsons[-2].read_text())
    new = json.loads(jsons[-1].read_text())
    old_date = jsons[-2].stem
    new_date = jsons[-1].stem

    changes = []
    all_keys = sorted(set(old.keys()) | set(new.keys()))

    for key in all_keys:
        if key in SKIP_FIELDS:
            continue
        old_val = old.get(key)
        new_val = new.get(key)
        if old_val == new_val:
            continue

        if key in LIST_FIELDS:
            old_set = set(old_val) if old_val else set()
            new_set = set(new_val) if new_val else set()
            added = sorted(new_set - old_set)
            removed = sorted(old_set - new_set)
            parts = []
            if added:
                parts.append(f"+{len(added)}")
            if removed:
                parts.append(f"-{len(removed)}")
            changes.append(f"  {key}: {', '.join(parts)}")
            for a in added:
                changes.append(f"    + {a}")
            for r in removed:
                changes.append(f"    - {r}")
        elif isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
            changes.append(f"  {key}: {old_val} -> {new_val}")
        else:
            old_str = json.dumps(old_val) if not isinstance(old_val, str) else old_val
            new_str = json.dumps(new_val) if not isinstance(new_val, str) else new_val
            if len(old_str) > 80 or len(new_str) > 80:
                changes.append(f"  {key}: (changed)")
            else:
                changes.append(f"  {key}: {old_str} -> {new_str}")

    if not changes:
        return []

    slug = slug_dir.name
    return [f"{slug} ({old_date} -> {new_date}):", *changes]


def main():
    if not DATA_DIR.is_dir():
        print("No data directory found.")
        sys.exit(0)

    all_diffs = []
    unchanged = 0

    for slug_dir in sorted(DATA_DIR.iterdir()):
        if not slug_dir.is_dir():
            continue
        diff = diff_agency(slug_dir)
        if diff:
            all_diffs.extend(diff)
            all_diffs.append("")
        elif len(sorted(slug_dir.glob("*.json"))) >= 2:
            unchanged += 1

    if all_diffs:
        print("\n".join(all_diffs))
    if unchanged:
        print(f"({unchanged} agencies unchanged)")
    if not all_diffs and not unchanged:
        print("No agencies with multiple scrapes to compare.")


if __name__ == "__main__":
    main()
