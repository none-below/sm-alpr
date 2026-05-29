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
