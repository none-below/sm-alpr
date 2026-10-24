#!/usr/bin/env python3
"""Regression tests for render_archive_pdf — the print step that flows
Flock's fixed-height sharing lists into the archive PDF.

The bug this pins: the first fix was a blunt
`*{max-height:none; overflow:visible}`. Flock's list box also carries an
inline `height:var(--tp-access-table-viewport-height, 480px)`, which that
rule left alone, so the box stayed 480px and the rest of the list painted
over the Policy & Trust cards below it. The rule also un-hid every row's
ellipsis name cell, so long names ran into the next column.

The fixture is hand-built HTML, not a scraped portal page. It copies the
structure of a real capture (lafayette-pd-in 2026-10-22: the inline styles,
nesting and row heights) with made-up agency names. Every assertion is on
the printed PDF, since that is what gets archived.
"""

import base64
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import flock_transparency as ft


def _name(i):
    # Every 25th name is long enough to wrap to three lines at print width,
    # the case where a fixed 48px row would clip it.
    if i % 25 == 0:
        return f"Agency {i:04d} Department of Natural Resources Law Enforcement Division Police Dept"
    return f"Agency {i:04d} PD"


def _cell(name):
    return (
        '<div style="display:flex;flex-direction:column;justify-content:center;'
        "align-items:flex-start;flex:1 0 0;min-width:0;align-self:stretch;"
        'position:relative;border-bottom:1px solid #e5e5e5">'
        '<div style="font-size:14px;line-height:20px;width:100%;min-width:0;'
        'white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'
        f"{name}</div></div>"
    )


def _card(title, names):
    rows = "".join(
        '<div style="display:flex;height:48px;align-items:center;gap:16px;'
        f'align-self:stretch;min-width:0">{_cell(a)}{_cell(b)}</div>'
        for a, b in zip(names[::2], names[1::2])
    )
    return (
        '<div style="padding:16px;background:#fff;border-radius:8px;border:1px solid #ddd">'
        '<div style="display:flex;flex-direction:column;gap:16px">'
        f"<div>{title}</div>"
        '<div style="display:flex;flex-direction:row;gap:8px;align-items:flex-start">'
        '<div style="flex:1;min-width:0;white-space:pre-line">'
        '<div style="position:relative;padding-right:12px;min-width:0">'
        '<div class="list" style="height:var(--tp-access-table-viewport-height, 480px);'
        'max-height:480px;overflow-y:auto;overflow-x:hidden">'
        f'<div style="display:flex;flex-direction:column;min-width:0">{rows}</div></div>'
        # Flock's custom scrollbar track, an absolute sibling of the box.
        '<div style="position:absolute;top:0;bottom:0;right:0;width:12px;pointer-events:none">'
        '<div style="height:40px;background:#789"></div></div>'
        "</div></div></div></div></div>"
    )


OUTBOUND = [_name(i) for i in range(1, 301)]
INBOUND = [_name(i) for i in range(301, 451)]
ALL_NAMES = OUTBOUND + INBOUND


def portal_html(main_style=""):
    return (
        "<!doctype html><html><head><style>"
        "body{margin:0;font:16px sans-serif;background:#f6f5f0}</style></head><body>"
        f'<main style="{main_style}">'
        # A fixed-height hero with an absolute overlay and an ellipsized title:
        # page furniture outside the lists that must print as it renders.
        '<div id="hero" style="position:relative;height:200px;background:#2d4a33">'
        '<div style="position:absolute;inset:0;padding:24px;color:#fff">'
        '<div id="title" style="width:160px;white-space:nowrap;overflow:hidden;'
        'text-overflow:ellipsis">Sample County Sheriff Transparency Portal</div>'
        "</div></div>"
        '<section><div style="--tp-access-table-viewport-height:480px;display:grid;'
        'grid-template-columns:1fr 1fr;gap:16px;padding:16px">'
        f'{_card("Sharing Network Data With", OUTBOUND)}'
        f'{_card("Receiving Network Data From", INBOUND)}'
        "</div></section>"
        '<section style="background:#2d4a33;color:#fff;padding:16px">'
        "<h2>Policy &amp; Trust</h2>"
        "<p>Acceptable Use Policy</p><p>Data is used for law enforcement purposes only.</p>"
        "</section></main></body></html>"
    )


# The blunt rule that shipped first, kept so a test can show the fixture
# reproduces its overlap.
LEGACY_CSS = "*{max-height:none !important;overflow:visible !important;}"


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        b = pw.chromium.launch(args=["--disable-dev-shm-usage", "--no-sandbox"])
        yield b
        b.close()
    finally:
        pw.stop()


@pytest.fixture
def page(browser):
    p = browser.new_page(viewport=ft.VIEWPORT)
    p.set_content(portal_html())
    yield p
    p.close()


