"""Tests for detect_state in build_agency_registry.

The function must extract the correct US state code from a Flock-style
agency display name. The hard case is when the place name itself
contains a state-name word (e.g. "City of California, Missouri" where
the explicit 2-letter code "MO" must beat the substring "California").
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from build_agency_registry import detect_state


@pytest.mark.parametrize("name,expected", [
    # The bug: a Missouri agency whose place name is "California" must
    # resolve to MO, not CA.
    ("City of California MO PD", "MO"),
    # Other place names that collide with state-name words.
    ("California MD PD", "MD"),
    ("Indiana PA PD", "PA"),
    ("Nevada IA PD", "IA"),
    ("Mississippi MI PD", "MI"),
    # Canonical California cases must keep working.
    ("Pacifica CA PD", "CA"),
    ("Los Angeles CA PD", "CA"),
    # Word-form California identifiers fall back to CA when no explicit
    # 2-letter code is present.
    ("Cal Fire", "CA"),
    ("Cal Poly Pomona", "CA"),
    ("Cal State Fullerton", "CA"),
    ("NCRIC", "CA"),
    # DC and edge cases.
    ("Washington DC MPD", "DC"),
])
def test_detect_state(name, expected):
    assert detect_state(name) == expected


def test_detect_state_no_match():
    assert detect_state("Some Agency With No State Hint") is None
