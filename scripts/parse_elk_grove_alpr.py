#!/usr/bin/env python3
"""Deterministic parser for the Elk Grove PD self-hosted ALPR transparency page.

Elk Grove publishes its ALPR program info on elkgrove.gov in Drupal `faqfield`
accordions instead of using a Flock-hosted transparency portal. This parser
extracts the same fields the Flock parser emits, so downstream graph/scoreboard
builders can ingest it identically.

Untrusted-content rule (CLAUDE.md): treats input HTML as third-party scraped
content. Output JSON is the safe view.

Usage:
  uv run python scripts/parse_elk_grove_alpr.py \\
      --html PATH \\
      --date 2025-12-31 \\
      --out assets/transparency.elkgrove.gov/elk-grove-ca-pd/2025-12-31.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from html import unescape
from pathlib import Path


# ── Field labels Elk Grove uses on the page (Drupal faqfield summaries) ──

LABELS = {
    "whats_detected": "What's Detected",
    "whats_not_detected": "What's Not Detected",
    "acceptable_use_policy": "Acceptable Use Policy",
    "prohibited_uses": "Prohibited Uses",
    "access_policy": "Access Policy",
    "hotlist_policy": "Hotlist Policy",
    "data_retention": "Data retention",
    "camera_count": "Number of owned cameras",
    "sharing_outbound_v1": "External agencies who have access",
    "sharing_outbound_v2": "External agencies who EGPD shares ALPR data with",
    "sharing_inbound": "Agencies who share their data with EGPD",
    "myth_busters": "Myth Busters and Facts",
}


_FAQ_BLOCK_RE = re.compile(
    r'<details[^>]*class="[^"]*faqfield-details[^"]*"[^>]*>\s*'
    r'<summary[^>]*class="[^"]*faqfield-question[^"]*"[^>]*>(?P<q>.*?)</summary>\s*'
    r'<div[^>]*class="[^"]*faqfield-answer[^"]*"[^>]*>(?P<a>.*?)</div>\s*'
    r'</details>',
    re.IGNORECASE | re.DOTALL,
)


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p>\s*<p[^>]*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    return s.strip()


_ORPHAN_PD_RE = re.compile(r"^PD\b\s*[,]?\s*(.*)$")


def _split_agency_list(text: str) -> list[str]:
    """Each agency is on its own line (was a <br/>). Trim and dedupe stable-order.

    Handles a known EG typo: a stray <br/> inside an agency name produces
    a line like "PD, Next Org" that should re-attach to the previous line.
    """
    # Step 1: line-split + orphan-PD repair
    raw_lines = [l.strip().rstrip(",").strip() for l in text.splitlines()]
    raw_lines = [l for l in raw_lines if l]

    repaired = []
    for line in raw_lines:
        m = _ORPHAN_PD_RE.match(line)
        if m and repaired:
            # Merge "PD" back onto the previous line. If the orphan line
            # also carries additional entries after a comma, those become
            # their own lines.
            repaired[-1] = f"{repaired[-1]} PD"
            tail = m.group(1).strip()
            if tail:
                for piece in tail.split(", "):
                    piece = piece.strip()
                    if piece:
                        repaired.append(piece)
        else:
            repaired.append(line)

    # Step 2: dedupe stable-order
    names = []
    seen = set()
    for n in repaired:
        if n in seen:
            continue
        seen.add(n)
        names.append(n)
    return names


def extract_faq_blocks(html: str) -> dict[str, str]:
    """Return {question_text: answer_text_plain} from all faqfield-details elements."""
    blocks = {}
    for m in _FAQ_BLOCK_RE.finditer(html):
        q = _strip_html(m.group("q"))
        a = _strip_html(m.group("a"))
        # Some pages have repeated questions; first wins (matches DOM order).
        if q and q not in blocks:
            blocks[q] = a
    return blocks


def parse(html: str, *, snapshot_date: str, source_url: str) -> dict:
    blocks = extract_faq_blocks(html)

    def field(label_key):
        return blocks.get(LABELS[label_key], "")

    outbound_raw = field("sharing_outbound_v2") or field("sharing_outbound_v1")
    inbound_raw = field("sharing_inbound")

    outbound = _split_agency_list(outbound_raw)
    inbound = _split_agency_list(inbound_raw)

    # Retention as integer days where parseable
    retention_raw = field("data_retention")
    m = re.search(r"(\d+)\s*day", retention_raw, re.IGNORECASE)
    retention_days = int(m.group(1)) if m else None

    # Camera count
    cam_raw = field("camera_count")
    m = re.search(r"(\d+)", cam_raw)
    cameras = int(m.group(1)) if m else None

    return {
        # Source metadata
        "source_kind": "self_hosted",
        "source_url": source_url,
        "snapshot_date": snapshot_date,
        "archived_date": snapshot_date,
        "crawled_slug": "elk-grove-ca-pd",
        "crawled_name": "Elk Grove CA PD",
        # Flock-shaped fields for graph builder compatibility
        "camera_count": cameras,
        "data_retention_days": retention_days,
        "prohibited_uses": field("prohibited_uses"),
        "acceptable_use_policy": field("acceptable_use_policy"),
        "access_policy": field("access_policy"),
        "hotlist_policy": field("hotlist_policy"),
        "sharing_outbound": outbound,
        "sharing_inbound": inbound,
        # Self-hosted-specific extras (no Flock equivalent)
        "whats_detected": field("whats_detected"),
        "whats_not_detected": field("whats_not_detected"),
        "myth_busters": field("myth_busters"),
        # Diagnostics: which heading actually supplied the outbound list,
        # because EG renamed it between Dec 2025 and May 2026.
        "_outbound_heading": (
            LABELS["sharing_outbound_v2"] if field("sharing_outbound_v2")
            else LABELS["sharing_outbound_v1"] if field("sharing_outbound_v1")
            else None
        ),
        "_inbound_present": bool(inbound_raw),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", type=Path, required=True,
                    help="Path to source HTML (untrusted scraped content)")
    ap.add_argument("--date", required=True, help="Snapshot date YYYY-MM-DD")
    ap.add_argument("--source-url", required=True,
                    help="Original URL the HTML was fetched from")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    html = args.html.read_text(encoding="utf-8", errors="replace")
    result = parse(html, snapshot_date=args.date, source_url=args.source_url)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    # Companion .txt: build_sharing_graph.py skips slug-dirs lacking .txt
    # files. We populate sharing_inbound directly in JSON, so this only
    # needs to exist; its content is a deterministic stub.
    txt_path = args.out.with_suffix(".txt")
    stub = (
        f"Self-hosted ALPR transparency snapshot for Elk Grove PD.\n"
        f"Source: {args.source_url}\n"
        f"Snapshot date: {args.date}\n"
        f"Parsed fields are in {args.out.name}.\n"
    )
    txt_path.write_text(stub)

    print(f"wrote {args.out}", file=sys.stderr)
    print(f"  outbound: {len(result['sharing_outbound'])}  "
          f"inbound: {len(result['sharing_inbound'])}  "
          f"retention: {result['data_retention_days']}d  "
          f"cameras: {result['camera_count']}", file=sys.stderr)


if __name__ == "__main__":
    main()
