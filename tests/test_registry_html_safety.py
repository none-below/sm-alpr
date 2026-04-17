"""Reject executable HTML in registry fields that are rendered unescaped.

scripts/build_report_data.py and docs/js/report.js thread a small number
of registry fields through to the browser as raw HTML so curators can
include anchor tags, <em>, etc. That trust model depends on never
letting script-carrying markup into the registry. This test enforces the
policy without adding a runtime sanitizer dependency.

Covers three insertion points documented in build_report_data.py and
report.js:
  - agency_registry.json entries[].notes                 -> rendered raw
  - agency_registry.json entries[].notes (flagged recipients) -> rendered raw
  - agency_registry.json entries[].data_concerns[].description -> rendered raw
"""

import json
import re
from pathlib import Path

import pytest

REGISTRY_PATH = Path("assets/agency_registry.json")

# Patterns that would introduce executable content when the string is
# inserted into innerHTML. The allowlist is the complement — any other
# HTML (anchors, em, br, etc.) passes through.
#
# - <script     : inline or src'd script blocks
# - on<word>=   : inline event handlers (onclick, onerror, onload, ...)
# - javascript: : javascript-scheme URIs inside hrefs
# - data:...html: rare but real; data-URIs delivering HTML
_SCRIPT_TAG_RE = re.compile(r"<\s*script\b", re.IGNORECASE)
_INLINE_HANDLER_RE = re.compile(r"\bon[a-z]+\s*=", re.IGNORECASE)
_JS_SCHEME_RE = re.compile(r"javascript\s*:", re.IGNORECASE)
_DATA_HTML_RE = re.compile(r"data\s*:[^,]*html", re.IGNORECASE)

_PATTERNS = [
    ("script tag", _SCRIPT_TAG_RE),
    ("inline event handler", _INLINE_HANDLER_RE),
    ("javascript: scheme", _JS_SCHEME_RE),
    ("data:*html URI", _DATA_HTML_RE),
]


def _violations(label, text):
    """Return [(pattern_name, matched_fragment), ...] found in text."""
    if not isinstance(text, str):
        return []
    out = []
    for name, rx in _PATTERNS:
        m = rx.search(text)
        if m:
            # Grab a few chars of context for the error message.
            start = max(0, m.start() - 10)
            end = min(len(text), m.end() + 20)
            out.append((name, text[start:end]))
    return out


@pytest.fixture(scope="module")
def registry():
    return json.loads(REGISTRY_PATH.read_text())


def test_notes_fields_free_of_executable_html(registry):
    """Registry 'notes' is inserted raw into report pages; no script content."""
    bad = []
    for e in registry:
        for name, fragment in _violations("notes", e.get("notes") or ""):
            bad.append(f"  {e['slug']} notes: {name} near {fragment!r}")
    assert not bad, (
        "Registry notes contain executable HTML patterns. These fields "
        "are rendered unescaped by report.js:\n" + "\n".join(bad)
    )


def test_data_concerns_description_free_of_executable_html(registry):
    """data_concerns[].description is inserted raw into report pages."""
    bad = []
    for e in registry:
        for c in e.get("data_concerns") or []:
            for name, fragment in _violations("data_concerns.description",
                                              c.get("description") or ""):
                bad.append(f"  {e['slug']} data_concerns.description: {name} near {fragment!r}")
    assert not bad, (
        "Registry data_concerns.description contains executable HTML "
        "patterns. These fields are rendered unescaped:\n" + "\n".join(bad)
    )


def test_notes_coverage(registry):
    """Sanity check: if this assertion breaks, the pattern runs may be over
    an empty dataset and hide real problems."""
    have_notes = sum(1 for e in registry if e.get("notes"))
    assert have_notes >= 10, (
        f"Only {have_notes} entries have notes — either the registry was "
        "stripped or the test is looking at the wrong file."
    )
