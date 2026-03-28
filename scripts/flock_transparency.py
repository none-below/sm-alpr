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

import argparse
import base64
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import date
from pathlib import Path

BASE_URL = "https://transparency.flocksafety.com"
DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")
HASH_FILE = ".content_hashes.json"
FAILED_FILE = ".failed_slugs.json"
VIEWPORT = {"width": 1440, "height": 900}
WAIT_MS = 5000


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
]


def name_to_slug(name):
    s = name.strip().lower()
    s = re.sub(r"\(acso\)", "", s)
    s = re.sub(r"\(ca\)", "ca", s)
    s = re.sub(r"\(smcso\)", "", s)
    s = re.sub(r"[''']s\b", "s", s)
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s


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


# ═══════════════════════════════════════════════════════════
# Parsing: raw DOM text -> structured JSON
# ═══════════════════════════════════════════════════════════

_AGENCY_MARKERS = re.compile(
    r"\b(PD|SO|SD|DA|Police|Sheriff|Office|Patrol|Parks|Fire|NCRIC|Cal Fire"
    r"|Highway Patrol|State Parks|Campus|College|University|Station"
    r"|Department of Public Safety)\b",
    re.IGNORECASE,
)

_SECTION_LABELS = [
    "Overview",
    "What's Detected", "What's Not Detected",
    "Acceptable Use Policy", "Prohibited Uses",
    "Access Policy", "Hotlist Policy",
    "Restrictions on Deployment",
    "Sharing with Partners", "Sharing Restrictions",
    "Data retention (in days)", "Data retention",
    "Data Retention for Flock Devices",
    "Number of LPR and other cameras", "Number of Owned Cameras",
    "Hotlists Alerted On",
    "Organizations granted access", "Approved NCRIC Share With",
    "Agencies NCRIC Shares With",
    "Vehicles detected in the last 30 days", "Vehicles Detected in the Last 30 Days",
    "Hotlist hits in the last 30 days",
    "Searches in the last 30 days", "Searches in the Last 30 Days",
    "Additional Info",
    "Policy Documents", "Policy Page", "ALPR Policy",
    "Provided by Flock Safety",
]


def _extract_between(text, start_label, end_labels):
    idx = text.find(start_label)
    if idx == -1:
        return ""
    start = idx + len(start_label)
    end = len(text)
    for el in end_labels:
        pos = text.find(el, start)
        if pos != -1 and pos < end:
            end = pos
    return text[start:end].strip()


def _parse_number(s):
    if not s:
        return None
    m = re.search(r"[\d,]+", s)
    return int(m.group(0).replace(",", "")) if m else None


def parse_org_list(orgs_text):
    if not orgs_text:
        return []
    _, _, body = orgs_text.partition("\n\n")
    if not body:
        _, _, body = orgs_text.partition("\n")
    raw = [n.strip() for n in body.split(", ") if n.strip()]
    names = []
    for part in raw:
        if names and not _AGENCY_MARKERS.search(part):
            names[-1] = f"{names[-1]}, {part}"
        else:
            names.append(part)
    return names