def _pdf_report(pdf_bytes):
    """Names missing from the PDF, colliding word pairs, and whether the
    Policy & Trust heading prints after the last list row."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    flat = " ".join("".join(pg.get_text() for pg in doc).split())
    missing = [n for n in ALL_NAMES if n not in flat]

    collisions = 0
    last_row = (-1, -1.0)
    heading = None
    for pno, pg in enumerate(doc):
        words = sorted(pg.get_text("words"), key=lambda w: w[1])
        for i, a in enumerate(words):
            if re.fullmatch(r"\d{4}", a[4]):
                last_row = max(last_row, (pno, a[3]))
            for b in words[i + 1:]:
                if b[1] >= a[3] - 1:
                    break
                if (a[5], a[6]) == (b[5], b[6]):
                    continue  # same text line
                if (min(a[2], b[2]) - max(a[0], b[0]) > 1
                        and min(a[3], b[3]) - max(a[1], b[1]) > 2):
                    collisions += 1
        for r in pg.search_for("Policy & Trust"):
            heading = heading or (pno, r.y0)
    return {
        "pages": doc.page_count,
        "missing": missing,
        "collisions": collisions,
        "heading_after_lists": heading is not None and heading > last_row,
    }


def _print_only(page):
    cdp = page.context.new_cdp_session(page)
    result = cdp.send("Page.printToPDF", {
        "printBackground": True, "paperWidth": 11, "paperHeight": 17,
        "marginTop": 0.4, "marginBottom": 0.4, "marginLeft": 0.4, "marginRight": 0.4,
    })
    cdp.detach()
    return base64.b64decode(result["data"])


def test_fixture_clips_without_expansion(page):
    """Sanity: printed as-is, the lists clip to their first rows."""
    report = _pdf_report(_print_only(page))
    assert len(report["missing"]) > 400, "fixture must clip most of the list"


def test_legacy_rule_paints_the_list_over_what_follows(page):
    """Characterize the bug: the blunt rule overlaps text."""
    page.add_style_tag(content=LEGACY_CSS)
    report = _pdf_report(_print_only(page))
    assert report["collisions"] > 0 or not report["heading_after_lists"], (
        "expected the legacy rule to overlap text; if not, the fixture no "
        "longer reproduces the bug"
    )


def test_pdf_holds_every_name_with_nothing_overlapping(page):
    report = _pdf_report(ft.render_archive_pdf(page))
    assert not report["missing"], f"{len(report['missing'])} names missing, e.g. {report['missing'][:3]}"
    assert report["collisions"] == 0, f"{report['collisions']} colliding word pairs"
    assert report["heading_after_lists"], "Policy & Trust must print after the last row"


def test_rows_keep_their_height_and_grow_for_wrapped_names(page):
    """Rows print at Flock's 48px; only a row whose name wraps past two
    lines grows. Measured at ~10.2in, the printable width of the PDF."""
    ft.render_archive_pdf(page)
    page.set_viewport_size({"width": 980, "height": 900})
    rows = page.evaluate(
        "() => [...document.querySelectorAll('.list > div > div')].map(r => r.offsetHeight)"
    )
    assert min(rows) == 48, "short rows should keep their on-screen height"
    assert max(rows) > 48, "a three-line name should grow its row, not overflow it"


def test_only_the_list_boxes_change(page):
    """The hero keeps its height and the title stays ellipsized: nothing
    outside the scroll boxes is restyled."""
    before = page.evaluate("() => document.querySelector('#hero').offsetHeight")
    ft.render_archive_pdf(page)
    after = page.evaluate(
        "() => {"
        "  const t = document.querySelector('#title');"
        "  const marks = [...document.querySelectorAll('[data-print-expand]')];"
        "  return {hero: document.querySelector('#hero').offsetHeight,"
        "          titleWs: getComputedStyle(t).whiteSpace,"
        "          titleClipped: t.scrollWidth > t.clientWidth,"
        "          marks: marks.map(m => [m.className, m.dataset.printExpand])}; }"
    )
    assert after["hero"] == before == 200
    assert after["titleWs"] == "nowrap" and after["titleClipped"]
    assert after["marks"] == [["list", "list"], ["list", "list"]]


def test_page_level_scroll_container_only_grows(browser):
    """If Flock made <main> the scroll container, it must grow without the
    list rules (fixed heights off, text wrapping) spreading to the whole page."""
    p = browser.new_page(viewport=ft.VIEWPORT)
    p.set_content(portal_html(main_style="height:100vh;overflow-y:auto"))
    pdf = ft.render_archive_pdf(p)
    state = p.evaluate(
        "() => ({main: document.querySelector('main').dataset.printExpand,"
        "        hero: document.querySelector('#hero').offsetHeight,"
        "        titleWs: getComputedStyle(document.querySelector('#title')).whiteSpace})"
    )
    p.close()

    assert state == {"main": "outer", "hero": 200, "titleWs": "nowrap"}
    report = _pdf_report(pdf)
    assert not report["missing"] and report["collisions"] == 0


def _mock_page():
    page = MagicMock()
    page.goto.return_value = MagicMock(status=200)
    page.inner_text.return_value = "\n".join(
        ["Foo PD", "What's Detected", "", "License Plates", ""]
    )
    page.content.return_value = '<p style="font-weight:700">What\'s Detected</p>'
    page.evaluate.return_value = 2
    cdp = MagicMock()
    cdp.send.return_value = {"data": "JVBERi0="}  # "%PDF-"
    page.context.new_cdp_session.return_value = cdp
    return page


def test_saved_html_is_captured_before_the_expansion(tmp_path):
    """The archived .html must be Flock's DOM, not one carrying our print
    markers and stylesheet."""
    page = _mock_page()
    ft.archive_agency(page, "foo-pd", tmp_path, force=True)

    names = [c[0] for c in page.mock_calls]
    assert "evaluate" in names, "expansion never ran"
    assert names.index("content") < names.index("evaluate")


def test_capture_survives_a_failed_expansion(tmp_path, capsys):
    """A page that navigates mid-capture breaks evaluate(); the slug must
    still save (with a clipped PDF) rather than abort the crawl."""
    page = _mock_page()
    page.evaluate.side_effect = Exception("Execution context was destroyed")

    status, _ = ft.archive_agency(page, "foo-pd", tmp_path, force=True)

    assert isinstance(status, Path) and status.read_bytes().startswith(b"%PDF-")
    assert "couldn't expand scroll boxes" in capsys.readouterr().out
