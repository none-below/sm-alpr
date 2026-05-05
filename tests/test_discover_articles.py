"""Tests for scripts/discover_articles.py.

Covers the keyword filter (a pure function — easy to regress) and feed
parsing against a static fixture XML so we don't hit the network.
"""

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import discover_articles as da  # noqa: E402


# ── matches_keywords ─────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("Story about ALPR cameras",                     "alpr"),
    ("This is about license plate readers",          "license plate reader"),
    # Returns the first matching keyword from KEYWORDS list order, not
    # the first occurrence in the text. "license plate recognition"
    # appears earlier in the list than "automated license plate".
    ("Automated license plate recognition systems",  "license plate recognition"),
    ("FLOCK SAFETY launches new product",            "flock safety"),  # case-insensitive
    ("Unrelated story about politics",               None),
])
def test_matches_keywords(text, expected):
    """Returns the first matching keyword (lowercased), or None."""
    got = da.matches_keywords(text, da.KEYWORDS)
    assert got == expected


def test_matches_keywords_skips_bare_flock():
    """We deliberately excluded bare 'flock' from the keyword list —
    it's too generic (flocks of birds, etc.). 'flock safety' or
    'flock cameras' must appear to match."""
    assert da.matches_keywords("A flock of geese flew overhead.", da.KEYWORDS) is None
    assert da.matches_keywords("Bird flocking behavior in winter", da.KEYWORDS) is None


def test_matches_keywords_empty_input():
    assert da.matches_keywords("", da.KEYWORDS) is None
    assert da.matches_keywords(None, da.KEYWORDS) is None


def test_matches_keywords_custom_list():
    """Caller can override the default keyword set."""
    assert da.matches_keywords("the cat sat on the mat", ["cat"]) == "cat"
    assert da.matches_keywords("the cat sat on the mat", ["dog"]) is None


# ── fetch_feed: error path ────────────────────────────────────────


def test_fetch_feed_returns_error_on_http_failure(monkeypatch):
    """Ensure HTTP errors are captured rather than thrown."""
    import requests

    def fake_get(*args, **kwargs):
        raise requests.ConnectionError("simulated connection refused")

    monkeypatch.setattr(da.requests, "get", fake_get)
    entries, err = da.fetch_feed("https://example.com/feed.xml")
    assert entries == []
    assert err is not None
    assert "ConnectionError" in err


def test_fetch_feed_returns_error_on_unparseable(monkeypatch):
    """If the response body is not valid RSS/Atom, error out."""
    class FakeResp:
        content = b"not even xml"
        def raise_for_status(self): pass
    monkeypatch.setattr(da.requests, "get", lambda *a, **kw: FakeResp())
    entries, err = da.fetch_feed("https://example.com/feed.xml")
    # feedparser is permissive — for "not even xml" it returns 0 entries
    # AND sets bozo, which we treat as an error since entries is empty.
    assert entries == [] and err is not None


# ── fetch_feed: success against a fixture XML ────────────────────


_FIXTURE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <link>https://example.com/</link>
    <description>fixture</description>
    <item>
      <title>Why ALPR cameras are everywhere</title>
      <link>https://example.com/post-1</link>
      <description>About automated license plate readers</description>
    </item>
    <item>
      <title>Unrelated story about taxes</title>
      <link>https://example.com/post-2</link>
      <description>Tax policy update</description>
    </item>
    <item>
      <title>Flock Safety expands network</title>
      <link>https://example.com/post-3</link>
      <description>New cities sign Flock Safety contracts</description>
    </item>
  </channel>
</rss>
"""


def test_fetch_feed_parses_fixture(monkeypatch):
    class FakeResp:
        content = _FIXTURE_RSS
        def raise_for_status(self): pass
    monkeypatch.setattr(da.requests, "get", lambda *a, **kw: FakeResp())
    entries, err = da.fetch_feed("https://example.com/feed.xml")
    assert err is None
    assert len(entries) == 3
    titles = [e.get("title") for e in entries]
    assert "Why ALPR cameras are everywhere" in titles
    assert "Unrelated story about taxes" in titles


def test_fetch_feed_filter_only_alpr_relevant():
    """Run the filter against the fixture entries: 2 should match
    (ALPR + Flock Safety) and 1 (taxes) should not."""
    import feedparser
    parsed = feedparser.parse(_FIXTURE_RSS)
    matches = []
    for entry in parsed.entries:
        haystack = f"{entry.get('title','')}\n{entry.get('summary','')}"
        kw = da.matches_keywords(haystack, da.KEYWORDS)
        if kw:
            matches.append((entry.get("link"), kw))
    matched_urls = {url for url, _ in matches}
    assert "https://example.com/post-1" in matched_urls  # ALPR
    assert "https://example.com/post-3" in matched_urls  # Flock Safety
    assert "https://example.com/post-2" not in matched_urls  # taxes
    assert len(matches) == 2
