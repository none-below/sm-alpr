#!/usr/bin/env python3
"""Check that every recipient of public-agency data has coordinates.

Reads the sharing graph and registry to find agencies that receive
shared data but lack lat/lng for the map.

Exit codes:
  0 — all recipients geocoded
  1 — one or more recipients missing coordinates (prints list to stdout)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import agency_coords, has_tag, registry_by_id, agency_display_name

GRAPH_PATH = Path("assets/transparency.flocksafety.com/.sharing_graph_full.json")

# Junk agency_types to skip (not real agencies worth geocoding)
JUNK_TYPES = {"test", "decommissioned"}


def main() -> int:
    if not GRAPH_PATH.exists():
        print("Sharing graph not found. Run build_sharing_graph.py first.", file=sys.stderr)
        return 1

    graph = json.loads(GRAPH_PATH.read_text())
    reg = registry_by_id()

    # Find agencies that receive data but lack coordinates
    missing: dict[str, set[str]] = {}  # agency_id -> set of source agency_ids

    for aid, data in graph["agencies"].items():
        if not data.get("crawled"):
            continue
        entry = reg.get(aid, {})
        if not has_tag(entry, "public"):
            continue

        for target_id in data.get("sharing_outbound_ids", []):
            target = reg.get(target_id, {})
            if not target:
                continue
            if target.get("agency_type") in JUNK_TYPES:
                continue
            # Also skip federal agencies (national, no specific location to geocode)
            if target.get("agency_type") == "federal" or has_tag(target, "federal"):
                continue
            lat, lng = agency_coords(target)
            if lat is None or lng is None:
                missing.setdefault(target_id, set()).add(aid)

    if not missing:
        print("All public-agency recipients have coordinates.")
        return 0

    print(f"{len(missing)} recipient(s) missing coordinates:\n")
    for aid in sorted(missing, key=lambda a: agency_display_name(reg.get(a, {}), a)):
        name = agency_display_name(reg.get(aid, {}), aid)
        senders = sorted(agency_display_name(reg.get(s, {}), s) for s in missing[aid])
        preview = ", ".join(senders[:3])
        if len(senders) > 3:
            preview += f" (+{len(senders) - 3} more)"
        print(f"  {name}  <- {preview}")

    return 1


if __name__ == "__main__":
    sys.exit(main())
