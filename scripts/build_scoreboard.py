#!/usr/bin/env python3
"""
Generate scoreboard data ranking agencies by sharing metrics.

Reads the sharing graph, agency registry, and crawled portal data to produce
a JSON file with top-3 rankings across several categories.

Usage:
  uv run python scripts/build_scoreboard.py
"""

import json
from pathlib import Path

REGISTRY_PATH = Path("assets/agency_registry.json")
GRAPH_PATH = Path("assets/transparency.flocksafety.com/.sharing_graph_full.json")
DATA_DIR = Path("assets/transparency.flocksafety.com")
OUT_PATH = Path("docs/data/scoreboard_data.json")

# Only CA agencies are ranked
RANKED_STATE = "CA"


def is_violation_entity(slug, registry_by_slug):
    """Match the violation logic from build_map.py."""
    r = registry_by_slug.get(slug, {})
    if r.get("public") is False:
        return "private"
    if r.get("state") and r["state"] != "CA":
        return "out_of_state"
    if r.get("agency_type") in ("federal", "decommissioned", "test"):
        return r["agency_type"]
    return None


def load_crawled_stats(slug):
    """Load the most recent crawled JSON for an agency, return key stats."""
    agency_dir = DATA_DIR / slug
    if not agency_dir.is_dir():
        return {}
    json_files = sorted(agency_dir.glob("*.json"), reverse=True)
    if not json_files:
        return {}
    with open(json_files[0]) as f:
        data = json.load(f)
    return {
        "vehicles_detected_30d": data.get("vehicles_detected_30d"),
        "hotlist_hits_30d": data.get("hotlist_hits_30d"),
        "searches_30d": data.get("searches_30d"),
    }


