#!/usr/bin/env python3
"""Build docs/data/articles_data.json for the article viewer.

Reads:
  assets/article_registry.json   curated article entries
  assets/tags.json               tag vocabulary (descriptions)
  assets/agency_registry.json    agency_id -> display name lookup

Writes:
  docs/data/articles_data.json   shape consumed by docs/js/articles.js

Usage:
  uv run python scripts/build_articles_data.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import agency_coords, agency_display_name, agency_state, registry_by_id

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "assets" / "article_registry.json"
TAGS = ROOT / "assets" / "tags.json"
OUT_PATH = ROOT / "docs" / "data" / "articles_data.json"


def tag_namespace(tag: str) -> str:
    return tag.split(":", 1)[0] if ":" in tag else "other"


def main() -> int:
    registry = json.loads(REGISTRY.read_text())
    tags_doc = json.loads(TAGS.read_text())
    reg_by_id = registry_by_id()

    tag_descriptions: dict[str, str] = {}
    for ns in ("topics", "editorial"):
        for tag_id, spec in (tags_doc.get(ns) or {}).items():
            tag_descriptions[tag_id] = (spec or {}).get("description", "")

    articles = []
    tag_counts: dict[str, int] = {}

    for entry in registry:
        agencies = []
        for aid in entry.get("agencies") or []:
            ag = reg_by_id.get(aid)
            if not ag:
                continue
            lat, lng = agency_coords(ag)
            agencies.append({
                "agency_id": aid,
                "name": agency_display_name(ag, fallback=aid),
                "state": agency_state(ag),
                "lat": lat,
                "lng": lng,
            })

        primary_id = entry.get("primary_subject_agency_id")
        primary = None
        if primary_id:
            ag = reg_by_id.get(primary_id)
            if ag:
                lat, lng = agency_coords(ag)
                primary = {
                    "agency_id": primary_id,
                    "name": agency_display_name(ag, fallback=primary_id),
                    "state": agency_state(ag),
                    "lat": lat,
                    "lng": lng,
                }

        tags = sorted(entry.get("tags") or [])
        for t in tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1

        paths = entry.get("paths") or {}
        articles.append({
            "article_id": entry.get("article_id"),
            "title": entry.get("title") or entry.get("url"),
            "url": entry.get("url"),
            "source_domain": entry.get("source_domain"),
            "byline": entry.get("byline"),
            "published_at": entry.get("published_at"),
            "fetched_at": entry.get("fetched_at"),
            "summary": entry.get("summary"),
            "key_quotes": entry.get("key_quotes") or [],
            "tags": tags,
            "agencies": agencies,
            "primary_subject_agency": primary,
            "stance": entry.get("stance"),
            "tier": entry.get("tier"),
            "wayback_url": entry.get("wayback_url"),
            "paths": {
                k: paths.get(k) for k in ("html", "txt", "pdf", "meta")
                if paths.get(k)
            },
        })

    # Sort newest first by published_at; entries without a date sink to bottom.
    articles.sort(
        key=lambda a: (a["published_at"] or "", a["article_id"] or ""),
        reverse=True,
    )

    tags_out: dict[str, dict] = {}
    for tag, count in tag_counts.items():
        tags_out[tag] = {
            "namespace": tag_namespace(tag),
            "description": tag_descriptions.get(tag, ""),
            "count": count,
        }

    namespace_order = ["source", "vendor", "policy", "incident", "legal",
                       "scope", "stance", "genre", "agency", "other"]

    source_counts: dict[str, int] = {}
    for a in articles:
        d = a.get("source_domain")
        if d:
            source_counts[d] = source_counts.get(d, 0) + 1
    sources_out = [
        {"domain": d, "count": c}
        for d, c in sorted(source_counts.items(),
                           key=lambda kv: (-kv[1], kv[0]))
    ]

    out = {
        "articles": articles,
        "tags": tags_out,
        "sources": sources_out,
        "namespace_order": namespace_order,
        "counts": {
            "total_articles": len(articles),
            "total_tags": len(tags_out),
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, sort_keys=False) + "\n")
    print(f"wrote {OUT_PATH} — {len(articles)} articles, "
          f"{len(tags_out)} unique tags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
