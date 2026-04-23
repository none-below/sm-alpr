#!/usr/bin/env python3
"""
Build per-agency change history from dated transparency snapshots.

For each agency with ≥1 snapshot, writes docs/data/history/<slug>.json
containing an events list computed by diffing consecutive snapshots.

Also writes docs/data/history/index.json — a lightweight directory of
which agencies have history and how much.

Reads:
  - assets/transparency.flocksafety.com/<slug>/*.json
  - assets/agency_registry.json (for display names / agency_id)

Writes:
  - docs/data/history/<slug>.json (one per agency)
  - docs/data/history/index.json

Usage:
  uv run python scripts/build_history.py
"""

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RECENT_WINDOW_DAYS = 90

sys.path.insert(0, str(Path(__file__).parent))
from lib import portal_jsons, registry_by_slug, resolve_agency

DATA_DIR = Path("assets/transparency.flocksafety.com")
OUT_DIR = Path("docs/data/history")

# Rename-map: normalize legacy scraper field names to current names so a
# field rename doesn't produce a false "everything changed" event.
FIELD_ALIASES = {
    "shared_org_names": "sharing_outbound",
    "orgs_sharing_with_names": "sharing_inbound",
}

# Observational / identity fields — not policy, don't track.
SKIP_FIELDS = {
    "archived_date",
    "crawled_slug",
    "crawled_name",
    "vehicles_detected_30d",
    "hotlist_hits_30d",
    "searches_30d",
    "shared_org_slugs",
    "orgs_sharing_with_slugs",
    "crawled_at",
    # High-churn audit-log payload: list of per-search records with
    # timestamps. Diffing this as a set would produce noisy add/remove
    # events on every scrape. Consumers that want audit-log analysis
    # should read the raw portal JSON directly.
    "search_audit_csv",
}

SCALAR_FIELDS = {"data_retention_days", "camera_count"}

SET_FIELDS = {"sharing_outbound", "sharing_inbound"}

# Fields stored as a comma-separated string but semantically a set.
COMMA_SET_FIELDS = {"hotlists_alerted_on"}

TEXT_FIELDS = {
    "access_policy",
    "hotlist_policy",
    "acceptable_use_policy",
    "prohibited_uses",
    "whats_detected",
    "whats_not_detected",
    "sharing_with_partners",
    "sharing_restrictions",
    "sb54",
    "sharing_info",
    "success_stories",
    "policy_info",
    "alpr_policy",
    "additional_info",
}


_UNKNOWN_FIELDS_WARNED: set[str] = set()


def _split_comma_set(val):
    if not val:
        return []
    return sorted({p.strip() for p in val.split(",") if p.strip()})


def _normalize(snapshot: dict) -> dict:
    out = {}
    for key, val in snapshot.items():
        if key in SKIP_FIELDS:
            continue
        out[FIELD_ALIASES.get(key, key)] = val
    return out


def _diff_pair(prev: dict, curr: dict, prev_date: str, curr_date: str) -> list[dict]:
    events = []
    prev_n = _normalize(prev)
    curr_n = _normalize(curr)
    keys = sorted(set(prev_n) | set(curr_n))
    for key in keys:
        before = prev_n.get(key)
        after = curr_n.get(key)
        if before == after:
            continue
        if key in SCALAR_FIELDS:
            events.append({
                "date": curr_date,
                "prev_date": prev_date,
                "field": key,
                "kind": "scalar",
                "before": before,
                "after": after,
            })
        elif key in SET_FIELDS:
            before_set = set(before or [])
            after_set = set(after or [])
            added = sorted(after_set - before_set)
            removed = sorted(before_set - after_set)
            if not added and not removed:
                continue
            events.append({
                "date": curr_date,
                "prev_date": prev_date,
                "field": key,
                "kind": "set",
                "added": added,
                "removed": removed,
            })
        elif key in COMMA_SET_FIELDS:
            before_set = set(_split_comma_set(before))
            after_set = set(_split_comma_set(after))
            added = sorted(after_set - before_set)
            removed = sorted(before_set - after_set)
            if not added and not removed:
                continue
            events.append({
                "date": curr_date,
                "prev_date": prev_date,
                "field": key,
                "kind": "set",
                "added": added,
                "removed": removed,
            })
        elif key in TEXT_FIELDS:
            events.append({
                "date": curr_date,
                "prev_date": prev_date,
                "field": key,
                "kind": "text",
                "before": before or "",
                "after": after or "",
            })
        else:
            # Unclassified field changed — emit once per (script-run, field)
            # so scraper schema drift is visible in CI logs without drowning
            # output when a field churns across many agencies.
            if key not in _UNKNOWN_FIELDS_WARNED:
                _UNKNOWN_FIELDS_WARNED.add(key)
                print(f"  WARN: unclassified field '{key}' changed — "
                      f"add to SCALAR/SET/COMMA_SET/TEXT/SKIP_FIELDS in "
                      f"build_history.py to track",
                      file=sys.stderr)
    return events


