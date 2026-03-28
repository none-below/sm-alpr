#!/usr/bin/env python3
"""
Archive Flock Safety transparency portal pages.

Takes a list of agency slugs (or a file of slugs) and captures date-stamped
text-searchable PDFs of each portal.

Skips capture if page content hasn't changed since the last archived version.
Use --force to capture regardless.

Output saved to assets/transparency.flocksafety.com/ as:
  {slug}_{YYYY-MM-DD}.pdf

Usage:
  uv run python scripts/flock_transparency_screenshots.py san-mateo-ca-pd
  uv run python scripts/flock_transparency_screenshots.py san-mateo-ca-pd redwood-city-ca-pd
  uv run python scripts/flock_transparency_screenshots.py --file agencies.txt
  uv run python scripts/flock_transparency_screenshots.py --force san-mateo-ca-pd
"""

import argparse
import base64
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = "https://transparency.flocksafety.com"
DEFAULT_OUT_DIR = Path("assets/transparency.flocksafety.com")
HASH_FILE = ".content_hashes.json"
VIEWPORT = {"width": 1440, "height": 900}
WAIT_MS = 5000  # wait for JS rendering


def load_hashes(out_dir):
    """Load previously saved content hashes."""
    path = out_dir / HASH_FILE
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_hashes(out_dir, hashes):
    """Persist content hashes."""
    path = out_dir / HASH_FILE
    path.write_text(json.dumps(hashes, indent=2) + "\n")


def content_hash(text):
    """SHA-256 hex digest of page text content."""
    return hashlib.sha256(text.encode()).hexdigest()


def archive_agency(page, slug, out_dir, force=False, hashes=None):
    """Navigate to a transparency page, capture PNG + PDF if content changed."""
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

    # Check for changes
    page_text = page.inner_text("body")
    current_hash = content_hash(page_text)
    prev_hash = (hashes or {}).get(slug)

    if not force and prev_hash == current_hash:
        print(f"    unchanged since last capture, skipping")
        return "unchanged"

    # Text-searchable PDF via CDP (works in headed mode)
    cdp = page.context.new_cdp_session(page)
    result = cdp.send("Page.printToPDF", {
        "printBackground": True,
        "preferCSSPageSize": False,
        "paperWidth": 11,      # landscape-ish to fit 1440px content
        "paperHeight": 17,     # tabloid
        "marginTop": 0.4,
        "marginBottom": 0.4,
        "marginLeft": 0.4,
        "marginRight": 0.4,
    })
    cdp.detach()

    pdf_data = base64.b64decode(result["data"])
    pdf_path.write_bytes(pdf_data)
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
    args = parser.parse_args()

    slugs = list(args.slugs)
    if args.file:
        text = args.file.read_text()
        slugs.extend(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    if not slugs:
        print("Error: provide slugs as arguments or via --file", file=sys.stderr)
        sys.exit(1)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    hashes = load_hashes(args.out_dir)

    print(f"Archiving {len(slugs)} agency portal(s):\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport=VIEWPORT,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        results = []
        for slug in slugs:
            result = archive_agency(page, slug, args.out_dir, args.force, hashes)
            results.append((slug, result))

        browser.close()

    save_hashes(args.out_dir, hashes)

    captured = sum(1 for _, r in results if r not in (None, "unchanged"))
    unchanged = sum(1 for _, r in results if r == "unchanged")
    failed = sum(1 for _, r in results if r is None)
    print(f"\nDone: {captured} captured, {unchanged} unchanged, {failed} failed.")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
