#!/usr/bin/env python3
"""
Extract Flock search-audit rows from PRA-produced audit PDFs.

Background:
  Public Flock transparency portals embed an audit CSV directly. Some
  agencies (e.g. SMPD) refuse to expose that portal section, then respond
  to a PRA with a PDF export of the same underlying log instead of the
  CSV. This script recovers the structured rows so audit data from those
  agencies can be merged into the same per-agency JSON the public-portal
  builder produces (scripts/build_audit_log.py).

PDF format expected (Flock's standard export, printed to PDF):
  Five header cells — ID, userID, networkCount, Search Time, Reason —
  followed by rows of four lines each:
    1. UUID
    2. userID (always '***' in practice; Flock platform-redacts it pre-export)
    3. "<networkCount> <Search Time>" joined into one line
    4. Reason (free text on one line)

Usage:
  uv run python scripts/parse_pra_audit.py <pra-folder>
  uv run python scripts/parse_pra_audit.py assets/san-mateo-public-records/W012541-041426

Writes one merged JSON file in the PRA folder: audit_rows.json (gitignored).

Next step — surface the rows in the per-portal audit pipeline:
  uv run python scripts/import_pra_audit.py <pra-folder> --slug <agency-slug>
"""

import argparse
import json
import re
import sys
from pathlib import Path

import fitz  # pymupdf

sys.path.insert(0, str(Path(__file__).parent))
from lib import audit_integrity, parse_pra_datetime

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
# "<int> MM/DD/YYYY, HH:MM:SS AM|PM UTC"
COUNT_TIME_RE = re.compile(
    r"^(\d+)\s+(\d{2}/\d{2}/\d{4},\s+\d{2}:\d{2}:\d{2}\s+(?:AM|PM)\s+UTC)$"
)


def parse_pdf(path: Path) -> list[dict]:
    """Return audit rows from a single Flock-export PDF.

    Robust against page-break artifacts: collects all text-lines across
    every page, then walks for UUID lines and reads the next 3 lines
    relative to each as the row body."""
    doc = fitz.open(path)
    try:
        lines: list[str] = []
        for page in doc:
            for raw in (page.get_text() or "").splitlines():
                s = raw.strip()
                if s:
                    lines.append(s)
    finally:
        doc.close()

    rows = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not UUID_RE.match(ln):
            i += 1
            continue
        if i + 3 >= len(lines):
            break
        rid = ln
        user_id = lines[i + 1]
        count_time = lines[i + 2]
        reason = lines[i + 3]
        m = COUNT_TIME_RE.match(count_time)
        if not m:
            # Shape didn't match — skip this run rather than mis-attribute fields.
            i += 1
            continue
        network_count = int(m.group(1))
        search_time_raw = m.group(2)
        rows.append({
            "id": rid,
            "userId": user_id,
            "searchDate": search_time_raw,
            "networkCount": network_count,
            "reason": reason,
            "source_pdf": path.name,
        })
        i += 4
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="PRA folder containing audit PDFs")
    ap.add_argument("--out", default="audit_rows.json",
                    help="Output filename inside the folder (default: audit_rows.json)")
    args = ap.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"not a directory: {folder}", file=sys.stderr)
        return 1

    # Audit PDFs only — skip message history.
    pdfs = sorted(p for p in folder.glob("*.pdf")
                  if "Audit" in p.name or "audit" in p.name)
    if not pdfs:
        print(f"no audit PDFs in {folder}", file=sys.stderr)
        return 1

    by_id: dict[str, dict] = {}
    per_file: list[dict] = []
    rows_in = 0
    for pdf in pdfs:
        rows = parse_pdf(pdf)
        rows_in += len(rows)
        # integrity is computed on the PDF's AS-DELIVERED order (userId-then-date)
        # BEFORE the merge/sort below erases it — date_resets ≈ distinct users − 1.
        per_file.append({
            "file": pdf.name,
            "row_count": len(rows),
            "integrity": audit_integrity(rows, to_dt=parse_pra_datetime),
        })
        for r in rows:
            existing = by_id.get(r["id"])
            if existing is None:
                by_id[r["id"]] = r
            else:
                # Same row recovered from two different PDFs (e.g. a "Part 1"
                # and a "(1) Part 1" duplicate batch). Keep the first; record
                # which other PDFs also contained it.
                also = existing.setdefault("also_in", [])
                if r["source_pdf"] not in also and r["source_pdf"] != existing["source_pdf"]:
                    also.append(r["source_pdf"])

    rows = sorted(by_id.values(),
                  key=lambda r: (r.get("searchDate", ""), r.get("id", "")))

    integrity = {
        "note": ("per-file integrity reflects each PDF's raw userId-then-date "
                 "order; the merged/sorted row array is re-ordered by "
                 "(searchDate, id) into a canonical timeline, which erases the "
                 "per-user grouping. To recover users, read per-file date_resets, "
                 "not the sorted rows."),
        "source_pdfs": len(pdfs),
        "rows_in": rows_in,
        "rows_unique": len(by_id),
        "duplicate_ids_merged": rows_in - len(by_id),
    }

    payload = {
        "source": "PRA-produced PDF exports of Flock search audit log",
        "pra_folder": folder.name,
        "integrity": integrity,
        "files": per_file,
        "row_count": len(rows),
        "rows": rows,
    }
    out_path = folder / args.out
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")

    print(f"wrote {out_path}")
    print(f"  {len(rows)} unique rows from {len(pdfs)} PDF(s):")
    for entry in per_file:
        print(f"    {entry['row_count']:>5}  {entry['file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
