#!/usr/bin/env python3
"""Check that every recipient of public-agency data has coordinates.

Scans the latest parsed portal JSON for each public agency and reports
any shared_org_slugs that exist in the registry but lack lat/lng.

Exit codes:
  0 — all recipients geocoded
  1 — one or more recipients missing coordinates (prints list to stdout)
"""

import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import has_tag, registry_by_slug
PORTAL_DIR = "assets/transparency.flocksafety.com"

# Flock test/placeholder slugs — not real agencies
JUNK_SLUGS = frozenset(
    [
        "decommissioned-org",
        "delete",
        "demo",
        "dnu",
        "duplicate-please-delete",
        "jaime-le-training",
        "old",
        "test-demo-1234",
    ]
)


def main() -> int:
    registry = registry_by_slug()

    # For each public agency, find the latest crawl JSON and collect recipients
    missing: dict[str, set[str]] = {}  # slug -> set of public senders

    for agency_dir in sorted(glob.glob(os.path.join(PORTAL_DIR, "*/"))):
        slug = os.path.basename(agency_dir.rstrip("/"))
        reg = registry.get(slug, {})
        if not has_tag(reg, "public"):
            continue

        jsons = sorted(glob.glob(os.path.join(agency_dir, "*.json")))
        if not jsons:
            continue

        with open(jsons[-1]) as f:
            data = json.load(f)

        for recipient in data.get("shared_org_slugs", []):
            if recipient in JUNK_SLUGS:
                continue
            r = registry.get(recipient)
            if r and (r.get("lat") is None or r.get("lng") is None):
                missing.setdefault(recipient, set()).add(slug)

    if not missing:
        print("All public-agency recipients have coordinates.")
        return 0

    print(f"{len(missing)} recipient(s) missing coordinates:\n")
    for slug in sorted(missing):
        senders = sorted(missing[slug])
        preview = ", ".join(senders[:3])
        if len(senders) > 3:
            preview += f" (+{len(senders) - 3} more)"
        print(f"  {slug}  <- {preview}")

    return 1


if __name__ == "__main__":
    sys.exit(main())