def parse_portal_text(raw_text, slug, datestamp):
    """Parse structured data from raw DOM text."""
    text = raw_text

    def field(start, *end_labels):
        ends = list(end_labels) if end_labels else _SECTION_LABELS
        return _extract_between(text, start, ends)

    org_text = ""
    for org_label in ["Organizations granted access", "Approved NCRIC Share With",
                       "Agencies NCRIC Shares With"]:
        org_text = field(org_label, "Additional Info", "Hotlists Alerted On",
                         "Policy Documents", "Provided by Flock Safety")
        if org_text:
            break
    org_text = re.sub(r"^.*?data\s*", "", org_text, count=1).strip()
    org_names = parse_org_list(
        f"Organizations granted access\n\n{org_text}" if org_text else ""
    )

    return {
        "slug": slug,
        "archived_date": datestamp,
        "whats_detected": field("What's Detected", "What's Not Detected"),
        "whats_not_detected": field("What's Not Detected",
                                     "Acceptable Use Policy", "Restrictions on Deployment",
                                     "Sharing with Partners", "Usage"),
        "acceptable_use_policy": field("Acceptable Use Policy", "Prohibited Uses"),
        "prohibited_uses": field("Prohibited Uses", "Access Policy"),
        "access_policy": field("Access Policy", "Hotlist Policy"),
        "hotlist_policy": field("Hotlist Policy", "Usage", "Data retention",
                                "Sharing with Partners"),
        "sharing_with_partners": field("Sharing with Partners", "Sharing Restrictions"),
        "sharing_restrictions": field("Sharing Restrictions", "Usage", "Data retention"),
        "data_retention_days": _parse_number(
            field("Data retention (in days)", "Number of") or
            field("Data Retention for Flock Devices", "Number of") or
            field("Data retention", "Number of")
        ),
        "camera_count": _parse_number(
            field("Number of LPR and other cameras", "Hotlists", "Organizations") or
            field("Number of Owned Cameras", "Vehicles", "Organizations")
        ),
        "hotlists_alerted_on": field("Hotlists Alerted On", "Vehicles detected", "Organizations"),
        "vehicles_detected_30d": _parse_number(
            field("Vehicles detected in the last 30 days", "Hotlist hits") or
            field("Vehicles Detected in the Last 30 Days", "Searches")
        ),
        "hotlist_hits_30d": _parse_number(
            field("Hotlist hits in the last 30 days", "Searches", "Organizations", "Additional")
        ),
        "searches_30d": _parse_number(
            field("Searches in the last 30 days", "Additional", "Organizations") or
            field("Searches in the Last 30 Days", "Additional", "Organizations")
        ),
        "shared_org_count": len(org_names),
        "shared_org_names": org_names,
        "shared_org_slugs": [name_to_slug(n) for n in org_names],
    }


# ═══════════════════════════════════════════════════════════
# Crawl: fetch pages, save .txt + .pdf
# ═══════════════════════════════════════════════════════════

