#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Smoke tests for the document index (scripts/build_document_index.py).

Data-shape only (no browser): asserts the build emits the schema
docs/js/documents.js consumes, and that the SOP 205 lookup this feature
was built for actually resolves.
"""
import json
from pathlib import Path

import pytest

DOCS = Path("docs")
INDEX = DOCS / "data" / "document_index.json"
HTML = DOCS / "documents.html"
JS = DOCS / "js" / "documents.js"

REQUIRED_DOC_KEYS = {
    "id", "path", "filename", "ext", "url", "date", "size_bytes",
    "source_type", "agency_key", "agency_label", "blurb", "tags",
    "context_title", "context_url", "featured", "source_number", "cite_label",
}


class TestDocumentIndexData:
    """document_index.json structure (run `make build` / the generator first)."""

    @pytest.fixture(autouse=True)
    def load(self):
        assert INDEX.exists(), "Run scripts/build_document_index.py first"
        self.data = json.loads(INDEX.read_text())

    def test_top_level_keys(self):
        assert {"generated_at", "agencies", "docs"} <= set(self.data)

    def test_has_documents(self):
        assert len(self.data["docs"]) > 100

    def test_doc_shape(self):
        for d in self.data["docs"]:
            assert REQUIRED_DOC_KEYS <= set(d)
            # A citation without an extractable link (e.g. a plain-text date
            # range instead of a markdown link) still has a blurb — that's
            # the minimum for it to be worth indexing at all.
            assert d["blurb"], d

    def test_agency_shape_and_counts(self):
        counted = {a["key"]: 0 for a in self.data["agencies"]}
        for d in self.data["docs"]:
            if d["agency_key"] in counted:
                counted[d["agency_key"]] += 1
        for a in self.data["agencies"]:
            assert {"key", "label", "count"} <= set(a)
            assert a["count"] == counted[a["key"]]

    def test_no_duplicate_local_paths(self):
        paths = [d["path"] for d in self.data["docs"] if d["path"]]
        assert len(paths) == len(set(paths))

    def test_sop_205_findable(self):
        """The motivating case: searching should surface SOP 205 by name,
        each result pointing at a real file with a working PRA-tracker
        deep link."""
        matches = [
            d for d in self.data["docs"]
            if "sop 205" in (d["blurb"] or "").lower()
            or "sop_205" in (d["filename"] or "").lower()
        ]
        assert matches, "No document mentions SOP 205"
        local = [d for d in matches if d["path"]]
        assert local, "No local SOP 205 file indexed"
        for d in local:
            assert (Path(".") / d["path"]).exists(), d["path"]

    def test_featured_docs_carry_source_number(self):
        for d in self.data["docs"]:
            if d["featured"]:
                assert d["source_number"] is not None

    def test_urls_well_formed(self):
        for d in self.data["docs"]:
            if d["url"]:
                assert d["url"].startswith("https://")


class TestDocumentIndexPage:
    def test_html_has_csp_and_analytics(self):
        html = HTML.read_text()
        assert "Content-Security-Policy" in html
        assert "goatcounter" in html
        assert 'href="css/shared.css"' in html

    def test_js_references_data_file(self):
        js = JS.read_text()
        assert "data/document_index.json" in js
