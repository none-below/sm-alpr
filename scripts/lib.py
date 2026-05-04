"""
Shared utilities for sm-alpr scripts.

Classification and identity data belongs in the agency registry
(assets/agency_registry.json), not here. This module provides
lookup helpers against the registry.
"""

import json
import re
from pathlib import Path

REGISTRY_PATH = Path("assets/agency_registry.json")

# Flock portal constants — shared by flock_transparency.py and slug_probe.py
BASE_URL = "https://transparency.flocksafety.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
FAILED_FILE = ".failed_slugs.json"


def load_json(path):
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def dedupe(items):
    seen = set()
    return [x for x in items if not (x in seen or seen.add(x))]

# Portal captures are named exactly YYYY-MM-DD.{txt,json} — nothing else.
# OCR sidecars (YYYY-MM-DD.pdf.HASH.txt) and any .pdf.HASH.json artifacts
# left over from past parser bugs must not be treated as captures.
_PORTAL_TXT_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.txt$")
_PORTAL_JSON_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.json$")


def portal_txts(slug_dir):
    """Sorted YYYY-MM-DD.txt portal captures in a slug dir (excludes sidecars)."""
    return sorted(p for p in slug_dir.iterdir() if _PORTAL_TXT_RE.match(p.name))


def portal_jsons(slug_dir):
    """Sorted YYYY-MM-DD.json portal parses in a slug dir (excludes sidecar artifacts)."""
    return sorted(p for p in slug_dir.iterdir() if _PORTAL_JSON_RE.match(p.name))


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
    """Return slug -> registry entry lookup. Backward compat.

    Entries without a slug (non-Flock agencies or not-yet-verified slugs)
    are excluded — use registry_by_id() instead for those.
    """
    return {e["slug"]: e for e in load_registry() if e.get("slug")}


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
            jsons = portal_jsons(slug_dir)
            if jsons:
                date = jsons[-1].stem  # e.g. "2026-04-10"
                if latest is None or date > latest:
                    latest = date
    return (True, latest) if latest else (False, None)


def has_tag(entry, tag):
    """Check if a registry entry has a specific tag."""
    return tag in entry.get("tags", [])


def agency_coords(entry):
    """Return (lat, lng) from entry's geo block, or (None, None)."""
    geo = entry.get("geo") or {}
    lat = geo.get("lat")
    lng = geo.get("lng")
    if lat is None or lng is None:
        return None, None
    return lat, lng


def agency_state(entry):
    """Return the 2-letter state code for an entry, or None."""
    geo = entry.get("geo") or {}
    return geo.get("state") or entry.get("state")



def agency_active_slug(entry, fallback=None):
    """Return the current active Flock portal slug for a registry entry."""
    if entry.get("flock_active_slug"):
        return entry["flock_active_slug"]
    slugs = entry.get("flock_slugs", [])
    if slugs:
        return slugs[0]
    return fallback or entry.get("slug", "?")


def agency_display_name(entry, fallback=None):
    """Return the best display name for a registry entry.

    Falls back to the LAST entry in flock_names so state transitions
    captured over time (e.g. "Agency X" → "Agency X [Inactive]") show
    the current name rather than the historical one. Explicit
    display_name still wins when set.
    """
    if entry.get("display_name"):
        return entry["display_name"]
    names = entry.get("flock_names", [])
    if names:
        return names[-1]
    return fallback or entry.get("slug", "?")


def resolve_agency(*, slug=None, name=None):
    """Find a registry entry by Flock slug or display name.

    Pass exactly one of ``slug`` or ``name``. Searches ``flock_slugs``
    (including legacy slugs) for slug lookups, and ``flock_names`` +
    ``aliases`` for name lookups.

    Returns the full registry entry dict, or None if not found.
    """
    for e in load_registry():
        if slug is not None:
            if slug in e.get("flock_slugs", []) or slug == e.get("slug"):
                return e
        if name is not None:
            if name in e.get("flock_names", []) or name in e.get("aliases", []):
                return e
    return None


# Mirror of docs/js/report.js → shortAgencyName(): strip role+state suffix
# from a display name when geo.name isn't available. Keep aligned.
_NAME_SUFFIX_STRIP = re.compile(
    r"\s+(CA|NV|AZ|OR|WA|ID|UT|NY|TX|FL|NH|MA|CT|NJ|PA|MD|VA|NC|SC|GA|"
    r"OH|MI|IL|IN|KY|TN|AL|MS|LA|AR|OK|MO|KS|NE|IA|MN|WI|ND|SD|MT|WY|"
    r"CO|NM|AK|HI|ME|VT|RI|DE|WV|DC)\s+"
    r"(PD|SO|SD|DA|FD|DPS|Police|Sheriff|Police Department|"
    r"Sheriff'?s? Office|District Attorney)\b.*$",
    re.IGNORECASE,
)


def canonical_place_name(entry):
    """Authoritative place name for an agency, mirroring report.js's
    shortAgencyName(): prefer geo.name (FIPS-derived), append 'County' if
    geo.kind is county and the name isn't already suffixed, else fall
    back to stripping the role+state suffix off agency_display_name().

    Returns None if no usable place name can be derived.
    """
    geo = entry.get("geo") or {}
    name = geo.get("name")
    if name:
        if geo.get("kind") == "county" and not re.search(r"\bCounty$", name, re.IGNORECASE):
            return f"{name} County"
        return name
    n = agency_display_name(entry) or ""
    n = _NAME_SUFFIX_STRIP.sub("", n).strip()
    return n or None


def agency_surface_forms(entry):
    """Every name an agency may be referenced by, for fuzzy matching.

    Combines display_name, flock_names (Flock portal's spellings), and
    aliases (article-found references). Order: longest-first so the
    matcher prefers specific forms ('San Mateo Police Department') over
    short ones ('San Mateo') that could collide across entries.
    """
    forms = set()
    if entry.get("display_name"):
        forms.add(entry["display_name"])
    forms.update(entry.get("flock_names", []) or [])
    forms.update(entry.get("aliases", []) or [])
    return sorted((f for f in forms if f and f.strip()),
                  key=lambda s: (-len(s), s.lower()))


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

# Standalone company-suffix tokens that should re-attach to the prior
# entry. Flock's source has un-escaped commas like "Foo Corp, LLC" that
# get split during ingest. Mirrors _IS_CONTINUATION_SUFFIX in
# flock_transparency.py — keep these in sync.
_IS_CONTINUATION_SUFFIX = re.compile(
    r"^(LLC|L\.L\.C\.?|Inc\.?|Corp\.?|Ltd\.?|Co\.?)$",
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
        merge = names and (
            _EXPECTS_CONTINUATION.search(names[-1])
            or _IS_CONTINUATION_SUFFIX.match(part)
        )
        if merge:
            names[-1] = f"{names[-1]}, {part}"
        else:
            names.append(part)
    return names
