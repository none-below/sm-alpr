"""
Sanitization gate for eyesonflock.com's public portal-data API.

eyesonflock.com is a third-party site that crawls Flock transparency portals
on its own schedule (https://eyesonflock.com/api/v1/data). Their slug list is
useful as an authoritative hint for slug_probe — they confirm portals with
HTTP probes the same way we do, so a slug they list is much higher confidence
than `name_to_slug(name)` guessing.

We don't trust their data blindly:
  - Strict per-segment slug regex (no path-injection chars, double dashes,
    trailing dashes); same shape as our own slugs.
  - Whitelist on state code and agency type; everything else dropped.
  - Free-text fields (`prohibited_uses`, `organizations_shared_with`, etc.)
    are NOT loaded — we only keep `slug`, `city`, `state`, `type`. Anything
    that could carry a prompt-injection payload stays out of our process.
  - Slugs are still cross-verified by slug_probe before promotion to the
    registry. Eyesonflock's role is "high-confidence first guess," not
    "trusted source of truth."

This module exposes pure validation functions that take a payload and return
sanitized records. The `fetch_api()` function is split out so tests don't
need network access.
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_agency_registry import ALL_STATES

EYESONFLOCK_API_URL = "https://eyesonflock.com/api/v1/data"

# ── Sanitization constants ──

# Per-segment slug pattern: optional leading dash (some Flock slugs render
# with one, e.g. "-el-cajon-pd-ca"), then one or more dash-joined alnum
# segments. Rejects double dashes, trailing dashes, whitespace, path-injection
# chars (`/`, `.`, `..`), and non-ASCII. Digits are allowed inside segments
# (e.g. "snohomish-county-wa-911").
SLUG_RE = re.compile(r"^-?[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SLUG_LEN = 80

# Untrusted display text — capped, no control chars. We don't pattern-match
# city names because Flock cities contain apostrophes, accents, and hyphens
# that vary too much to whitelist; we just bound the length and strip ctrl.
MAX_CITY_LEN = 100

# Whitelist of agency-type codes eyesonflock emits. Verified against their
# 2026-05-05 snapshot which contained only PD (police) and SD (sheriff dept).
# Add codes here only after confirming they appear in their data — anything
# unexpected should be dropped at the gate, not silently passed through.
ALLOWED_TYPES = frozenset({"PD", "SD"})

# Required keys per portal record. Eyesonflock's data splits geo locality
# by type: PD records carry `city` (city government police), SD records
# carry `county` (county sheriff). Validation accepts whichever fits the
# type and surfaces it as `locality` + `locality_kind` so callers can match
# against either side of our registry's geo block (kind="city"/"county").
REQUIRED_FIELDS = ("slug", "state", "type")
LOCALITY_FIELD_FOR_TYPE = {
    "PD": ("city", "city"),
    "SD": ("county", "county"),
}

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_slug(raw):
    """Return the canonical slug if `raw` is a real Flock-shaped slug, else None.

    Reject (not strip) is the right answer: bad-shape input is much more
    likely a poison payload than a typo we can salvage.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if not s or len(s) > MAX_SLUG_LEN:
        return None
    if not SLUG_RE.match(s):
        return None
    return s


def normalize_locality(raw):
    """Strip whitespace, drop control characters, cap length. Used for
    both city and county names. Returns None on non-string or empty-
    after-strip — locality is required to build a geo lookup key."""
    if not isinstance(raw, str):
        return None
    s = _CTRL_RE.sub("", raw)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    if not s:
        return None
    return s[:MAX_CITY_LEN]


def locality_key(locality):
    """Lookup-fingerprint form of a city/county name: lowercase, ASCII-
    alnum only. Drops apostrophes, periods, accents, and whitespace so
    "O'Fallon" and "OFallon", or "San Francisco" and "san  francisco",
    collide cleanly. Kept pure-ASCII to avoid Unicode surprises.
    """
    if not isinstance(locality, str):
        return None
    s = locality.lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or None


