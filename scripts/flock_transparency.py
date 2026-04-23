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
import os
import random
import re
import sys
import tempfile
import time
import urllib.parse
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import resolve_agency, name_to_slug, portal_jsons, portal_txts

BASE_URL = "https://transparency.flocksafety.com"
DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")
HASH_FILE = ".content_hashes.json"
FAILED_FILE = ".failed_slugs.json"
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


def load_json(path):
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def dedupe(slugs):
    seen = set()
    return [s for s in slugs if not (s in seen or seen.add(s))]


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


# ═══════════════════════════════════════════════════════════
# Parsing: raw DOM text -> structured JSON
# ═══════════════════════════════════════════════════════════

# Pattern for names that expect a comma continuation (e.g. "University of California, Berkeley")
_EXPECTS_CONTINUATION = re.compile(
    r"University of California$",
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
    "ALPR POLICY":                           "alpr_policy",
    "ALPR Manual":                           "alpr_policy",
    "Alpr policy":                           "alpr_policy",
    "Full ALPR Policy":                      "alpr_policy",
    "Full ALPR Policy:":                     "alpr_policy",
    "Full ALPR Policy Here:":                "alpr_policy",
    "Full LPR Policy Here:":                 "alpr_policy",
    "Complete ALPR Policy":                  "alpr_policy",
    "OPD Policy: DGO I-12 - Automated License Plate Readers": "alpr_policy",
    # ── other structural fields ──
    "Additional Info":                       "additional_info",
    "Additional Information":                "additional_info",
    "Download CSV":                          "download_csv",
    "Public Search Audit":                   "search_audit",
    "Search Audit":                          "search_audit",
    "Delivery address":                      "delivery_address",
    "Sharing":                               "sharing_info",
    "Success Story":                         "success_stories",
    "Recent Success Story":                  "success_stories",
    "Recent Success Stories":                "success_stories",
    "Success Stories":                       "success_stories",
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
    "Sharing Restrictions":                  "sharing_restrictions",
    "Data retention (in days)":              "data_retention",
    "Data retention":                        "data_retention",
    "Data Retention (days)":                 "data_retention",
    "Data Retention for Flock Devices":      "data_retention",
    "Flock Data retention (in days)":        "data_retention",
    "Number of LPR and other cameras":       "camera_count",
    "Number of LPR cameras":                 "camera_count",
    "Number of LPR Cameras":                 "camera_count",
    "Number of Active LPR cameras":          "camera_count",
    "Number of Owned Cameras":               "camera_count",
    "Hotlists Alerted On":                   "hotlists_alerted_on",
    "Vehicles detected in the last 30 days": "vehicles_detected_30d",
    "Vehicles Detected in the Last 30 Days": "vehicles_detected_30d",
    "Unique vehicles detected in the last 30 days": "vehicles_detected_30d",
    "Hotlist hits in the last 30 days":      "hotlist_hits_30d",
    "Hotlist Hits in the Last 30 Days":      "hotlist_hits_30d",
    "Searches in the last 30 days":          "searches_30d",
    "Searches in the Last 30 Days":          "searches_30d",
    "Livermore PD searches in the last 30 days": "searches_30d",
    # org sharing — prefix match handles "Organizations granted access to X data"
    "Organizations granted access":          "orgs_granted_access",
    "Approved NCRIC Share With":             "orgs_granted_access",
    "Agencies NCRIC Shares With":            "orgs_granted_access",
    "External agencies who have access":     "orgs_granted_access",
    "Only Agencies With External Access":    "orgs_granted_access",
    "Organizations sharing their data":      "orgs_sharing_with",
}

# Dynamic heading patterns — matched after exact/prefix lookup fails.
# These are headings that contain variable text (agency names, URLs, etc.)
# Map to a field name or None for structural.
_DYNAMIC_HEADINGS = [
    (re.compile(r"^Last Updated:"), "last_updated"),
    (re.compile(r"^(Link to |Link To |To view ).+"), "policy_info"),
    (re.compile(r"^(Full ALPR|Full LPR|Full ALPRY).+"), "alpr_policy"),
    (re.compile(r"^.+ (ALPR|LPR) Policy.*$"), "alpr_policy"),
    (re.compile(r"^.+Police Department Policy Manual.*$"), "alpr_policy"),
]

