"""Tests for scripts/article_curate.py — article_id minting.

article_id_for_url replaced a max(n)+1 counter so parallel PRs can't mint
colliding ids; these lock in the properties that makes possible.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import article_curate  # noqa: E402


def test_article_id_is_deterministic():
    url = "https://example.org/news/license-plate-readers"
    assert (article_curate.article_id_for_url(url)
            == article_curate.article_id_for_url(url))


def test_article_id_format_matches_lint():
    aid = article_curate.article_id_for_url("https://example.org/x")
    assert aid.startswith("art_")
    hexpart = aid[len("art_"):]
    assert len(hexpart) == 12
    assert all(c in "0123456789abcdef" for c in hexpart)


def test_distinct_urls_get_distinct_ids():
    a = article_curate.article_id_for_url("https://example.org/a")
    b = article_curate.article_id_for_url("https://example.org/b")
    assert a != b


# ── Phase 2 routing: off_topic vs enriched ───────────────────────


def _phase2_once(tmp_path, monkeypatch, verdict):
    """Drive run_phase2 over a single eligible 'mechanical' entry with a
    stubbed curator verdict. Returns the entry after processing."""
    monkeypatch.setattr(article_curate, "ROOT", tmp_path)
    (tmp_path / "a.txt").write_text("article body text")
    entry = {
        "article_id": "art_aaaaaaaaaaaa",
        "url": "https://eff.org/x",
        "tier": 2,
        "scanner_verdict": "CLEAN — no findings",
        "curation_status": "mechanical",
        "discovered_by": "bing:flock",
        "paths": {"txt": "a.txt"},
        "tags": [],
        "agencies": [],
        "agency_candidates": [],
    }
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(article_curate, "call_claude_for_curation",
                        lambda *a, **k: (verdict, "raw"))
    article_curate.run_phase2(
        [entry], tags_data={"topics": {}, "editorial": {}},
        limit=5, model="x", dry_run=False,
    )
    return entry


def test_phase2_routes_off_topic_to_terminal_status(tmp_path, monkeypatch):
    """off_topic verdict → curation_status 'off_topic' (terminal tombstone),
    not 'needs_review' and not 'enriched'."""
    verdict = {
        "refusal": False, "refusal_reason": None,
        "off_topic": True, "off_topic_reason": "a flock of birds, not ALPR",
        "summary": "", "key_quotes": [], "topic_tags": [],
        "genre": "explainer", "primary_subject_agency_ids": [],
    }
    entry = _phase2_once(tmp_path, monkeypatch, verdict)
    assert entry["curation_status"] == "off_topic"
    assert "off_topic" in entry["curation_error"]


def test_phase2_enriches_on_topic_entry(tmp_path, monkeypatch):
    """The off_topic branch must not swallow a normal on-topic article."""
    verdict = {
        "refusal": False, "refusal_reason": None,
        "off_topic": False, "off_topic_reason": None,
        "summary": "Real ALPR story.", "key_quotes": [], "topic_tags": [],
        "genre": "investigative", "primary_subject_agency_ids": [],
    }
    entry = _phase2_once(tmp_path, monkeypatch, verdict)
    assert entry["curation_status"] == "enriched"
    assert entry["summary"] == "Real ALPR story."
