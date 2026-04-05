#!/usr/bin/env python3
"""
Screenshot the main site pages for PR preview.

Serves docs/ locally and captures full-page screenshots of each page
using Playwright. Outputs PNGs to a specified directory.

Usage:
  uv run python scripts/screenshot_pages.py --out /tmp/screenshots
"""

import argparse
import http.server
import threading
from pathlib import Path

DOCS_DIR = Path("docs")
PAGES = [
    ("index.html", "index", {"width": 800, "height": 600}),
    ("sharing_map.html", "sharing_map", {"width": 1280, "height": 900}),
    ("scoreboard.html", "scoreboard", {"width": 1280, "height": 900}),
]


def serve_docs(port: int) -> http.server.HTTPServer:
    """Start a background HTTP server for docs/."""
    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main():
    parser = argparse.ArgumentParser(description="Screenshot site pages")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    # Serve from docs/ so relative asset paths work
    import os
    os.chdir(DOCS_DIR)
    server = serve_docs(args.port)
    base = f"http://127.0.0.1:{args.port}"

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for filename, slug, viewport in PAGES:
                page = browser.new_page(viewport=viewport)
                page.goto(f"{base}/{filename}", wait_until="networkidle")
                # Give map tiles a moment to render
                if "map" in slug:
                    page.wait_for_timeout(2000)
                out_path = args.out / f"{slug}.png"
                page.screenshot(path=str(out_path), full_page=(slug == "index"))
                print(f"Captured {out_path}")
                page.close()
            browser.close()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
