#!/usr/bin/env python3
"""Regression tests for EXPAND_SLIDERS_JS — the print-time unclamp that
flows Flock's fixed-height sharing "slider" into the archive PDF.

The bug this pins: the original fix was a blunt
`*{max-height:none; overflow:visible}`. That made the clipped rows paint,
but the slider kept its fixed `height`, so the list spilled out of its box
and rendered *on top of* the cards below it. Long agency lists came out as
unreadable text-over-text.

The fixture below is hand-built HTML, not a scraped portal page — it just
reproduces the structural shape (fixed-height scroll box, card after it).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import flock_transparency as ft


# Structural stand-in for a Flock portal: a fixed-height scroll box holding
# far more rows than fit, followed by a card that must not be painted over.
SLIDER_PAGE = """
<!doctype html><html><head><style>
  body { margin: 0; font: 16px sans-serif; }
  .section { height: 300px; }             /* ancestor pinning a height */
  .slider  { height: 200px; overflow-y: auto; }
  .row     { height: 24px; }
  .card    { background: #fff; }
</style></head><body>
  <div class="section"><div class="slider">
    %s
  </div></div>
  <div class="card"><p id="after">Acceptable Use Policy</p></div>
</body></html>
""" % "\n".join(f'<div class="row">Agency Number {i} PD</div>' for i in range(120))


# Counts overlapping pairs of text-owning elements that are not ancestor
# and descendant of one another — i.e. visible text painted over text.
COUNT_OVERLAPS = r"""
() => {
  const leaves = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let el;
  while ((el = walk.nextNode())) {
    let own = '';
    for (const n of el.childNodes) if (n.nodeType === 3) own += n.nodeValue;
    if (!own.trim()) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    leaves.push({el, x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height});
  }
  leaves.sort((a, b) => a.y - b.y);
  let pairs = 0;
  for (let i = 0; i < leaves.length; i++) {
    const a = leaves[i];
    for (let j = i + 1; j < leaves.length; j++) {
      const b = leaves[j];
      if (b.y >= a.y + a.h) break;
      if (b.x >= a.x + a.w || a.x >= b.x + b.w) continue;
      if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
      pairs++;
    }
  }
  return pairs;
}
"""

# The blunt fix that shipped first, kept here so the test can show it is the
# one that produces the overlap.
LEGACY_CSS = "*{max-height:none !important;overflow:visible !important;}"


@pytest.fixture(scope="module")
def browser():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    b = pw.chromium.launch(args=["--disable-dev-shm-usage", "--no-sandbox"])
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def page(browser):
    p = browser.new_page(viewport={"width": 1100, "height": 800})
    p.set_content(SLIDER_PAGE)
    p.emulate_media(media="print")
    yield p
    p.close()


def _metrics(page):
    return {
        "overlaps": page.evaluate(COUNT_OVERLAPS),
        "doc_height": page.evaluate("() => document.documentElement.scrollHeight"),
        # A clipped row still has layout height — it is just scrolled out of
        # its box and never painted. So count rows that actually fall inside
        # the client box of their nearest clipping ancestor.
        "rows_painted": page.evaluate(
            "() => [...document.querySelectorAll('.row')].filter(row => {"
            "  const r = row.getBoundingClientRect();"
            "  for (let n = row.parentElement; n; n = n.parentElement) {"
            "    const cs = getComputedStyle(n);"
            "    if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;"
            "    const c = n.getBoundingClientRect();"
            "    return r.top >= c.top - 1 && r.bottom <= c.bottom + 1;"
            "  }"
            "  return true;"
            "}).length"
        ),
        "slider_clipped": page.evaluate(
            "() => { const s = document.querySelector('.slider');"
            "return s.scrollHeight > s.clientHeight + 1; }"
        ),
    }


def test_legacy_css_overlaps_the_following_card(page):
    """Characterize the bug: the old blunt rule paints rows over the card."""
    before = _metrics(page)
    assert before["slider_clipped"], "fixture must start with a clipped slider"

    page.add_style_tag(content=LEGACY_CSS)
    after = _metrics(page)

    assert after["overlaps"] > 0, (
        "expected the legacy rule to leave text painted over text; if this "
        "stops being true the fixture no longer reproduces the bug"
    )


def test_expansion_grows_the_box_instead_of_spilling(page):
    """The shipped fix: slider grows, page reflows, nothing overlaps."""
    before = _metrics(page)
    assert before["slider_clipped"]
    assert before["rows_painted"] < 120, "fixture must start with rows clipped away"

    expanded = page.evaluate(ft.EXPAND_SLIDERS_JS)
    after = _metrics(page)

    assert expanded >= 1, "should have found the clipped slider"
    assert after["overlaps"] == 0, f"{after['overlaps']} text boxes overlap"
    assert after["rows_painted"] == 120, "every row must land in the PDF"
    assert after["doc_height"] > before["doc_height"], (
        "the page must get taller — that is the difference between growing "
        "the box and spilling out of it"
    )


def test_expansion_reflows_content_below_the_slider(page):
    """The card after the slider must move down past the full list, not sit
    under it. This is the precise thing the legacy rule got wrong."""
    page.evaluate(ft.EXPAND_SLIDERS_JS)
    gap = page.evaluate(
        "() => {"
        "  const s = document.querySelector('.slider').getBoundingClientRect();"
        "  const a = document.querySelector('#after').getBoundingClientRect();"
        "  return a.top - s.bottom; }"
    )
    assert gap >= 0, f"card starts {-gap:.0f}px above the end of the list"


def test_expansion_is_a_noop_without_a_clipped_box(browser):
    """Portals with short lists must render exactly as they do today."""
    p = browser.new_page(viewport={"width": 1100, "height": 800})
    p.set_content(
        "<!doctype html><html><body style='margin:0'>"
        "<div><p>Short list</p><p>Two entries</p></div></body></html>"
    )
    p.emulate_media(media="print")
    before = p.evaluate("() => document.body.outerHTML")
    expanded = p.evaluate(ft.EXPAND_SLIDERS_JS)
    after = p.evaluate("() => document.body.outerHTML")
    p.close()

    assert expanded == 0, "nothing was clipped, so nothing should be touched"
    assert before == after, "no inline styles should have been written"


def test_archive_agency_resets_media_emulation(tmp_path):
    """The Page outlives archive_agency and is reused for the next slug.
    Leaving print media emulated would make the next capture's inner_text()
    read through the site's print stylesheet."""
    from unittest.mock import MagicMock

    page = MagicMock()
    page.goto.return_value = MagicMock(status=200)
    page.inner_text.return_value = "\n".join(
        ["Foo PD", "What's Detected", "", "License Plates", ""]
    )
    page.content.return_value = '<p style="font-weight:700">What\'s Detected</p>'
    page.evaluate.return_value = 1
    cdp = MagicMock()
    cdp.send.return_value = {"data": "AAAA"}
    page.context.new_cdp_session.return_value = cdp

    ft.archive_agency(page, "foo-pd", tmp_path, force=True)

    media = [c.kwargs.get("media") for c in page.emulate_media.call_args_list]
    assert media == ["print", "null"], f"expected print then reset, got {media}"
    page.evaluate.assert_any_call(ft.EXPAND_SLIDERS_JS)
