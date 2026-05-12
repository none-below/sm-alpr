#!/usr/bin/env python3
"""
Flock Safety transparency portal archiver, parser, and analyzer.

Three-stage pipeline:
  crawl      Fetch pages, save raw DOM text + PDF visual archive
  parse      (Re)generate structured JSON from saved .txt files
  aggregate  Build sharing graph and run analysis from stored JSONs

Directory structure:
  assets/transparency.flocksafety.com/
    {slug}/
      2026-03-27.txt   # raw DOM text (source of truth)
      2026-03-27.json  # parsed structured data (derived from .txt)
      2026-03-27.pdf   # visual archive
    .content_hashes.json
    .failed_slugs.json

Usage:
  uv run python scripts/flock_transparency.py crawl
  uv run python scripts/flock_transparency.py crawl --related --delay 60
  uv run python scripts/flock_transparency.py crawl --all --batch 5 --delay 300
  uv run python scripts/flock_transparency.py crawl --depth 3 --delay 300
  uv run python scripts/flock_transparency.py parse
  uv run python scripts/flock_transparency.py parse --slug san-mateo-ca-pd
  uv run python scripts/flock_transparency.py aggregate
  uv run python scripts/flock_transparency.py aggregate --json --out outputs/sharing.json
"""

import functools
print = functools.partial(print, flush=True)

import argparse
import base64
import csv
import hashlib
import html.parser
import io
import json
import random
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import (
    BASE_URL, FAILED_FILE, USER_AGENT,
    dedupe, load_json, save_json,
    portal_jsons, portal_txts, resolve_agency,
)

DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")
HASH_FILE = ".content_hashes.json"
VIEWPORT = {"width": 1440, "height": 900}
WAIT_MS = 5000
STALE_DAYS = 14


# ═══════════════════════════════════════════════════════════
# Shared utilities
# ═══════════════════════════════════════════════════════════

DEFAULT_SLUGS = ["san-mateo-ca-pd"]

RELATED_SLUGS = DEFAULT_SLUGS + [
    "stockton-ca-pd", "ncric", "redwood-city-ca-pd", "belmont-ca-pd",
    "san-mateo-county-ca-so", "daly-city-ca-pd", "foster-city-ca-pd",
    "south-san-francisco-ca-pd", "atherton-ca-pd", "hillsborough-ca-pd",
    "menlo-park-ca-pd", "east-palo-alto-ca-pd", "burlingame-ca-pd",
    "san-bruno-ca-pd", "pacifica-ca-pd", "colma-ca-pd", "brisbane-ca-pd",
    "sunnyvale-ca-pd",
]


def slug_variations(slug):
    """Generate plausible Flock portal slug variations to try."""
    variations = [slug]

    # Try without state code: anaheim-ca-pd -> anaheim-pd
    no_state = re.sub(r"-ca-", "-", slug)
    if no_state != slug:
        variations.append(no_state)

    # Try with "police-department": anaheim-ca-pd -> anaheim-ca-police-department
    if slug.endswith("-pd"):
        variations.append(slug[:-3] + "-police-department")
        variations.append(re.sub(r"-ca-pd$", "-pd", slug))

    # Try with "sheriffs-office": foo-ca-so -> foo-ca-sheriffs-office
    if slug.endswith("-so"):
        variations.append(slug[:-3] + "-sheriffs-office")
        variations.append(re.sub(r"-ca-so$", "-so", slug))

    # Try with "-ca-so": foo-sheriffs-office -> foo-ca-so
    if "sheriffs-office" in slug:
        variations.append(re.sub(r"-(?:ca-)?sheriffs-office(?:-ca)?$", "-ca-so", slug))

    # Try pd-ca instead of ca-pd: downey-pd-ca -> downey-ca-pd
    if re.search(r"-pd-ca$", slug):
        variations.append(re.sub(r"-pd-ca$", "-ca-pd", slug))
    elif re.search(r"-ca-pd$", slug):
        variations.append(re.sub(r"-ca-pd$", "-pd-ca", slug))

    # Try -ca-sd (sheriff's department): foo-ca-so -> foo-ca-sd
    if slug.endswith("-ca-so"):
        variations.append(slug[:-3] + "-sd")
    elif slug.endswith("-ca-sd"):
        variations.append(slug[:-3] + "-so")

    # Try collapsed state suffix: mendocino-county-so-ca -> mendocino-county-soca
    collapsed = re.sub(r"-so-ca$", "-soca", slug)
    if collapsed != slug:
        variations.append(collapsed)

    # Try leading dash on all variations so far: el-cajon-ca-pd -> -el-cajon-pd-ca
    if not slug.startswith("-"):
        for v in list(variations):
            variations.append("-" + v)

    # Try removing hyphens in city name (compound names):
    # foothill-deanza-ca-pd -> foothilldeanza-ca-pd
    m = re.match(r"^(.+?)(-ca-(?:pd|so|sd|da)|-pd-ca|-so-ca|-soca)$", slug)
    if m:
        city, suffix = m.group(1), m.group(2)
        dehyphenated = city.replace("-", "")
        if dehyphenated != city:
            variations.append(dehyphenated + suffix)

    # Try city-of / town-of prefix removal/addition:
    # city-of-monte-sereno-ca -> monte-sereno-ca-pd
    # town-of-los-gatos-ca -> los-gatos-ca-pd
    for prefix in ("city-of-", "town-of-"):
        if slug.startswith(prefix):
            bare = slug[len(prefix):]
            variations.append(bare)
            # Also try with -pd suffix: town-of-los-gatos-ca -> los-gatos-ca-pd
            if bare.endswith("-ca"):
                variations.append(bare + "-pd")

    # Try -smcso suffix for woodside-style slugs: town-of-woodside-ca -> town-of-woodside-ca-smcso
    if "woodside" in slug:
        variations.append(slug + "-smcso")
        variations.append("town-of-woodside-ca-smcso")

    return dedupe(variations)


def content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def is_stale(slug, data_dir, max_age_days=STALE_DAYS):
    """Check if a slug's latest capture is older than max_age_days."""
    slug_dir = data_dir / slug
    if not slug_dir.is_dir():
        return True
    txts = portal_txts(slug_dir)
    if not txts:
        return True
    latest_date_str = txts[-1].stem  # e.g. "2026-03-27"
    try:
        latest = datetime.strptime(latest_date_str, "%Y-%m-%d").date()
        age = (date.today() - latest).days
        return age >= max_age_days
    except ValueError:
        return True


def has_prior_success(slug, data_dir):
    """Check if a slug has any prior successful capture (a .json file)."""
    slug_dir = data_dir / slug
    if not slug_dir.is_dir():
        return False
    return bool(portal_jsons(slug_dir))


def latest_capture_date(slug, data_dir):
    """Return the latest successful capture date for a slug, or None.

    A capture exists when a parsed .json is present — .txt alone can be a
    failure stub. None means never successfully captured.
    """
    slug_dir = data_dir / slug
    if not slug_dir.is_dir():
        return None
    jsons = portal_jsons(slug_dir)
    if not jsons:
        return None
    try:
        return datetime.strptime(jsons[-1].stem, "%Y-%m-%d").date()
    except ValueError:
        return None


def split_batch_across_levels(batch, num_levels):
    """Allocate a per-level slot budget for a depth-iterating crawl.

    Returns a list of length `num_levels`. Lower levels (closer to seed)
    get the remainder when the split is uneven — they're the highest-
    signal candidates so they get the extra slot. With `batch=None` /
    `batch=0`, every level gets unlimited budget (float('inf')).

    Examples:
      batch=3, num_levels=2 → [2, 1]
      batch=3, num_levels=3 → [1, 1, 1]
      batch=2, num_levels=3 → [1, 1, 0]   (level 2 starves, by design)
    """
    if not batch:
        return [float("inf")] * num_levels
    per_level_floor = batch // num_levels
    remainder = batch % num_levels
    return [
        per_level_floor + (1 if lvl < remainder else 0)
        for lvl in range(num_levels)
    ]


def latest_capture_attempt_date(slug, data_dir):
    """Return the latest *attempted* capture date — date of the latest .txt
    even if parsing failed afterwards. Used only for crawl-queue ordering
    so a slug whose parser is broken doesn't stay pinned to queue position
    #1 forever. Downstream consumers should keep using latest_capture_date,
    which only counts successful captures.
    """
    slug_dir = data_dir / slug
    if not slug_dir.is_dir():
        return None
    txts = portal_txts(slug_dir)
    if not txts:
        return None
    try:
        return datetime.strptime(txts[-1].stem, "%Y-%m-%d").date()
    except ValueError:
        return None


