#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
pdf_vector_redaction.py — detect vector-path ("outlined") text hidden under an
opaque box: a failed-redaction style that leaves no font-based text layer for a
text extractor to catch, and no unhidden raster pixels for an image-based check
to catch.

Why this exists:
  An ordinary failed redaction (a box drawn over live text) is caught by
  extracting the page's text spans and checking whether a span's bounding box
  is covered by a dark filled rectangle — see e.g. the `covered_spans` check in
  the MuckRock/PRA-portal forensic scanners. But some PDF producers (design
  tools, "flatten to curves" print drivers, editors that convert text to
  outlines before saving) never register the covered content as text at all:
  it becomes ordinary filled vector paths, indistinguishable in the page's
  object model from the box drawn on top of it. get_text() returns nothing for
  either the box or the "text," so a span-based check silently passes the page
  as clean.

  This module instead reasons purely in the vector-graphics domain: a solid,
  opaque, dark axis-aligned rectangle is a redaction-box candidate; any *other*
  dark, filled path whose bounding box falls mostly inside that rectangle, and
  whose outline is not itself a bare rectangle, is candidate hidden content —
  outlined glyphs are drawn as compound line/curve paths, never as a single
  "re" (rectangle) operator. When enough such paths cluster in a box (a
  redaction covers a run of characters, not one stray mark), the box is
  flagged.

  Recovery is genuinely possible, not just detection, because the hidden paths
  are ordinary vector graphics: redraw them alone (omitting the box) and OCR
  the render. Detection here is pure PyMuPDF (fast, no browser); recovery goes
  through a headless browser running docs/js/pdf_vector_redaction.js instead —
  see browser_recover_pdf's docstring for why PyMuPDF's own Shape API can't be
  used to redraw multi-contour glyphs correctly.

Limitations:
  - Flags redaction-shaped rectangles with several compound-path shapes
    clustered underneath; a false positive is possible if a page has a dark
    rectangle behind unrelated vector art (e.g. a logo). Findings are a lead
    for manual/OCR verification, not a standalone legal claim.
  - Only catches vector-path content. Real text under a box is a different,
    already-handled case; a rasterized (flattened to an image) redaction is a
    burned-in redaction and out of scope here.

Usage:
  uv run python scripts/pdf_vector_redaction.py <file.pdf> [more.pdf ...]
  uv run python scripts/pdf_vector_redaction.py --dir <folder>
  uv run python scripts/pdf_vector_redaction.py --recover --save-images <dir> <file.pdf>
