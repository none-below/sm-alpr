"""Verify that cached geo fields in the registry match the Census gazetteer.

For each entry with kind in (place, county, state), cross-reference the
FIPS code against the gazetteer and confirm name, state, lat, and lng
are still accurate. Drift means either the registry or gazetteer has
changed and they need to be resynced.

Kind == "manual" entries are not validated (hand-curated locations
like university campuses or agency HQs don't match FIPS centroids).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from lib import load_registry, agency_display_name
from gazetteer import lookup_by_place_fips, lookup_by_county_fips, _load_cousubs, lookup_state

COORD_TOLERANCE = 0.0001  # ~10 meters


class TestGeoCache:
    """Verify cached geo fields match the Census gazetteer."""

    @pytest.fixture(autouse=True)
    def load(self):
        self.registry = load_registry()
        self.fips_entries = [
            e for e in self.registry
            if e.get("geo") and e["geo"].get("kind") in ("place", "county")
        ]

    def test_has_fips_entries(self):
        """Sanity check: we should have a bunch of FIPS-coded entries."""
        assert len(self.fips_entries) > 500

    def test_place_fips_valid(self):
        """Every kind=place entry has a 7-digit FIPS code resolvable in the gazetteer."""
        for e in self.registry:
            geo = e.get("geo") or {}
            if geo.get("kind") != "place":
                continue
            fips = geo.get("fips")
            name = agency_display_name(e)
            assert fips, f"{name}: kind=place but no fips"
            assert len(fips) == 7, f"{name}: place fips {fips!r} is not 7 digits"
            place = lookup_by_place_fips(fips)
            assert place, f"{name}: place fips {fips} not found in gazetteer"

    def test_county_fips_valid(self):
        """Every kind=county entry has a 5-digit FIPS code resolvable in the gazetteer."""
        for e in self.registry:
            geo = e.get("geo") or {}
            if geo.get("kind") != "county":
                continue
            fips = geo.get("fips")
            name = agency_display_name(e)
            assert fips, f"{name}: kind=county but no fips"
            assert len(fips) == 5, f"{name}: county fips {fips!r} is not 5 digits"
            county = lookup_by_county_fips(fips)
            assert county, f"{name}: county fips {fips} not found in gazetteer"

    def test_place_cached_fields_match(self):
        """Cached name, state, lat, lng must match the gazetteer for kind=place."""
        mismatches = []
        for e in self.registry:
            geo = e.get("geo") or {}
            if geo.get("kind") != "place":
                continue
            fips = geo.get("fips")
            place = lookup_by_place_fips(fips)
            if not place:
                continue
            if geo.get("state") != place["state"]:
                mismatches.append(f"{fips}: state {geo.get('state')!r} != {place['state']!r}")
            if geo.get("name") != place["name"]:
                mismatches.append(f"{fips}: name {geo.get('name')!r} != {place['name']!r}")
            if abs(geo.get("lat", 0) - place["lat"]) > COORD_TOLERANCE:
                mismatches.append(f"{fips}: lat {geo.get('lat')} != {place['lat']}")
            if abs(geo.get("lng", 0) - place["lng"]) > COORD_TOLERANCE:
                mismatches.append(f"{fips}: lng {geo.get('lng')} != {place['lng']}")
        assert not mismatches, (
            "Registry geo cache out of sync with gazetteer:\n"
            + "\n".join(mismatches[:20])
            + (f"\n... and {len(mismatches) - 20} more" if len(mismatches) > 20 else "")
        )

    def test_county_cached_fields_match(self):
        """Cached name, state, lat, lng must match the gazetteer for kind=county."""
        mismatches = []
        for e in self.registry:
            geo = e.get("geo") or {}
            if geo.get("kind") != "county":
                continue
            fips = geo.get("fips")
            county = lookup_by_county_fips(fips)
            if not county:
                continue
            if geo.get("state") != county["state"]:
                mismatches.append(f"{fips}: state {geo.get('state')!r} != {county['state']!r}")
            if geo.get("name") != county["bare_name"]:
                mismatches.append(f"{fips}: name {geo.get('name')!r} != {county['bare_name']!r}")
            if abs(geo.get("lat", 0) - county["lat"]) > COORD_TOLERANCE:
                mismatches.append(f"{fips}: lat {geo.get('lat')} != {county['lat']}")
            if abs(geo.get("lng", 0) - county["lng"]) > COORD_TOLERANCE:
                mismatches.append(f"{fips}: lng {geo.get('lng')} != {county['lng']}")
        assert not mismatches, (
            "Registry geo cache out of sync with gazetteer:\n"
            + "\n".join(mismatches[:20])
            + (f"\n... and {len(mismatches) - 20} more" if len(mismatches) > 20 else "")
        )

    def test_state_consistent_between_top_level_and_geo(self):
        """If both top-level state and geo.state exist, they must match."""
        for e in self.registry:
            geo = e.get("geo") or {}
            top_state = e.get("state")
            geo_state = geo.get("state")
            if top_state and geo_state and top_state != geo_state:
                assert False, f"{e['slug']}: top state {top_state!r} != geo state {geo_state!r}"

    def test_kind_valid(self):
        """geo.kind must be one of the known values."""
        valid = {"place", "county", "cousub", "state", "state-only",
                 "ambiguous", "manual"}
        for e in self.registry:
            geo = e.get("geo")
            if not geo:
                continue
            assert geo.get("kind") in valid, f"{e['slug']}: invalid geo kind {geo.get('kind')!r}"

    def test_state_fips_valid(self):
        """Every kind=state entry has a 2-digit FIPS matching the state centroid."""
        mismatches = []
        for e in self.registry:
            geo = e.get("geo") or {}
            if geo.get("kind") != "state":
                continue
            fips = geo.get("fips")
            state = geo.get("state")
            assert fips, f"{e['slug']}: kind=state but no fips"
            assert len(fips) == 2, f"{e['slug']}: state fips {fips!r} is not 2 digits"
            assert state, f"{e['slug']}: kind=state but no state"
            s = lookup_state(state)
            assert s, f"{e['slug']}: could not compute state centroid for {state}"
            if s["state_fips"] != fips:
                mismatches.append(f"{e['slug']}: fips {fips} != expected {s['state_fips']}")
            if abs(geo.get("lat", 0) - s["lat"]) > COORD_TOLERANCE:
                mismatches.append(f"{e['slug']}: lat {geo.get('lat')} != {s['lat']}")
            if abs(geo.get("lng", 0) - s["lng"]) > COORD_TOLERANCE:
                mismatches.append(f"{e['slug']}: lng {geo.get('lng')} != {s['lng']}")
        assert not mismatches, "State-kind cache out of sync:\n" + "\n".join(mismatches[:20])

    def test_cousub_fips_valid(self):
        """Every kind=cousub entry has a valid 10-digit FIPS."""
        cousubs_by_fips = {c["fips"]: c for c in _load_cousubs()}
        mismatches = []
        for e in self.registry:
            geo = e.get("geo") or {}
            if geo.get("kind") != "cousub":
                continue
            fips = geo.get("fips")
            assert fips, f"{e['slug']}: kind=cousub but no fips"
            assert len(fips) == 10, f"{e['slug']}: cousub fips {fips!r} is not 10 digits"
            cousub = cousubs_by_fips.get(fips)
            assert cousub, f"{e['slug']}: cousub fips {fips} not found in gazetteer"
            if geo.get("state") != cousub["state"]:
                mismatches.append(f"{fips}: state mismatch")
            if geo.get("name") != cousub["bare_name"]:
                mismatches.append(f"{fips}: name {geo.get('name')!r} != {cousub['bare_name']!r}")
            if abs(geo.get("lat", 0) - cousub["lat"]) > COORD_TOLERANCE:
                mismatches.append(f"{fips}: lat mismatch")
            if abs(geo.get("lng", 0) - cousub["lng"]) > COORD_TOLERANCE:
                mismatches.append(f"{fips}: lng mismatch")
        assert not mismatches, "Cousub cache out of sync:\n" + "\n".join(mismatches[:20])