# ═══════════════════════════════════════════════════════════
# Parsing: raw DOM text -> structured JSON
# ═══════════════════════════════════════════════════════════

# Pattern for names that expect a comma continuation (e.g. "University
# of California, Berkeley"). Used when the previous-name end signals
# that more-name follows.
_EXPECTS_CONTINUATION = re.compile(
    r"University of California$",
    re.IGNORECASE,
)

# Pattern for standalone tokens that are clearly a continuation *of* a
# previous name (e.g. "LLC", "Inc."). Flock's data has split entries
# like ["CA - Topgolf USA El Segundo", "LLC", ...] from un-escaped
# commas in their source — re-attach the suffix to the prior entry.
_IS_CONTINUATION_SUFFIX = re.compile(
    r"^(LLC|L\.L\.C\.?|Inc\.?|Corp\.?|Ltd\.?|Co\.?)$",
    re.IGNORECASE,
)

# Maps heading aliases -> canonical field name.  This is the single source of
# truth for all known headings.  Use None for structural headings (page chrome
# like "Policies", "Usage") that act as section boundaries but carry no data.
# If a heading isn't in this map (exact or prefix match), parsing will fail
# so you know to update the map.
_HEADING_MAP = {
    # ── section / overview headings ──
    "Overview":                              "overview",
    "Usage":                                 "overview",
    "Transparency Portal":                   "overview",
    "Provided by Flock Safety":              "overview",
    # ── policy document links / text ──
    "Policies":                              "policy_info",
    "Policy":                                "policy_info",
    "Policy Documents":                      "policy_info",
    "Policy Page":                           "policy_info",
    "Policy Link":                           "policy_info",
    "Documentation":                         "policy_info",
    "ALPR Policy":                           "alpr_policy",
    "ALPR":                                  "alpr_policy",
    "ALPR Manual":                           "alpr_policy",
    "Automated License Plate Reader Usage and Privacy Policy": "alpr_policy",
    "Full ALPR Policy":                      "alpr_policy",
    "Full ALPR Policy:":                     "alpr_policy",
    "Full ALPR Policy Here:":                "alpr_policy",
    "Full LPR Policy Here:":                 "alpr_policy",
    "Complete ALPR Policy":                  "alpr_policy",
    "OPD Policy: DGO I-12 - Automated License Plate Readers": "alpr_policy",
    # ── other structural fields ──
    "Additional Info":                       "additional_info",
    "Additional Information":                "additional_info",
    "Additional Flock Safety Information":   "additional_info",
    "Community Safeguards":                  "additional_info",
    "LPR and other Cameras":                 "additional_info",
    "Download CSV":                          "download_csv",
    "Public Search Audit":                   "search_audit",
    "Search Audit":                          "search_audit",
    "Delivery address":                      "delivery_address",
    "Sharing":                               "sharing_info",
    "Success Story":                         "success_stories",
    "Recent Success Story":                  "success_stories",
    "Recent Success Stories":                "success_stories",
    "Success Stories":                       "success_stories",
    "Safe City Success Stories":             "success_stories",
    "Disclaimer":                            "disclaimer",
    "California SVS":                        "california_svs",
    "SB54: California Values Act":           "sb54",
    # ── data fields ──
    "What's Detected":                       "whats_detected",
    "What's Not Detected":                   "whats_not_detected",
    "Acceptable Use Policy":                 "acceptable_use_policy",
    "Prohibited Uses":                       "prohibited_uses",
    "Access Policy":                         "access_policy",
    "Hotlist Policy":                        "hotlist_policy",
    "Restrictions on Deployment":            "restrictions_on_deployment",
    "Sharing with Partners":                 "sharing_with_partners",
    "Network Sharing Policy":                "sharing_with_partners",
    "Sharing Policy":                        "sharing_with_partners",
    "Sharing Restrictions":                  "sharing_restrictions",
    "Data retention (in days)":              "data_retention",
    "Data retention":                        "data_retention",
    "Data Retention":                        "data_retention",
    "Data Retention (days)":                 "data_retention",
    "Data Retention for Flock Devices":      "data_retention",
    "Flock Data retention (in days)":        "data_retention",
    "Number of LPR and other cameras":       "camera_count",
    "Number of LPR cameras":                 "camera_count",
    "Number of Active LPR cameras":          "camera_count",
    "Number of Owned Cameras":               "camera_count",
    "Number of Owned LPR Cameras":           "camera_count",
    "Number of Flock LPR Cameras":           "camera_count",
    "Number of LPRs":                        "camera_count",
    "LPR Cameras":                           "camera_count",
    "Total Cameras":                         "camera_count",
    "Hotlists Alerted On":                   "hotlists_alerted_on",
    "Vehicles detected in the last 30 days": "vehicles_detected_30d",
    "Unique vehicles detected in the last 30 days": "vehicles_detected_30d",
    "Unique Vehicles Detected":              "vehicles_detected_30d",
    "Hotlist hits in the last 30 days":      "hotlist_hits_30d",
    "Number of Hotlist Hits":                "hotlist_hits_30d",
    "Searches in the last 30 days":          "searches_30d",
    "Number of Searches":                    "searches_30d",
    "Livermore PD searches in the last 30 days": "searches_30d",
    # org sharing — prefix match handles "Organizations granted access to X data"
    "Organizations granted access":          "orgs_granted_access",
    "External Organizations with Access":    "orgs_granted_access",
    "Sharing Network Data With":             "orgs_granted_access",
    "Approved NCRIC Share With":             "orgs_granted_access",
    "Agencies NCRIC Shares With":            "orgs_granted_access",
    "External agencies who have access":     "orgs_granted_access",
    "Only Agencies With External Access":    "orgs_granted_access",
    "Organizations sharing their data":      "orgs_sharing_with",
    "Receiving Network Data From":           "orgs_sharing_with",
}

# Dynamic heading patterns — matched after exact/prefix lookup fails.
# These are headings that contain variable text (agency names, URLs, etc.)
# Map to a field name or None for structural.
_DYNAMIC_HEADINGS = [
    (re.compile(r"^Last Updated:", re.IGNORECASE), "last_updated"),
    (re.compile(r"^(Link to |To view ).+", re.IGNORECASE), "policy_info"),
    (re.compile(r"^(Full ALPR|Full LPR|Full ALPRY).+", re.IGNORECASE), "alpr_policy"),
    (re.compile(r"^.+[\s(](ALPR|LPR)[\s)]\s*Policy.*$", re.IGNORECASE), "alpr_policy"),
    # Spelled-out form of the ALPR/LPR policy pattern above. Catches
    # agency-prefixed titles like raleigh-nc-pd's "Raleigh Police
    # Department Automated License Plate Recognition and Internet
    # Protocol Camera System Policy" without forcing every variant
    # into _HEADING_MAP one-by-one.
    (re.compile(r"^.+Automated License Plate (?:Recognition|Readers?).+Policy.*$", re.IGNORECASE), "alpr_policy"),
    (re.compile(r"^.+Police Department Policy Manual.*$", re.IGNORECASE), "alpr_policy"),
    # Agency-prefixed bare "Policy" headings, e.g.
    # "Marin County Sheriff's Office Policy" — Flock now bolds these
    # standalone where they used to live as body text under Additional
    # Info. The ALPR-specific patterns above win first; this catches the
    # generic policy-link variant.
    (re.compile(r"^.+(?:Police Department|Sheriff(?:'s)?(?: Office)?|Police Bureau) Policy$", re.IGNORECASE), "policy_info"),
    # Success-story subheadings — agencies post titled excerpts under
    # "Success Stories" (e.g. "Yuba County SO - Facebook Post - …",
    # "Credit Card Skimming Ring - Success story").
    (re.compile(r"^.+ - Facebook Post - .+$", re.IGNORECASE), "success_stories"),
    (re.compile(r"^.+ - Success Stor(?:y|ies)$", re.IGNORECASE), "success_stories"),
    # Sentence-style link blurbs, e.g. "Auburn PD's Policies and
    # Procedures can be found at the following link:" — same role as
    # the "Policy Documents" / "Policy Link" exact headings. Placed
    # last (before the structural-noise None patterns) so more specific
    # patterns above (e.g. alpr_policy) win when a heading matches both.
    (re.compile(r"^.+\bthe following link\b.*$", re.IGNORECASE), "policy_info"),
    # Page chrome / structural noise — explicit None so these don't trip
    # the bold-heading fail-loud check downstream.
    (re.compile(
        r"^(January|February|March|April|May|June|"
        r"July|August|September|October|November|December)\s+\d{4}$",
        re.IGNORECASE,
    ), None),
    (re.compile(r".+Transparency Portal$", re.IGNORECASE), None),
    # Empty-state placeholder for sharing fields, e.g.
    # "None: Alameda does not share with outside agencies".
    (re.compile(r"^None:\s", re.IGNORECASE), None),
    # Documentation chrome some portals add (durango-co-pd 2026-05-11).
    (re.compile(r"^Glossary$", re.IGNORECASE), None),
]

