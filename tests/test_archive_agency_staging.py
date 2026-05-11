"""Pin the temp-dir staging behavior in archive_agency.

Invariant: a slug-dir capture is the four-tuple .txt/.html/.json/.pdf.
Either all four land together, or none do. A parse failure (new heading
variant) must NOT leave .txt/.html on disk without a matching .json —
that pollutes downstream consumers that assume html ↔ json parity per
date.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import flock_transparency as ft


def _make_page(body_text, html):
    """Build a MagicMock that quacks like a Playwright Page just enough
    for archive_agency. Returns http 200 with the given body/html.
    """
    page = MagicMock()
    response = MagicMock()
    response.status = 200
    page.goto.return_value = response
    page.inner_text.return_value = body_text
    page.content.return_value = html

    cdp = MagicMock()
    # archive_agency b64-decodes result["data"]; "AAAA" decodes to 3 NULs.
    cdp.send.return_value = {"data": "AAAA"}
    page.context.new_cdp_session.return_value = cdp
    return page


def test_parse_failure_leaves_slug_dir_empty(tmp_path):
    """Probe lands on a Flock portal with an unknown bold heading. The
    parser raises ValueError; archive_agency must return parse_error
    and write nothing under <data_dir>/<slug>/. Earlier behavior left
    .txt + .html on disk with no .json (PR #252 / PR #333 incident).
    """
    body = "\n".join([
        "Some Agency PD",
        "Brand New Mystery Heading",
        "",
        "body content under the mystery heading",
        "",
        "What's Detected",
        "",
        "License Plates",
        "",
    ])
    html = (
        '<p style="font-weight:700">Brand New Mystery Heading</p>'
        '<p style="font-weight:700">What\'s Detected</p>'
    )
    page = _make_page(body, html)

    slug = "fake-mystery-pd"
    status, discovered = ft.archive_agency(page, slug, tmp_path, force=True)

    assert status == ("failed", "parse_error"), status
    assert discovered == []
    slug_dir = tmp_path / slug
    if slug_dir.exists():
        files = sorted(p.name for p in slug_dir.iterdir())
        assert files == [], (
            f"slug dir must be empty on parse_error, got {files}"
        )


def test_parse_success_writes_full_four_tuple(tmp_path):
    """Happy path: parser succeeds, all four artifacts land together."""
    body = "\n".join([
        "Foo PD",
        "Overview",
        "",
        "Foo PD uses Flock Safety LPR technology to capture evidence.",
        "",
        "Policies",
        "",
        "What's Detected",
        "",
        "License Plates",
        "",
    ])
    html = (
        '<p style="font-weight:700">Overview</p>'
        '<p style="font-weight:700">Policies</p>'
        '<p style="font-weight:700">What\'s Detected</p>'
    )
    page = _make_page(body, html)

    slug = "fake-foo-pd"
    status, _ = ft.archive_agency(page, slug, tmp_path, force=True)

    assert not (isinstance(status, tuple) and status[0] == "failed"), status
    slug_dir = tmp_path / slug
    files = sorted(p.name for p in slug_dir.iterdir())
    from datetime import date
    today = date.today().isoformat()
    assert files == [
        f"{today}.html", f"{today}.json", f"{today}.pdf", f"{today}.txt",
    ], files