def main():
    with open(REGISTRY_PATH) as f:
        registry = json.load(f)
    registry_by_slug = {a["slug"]: a for a in registry}

    with open(GRAPH_PATH) as f:
        graph = json.load(f)

    # Build outbound lookup for indirect violation computation
    outbound_by_slug = {}
    for slug, data in graph["agencies"].items():
        outbound_by_slug[slug] = data.get("outbound_slugs", [])

    agencies = []
    for slug, data in graph["agencies"].items():
        info = registry_by_slug.get(slug, {})
        if not data.get("crawled"):
            continue

        outbound_slugs = data.get("outbound_slugs", [])
        outbound_count = data.get("outbound_count", 0)
        inbound_count = data.get("inbound_count", 0)

        # Classify outbound targets
        out_of_state = 0
        private = 0
        federal = 0
        universities = 0
        non_conforming = 0

        direct_violations = set()
        for target in outbound_slugs:
            v = is_violation_entity(target, registry_by_slug)
            if v == "out_of_state":
                out_of_state += 1
            elif v == "private":
                private += 1
            elif v == "federal":
                federal += 1
            if v:
                non_conforming += 1
                direct_violations.add(target)

            tr = registry_by_slug.get(target, {})
            if tr.get("agency_type") == "university":
                universities += 1

        # Indirect violations: targets' outbound that are violation entities
        indirect_violations = 0
        indirect_seen = set()
        for target in outbound_slugs:
            if target in outbound_by_slug:
                for second_hop in outbound_by_slug[target]:
                    if second_hop not in direct_violations and second_hop not in indirect_seen:
                        if is_violation_entity(second_hop, registry_by_slug):
                            indirect_violations += 1
                            indirect_seen.add(second_hop)

        # Load crawled portal stats
        crawled_stats = load_crawled_stats(slug)

        agencies.append({
            "slug": slug,
            "name": info.get("human_name", info.get("flock_name", slug)),
            "state": info.get("state", ""),
            "outbound": outbound_count,
            "inbound": inbound_count,
            "non_conforming": non_conforming,
            "out_of_state": out_of_state,
            "private": private,
            "federal": federal,
            "universities": universities,
            "indirect_violations": indirect_violations,
            "cameras": data.get("camera_count") or 0,
            "retention_days": data.get("data_retention_days"),
            "vehicles_30d": crawled_stats.get("vehicles_detected_30d"),
            "searches_30d": crawled_stats.get("searches_30d"),
        })

    # ── Compute conflict / transparency-gap metrics ──

    # Load the full graph (with mismatches) for conflict analysis
    mismatches = graph.get("mismatches", [])

    # Per-agency conflict count: cases where Agency B's inbound claims
    # that Source A shares with it, but Source A publishes an outbound
    # list that doesn't include B.  Rank by how many such claims point
    # at a given source.  Self-references are excluded.
    from collections import defaultdict
    source_conflict_targets = defaultdict(list)  # source -> [receivers]
    for m in mismatches:
        if m["type"] == "inbound_not_confirmed" and m.get("source_has_outbound_list"):
            source = m["claims_shared_by"]
            receiver = m["agency"]
            if source != receiver:
                source_conflict_targets[source].append(receiver)

    for a in agencies:
        a["sharing_conflicts"] = len(source_conflict_targets.get(a["slug"], []))

    # Agencies with a crawled portal but zero sharing list published
    # (they have a transparency page but hide who they share with)
    no_sharing_slugs = set()
    for slug, data in graph["agencies"].items():
        if data.get("crawled") and data.get("outbound_count", 0) == 0:
            # Check they actually don't publish sharing data at all
            if not data.get("outbound_slugs"):
                no_sharing_slugs.add(slug)

    no_sharing_list = []
    for a in agencies:
        if a["slug"] in no_sharing_slugs:
            no_sharing_list.append({"slug": a["slug"], "name": a["name"]})
    # Also include crawled agencies that weren't in the ranked set
    # (they might have been filtered out for other reasons)
    for slug in sorted(no_sharing_slugs):
        if not any(ns["slug"] == slug for ns in no_sharing_list):
            info = registry_by_slug.get(slug, {})
            no_sharing_list.append({
                "slug": slug,
                "name": info.get("human_name", info.get("flock_name", slug)),
            })

    # ── Compute metadata for disclaimers ──

    ca_in_registry = sum(1 for a in registry if a.get("state") == RANKED_STATE)
    ca_crawled = sum(
        1 for slug, d in graph["agencies"].items()
        if d.get("crawled") and registry_by_slug.get(slug, {}).get("state") == RANKED_STATE
    )
    # Agencies known to use Flock (appear in other agencies' sharing lists)
    # but have no findable transparency portal
    ca_no_portal = sum(
        1 for slug, d in graph["agencies"].items()
        if not d.get("crawled") and registry_by_slug.get(slug, {}).get("state") == RANKED_STATE
    )

    # Build categories — top 3 for each, spiciest first
    categories = [
        {
            "id": "out_of_state",
            "title": "Most Out-of-State Shares",
            "subtitle": "Agencies sharing ALPR data across state lines",
            "key": "out_of_state",
        },
        {
            "id": "non_conforming",
            "title": "Most Direct Non-Conforming Shares",
            "subtitle": "Direct shares with private, out-of-state, or federal entities",
            "key": "non_conforming",
        },
        {
            "id": "indirect",
            "title": "Most Indirect Non-Conforming Shares",
            "subtitle": "Non-conforming exposure through sharing partners' networks",
            "key": "indirect_violations",
        },
        {
            "id": "cameras",
            "title": "Most Cameras Deployed",
            "subtitle": "Largest ALPR camera networks by agency",
            "key": "cameras",
        },
        {
            "id": "outbound",
            "title": "Most Outbound Shares",
            "subtitle": "Agencies sharing their data with the most partners",
            "key": "outbound",
        },
        {
            "id": "vehicles_30d",
            "title": "Most Vehicles Detected (30 days)",
            "subtitle": "Highest surveillance volume in the last 30 days",
            "key": "vehicles_30d",
        },
        {
            "id": "searches_30d",
            "title": "Most Plate Lookups (30 days)",
            "subtitle": "Most manual plate searches in the last 30 days",
            "key": "searches_30d",
        },
        {
            "id": "inbound",
            "title": "Most Inbound Shares",
            "subtitle": "Agencies receiving data from the most other agencies",
            "key": "inbound",
        },
        {
            "id": "universities",
            "title": "Most Universities Shared To",
            "subtitle": "Agencies sharing ALPR data with university police",
            "key": "universities",
        },
        {
            "id": "retention",
            "title": "Longest Data Retention",
            "subtitle": "Agencies keeping your plate data the longest",
            "key": "retention_days",
        },
        {
            "id": "conflicts",
            "title": "Most Conflicting Sharing Data",
            "subtitle": "Other agencies claim to receive their data, but their outbound list disagrees",
            "key": "sharing_conflicts",
        },
    ]

    scoreboard = {
        "meta": {
            "state": RANKED_STATE,
            "agencies_in_registry": ca_in_registry,
            "agencies_crawled": ca_crawled,
            "agencies_no_portal": ca_no_portal,
        },
        "no_sharing_published": no_sharing_list,
        "categories": [],
    }
    for cat in categories:
        key = cat["key"]
        ranked = sorted(
            [a for a in agencies if a.get(key)],
            key=lambda a: a[key],
            reverse=True,
        )

        # 3 medal slots: gold, silver, bronze. Ties share the same medal.
        # Once we have 3+ entries, don't start a new rank — but do finish
        # a tie in progress (e.g. 1 gold, 1 silver, 3 bronze = 5 shown).
        podium = []
        slots_used = 0
        prev_value = None
        for a in ranked:
            if a[key] != prev_value:
                # Starting a new rank — stop if we already have 3+ entries
                if len(podium) >= 3:
                    break
                slots_used += 1
                prev_value = a[key]
            if slots_used > 3:
                break
            podium.append({
                "rank": slots_used,
                "name": a["name"],
                "slug": a["slug"],
                "value": a[key],
            })

        scoreboard["categories"].append({
            "id": cat["id"],
            "title": cat["title"],
            "subtitle": cat["subtitle"],
            "podium": podium,
        })

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(scoreboard, f, indent=2)

    print(f"Scoreboard data written to {OUT_PATH}")
    print(f"  {len(scoreboard['categories'])} categories")
    for cat in scoreboard["categories"]:
        print(f"  {cat['title']}: {', '.join(p['name'] for p in cat['podium'])}")


if __name__ == "__main__":
    main()
