"""Tests for the discovery-reject triage pipeline.

Covers discover_articles.parse_rejects (stderr → (url, domain) pairs) and
the discover_reject_issue grouping/ignore/render logic. All pure functions,
no network.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import discover_articles as da  # noqa: E402
import discover_reject_issue as dri  # noqa: E402


# ── parse_rejects ────────────────────────────────────────────────


def test_parse_rejects_extracts_domain_rejects():
    stderr = (
        "REJECT https://www.msn.com/a: domain 'msn.com' not in sources.json. "
        "Add the domain (with tier/stance) and re-run.\n"
        "REJECT https://x.com/b: scheme must be http(s), got ''\n"  # malformed
        "appended=0 duplicate=0 rejected=1 -> queue/\n"
    )
    assert da.parse_rejects(stderr) == [("https://www.msn.com/a", "msn.com")]


def test_parse_rejects_empty():
    assert da.parse_rejects("") == []
    assert da.parse_rejects("appended=2 duplicate=0 rejected=0 -> queue/") == []


# ── is_ignored ───────────────────────────────────────────────────


def test_is_ignored_matches_apex_and_subdomain():
    ig = {"businessinsider.com", "msn.com"}
    assert dri.is_ignored("markets.businessinsider.com", ig)
    assert dri.is_ignored("msn.com", ig)
    assert dri.is_ignored("www.msn.com", ig)  # normalized first
    assert not dri.is_ignored("hoodline.com", ig)
    # A domain that merely ends in the apex string but isn't a subdomain
    # must not match (notbusinessinsider.com).
    assert not dri.is_ignored("notbusinessinsider.com", ig)


# ── group_rejects ────────────────────────────────────────────────


def test_group_rejects_filters_counts_and_dedups():
    rejects = [
        {"url": "https://a.com/1", "domain": "a.com", "discovered_by": "bing:alpr"},
        {"url": "https://a.com/2", "domain": "a.com", "discovered_by": "bing:flock"},
        {"url": "https://a.com/1", "domain": "a.com", "discovered_by": "bing:alpr"},  # dup url
        {"url": "https://msn.com/x", "domain": "msn.com", "discovered_by": "bing:alpr"},
    ]
    groups = dri.group_rejects(rejects, {"msn.com"})
    assert len(groups) == 1
    g = groups[0]
    assert g["domain"] == "a.com"
    assert g["count"] == 3  # counts every reject row...
    assert g["urls"] == ["https://a.com/1", "https://a.com/2"]  # ...but de-dups urls
    assert g["sources"] == {"bing:alpr", "bing:flock"}


def test_group_rejects_sorted_by_count_desc():
    rejects = [
        {"url": "https://a.com/1", "domain": "a.com", "discovered_by": "x"},
        {"url": "https://b.com/1", "domain": "b.com", "discovered_by": "x"},
        {"url": "https://b.com/2", "domain": "b.com", "discovered_by": "x"},
    ]
    groups = dri.group_rejects(rejects, set())
    assert [g["domain"] for g in groups] == ["b.com", "a.com"]


# ── render_body ──────────────────────────────────────────────────


def test_render_body_empty_when_nothing_pending():
    assert dri.render_body([]) == ""


def test_render_body_includes_domain_and_urls():
    groups = dri.group_rejects(
        [{"url": "https://hoodline.com/x", "domain": "hoodline.com",
          "discovered_by": "bing:flock"}],
        set(),
    )
    body = dri.render_body(groups)
    assert "hoodline.com" in body
    assert "https://hoodline.com/x" in body
    assert "awaiting triage" in body


def test_render_body_truncates_examples():
    urls = [f"https://a.com/{i}" for i in range(5)]
    groups = dri.group_rejects(
        [{"url": u, "domain": "a.com", "discovered_by": "x"} for u in urls],
        set(),
    )
    body = dri.render_body(groups, max_examples=2)
    assert "https://a.com/0" in body
    assert "https://a.com/1" in body
    assert "https://a.com/2" not in body
    assert "and 3 more" in body
