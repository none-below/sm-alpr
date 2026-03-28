#!/usr/bin/env python3
"""
Archive Flock Safety transparency portal pages.

Captures date-stamped text-searchable PDFs of each portal. Skips capture
if page content hasn't changed since the last archived version.

Modes:
  (default)     San Mateo only
  --related     Agencies referenced in the findings document
  --all         All agencies with access to San Mateo ALPR data (scraped live)
  <slugs>       Specific slugs

Output saved to assets/transparency.flocksafety.com/ as:
  {slug}_{YYYY-MM-DD}.pdf

Usage:
  uv run python scripts/flock_transparency_screenshots.py
  uv run python scripts/flock_transparency_screenshots.py --related
  uv run python scripts/flock_transparency_screenshots.py --all --delay 30
  uv run python scripts/flock_transparency_screenshots.py --file agencies.txt
  uv run python scripts/flock_transparency_screenshots.py --force san-mateo-ca-pd
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
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "https://transparency.flocksafety.com"
DEFAULT_OUT_DIR = Path("assets/transparency.flocksafety.com")
HASH_FILE = ".content_hashes.json"
VIEWPORT = {"width": 1440, "height": 900}
WAIT_MS = 5000  # wait for JS rendering

# ── Agency tiers ──

# Default: just San Mateo
DEFAULT_SLUGS = [
    "san-mateo-ca-pd",
]

# Related: agencies directly referenced in the findings document
RELATED_SLUGS = DEFAULT_SLUGS + [
    "stockton-ca-pd",
    "ncric",
    "redwood-city-ca-pd",
    "belmont-ca-pd",
    "san-mateo-county-ca-so",
    "daly-city-ca-pd",
    "foster-city-ca-pd",
    "south-san-francisco-ca-pd",
    "atherton-ca-pd",
    "hillsborough-ca-pd",
    "menlo-park-ca-pd",
    "east-palo-alto-ca-pd",
    "burlingame-ca-pd",
    "san-bruno-ca-pd",
    "pacifica-ca-pd",
    "colma-ca-pd",
    "brisbane-ca-pd",
]


def name_to_slug(name):
    """Best-effort conversion of a Flock display name to a URL slug."""
    s = name.strip()
    s = s.lower()
    # Normalize common patterns
    s = re.sub(r"\(acso\)", "", s)
    s = re.sub(r"\(ca\)", "ca", s)
    s = re.sub(r"\(smcso\)", "", s)
    s = re.sub(r"[''']s\b", "s", s)  # possessives
    s = re.sub(r"[^a-z0-9\s-]", "", s)  # drop parens, punctuation
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s


def scrape_shared_agencies(page, source_slug="san-mateo-ca-pd"):
    """Scrape the list of orgs with shared access from a transparency page."""
    url = f"{BASE_URL}/{source_slug}"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(WAIT_MS)

    orgs_text = page.evaluate("""() => {
        const all = document.querySelectorAll('*');
        for (const el of all) {
            if (el.innerText && el.innerText.startsWith('Organizations granted access')) {
                return el.innerText;
            }
        }
        return '';
    }""")

    if not orgs_text:
        return []

    # Strip header line
    _, _, body = orgs_text.partition("\n\n")
    if not body:
        _, _, body = orgs_text.partition("\n")

    raw = [n.strip() for n in body.split(", ") if n.strip()]

    # Rejoin names that Flock split on internal commas.
    # A standalone fragment (no "PD", "SO", "DA", "Office", etc.) is likely
    # the second half of the previous entry.
    agency_markers = re.compile(
        r"\b(PD|SO|SD|DA|Police|Sheriff|Office|Patrol|Parks|Fire|NCRIC|Cal Fire"
        r"|Highway Patrol|State Parks|Campus|College|University|Station)\b",
        re.IGNORECASE,
    )
    names = []
    for part in raw:
        if names and not agency_markers.search(part):
            # Likely a split fragment — rejoin with previous
            names[-1] = f"{names[-1]}, {part}"
        else:
            names.append(part)

    return names


# ── Archiving ──

def load_hashes(out_dir):
    """Load previously saved content hashes."""
    path = out_dir / HASH_FILE
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_hashes(out_dir, hashes):
    """Persist content hashes."""
    path = out_dir / HASH_FILE
    path.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n")


def content_hash(text):
    """SHA-256 hex digest of page text content."""
    return hashlib.sha256(text.encode()).hexdigest()


def archive_agency(page, slug, out_dir, force=False, hashes=None):
    """Navigate to a transparency page, capture PDF if content changed."""
    url = f"{BASE_URL}/{slug}"
    datestamp = date.today().isoformat()
    pdf_path = out_dir / f"{slug}_{datestamp}.pdf"

    print(f"  {slug} -> {url}")
    response = page.goto(url, wait_until="domcontentloaded", timeout=30000)

    if response and response.status >= 400:
        print(f"    WARNING: got HTTP {response.status}, skipping")
        return None

    # Wait for client-side rendering
    page.wait_for_timeout(WAIT_MS)

    # Validate that the page is a real transparency portal
    page_text = page.inner_text("body")
    expected_sections = ["Policies", "Usage", "What's Detected"]
    if not any(section in page_text for section in expected_sections):
        print(f"    WARNING: page does not look like a transparency portal, skipping")
        return None

    current_hash = content_hash(page_text)
    prev_hash = (hashes or {}).get(slug)

    if not force and prev_hash == current_hash:
        print(f"    unchanged since last capture, skipping")
        return "unchanged"

    # Text-searchable PDF via CDP
    cdp = page.context.new_cdp_session(page)
    result = cdp.send("Page.printToPDF", {
        "printBackground": True,
        "preferCSSPageSize": False,
        "paperWidth": 11,
        "paperHeight": 17,
        "marginTop": 0.4,
        "marginBottom": 0.4,
        "marginLeft": 0.4,
        "marginRight": 0.4,
    })
    cdp.detach()

    pdf_data = base64.b64decode(result["data"])

    # Write to temp file, then atomically move into place
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf", dir=str(out_dir))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_data)
        Path(tmp_path).replace(pdf_path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    print(f"    saved {pdf_path}")

    # Update hash
    if hashes is not None:
        hashes[slug] = current_hash

    return pdf_path


def main():
    parser = argparse.ArgumentParser(
        description="Archive Flock Safety transparency portals"
    )
    parser.add_argument(
        "slugs",
        nargs="*",
        help="Agency slugs (e.g. san-mateo-ca-pd)",
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="File with one slug per line",
    )
    parser.add_argument(
        "--related",
        action="store_true",
        help="Archive agencies referenced in the findings document",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_agencies",
        help="Archive all agencies with access to San Mateo ALPR data",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Capture even if content hasn't changed",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Base delay between requests (jittered ±30%%, default: 0)",
    )
    args = parser.parse_args()

    slugs = list(args.slugs)
    if args.file:
        text = args.file.read_text()
        slugs.extend(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    hashes = load_hashes(args.out_dir)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--headless=new"])
        context = browser.new_context(
            viewport=VIEWPORT,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Resolve slug list based on mode
        if args.all_agencies:
            print("Scraping shared agency list from san-mateo-ca-pd...\n")
            names = scrape_shared_agencies(page)
            scraped_slugs = [name_to_slug(n) for n in names]
            # Always include san-mateo-ca-pd itself
            slugs = ["san-mateo-ca-pd"] + [s for s in scraped_slugs if s != "san-mateo-ca-pd"]
            print(f"  found {len(names)} agencies, {len(slugs)} slugs to check\n")
        elif args.related and not slugs:
            slugs = list(RELATED_SLUGS)
        elif not slugs:
            slugs = list(DEFAULT_SLUGS)

        # Deduplicate preserving order
        seen = set()
        unique_slugs = []
        for s in slugs:
            if s not in seen:
                seen.add(s)
                unique_slugs.append(s)
        slugs = unique_slugs

        if not args.delay and len(slugs) > 10:
            args.delay = 15
            print(f"  auto-setting --delay {args.delay}s for {len(slugs)} agencies\n")

        print(f"Archiving {len(slugs)} agency portal(s):\n")

        results = []
        for i, slug in enumerate(slugs):
            result = archive_agency(page, slug, args.out_dir, args.force, hashes)
            results.append((slug, result))
            save_hashes(args.out_dir, hashes)
            if args.delay and i < len(slugs) - 1:
                jitter = args.delay * random.uniform(0.7, 1.3)
                print(f"    waiting {jitter:.0f}s...")
                time.sleep(jitter)

        browser.close()

    save_hashes(args.out_dir, hashes)

    captured = sum(1 for _, r in results if r not in (None, "unchanged"))
    unchanged = sum(1 for _, r in results if r == "unchanged")
    failed = sum(1 for _, r in results if r is None)
    print(f"\nDone: {captured} captured, {unchanged} unchanged, {failed} failed.")

    if failed and not captured:
        sys.exit(1)


if __name__ == "__main__":
    main()
