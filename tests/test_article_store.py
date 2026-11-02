"""Tests for scripts/article_store.py — the sharded registry load/save."""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import article_store  # noqa: E402


def _entry(aid, **over):
    e = {"article_id": aid, "url": f"https://eff.org/{aid}", "title": f"T {aid}"}
    e.update(over)
    return e


def test_save_then_load_roundtrips_sorted(tmp_path):
    d = tmp_path / "reg"
    article_store.save_registry([_entry("art_002"), _entry("art_001")], d)
    loaded = article_store.load_registry(d)
    # load_registry sorts by article_id regardless of write order.
    assert [e["article_id"] for e in loaded] == ["art_001", "art_002"]
    assert loaded[0]["url"] == "https://eff.org/art_001"


def test_save_writes_one_file_per_article(tmp_path):
    d = tmp_path / "reg"
    article_store.save_registry([_entry("art_001"), _entry("art_002")], d)
    assert sorted(p.name for p in d.glob("*.json")) == [
        "art_001.json", "art_002.json"]


def test_save_only_writes_changed_shards(tmp_path):
    # The diff-minimization property: a one-entry edit is a one-file write,
    # not a rewrite of every shard.
    d = tmp_path / "reg"
    entries = [_entry("art_001"), _entry("art_002")]
    assert article_store.save_registry(entries, d) == 2   # both new
    assert article_store.save_registry(entries, d) == 0   # nothing changed
    entries[0]["title"] = "changed"
    assert article_store.save_registry(entries, d) == 1   # only art_001


def test_load_missing_dir_returns_empty(tmp_path):
    assert article_store.load_registry(tmp_path / "does-not-exist") == []


# ── story_key ────────────────────────────────────────────────────


def test_story_key_ignores_section_prefix():
    """RWC Pulse newsletter path and feed permalink are one story."""
    a = article_store.story_key(
        "https://www.rwcpulse.com/express-newsletter-content/2026/11/02/"
        "redwood-city-postpones-alpr-recommendation-until-2027/")
    b = article_store.story_key(
        "https://www.rwcpulse.com/public-safety/2026/11/02/"
        "redwood-city-postpones-alpr-recommendation-until-2027/")
    assert a == b == ("rwcpulse.com/2026/11/02/"
                      "redwood-city-postpones-alpr-recommendation-until-2027")


def test_story_key_distinguishes_host_date_and_slug():
    base = "https://rwcpulse.com/news/2026/11/02/story/"
    assert article_store.story_key(base) != article_store.story_key(
        "https://almanacnews.com/news/2026/11/02/story/")
    assert article_store.story_key(base) != article_store.story_key(
        "https://rwcpulse.com/news/2026/11/03/story/")
    assert article_store.story_key(base) != article_store.story_key(
        "https://rwcpulse.com/news/2026/11/02/other-story/")


def test_story_key_none_without_dated_slug_path():
    assert article_store.story_key("https://eff.org/deeplinks/2026/11/x") is None
    assert article_store.story_key("https://eff.org/post-1") is None
    assert article_store.story_key(
        "https://rwcpulse.com/news/2026/11/02/story/amp/") is None