_MAX_HEADING_LEN = 120

# Case-insensitive view of _HEADING_MAP, built once. Heading match
# becomes case-insensitive — Flock has at least three case variants of
# the same heading across agencies ("Number of LPR cameras", "Number of
# LPR Cameras", "LPR Cameras"), and explicitly listing every casing
# bloats the map.
_HEADING_MAP_LOWER = {k.lower(): v for k, v in _HEADING_MAP.items()}

# Prefix-match candidates sorted longest-first. Necessary because some
# headings are extensions of others — "Sharing Network Data With" must
# match before the generic "Sharing" prefix, which would otherwise route
# 12 agencies' partner lists into sharing_info instead of orgs_granted_access.
_HEADING_PREFIXES = sorted(_HEADING_MAP_LOWER.items(), key=lambda kv: -len(kv[0]))


def _match_heading(line):
    """Return canonical field name if line is a known heading, None if it's a
    known structural heading, or the sentinel _UNKNOWN if unrecognised."""
    field, _kind = _match_heading_kind(line)
    return field


def _match_heading_kind(line):
    """Same as _match_heading but also returns how it matched:
    'exact', 'prefix', 'dynamic', or 'unknown'.
    Needed by parse_sections to gate prefix matches on bold-heading evidence —
    prefix matching can otherwise promote body text to a heading (e.g.
    "California SVS, NCMEC Amber Alert" matches the "California SVS" prefix).
    Matching is case-insensitive: heading text varies in case across agencies.
    """
    lowered = line.lower()
    if lowered in _HEADING_MAP_LOWER:
        return _HEADING_MAP_LOWER[lowered], "exact"
    for prefix_lower, field_name in _HEADING_PREFIXES:
        if lowered.startswith(prefix_lower):
            return field_name, "prefix"
    for pattern, field_name in _DYNAMIC_HEADINGS:
        if pattern.match(line):
            return field_name, "dynamic"
    return _UNKNOWN, "unknown"


_UNKNOWN = object()  # sentinel — distinct from None (which means "structural")


def extract_bold_headings(html):
    """Extract bold text from HTML — these are the real section headings.

    Flock transparency pages style section headings two ways:
      - Field headings (e.g. "What's Detected", "Acceptable Use Policy")
        use a heavier font-weight than body text. Body is 400 across the
        page; headings have been 700 (legacy) and 600 (2026 redesign).
        Match 500–900 to absorb future drift (CSS standardizes weights
        to 100/200/.../900) without false-positiving on body text.
      - Section dividers (Overview / Policies / Usage / Additional Info)
        in the 2026 redesign live in <h3> with text-transform: uppercase,
        so the rendered .txt sees them upper-cased even though the HTML
        carries title case. Add both forms so .txt matching works either way.

    Returns a set of stripped text strings.
    """
    headings = set()
    # Field headings live in <p> or <h2>–<h6>. <h1> is the page title
    # ("San Francisco CA PD") and isn't a field heading — exclude it so
    # the unrecognized-bold-heading defense doesn't trip on every page.
    for m in re.findall(
        r"<(?:p|h[2-6])[^>]*style=\"[^\"]*font-weight:\s*[5-9]\d\d[^\"]*\"[^>]*>(.*?)</(?:p|h[2-6])>",
        html,
        re.DOTALL,
    ):
        text = re.sub(r"<[^>]+>", "", m).strip()
        if text:
            headings.add(text)
    for m in re.findall(
        r"<h[2-6][^>]*style=\"[^\"]*text-transform:\s*uppercase[^\"]*\"[^>]*>(.*?)</h[2-6]>",
        html,
        re.DOTALL,
    ):
        text = re.sub(r"<[^>]+>", "", m).strip()
        if text:
            headings.add(text)
            headings.add(text.upper())
    return headings


def _looks_like_heading(line):
    """Heuristic: could this line plausibly be a new/unknown section heading?

    Catches things like "Delivery address", "Disclaimer", "Success Story" that
    we haven't added to the known lists yet.  Tries to reject content lines
    like "License Plates, Vehicles" or "License Plates and Vehicles".
    """
    # Starts with digit or paren -> data value like "365 days", "(TBD)"
    if line[0].isdigit() or line[0] == "(":
        return False
    # Starts with lowercase -> likely content prose
    if line[0].islower():
        return False
    # Contains commas -> likely a list of values
    if "," in line:
        return False
    # Long -> content
    if len(line) > 60:
        return False
    # Reject short content that lists items with "and"/"or"
    # e.g. "License Plates and Vehicles", "Facial Recognition and People"
    words = line.split()
    if len(words) <= 5 and any(w in ("and", "or") for w in words):
        return False
    return True


def parse_sections(text, bold_headings=None):
    """Split raw DOM text into [(heading, body), ...] pairs.

    A heading is a line that either matches a known heading label or looks like
    a plausible new heading (short, title-like, no commas).  Headings must be
    followed by a blank line.  The body is everything up to the next heading.

    When *bold_headings* is provided (a set of strings extracted from the HTML),
    only lines that appear in that set are considered as potential headings.
    This avoids false positives from agency-added content that looks heading-like.

    Returns a list of (heading_str, body_str) tuples and a list of
    unrecognised heading strings.
    """
    lines = text.split("\n")
    # First pass: identify which lines are headings
    heading_indices = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Must be followed by a blank line (or be at end of text)
        next_blank = (i == len(lines) - 1) or not lines[i + 1].strip()
        if not next_blank:
            continue
        prev_blank = (i == 0) or not lines[i - 1].strip()
        match, kind = _match_heading_kind(stripped)
        if match is not _UNKNOWN:
            # Gate prefix matches on bold-heading evidence — body text like
            # "California SVS, NCMEC Amber Alert" should not be promoted to
            # a heading just because it starts with a known heading prefix.
            # Exact and dynamic matches are trusted (structural section
            # dividers like "Policies"/"Usage" are exact matches that
            # aren't styled as bold headings).
            if kind == "prefix" and bold_headings is not None and stripped not in bold_headings:
                continue
            # Known heading — accept even without preceding blank line
            # (handles "Hotlist Policy\nUsage" pattern)
            heading_indices.append(i)
        elif bold_headings is not None:
            # When HTML is available we know the real heading set —
            # don't guess at unknown headings (bold content inside
            # gray boxes can look heading-like but isn't)
            pass
        elif len(stripped) <= _MAX_HEADING_LEN and prev_blank and _looks_like_heading(stripped):
            # No HTML available — fall back to heuristic for unknown headings
            heading_indices.append(i)

    # Second pass: extract (heading, body) pairs
    sections = []
    unknown = []
    for idx, hi in enumerate(heading_indices):
        heading = lines[hi].strip()
        body_start = hi + 1
        body_end = heading_indices[idx + 1] if idx + 1 < len(heading_indices) else len(lines)
        body = "\n".join(lines[body_start:body_end]).strip()
        if _match_heading(heading) is _UNKNOWN:
            unknown.append(heading)
        sections.append((heading, body))

    return sections, unknown