def archive_agency(page, slug, data_dir, force=False, hashes=None, progress=""):
    """Returns (status, shared_org_slugs).

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
    print(f"{prefix}{slug} -> {url}", flush=True)

    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"    WARNING: navigation failed: {e}")
        return None, []

    if response and response.status == 429:
        print(f"    RATE LIMITED (429), will retry later")
        return "rate_limited", []

    if response and response.status >= 400:
        print(f"    WARNING: got HTTP {response.status}, skipping")
        return None, []

    page.wait_for_timeout(WAIT_MS)

    page_text = page.inner_text("body")
    expected_sections = ["Policies", "Usage", "What's Detected"]
    if not any(section in page_text for section in expected_sections):
        print(f"    WARNING: page does not look like a transparency portal, skipping")
        return None, []

    current_hash = content_hash(page_text)
    prev_hash = (hashes or {}).get(slug)

    # Parse for shared slugs (needed for recursive crawling even if unchanged)
    portal_data = parse_portal_text(page_text, slug, datestamp)
    shared_slugs = portal_data.get("shared_org_slugs", [])

    if not force and prev_hash == current_hash:
        print(f"    unchanged since last capture, skipping")
        return "unchanged", shared_slugs

    # 1. Raw DOM text (source of truth for parsing)
    txt_path.write_text(page_text, encoding="utf-8")

    # 1b. Full HTML (source of truth for re-rendering)
    html_path = slug_dir / f"{datestamp}.html"
    html_path.write_text(page.content(), encoding="utf-8")

    # 2. Parsed JSON (derived from .txt)
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

    return pdf_path, shared_slugs


def run_crawl_batch(page, slugs, data_dir, force, delay, hashes, failed_slugs):
    """Crawl a list of slugs. Returns (results, discovered_slugs)."""
    results = []
    discovered = []

    total = len(slugs)
    for i, slug in enumerate(slugs):
        progress = f"({i + 1}/{total})"
        if slug in failed_slugs:
            print(f"  {progress} {slug} -> previously failed, skipping")
            results.append((slug, None))
            continue

        status, shared_slugs = archive_agency(page, slug, data_dir, force, hashes, progress=progress)

        if status == "rate_limited":
            for attempt in range(4):
                backoff = max(delay, 30) * (2 ** (attempt + 1))
                print(f"    rate limited, backing off {backoff}s (attempt {attempt + 1}/4)...")
                time.sleep(backoff)
                status, shared_slugs = archive_agency(page, slug, data_dir, force, hashes)
                if status != "rate_limited":
                    break
            if status == "rate_limited":
                print(f"    still rate limited after 4 retries, skipping for now")
                results.append((slug, None))
                save_json(data_dir / HASH_FILE, hashes)
                continue

        results.append((slug, status))
        if shared_slugs:
            discovered.extend(shared_slugs)
        if status is None:
            failed_slugs[slug] = date.today().isoformat()

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
            visited.update(hashes.keys())
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
                        jsons = sorted(slug_dir.glob("*.json"))
                        if jsons:
                            stored = json.loads(jsons[-1].read_text())
                            discovered_from_existing.extend(
                                stored.get("shared_org_slugs", [])
                            )

                new_slugs = [s for s in slugs if s not in visited]
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
            )
            all_results.extend(results)

        browser.close()

    captured = sum(1 for _, r in all_results if r not in (None, "unchanged"))
    unchanged = sum(1 for _, r in all_results if r == "unchanged")
    failed = sum(1 for _, r in all_results if r is None)
    print(f"\nDone: {captured} captured, {unchanged} unchanged, {failed} failed.")

    if failed and not captured and not unchanged:
        sys.exit(1)


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
        for txt_path in sorted(slug_dir.glob("*.txt")):
            datestamp = txt_path.stem  # e.g. "2026-03-27"
            json_path = slug_dir / f"{datestamp}.json"

            if json_path.exists() and not args.force:
                continue

            raw_text = txt_path.read_text(encoding="utf-8")
            portal_data = parse_portal_text(raw_text, slug, datestamp)
            json_path.write_text(json.dumps(portal_data, indent=2) + "\n")
            cameras = portal_data.get("camera_count") or "?"
            orgs = portal_data.get("shared_org_count", 0)
            print(f"  {slug}/{datestamp}.json — {cameras} cameras, {orgs} orgs")
            count += 1

    print(f"\nParsed {count} file(s).")


# ═══════════════════════════════════════════════════════════
# Aggregate: build sharing graph + analysis from .json files
# ═══════════════════════════════════════════════════════════

# Entity classification patterns
_PUBLIC_LE = re.compile(
    r"\b(pd|police|sheriff|so|sd|da|district-attorney|highway-patrol"
    r"|state-parks|state-police|marshal|constable|ranger"
    r"|probation|corrections|parole)\b", re.IGNORECASE)

_UNIVERSITY = re.compile(
    r"\b(university|college|campus|cal-state|uc-|csu-)\b", re.IGNORECASE)

_CA_PUBLIC_UNI_FRAGMENTS = {
    "uc", "csu", "cal-state", "cal-poly",
    "california-state-university", "university-of-california",
    "san-jose-state", "foothill-deanza", "rio-hondo-college",
    "sequoias-community-college", "san-joaquin-delta-college",
    "cerritos-college", "west-valley-mission-college",
    "san-jose-evergreen-community-college",
    "san-bernardino-community-college",
}

_KNOWN_PRIVATE = {
    "university-of-the-pacific", "stanford", "university-of-san-francisco",
    "usc", "santa-clara-university", "loyola-marymount",
    "pepperdine", "chapman", "azusa-pacific",
}

_CA_INDICATOR = re.compile(
    r"(-ca-|-ca$|^ca-|california|cal-fire|cal-state|ncric"
    r"|-so-ca|-da-ca|county-ca)", re.IGNORECASE)

_NON_LE = re.compile(
    r"\b(fire-authority|fire-department|fire-district|housing-authority"
    r"|water-district|transit|school-district)\b", re.IGNORECASE)


def classify_entity(slug):
    flags = []
    if _UNIVERSITY.search(slug):
        is_known_private = any(p in slug for p in _KNOWN_PRIVATE)
        is_public = any(p in slug for p in _CA_PUBLIC_UNI_FRAGMENTS)
        if is_known_private:
            flags.append("PRIVATE_UNIVERSITY")
        elif not is_public:
            flags.append("POSSIBLY_PRIVATE_UNIVERSITY")
    if _NON_LE.search(slug):
        flags.append("NON_LAW_ENFORCEMENT")
    if not _CA_INDICATOR.search(slug):
        flags.append("POSSIBLY_OUT_OF_STATE")
    return flags


def cmd_aggregate(args):
    data_dir = args.data_dir

    # Load the latest JSON for each agency
    agencies = {}
    sharing_graph = {}
    for slug_dir in sorted(data_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue
        jsons = sorted(slug_dir.glob("*.json"))
        if not jsons:
            continue
        latest = jsons[-1]
        data = json.loads(latest.read_text())
        slug = slug_dir.name
        agencies[slug] = data
        if data.get("shared_org_slugs"):
            sharing_graph[slug] = data["shared_org_slugs"]

    if not agencies:
        print("No parsed data found. Run 'parse' first.", file=sys.stderr)
        sys.exit(1)

    # Collect all entities
    all_entities = set(sharing_graph.keys())
    for slugs in sharing_graph.values():
        all_entities.update(slugs)

    # Inbound counts
    inbound = Counter()
    for src, targets in sharing_graph.items():
        for t in targets:
            inbound[t] += 1

    # Classify and flag
    flagged = []
    for entity in sorted(all_entities):
        flags = classify_entity(entity)
        if not flags:
            continue
        shared_by = [src for src, targets in sharing_graph.items() if entity in targets]
        flagged.append({
            "entity": entity, "flags": flags,
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
        "agencies": {slug: {
            "camera_count": a.get("camera_count"),
            "data_retention_days": a.get("data_retention_days"),
            "vehicles_detected_30d": a.get("vehicles_detected_30d"),
            "hotlist_hits_30d": a.get("hotlist_hits_30d"),
            "searches_30d": a.get("searches_30d"),
            "shared_org_count": a.get("shared_org_count"),
            "archived_date": a.get("archived_date"),
        } for slug, a in sorted(agencies.items())},
        "flagged_entities": flagged,
        "asymmetric_sharing": asymmetric,
        "most_connected": [
            {"entity": s, "inbound_count": c} for s, c in inbound.most_common(30)
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
        orgs = info.get("shared_org_count") or "?"
        dt = info.get("archived_date") or "?"
        print(f"  {slug:<40} {cam:>4} {ret:>4} {orgs:>5} {dt:<12}")

    # Flagged universities
    private = [e for e in r["flagged_entities"] if any("UNIVERSITY" in f for f in e["flags"])]
    if private:
        print(f"\n{'─' * 60}")
        print(f"PRIVATE / POSSIBLY PRIVATE UNIVERSITIES")
        print(f"  CA law (§1798.90.55(b)) restricts sharing to public agencies\n")
        for e in sorted(private, key=lambda x: -x["shared_by_count"]):
            print(f"  {e['entity']}  [{', '.join(e['flags'])}]")
            if e["shared_by"]:
                print(f"    shared by: {', '.join(e['shared_by'])}")
            print()

    # Out of state
    oos = [e for e in r["flagged_entities"] if "POSSIBLY_OUT_OF_STATE" in e["flags"]
           and not any("UNIVERSITY" in f for f in e["flags"])]
    if oos:
        print(f"{'─' * 60}")
        print(f"POSSIBLY OUT-OF-STATE ENTITIES\n")
        for e in sorted(oos, key=lambda x: -x["shared_by_count"]):
            print(f"  {e['entity']}")
            if e["shared_by"]:
                print(f"    shared by: {', '.join(e['shared_by'])}")
            print()

    # Non-LE
    non_le = [e for e in r["flagged_entities"] if "NON_LAW_ENFORCEMENT" in e["flags"]]
    if non_le:
        print(f"{'─' * 60}")
        print(f"NON-LAW-ENFORCEMENT ENTITIES\n")
        for e in sorted(non_le, key=lambda x: -x["shared_by_count"]):
            print(f"  {e['entity']}")
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
    for e in r["most_connected"][:20]:
        bar = "█" * min(e["inbound_count"], 40)
        print(f"  {e['inbound_count']:3d}  {bar}  {e['entity']}")

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
    p_crawl.add_argument("--retry-failed", action="store_true",
                         help="Retry previously failed slugs")

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
