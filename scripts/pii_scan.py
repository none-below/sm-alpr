#!/usr/bin/env python3
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
import re
import subprocess
import sys
from pathlib import Path

import fitz  # pymupdf
import pytesseract
from PIL import Image
import io

# ── Allowlists ──

ALLOWED_EMAIL_DOMAINS = {
    "cityofsanmateo.org",
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
}

# Known public SMPD phone numbers (650-522-xxxx)
ALLOWED_PHONES = {
    "6505227600",
    "6505227608",
    "6505227627",
    "6505227633",
    "6505227684",
    "6505227685",
    "6505227700",
    "6505227709",
    "6505227710",
    "6505227711",
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

    sys.exit(1)


if __name__ == "__main__":
    main()
