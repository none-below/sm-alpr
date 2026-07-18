#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
PII scanner for PDF assets.

Scans text-based and image-based PDFs for:
  - Email addresses not on known government/vendor domains
  - Phone numbers (650 area code, excluding known SMPD public lines)

Designed to run as a pre-commit hook (exits 0/1) or standalone.

Usage:
  uv run python scripts/pii_scan.py                    # scan all assets
  uv run python scripts/pii_scan.py --staged            # scan only staged PDFs
  uv run python scripts/pii_scan.py --files a.pdf b.pdf # scan specific files
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import fitz  # pymupdf
import pytesseract
from PIL import Image
import io

# ── Allowlists ──

# Path prefixes excluded from scanning. Article PDFs under assets/articles/ are
# scraped public journalism — author bylines and outlet contact addresses are
# intentional, not leaked PII, and would otherwise flood the scanner.
EXCLUDED_PATH_PREFIXES = (
    "assets/articles/",
    # Flock transparency portal PDFs are public-by-design agency
    # transparency pages. Agencies publish their PIO/info/records contact
    # addresses on them intentionally (e.g. PIO@lvmpd.com on the
    # las-vegas-metro portal). Not a PII leak surface.
    "assets/transparency.flocksafety.com/",
    # W012462 is the Flock-to-SMPD email PRA. Produced records are Flock
    # marketing emails and partner-agency sharing notifications — they
    # intentionally contain external agency email addresses from dozens of
    # non-allowlisted domains. Not a PII leak; expected content.
    "assets/san-mateo-public-records/W012462-040226/",
)

ALLOWED_EMAIL_DOMAINS = {
    "cityofsanmateo.org",
    # OCR sometimes drops the trailing "g" in cityofsanmateo.org email
    # signatures (e.g., when followed by a comma in a small-font line).
    # Seen in W012459-040226 ORT grant procurement emails.
    "cityofsanmateo.or",
    "flocksafety.com",
    "mail.flocksafety.com",
    "sanmateocity.org",
    "smcgov.org",
    "stocktonca.gov",
    "pacific.edu",
    "lexipol.com",
    "mycusthelp.net",
    "ncric.net",
    "ncric.ca.gov",
    # Requester's own domain — appears in produced records where SMPD
    # includes requester's outbound emails (e.g. W012666 Flock Camera Concerns thread)
    "zerobelow.org",
    # South San Francisco PD — appears in W012665 _Re__SMPD_ALPR_Stuff.pdf
    "ssf.net",
}

# Known public phone numbers from published City of San Mateo records.
# SMPD: 650-522-xxxx. Other City staff contacts appear in published Council
# packets (see W012459-040226 strategic-plan attachments).
ALLOWED_PHONES = {
    "6505227003",  # City staff (Council packet, Strategic Plan updates 2020-09-21)
    "6505227044",  # M. McTaggart, City staff (Council packet, Blue Sky 2024-02-03)
    "6505227600",
    "6505227608",
    "6505227627",
    "6505227633",
    "6505227663",  # Lt. S. Casazza, SMPD audit officer (W012665/W012666 audit email signatures)
    "6505237663",  # OCR variant of 650-522-7663 (Casazza, same emails)
    "6505227664",  # J. Santiago, Management Analyst, Office of the Chief (chief email signatures, sources 56-60)
    "6505227662",  # SMPD (W012665 Emails_Sent_to_Users_Re_Audit.pdf)
    "6505227660",  # SMPD (W012672 _Re__ALPR_Training.pdf)
    "6505227002",  # A. Khojikian, SMPD (W012666 _Re__Flock_Camera_Concerns.pdf)
    "6505227681",  # M. Venikov, City of San Mateo (W012672 _Re__ALPR_Training.pdf)
    "6508654283",  # P. Monaghan / P. O'Rourke, City of San Mateo (W012672 _Re__ALPR_Training.pdf)
    "6505227684",
    "6505227685",
    "6505227700",
    "6505227709",
    "6505227710",
    "6505227711",
    "6505227623",  # T. Toomey, Business Manager (W012459-040226 ORT procurement email, 2024-03-27)
    "6505227720",  # R. Sianez, Field Operations admin (W012459-040226 ORT email + PO-0001042, 2024)
    "6505227100",  # S. Wong, Buyer (W012459-040226 PO-0001042, 2024)
    "6505333539",  # San Mateo County community chapter (Council packet, 2022-04-04)
}

# ── Patterns ──

EMAIL_RE = re.compile(
    r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
)

# 650 area code phone numbers in common formats
PHONE_650_RE = re.compile(
    r'(?:'
    r'\(650\)\s*\d{3}[\s.\-]?\d{4}'   # (650) 522-7710
    r'|650[\s.\-]\d{3}[\s.\-]\d{4}'    # 650-522-7710 / 650.522.7710
    r')',
)


def extract_text_from_page(page):
    """Extract text from a PDF page, falling back to OCR for image pages."""
    text = page.get_text()
    if text and len(text.strip()) > 50:
        return text

    try:
        pix = page.get_pixmap(dpi=300)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img)
    except Exception:
        return ""


