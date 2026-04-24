#!/usr/bin/env python3
"""
Build the data bundle for the ALPR contract-status map.

Reads hand-curated source files under data/contracts/:
  - events.json    (flat list of timeline events)
  - articles.json  (article metadata, referenced by events)

Agency identity, name, and coordinates are resolved from
assets/agency_registry.json via the event's `agency_id` (registry UUID).
The registry is the central identity DB — any agency that lacks a registry
entry must be added there first (see scripts/seed_contract_registry.py for
a bulk-seeding helper, or scripts/build_agency_registry.py for the general
workflow).

When the registry entry has a scraped Flock transparency portal under
assets/transparency.flocksafety.com/<slug>/, the latest snapshot (plus an
older one for deltas) is attached to the agency payload.

Usage:
  uv run python scripts/build_contract_map.py
  uv run python scripts/build_contract_map.py --out path/to/out.json
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402
    agency_active_slug,
    agency_coords,
    agency_display_name,
    agency_state,
    load_registry,
    portal_jsons,
    registry_by_id,
)

SRC_DIR = Path("data/contracts")
DEFAULT_OUT = Path("docs/data/contract_map_data.json")
TRANSPARENCY_DIR = Path("assets/transparency.flocksafety.com")

VALID_EVENT_TYPES = {
    "signed",
    "considering",
    "reviewing",
    "paused",
    "canceled",
    "reinstated",
}

# Severity ordering for the "overall" marker color. Lower index = more severe.
STATUS_SEVERITY = [
    "canceled",
    "paused",
    "reviewing",
    "considering",
    "reinstated",
    "signed",
]

KNOWN_VENDORS = {"flock", "vigilant", "axon-fusus", "rekor", "other"}

# Reason codes for why an event happened. Attached to events as `reasons: [...]`.
# The UI renders each as a colored chip with a short letter-badge icon.
REASONS = {
    "federal-access": {
        "label": "Federal / ICE access",
        "icon": "F",
        "color": "#b91c1c",
        "description": "Data accessed by federal immigration enforcement (ICE, CBP, DHS) or shared without local authorization.",
    },
    "unauthorized-access": {
        "label": "Unauthorized searches",
        "icon": "U",
        "color": "#c2410c",
        "description": "Out-of-state, federal, or third-party agencies ran searches against the jurisdiction's data without approval.",
    },
    "vendor-misconduct": {
        "label": "Vendor misconduct",
        "icon": "V",
        "color": "#7f1d1d",
        "description": "Flock (or vendor) conduct: unauthorized installations, misrepresentation to the city, undisclosed pilots.",
    },
    "broad-disclosure": {
        "label": "Broad disclosure clause",
        "icon": "D",
        "color": "#6d28d9",
        "description": "Contract language permits the vendor to disclose data to any government entity or third party.",
    },
    "sanctuary-conflict": {
        "label": "Sanctuary conflict",
        "icon": "S",
        "color": "#b45309",
        "description": "Data use conflicts with the jurisdiction's sanctuary ordinance or non-cooperation policy.",
    },
    "data-breach": {
        "label": "Data breach / leak",
        "icon": "B",
        "color": "#9f1239",
        "description": "An actual breach or unauthorized exposure of collected data.",
    },
    "privacy-general": {
        "label": "Privacy concerns",
        "icon": "P",
        "color": "#475569",
        "description": "Catchall for general privacy concerns not captured by a more specific code.",
    },
}

# Continental US + Alaska + Hawaii + territories rough bounding box.
US_BBOX = {"lat_min": 17.0, "lat_max": 72.0, "lng_min": -180.0, "lng_max": -64.0}

MIN_SNAPSHOT_DELTA_DAYS = 14


def load_source(name, src_dir=None):
    p = (src_dir or SRC_DIR) / name
    with open(p) as f:
        return json.load(f)


def parse_event_date(raw):
    """Return (sortable_tuple, display_string, is_full_date)."""
    if raw is None or raw == "":
        return (9999, 99, 99), "undated", False
    parts = raw.split("-")
    try:
        if len(parts) == 3:
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            dt.date(y, m, d)
            return (y, m, d), raw, True
        if len(parts) == 2:
            y, m = int(parts[0]), int(parts[1])
            if not (1 <= m <= 12):
                raise ValueError(f"bad month: {m}")
            return (y, m, 99), raw, False
        if len(parts) == 1:
            y = int(parts[0])
            return (y, 99, 99), raw, False
    except ValueError as e:
        raise ValueError(f"invalid date {raw!r}: {e}")
    raise ValueError(f"invalid date format {raw!r}")


def validate(events, articles, reg_by_id):
    errors = []
    warnings = []

    article_ids = set()
    for art in articles:
        aid = art.get("id")
        if not aid:
            errors.append(f"article missing id: {art}")
            continue
        if aid in article_ids:
            errors.append(f"duplicate article id: {aid}")
        article_ids.add(aid)
        for f in ("url", "title", "outlet", "published_date"):
            if not art.get(f):
                errors.append(f"article {aid}: missing required field {f!r}")

    for ev in events:
        agid = ev.get("agency_id")
        if not agid:
            errors.append(f"event missing agency_id: {ev}")
            continue
        reg = reg_by_id.get(agid)
        if not reg:
            errors.append(
                f"event references unknown agency_id {agid!r} — add to registry "
                f"via scripts/seed_contract_registry.py or "
                f"scripts/build_agency_registry.py"
            )
            continue
        lat, lng = agency_coords(reg)
        if lat is None or lng is None:
            errors.append(f"event for {agid}: registry entry lacks lat/lng")
        else:
            if not (US_BBOX["lat_min"] <= lat <= US_BBOX["lat_max"]):
                errors.append(f"agency {agid}: lat {lat} outside US bbox")
            if not (US_BBOX["lng_min"] <= lng <= US_BBOX["lng_max"]):
                errors.append(f"agency {agid}: lng {lng} outside US bbox")
        t = ev.get("type")
        if t not in VALID_EVENT_TYPES:
            errors.append(f"event for {agid}: invalid type {t!r}")
        v = ev.get("vendor")
        if v and v not in KNOWN_VENDORS:
            warnings.append(f"event for {agid}: unknown vendor {v!r}")
        try:
            parse_event_date(ev.get("date"))
        except ValueError as e:
            errors.append(f"event for {agid}: {e}")
        for art_id in ev.get("article_ids") or []:
            if art_id not in article_ids:
                errors.append(
                    f"event for {agid}: unknown article_id {art_id!r}"
                )
        for r in ev.get("reasons") or []:
            if r not in REASONS:
                errors.append(f"event for {agid}: unknown reason {r!r}")

    return errors, warnings


def snapshot_from_portal_json(path):
    with open(path) as f:
        d = json.load(f)
    return {
        "as_of": d.get("archived_date"),
        "camera_count": d.get("camera_count"),
        "vehicles_detected_30d": d.get("vehicles_detected_30d"),
        "hotlist_hits_30d": d.get("hotlist_hits_30d"),
        "searches_30d": d.get("searches_30d"),
        "data_retention_days": d.get("data_retention_days"),
    }


def load_flock_snapshots(reg_entry):
    """If the registry entry has a crawled Flock portal, return
    (latest_snapshot, prior_snapshot_or_none, slug). Else (None, None, None)."""
    slug = reg_entry.get("flock_active_slug")
    if not slug:
        return None, None, None
    portal_dir = TRANSPARENCY_DIR / slug
    if not portal_dir.is_dir():
        return None, None, None
    jsons = portal_jsons(portal_dir)
    if not jsons:
        return None, None, None
    latest = snapshot_from_portal_json(jsons[-1])
    prior = None
    if len(jsons) >= 2:
        latest_date = latest.get("as_of")
        try:
            latest_d = dt.date.fromisoformat(latest_date) if latest_date else None
        except ValueError:
            latest_d = None
        if latest_d:
            for candidate in jsons[:-1]:
                try:
                    cand_d = dt.date.fromisoformat(candidate.stem)
                except ValueError:
                    continue
                if (latest_d - cand_d).days >= MIN_SNAPSHOT_DELTA_DAYS:
                    prior = snapshot_from_portal_json(candidate)
                    break
    return latest, prior, slug


def derive_status(agency_events):
    """Per vendor, status = latest event type. Returns (per_vendor, overall)."""
    by_vendor = {}
    for ev in agency_events:
        v = ev.get("vendor") or "other"
        by_vendor.setdefault(v, []).append(ev)
    per_vendor = {}
    for v, evs in by_vendor.items():
        evs_sorted = sorted(evs, key=lambda e: parse_event_date(e.get("date"))[0])
        latest = evs_sorted[-1]
        per_vendor[v] = {
            "status": latest["type"],
            "last_event_date": latest.get("date"),
        }
    overall = None
    for s in STATUS_SEVERITY:
        if any(p["status"] == s for p in per_vendor.values()):
            overall = s
            break
    return per_vendor, overall


def build(events_src, articles_src, reg_by_id):
    articles_by_id = {a["id"]: a for a in articles_src}

    events_by_agency = {}
    for ev in events_src:
        events_by_agency.setdefault(ev["agency_id"], []).append(ev)

    markers = []
    agencies_out = {}
    for aid, agency_events in events_by_agency.items():
        reg = reg_by_id[aid]  # validated above
        name = agency_display_name(reg)
        state = agency_state(reg)
        lat, lng = agency_coords(reg)
        geo = reg.get("geo") or {}
        city = geo.get("name") if geo.get("kind") == "place" else None

        # Vendors = union of vendors from all this agency's events
        vendors = sorted({ev.get("vendor") for ev in agency_events if ev.get("vendor")})

        per_vendor, overall = derive_status(agency_events)
        sorted_events = sorted(
            agency_events,
            key=lambda e: parse_event_date(e.get("date"))[0],
            reverse=True,
        )

        # Dedup articles in event-order (newest event first)
        article_ids_seen = []
        seen = set()
        for ev in sorted_events:
            for art_id in ev.get("article_ids") or []:
                if art_id in seen:
                    continue
                seen.add(art_id)
                article_ids_seen.append(art_id)
        agency_articles = [articles_by_id[a_id] for a_id in article_ids_seen if a_id in articles_by_id]

        # Flock snapshot enrichment (if registry has a crawled portal)
        snapshot, snapshot_prior, flock_slug = load_flock_snapshots(reg)
        flock_portal_url = (
            f"https://transparency.flocksafety.com/{flock_slug}"
            if flock_slug else None
        )

        payload = {
            "id": aid,
            "name": name,
            "city": city,
            "state": state,
            "lat": lat,
            "lng": lng,
            "vendors": vendors,
            "events": sorted_events,
            "articles": agency_articles,
            "status_by_vendor": per_vendor,
            "status_overall": overall,
        }
        if snapshot:
            payload["flock_portal_url"] = flock_portal_url
            payload["flock_active_slug"] = flock_slug
            payload["flock_snapshot"] = snapshot
            if snapshot_prior:
                payload["flock_snapshot_prior"] = snapshot_prior
        agencies_out[aid] = payload

        marker = {
            "id": aid,
            "lat": lat,
            "lng": lng,
            "name": name,
            "full_name": name,
            "city": city,
            "state": state,
            "vendors": vendors,
            "status_overall": overall,
            "status_by_vendor": per_vendor,
            "event_count": len(agency_events),
            "article_count": len(agency_articles),
            "camera_count": (snapshot or {}).get("camera_count"),
        }
        markers.append(marker)

    return {
        "markers": markers,
        "agencies": agencies_out,
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "meta": {
            "status_severity": STATUS_SEVERITY,
            "known_vendors": sorted(KNOWN_VENDORS),
            "reasons": REASONS,
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT), type=Path)
    ap.add_argument(
        "--src",
        default=str(SRC_DIR),
        type=Path,
        help="Source dir with events.json/articles.json",
    )
    args = ap.parse_args()

    src_dir = args.src
    events = load_source("events.json", src_dir)
    articles = load_source("articles.json", src_dir)

    load_registry()
    reg_by_id = registry_by_id()

    errors, warnings = validate(events, articles, reg_by_id)
    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    bundle = build(events, articles, reg_by_id)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(bundle, f, indent=2)
    print(
        f"wrote {args.out}: {len(bundle['markers'])} agencies, "
        f"{sum(len(a['events']) for a in bundle['agencies'].values())} events, "
        f"{len({a['id'] for g in bundle['agencies'].values() for a in g['articles']})} articles"
    )


if __name__ == "__main__":
    main()
