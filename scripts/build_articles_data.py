#!/usr/bin/env python3
"""Build docs/data/articles_data.json for the article viewer.

Reads:
  assets/article_registry/       curated article entries (one file each)
  assets/tags.json               tag vocabulary (descriptions)
  assets/agency_registry.json    agency_id -> display name lookup

Writes:
  docs/data/articles_data.json   shape consumed by docs/js/articles.js

Usage:
  uv run python scripts/build_articles_data.py
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import article_store
from lib import agency_coords, agency_display_name, agency_state, registry_by_id

ROOT = Path(__file__).resolve().parent.parent
TAGS = ROOT / "assets" / "tags.json"
OUT_PATH = ROOT / "docs" / "data" / "articles_data.json"


def tag_namespace(tag: str) -> str:
    return tag.split(":", 1)[0] if ":" in tag else "other"


# published_at arrives in whatever shape the source page exposed: ISO
# datetimes with or without an offset, bare ISO dates, US
# "M/D/YYYY h:mm:ss AM/PM" strings, and human-readable "Sep 22nd 2021"
# from the Genetec/Vigilant vendor-newsroom scrapes. A plain string sort
# on that mix is nonsense -- it compares leading characters, so "Sep 22nd
# 2021" and "8/25/2025" both sorted above every 2026 ISO date and took
# over the top of the "newest first" list. Normalize to a UTC timestamp
# and sort on that instead.
_TEXT_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y",
    "%b %d %Y",
    "%B %d %Y",
)

# "22nd" -> "22": strptime has no ordinal-suffix directive.
_ORDINAL_RE = re.compile(r"\b(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)


def parse_timestamp(value) -> float | None:
    """Best-effort parse of a published_at/fetched_at string to epoch seconds.

    Returns None when the value is missing or in a shape we don't
    recognize; callers sort those entries last rather than guessing.
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        dt = None
        stripped = _ORDINAL_RE.sub(r"\1", raw)
        for fmt in _TEXT_FORMATS:
            try:
                dt = datetime.strptime(stripped, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    # Naive values carry no zone; treat as UTC so they stay comparable
    # with the offset-aware ones rather than raising on comparison.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def sort_key(entry: dict) -> tuple:
    """Newest first, undated last.

    Tuple order: has-a-date flag, published timestamp, fetched-at
    timestamp, article_id. Everything is compared descending (callers
    pass reverse=True), so the flag puts dated articles above undated
    ones, and undated ones fall back to when we crawled them -- the
    closest proxy we have -- instead of landing in article_id order.
    """
    published = entry.get("published_ts")
    fetched = parse_timestamp(entry.get("fetched_at"))
    return (
        published is not None,
        published if published is not None else 0.0,
        fetched if fetched is not None else 0.0,
        entry.get("article_id") or "",
    )


def main() -> int:
    registry = article_store.load_registry()
    tags_doc = json.loads(TAGS.read_text())
    reg_by_id = registry_by_id()

    tag_descriptions: dict[str, str] = {}
    for ns in ("topics", "editorial"):
        for tag_id, spec in (tags_doc.get(ns) or {}).items():
            tag_descriptions[tag_id] = (spec or {}).get("description", "")

    articles = []
    tag_counts: dict[str, int] = {}

    for entry in registry:
        # Off-topic entries (the curator's not-about-ALPR verdict) stay in
        # the registry as dedup tombstones but never reach the public viewer
        # — skip them here so they're absent from the article list, tag/
        # source counts, and the per-agency index alike.
        if entry.get("curation_status") == "off_topic":
            continue
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

        # Each id in primary_subject_agency_ids gets a map pin. Vendor-
        # primary entries (e.g. Flock Safety year-in-review pieces) get
        # bucketed off-coast in the viewer instead of at the vendor HQ
        # to avoid misleading geographic clustering.
        primary_subjects = []
        for primary_id in entry.get("primary_subject_agency_ids") or []:
            ag = reg_by_id.get(primary_id)
            if not ag:
                continue
            lat, lng = agency_coords(ag)
            primary_subjects.append({
                "agency_id": primary_id,
                "name": agency_display_name(ag, fallback=primary_id),
                "state": agency_state(ag),
                "lat": lat,
                "lng": lng,
                "is_vendor": ag.get("agency_role") == "vendor",
            })

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
            "published_ts": parse_timestamp(entry.get("published_at")),
            "fetched_at": entry.get("fetched_at"),
            "summary": entry.get("summary"),
            "key_quotes": entry.get("key_quotes") or [],
            "tags": tags,
            "agencies": agencies,
            "primary_subject_agencies": primary_subjects,
            "stance": entry.get("stance"),
            "tier": entry.get("tier"),
            "wayback_url": entry.get("wayback_url"),
            "paths": {
                k: paths.get(k) for k in ("html", "txt", "pdf", "meta")
                if paths.get(k)
            },
        })

    # Newest first on the normalized timestamp; undated entries sink to
    # the bottom. See sort_key/parse_timestamp above for why the raw
    # published_at string can't be sorted directly.
    articles.sort(key=sort_key, reverse=True)

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

    # Per-agency index. Lets the sharing_map page show an "Articles (N)"
    # link and the report page render a press-coverage list without
    # having to load and traverse the full articles array. Includes the
    # registry slug so callers using ?agency=<slug> URL params (report,
    # justifications) can resolve the lookup without an extra fetch.
    articles_by_agency: dict[str, dict] = {}
    for a in articles:
        primary_ids = set()
        for p in (a.get("primary_subject_agencies") or []):
            pid = p.get("agency_id")
            if pid:
                primary_ids.add(pid)
        seen_ids: set[str] = set()
        for ag in (a.get("agencies") or []):
            aid = ag.get("agency_id")
            if not aid or aid in seen_ids:
                continue
            seen_ids.add(aid)
            entry = articles_by_agency.setdefault(aid, {
                "agency_id": aid,
                "name": ag.get("name"),
                "state": ag.get("state"),
                "articles": [],
            })
            entry["articles"].append({
                "article_id": a["article_id"],
                "title": a["title"],
                "url": a["url"],
                "source_domain": a.get("source_domain"),
                "published_at": a.get("published_at"),
                "published_ts": a.get("published_ts"),
                "fetched_at": a.get("fetched_at"),
                "is_primary": aid in primary_ids,
            })
        # Primary subjects are usually in the agencies list, but defend
        # against the case where they're not — we still want them
        # indexed so the report page can find their press coverage.
        for pid in primary_ids - seen_ids:
            ag_entry = reg_by_id.get(pid)
            if not ag_entry:
                continue
            entry = articles_by_agency.setdefault(pid, {
                "agency_id": pid,
                "name": agency_display_name(ag_entry, fallback=pid),
                "state": agency_state(ag_entry),
                "articles": [],
            })
            entry["articles"].append({
                "article_id": a["article_id"],
                "title": a["title"],
                "url": a["url"],
                "source_domain": a.get("source_domain"),
                "published_at": a.get("published_at"),
                "published_ts": a.get("published_ts"),
                "fetched_at": a.get("fetched_at"),
                "is_primary": True,
            })

    for aid, entry in articles_by_agency.items():
        reg_entry = reg_by_id.get(aid) or {}
        slug = reg_entry.get("active_slug") or reg_entry.get("slug")
        if slug:
            entry["slug"] = slug
        entry["articles"].sort(key=sort_key, reverse=True)
        # primary_count is "articles this piece is substantively *about*"
        # — used by sharing_map's "Articles (N)" link so the badge
        # matches what articles.html actually filters to. The wider
        # entry["articles"] list (primary + mentions) stays available
        # for the report page, which surfaces peripheral coverage too.
        entry["primary_count"] = sum(
            1 for a in entry["articles"] if a.get("is_primary")
        )

    out = {
        "articles": articles,
        "tags": tags_out,
        "sources": sources_out,
        "namespace_order": namespace_order,
        "articles_by_agency": articles_by_agency,
        "counts": {
            "total_articles": len(articles),
            "total_tags": len(tags_out),
            "agencies_with_articles": len(articles_by_agency),
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, sort_keys=False) + "\n")
    print(f"wrote {OUT_PATH} — {len(articles)} articles, "
          f"{len(tags_out)} unique tags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
