#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Build the operations dashboard data: docs/data/dashboard.json.

Surfaces the "backlog" worth acting on:

  - crawl backlog — Flock transparency portals due for a re-crawl, judged by
    the same staleness timer the crawler uses (latest .txt *attempt* older
    than the refresh max-age). Per-agency capture/attempt dates are emitted
    raw; dashboard.js recomputes age against the viewer's current date, so the
    staleness view stays live between rebuilds rather than baking in age_days.

  - article backlog — counts by curation_status (enriched / mechanical /
    needs_review), failed-fetch URLs, and the last discovery run.

Reads only filenames/dates under assets/transparency.flocksafety.com (never
scraped page content) and the curated article-registry JSON. Writes
docs/data/dashboard.json — a build artifact (gitignored), regenerated on
every deploy and Flock refresh.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flock_transparency import (  # noqa: E402
    DEFAULT_DATA_DIR,
    STALE_DAYS,
    crawled_slugs_on_disk,
    latest_capture_attempt_date,
    latest_capture_date,
)
from lib import (  # noqa: E402
    FAILED_FILE,
    agency_display_name,
    agency_state,
    load_registry,
    portal_jsons,
)
from article_store import load_registry as load_article_registry  # noqa: E402

# Width of the throughput trend series the dashboard sparkline renders.
THROUGHPUT_WINDOW_DAYS = 14

# The rolling refresh workflow runs the crawler with --max-age 7 by default
# (.github/workflows/refresh-flock-data.yml), so an agency is "due" once its
# latest capture attempt is at least this many days old. STALE_DAYS (14) is
# the crawler's hard fallback when --max-age isn't passed — shown as the
# "overdue" threshold.
REFRESH_MAX_AGE_DEFAULT = 7

REPO_ROOT = Path(__file__).resolve().parent.parent
FAILED_URLS = REPO_ROOT / "assets" / "articles" / ".failed_urls.json"
CRAWL_STATE = REPO_ROOT / "assets" / "articles" / ".crawl_state.json"
OUT = REPO_ROOT / "docs" / "data" / "dashboard.json"


def _slug_to_entry():
    """Map every on-disk portal slug (base + Flock variants) to its registry entry.

    Crawled directories are named by Flock slug, which may be a flock_slugs
    variant rather than the registry's base slug — index all of them so the
    display name resolves regardless of which one a capture landed under.
    """
    by_slug = {}
    for entry in load_registry():
        candidates = [entry.get("slug"), entry.get("flock_active_slug")]
        candidates += entry.get("flock_slugs") or []
        for slug in candidates:
            if slug and slug not in by_slug:
                by_slug[slug] = entry
    return by_slug


def _iso_or_none(d):
    return d.isoformat() if d else None


def build_throughput(data_dir, slugs):
    """Captures landed per day over the trailing window.

    A new dated .json is only written when a portal's content changed, so this
    is the pipeline's *useful-output* rate — and, since the cron only fires the
    crawler a fraction of the times it's scheduled, the binding constraint on
    how fast the whole population gets refreshed. The effective cycle (days to
    cover everyone once) falls out as total_agencies / captures-per-day.
    """
    today = date.today()
    per_date = {}
    for slug in slugs:
        for j in portal_jsons(data_dir / slug):
            try:
                d = date.fromisoformat(j.stem)
            except ValueError:
                continue
            age = (today - d).days
            if 0 <= age < THROUGHPUT_WINDOW_DAYS:
                per_date[j.stem] = per_date.get(j.stem, 0) + 1
    # Dense oldest→today series so the sparkline shows zero-capture days too.
    series = [
        {"date": (today - timedelta(days=i)).isoformat(),
         "captures": per_date.get((today - timedelta(days=i)).isoformat(), 0)}
        for i in range(THROUGHPUT_WINDOW_DAYS - 1, -1, -1)
    ]

    def mean(n):
        window = series[-n:]
        return round(sum(x["captures"] for x in window) / n, 1) if window else 0

    return {"per_day": series, "mean_7d": mean(7), "mean_14d": mean(14)}


