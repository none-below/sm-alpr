"""Regression tests for the "CO" state code vs "Co" County abbreviation
ambiguity in geocode_agencies.

Two distinct sites of confusion existed:

1. `_ABBREV_CO` expanded any standalone "Co"/"CO" to "County", which
   wrongly turned "Aurora CO PD" into "Aurora County PD".

2. `detect_county_co_state` matched "X CO YY" and returned YY as the
   actual state when CO was the County abbrev — but it accepted role
   suffixes (PD/SO/...) as YY, so "Aurora CO PD" returned "PD" and
   overrode the entry's state.

Both cases now disambiguate by position: "Co" expands to "County" only
when followed by *both* a 2-letter token and at least one more token,
and the state-override only fires when the trailing 2-letter token
isn't a known role suffix.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from geocode_agencies import (
    detect_county_co_state,
    extract_county_candidate,
    extract_place_candidate,
)


class TestColoradoStateCode:
    """'CO' as Colorado state code must not be expanded to 'County'."""

    def test_colorado_city_pd_extracts_city(self):
        assert extract_place_candidate("Aurora CO PD", "CO") == "Aurora"
        assert extract_place_candidate("Avon CO PD", "CO") == "Avon"
        assert extract_place_candidate("Brighton CO PD", "CO") == "Brighton"

    def test_colorado_county_so_extracts_county(self):
        assert extract_place_candidate("Arapahoe County CO SO", "CO") == "Arapahoe County"
        assert extract_place_candidate("Chaffee County CO SO", "CO") == "Chaffee County"

    def test_colorado_city_pd_does_not_match_county_extractor(self):
        # "Aurora CO PD" must not look like a county once normalized
        assert extract_county_candidate("Aurora CO PD") is None
        assert extract_county_candidate("Avon CO PD") is None

    def test_detect_county_co_state_ignores_role_suffix(self):
        # "Aurora CO PD" — CO is the state code, PD is the role; no override
        assert detect_county_co_state("Aurora CO PD") is None
        assert detect_county_co_state("Avon CO PD") is None
        # "Wagoner CO SO OK" — SO is role; override would otherwise return SO
        assert detect_county_co_state("Wagoner CO SO OK") is None


class TestCountyAbbreviation:
    """'Co'/'Co.' as County abbreviation must still expand correctly."""

    def test_co_dot_county_extracts_county(self):
        assert extract_place_candidate("Schuyler Co. IL SO", "IL") == "Schuyler County"
        assert extract_place_candidate("Bureau Co. IL SO", "IL") == "Bureau County"
        assert extract_place_candidate("Florence Co. WI SO", "WI") == "Florence County"

    def test_co_no_dot_county_extracts_county(self):
        assert extract_place_candidate("Gasconade Co MO SO", "MO") == "Gasconade County"
        assert extract_place_candidate("Morgan Co MO SO", "MO") == "Morgan County"
        assert extract_place_candidate("Curry Co NM SO", "NM") == "Curry County"

    def test_co_then_state_then_role_pattern(self):
        # "Caldwell CO TX SO" — CO=County, TX=state, SO=role
        # detect_county_co_state should return "TX" (the real state)
        assert detect_county_co_state("Caldwell CO TX SO") == "TX"
        assert detect_county_co_state("Reeves CO TX SO") == "TX"

    def test_co_followed_by_dash_suffix(self):
        # "LaSalle Co. IL SO - New" — _DASH_SUFFIX strips the trailing
        # "- New", and Co. should still expand
        assert extract_place_candidate("LaSalle Co. IL SO - New", "IL") == "LaSalle County"


class TestNonAmbiguousCases:
    """Names without 'Co'/'CO' shouldn't be affected by the disambiguation."""

    def test_california_city(self):
        assert extract_place_candidate("San Mateo CA PD", "CA") == "San Mateo"
        assert extract_place_candidate("Foster City CA PD", "CA") == "Foster City"

    def test_other_state_city(self):
        assert extract_place_candidate("Akron OH PD", "OH") == "Akron"
