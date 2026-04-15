"""
Shared utilities for sm-alpr scripts.

Classification and identity data belongs in the agency registry
(assets/agency_registry.json), not here. This module provides
lookup helpers against the registry.
"""

import json
from pathlib import Path

REGISTRY_PATH = Path("assets/agency_registry.json")

_registry_cache = None


def load_registry():
    """Load the agency registry. Cached after first call."""
    global _registry_cache
    if _registry_cache is None:
        if not REGISTRY_PATH.exists():
            _registry_cache = []
        else:
            _registry_cache = json.loads(REGISTRY_PATH.read_text())
    return _registry_cache


def registry_by_id():
    """Return agency_id -> registry entry lookup."""
    return {e["agency_id"]: e for e in load_registry()}


def registry_by_slug():
    """Return slug -> registry entry lookup. Backward compat."""
    return {e["slug"]: e for e in load_registry()}


DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")


def crawl_status(entry, data_dir=None):
    """Derive crawled/crawled_date from the filesystem for a registry entry.

    Checks all directories in ``flock_slugs`` and returns the most recent
    crawl date found across any of them.

    Returns (crawled: bool, crawled_date: str|None).
    """
    data_dir = data_dir or DEFAULT_DATA_DIR
    latest = None
    slugs = entry.get("flock_slugs", []) or []
    # Always check the slug field as fallback (it's the directory name)
    base_slug = entry.get("slug")
    if base_slug and base_slug not in slugs:
        slugs = list(slugs) + [base_slug]
    for slug in slugs:
        slug_dir = data_dir / slug
        if slug_dir.is_dir():
            jsons = sorted(slug_dir.glob("*.json"))
            if jsons:
                date = jsons[-1].stem  # e.g. "2026-04-10"
                if latest is None or date > latest:
                    latest = date
    return (True, latest) if latest else (False, None)


def has_tag(entry, tag):
    """Check if a registry entry has a specific tag."""
    return tag in entry.get("tags", [])



def agency_active_slug(entry, fallback=None):
    """Return the current active Flock portal slug for a registry entry."""
    if entry.get("flock_active_slug"):
        return entry["flock_active_slug"]
    slugs = entry.get("flock_slugs", [])
    if slugs:
        return slugs[0]
    return fallback or entry.get("slug", "?")


def agency_display_name(entry, fallback=None):
    """Return the best display name for a registry entry."""
    if entry.get("display_name"):
        return entry["display_name"]
    names = entry.get("flock_names", [])
    if names:
        return names[0]
    return fallback or entry.get("slug", "?")


def resolve_agency(*, slug=None, name=None):
    """Find a registry entry by Flock slug or display name.

    Pass exactly one of ``slug`` or ``name``. Searches ``flock_slugs``
    (including legacy slugs) and ``flock_names`` respectively.

    Returns the full registry entry dict, or None if not found.
    """
    for e in load_registry():
        if slug is not None:
            if slug in e.get("flock_slugs", []) or slug == e.get("slug"):
                return e
        if name is not None:
            if name in e.get("flock_names", []):
                return e
    return None


# Backward-compat wrappers — callers migrating to resolve_agency()

def flock_name_to_slug(name):
    """Look up a Flock display name, return active slug. Use resolve_agency() instead."""
    entry = resolve_agency(name=name)
    return agency_active_slug(entry) if entry else None


# ── Text parsing utilities ──

import re


def name_to_slug(name):
    """Heuristic: derive a plausible Flock portal slug from a display name.

    This is a guess — the result may not be a real portal URL. Use
    resolve_agency() to check if it's in the registry first.
    """
    s = name.strip().lower()
    s = re.sub(r"\(acso\)", "", s)
    s = re.sub(r"\(ca\)", "ca", s)
    s = re.sub(r"\(smcso\)", "", s)
    s = re.sub(r"['''\u2019]s\b", "s", s)
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s

_EXPECTS_CONTINUATION = re.compile(
    r"University of California$",
    re.IGNORECASE,
)


def parse_org_list(orgs_text):
    """Parse a comma-separated org list from Flock portal text.

    Returns a list of display names. Use resolve_agency(name=n) to look
    up each name in the registry.
    """
    if not orgs_text:
        return []
    _, _, body = orgs_text.partition("\n\n")
    if not body:
        _, _, body = orgs_text.partition("\n")
    raw = [n.strip() for n in body.split(", ") if n.strip()]
    names = []
    for part in raw:
        if names and _EXPECTS_CONTINUATION.search(names[-1]):
            names[-1] = f"{names[-1]}, {part}"
        else:
            names.append(part)
    return names
