#!/usr/bin/env python3
"""
Fetch and OCR San Mateo City Council meeting packets.

Pipeline:
  fetch     Download packet PDFs from PrimeGov API (gitignored)
  ocr       Generate page-numbered text sidecars from downloaded PDFs
  index     Build searchable metadata index from all fetched packets

Directory structure:
  assets/council-packets/
    index.json                        # metadata for all packets
    {meeting-id}_{date}/
      packet.pdf                      # full compiled packet (gitignored)
      packet.txt                      # OCR/text extraction with page markers
      metadata.json                   # meeting info + source URL

Usage:
  uv run python scripts/council_packets.py fetch
  uv run python scripts/council_packets.py fetch --year 2023 --year 2024
  uv run python scripts/council_packets.py fetch --meeting-id 1391
  uv run python scripts/council_packets.py ocr
  uv run python scripts/council_packets.py ocr --meeting-id 1391
  uv run python scripts/council_packets.py index
"""

import argparse
import hashlib
import io
import json
import os
import random
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PRIMEGOV_API = "https://sanmateo.primegov.com/api/v2/PublicPortal"
PACKET_URL = "https://sanmateo.primegov.com/Public/CompiledDocument"
DEFAULT_DATA_DIR = Path("assets/san-mateo/council-packets")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Years covering the Flock timeline
DEFAULT_YEARS = [2020, 2021, 2022, 2023, 2024, 2025, 2026]


# ═══════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════

def fetch_meetings_for_year(year):
    """Fetch all meetings for a given year from PrimeGov API."""
    url = f"{PRIMEGOV_API}/ListArchivedMeetings?year={year}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"    WARNING: failed to fetch {year}: {e}")
        return []


def fetch_upcoming_meetings():
    """Fetch upcoming meetings from PrimeGov API."""
    url = f"{PRIMEGOV_API}/ListUpcomingMeetings"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"    WARNING: failed to fetch upcoming: {e}")
        return []


def find_packet_template_id(meeting):
    """Find the templateId for the compiled packet PDF."""
    for doc in meeting.get("documentList", []):
        if doc.get("templateName") == "Packet" and doc.get("compileOutputType") == 1:
            return doc["templateId"]
    return None


def download_packet(template_id, dest_path, retries=3):
    """Download a compiled packet PDF with retries."""
    url = f"{PACKET_URL}?meetingTemplateId={template_id}&compileOutputType=1"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
                dest_path.write_bytes(data)
                size_mb = len(data) / (1024 * 1024)
                return size_mb
        except Exception as e:
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"    WARNING: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"    WARNING: download failed after {retries} attempts: {e}")
                return None


def safe_dirname(meeting):
    """Generate a directory name from meeting id and date."""
    mid = meeting["id"]
    # Parse date like "Aug 21, 2023" -> "2023-08-21"
    date_str = meeting.get("date", "unknown")
    try:
        from datetime import datetime
        dt = datetime.strptime(date_str, "%b %d, %Y")
        date_slug = dt.strftime("%Y-%m-%d")
    except (ValueError, KeyError):
        date_slug = re.sub(r"[^a-zA-Z0-9-]", "_", date_str)
    return f"{mid}_{date_slug}"


# ═══════════════════════════════════════════════════════════
# Fetch
# ═══════════════════════════════════════════════════════════

