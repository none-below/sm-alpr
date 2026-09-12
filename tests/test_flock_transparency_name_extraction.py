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


def test_uninflected_verb_variant():
    """issue #698: amberley-village-oh-pd writes "The Amberley Village
    Police Department utilize Flock Safety technology" — the uninflected
    verb, agreeing with a plural reading of "Department". Stuck since
    2026-08-04 on this one missing "s".
    """
    assert _extract(
        "The Amberley Village Police Department utilize Flock Safety "
        "technology to enhance public safety while protecting individual "
        "privacy."
    ) == "The Amberley Village Police Department"


def test_spelled_out_product_phrase_variant():
    """issue #698: des-moines-wa-pd names the product line in full —
    "uses Flock Safety automated license plate reader technology" — where
    the boilerplate normally says "Flock Safety technology" or inserts the
    "LPR" acronym. Stuck since 2026-07-26.
    """
    assert _extract(
        "The Des Moines Police Department uses Flock Safety automated "
        "license plate reader technology to support criminal investigations."
    ) == "The Des Moines Police Department"
    # acronym form of the same slot
    assert _extract(
        "Springfield PD uses Flock Safety ALPR technology to capture..."
    ) == "Springfield PD"


def test_spelled_out_product_phrase_with_parenthetical_acronym():
    """issue #698: north-kingstown-ri-pd writes the product line out in
    full AND glosses it — "uses Flock Safety's Automated License Plate
    Reader (ALPR) technology". Stuck since 2026-07-17.
    """
    assert _extract(
        "The North Kingstown Police Department (NKPD) uses Flock Safety's "
        "Automated License Plate Reader (ALPR) technology as an "
        "investigative tool."
    ) == "The North Kingstown Police Department (NKPD)"


def test_curly_apostrophe_in_flock_safetys():
    """Flock renders the possessive with either a straight or a typographic
    apostrophe depending on the portal's CMS; both must match."""
    assert _extract(
        "Springfield PD uses Flock Safety’s LPR Technology to capture..."
    ) == "Springfield PD"


def test_plural_subject_does_not_capture_non_name_prefix():
    """Guard on widening the verb to its uninflected form. The marker branch
    takes *everything before* the verb as the name, so "Officers use Flock
    Safety technology ..." would capture the bare subject "Officers". A
    non-name-shaped prefix must fall through — to the "helps advance"
    template if it carries the real name, otherwise to the fail-loud raise.
    """
    with pytest.raises(ValueError, match="agency-name marker"):
        _extract(
            "Officers use Flock Safety technology to help advance public safety."
        )
    assert _extract(
        "Officers use Flock Safety technology. Flock Safety technology helps "
        "advance the Springfield Police Department's public safety mission."
    ) == "Springfield Police Department"


def test_all_caps_single_word_name_still_accepted():
    """The name-shape guard must not reject a legitimate one-word acronym
    name — requiring two words alone would break NCRIC-style agencies.
    """
    assert _extract(
        "NCRIC uses Flock Safety technology to support member agencies."
    ) == "NCRIC"