def _parse_number(s, *, field=None, slug=None):
    """Extract a numeric stat from a section body.

    The 2026 Flock redesign added a description line above each stat value
    (e.g. "Number of unique plate reads over the last 30 days.\\n\\n341,081"),
    so a naive "first integer" search grabs "30" out of "30 days" instead
    of the real value. Resolution order:

      1. A line that is *only* a number (canonical 2026 layout).
      2. A line that is "N day(s)" (data_retention legacy + 2026).
      3. Single-paragraph body — fall back to first numeric token (legacy).

    If none of these match a multi-paragraph body, raise — the layout has
    drifted again and silently picking a digit out of prose would produce
    bogus stats (the bug we hit on 2026-04-29). Pass field/slug for a
    helpful error message.
    """
    if not s or not s.strip():
        return None
    for line in s.split("\n"):
        stripped = line.strip()
        if re.fullmatch(r"[\d,]+", stripped):
            return int(stripped.replace(",", ""))
    for line in s.split("\n"):
        stripped = line.strip()
        m = re.fullmatch(r"([\d,]+)\s*days?", stripped, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))
    paragraphs = [p for p in re.split(r"\n\s*\n", s) if p.strip()]
    if len(paragraphs) <= 1:
        m = re.search(r"[\d,]+", s)
        return int(m.group(0).replace(",", "")) if m else None
    raise ValueError(
        f"Numeric field {field!r}"
        + (f" in {slug}" if slug else "")
        + f" has multi-paragraph body without a pure-number line — Flock "
        f"layout may have changed. Body: {s[:300]!r}"
    )


def _strip_leading_description(body):
    """If body has a sentence-style description as its first paragraph
    followed by the real value in a later paragraph, return just the value
    paragraphs. Used by text fields the 2026 redesign added boilerplate to
    (e.g. "Hotlists Alerted On" gained "National and statewide hotlist sources.").
    """
    if not body:
        return body
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if len(paragraphs) >= 2 and paragraphs[0].endswith(".") and "\n" not in paragraphs[0]:
        return "\n\n".join(paragraphs[1:])
    return body


_ORG_DESCRIPTION_RE = re.compile(
    r"^Organizations (?:granted access to|sharing with) .+ data\.$"
)


def _parse_org_names(body):
    """Extract org names from a shared-orgs section body.

    Handles three layouts:
      - 2026 redesign: one agency per line, optionally preceded by a
        description sentence ("Organizations granted access to … data.").
      - Legacy: comma-separated within a single paragraph.
      - Mixed: a description paragraph followed by a comma list.
    """
    if not body:
        return []
    body = _strip_leading_description(body)
    lines = [L.strip() for L in body.split("\n") if L.strip()]
    # Drop the description sentence — _strip_leading_description only
    # fires when ≥2 paragraphs are present, so when the table is empty
    # the lone description line survives and would be parsed as a fake
    # org name (PR #240).
    lines = [L for L in lines if not _ORG_DESCRIPTION_RE.match(L)]
    if not lines:
        return []
    if len(lines) >= 3 and sum(1 for L in lines if "," not in L) >= len(lines) * 0.8:
        raw = lines
    else:
        raw = [n.strip() for n in " ".join(lines).split(", ") if n.strip()]
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


_PORTAL_CONTENT_MARKERS = (
    "What's Detected",
    "Acceptable Use Policy",
    "Hotlist Policy",
)


# Verbs we've seen agencies use to introduce their ALPR tech: "uses"
# (most common), "utilizes" (Oakland), "employs" (Mill Valley),
# "leverages" (occasional). Object is usually "Flock Safety ..." but
# some agencies (Napa PD) describe the product generically as
# "Automatic License Plate Reader technology".
_FLOCK_MARKER_RE = re.compile(
    r" (?:uses|utilizes|employs|leverages) "
    r"(?:Flock Safety(?:'s)? (?:LPR )?(?:[Tt]echnology|Operating System)"
    r"|Automatic License Plate Reader(?:s)?"
    r"(?: \(?[Aa]LPR\)?)? technology)"
)


def _extract_crawled_name(overview, slug, datestamp):
    """Pull the agency's self-described name from the overview prose.

    Flock-rendered overviews follow the shape
        "<Agency Name> uses Flock Safety's Operating System ..."
    so the name is everything before that boilerplate marker. We strip
    stray surrounding quotes too (San Rafael wraps the whole sentence
    in `"…"`, leaving a leading `"` that .strip() alone won't drop).

    Returns None silently when the overview is empty or doesn't follow
    the boilerplate at all (NCRIC, for example, writes a custom intro
    that never mentions Flock Safety). Raises when "Flock Safety" IS
    mentioned but the marker shape doesn't match — that's the regression
    we want to surface, since it means Flock rephrased the boilerplate
    or an agency picked a verb we haven't seen.
    """
    m = _FLOCK_MARKER_RE.search(overview)
    if m:
        return (
            overview[: m.start()]
            .strip()
            .strip("\"'“”‘’")
            .strip()
        ) or None
    if overview.strip() and "Flock Safety" in overview:
        raise ValueError(
            f"{slug} {datestamp}: overview mentions Flock Safety but the "
            f"agency-name marker ("
            f"' (uses|utilizes|employs|leverages) Flock Safety[\\'s] [LPR] "
            f"[Tt]echnology|Operating System | Automatic License Plate "
            f"Reader technology') doesn't match — Flock may have rephrased "
            f"the boilerplate or the agency used a new verb. Update "
            f"_FLOCK_MARKER_RE in scripts/flock_transparency.py."
        )
    return None


def parse_portal_text(raw_text, slug, datestamp, bold_headings=None):
    """Parse structured data from raw DOM text."""
    sections, unknown = parse_sections(raw_text, bold_headings=bold_headings)

    if unknown:
        raise ValueError(
            f"Unrecognised headings in {slug}: {unknown}  — add them to _HEADING_MAP"
        )

    # Fail-loud: the page has portal content but the HTML returned zero
    # styled headings, meaning Flock changed the heading CSS in a way
    # extract_bold_headings doesn't recognize. Without bold evidence, every
    # prefix-match heading is silently rejected, so most fields would come
    # back empty. This is the cascade we hit on 2026-04-29 — surface it
    # instead of saving bogus data. Stub/disabled portals lack the markers
    # and don't trip this; empty value boxes still have styled heading
    # wrappers and don't trip it either.
    if (
        bold_headings is not None
        and not bold_headings
        and any(m in raw_text for m in _PORTAL_CONTENT_MARKERS)
    ):
        raise ValueError(
            f"{slug} {datestamp}: portal content present but no styled "
            f"headings extracted — Flock heading CSS may have drifted "
            f"(extract_bold_headings is gated on font-weight 500-900). "
            f"Update extract_bold_headings before re-parsing."
        )

    # Build field_name -> body lookup.
    # For numeric/data fields, last match wins (when a page has both a
    # specific and a general heading for the same field, the general one
    # tends to appear later and be more complete, e.g. "Number of LPR
    # cameras" (44) followed by "Number of LPR and other cameras" (140)).
    # For text fields that can appear under multiple aliased headings,
    # concatenate with double-newline so nothing is lost.
    _LAST_WINS = {
        "data_retention", "camera_count", "vehicles_detected_30d",
        "hotlist_hits_30d", "searches_30d",
    }
    fields = {}
    for heading, body in sections:
        field_name = _match_heading(heading)
        if field_name is _UNKNOWN:
            continue
        if field_name in _LAST_WINS or field_name not in fields:
            fields[field_name] = body
        elif body:
            fields[field_name] = fields[field_name] + "\n\n" + body if fields[field_name] else body

    outbound_names = _parse_org_names(fields.get("orgs_granted_access", ""))
    inbound_names = _parse_org_names(fields.get("orgs_sharing_with", ""))

    #   "<Agency Name> uses Flock Safety's Operating System..."
    # Flock has rephrased the boilerplate over time — older portals say
    # "uses Flock Safety [LPR] technology", newer ones (seen on pacifica
    # 2026-05-06) say "uses Flock Safety's Operating System". Match either.
    #
    # Some agencies write a custom overview that doesn't follow the
    # boilerplate at all. NCRIC's reads "***draft version*** The Northern
    # California Regional Intelligence Center is a multi-jurisdiction
    # government program..." and never mentions Flock Safety. For those
    # crawled_name stays None silently — we only raise when "Flock Safety"
    # IS mentioned but the marker shape doesn't match, which is the case
    # we actually want to catch (Flock rephrased their boilerplate).
    overview = fields.get("overview", "")
    crawled_name = _extract_crawled_name(overview, slug, datestamp)

    # Fail-loud: each bold heading should resolve to something in
    # _HEADING_MAP or a _DYNAMIC_HEADINGS pattern. Anything else is a
    # field heading we don't know about — silently skipping it would
    # drop that field's body (e.g. "LPR Cameras" → camera_count). Better
    # to surface and add the alias than to lose data quietly.
    # Filter out the agency-name page title (some templates render it as
    # a standalone bold <h1>; others concatenate with "Transparency Portal"
    # which the dynamic noise regex catches).
    if bold_headings:
        unrecognized_bold = [
            h for h in bold_headings
            if _match_heading(h) is _UNKNOWN and h != crawled_name
        ]
        if unrecognized_bold:
            raise ValueError(
                f"{slug} {datestamp}: bold headings not in _HEADING_MAP: "
                f"{sorted(unrecognized_bold)} — add them as aliases or "
                f"as _DYNAMIC_HEADINGS noise patterns."
            )

    return {
        "crawled_slug": slug,
        "crawled_name": crawled_name,
        "archived_date": datestamp,
        "whats_detected": fields.get("whats_detected", ""),
        "whats_not_detected": fields.get("whats_not_detected", ""),
        "acceptable_use_policy": fields.get("acceptable_use_policy", ""),
        "prohibited_uses": fields.get("prohibited_uses", ""),
        "access_policy": fields.get("access_policy", ""),
        "hotlist_policy": fields.get("hotlist_policy", ""),
        "sharing_with_partners": fields.get("sharing_with_partners", ""),
        "sharing_restrictions": fields.get("sharing_restrictions", ""),
        "data_retention_days": _parse_number(fields.get("data_retention", ""), field="data_retention", slug=slug),
        "camera_count": _parse_number(fields.get("camera_count", ""), field="camera_count", slug=slug),
        "hotlists_alerted_on": _strip_leading_description(fields.get("hotlists_alerted_on", "")),
        "vehicles_detected_30d": _parse_number(fields.get("vehicles_detected_30d", ""), field="vehicles_detected_30d", slug=slug),
        "hotlist_hits_30d": _parse_number(fields.get("hotlist_hits_30d", ""), field="hotlist_hits_30d", slug=slug),
        "searches_30d": _parse_number(fields.get("searches_30d", ""), field="searches_30d", slug=slug),
        "sharing_outbound": outbound_names,
        "sharing_inbound": inbound_names,
        # ── newly captured fields (empty string when absent) ──
        "overview": fields.get("overview", ""),
        "policy_info": fields.get("policy_info", ""),
        "alpr_policy": fields.get("alpr_policy", ""),
        "additional_info": fields.get("additional_info", ""),
        "download_csv": fields.get("download_csv", ""),
        "search_audit": fields.get("search_audit", ""),
        "delivery_address": fields.get("delivery_address", ""),
        "sharing_info": fields.get("sharing_info", ""),
        "success_stories": fields.get("success_stories", ""),
        "disclaimer": fields.get("disclaimer", ""),
        "california_svs": fields.get("california_svs", ""),
        "sb54": fields.get("sb54", ""),
        "last_updated": fields.get("last_updated", ""),
        "restrictions_on_deployment": fields.get("restrictions_on_deployment", ""),
    }


