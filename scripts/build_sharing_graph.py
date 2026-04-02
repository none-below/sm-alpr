#!/usr/bin/env python3
"""
Build a comprehensive sharing graph from Flock transparency portal data.

Extracts both outbound sharing ("Organizations granted access to X") and
inbound sharing ("Organizations sharing their data with X") from stored
.txt files. Validates consistency between the two directions.

Outputs:
  - Per-agency JSON with outbound/inbound lists
  - Full graph with bidirectional edges and mismatch flags
  - Summary statistics

Usage:
  uv run python scripts/build_sharing_graph.py
  uv run python scripts/build_sharing_graph.py --out outputs/sharing_graph.json
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")

sys.path.insert(0, str(Path(__file__).parent))
from lib import load_registry, flock_name_to_slug, parse_org_list


def extract_inbound_orgs(raw_text):
    """Extract inbound sharing list from raw DOM text."""
    label = "Organizations sharing their data with"
    idx = raw_text.find(label)
    if idx == -1:
        return []

    start = idx + len(label)
    # Find the end — look for common boundaries
    end_markers = [
        "Additional Info", "Policy Documents", "Provided by Flock Safety",
        "Organizations granted access",
        "Delivery address for new",
        "Download CSV",
        "Public Search Audit",
    ]
    end = len(raw_text)
    for marker in end_markers:
        pos = raw_text.find(marker, start)
        if pos != -1 and pos < end:
            end = pos

    section = raw_text[start:end]
    # Strip "Agency Name\n\n" header
    section = re.sub(r"^.*?\n", "", section, count=1).strip()

    return parse_org_list(f"Organizations\n\n{section}" if section else "")


def main():
    parser = argparse.ArgumentParser(description="Build sharing graph from portal data")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    data_dir = args.data_dir

    # ── Collect data from all portals ──
    agencies = {}  # slug -> {outbound_names, inbound_names, outbound_slugs, inbound_slugs, ...}

    for slug_dir in sorted(data_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue

        slug = slug_dir.name
        txts = sorted(slug_dir.glob("*.txt"))
        jsons = sorted(slug_dir.glob("*.json"))
        if not txts:
            continue

        raw_text = txts[-1].read_text(encoding="utf-8")
        portal_data = json.loads(jsons[-1].read_text()) if jsons else {}

        outbound_names = portal_data.get("shared_org_names", [])
        outbound_slugs = portal_data.get("shared_org_slugs", [])

        inbound_names = extract_inbound_orgs(raw_text)
        inbound_slugs = [flock_name_to_slug(n) for n in inbound_names]
        inbound_slugs = [s for s in inbound_slugs if s]  # drop unresolved

        agencies[slug] = {
            "slug": slug,
            "archived_date": portal_data.get("archived_date"),
            "camera_count": portal_data.get("camera_count"),
            "data_retention_days": portal_data.get("data_retention_days"),
            "outbound_count": len(outbound_names),
            "outbound_names": outbound_names,
            "outbound_slugs": outbound_slugs,
            "inbound_count": len(inbound_names),
            "inbound_names": inbound_names,
            "inbound_slugs": inbound_slugs,
        }

    # ── Build bidirectional graph ──

    # Outbound edges: A shares with B (A's portal says "granted access to B")
    outbound_edges = defaultdict(set)  # source -> {targets}
    # Inbound edges: B receives from A (B's portal says "A shares with B")
    inbound_edges = defaultdict(set)   # target -> {sources}

    for slug, data in agencies.items():
        for target in data["outbound_slugs"]:
            outbound_edges[slug].add(target)
        for source in data["inbound_slugs"]:
            inbound_edges[slug].add(source)

    # ── Validate consistency ──

    mismatches = []
    crawled = set(agencies.keys())

    for slug in crawled:
        data = agencies[slug]

        # For agencies with inbound data: check if the claimed sources
        # actually list this agency in their outbound
        if data["inbound_slugs"]:
            for source_slug in data["inbound_slugs"]:
                if source_slug in crawled:
                    source_outbound = outbound_edges.get(source_slug, set())
                    if slug not in source_outbound:
                        source_has_outbound = bool(agencies[source_slug]["outbound_slugs"])
                        mismatches.append({
                            "type": "inbound_not_confirmed",
                            "agency": slug,
                            "claims_shared_by": source_slug,
                            "source_has_outbound_list": source_has_outbound,
                            "source_outbound_count": len(source_outbound),
                            "detail": f"{slug}'s portal says {source_slug} shares with it, "
                                      f"but {source_slug}'s portal "
                                      f"({'lists ' + str(len(source_outbound)) + ' agencies but not ' + slug if source_has_outbound else 'has no outbound sharing section'}",
                        })

        # Check if agencies we share with list us in their inbound
        for target_slug in data["outbound_slugs"]:
            if target_slug in crawled and agencies[target_slug]["inbound_slugs"]:
                if slug not in inbound_edges.get(target_slug, set()):
                    mismatches.append({
                        "type": "outbound_not_confirmed",
                        "agency": slug,
                        "shares_with": target_slug,
                        "detail": f"{slug} shares with {target_slug}, "
                                  f"but {target_slug}'s inbound list doesn't include {slug}",
                    })

    # ── Compute stats ──

    all_entities = set()
    for slug, data in agencies.items():
        all_entities.add(slug)
        all_entities.update(data["outbound_slugs"])
        all_entities.update(data["inbound_slugs"])

    # Inbound count per entity (how many agencies share with this entity)
    inbound_counts = defaultdict(int)
    for slug, targets in outbound_edges.items():
        for t in targets:
            inbound_counts[t] += 1

    most_shared_with = sorted(inbound_counts.items(), key=lambda x: -x[1])[:30]

    # ── Build entries for uncrawled entities ──
    # We know they exist because crawled agencies list them as recipients.

    uncrawled = {}
    for entity in sorted(all_entities - set(agencies.keys())):
        received_from = sorted(s for s, targets in outbound_edges.items() if entity in targets)
        uncrawled[entity] = {
            "archived_date": None,
            "crawled": False,
            "camera_count": None,
            "data_retention_days": None,
            "outbound_count": 0,
            "inbound_count": inbound_counts.get(entity, 0),
            "outbound_slugs": [],
            "inbound_slugs": received_from,
        }

    # Merge crawled + uncrawled
    all_agencies = {}
    for slug, d in sorted(agencies.items()):
        all_agencies[slug] = {
            "archived_date": d["archived_date"],
            "crawled": True,
            "camera_count": d["camera_count"],
            "data_retention_days": d["data_retention_days"],
            "outbound_count": d["outbound_count"],
            "inbound_count": d["inbound_count"],
            "outbound_slugs": d["outbound_slugs"],
            "inbound_slugs": d["inbound_slugs"],
        }
    all_agencies.update(uncrawled)

    results = {
        "summary": {
            "agencies_crawled": len(agencies),
            "agencies_uncrawled": len(uncrawled),
            "total_entities": len(all_entities),
            "agencies_with_inbound_data": sum(
                1 for a in agencies.values() if a["inbound_count"] > 0
            ),
            "mismatches_found": len(mismatches),
        },
        "agencies": dict(sorted(all_agencies.items())),
        "mismatches": mismatches,
        "most_shared_with": [
            {"slug": slug, "inbound_count": count}
            for slug, count in most_shared_with
        ],
    }

    # ── Print report ──

    s = results["summary"]
    print(f"{'=' * 70}")
    print(f"SHARING GRAPH")
    print(f"{'=' * 70}")
    print(f"  Agencies crawled:         {s['agencies_crawled']}")
    print(f"  Total entities:           {s['total_entities']}")
    print(f"  With inbound data:        {s['agencies_with_inbound_data']}")
    print(f"  Mismatches found:         {s['mismatches_found']}")

    if mismatches:
        print(f"\n{'─' * 70}")
        print(f"MISMATCHES ({len(mismatches)})\n")
        for m in mismatches[:30]:
            print(f"  [{m['type']}] {m['detail']}")
        if len(mismatches) > 30:
            print(f"  ... and {len(mismatches) - 30} more")

    print(f"\n{'─' * 70}")
    print(f"MOST SHARED WITH (top 20)\n")
    for entry in results["most_shared_with"][:20]:
        bar = "█" * min(entry["inbound_count"], 40)
        print(f"  {entry['inbound_count']:3d}  {bar}  {entry['slug']}")

    print(f"\n{'=' * 70}")

    # ── Save ──

    out_path = args.out or data_dir / ".sharing_graph_full.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=list) + "\n")
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
