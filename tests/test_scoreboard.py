#!/usr/bin/env python3
"""
Tests for the scoreboard builder.

Run with: uv run pytest tests/test_scoreboard.py
"""

import json
from pathlib import Path

import pytest

SCOREBOARD_DATA = Path("docs/data/scoreboard_data.json")


# ── Podium ranking logic (unit tests with synthetic data) ──


def build_podium(values):
    """Replicate the podium logic from build_scoreboard.py.

    Takes a descending-sorted list of (name, value) tuples and returns
    podium entries with rank assignments.
    """
    podium = []
    slots_used = 0
    prev_value = None
    for name, value in values:
        if value != prev_value:
            if len(podium) >= 3:
                break
            slots_used += 1
            prev_value = value
        if slots_used > 3:
            break
        podium.append({"rank": slots_used, "name": name, "value": value})
    return podium


class TestPodiumLogic:
    """Verify the medal assignment / tie-breaking rules."""

    def test_simple_top_3(self):
        values = [("A", 10), ("B", 8), ("C", 5)]
        podium = build_podium(values)
        assert len(podium) == 3
        assert [p["rank"] for p in podium] == [1, 2, 3]

    def test_two_way_gold_tie(self):
        values = [("A", 10), ("B", 10), ("C", 5)]
        podium = build_podium(values)
        assert len(podium) == 3
        assert [p["rank"] for p in podium] == [1, 1, 2]

    def test_three_way_gold_tie_no_silver(self):
        """3 golds already fills the podium — no lower medals."""
        values = [("A", 10), ("B", 10), ("C", 10), ("D", 5)]
        podium = build_podium(values)
        assert len(podium) == 3
        assert [p["rank"] for p in podium] == [1, 1, 1]
        assert all(p["value"] == 10 for p in podium)

    def test_five_way_gold_tie(self):
        """5-way gold: all get gold, no silver/bronze."""
        values = [("A", 10), ("B", 10), ("C", 10), ("D", 10), ("E", 10)]
        podium = build_podium(values)
        assert len(podium) == 5
        assert all(p["rank"] == 1 for p in podium)

    def test_gold_silver_bronze_tie(self):
        """1 gold, 1 silver, 3 bronze = 5 entries shown."""
        values = [("A", 10), ("B", 8), ("C", 5), ("D", 5), ("E", 5), ("F", 2)]
        podium = build_podium(values)
        assert len(podium) == 5
        assert [p["rank"] for p in podium] == [1, 2, 3, 3, 3]

    def test_two_golds_fills_then_stops(self):
        """2 golds + 2 silvers: silvers shown because tie started before 3."""
        values = [("A", 10), ("B", 10), ("C", 8), ("D", 8), ("E", 5)]
        podium = build_podium(values)
        assert len(podium) == 4
        assert [p["rank"] for p in podium] == [1, 1, 2, 2]

    def test_large_silver_tie_no_bronze(self):
        """1 gold + 10 silvers: no bronze since 11 entries > 3."""
        values = [("G", 20)] + [(f"S{i}", 15) for i in range(10)] + [("B", 5)]
        podium = build_podium(values)
        assert len(podium) == 11
        assert podium[0]["rank"] == 1
        assert all(p["rank"] == 2 for p in podium[1:])

    def test_single_entry(self):
        values = [("A", 10)]
        podium = build_podium(values)
        assert len(podium) == 1
        assert podium[0]["rank"] == 1

    def test_empty(self):
        podium = build_podium([])
        assert podium == []

    def test_two_entries_no_tie(self):
        values = [("A", 10), ("B", 5)]
        podium = build_podium(values)
        assert len(podium) == 2
        assert [p["rank"] for p in podium] == [1, 2]


# ── Integration tests against generated scoreboard_data.json ──


class TestScoreboardData:
    """Verify scoreboard_data.json structure and content."""

    @pytest.fixture(autouse=True)
    def load_data(self):
        assert SCOREBOARD_DATA.exists(), "Run build_scoreboard.py first"
        self.data = json.loads(SCOREBOARD_DATA.read_text())

    def test_has_categories(self):
        assert len(self.data["categories"]) == 10

    def test_category_ids_unique(self):
        ids = [c["id"] for c in self.data["categories"]]
        assert len(ids) == len(set(ids))

    def test_each_category_has_required_fields(self):
        for cat in self.data["categories"]:
            assert "id" in cat
            assert "title" in cat
            assert "subtitle" in cat
            assert "podium" in cat

    def test_podium_entries_have_required_fields(self):
        for cat in self.data["categories"]:
            for p in cat["podium"]:
                assert "rank" in p
                assert "name" in p
                assert "slug" in p
                assert "value" in p

    def test_ranks_are_ascending(self):
        for cat in self.data["categories"]:
            ranks = [p["rank"] for p in cat["podium"]]
            for i in range(1, len(ranks)):
                assert ranks[i] >= ranks[i - 1], f"{cat['id']}: ranks not ascending"

    def test_values_are_descending(self):
        for cat in self.data["categories"]:
            values = [p["value"] for p in cat["podium"]]
            for i in range(1, len(values)):
                assert values[i] <= values[i - 1], f"{cat['id']}: values not descending"

    def test_no_rank_beyond_3(self):
        for cat in self.data["categories"]:
            for p in cat["podium"]:
                assert p["rank"] <= 3, f"{cat['id']}: rank {p['rank']} > 3"

    def test_ties_share_rank(self):
        for cat in self.data["categories"]:
            for i in range(1, len(cat["podium"])):
                if cat["podium"][i]["value"] == cat["podium"][i - 1]["value"]:
                    assert cat["podium"][i]["rank"] == cat["podium"][i - 1]["rank"], \
                        f"{cat['id']}: tied values have different ranks"

    def test_no_new_rank_after_3_entries(self):
        """Once podium has 3+ entries, no new (higher-numbered) rank should start."""
        for cat in self.data["categories"]:
            seen_ranks = set()
            for i, p in enumerate(cat["podium"]):
                if i >= 3 and p["rank"] not in seen_ranks:
                    pytest.fail(
                        f"{cat['id']}: new rank {p['rank']} introduced at position {i} "
                        f"(already had {i} entries)"
                    )
                seen_ranks.add(p["rank"])

    def test_spicy_categories_first(self):
        ids = [c["id"] for c in self.data["categories"]]
        assert ids[:5] == ["out_of_state", "non_conforming", "indirect", "cameras", "outbound"]

    def test_all_values_positive(self):
        for cat in self.data["categories"]:
            for p in cat["podium"]:
                assert p["value"] > 0, f"{cat['id']}: {p['name']} has value {p['value']}"
