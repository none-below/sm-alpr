"""Pin _extract_crawled_name behavior for the variants we've seen in the wild.

Regression cases:
  - San Rafael wraps its overview in quotation marks, leaving a leading "
    that survives a plain .strip()
  - Mill Valley uses "employs" instead of "uses"
  - Oakland uses "utilizes"
  - Napa PD describes the product as "Automatic License Plate Reader
    technology" rather than naming Flock Safety

All four had crawled_name == null before this change. Plus the negative
cases: NCRIC has no boilerplate marker and should stay null; an overview
that mentions Flock Safety but with an unrecognized verb should raise.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from flock_transparency import _extract_crawled_name


def _extract(overview):
    return _extract_crawled_name(overview, slug="test-slug", datestamp="2026-05-12")


def test_canonical_uses_flock_safety_technology():
    assert _extract(
        "San Mateo CA PD uses Flock Safety technology to capture objective evidence..."
    ) == "San Mateo CA PD"


def test_uses_flock_safety_operating_system():
    """Newer Flock boilerplate (2026-05+) renames Technology → Operating System."""
    assert _extract(
        "The Pacifica Police Department uses Flock Safety's Operating System to capture..."
    ) == "The Pacifica Police Department"


def test_employs_variant():
    """Mill Valley uses 'employs' instead of 'uses'."""
    assert _extract(
        "The Mill Valley Police Department employs Flock Safety technology to capture..."
    ) == "The Mill Valley Police Department"


def test_utilizes_variant():
    """Oakland uses 'utilizes' instead of 'uses'."""
    assert _extract(
        "The Oakland Police Department utilizes Flock Safety technology to capture..."
    ) == "The Oakland Police Department"


def test_alpr_phrasing_without_flock_brand():
    """Napa PD describes the product generically as 'Automatic License
    Plate Reader technology' — no 'Flock Safety' string at all."""
    assert _extract(
        "Napa Police Department uses Automatic License Plate Reader "
        "technology to capture objective evidence..."
    ) == "Napa Police Department"


def test_leading_ascii_quote_stripped():
    """San Rafael's overview opens with a leading `"` that survives
    plain .strip(). Result must be the bare agency name."""
    assert _extract(
        '"San Rafael Police Department uses Flock Safety technology..."'
    ) == "San Rafael Police Department"


def test_leading_smart_quote_stripped():
    """Defensive: same logic should handle curly quotes too."""
    assert _extract(
        "“Foo PD uses Flock Safety technology..."
    ) == "Foo PD"


def test_apostrophe_in_name_preserved():
    """Apostrophes mid-name (sheriff's office) must not be stripped."""
    assert _extract(
        "The Napa County Sheriff's Office uses Flock Safety's Operating System..."
    ) == "The Napa County Sheriff's Office"


def test_empty_overview_returns_none():
    assert _extract("") is None


def test_overview_with_no_marker_and_no_flock_safety_returns_none():
    """NCRIC writes a custom intro that never mentions Flock Safety —
    silently leave crawled_name=None rather than guess."""
    assert _extract(
        "***draft version*** The Northern California Regional Intelligence "
        "Center (NCRIC) is a multi-jurisdiction government program..."
    ) is None


def test_overview_with_flock_safety_but_no_marker_raises():
    """Surface a loud failure if Flock rephrases the boilerplate in a
    way we don't anticipate — silently dropping every agency's name
    would be much worse than a CI break."""
    with pytest.raises(ValueError, match="agency-name marker"):
        _extract(
            "The Townsville PD operates Flock Safety cameras across the city."
        )


def test_dynamic_lpr_modifier():
    """Older boilerplate inserts 'LPR' before 'Technology' on some
    portals: '...uses Flock Safety's LPR Technology...'"""
    assert _extract(
        "Springfield PD uses Flock Safety's LPR Technology to capture..."
    ) == "Springfield PD"