def build_agency_history(slug_dir: Path, reg_by_slug: dict) -> dict | None:
    jsons = portal_jsons(slug_dir)
    if not jsons:
        return None

    snapshots = [p.stem for p in jsons]
    entry = reg_by_slug.get(slug_dir.name, {})

    latest = json.loads(jsons[-1].read_text())
    display_name = entry.get("display_name") or latest.get("crawled_name") or slug_dir.name

    events = []
    for i in range(1, len(jsons)):
        prev = json.loads(jsons[i - 1].read_text())
        curr = json.loads(jsons[i].read_text())
        events.extend(_diff_pair(prev, curr, jsons[i - 1].stem, jsons[i].stem))

    events.sort(key=lambda e: (e["date"], e["field"]), reverse=True)

    return {
        "slug": slug_dir.name,
        "agency_id": entry.get("agency_id"),
        "display_name": display_name,
        "first_seen": snapshots[0],
        "last_seen": snapshots[-1],
        "snapshots": snapshots,
        "events": events,
    }


def _resolve_name(name: str) -> dict:
    """Return {name, slug, agency_id} for a sharing target, with nulls on miss."""
    entry = resolve_agency(name=name)
    if entry:
        return {"name": name, "slug": entry.get("slug"), "agency_id": entry.get("agency_id")}
    return {"name": name, "slug": None, "agency_id": None}


def _build_changelog(all_histories: list[dict], oldest_snapshot: str | None) -> dict:
    """Flatten recent events into a per-source-slug map for map-client use.

    Only events with date >= today - RECENT_WINDOW_DAYS are included.
    Sharing targets are resolved to slug+agency_id so the map can draw
    edges (including ghost edges to former targets).
    """
    today = date.today()
    cutoff = today - timedelta(days=RECENT_WINDOW_DAYS)
    cutoff_str = cutoff.isoformat()

    by_slug: dict[str, dict] = {}

    for hist in all_histories:
        slug = hist["slug"]
        entry = {
            "display_name": hist["display_name"],
            "agency_id": hist["agency_id"],
            "sharing_outbound_added": [],
            "sharing_outbound_removed": [],
            "sharing_inbound_added": [],
            "sharing_inbound_removed": [],
            "policy_events": [],
        }
        has_any = False
        for ev in hist["events"]:
            if ev["date"] < cutoff_str:
                continue
            field = ev["field"]
            if field in ("sharing_outbound", "sharing_inbound") and ev["kind"] == "set":
                added_key = f"{field}_added"
                removed_key = f"{field}_removed"
                for name in ev.get("added", []):
                    entry[added_key].append({**_resolve_name(name), "date": ev["date"]})
                    has_any = True
                for name in ev.get("removed", []):
                    entry[removed_key].append({**_resolve_name(name), "date": ev["date"]})
                    has_any = True
            else:
                entry["policy_events"].append({
                    "date": ev["date"],
                    "field": field,
                    "kind": ev["kind"],
                    **({"before": ev["before"], "after": ev["after"]}
                       if ev["kind"] in ("scalar", "text")
                       else {"added": ev.get("added", []), "removed": ev.get("removed", [])}),
                })
                has_any = True
        if has_any:
            by_slug[slug] = entry

    tracking_days = None
    if oldest_snapshot:
        try:
            tracking_days = (today - date.fromisoformat(oldest_snapshot)).days
        except ValueError:
            pass

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": RECENT_WINDOW_DAYS,
        "cutoff_date": cutoff_str,
        "oldest_snapshot": oldest_snapshot,
        "tracking_days": tracking_days,
        "window_complete": tracking_days is not None and tracking_days >= RECENT_WINDOW_DAYS,
        "by_slug": by_slug,
    }


def main():
    if not DATA_DIR.is_dir():
        print(f"No data directory at {DATA_DIR}", file=sys.stderr)
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    reg_by_slug = registry_by_slug()
    index = {}
    total_events = 0
    all_histories = []
    oldest_snapshot = None

    for slug_dir in sorted(DATA_DIR.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue
        history = build_agency_history(slug_dir, reg_by_slug)
        if history is None:
            continue
        out_path = OUT_DIR / f"{slug_dir.name}.json"
        out_path.write_text(json.dumps(history, indent=2) + "\n")
        index[slug_dir.name] = {
            "agency_id": history["agency_id"],
            "display_name": history["display_name"],
            "first_seen": history["first_seen"],
            "last_seen": history["last_seen"],
            "snapshots": len(history["snapshots"]),
            "events": len(history["events"]),
        }
        total_events += len(history["events"])
        all_histories.append(history)
        if oldest_snapshot is None or history["first_seen"] < oldest_snapshot:
            oldest_snapshot = history["first_seen"]

    index_path = OUT_DIR / "index.json"
    index_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agencies": index,
    }, indent=2) + "\n")

    changelog = _build_changelog(all_histories, oldest_snapshot)
    changelog_path = Path("docs/data/agency_changelog.json")
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    changelog_path.write_text(json.dumps(changelog, indent=2) + "\n")

    print(f"Wrote history for {len(index)} agencies ({total_events} events total) to {OUT_DIR}/")
    print(f"Wrote {RECENT_WINDOW_DAYS}d changelog for {len(changelog['by_slug'])} agencies to {changelog_path}")


if __name__ == "__main__":
    main()