def cmd_fetch(args):
    data_dir = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    # Gather meetings
    all_meetings = []

    if args.meeting_id:
        # Fetch a specific meeting — we need to find it across years
        print(f"Looking for meeting {args.meeting_id}...")
        for year in DEFAULT_YEARS:
            meetings = fetch_meetings_for_year(year)
            for m in meetings:
                if m["id"] == args.meeting_id:
                    all_meetings.append(m)
                    break
            if all_meetings:
                break
        if not all_meetings:
            # Try upcoming
            for m in fetch_upcoming_meetings():
                if m["id"] == args.meeting_id:
                    all_meetings.append(m)
        if not all_meetings:
            print(f"  Meeting {args.meeting_id} not found")
            sys.exit(1)
    else:
        years = args.years if args.years else DEFAULT_YEARS
        for year in years:
            print(f"  Fetching meeting list for {year}...")
            meetings = fetch_meetings_for_year(year)
            all_meetings.extend(meetings)
            time.sleep(0.5)

        # Also check upcoming
        print(f"  Fetching upcoming meetings...")
        all_meetings.extend(fetch_upcoming_meetings())

    # Filter to meetings that have packets
    with_packets = []
    for m in all_meetings:
        tid = find_packet_template_id(m)
        if tid:
            with_packets.append((m, tid))

    if args.council_only:
        with_packets = [
            (m, tid) for m, tid in with_packets
            if "council" in m.get("title", "").lower()
        ]

    print(f"\n  {len(with_packets)} meetings with packets found\n")

    # Download
    downloaded = 0
    skipped = 0
    for i, (meeting, template_id) in enumerate(with_packets):
        dirname = safe_dirname(meeting)
        meeting_dir = data_dir / dirname
        meeting_dir.mkdir(parents=True, exist_ok=True)

        pdf_path = meeting_dir / "packet.pdf"
        meta_path = meeting_dir / "metadata.json"

        if pdf_path.exists() and not args.force:
            skipped += 1
            continue

        title = meeting.get("title", "?")
        date_str = meeting.get("date", "?")
        source_url = f"{PACKET_URL}?meetingTemplateId={template_id}&compileOutputType=1"

        print(f"  ({i + 1}/{len(with_packets)}) {date_str} — {title}")

        size = download_packet(template_id, pdf_path)
        if size is None:
            continue

        # Save metadata
        meta = {
            "meeting_id": meeting["id"],
            "title": title,
            "date": date_str,
            "time": meeting.get("time", ""),
            "meeting_type_id": meeting.get("meetingTypeId"),
            "committee_id": meeting.get("committeeId"),
            "template_id": template_id,
            "source_url": source_url,
            "file_size_mb": round(size, 1),
        }
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")

        print(f"    saved {dirname}/packet.pdf ({size:.1f} MB)")
        downloaded += 1

        if args.delay and i < len(with_packets) - 1:
            jitter = args.delay * random.uniform(0.7, 1.3)
            time.sleep(jitter)

    print(f"\nDone: {downloaded} downloaded, {skipped} already had.")


# ═══════════════════════════════════════════════════════════
# OCR
# ═══════════════════════════════════════════════════════════

def _ocr_image(img_bytes):
    """OCR a single page image. Runs in a subprocess via ProcessPoolExecutor."""
    import pytesseract  # imported here because subprocesses need their own import
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes))
    return pytesseract.image_to_string(img)


