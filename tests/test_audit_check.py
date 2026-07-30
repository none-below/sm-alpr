# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Smoke test for docs/audit-check.html + docs/js/audit-check.js (the client-side
PDF revision/edit checker). Mirrors the per-page CSP/GoatCounter checks in
test_dashboard.py."""
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"
HTML = (DOCS / "audit-check.html").read_text()
JS = (DOCS / "js" / "audit-check.js").read_text()


def test_csp_and_goatcounter():
    assert "Content-Security-Policy" in HTML
    assert "connect-src 'self'" in HTML
    # repo-load fetch host must be whitelisted, plus the pdf.js CDN
    assert "raw.githubusercontent.com" in HTML
    assert "cdnjs.cloudflare.com" in HTML
    assert "gc.zgo.at/count.js" in HTML
    assert "none-below.goatcounter.com" in HTML
    assert "css/shared.css" in HTML


def test_semi_hidden_noindex():
    assert "noindex" in HTML


def test_wiring_elements_present():
    assert "js/audit-check.js" in HTML
    assert "cdnjs.cloudflare.com/ajax/libs/pdf.js" in HTML
    for el in ('id="file"', 'id="url"', 'id="loadbtn"', 'id="out"', 'id="picker"'):
        assert el in HTML, el


def test_js_has_w012541_picker():
    assert "MANIFEST" in JS and "renderPicker" in JS
    assert "W012541-041426" in JS
    assert "audit_check_manifest.json" in JS  # loads the build-time picker manifest
    assert "flattened" in JS                  # opaque-file (Print-to-PDF) flag
    # chips carry flags only — no recovered edit detail or person names baked in
    assert "Jodi Ferreira" not in JS


def test_js_repo_loader_and_shareable_link():
    assert "raw.githubusercontent.com/none-below/sm-alpr" in JS
    assert "history.replaceState" in JS  # shareable ?pdf= link
    assert 'params.get("pdf")' in JS     # auto-load from query


def test_js_uses_order_independent_token_diff_and_editor():
    # token/UUID-set diff (not the old coordinate-binned line diff) + editor name
    assert "diffRevisions" in JS and "rowsOf" in JS and "isTrivial" in JS
    assert "dcCreator" in JS              # Author / XMP dc:creator (who edited)
    assert "transform[5]" not in JS       # no y-coordinate line binning (the false-positive cause)


def test_js_attributes_multi_row_edits_and_sums_net_change():
    # a token removed from several rows in one save is pinned to each affected row
    # (mirrors attribute_tokens in pdf_audit_revisions.py) instead of "(no unique row)",
    # and multi-save files get a summed original-vs-produced view
    assert "attributeTokens" in JS and "renderRowDiffs" in JS
    assert "no unique row" not in JS
    assert "net change" in JS