# ═══════════════════════════════════════════════════════════
# CSV extraction from HTML
# ═══════════════════════════════════════════════════════════

class _CSVLinkExtractor(html.parser.HTMLParser):
    """Extract data-URI CSVs from <a download="*.csv" href="data:..."> tags."""

    def __init__(self):
        super().__init__()
        self.csvs = []  # list of (filename, csv_text)

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        download = attrs_dict.get("download", "")
        href = attrs_dict.get("href", "")
        if download.endswith(".csv") and href.startswith("data:"):
            try:
                _, encoded = href.split(",", 1)
                csv_text = urllib.parse.unquote(encoded)
                self.csvs.append((download, csv_text))
            except (ValueError, UnicodeDecodeError):
                pass


# Flock renamed embedded CSV files in the 2026 redesign. Normalize at
# extraction so downstream field names stay stable across the format
# change (otherwise an agency's diff shows search_audit_csv → null AND
# public_search_audit_csv null → rows on the format-flip scrape).
# Verified: no portal serves both filenames simultaneously.
_CSV_FILENAME_ALIASES = {
    "public_search_audit.csv": "search_audit.csv",
    "public-search-audit.csv": "search_audit.csv",
}


def extract_csvs_from_html(html_text):
    """Parse HTML and return list of (filename, [row_dicts]) for embedded CSVs."""
    parser = _CSVLinkExtractor()
    parser.feed(html_text)
    results = []
    for filename, csv_text in parser.csvs:
        filename = _CSV_FILENAME_ALIASES.get(filename, filename)
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        results.append((filename, rows))
    return results


# ═══════════════════════════════════════════════════════════
# Crawl: fetch pages, save .txt + .pdf
# ═══════════════════════════════════════════════════════════

def archive_agency(page, slug, data_dir, force=False, hashes=None, progress=""):
    """Returns (status, discovered_slugs).

    status: Path (saved), "unchanged", "rate_limited", or ("failed", reason).

    Artifact-set integrity: a slug-dir capture is the four-tuple
    .txt/.html/.json/.pdf. The whole set is staged in a `mktemp -d`
    directory and only moved into `assets/transparency.flocksafety.com/<slug>/`
    after all four artifacts validate. Parser failure on a new heading
    variant returns ("failed", "parse_error") and writes nothing to the
    asset tree — the PARSE_ERROR log line is surfaced as a tracking issue
    by the workflows, and the next crawl will re-capture once the parser
    is updated. (Earlier behavior left .txt + .html on disk with no .json,
    which polluted diff/build logic that assumes html ↔ json parity per date.)
    """
    url = f"{BASE_URL}/{slug}"
    datestamp = date.today().isoformat()
    slug_dir = data_dir / slug

    prefix = f"  {progress} " if progress else "  "
    print(f"{prefix}{slug} -> {url}")

    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"    WARNING: navigation failed: {e}")
        return ("failed", "navigation_error"), []

    if response and response.status == 429:
        print(f"    RATE LIMITED (429), will retry later")
        return "rate_limited", []

    if response and response.status >= 400:
        print(f"    WARNING: got HTTP {response.status}, skipping")
        return ("failed", f"http_{response.status}"), []

    page.wait_for_timeout(WAIT_MS)

    page_text = page.inner_text("body")
    expected_sections = ["Policies", "Usage", "What's Detected"]
    if not any(section in page_text for section in expected_sections):
        print(f"    WARNING: page does not look like a transparency portal, skipping")
        return ("failed", "not_a_portal"), []

    current_hash = content_hash(page_text)
    prev_hash = (hashes or {}).get(slug)
    page_html = page.content()
    crawled_at = datetime.now(timezone.utc).isoformat()

    # Parse in memory before writing anything. discovered_slugs is needed
    # for depth crawling even when content is unchanged, so we parse on
    # every successful fetch.
    bold_headings = extract_bold_headings(page_html)
    discovered_slugs = []
    try:
        portal_data = parse_portal_text(page_text, slug, datestamp, bold_headings=bold_headings)
    except ValueError as e:
        # PARSE_ERROR marker line is grepped by the refresh-flock-data
        # and probe-slugs workflows to surface a tracking issue.
        print(f"    PARSE_ERROR: {slug} {datestamp} :: {e}")
        if not force and prev_hash == current_hash:
            return "unchanged", []
        return ("failed", "parse_error"), []

    portal_data["crawled_at"] = crawled_at
    # Resolve sharing names to slugs for depth crawling. Only registry-
    # confirmed slugs (flock_active_slug set after a real capture or
    # probe hit) propagate — name_to_slug guesses are slug_probe's job
    # now, not the crawler's. A peer-outbound name without a registry
    # match drops out here; it'll get added as a stub on the next
    # build_agency_registry --merge run and slug_probe will try it.
    for name in portal_data.get("sharing_outbound", []):
        entry = resolve_agency(name=name)
        if entry and entry.get("flock_active_slug"):
            discovered_slugs.append(entry["flock_active_slug"])

    if not force and prev_hash == current_hash:
        print(f"    unchanged since last capture, skipping")
        return "unchanged", discovered_slugs

    for csv_name, csv_rows in extract_csvs_from_html(page_html):
        field = csv_name.replace(".csv", "").replace("-", "_") + "_csv"
        portal_data[field] = csv_rows
        print(f"    extracted {csv_name}: {len(csv_rows)} rows")

    cdp = page.context.new_cdp_session(page)
    result = cdp.send("Page.printToPDF", {
        "printBackground": True, "preferCSSPageSize": False,
        "paperWidth": 11, "paperHeight": 17,
        "marginTop": 0.4, "marginBottom": 0.4,
        "marginLeft": 0.4, "marginRight": 0.4,
    })
    cdp.detach()
    pdf_data = base64.b64decode(result["data"])

    # Stage the four-artifact set in a fresh temp dir, then mv into
    # slug_dir only after all four write successfully. mktemp -d (not
    # a fixed staging path) so retries/parallel batches can't collide.
    with tempfile.TemporaryDirectory(prefix=f"flock-{slug}-") as staging_str:
        staging = Path(staging_str)
        (staging / f"{datestamp}.txt").write_text(page_text, encoding="utf-8")
        (staging / f"{datestamp}.html").write_text(page_html, encoding="utf-8")
        (staging / f"{datestamp}.json").write_text(json.dumps(portal_data, indent=2) + "\n")
        (staging / f"{datestamp}.pdf").write_bytes(pdf_data)

        slug_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = slug_dir / f"{datestamp}.pdf"
        for ext in ("txt", "html", "json", "pdf"):
            shutil.move(
                str(staging / f"{datestamp}.{ext}"),
                str(slug_dir / f"{datestamp}.{ext}"),
            )

    print(f"    saved {slug}/{datestamp}.{{txt,html,json,pdf}}")

    if hashes is not None:
        hashes[slug] = current_hash

    return pdf_path, discovered_slugs


