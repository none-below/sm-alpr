#!/usr/bin/env python3
"""
Generate .txt sidecar files for PDFs, Word docs, and image attachments.

For each PDF, extracts text from every page (native text where available,
OCR via tesseract for image-only pages). For .doc/.docx files, extracts
text via textutil (macOS) or antiword/pandoc (Linux). For images
(.png/.jpg/.jpeg), runs tesseract directly.

Writes the result to a hash-stamped .txt sidecar alongside the source file
(e.g. foo.pdf -> foo.pdf.a1b2c3d4.txt, bar.doc -> bar.doc.e5f6a7b8.txt).

The sidecar filename includes the first 8 hex chars of the file's MD5, so a
changed file always produces a new sidecar. Stale sidecars are cleaned up
automatically.

Usage:
  uv run python scripts/ocr_sidecar.py                     # all assets
  uv run python scripts/ocr_sidecar.py --staged            # staged files in assets/
  uv run python scripts/ocr_sidecar.py --files a.pdf b.doc # specific files
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


def extract_text_from_doc(doc_path):
    """Extract text from a .doc/.docx file.

    macOS: textutil handles both formats natively.
    Linux: antiword for .doc, pandoc for .docx.
    """
    doc_path = Path(doc_path)
    suffix = doc_path.suffix.lower()
    if sys.platform == "darwin":
        cmd = ["textutil", "-convert", "txt", "-stdout", str(doc_path)]
    elif suffix == ".docx":
        cmd = ["pandoc", "-t", "plain", str(doc_path)]
    else:
        cmd = ["antiword", str(doc_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"  WARNING: could not extract text from {doc_path}: {e}", file=sys.stderr)
        return ""


def extract_text_from_image(img_path):
    """OCR an image file (png/jpg/jpeg) with tesseract."""
    try:
        img = Image.open(img_path)
        return pytesseract.image_to_string(img)
    except Exception as e:
        print(f"  WARNING: could not OCR {img_path}: {e}", file=sys.stderr)
        return ""


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


def extract_annotations(doc):
    """Return a list of 'p{N} [author]: content' strings for PDF comment annotations.

    Filters out annotations with no content (decorative highlights) and
    AutoCAD-generated labels that carry no review value.
    """
    out = []
    for page_num, page in enumerate(doc, start=1):
        for annot in (page.annots() or []):
            info = annot.info
            author = (info.get("title") or "").strip()
            content = (info.get("content") or "").strip()
            subject = (info.get("subject") or "").strip()
            if not content:
                continue
            if author.startswith("AutoCAD"):
                continue
            line = f"p{page_num} [{author or '(no author)'}]"
            if subject and subject != content:
                line += f" ({subject})"
            line += f": {content}"
            out.append(line)
    return out


def file_hash(file_path):
    """Return first 8 hex chars of the file's MD5."""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def sidecar_path_for(file_path):
    """Return the hash-stamped sidecar path for a file."""
    file_path = Path(file_path)
    digest = file_hash(file_path)
    return file_path.parent / f"{file_path.name}.{digest}.txt"


def generate_sidecar(file_path, force=False, removed_stale=None):
    """Generate a .txt sidecar for a PDF or .doc file.

    Returns the sidecar path if written, else None.
    If `removed_stale` is a list, any stale sidecars cleaned up along the way
    are appended to it (so callers can stage the deletions in git).
    """
    file_path = Path(file_path)
    # Transparency portal PDFs are rendered from the HTML we already save as
    # YYYY-MM-DD.txt during the crawl. A hash-suffixed sidecar next to them
    # collides with the portal parser's strict date-keyed file layout.
    if "assets/transparency.flocksafety.com/" in file_path.as_posix():
        return None
    sidecar = sidecar_path_for(file_path)

    if not force and sidecar.exists():
        return None

    # Clean up any stale sidecars for this file (different hash)
    for old in file_path.parent.glob(f"{file_path.name}.*.txt"):
        if old != sidecar:
            old.unlink()
            if removed_stale is not None:
                removed_stale.append(old)

    suffix = file_path.suffix.lower()
    if suffix in (".doc", ".docx"):
        full_text = extract_text_from_doc(file_path)
        if not full_text:
            return None
        sidecar.write_text(full_text, encoding="utf-8")
        return sidecar

    if suffix in (".png", ".jpg", ".jpeg"):
        full_text = extract_text_from_image(file_path)
        if not full_text:
            return None
        sidecar.write_text(full_text, encoding="utf-8")
        return sidecar

    try:
        doc = fitz.open(file_path)
    except Exception as e:
        print(f"  WARNING: could not open {file_path}: {e}", file=sys.stderr)
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

    annotations = extract_annotations(doc)
    doc.close()

    full_text = "\n\n".join(pages)
    if annotations:
        full_text += "\n\n--- annotations ---\n" + "\n".join(annotations)
    sidecar.write_text(full_text, encoding="utf-8")
    return sidecar


SUPPORTED_EXTENSIONS = {".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg"}


def get_staged_assets():
    """Get list of staged PDF and .doc files under assets/."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
    )
    return [
        f
        for f in result.stdout.strip().splitlines()
        if Path(f).suffix.lower() in SUPPORTED_EXTENSIONS and f.startswith("assets/")
    ]


def main():
    parser = argparse.ArgumentParser(
        description="Generate .txt sidecars for PDFs and .doc files"
    )
    parser.add_argument(
        "--dir",
        default="assets",
        help="Directory to scan (default: assets)",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Process only git-staged files in assets/",
    )
    parser.add_argument("--files", nargs="+", help="Process specific files")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even if sidecar is up-to-date",
    )
    args = parser.parse_args()

    if args.staged:
        pdfs = [Path(f) for f in get_staged_assets()]
        if not pdfs:
            sys.exit(0)
    elif args.files:
        pdfs = [Path(f) for f in args.files]
    else:
        scan_dir = Path(args.dir)
        if not scan_dir.exists():
            print(f"Error: {scan_dir} not found", file=sys.stderr)
            sys.exit(1)
        pdfs = []
        for ext in ("pdf", "doc", "docx", "png", "jpg", "jpeg"):
            pdfs.extend(sorted(scan_dir.rglob(f"*.{ext}")))
            pdfs.extend(sorted(scan_dir.rglob(f"*.{ext.upper()}")))

    written = []
    removed_stale: list[Path] = []
    for pdf in pdfs:
        result = generate_sidecar(pdf, force=args.force, removed_stale=removed_stale)
        if result:
            print(f"  wrote {result}")
            written.append(result)

    if not args.staged:
        print(f"Text sidecars: {len(written)} written, {len(pdfs) - len(written)} skipped.")

    if args.staged and (written or removed_stale):
        # Stage new sidecars and deletions of stale ones so the in-flight
        # commit picks them up. Exit 0 so git proceeds with the commit.
        for s in written:
            subprocess.run(["git", "add", "--", str(s)], check=False)
        for s in removed_stale:
            subprocess.run(["git", "add", "--", str(s)], check=False)
        added = len(written) + len(removed_stale)
        print(f"\nAuto-staged {added} sidecar change(s) for this commit.")


if __name__ == "__main__":
    main()
