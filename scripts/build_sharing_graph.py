#!/usr/bin/env python3
"""
Build a comprehensive sharing graph from Flock transparency portal data.

Resolves agency display names to agency_ids via the registry. All graph
keys and edges use agency_id (UUID).

Extracts both outbound sharing ("Organizations granted access to X") and
inbound sharing ("Organizations sharing their data with X") from stored
.txt files. Validates consistency between the two directions.

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
from lib import resolve_agency, agency_display_name, parse_org_list, portal_jsons, portal_txts


def extract_inbound_orgs(raw_text):
    """Extract inbound sharing list from raw DOM text."""
    label = "Organizations sharing their data with"
    idx = raw_text.find(label)
    if idx == -1:
        return []

    start = idx + len(label)
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
    section = re.sub(r"^.*?\n", "", section, count=1).strip()

    return parse_org_list(f"Organizations\n\n{section}" if section else "")


def _resolve_names_to_ids(names):
    """Resolve a list of agency display names to agency_ids."""
    ids = []
    for name in names:
        entry = resolve_agency(name=name)
        if entry:
            ids.append(entry["agency_id"])
    return ids


def main():
    parser = argparse.ArgumentParser(description="Build sharing graph from portal data")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    data_dir = args.data_dir

    # ── Collect data from all portals ──
    agencies = {}  # agency_id -> {...}

    for slug_dir in sorted(data_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue

        dir_slug = slug_dir.name
        txts = portal_txts(slug_dir)
        jsons = portal_jsons(slug_dir)
        if not txts:
            continue

        entry = resolve_agency(slug=dir_slug)
        if not entry:
            print(f"  WARNING: {dir_slug} not in registry, skipping")
            continue
        agency_id = entry["agency_id"]

        raw_text = txts[-1].read_text(encoding="utf-8")
        portal_data = json.loads(jsons[-1].read_text()) if jsons else {}

        # Support both old and new field names during transition
        outbound_names = portal_data.get("sharing_outbound") or portal_data.get("shared_org_names", [])
        outbound_ids = _resolve_names_to_ids(outbound_names)

        inbound_names = portal_data.get("sharing_inbound") or portal_data.get("orgs_sharing_with_names", [])
        if not inbound_names:
            inbound_names = extract_inbound_orgs(raw_text)
        inbound_ids = _resolve_names_to_ids(inbound_names)

        agencies[agency_id] = {
            "agency_id": agency_id,
            "slug": dir_slug,
            "archived_date": portal_data.get("archived_date"),
            "camera_count": portal_data.get("camera_count"),
            "data_retention_days": portal_data.get("data_retention_days"),
            "sharing_outbound_count": len(outbound_names),
            "sharing_outbound_ids": outbound_ids,
            "sharing_inbound_count": len(inbound_names),
            "sharing_inbound_ids": inbound_ids,
        }

    # ── Build bidirectional graph ──

    outbound_edges = defaultdict(set)  # source_id -> {target_ids}
    inbound_edges = defaultdict(set)   # target_id -> {source_ids}

    for aid, data in agencies.items():
        for target_id in data["sharing_outbound_ids"]:
            outbound_edges[aid].add(target_id)
        for source_id in data["sharing_inbound_ids"]:
            inbound_edges[aid].add(source_id)

    # ── Validate consistency ──

    mismatches = []
    crawled_ids = set(agencies.keys())

    for aid in crawled_ids:
        data = agencies[aid]

        if data["sharing_inbound_ids"]:
            for source_id in data["sharing_inbound_ids"]:
                if source_id in crawled_ids:
                    source_outbound = outbound_edges.get(source_id, set())
                    if aid not in source_outbound:
                        mismatches.append({
                            "type": "inbound_not_confirmed",
                            "agency": aid,
                            "claims_shared_by": source_id,
                            "source_has_outbound_list": bool(agencies[source_id]["sharing_outbound_ids"]),
                            "source_outbound_count": len(source_outbound),
                        })

        for target_id in data["sharing_outbound_ids"]:
            if target_id in crawled_ids and agencies[target_id]["sharing_inbound_ids"]:
                if aid not in inbound_edges.get(target_id, set()):
                    mismatches.append({
                        "type": "outbound_not_confirmed",
                        "agency": aid,
                        "shares_with": target_id,
                    })

    # ── Compute stats ──

    all_entities = set()
    for aid, data in agencies.items():
        all_entities.add(aid)
        all_entities.update(data["sharing_outbound_ids"])
        all_entities.update(data["sharing_inbound_ids"])

    inbound_counts = defaultdict(int)
    for source_id, targets in outbound_edges.items():
        for t in targets:
            inbound_counts[t] += 1

    most_sharing_inbound = sorted(inbound_counts.items(), key=lambda x: -x[1])[:30]

    # ── Build entries for uncrawled entities ──

    inbound_claimed_outbound = defaultdict(set)
    for target, sources in inbound_edges.items():
        for source in sources:
            inbound_claimed_outbound[source].add(target)

    uncrawled = {}
    for entity_id in sorted(all_entities - crawled_ids):
        received_from = sorted(s for s, targets in outbound_edges.items() if entity_id in targets)
        sends_to = sorted(inbound_claimed_outbound.get(entity_id, set()))
        uncrawled[entity_id] = {
            "archived_date": None,
            "crawled": False,
            "camera_count": None,
            "data_retention_days": None,
            "sharing_outbound_count": len(sends_to),
            "sharing_inbound_count": inbound_counts.get(entity_id, 0),
            "sharing_outbound_ids": sends_to,
            "sharing_inbound_ids": received_from,
        }

    # Merge crawled + uncrawled
    all_agencies = {}
    for aid, d in sorted(agencies.items()):
        all_agencies[aid] = {
            "archived_date": d["archived_date"],
            "crawled": True,
            "camera_count": d["camera_count"],
            "data_retention_days": d["data_retention_days"],
            "sharing_outbound_count": d["sharing_outbound_count"],
            "sharing_inbound_count": d["sharing_inbound_count"],
            "sharing_outbound_ids": d["sharing_outbound_ids"],
            "sharing_inbound_ids": d["sharing_inbound_ids"],
        }
    all_agencies.update(uncrawled)

    results = {
        "summary": {
            "agencies_crawled": len(agencies),
            "agencies_uncrawled": len(uncrawled),
            "total_entities": len(all_entities),
            "agencies_with_inbound_data": sum(
                1 for a in agencies.values() if a["sharing_inbound_count"] > 0
            ),
            "mismatches_found": len(mismatches),
        },
        "agencies": dict(sorted(all_agencies.items())),
        "mismatches": mismatches,
        "most_sharing_inbound": [
            {"agency_id": aid, "inbound_count": count}
            for aid, count in most_sharing_inbound
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
            print(f"  [{m['type']}] {m['agency']}")
        if len(mismatches) > 30:
            print(f"  ... and {len(mismatches) - 30} more")

    print(f"\n{'─' * 70}")
    print(f"MOST SHARING INBOUND (top 20)\n")
    for entry in results["most_sharing_inbound"][:20]:
        bar = "█" * min(entry["inbound_count"], 40)
        print(f"  {entry['inbound_count']:3d}  {bar}  {entry['agency_id']}")

    print(f"\n{'=' * 70}")

    # ── Save ──

    out_path = args.out or data_dir / ".sharing_graph_full.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=list) + "\n")
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