def run_crawl_batch(page, slugs, data_dir, force, delay, hashes, failed_slugs,
                    try_variations=False):
    """Crawl a list of slugs. Returns (results, discovered_slugs)."""
    results = []
    discovered = []

    total = len(slugs)
    for i, slug in enumerate(slugs):
        progress = f"({i + 1}/{total})"
        if slug in failed_slugs:
            reason = failed_slugs[slug].get("reason", "unknown") if isinstance(failed_slugs[slug], dict) else "unknown"
            print(f"  {progress} {slug} -> previously failed ({reason}), skipping")
            results.append((slug, None))
            continue

        status, discovered_slugs = archive_agency(page, slug, data_dir, force, hashes, progress=progress)

        # Try slug variations on 404
        if try_variations and isinstance(status, tuple) and status[0] == "failed" and status[1] == "http_404":
            for alt in slug_variations(slug)[1:]:
                print(f"    trying variation: {alt}")
                status, discovered_slugs = archive_agency(page, alt, data_dir, force, hashes)
                if not (isinstance(status, tuple) and status[0] == "failed"):
                    print(f"    found working slug: {alt}")
                    slug = alt
                    break
                if delay:
                    time.sleep(delay * random.uniform(0.7, 1.3))

        if status == "rate_limited":
            for attempt in range(4):
                backoff = max(delay, 30) * (2 ** (attempt + 1))
                print(f"    rate limited, backing off {backoff}s (attempt {attempt + 1}/4)...")
                time.sleep(backoff)
                status, discovered_slugs = archive_agency(page, slug, data_dir, force, hashes)
                if status != "rate_limited":
                    break
            if status == "rate_limited":
                print(f"    still rate limited after 4 retries, skipping for now")
                results.append((slug, None))
                save_json(data_dir / HASH_FILE, hashes)
                continue

        results.append((slug, status))
        if discovered_slugs:
            discovered.extend(discovered_slugs)
        if isinstance(status, tuple) and status[0] == "failed":
            # parse_error means we got the page but the parser tripped on
            # a new format variant — nothing was written to the asset tree
            # (archive_agency stages and only commits on full success).
            # Don't permanently quarantine these: leave them out of
            # failed_slugs so the next crawl re-fetches and re-parses
            # against an updated parser. The PARSE_ERROR log line is
            # surfaced as a tracking issue by the workflow.
            if status[1] != "parse_error":
                failed_slugs[slug] = {"reason": status[1], "date": date.today().isoformat()}

        save_json(data_dir / HASH_FILE, hashes)
        save_json(data_dir / FAILED_FILE, failed_slugs)

        if delay and i < len(slugs) - 1:
            jitter = delay * random.uniform(0.7, 1.3)
            print(f"    waiting {jitter:.0f}s...")
            time.sleep(jitter)

    return results, discovered


