"""Tests for scripts/build_articles_data.py — the article viewer's ordering.

The viewer promises "newest first". published_at arrives from scrapes in
four different shapes, and the old plain-string sort compared them
character by character, which floated "Sep 22nd 2021" and "8/25/2025"
above every 2026 ISO date. These lock in the normalization.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_articles_data as bad  # noqa: E402


def ts(value):
    return bad.parse_timestamp(value)


def test_parses_every_published_at_shape_in_the_corpus():
    # One instant, 2021-09-22T00:00Z, written four ways.
    iso_z = ts("2021-09-22T00:00:00.000Z")
    assert ts("2021-09-22T00:00:00+00:00") == iso_z
    assert ts("2021-09-22") == iso_z
    assert ts("9/22/2021 12:00:00 AM") == iso_z
    assert ts("Sep 22nd 2021") == iso_z


def test_offsets_are_normalized_not_compared_as_text():
    # 09:23-07:00 is 16:23Z — later than 14:03Z, though the string
    # "09:..." sorts below "14:...".
    assert ts("2026-09-16T09:23:19-07:00") > ts("2026-09-16T14:03:39+00:00")


def test_us_format_sorts_above_an_older_iso_date():
    # The exact inversion the string sort produced: a 2025 US-format
    # date outranking every 2026 ISO date because "8" > "2".
    assert ts("8/25/2025 5:46:45 PM") < ts("2026-05-27T08:30:00+00:00")


def test_ordinal_suffixes_and_month_spellings():
    assert ts("May 1st 2025") == ts("2025-05-01")
    assert ts("Nov 19th 2015") == ts("2015-11-19")
    assert ts("November 19th 2015") == ts("2015-11-19")


def test_unparseable_and_empty_values_are_none():
    for v in (None, "", "   ", "no date here", 20210922, {"y": 2021}):
        assert ts(v) is None


def _sorted(entries):
    """Apply the generator's ordering and return the resulting ids."""
    out = sorted(entries, key=bad.sort_key, reverse=True)
    return [e["article_id"] for e in out]


def test_sorts_newest_first_across_mixed_formats():
    entries = [
        {"article_id": "old_iso", "published_ts": ts("2014-01-29T20:46:32+00:00")},
        {"article_id": "newest", "published_ts": ts("2026-09-17T09:00:00.000Z")},
        {"article_id": "us_2025", "published_ts": ts("8/25/2025 5:46:45 PM")},
        {"article_id": "ordinal", "published_ts": ts("Sep 22nd 2021")},
    ]
    assert _sorted(entries) == ["newest", "us_2025", "ordinal", "old_iso"]


def test_undated_articles_sink_below_dated_ones():
    entries = [
        {"article_id": "undated", "published_ts": None},
        {"article_id": "ancient", "published_ts": ts("2014-01-29")},
    ]
    assert _sorted(entries) == ["ancient", "undated"]


def test_undated_articles_fall_back_to_fetched_at():
    # No published_at at all — order by when we crawled it, newest
    # first, rather than by article_id.
    entries = [
        {"article_id": "art_001", "published_ts": None,
         "fetched_at": "2026-01-02T00:00:00+00:00"},
        {"article_id": "art_002", "published_ts": None,
         "fetched_at": "2026-06-30T00:00:00+00:00"},
    ]
    assert _sorted(entries) == ["art_002", "art_001"]


def test_sort_key_tolerates_missing_fields():
    # Per-agency records are built from a narrower dict; nothing should
    # raise if a key is absent.
    assert _sorted([{"article_id": "a"}, {"article_id": "b"}]) == ["b", "a"]