_MAX_HEADING_LEN = 120


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
    """
    if line in _HEADING_MAP:
        return _HEADING_MAP[line], "exact"
    for prefix, field_name in _HEADING_MAP.items():
        if line.startswith(prefix):
            return field_name, "prefix"
    for pattern, field_name in _DYNAMIC_HEADINGS:
        if pattern.match(line):
            return field_name, "dynamic"
    return _UNKNOWN, "unknown"


_UNKNOWN = object()  # sentinel — distinct from None (which means "structural")


def extract_bold_headings(html):
    """Extract bold text from HTML — these are the real section headings.

    Flock transparency pages use font-weight: 700 on <p> and <h3> elements
    for section headings.  Returns a set of stripped text strings.
    """
    return set(
        re.sub(r"<[^>]+>", "", m).strip()
        for m in re.findall(
            r"<(?:p|h[1-6])[^>]*style=\"[^\"]*font-weight:\s*700[^\"]*\"[^>]*>(.*?)</(?:p|h[1-6])>",
            html,
            re.DOTALL,
        )
        if re.sub(r"<[^>]+>", "", m).strip()
    )


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


def _parse_number(s):
    if not s:
        return None
    m = re.search(r"[\d,]+", s)
    return int(m.group(0).replace(",", "")) if m else None


def _parse_org_names(body):
    """Extract org names from a shared-orgs section body."""
    if not body:
        return []
    raw = [n.strip() for n in body.split(", ") if n.strip()]
    names = []
    for part in raw:
        if names and _EXPECTS_CONTINUATION.search(names[-1]):
            names[-1] = f"{names[-1]}, {part}"
        else:
            names.append(part)
    return names


def parse_portal_text(raw_text, slug, datestamp, bold_headings=None):
    """Parse structured data from raw DOM text."""
    sections, unknown = parse_sections(raw_text, bold_headings=bold_headings)

    if unknown:
        raise ValueError(
            f"Unrecognised headings in {slug}: {unknown}  — add them to _HEADING_MAP"
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

    # Extract the agency name from the overview text
    # Pattern: "<Agency Name> uses Flock Safety technology..."
    overview = fields.get("overview", "")
    crawled_name = None
    flock_marker = " uses Flock Safety technology"
    if flock_marker in overview:
        crawled_name = overview[:overview.index(flock_marker)].strip()

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
        "data_retention_days": _parse_number(fields.get("data_retention", "")),
        "camera_count": _parse_number(fields.get("camera_count", "")),
        "hotlists_alerted_on": fields.get("hotlists_alerted_on", ""),
        "vehicles_detected_30d": _parse_number(fields.get("vehicles_detected_30d", "")),
        "hotlist_hits_30d": _parse_number(fields.get("hotlist_hits_30d", "")),
        "searches_30d": _parse_number(fields.get("searches_30d", "")),
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


def extract_csvs_from_html(html_text):
    """Parse HTML and return list of (filename, [row_dicts]) for embedded CSVs."""
    parser = _CSVLinkExtractor()
    parser.feed(html_text)
    results = []
    for filename, csv_text in parser.csvs:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        results.append((filename, rows))
    return results


# ═══════════════════════════════════════════════════════════
# Crawl: fetch pages, save .txt + .pdf
# ═══════════════════════════════════════════════════════════

def archive_agency(page, slug, data_dir, force=False, hashes=None, progress=""):
    """Returns (status, discovered_slugs).

    status: Path (saved), "unchanged", "rate_limited", or None (failed).
    """
    url = f"{BASE_URL}/{slug}"
    datestamp = date.today().isoformat()
    slug_dir = data_dir / slug
    slug_dir.mkdir(parents=True, exist_ok=True)

    txt_path = slug_dir / f"{datestamp}.txt"
    json_path = slug_dir / f"{datestamp}.json"
    pdf_path = slug_dir / f"{datestamp}.pdf"

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

    # Parse for sharing names (needed for recursive crawling even if unchanged)
    crawled_at = datetime.now(timezone.utc).isoformat()
    page_html = page.content()
    bold_headings = extract_bold_headings(page_html)
    portal_data = parse_portal_text(page_text, slug, datestamp, bold_headings=bold_headings)
    portal_data["crawled_at"] = crawled_at
    # Resolve sharing names to slugs for depth crawling
    discovered_slugs = []
    for name in portal_data.get("sharing_outbound", []):
        entry = resolve_agency(name=name)
        if entry and entry.get("flock_active_slug"):
            discovered_slugs.append(entry["flock_active_slug"])
        else:
            guessed = name_to_slug(name)
            if guessed:
                discovered_slugs.append(guessed)

    if not force and prev_hash == current_hash:
        print(f"    unchanged since last capture, skipping")
        return "unchanged", discovered_slugs

    # 1. Raw DOM text (source of truth for parsing)
    txt_path.write_text(page_text, encoding="utf-8")

    # 1b. Full HTML (source of truth for re-rendering + CSV extraction)
    html_path = slug_dir / f"{datestamp}.html"
    html_path.write_text(page_html, encoding="utf-8")

    # 1c. Extract embedded CSVs from HTML
    for csv_name, csv_rows in extract_csvs_from_html(page_html):
        field = csv_name.replace(".csv", "").replace("-", "_") + "_csv"
        portal_data[field] = csv_rows
        print(f"    extracted {csv_name}: {len(csv_rows)} rows")

    # 2. Parsed JSON (derived from .txt + html)
    json_path.write_text(json.dumps(portal_data, indent=2) + "\n")

    # 3. PDF visual archive
    cdp = page.context.new_cdp_session(page)
    result = cdp.send("Page.printToPDF", {
        "printBackground": True, "preferCSSPageSize": False,
        "paperWidth": 11, "paperHeight": 17,
        "marginTop": 0.4, "marginBottom": 0.4,
        "marginLeft": 0.4, "marginRight": 0.4,
    })
    cdp.detach()
    pdf_data = base64.b64decode(result["data"])

    fd, tmp_path = tempfile.mkstemp(suffix=".pdf", dir=str(slug_dir))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_data)
        Path(tmp_path).replace(pdf_path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise

    print(f"    saved {slug}/{datestamp}.{{txt,json,pdf}}")

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
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
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
        batch_remaining = args.batch if args.batch else float("inf")

        if args.all_agencies or args.depth:
            max_depth = args.depth if args.depth else 1
            for level in range(max_depth + 1):
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
                                else:
                                    guessed = name_to_slug(name)
                                    if guessed:
                                        discovered_from_existing.append(guessed)

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
                    last = latest_capture_date(s, data_dir)
                    if last is not None:
                        return (1, last)
                    if s in previously_failed:
                        return (2, date.min)
                    return (0, date.min)
                new_slugs.sort(key=_order)
                if args.batch:
                    new_slugs = new_slugs[:int(batch_remaining)]

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
                    batch_remaining -= len(new_slugs)
                    discovered.extend(newly_discovered)

                    if batch_remaining <= 0:
                        print(f"\n  batch limit reached, stopping.\n")
                        break

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

            portal_data = parse_portal_text(raw_text, slug, datestamp, bold_headings=bold_headings)

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

    print(f"\nParsed {count} file(s).")


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