def build_quarantined(data_dir, slugs, by_slug):
    """Previously-captured agencies now parked in .failed_slugs.json.

    The crawler skips a quarantined slug on every run until a manual
    --retry-failed, so these silently fall out of rotation. The file is mostly
    http_404 probe-guesses that were never real portals; intersecting with
    crawled_slugs_on_disk (slugs that have a real prior capture) leaves only the
    genuinely-stuck agencies worth surfacing.
    """
    fs_path = data_dir / FAILED_FILE
    if not fs_path.is_file():
        return []
    try:
        failed = json.loads(fs_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    captured = set(slugs)
    out = []
    for slug, info in failed.items():
        if slug not in captured:
            continue  # never-real probe guess, not a stuck agency
        entry = by_slug.get(slug)
        out.append({
            "slug": slug,
            "name": agency_display_name(entry, fallback=slug) if entry else slug,
            "reason": info.get("reason") if isinstance(info, dict) else str(info),
            "since": info.get("date") if isinstance(info, dict) else None,
            "last_capture": _iso_or_none(latest_capture_date(slug, data_dir)),
        })
    out.sort(key=lambda x: x["last_capture"] or "")
    return out


def build_crawl():
    by_slug = _slug_to_entry()
    slugs = crawled_slugs_on_disk(DEFAULT_DATA_DIR)
    agencies = []
    total_captures = 0
    for slug in slugs:
        entry = by_slug.get(slug)
        captures = portal_jsons(DEFAULT_DATA_DIR / slug)
        total_captures += len(captures)
        agencies.append({
            "slug": slug,
            "name": agency_display_name(entry, fallback=slug) if entry else slug,
            "state": (agency_state(entry) if entry else None),
            # last successful parse (.json) vs last attempt (.txt). When the
            # attempt is newer than the capture, recent crawls are failing to
            # parse — the page flags that.
            "last_capture": _iso_or_none(latest_capture_date(slug, DEFAULT_DATA_DIR)),
            "last_attempt": _iso_or_none(latest_capture_attempt_date(slug, DEFAULT_DATA_DIR)),
            "captures": len(captures),
        })
    # Stalest first — the page re-sorts, but a sensible default helps anyone
    # eyeballing the raw JSON.
    agencies.sort(key=lambda a: a["last_attempt"] or "")

    throughput = build_throughput(DEFAULT_DATA_DIR, slugs)
    rate = throughput["mean_7d"]
    throughput["effective_cycle_days"] = round(len(agencies) / rate, 1) if rate else None

    return {
        "stale_days": STALE_DAYS,
        "refresh_max_age_days": REFRESH_MAX_AGE_DEFAULT,
        "total_agencies": len(agencies),
        "total_captures": total_captures,
        "throughput": throughput,
        "quarantined": build_quarantined(DEFAULT_DATA_DIR, slugs, by_slug),
        "agencies": agencies,
    }


def build_articles():
    counts = {}
    total = 0
    for entry in load_article_registry():
        total += 1
        status = entry.get("curation_status") or "unknown"
        counts[status] = counts.get(status, 0) + 1

    failed = 0
    if FAILED_URLS.is_file():
        try:
            failed = len(json.loads(FAILED_URLS.read_text()))
        except (json.JSONDecodeError, OSError):
            failed = 0

    last_run, fetched = None, 0
    if CRAWL_STATE.is_file():
        try:
            state = json.loads(CRAWL_STATE.read_text())
            last_run = state.get("last_run")
            fetched = len(state.get("fetched") or [])
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "total": total,
        "enriched": counts.get("enriched", 0),
        "mechanical": counts.get("mechanical", 0),
        "needs_review": counts.get("needs_review", 0),
        "failed_urls": failed,
        "fetched": fetched,
        "last_crawl_run": last_run,
    }


def main():
    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "crawl": build_crawl(),
        "articles": build_articles(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2) + "\n")
    print(
        f"Wrote {OUT.relative_to(REPO_ROOT)} — "
        f"{data['crawl']['total_agencies']} agencies, "
        f"{data['articles']['total']} articles"
    )


if __name__ == "__main__":
    main()
