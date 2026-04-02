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


def registry_by_slug():
    """Return slug -> registry entry lookup."""
    return {e["slug"]: e for e in load_registry()}


def flock_name_to_slug(name):
    """Look up a Flock display name in the registry, return slug.

    Returns None if the name isn't in the registry. New names should be
    added to the registry via build_agency_registry.py, not derived here.
    """
    for e in load_registry():
        if e.get("flock_name") == name:
            return e["slug"]
        for aka_name in e.get("also_known_as_names", []):
            if aka_name == name:
                return e["slug"]
    return None


# ── Text parsing utilities ──
# These parse raw Flock portal text. They don't derive identity —
# that comes from the registry.

import re

_EXPECTS_CONTINUATION = re.compile(
    r"University of California$",
    re.IGNORECASE,
)


def parse_org_list(orgs_text):
    """Parse a comma-separated org list from Flock portal text.

    Returns a list of display names. Use flock_name_to_slug() to resolve
    each name to a registry slug.
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