def validate_record(rec, allowed_states=None, allowed_types=None):
    """Validate a single portal record from the eyesonflock payload.

    Returns a sanitized dict containing slug, state, type, locality,
    locality_kind, and a derived locality_key for matching. None if any
    field fails its check. PD records carry city, SD records carry
    county — the type drives which field we read.
    """
    if not isinstance(rec, dict):
        return None
    if allowed_states is None:
        allowed_states = ALL_STATES
    if allowed_types is None:
        allowed_types = ALLOWED_TYPES

    if not all(k in rec for k in REQUIRED_FIELDS):
        return None

    slug = normalize_slug(rec.get("slug"))
    if not slug:
        return None

    state = rec.get("state")
    if not isinstance(state, str):
        return None
    state = state.strip().upper()
    if state not in allowed_states:
        return None

    typ = rec.get("type")
    if not isinstance(typ, str):
        return None
    typ = typ.strip().upper()
    if typ not in allowed_types:
        return None

    locality_field, locality_kind = LOCALITY_FIELD_FOR_TYPE[typ]
    locality = normalize_locality(rec.get(locality_field))
    if not locality:
        return None

    return {
        "slug": slug,
        "locality": locality,
        "locality_kind": locality_kind,
        "locality_key": locality_key(locality),
        "state": state,
        "type": typ,
    }


def parse_payload(text, allowed_states=None, allowed_types=None):
    """Parse the JSON response body and return a list of validated records.

    Bad records are dropped silently — the caller gets only the survivors.
    Raises ValueError if the top-level shape doesn't have a `portals` list
    (that's a structural change in their API, not a content issue).
    """
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("eyesonflock payload root is not an object")
    portals = data.get("portals")
    if not isinstance(portals, list):
        raise ValueError("eyesonflock payload missing `portals` list")

    out = []
    for rec in portals:
        v = validate_record(rec, allowed_states=allowed_states,
                            allowed_types=allowed_types)
        if v is not None:
            out.append(v)
    return out


def index_by_geo(records):
    """Build a lookup index keyed by (locality_key, state, type) → slug.

    Returns (index, conflicts):
      index    — dict where the key uniquely maps to one slug
      conflicts — list of (key, slugs) tuples for keys with multiple
                  candidate slugs. Surfaced rather than silently picking
                  one so we can audit disagreement before auto-promoting.

    Type already disambiguates city vs county (PD↔city, SD↔county) so
    locality_kind doesn't need to be in the key.
    """
    by_key = {}
    for r in records:
        key = (r["locality_key"], r["state"], r["type"])
        by_key.setdefault(key, []).append(r["slug"])

    index = {k: v[0] for k, v in by_key.items() if len(v) == 1}
    conflicts = [(k, sorted(v)) for k, v in by_key.items() if len(v) > 1]
    return index, conflicts


def fetch_api(url=EYESONFLOCK_API_URL, timeout=30):
    """HTTP GET the API. Returns raw response text. Raises on non-200.

    Kept separate from parse/validate so tests don't need network access.
    Sends a browser-ish UA because the site is behind Cloudflare and 403's
    bare urllib.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"eyesonflock fetch returned HTTP {resp.status}")
        return resp.read().decode("utf-8")


def main():
    """CLI: print a sanitized summary of the API response."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Fetch and validate the eyesonflock.com portal API."
    )
    parser.add_argument("--from-file", type=Path, default=None,
                        help="Read JSON from file instead of fetching")
    args = parser.parse_args()

    if args.from_file:
        text = args.from_file.read_text()
    else:
        text = fetch_api()

    records = parse_payload(text)
    index, conflicts = index_by_geo(records)

    print(f"validated records: {len(records)}")
    print(f"unique geo keys:   {len(index)}")
    print(f"conflicts:         {len(conflicts)}")
    if conflicts:
        print("\nfirst 10 conflicts:")
        for key, slugs in conflicts[:10]:
            print(f"  {key} -> {slugs}")


if __name__ == "__main__":
    main()
