#!/usr/bin/env python3
"""
Generate .txt sidecar files for image-based PDFs via OCR.

For each PDF, extracts text from every page (native text where available,
OCR via tesseract for image-only pages). Writes the result to a hash-stamped
.txt sidecar alongside the PDF (e.g. foo.pdf -> foo.pdf.a1b2c3d4.txt).

The sidecar filename includes the first 8 hex chars of the PDF's MD5, so a
changed PDF always produces a new sidecar. Stale sidecars are cleaned up
automatically.

Usage:
  uv run python scripts/ocr_sidecar.py                    # all assets
  uv run python scripts/ocr_sidecar.py --staged            # staged PDFs in assets/
  uv run python scripts/ocr_sidecar.py --files a.pdf b.pdf # specific files
"""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

import fitz  # pymupdf
import pytesseract
from PIL import Image
import io


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


def pdf_hash(pdf_path):
    """Return first 8 hex chars of the PDF's MD5."""
    h = hashlib.md5()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def sidecar_path_for(pdf_path):
    """Return the hash-stamped sidecar path for a PDF."""
    pdf_path = Path(pdf_path)
    digest = pdf_hash(pdf_path)
    return pdf_path.parent / f"{pdf_path.name}.{digest}.txt"


def generate_sidecar(pdf_path, force=False):
    """Generate a .txt sidecar for a PDF. Returns the sidecar path if written, else None."""
    pdf_path = Path(pdf_path)
    sidecar = sidecar_path_for(pdf_path)

    if not force and sidecar.exists():
        return None

    # Clean up any stale sidecars for this PDF (different hash)
    for old in pdf_path.parent.glob(f"{pdf_path.name}.*.txt"):
        if old != sidecar:
            old.unlink()

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"  WARNING: could not open {pdf_path}: {e}", file=sys.stderr)
        return None

    # Extract text from each page, tracking whether OCR added anything
    native_chars = 0
    ocr_chars = 0
    pages = []
    for page_num in range(len(doc)):
        native = (doc[page_num].get_text() or "").strip()
        native_chars += len(native)

        has_images = len(doc[page_num].get_images()) > 0
        needs_ocr = has_images and len(native) <= 50

        if needs_ocr:
            ocr_text = extract_text_from_page(doc[page_num])
            ocr_chars += len(ocr_text.strip())
            pages.append(f"--- page {page_num + 1} ---\n{ocr_text}")
        else:
            pages.append(f"--- page {page_num + 1} ---\n{native}")

    doc.close()

    full_text = "\n\n".join(pages)
    sidecar.write_text(full_text, encoding="utf-8")
    return sidecar


def get_staged_asset_pdfs():
    """Get list of staged PDF files under assets/."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
    )
    return [
        f
        for f in result.stdout.strip().splitlines()
        if f.lower().endswith(".pdf") and f.startswith("assets/")
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Generate .txt sidecars for image-based PDFs"
    )
    parser.add_argument(
        "--dir",
        default="assets",
        help="Directory to scan (default: assets)",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Process only git-staged PDFs in assets/",
    )
    parser.add_argument("--files", nargs="+", help="Process specific files")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even if sidecar is up-to-date",
    )
    args = parser.parse_args()

    if args.staged:
        pdfs = [Path(f) for f in get_staged_asset_pdfs()]
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

    written = []
    for pdf in pdfs:
        result = generate_sidecar(pdf, force=args.force)
        if result:
            print(f"  wrote {result}")
            written.append(result)

    if not args.staged:
        print(f"OCR sidecars: {len(written)} written, {len(pdfs) - len(written)} skipped.")

    if args.staged and written:
        print(
            f"\nOCR sidecars generated for {len(written)} PDF(s).\n"
            "Please stage them and re-commit:\n"
        )
        for s in written:
            print(f'  git add "{s}"')
        print()
        sys.exit(1)


if __name__ == "__main__":
    main()