def cmd_ocr(args):
    import fitz  # pymupdf — lazy import since not needed for fetch/index

    data_dir = args.data_dir

    meeting_dirs = []
    if args.meeting_id:
        matches = list(data_dir.glob(f"{args.meeting_id}_*"))
        meeting_dirs = [d for d in matches if d.is_dir()]
    else:
        meeting_dirs = sorted(d for d in data_dir.iterdir()
                              if d.is_dir() and not d.name.startswith("."))

    count = 0
    for meeting_dir in meeting_dirs:
        pdf_path = meeting_dir / "packet.pdf"
        txt_path = meeting_dir / "packet.txt"

        if not pdf_path.exists():
            continue

        pdf_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

        if txt_path.exists() and not args.force:
            existing_header = txt_path.read_text(encoding="utf-8")[:200]
            if f"sha256: {pdf_hash}" in existing_header:
                continue
            print(f"  {meeting_dir.name}: PDF changed, re-extracting...", end="", flush=True)
        else:
            print(f"  {meeting_dir.name}...", end="", flush=True)

        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            print(f" ERROR: {e}")
            continue

        # Extract page data: native text + image bytes for OCR candidates
        page_data = []  # (page_num, native_text, image_bytes_or_None)
        for page_num in range(len(doc)):
            page = doc[page_num]
            native = (page.get_text() or "").strip()
            has_images = len(page.get_images()) > 0
            needs_ocr = has_images and len(native) <= 50

            if needs_ocr:
                try:
                    pix = page.get_pixmap(dpi=300)
                    img_bytes = pix.tobytes("png")
                except Exception:
                    img_bytes = None
                page_data.append((page_num, native, img_bytes))
            else:
                page_data.append((page_num, native, None))

        doc.close()

        # OCR image pages in parallel
        from concurrent.futures import ProcessPoolExecutor
        ocr_items = [(i, d) for i, d in enumerate(page_data) if d[2] is not None]
        ocr_results = {}

        if ocr_items:
            with ProcessPoolExecutor() as pool:
                futures = {
                    pool.submit(_ocr_image, d[2]): i
                    for i, d in ocr_items
                }
                for future in futures:
                    idx = futures[future]
                    try:
                        ocr_results[idx] = future.result()
                    except Exception:
                        ocr_results[idx] = page_data[idx][1]  # fallback to native

        # Assemble pages
        pages = []
        ocr_pages = len(ocr_results)
        for i, (page_num, native, img_bytes) in enumerate(page_data):
            text = ocr_results.get(i, native)
            pages.append(f"--- page {page_num + 1} ---\n{text}")

        header = (
            f"source: packet.pdf\n"
            f"sha256: {pdf_hash}\n"
            f"pages: {len(pages)}\n"
            f"ocr_pages: {ocr_pages}\n"
            f"---\n"
        )
        full_text = header + "\n\n".join(pages)
        fd, tmp_path = tempfile.mkstemp(prefix=".packet_", suffix=".tmp", dir=str(meeting_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(full_text)
            Path(tmp_path).replace(txt_path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        print(f" {len(pages)} pages ({ocr_pages} OCR'd)")
        count += 1

    print(f"\nProcessed {count} packet(s).")


# ═══════════════════════════════════════════════════════════
# Index
# ═══════════════════════════════════════════════════════════

def cmd_index(args):
    data_dir = args.data_dir
    entries = []

    for meeting_dir in sorted(data_dir.iterdir()):
        if not meeting_dir.is_dir() or meeting_dir.name.startswith("."):
            continue

        meta_path = meeting_dir / "metadata.json"
        if not meta_path.exists():
            continue

        meta = json.loads(meta_path.read_text())
        meta["has_pdf"] = (meeting_dir / "packet.pdf").exists()
        meta["has_txt"] = (meeting_dir / "packet.txt").exists()
        meta["dir"] = meeting_dir.name
        entries.append(meta)

    index_path = data_dir / "index.json"
    index_path.write_text(json.dumps(entries, indent=2) + "\n")

    print(f"Indexed {len(entries)} packets -> {index_path}")

    # Summary
    with_txt = sum(1 for e in entries if e["has_txt"])
    total_mb = sum(e.get("file_size_mb", 0) for e in entries)
    print(f"  {with_txt} with text extraction, {total_mb:.0f} MB total")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="San Mateo Council packet fetcher & OCR"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DEFAULT_DATA_DIR,
        help=f"Data directory (default: {DEFAULT_DATA_DIR})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── fetch ──
    p_fetch = sub.add_parser("fetch", help="Download council packet PDFs")
    p_fetch.add_argument("--year", type=int, action="append", dest="years",
                         help="Year(s) to fetch (default: 2020-2026)")
    p_fetch.add_argument("--meeting-id", type=int,
                         help="Fetch a specific meeting by ID")
    p_fetch.add_argument("--council-only", action="store_true",
                         help="Only fetch City Council meetings")
    p_fetch.add_argument("--force", action="store_true",
                         help="Re-download even if already fetched")
    p_fetch.add_argument("--delay", type=float, default=1,
                         help="Delay between downloads in seconds (default: 1)")

    # ── ocr ──
    p_ocr = sub.add_parser("ocr", help="Generate text from downloaded PDFs")
    p_ocr.add_argument("--meeting-id", type=int,
                        help="OCR a specific meeting")
    p_ocr.add_argument("--force", action="store_true",
                        help="Regenerate even if .txt exists")

    # ── index ──
    sub.add_parser("index", help="Build metadata index from fetched packets")

    args = parser.parse_args()

    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "ocr":
        cmd_ocr(args)
    elif args.command == "index":
        cmd_index(args)


if __name__ == "__main__":
    main()
