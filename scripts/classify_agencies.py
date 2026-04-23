#!/usr/bin/env python3
"""
Report on agency classifications from the registry.

Reads assets/agency_registry.json (the single source of truth for
agency identity and classification) and generates reports on:
  - Private/non-public entities receiving ALPR data
  - Out-of-state agencies
  - Federal entities
  - Decommissioned/test entries
  - Entities needing review

Usage:
  uv run python scripts/classify_agencies.py
  uv run python scripts/classify_agencies.py --json --out outputs/agency_classifications.json
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import agency_display_name, agency_state, has_tag, load_registry, portal_jsons

DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")


def main():
    parser = argparse.ArgumentParser(description="Report on agency classifications")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    registry = load_registry()
    if not registry:
        print("Error: agency registry not found. Run build_agency_registry.py first.", file=sys.stderr)
        sys.exit(1)

    # Collect sharing info — who shares with whom (resolve names to agency_ids)
    from lib import resolve_agency
    org_sources = {}  # agency_id -> set of source agency_ids
    for slug_dir in sorted(DEFAULT_DATA_DIR.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue
        jsons = portal_jsons(slug_dir)
        if not jsons:
            continue
        data = json.loads(jsons[-1].read_text())
        source_entry = resolve_agency(slug=slug_dir.name)
        if not source_entry:
            continue
        source_id = source_entry["agency_id"]
        # Support both old and new field names
        outbound_names = data.get("sharing_outbound") or data.get("shared_org_names", [])
        for name in outbound_names:
            target = resolve_agency(name=name)
            if target:
                org_sources.setdefault(target["agency_id"], set()).add(source_id)

    # Classify from registry
    classifications = []
    for e in registry:
        aid = e["agency_id"]
        c = {
            "agency_id": aid,
            "name": agency_display_name(e),
            "tags": e.get("tags", []),
            "state": agency_state(e),
            "agency_type": e.get("agency_type"),
            "agency_role": e.get("agency_role"),
            "flags": [],
            "shared_by_count": len(org_sources.get(aid, set())),
            "shared_by": sorted(org_sources.get(aid, set())),
        }

        e_state = agency_state(e)
        if e_state and e_state != "CA":
            c["flags"].append("OUT_OF_STATE")
        if has_tag(e, "private"):
            c["flags"].append("PRIVATE")
        if has_tag(e, "federal"):
            c["flags"].append("FEDERAL")
        if e.get("agency_type") == "decommissioned":
            c["flags"].append("DECOMMISSIONED")
        if e.get("agency_type") == "test":
            c["flags"].append("TEST")
        if has_tag(e, "needs-review"):
            c["flags"].append("NEEDS_REVIEW")

        classifications.append(c)

    # Stats
    total = len(classifications)
    scope_counts = Counter(c["agency_type"] for c in classifications)
    role_counts = Counter(c["agency_role"] for c in classifications)
    flagged = [c for c in classifications if c["flags"]]

    results = {
        "summary": {
            "total": total,
            "california": sum(1 for c in classifications if c["state"] == "CA"),
            "out_of_state": sum(1 for c in classifications if c["state"] and c["state"] != "CA"),
            "private": sum(1 for c in classifications if "private" in c["tags"]),
            "federal": sum(1 for c in classifications if "federal" in c["tags"]),
            "needs_review": sum(1 for c in classifications if "needs-review" in c["tags"]),
            "scopes": dict(scope_counts.most_common()),
            "roles": dict(role_counts.most_common()),
        },
        "classifications": classifications,
        "flagged": flagged,
    }

    if args.json_output:
        print(json.dumps(results, indent=2, default=list))
    else:
        _print_report(results)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2, default=list) + "\n")
        print(f"\nJSON written to {args.out}")


def _print_report(r):
    s = r["summary"]
    print(f"{'=' * 70}")
    print(f"AGENCY CLASSIFICATION REPORT (from registry)")
    print(f"{'=' * 70}")
    print(f"\n  Total entities:          {s['total']}")
    print(f"  California:              {s['california']}")
    print(f"  Out-of-state:            {s['out_of_state']}")
    print(f"  Private:                 {s['private']}")
    print(f"  Federal:                 {s['federal']}")
    print(f"  Needs review:            {s['needs_review']}")

    print(f"\n  Type breakdown:")
    for scope, count in sorted(s["scopes"].items(), key=lambda x: -x[1]):
        print(f"    {scope:<20} {count:>4}")

    print(f"\n  Role breakdown:")
    for role, count in sorted(s["roles"].items(), key=lambda x: -x[1]):
        print(f"    {role:<20} {count:>4}")

    # Private
    private = [c for c in r["classifications"] if "PRIVATE" in c["flags"]]
    if private:
        print(f"\n{'─' * 70}")
        print(f"PRIVATE ENTITIES ({len(private)})")
        print(f"  CA Civil Code §1798.90.55(b): sharing restricted to public agencies\n")
        for c in sorted(private, key=lambda x: -x["shared_by_count"]):
            print(f"  {c['name']}")
            print(f"    type: {c['agency_type']}, role: {c['agency_role']}")
            print(f"    shared by {c['shared_by_count']} agencies")
            print()

    # Out-of-state
    oos = [c for c in r["classifications"] if "OUT_OF_STATE" in c["flags"]]
    if oos:
        print(f"{'─' * 70}")
        print(f"OUT-OF-STATE ENTITIES ({len(oos)})\n")
        for c in sorted(oos, key=lambda x: -x["shared_by_count"]):
            print(f"  {c['name']}  [{c['state']}]")
            print(f"    shared by {c['shared_by_count']} agencies")
            print()

    # Federal
    federal = [c for c in r["classifications"] if "FEDERAL" in c["flags"]]
    if federal:
        print(f"{'─' * 70}")
        print(f"FEDERAL ENTITIES ({len(federal)})\n")
        for c in sorted(federal, key=lambda x: -x["shared_by_count"]):
            print(f"  {c['name']}")
            print(f"    shared by {c['shared_by_count']} agencies")
            print()

    # Needs review
    review = [c for c in r["classifications"] if "NEEDS_REVIEW" in c["flags"]]
    if review:
        print(f"{'─' * 70}")
        print(f"NEEDS REVIEW ({len(review)})\n")
        for c in review:
            print(f"  {c['name']}: type={c['agency_type']}, role={c['agency_role']}, public={c['public']}")

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    main()