"""
import argparse
import io
import sys
from pathlib import Path

import fitz  # pymupdf

# ── Tunables ──

BOX_MAX_LUM = 0.3          # box fill must be this dark or darker
BOX_MIN_OPACITY = 0.9      # box must be effectively opaque
BOX_MIN_W = 6.0             # ignore thin rules/underlines
BOX_MIN_H = 4.0
# A redaction box is one of two shapes: a LINE (a field or short phrase — wide relative
# to its height) or a COLUMN (an entire field redacted down every row of a table — tall
# relative to its width, up to a full page). What it is never shaped like is a paragraph
# block, a table header row, or a page border: those are wide AND tall/blocky at once.
# Without this split, a plain "reject anything too big" cap either lets full-page
# background rectangles through (if the height cap is loose) or blinds the scanner to
# real column-spanning redactions (if it's tight) — both were seen on real SMPD audit
# PDFs: a 259x203pt paragraph block one page, a 28x500pt genuinely-solid redacted
# column (no hidden content under it, but the shape itself is legitimate) on another.
# Tuned against those two pages plus the one synthetic true positive — a first pass,
# not a proven bound.
LINE_MAX_W = 400.0
LINE_MAX_H = 30.0
COLUMN_MAX_W = 60.0
COLUMN_MAX_H = 2000.0

SHAPE_MAX_LUM = 0.4         # candidate hidden shape must be dark
SHAPE_MAX_AREA_FRAC = 0.25  # a single glyph shouldn't approach the box's area
SHAPE_MIN_CONTAINMENT = 0.8  # fraction of the shape's own area inside the box

MIN_SHAPES_TO_FLAG = 3      # require a cluster, not one stray mark
# Second, independent guard against the same false-positive class: even a full-column
# redaction across a dense table page is at most a few hundred glyphs, not thousands.
MAX_SHAPES_TO_FLAG = 1000


def _plausible_redaction_shape(w: float, h: float) -> bool:
    return (w <= LINE_MAX_W and h <= LINE_MAX_H) or (w <= COLUMN_MAX_W and h <= COLUMN_MAX_H)


def lum(color) -> float:
    """Perceptual luminance of a fitz fill color (gray/RGB/CMYK tuple), 0=black."""
    if not color:
        return 1.0
    if len(color) == 1:
        return color[0]
    if len(color) == 3:
        return 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
    if len(color) == 4:  # CMYK
        c, m, y, k = color
        return (1 - min(1, c + k)) * 0.3 + (1 - min(1, m + k)) * 0.59 + (1 - min(1, y + k)) * 0.11
    return 1.0


def is_bare_rect(item: dict) -> bool:
    """True if a drawing's path is nothing but rectangle operators — a box, not
    a traced outline. Real outlined glyphs are drawn with line/curve segments."""
    ops = {it[0] for it in item.get("items", [])}
    return bool(ops) and ops <= {"re"}


def _opaque(item: dict) -> bool:
    op = item.get("fill_opacity")
    return op is None or op >= BOX_MIN_OPACITY


def find_boxes(page: "fitz.Page") -> list[dict]:
    """Solid, opaque, dark rectangles shaped like a plausible redaction (see
    _plausible_redaction_shape). A single drawing can paint several disjoint
    rectangles as one multi-subpath fill (e.g. one mark per table row, all filled
    together) — its overall bounding box overstates what it actually covers, so
    each 're' item is its own candidate rather than trusting d['rect']."""
    boxes = []
    for d in page.get_drawings():
        if d.get("fill") is None or not _opaque(d):
            continue
        if lum(d["fill"]) > BOX_MAX_LUM:
            continue
        if not is_bare_rect(d):
            continue
        for it in d["items"]:
            if it[0] != "re":
                continue
            r = it[1]
            if r.width < BOX_MIN_W or r.height < BOX_MIN_H:
                continue
            if not _plausible_redaction_shape(r.width, r.height):
                continue
            boxes.append({"rect": r, "drawing": d})
    return boxes


def find_hidden_shapes(page: "fitz.Page", box: dict, drawings: list[dict] | None = None) -> list[dict]:
    """Compound-path filled shapes, dark and small, mostly contained in box['rect']."""
    if drawings is None:
        drawings = page.get_drawings()
    box_rect = box["rect"]
    box_area = abs(box_rect) or 1.0
    hits = []
    for d in drawings:
        if d is box["drawing"] or d.get("fill") is None:
            continue
        if lum(d["fill"]) > SHAPE_MAX_LUM:
            continue
        if is_bare_rect(d):
            continue  # another box-like rectangle, not traced content
        r = fitz.Rect(d["rect"])
        # A degenerate (zero-area) path — some PDF producers emit these, e.g. for
        # whitespace — would otherwise pass containment trivially for ANY box, since
        # SHAPE_MIN_CONTAINMENT * 0 = 0 and overlap can't be negative. It draws
        # nothing, so it can't be recovered content; exclude it before that math runs.
        if r.is_empty:
            continue
        if abs(r) > SHAPE_MAX_AREA_FRAC * box_area:
            continue
        overlap = abs(r & box_rect)
        if overlap < SHAPE_MIN_CONTAINMENT * abs(r):
            continue
        hits.append({"rect": r, "drawing": d})
    return hits


def row_aligned(shapes: list[dict]) -> bool:
    """True if the shapes' vertical centers cluster into text-line bands rather
    than scattering — a weak extra signal that this is a line of characters."""
    if len(shapes) < 2:
        return False
    centers = sorted((s["rect"].y0 + s["rect"].y1) / 2 for s in shapes)
    heights = [s["rect"].height for s in shapes]
    band = max(sum(heights) / len(heights), 1.0)
    return (centers[-1] - centers[0]) <= band * 1.5


class Finding:
    def __init__(self, page_num: int, box: dict, shapes: list[dict]):
        self.page_num = page_num
        self.box = box
        self.shapes = shapes

    @property
    def rect(self) -> "fitz.Rect":
        return self.box["rect"]

    @property
    def confidence(self) -> str:
        if len(self.shapes) >= MIN_SHAPES_TO_FLAG and row_aligned(self.shapes):
            return "high"
        return "medium"

    def __repr__(self):
        return f"Finding(page={self.page_num}, rect={self.rect}, n_shapes={len(self.shapes)}, confidence={self.confidence})"


def scan_page(page: "fitz.Page") -> list[Finding]:
    """Every box on the page with a cluster of hidden vector shapes under it."""
    drawings = page.get_drawings()
    boxes = find_boxes(page)
    findings = []
    for box in boxes:
        shapes = find_hidden_shapes(page, box, drawings)
        if MIN_SHAPES_TO_FLAG <= len(shapes) <= MAX_SHAPES_TO_FLAG:
            findings.append(Finding(page.number, box, shapes))
    return findings


def scan_doc(doc: "fitz.Document") -> list[Finding]:
    findings = []
    for page in doc:
        findings += scan_page(page)
    return findings


def _findings_to_rows(doc: "fitz.Document", label: str) -> list[dict]:
    return [
        {
            "file": label,
            "page": f.page_num + 1,
            "rect": tuple(round(v, 1) for v in f.rect),
            "n_shapes": len(f.shapes),
            "confidence": f.confidence,
        }
        for f in scan_doc(doc)
    ]


def scan_pdf(path: Path) -> list[dict]:
    """Open path, scan every page, return one dict per finding (detection only —
    fast, no browser). For recovery see browser_recover_pdf."""
    doc = fitz.open(path)
    out = _findings_to_rows(doc, str(path))
    doc.close()
    return out


def scan_bytes(data: bytes, label: str = "<bytes>") -> list[dict]:
    """Same as scan_pdf but for an in-memory PDF (e.g. one revision of a larger
    incremental-update file already read elsewhere)."""
    doc = fitz.open(stream=data, filetype="pdf")
    out = _findings_to_rows(doc, label)
    doc.close()
    return out


# pdf.js version pinned here must match docs/audit-check.html's <script> tag —
# they're independent copies of the same CDN reference, not shared config.
PDFJS_CDN = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"
PDFJS_WORKER_CDN = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js"

_PDF_URL = "https://local.invalid/doc.pdf"  # intercepted by page.route(), never actually requested over the network

_BROWSER_SCAN_JS = """async (pages) => {
    pdfjsLib.GlobalWorkerOptions.workerSrc = %r;
    const doc = await pdfjsLib.getDocument({url: %r, stopAtErrors: false}).promise;
    const findings = await PdfVectorRedaction.scanDocument(doc, pdfjsLib, null, pages && pages.length ? pages : null);
    await doc.destroy();
    return findings.map(f => ({
        page: f.page, rect: [f.rect.x0, f.rect.y0, f.rect.x1, f.rect.y1],
        nShapes: f.nShapes, confidence: f.confidence, image: f.image
    }));
}""" % (PDFJS_WORKER_CDN, _PDF_URL)


def browser_recover_pdf(path: Path, ocr: bool = True, pages: list[int] | None = None) -> list[dict]:
    """Detect AND recover via a headless browser running docs/js/pdf_vector_redaction.js
    — the exact code the audit-check web page uses, not a second implementation.

    Recovery cannot go through PyMuPDF's Shape API: accumulating many draw_line/
    draw_bezier calls per multi-contour glyph (a real font's "8", "9", "B" — anything
    with an inner counter as a separate subpath) does not reproduce the nonzero-winding
    hole correctly, so letters merge into solid blobs. Verified on a real SMPD audit
    PDF: the canvas (pdf.js operator-list + ctx.fill) reconstruction read as clean
    plates; the equivalent PyMuPDF-rendered image of the same shapes did not, even
    isolated to a single 7-glyph row. Detection alone doesn't hit this (it only needs
    bounding boxes, no fill), so the fast default path (scan_pdf) still uses PyMuPDF —
    only recovery goes through the browser.

    pages: optional 1-indexed page numbers to restrict recovery to. Reconstructing
    every finding across every page of a large, physically-huge-paged document (e.g.
    an architectural sheet set) in one browser session can exhaust the tab's memory
    and crash it (seen on a 131-page, 2592x1728pt council packet with 81 findings) —
    pass the specific pages a prior scan_pdf() call already flagged instead of
    scanning the whole file again.

    Returns one dict per finding: file/page/rect/n_shapes/confidence (as scan_pdf) plus
    'recovered_png' (raw PNG bytes — caller decides whether/where to save; this
    function never writes recovered content to disk) and, if ocr=True, 'recovered_text'
    (best-effort OCR on that PNG — verify visually before treating it as ground truth).
    """
    import base64

    from playwright.sync_api import sync_playwright

    js_src = (Path(__file__).resolve().parent.parent / "docs" / "js" / "pdf_vector_redaction.js").read_text()

    # When specific pages are requested, extract just those into a standalone PDF
    # BEFORE handing anything to the browser. pdf.js has to fully load/parse a
    # document up front regardless of which pages scanDocument is later restricted
    # to — `pages` alone doesn't avoid the memory hit of a huge source file (a real
    # 378MB, 131-page architectural production crashed the tab here even scoped to
    # one page); shrinking the file itself is what actually bounds the cost.
    page_map: dict[int, int] | None = None  # sub-doc page number -> original page number
    if pages:
        sorted_pages = sorted(set(pages))
        src_doc = fitz.open(path)
        sub_doc = fitz.open()
        for p in sorted_pages:
            sub_doc.insert_pdf(src_doc, from_page=p - 1, to_page=p - 1)
        pdf_bytes = sub_doc.tobytes()
        page_map = {i + 1: p for i, p in enumerate(sorted_pages)}
        sub_doc.close()
        src_doc.close()
    else:
        pdf_bytes = Path(path).read_bytes()

    def _serve_pdf(route):
        # Fulfilling from the already-read bytes (not re-reading the file) and turning
        # off ranges: base64+atob was the original approach, but a large real-world
        # production blows past what a JS byte-by-byte atob decode loop and
        # Playwright's JSON-based evaluate() channel can handle without crashing the
        # tab. Native binary fetch via route interception has no such cap.
        route.fulfill(status=200, content_type="application/pdf", body=pdf_bytes,
                      headers={"Accept-Ranges": "none"})

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content("<!doctype html><html><body></body></html>")
        page.add_script_tag(url=PDFJS_CDN)
        page.add_script_tag(content=js_src)
        page.route(_PDF_URL, _serve_pdf)
        raw = page.evaluate(_BROWSER_SCAN_JS, [])  # sub-doc already has only the wanted pages
        browser.close()

    out = []
    for r in raw:
        out_page = page_map[r["page"]] if page_map else r["page"]
        row = {
            "file": str(path),
            "page": out_page,
            "rect": tuple(round(v, 1) for v in r["rect"]),
            "n_shapes": r["nShapes"],
            "confidence": r["confidence"],
            "recovered_png": base64.b64decode(r["image"].split(",", 1)[1]),
        }
        if ocr:
            import pytesseract
            from PIL import Image

            img = Image.open(io.BytesIO(row["recovered_png"]))
            try:
                row["recovered_text"] = pytesseract.image_to_string(img, config="--psm 6").strip()
            except Exception as e:
                row["recovered_text"] = f"<OCR failed: {e}>"
        out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Detect vector-path text hidden under an opaque box (a failed-redaction style text "
                    "extractors and image-diff checks both miss)."
    )
    ap.add_argument("paths", nargs="*", help="PDF file(s) to scan")
    ap.add_argument("--dir", help="scan every *.pdf under this folder (recursive)")
    ap.add_argument("--recover", action="store_true",
                     help="reconstruct + OCR hidden content via a headless browser (slower; needs "
                          "`playwright install chromium` once)")
    ap.add_argument("--save-images", metavar="DIR",
                     help="with --recover, also save each finding's reconstructed PNG under DIR "
                          "(0600, DIR created 0700) for visual double-check — OCR is best-effort, "
                          "not ground truth. Contents may be PII; never point this at a repo path.")
    args = ap.parse_args()
    if args.save_images and not args.recover:
        ap.error("--save-images requires --recover")

    files: list[Path] = []
    if args.dir:
        files += sorted(Path(args.dir).rglob("*.pdf")) + sorted(Path(args.dir).rglob("*.PDF"))
    files += [Path(p) for p in args.paths]
    if not files:
        ap.error("provide one or more PDF files, or --dir <folder>")

    save_dir = None
    if args.save_images:
        save_dir = Path(args.save_images)
        save_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    total = 0
    for f in files:
        if not f.exists():
            print(f"### {f}: not found", file=sys.stderr)
            continue
        try:
            findings = browser_recover_pdf(f) if args.recover else scan_pdf(f)
        except Exception as e:
            print(f"### {f}: could not scan ({e})", file=sys.stderr)
            continue
        if not findings:
            continue
        total += len(findings)
        print(f"\n### {f}")
        for i, row in enumerate(findings):
            print(f"  p{row['page']} rect={row['rect']} shapes={row['n_shapes']} confidence={row['confidence']}")
            if "recovered_text" in row:
                print(f"      recovered: {row['recovered_text']!r}")
            if save_dir and "recovered_png" in row:
                out = save_dir / f"{f.stem}_p{row['page']}_{i}.png"
                out.write_bytes(row["recovered_png"])
                out.chmod(0o600)
                print(f"      image: {out}")

    if total:
        print(f"\n{total} suspected vector-redaction failure(s) across {len(files)} file(s).")
    else:
        print(f"clean: no vector-redaction failures found in {len(files)} file(s).")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