def scan_text(text):
    """Scan text for PII. Returns list of (category, match, context)."""
    findings = []

    for m in EMAIL_RE.finditer(text):
        email = m.group(0)
        domain = email.split("@")[1].lower()
        if domain not in ALLOWED_EMAIL_DOMAINS:
            ctx = text[max(0, m.start() - 30):m.end() + 30].replace("\n", " ").strip()
            findings.append(("EMAIL", email, ctx))

    for m in PHONE_650_RE.finditer(text):
        phone = m.group(0).strip()
        digits = re.sub(r'\D', '', phone)
        if digits not in ALLOWED_PHONES:
            ctx = text[max(0, m.start() - 30):m.end() + 30].replace("\n", " ").strip()
            findings.append(("PHONE", phone, ctx))

    return findings


def scan_pdf(pdf_path):
    """Scan a single PDF for PII. Returns list of findings."""
    results = []
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"  WARNING: could not open {pdf_path}: {e}", file=sys.stderr)
        return []

    for page_num in range(len(doc)):
        text = extract_text_from_page(doc[page_num])
        if not text:
            continue
        for category, match, context in scan_text(text):
            results.append({
                "file": str(pdf_path),
                "page": page_num + 1,
                "category": category,
                "match": match,
                "context": context,
            })

    doc.close()
    return results


def get_staged_pdfs():
    """Get list of staged PDF files from git."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True,
    )
    return [f for f in result.stdout.strip().splitlines()
            if f.lower().endswith(".pdf")]


def is_excluded(path):
    posix = Path(path).as_posix()
    return any(prefix in posix for prefix in EXCLUDED_PATH_PREFIXES)


def emit_github_feedback(by_file):
    """In GitHub Actions, surface findings beyond the workflow log:
    - ::error file=...:: annotations, visible on the PR's Checks tab
    - markdown report appended to $GITHUB_STEP_SUMMARY, visible at the top
      of the workflow run page
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return

    # Annotations: one per file, summarizing findings. Commas and newlines
    # in the message must be percent-encoded so GitHub doesn't mangle them.
    for filepath, findings in sorted(by_file.items()):
        parts = [f"p{f['page']} {f['category']}: {f['match']}" for f in findings]
        msg = "; ".join(parts)
        msg = (msg.replace("%", "%25").replace("\r", "%0D")
                  .replace("\n", "%0A").replace(",", "%2C"))
        title = f"PII detected ({len(findings)} finding(s))"
        print(f"::error file={filepath},title={title}::{msg}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    total = sum(len(v) for v in by_file.values())
    lines = [
        "## PII scanner failed\n",
        f"**{total} finding(s) in {len(by_file)} file(s).**\n",
        "Either add the matched contacts to the allowlists in "
        "`scripts/pii_scan.py`, or remove the records if they shouldn't be "
        "public.\n",
    ]
    for filepath, findings in sorted(by_file.items()):
        lines.append(f"\n### `{filepath}`\n")
        for h in findings:
            lines.append(f"- **p{h['page']} {h['category']}:** `{h['match']}`")
            lines.append(f"  - Context: `…{h['context']}…`")
    lines.append("")
    with open(summary_path, "a") as fh:
        fh.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Scan PDF assets for PII")
    parser.add_argument("--dir", default="assets/san-mateo-public-records",
                        help="Directory to scan (default mode)")
    parser.add_argument("--staged", action="store_true",
                        help="Scan only git-staged PDFs")
    parser.add_argument("--files", nargs="+",
                        help="Scan specific files")
    args = parser.parse_args()

    if args.staged:
        pdfs = [Path(f) for f in get_staged_pdfs()]
        if not pdfs:
            sys.exit(0)
    elif args.files:
        pdfs = [Path(f) for f in args.files]
    else:
        scan_dir = Path(args.dir)
        if not scan_dir.exists():
            print(f"Error: {scan_dir} not found", file=sys.stderr)
            sys.exit(1)
        pdfs = sorted(scan_dir.rglob("*.pdf")) + sorted(scan_dir.rglob("*.PDF"))

    pdfs = [p for p in pdfs if not is_excluded(p)]

    all_findings = []
    for pdf in pdfs:
        findings = scan_pdf(pdf)
        all_findings.extend(findings)

    if not all_findings:
        if not args.staged:
            print(f"PII scan: {len(pdfs)} PDFs clean.")
        sys.exit(0)

    # Group by file
    by_file = {}
    for h in all_findings:
        by_file.setdefault(h["file"], []).append(h)

    print(f"PII DETECTED — {len(all_findings)} finding(s) in {len(by_file)} file(s):\n")
    for filepath, findings in sorted(by_file.items()):
        print(f"  {filepath}")
        for f in findings:
            print(f"    p{f['page']} {f['category']}: {f['match']}")
            print(f"      ...{f['context']}...")
    print()

    emit_github_feedback(by_file)

    sys.exit(1)


if __name__ == "__main__":
    main()
