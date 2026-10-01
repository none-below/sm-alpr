# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Tests for scripts/pdf_vector_redaction.py — the vector-path-under-box redaction
detector. Fixtures are synthetic PDFs built in-memory with PyMuPDF (no real PRA data,
so nothing here can leak PII); each one targets a specific real-world failure mode
found while validating this tool against actual SMPD audit PDFs (see the module's own
comments for the concrete pages each cap/guard was tuned against)."""
import sys
from pathlib import Path

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pdf_vector_redaction as vr  # noqa: E402

BLACK = (0, 0, 0)


def _new_page(width=200.0, height=200.0):
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    return doc, page


def _draw_glyph(shape, x0, y0, x1, y1):
    """A small triangle: closed, filled, but not axis-aligned — PyMuPDF's rect
    normalization only recognizes rectilinear closed paths, so this stays a
    traced (line-based) shape, standing in for an outlined character."""
    xm = (x0 + x1) / 2
    shape.draw_line((x0, y0), (x1, y0))
    shape.draw_line((x1, y0), (xm, y1))
    shape.draw_line((xm, y1), (x0, y0))
    shape.finish(fill=BLACK, color=BLACK)


def _draw_box(shape, rect):
    shape.draw_rect(fitz.Rect(rect))
    shape.finish(fill=BLACK, color=BLACK)


def _draw_box_as_line_segments(shape, rect):
    """Same visual box as _draw_box, but written as explicit moveto/lineto/
    closepath operators (PyMuPDF's draw_line writes literal 'm'/'l'/'h', not the
    dedicated 're' operator — confirmed by inspecting the raw content stream).
    PyMuPDF's OWN get_drawings() still normalizes this back to 're' on read
    (Python sees no difference), but pdf.js's raw operator list does not — it's
    exactly what a real Microsoft Print-to-PDF export was found to emit, and
    the browser-side detector missed it entirely until decomposeIntoRects
    learned to recognize an axis-aligned moveTo+3*lineTo block as a box too."""
    x0, y0, x1, y1 = rect
    shape.draw_line((x0, y0), (x1, y0))
    shape.draw_line((x1, y0), (x1, y1))
    shape.draw_line((x1, y1), (x0, y1))
    shape.draw_line((x0, y1), (x0, y0))
    shape.finish(fill=BLACK, color=BLACK, closePath=True)


def _draw_open_rect_glyph(shape, x0, y0, x1, y1):
    """A letter like a lowercase "l" — geometrically a perfect axis-aligned
    rectangle (its last point equals its first), but drawn WITHOUT an explicit
    closePath. There is no structural way to tell this apart from a real box
    drawn the same way (see _draw_box_as_line_segments) except that a box is
    always explicitly closed; a stroke that only happens to trace back to its
    start isn't. Confirmed against a real font: this file's own "l"/"i" glyphs
    are emitted exactly this way, and PyMuPDF's get_drawings() leaves them as
    raw line ops rather than normalizing them to 're' — closePath is the signal
    it keys off of too."""
    shape.draw_line((x0, y0), (x1, y0))
    shape.draw_line((x1, y0), (x1, y1))
    shape.draw_line((x1, y1), (x0, y1))
    shape.draw_line((x0, y1), (x0, y0))
    shape.finish(fill=BLACK, color=BLACK, closePath=False)


def _draw_degenerate(shape, x, y):
    """A zero-length line: a real path object, but zero area — some PDF
    producers emit these (e.g. for whitespace)."""
    shape.draw_line((x, y), (x, y))
    shape.finish(fill=BLACK, color=BLACK)


def _row_of_glyphs(shape, x0, y0, n, w=6.0, h=8.0, gap=1.0):
    for i in range(n):
        gx0 = x0 + i * (w + gap)
        _draw_glyph(shape, gx0, y0, gx0 + w, y0 + h)


def test_detects_hidden_text_under_box():
    doc, page = _new_page()
    shape = page.new_shape()
    _row_of_glyphs(shape, x0=20, y0=50, n=7)
    _draw_box(shape, (18, 48, 75, 60))
    shape.commit()

    findings = vr.scan_page(page)
    assert len(findings) == 1
    assert len(findings[0].shapes) == 7
    doc.close()


def test_no_false_positive_when_box_hides_nothing():
    doc, page = _new_page()
    shape = page.new_shape()
    _draw_box(shape, (18, 48, 75, 60))
    shape.commit()

    assert vr.scan_page(page) == []
    doc.close()


def test_zero_area_paths_do_not_count_as_hidden_content():
    """Regression: a degenerate (zero-area) path trivially satisfies the
    containment check for ANY box (SHAPE_MIN_CONTAINMENT * 0 == 0, and overlap
    can't be negative), so without an explicit area floor, enough of them under
    an otherwise-empty box would falsely read as recovered content that in fact
    renders nothing. Found via real garbled/blank reconstructions."""
    doc, page = _new_page()
    shape = page.new_shape()
    for i in range(5):
        _draw_degenerate(shape, 20 + i, 55)
    _draw_box(shape, (18, 48, 75, 60))
    shape.commit()

    assert vr.scan_page(page) == []
    doc.close()


def test_page_spanning_rect_is_not_a_redaction_box():
    """Regression: a page-sized decorative/background rectangle on a page whose
    body text is *also* vector-outlined must not read as a box "hiding" the
    whole page's ordinary content. Found on a real 24-page SMPD audit PDF where
    one such rect matched all 9,648 glyph-shapes on its page."""
    doc, page = _new_page(width=400.0, height=400.0)
    shape = page.new_shape()
    for row in range(10):
        _row_of_glyphs(shape, x0=20, y0=20 + row * 30, n=8)
    _draw_box(shape, (10, 10, 380, 380))
    shape.commit()

    assert vr.scan_page(page) == []
    doc.close()


def test_narrow_tall_column_is_a_plausible_redaction_shape():
    """A redaction can legitimately run the full height of a table (one mark per
    row, same column, down the whole page) as long as it stays narrow — unlike
    the page-spanning case above. Confirmed on a real audit PDF: a 28x500pt
    solid column with recoverable license plates underneath."""
    doc, page = _new_page(width=200.0, height=600.0)
    shape = page.new_shape()
    for row in range(20):
        _draw_glyph(shape, 20, 20 + row * 25, 35, 20 + row * 25 + 15)
    _draw_box(shape, (18, 15, 40, 590))
    shape.commit()

    findings = vr.scan_page(page)
    assert len(findings) == 1
    assert len(findings[0].shapes) == 20
    doc.close()


def test_multi_rect_compound_path_decomposed_per_item():
    """Regression: a single drawing can paint several disjoint rectangles as one
    multi-subpath fill (PyMuPDF reports ONE drawing with N 're' items — e.g. one
    mark per table row, all filled together in one operation). Its merged
    bounding box overstates what it actually covers.

    The two rects here are placed diagonally so the point lands squarely on
    decomposition: each individual rect (12x12) is a plausible redaction shape,
    but their MERGED bounding box (144x142) fails BOTH branches of
    _plausible_redaction_shape (too tall for a line, too wide for a column) —
    so without per-item decomposition this drawing would never even become a
    box candidate, and real hidden content under one of its rects would go
    completely undetected. This is what happened on a real SMPD audit PDF: a
    column-divider drawing's two disjoint rects, merged, no longer read as a
    redaction shape at all, until per-item decomposition was added."""
    doc, page = _new_page()
    shape = page.new_shape()
    _row_of_glyphs(shape, x0=18, y0=20, n=3, w=3, h=4)  # hidden content under rect A only
    shape.draw_rect(fitz.Rect(18, 18, 30, 30))     # rect A (top-left)
    shape.draw_rect(fitz.Rect(150, 150, 162, 162))  # rect B (bottom-right), nothing hidden under it
    shape.finish(fill=BLACK, color=BLACK)
    shape.commit()

    merged = fitz.Rect(18, 18, 162, 162)
    assert not vr._plausible_redaction_shape(merged.width, merged.height), \
        "test fixture must actually exercise the merged-box-fails case"

    findings = vr.scan_page(page)
    assert len(findings) == 1
    f = findings[0]
    assert (f.rect.x0, f.rect.y0) == (18, 18)  # matched rect A specifically, not the merge
    assert len(f.shapes) == 3
    doc.close()


def test_browser_recognizes_box_drawn_as_line_segments(tmp_path):
    """Regression, JS-only: docs/js/pdf_vector_redaction.js walks pdf.js's raw
    operator list, which does NOT normalize a rectilinear moveto/lineto/lineto/
    lineto/closepath path into a rectangle op the way PyMuPDF's get_drawings()
    does on read (Python's is_bare_rect never saw this gap — see
    _draw_box_as_line_segments). A real Microsoft Print-to-PDF export draws
    every box this way, never with the dedicated 're' operator; before
    decomposeIntoRects learned to recognize this shape, the browser scanner
    missed every redaction box in that file."""
    pytest.importorskip("playwright.sync_api")
    doc, page = _new_page()
    shape = page.new_shape()
    _row_of_glyphs(shape, x0=20, y0=50, n=7)
    _draw_box_as_line_segments(shape, (18, 48, 75, 60))
    shape.commit()
    pdf_path = tmp_path / "line_segment_box.pdf"
    doc.save(pdf_path)
    doc.close()

    try:
        rows = vr.browser_recover_pdf(pdf_path, ocr=False)
    except Exception as e:
        pytest.skip(f"headless browser unavailable ({e})")
    assert len(rows) == 1
    assert rows[0]["n_shapes"] == 7


def test_implicitly_closed_rect_glyph_is_not_mistaken_for_a_box():
    """Regression: a glyph that happens to be a perfect axis-aligned rectangle
    (e.g. "l") but is drawn WITHOUT an explicit closePath must NOT be treated as
    box-like — see _draw_open_rect_glyph. Broadening box-recognition to catch
    real line-segment-drawn boxes (test_multi_rect_compound_path_decomposed_per_item
    et al.) initially also caught these by accident, silently dropping such
    letters from every reconstruction (found on the "claude is cool" demo: a
    real per-character shape count regressed from 12 to 9 once decomposeIntoRects
    grew a hand — the missing 3 were exactly its two "l"s and one "i" stem)."""
    doc, page = _new_page()
    shape = page.new_shape()
    for i in range(3):
        _draw_open_rect_glyph(shape, 20 + i * 8, 50, 24 + i * 8, 60)
    _draw_box(shape, (18, 48, 75, 62))
    shape.commit()

    findings = vr.scan_page(page)
    assert len(findings) == 1
    assert len(findings[0].shapes) == 3
    doc.close()


def test_browser_does_not_mistake_open_rect_glyph_for_a_box(tmp_path):
    """JS side of test_implicitly_closed_rect_glyph_is_not_mistaken_for_a_box —
    the bug it guards was actually introduced and found in the browser port,
    not scan_page; both are covered since PyMuPDF's own get_drawings() applies
    the identical closePath-sensitive normalization."""
    pytest.importorskip("playwright.sync_api")
    doc, page = _new_page()
    shape = page.new_shape()
    for i in range(3):
        _draw_open_rect_glyph(shape, 20 + i * 8, 50, 24 + i * 8, 60)
    _draw_box(shape, (18, 48, 75, 62))
    shape.commit()
    pdf_path = tmp_path / "open_rect_glyph.pdf"
    doc.save(pdf_path)
    doc.close()

    try:
        rows = vr.browser_recover_pdf(pdf_path, ocr=False)
    except Exception as e:
        pytest.skip(f"headless browser unavailable ({e})")
    assert len(rows) == 1
    assert rows[0]["n_shapes"] == 3


def test_browser_recover_agrees_with_python_detection_and_produces_an_image(tmp_path):
    """End-to-end plumbing check: browser_recover_pdf (headless Chromium running
    docs/js/pdf_vector_redaction.js) finds the same hidden-content region Python's
    own scan_page does on the same fixture, and returns actual PNG bytes for it.
    Not an OCR-accuracy test (the triangle fixture isn't real letterforms, so its
    recovered_text is meaningless) — this only guards that Python detection and
    the browser recovery path stay wired together and agree on *where* the
    content is. Requires `playwright install chromium`; skips cleanly if unmet
    (matches this repo's other playwright-based tests/scripts, e.g. pra_download.py)."""
    pytest.importorskip("playwright.sync_api")
    doc, page = _new_page()
    shape = page.new_shape()
    _row_of_glyphs(shape, x0=20, y0=50, n=7)
    _draw_box(shape, (18, 48, 75, 60))
    shape.commit()
    pdf_path = tmp_path / "hidden.pdf"
    doc.save(pdf_path)
    doc.close()

    py_findings = vr.scan_pdf(pdf_path)
    assert len(py_findings) == 1

    try:
        rows = vr.browser_recover_pdf(pdf_path, ocr=False)
    except Exception as e:
        pytest.skip(f"headless browser unavailable ({e})")
    assert len(rows) == 1
    assert rows[0]["page"] == py_findings[0]["page"]
    assert rows[0]["recovered_png"].startswith(b"\x89PNG")


# The audit-export shape the context view exists for: a per-row redaction box over
# one column, with ordinary text in the columns either side.
_CTX_PAGE_W, _CTX_PAGE_H = 612.0, 240.0
_CTX_PLATE_X0, _CTX_PLATE_X1 = 230.0, 284.0
_CTX_REASON_X = 300.0
_CTX_ROWS_Y = (60.0, 90.0, 120.0)


def _audit_table_pdf(path):
    doc, page = _new_page(_CTX_PAGE_W, _CTX_PAGE_H)
    for y in _CTX_ROWS_Y:
        page.insert_text((20, y + 8), "0f3c9a1e-SEARCH-ID", fontsize=8)
        page.insert_text((130, y + 8), "06/14/2024 10:22", fontsize=8)
        page.insert_text((_CTX_REASON_X, y + 8), "Investigation - stolen vehicle", fontsize=8)
    shape = page.new_shape()
    for y in _CTX_ROWS_Y:
        _row_of_glyphs(shape, x0=_CTX_PLATE_X0 + 2, y0=y, n=7)
        _draw_box(shape, (_CTX_PLATE_X0, y - 2, _CTX_PLATE_X1, y + 10))
    shape.commit()
    doc.save(path)
    doc.close()


_PIXEL_PROBE_JS = """async ([x0, x1]) => {
    const img = document.querySelector('.vecctx img');
    await img.decode();
    const c = document.createElement('canvas');
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let red = 0, darkInReason = 0;
    for (let y = 0; y < c.height; y++) {
        for (let x = 0; x < c.width; x++) {
            const i = (y * c.width + x) * 4, r = d[i], g = d[i + 1], b = d[i + 2];
            if (r > 150 && g < 60 && b < 60 && x >= x0 && x <= x1) red++;
            if (r < 90 && g < 90 && b < 90 && x > x1 + 20) darkInReason++;
        }
    }
    return {w: img.naturalWidth, red, darkInReason};
}"""


def test_audit_check_page_shows_recovered_content_in_row_context(tmp_path):
    """The audit-check page groups findings per page and renders each flagged page
    whole with its boxes opened up in place, so hidden content reads next to the
    rest of its row — not only as an isolated crop of the redacted field. Drives the
    real docs/audit-check.html in headless Chromium against a synthetic table."""
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    pdf_path = tmp_path / "audit_table.pdf"
    _audit_table_pdf(pdf_path)
    docs = Path(__file__).resolve().parent.parent / "docs"
    origin = "http://audit-check.test"

    def _serve_docs(route):
        rel = route.request.url[len(origin) + 1:].split("?")[0]
        f = docs / rel
        if f.is_file():
            ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css",
                     "json": "application/json"}.get(f.suffix.lstrip("."), "application/octet-stream")
            route.fulfill(status=200, content_type=ctype, body=f.read_bytes())
        else:
            route.fulfill(status=404, body="")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.route(origin + "/**", _serve_docs)
            page.route("https://gc.zgo.at/**", lambda r: r.abort())
            page.goto(origin + "/audit-check.html")
            page.wait_for_function("window.pdfjsLib && window.PdfVectorRedaction")
            page.set_input_files("#file", str(pdf_path))
            page.wait_for_selector(".vecctx img", timeout=30000)
            blocks = page.locator(".vecblk").count()
            header = page.locator(".vecblk h4").first.inner_text()
            crops = page.locator(".veccrops img").count()
            scale = 6  # CONTEXT_SCALE in pdf_vector_redaction.js
            probe = page.evaluate(_PIXEL_PROBE_JS, [_CTX_PLATE_X0 * scale, _CTX_PLATE_X1 * scale])
            browser.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "net::" in str(e):
            pytest.skip(f"headless browser unavailable ({e})")
        raise

    assert blocks == 1                        # one block for the page, not one per box
    assert "3 boxes" in header and "21 hidden shape(s)" in header
    assert crops == 3                         # isolated reconstructions still available
    assert probe["w"] == int(_CTX_PAGE_W * scale)  # the whole row width, not the plate crop
    assert probe["red"] > 200                 # recovered glyphs drawn inside the plate column
    assert probe["darkInReason"] > 200        # the reason column's own text is rendered alongside