def cmd_crawl(args):
    from playwright.sync_api import sync_playwright

    if args.depth:
        args.all_agencies = True

    slugs = list(args.slugs)
    if args.file:
        text = args.file.read_text()
        slugs.extend(
            line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    data_dir = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    hashes = load_json(data_dir / HASH_FILE)
    failed_slugs = load_json(data_dir / FAILED_FILE)
    # Snapshot the previously-failed set before --retry-failed wipes it — we
    # still want the sort to deprioritize retries of prior 404s below fresh
    # uncrawled slugs.
    previously_failed = set(failed_slugs.keys())

    if args.retry_failed:
        failed_slugs.clear()

    with sync_playwright() as p:
        launch_args = ["--headless=new"]
        if args.proxy:
            browser = p.chromium.launch(headless=True, args=launch_args, proxy={"server": args.proxy})
        else:
            browser = p.chromium.launch(headless=True, args=launch_args)
        context = browser.new_context(
            viewport=VIEWPORT,
            user_agent=USER_AGENT,
        )
        page = context.new_page()

        if args.all_agencies and not slugs:
            slugs = list(DEFAULT_SLUGS)
        elif args.related and not slugs:
            slugs = list(RELATED_SLUGS)
        elif not slugs:
            slugs = list(DEFAULT_SLUGS)

        slugs = dedupe(slugs)

        if args.delay == 0 and not any("--delay" in a for a in sys.argv):
            if len(slugs) > 10 or args.depth or args.all_agencies:
                args.delay = 300
                print(f"  auto-setting --delay {args.delay}s (5 min) to avoid rate limiting\n")

        all_results = []
        visited = set()
        if not args.retry_failed:
            visited.update(failed_slugs.keys())
        if not args.force:
            # Only skip slugs with fresh captures (< STALE_DAYS old)
            visited.update(
                s for s in hashes if not is_stale(s, data_dir, args.max_age)
            )
        if args.all_agencies or args.depth:
            max_depth = args.depth if args.depth else 1
            # Split --batch across levels so peers reachable only at deeper
            # levels actually get a turn. Without this, level 0 (the seed's
            # direct outbound) consumes the whole batch every run and tier-0
            # candidates that only surface at level 1+ starve (e.g. alameda-
            # ca-pd, which is in 70 peers' outbound but not san-mateo's).
            # Lower levels (closer to seed = higher signal) get the remainder
            # when the split is uneven; e.g. batch=3 at 2 levels → 2 + 1.
            # Unused budget rolls forward into the next level via `available`,
            # so we still spend up to the global rate limit when possible.
            num_levels = max_depth + 1
            level_budgets = split_batch_across_levels(args.batch, num_levels)
            available = 0

            for level in range(num_levels):
                available += level_budgets[level]
                # For already-visited slugs, load sharing lists from stored JSONs
                # so we can discover their downstream agencies without re-fetching
                discovered_from_existing = []
                for s in slugs:
                    if s in visited:
                        slug_dir = data_dir / s
                        # No dir means this slug is in `visited` via failed_slugs
                        # (never successfully captured). Nothing to harvest from.
                        # Whether it gets crawled is decided below at `new_slugs`;
                        # skipping here only opts out of the outbound-sharing read.
                        if not slug_dir.is_dir():
                            continue
                        jsons = portal_jsons(slug_dir)
                        if jsons:
                            stored = json.loads(jsons[-1].read_text())
                            # Support both old and new field names
                            names = stored.get("sharing_outbound") or stored.get("shared_org_names", [])
                            for name in names:
                                entry = resolve_agency(name=name)
                                if entry and entry.get("flock_active_slug"):
                                    discovered_from_existing.append(entry["flock_active_slug"])
                                # else: no registry-confirmed slug for this
                                # peer-outbound name. Drop it — slug_probe
                                # owns first-try discovery now, so the crawler
                                # never spends rate budget on speculative guesses.

                # Eligible candidate slugs this level = the current seed list plus
                # anything discovered in the outbound of fresh neighbors. Merging
                # `discovered_from_existing` here is what lets a known-but-never-
                # crawled slug (e.g. a newly seeded registry entry that first
                # appears in some peer's outbound list) get picked up on the same
                # level it's discovered, instead of waiting for a deeper run.
                candidates = dedupe(list(slugs) + discovered_from_existing)
                new_slugs = [s for s in candidates if s not in visited]
                # Crawl order by tier:
                #   0 never attempted — fills gaps like newly-seeded registry
                #     entries
                #   1 previously captured — refresh stalest first (by capture
                #     date)
                #   2 attempted before but no successful capture (was in
                #     failed_slugs.json) — lowest priority so retries of prior
                #     404s/parse-failures don't starve fresh slugs. Normal runs
                #     already exclude these via `visited`; this tier only fires
                #     under --retry-failed, where we still want the retry to
                #     happen *after* genuinely new slugs.
                def _order(s):
                    # Use attempt date (.txt presence) so a slug whose
                    # parser is broken still ages out of tier 0 — its
                    # .txt got saved on the failed attempt, even though
                    # no .json was produced.
                    last = latest_capture_attempt_date(s, data_dir)
                    if last is not None:
                        return (1, last)
                    if s in previously_failed:
                        return (2, date.min)
                    return (0, date.min)
                new_slugs.sort(key=_order)
                if args.batch:
                    # Pick a random sample from the top 3*budget oldest
                    # candidates instead of strictly the top N. Preserves
                    # "oldest first" intent but spreads attention so a
                    # single failing slug at position #1 doesn't waste
                    # the same crawl slot on every run — over a few
                    # cycles the failing slug just becomes one of many
                    # candidates for the slot.
                    n = int(available)
                    pool = new_slugs[:n * 3]
                    new_slugs = random.sample(pool, min(n, len(pool)))

                if not new_slugs and not discovered_from_existing:
                    print(f"\nDepth {level}: no new agencies to check, stopping.\n")
                    break

                discovered = list(discovered_from_existing)

                if new_slugs:
                    label = f"depth {level}" if args.depth else "all"
                    print(f"[{label}] Archiving {len(new_slugs)} agency portal(s):\n")

                    results, newly_discovered = run_crawl_batch(
                        page, new_slugs, data_dir, args.force,
                        args.delay, hashes, failed_slugs,
                        try_variations=args.try_variations,
                    )
                    all_results.extend(results)
                    visited.update(s for s, _ in results)
                    available -= len(new_slugs)
                    discovered.extend(newly_discovered)

                slugs = dedupe(discovered)
        else:
            if args.batch:
                slugs = slugs[:args.batch]
            print(f"Archiving {len(slugs)} agency portal(s):\n")
            results, _ = run_crawl_batch(
                page, slugs, data_dir, args.force,
                args.delay, hashes, failed_slugs,
                try_variations=args.try_variations,
            )
            all_results.extend(results)

        browser.close()

    def _is_failed(r):
        return r is None or (isinstance(r, tuple) and r[0] == "failed")
    captured = sum(1 for _, r in all_results if not _is_failed(r) and r != "unchanged")
    unchanged = sum(1 for _, r in all_results if r == "unchanged")
    failed = sum(1 for _, r in all_results if _is_failed(r))
    print(f"\nDone: {captured} captured, {unchanged} unchanged, {failed} failed.")

    if failed and not captured and not unchanged:
        print("  (all agencies failed — not treated as error for batch crawls)")



# ═══════════════════════════════════════════════════════════
# Parse: (re)generate .json from .txt files
# ═══════════════════════════════════════════════════════════

def cmd_parse(args):
    data_dir = args.data_dir
    count = 0
    failed = 0

    slug_dirs = sorted(data_dir.iterdir()) if not args.slug else [data_dir / args.slug]

    for slug_dir in slug_dirs:
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue

        slug = slug_dir.name
        for txt_path in portal_txts(slug_dir):
            datestamp = txt_path.stem  # e.g. "2026-03-27"
            json_path = slug_dir / f"{datestamp}.json"

            if json_path.exists() and not args.force:
                continue

            raw_text = txt_path.read_text(encoding="utf-8")

            # Extract bold headings from HTML if available
            html_path = slug_dir / f"{datestamp}.html"
            bold_headings = None
            page_html = None
            if html_path.exists():
                page_html = html_path.read_text(encoding="utf-8")
                bold_headings = extract_bold_headings(page_html)

            try:
                portal_data = parse_portal_text(raw_text, slug, datestamp, bold_headings=bold_headings)
            except ValueError as e:
                # Soft-fail per file (mirrors cmd_crawl): one slug's new
                # heading variant must not block parsing of other slugs
                # captured in the same batch. The .txt/.html stay on
                # disk; `parse --force --slug <slug>` re-tries once the
                # parser is fixed. The PARSE_ERROR marker is grepped by
                # the workflow's issue-surfacing step.
                print(f"    PARSE_ERROR: {slug} {datestamp} :: {e}")
                failed += 1
                continue

            # Preserve crawled_at from existing JSON (set during crawl, not recoverable from .txt)
            if json_path.exists():
                try:
                    existing = json.loads(json_path.read_text())
                    if "crawled_at" in existing:
                        portal_data["crawled_at"] = existing["crawled_at"]
                except (json.JSONDecodeError, KeyError):
                    pass

            # Extract embedded CSVs from HTML if available
            if page_html is not None:
                for csv_name, csv_rows in extract_csvs_from_html(
                    page_html
                ):
                    field = csv_name.replace(".csv", "").replace("-", "_") + "_csv"
                    portal_data[field] = csv_rows
                    print(f"    extracted {csv_name}: {len(csv_rows)} rows")

            json_path.write_text(json.dumps(portal_data, indent=2) + "\n")
            cameras = portal_data.get("camera_count") or "?"
            orgs = len(portal_data.get("sharing_outbound", []))
            print(f"  {slug}/{datestamp}.json — {cameras} cameras, {orgs} orgs")
            count += 1

    summary = f"\nParsed {count} file(s)."
    if failed:
        summary += f" {failed} failed (see PARSE_ERROR lines above)."
    print(summary)


# ═══════════════════════════════════════════════════════════
# Aggregate: build sharing graph + analysis from .json files
# ═══════════════════════════════════════════════════════════


def cmd_aggregate(args):
    data_dir = args.data_dir
    from lib import resolve_agency, agency_display_name, agency_state, registry_by_id, has_tag

    # Load the latest JSON for each agency, resolve to agency_id
    agencies = {}         # agency_id -> crawled data
    sharing_graph = {}    # agency_id -> [target_agency_ids]

    for slug_dir in sorted(data_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue
        jsons = portal_jsons(slug_dir)
        if not jsons:
            continue
        data = json.loads(jsons[-1].read_text())
        dir_slug = slug_dir.name

        entry = resolve_agency(slug=dir_slug)
        if not entry:
            continue
        agency_id = entry["agency_id"]
        agencies[agency_id] = data

        # Resolve outbound names to agency_ids (support old + new field names)
        outbound_names = data.get("sharing_outbound") or data.get("shared_org_names", [])
        outbound_ids = []
        for name in outbound_names:
            target = resolve_agency(name=name)
            if target:
                outbound_ids.append(target["agency_id"])
        if outbound_ids:
            sharing_graph[agency_id] = outbound_ids

    if not agencies:
        print("No parsed data found. Run 'parse' first.", file=sys.stderr)
        sys.exit(1)

    # Collect all entities
    all_entities = set(sharing_graph.keys())
    for ids in sharing_graph.values():
        all_entities.update(ids)

    # Inbound counts
    inbound = Counter()
    for _, targets in sharing_graph.items():
        for t in targets:
            inbound[t] += 1

    # Classify and flag using registry
    reg_by_id = registry_by_id()
    flagged = []
    for entity_id in sorted(all_entities):
        e = reg_by_id.get(entity_id, {})
        flags = []
        if has_tag(e, "private"):
            flags.append("PRIVATE")
        e_state = agency_state(e)
        if e_state and e_state != "CA":
            flags.append("OUT_OF_STATE")
        if has_tag(e, "federal"):
            flags.append("FEDERAL")
        if e.get("agency_type") == "decommissioned":
            flags.append("DECOMMISSIONED")
        if e.get("agency_type") == "test":
            flags.append("TEST")
        if e.get("agency_role") == "fire":
            flags.append("NON_LAW_ENFORCEMENT")
        if has_tag(e, "needs-review"):
            flags.append("NEEDS_REVIEW")
        if not flags:
            continue
        shared_by = [src for src, targets in sharing_graph.items() if entity_id in targets]
        flagged.append({
            "entity": entity_id,
            "name": agency_display_name(e, entity_id),
            "flags": flags,
            "shared_by": shared_by, "shared_by_count": len(shared_by),
        })

    # Asymmetric sharing
    mapped = set(sharing_graph.keys())
    asymmetric = []
    for a in sorted(mapped):
        for b in sharing_graph[a]:
            if b in mapped and a not in sharing_graph.get(b, []):
                asymmetric.append({"source": a, "target": b})

    # Shared by all
    shared_by_all = []
    if sharing_graph:
        sets = [set(t) for t in sharing_graph.values()]
        common = sets[0]
        for s in sets[1:]:
            common &= s
        shared_by_all = sorted(common)

    results = {
        "summary": {
            "agencies_mapped": len(sharing_graph),
            "total_entities": len(all_entities),
            "flagged_count": len(flagged),
            "total_cameras": sum(a.get("camera_count") or 0 for a in agencies.values()),
            "total_vehicles_30d": sum(a.get("vehicles_detected_30d") or 0 for a in agencies.values()),
        },
        "agencies": {aid: {
            "camera_count": a.get("camera_count"),
            "data_retention_days": a.get("data_retention_days"),
            "vehicles_detected_30d": a.get("vehicles_detected_30d"),
            "hotlist_hits_30d": a.get("hotlist_hits_30d"),
            "searches_30d": a.get("searches_30d"),
            "sharing_outbound_count": len(a.get("sharing_outbound") or a.get("shared_org_names") or []),
            "archived_date": a.get("archived_date"),
        } for aid, a in sorted(agencies.items())},
        "flagged_entities": flagged,
        "asymmetric_sharing": asymmetric,
        "most_sharing_inbound": [
            {"agency_id": aid, "inbound_count": c} for aid, c in inbound.most_common(30)
        ],
        "shared_by_all": shared_by_all,
        "sharing_graph": sharing_graph,
    }

    if args.json_output:
        print(json.dumps(results, indent=2))
    else:
        _print_report(results)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2) + "\n")
        print(f"\nJSON written to {args.out}")


def _print_report(r):
    s = r["summary"]
    print(f"{'=' * 60}")
    print(f"FLOCK ALPR SHARING ANALYSIS")
    print(f"{'=' * 60}")
    print(f"\n  Agencies mapped:    {s['agencies_mapped']}")
    print(f"  Total entities:     {s['total_entities']}")
    print(f"  Total cameras:      {s['total_cameras']}")
    print(f"  Vehicles (30d):     {s['total_vehicles_30d']:,}")
    print(f"  Flagged entities:   {s['flagged_count']}")

    # Agency table
    print(f"\n{'─' * 60}")
    print(f"AGENCY SUMMARY\n")
    print(f"  {'Agency':<40} {'Cam':>4} {'Ret':>4} {'Orgs':>5} {'Date':<12}")
    print(f"  {'─'*40} {'─'*4} {'─'*4} {'─'*5} {'─'*12}")
    for slug, info in r["agencies"].items():
        cam = info.get("camera_count") or "?"
        ret = info.get("data_retention_days") or "?"
        orgs = len(info.get("sharing_outbound") or info.get("shared_org_names") or []) or "?"
        dt = info.get("archived_date") or "?"
        print(f"  {slug:<40} {cam:>4} {ret:>4} {orgs:>5} {dt:<12}")

    # Private entities
    private = [e for e in r["flagged_entities"] if "PRIVATE" in e["flags"]]
    if private:
        print(f"\n{'─' * 60}")
        print(f"PRIVATE ENTITIES ({len(private)})")
        print(f"  CA law (§1798.90.55(b)) restricts sharing to public agencies\n")
        for e in sorted(private, key=lambda x: -x["shared_by_count"]):
            print(f"  {e.get('name', e['entity'])}  [{', '.join(e['flags'])}]")
            if e["shared_by"]:
                print(f"    shared by: {', '.join(e['shared_by'])}")
            print()

    # Out of state
    oos = [e for e in r["flagged_entities"] if "OUT_OF_STATE" in e["flags"]]
    if oos:
        print(f"{'─' * 60}")
        print(f"OUT-OF-STATE ENTITIES ({len(oos)})\n")
        for e in sorted(oos, key=lambda x: -x["shared_by_count"]):
            print(f"  {e.get('name', e['entity'])}")
            if e["shared_by"]:
                print(f"    shared by: {', '.join(e['shared_by'])}")
            print()

    # Federal
    federal = [e for e in r["flagged_entities"] if "FEDERAL" in e["flags"]]
    if federal:
        print(f"{'─' * 60}")
        print(f"FEDERAL ENTITIES ({len(federal)})\n")
        for e in sorted(federal, key=lambda x: -x["shared_by_count"]):
            print(f"  {e.get('name', e['entity'])}")
            if e["shared_by"]:
                print(f"    shared by: {', '.join(e['shared_by'])}")
            print()

    # Non-LE
    non_le = [e for e in r["flagged_entities"] if "NON_LAW_ENFORCEMENT" in e["flags"]]
    if non_le:
        print(f"{'─' * 60}")
        print(f"NON-LAW-ENFORCEMENT ENTITIES\n")
        for e in sorted(non_le, key=lambda x: -x["shared_by_count"]):
            print(f"  {e.get('name', e['entity'])}")
            if e["shared_by"]:
                print(f"    shared by: {', '.join(e['shared_by'])}")
            print()

    # Asymmetric
    if r["asymmetric_sharing"]:
        print(f"{'─' * 60}")
        print(f"ASYMMETRIC SHARING (A→B but B↛A)\n")
        for pair in r["asymmetric_sharing"][:50]:
            print(f"  {pair['source']}  →  {pair['target']}")
        remaining = len(r["asymmetric_sharing"]) - 50
        if remaining > 0:
            print(f"  ... and {remaining} more")
        print()

    # Most connected
    print(f"{'─' * 60}")
    print(f"MOST CONNECTED (top 20 by inbound sharing count)\n")
    for e in r["most_sharing_inbound"][:20]:
        bar = "█" * min(e["inbound_count"], 40)
        print(f"  {e['inbound_count']:3d}  {bar}  {e['agency_id']}")

    print(f"\n{'=' * 60}")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Flock Safety transparency portal archiver & analyzer",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
        help=f"Data directory (default: {DEFAULT_DATA_DIR})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── crawl ──
    p_crawl = sub.add_parser("crawl", help="Fetch and archive transparency pages")
    p_crawl.add_argument("slugs", nargs="*", help="Agency slugs")
    p_crawl.add_argument("--file", type=Path, help="File with one slug per line")
    p_crawl.add_argument("--related", action="store_true",
                         help="Agencies referenced in the findings document")
    p_crawl.add_argument("--all", action="store_true", dest="all_agencies",
                         help="All agencies with access to San Mateo ALPR data")
    p_crawl.add_argument("--depth", type=int, default=0, metavar="N",
                         help="Recurse N levels deep (implies --all)")
    p_crawl.add_argument("--force", action="store_true",
                         help="Capture even if unchanged")
    p_crawl.add_argument("--delay", type=int, default=0, metavar="SECONDS",
                         help="Delay between requests (jittered ±30%%)")
    p_crawl.add_argument("--batch", type=int, default=0, metavar="N",
                         help="Process at most N agencies then exit")
    p_crawl.add_argument("--proxy", type=str, metavar="URL",
                         help="SOCKS5/HTTP proxy (e.g. socks5://localhost:9050)")
    p_crawl.add_argument("--max-age", type=int, default=STALE_DAYS, metavar="DAYS",
                         help=f"Re-fetch if latest capture is older than DAYS (default: {STALE_DAYS})")
    p_crawl.add_argument("--retry-failed", action="store_true",
                         help="Retry previously failed slugs")
    p_crawl.add_argument("--try-variations", action="store_true",
                         help="On 404, try common slug variations")

    # ── parse ──
    p_parse = sub.add_parser("parse", help="(Re)generate JSON from saved .txt files")
    p_parse.add_argument("--slug", help="Parse only this agency")
    p_parse.add_argument("--force", action="store_true",
                         help="Regenerate even if .json already exists")

    # ── aggregate ──
    p_agg = sub.add_parser("aggregate", help="Build sharing graph and run analysis")
    p_agg.add_argument("--json", action="store_true", dest="json_output",
                       help="Output raw JSON instead of report")
    p_agg.add_argument("--out", type=Path,
                       help="Write JSON results to file")

    args = parser.parse_args()

    if args.command == "crawl":
        cmd_crawl(args)
    elif args.command == "parse":
        cmd_parse(args)
    elif args.command == "aggregate":
        cmd_aggregate(args)


if __name__ == "__main__":
    main()
