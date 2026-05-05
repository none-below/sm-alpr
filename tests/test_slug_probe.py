"""Tests for the slug probe candidate generator.

The generator is the load-bearing part: if it doesn't produce the right
spellings, we can't find the portal. These tests pin the known quirks
(leading dash, collapsed state suffix, dehyphenation) against real
registry agencies.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from slug_probe import (
    allocate_tier_budget,
    extract_hints,
    eyesonflock_hint,
    generate_candidates,
    normalize_name,
    select_targets,
)


def test_normalize_drops_parens():
    assert normalize_name("Town of Woodside CA (SMCSO)") == "town-of-woodside-ca"
    assert normalize_name("El Cajon CA PD") == "el-cajon-ca-pd"


def test_normalize_handles_apostrophe():
    assert normalize_name("Sheriff's Office") == "sheriffs-office"


def test_hints_strip_prefix_suffix():
    h = extract_hints("Town of Woodside CA (SMCSO)")
    assert h["base"] == "woodside"
    assert h["state"] == "ca"
    assert "town-of-" in h["prefixes"]


def test_hints_role_from_name():
    h = extract_hints("El Cajon CA PD")
    assert h["base"] == "el-cajon"
    assert h["state"] == "ca"
    assert h["role"] == "police"


def test_hints_sheriff_role():
    h = extract_hints("Mendocino County CA SO")
    assert h["base"] == "mendocino-county"
    assert h["role"] == "sheriff"


def test_hints_role_before_state_in_name():
    # "Shafter PD CA" — role appears BEFORE state, both must strip
    h = extract_hints("Shafter PD CA")
    assert h["base"] == "shafter"
    assert h["state"] == "ca"
    assert h["role"] == "police"


def test_hints_respect_entry_role_over_name():
    # agency_role on the registry entry is authoritative
    h = extract_hints("Foo Bar", state="CA", agency_role="sheriff")
    assert h["role"] == "sheriff"


def test_candidates_include_leading_dash_variant():
    # El Cajon's real Flock slug is "-el-cajon-pd-ca" — the leading dash
    # and the pd-before-state order are the quirk we most need to catch.
    entry = {
        "agency_id": "x",
        "flock_names": ["El Cajon CA PD"],
        "display_name": None,
        "geo": {"state": "CA"},
        "agency_role": "police",
        "slug": "el-cajon-ca-pd",
    }
    candidates = generate_candidates(entry)
    assert "-el-cajon-pd-ca" in candidates, (
        f"missing leading-dash+swap variant; got sample: {candidates[:10]}"
    )
    # Also the swapped-order non-dashed variant
    assert "el-cajon-pd-ca" in candidates


def test_candidates_include_collapsed_state_suffix():
    # mendocino-county-soca is the collapsed form
    entry = {
        "agency_id": "x",
        "flock_names": ["Mendocino County CA SO"],
        "display_name": None,
        "geo": {"state": "CA"},
        "agency_role": "sheriff",
        "slug": "mendocino-county-ca-so",
    }
    candidates = generate_candidates(entry)
    assert "mendocino-county-soca" in candidates, (
        f"missing collapsed state-suffix variant; got sample: {candidates[:10]}"
    )


def test_candidates_include_dehyphenated_base():
    # Compound names: foothill-deanza -> foothilldeanza
    entry = {
        "agency_id": "x",
        "flock_names": ["Foothill Deanza CA PD"],
        "display_name": None,
        "geo": {"state": "CA"},
        "agency_role": "police",
        "slug": "foothill-deanza-ca-pd",
    }
    candidates = generate_candidates(entry)
    assert "foothilldeanza-ca-pd" in candidates, (
        f"missing dehyphenated base; got sample: {candidates[:10]}"
    )


def test_candidates_include_town_of_prefix_when_observed():
    # If the name has "Town of", we should try both with and without
    entry = {
        "agency_id": "x",
        "flock_names": ["Town of Woodside CA (SMCSO)"],
        "display_name": None,
        "geo": {"state": "CA"},
        "agency_role": "police",
        "slug": "town-of-woodside-ca",
    }
    candidates = generate_candidates(entry)
    # bare (no prefix)
    assert "woodside-ca-pd" in candidates
    # with prefix
    assert "town-of-woodside-ca" in candidates
    assert "town-of-woodside-ca-pd" in candidates


def test_candidates_dedupe():
    entry = {
        "agency_id": "x",
        "flock_names": ["Alameda CA PD"],
        "display_name": None,
        "geo": {"state": "CA"},
        "agency_role": "police",
        "slug": "alameda-ca-pd",
    }
    candidates = generate_candidates(entry)
    assert len(candidates) == len(set(candidates))


def test_candidates_priority_default_first():
    # Default pattern {base}-{state}-pd should be the very first candidate
    entry = {
        "agency_id": "x",
        "flock_names": ["Alhambra CA PD"],
        "display_name": None,
        "geo": {"state": "CA"},
        "agency_role": "police",
        "slug": "alhambra-ca-pd",
    }
    candidates = generate_candidates(entry)
    assert candidates[0] == "alhambra-ca-pd"


# ── select_targets tier split ───────────────────────────────────


def _entry(aid, **overrides):
    """Helper for building a minimal registry entry for tier tests."""
    e = {
        "agency_id": aid,
        "flock_names": ["Alameda CA PD"],
        "display_name": None,
        "flock_active_slug": "alameda-ca-pd",
        "flock_slugs": ["alameda-ca-pd"],
        "agency_role": "police",
        "geo": {"state": "CA"},
    }
    e.update(overrides)
    return e


def test_select_targets_tier_a_picks_null_active_with_name():
    """Tier A is registry entries with no confirmed slug yet — these are
    peer-outbound discoveries that probe should try first because their
    single-try hit rate from name_to_slug-style guesses is high."""
    registry = [
        _entry("x", flock_active_slug=None, flock_slugs=[]),
    ]
    tier_a, tier_b = select_targets(registry, failed_slugs={}, state={})
    assert [e["agency_id"] for e in tier_a] == ["x"]
    assert tier_b == []


def test_select_targets_tier_a_skips_entries_with_no_name_signal():
    """Without flock_names or display_name, generate_candidates can't
    derive variants — there's nothing for probe to work with, so the
    entry should be skipped at selection."""
    registry = [
        _entry("x", flock_active_slug=None, flock_names=[], display_name=None),
    ]
    tier_a, tier_b = select_targets(registry, failed_slugs={}, state={})
    assert tier_a == []
    assert tier_b == []


def test_select_targets_tier_b_picks_active_in_failed_slugs():
    """Existing behavior, now tier B: registry entries whose
    flock_active_slug is in failed_slugs.json get variant-searched."""
    registry = [_entry("x", flock_active_slug="alameda-ca-pd")]
    tier_a, tier_b = select_targets(
        registry, failed_slugs={"alameda-ca-pd": {"reason": "404"}}, state={}
    )
    assert tier_a == []
    assert [e["agency_id"] for e in tier_b] == ["x"]


def test_select_targets_skips_active_set_and_not_failed():
    """If active is set and not in failed_slugs, the main crawler handles
    refresh — probe stays out of the way to preserve its rate budget."""
    registry = [_entry("x", flock_active_slug="alameda-ca-pd")]
    tier_a, tier_b = select_targets(registry, failed_slugs={}, state={})
    assert tier_a == []
    assert tier_b == []


def test_select_targets_skips_already_found():
    """state.found means probe has already resolved this agency; don't
    re-probe."""
    registry = [_entry("x", flock_active_slug=None)]
    state = {"agencies": {"x": {"found": "alameda-ca-pd"}}}
    tier_a, tier_b = select_targets(registry, failed_slugs={}, state=state)
    assert tier_a == [] and tier_b == []


def test_select_targets_skips_exhausted():
    """Agencies whose entire candidate space was tried get marked
    exhausted; selection skips them so the limited probe budget goes
    to agencies still in play."""
    registry = [_entry("x", flock_active_slug=None)]
    state = {"agencies": {"x": {"exhausted": True}}}
    tier_a, tier_b = select_targets(registry, failed_slugs={}, state=state)
    assert tier_a == [] and tier_b == []


def test_select_targets_only_agency_filter_applies_to_both_tiers():
    registry = [
        _entry("a", flock_active_slug=None),
        _entry("b", flock_active_slug="alameda-ca-pd"),
    ]
    failed = {"alameda-ca-pd": {"reason": "404"}}
    tier_a, tier_b = select_targets(registry, failed, state={}, only_agency="a")
    assert [e["agency_id"] for e in tier_a] == ["a"]
    assert tier_b == []
    tier_a, tier_b = select_targets(registry, failed, state={}, only_agency="b")
    assert tier_a == []
    assert [e["agency_id"] for e in tier_b] == ["b"]


# ── allocate_tier_budget ────────────────────────────────────────


def test_allocate_tier_budget_default_split_at_three():
    """At the workflow's hourly limit of 3 probes, the default split is
    2 + 1 — biased toward tier A because its single-try hit rate is
    meaningfully higher than tier B's variant-search hit rate."""
    assert allocate_tier_budget(3, tier_a_count=10, tier_b_count=10) == (2, 1)


def test_allocate_tier_budget_full_to_a_when_b_empty():
    """No tier B work → full budget to tier A. Don't waste slots."""
    assert allocate_tier_budget(3, tier_a_count=5, tier_b_count=0) == (3, 0)


def test_allocate_tier_budget_full_to_b_when_a_empty():
    """No tier A work → full budget to tier B (the original behavior
    before tiers existed)."""
    assert allocate_tier_budget(3, tier_a_count=0, tier_b_count=5) == (0, 3)


def test_allocate_tier_budget_zero_when_both_empty():
    assert allocate_tier_budget(3, tier_a_count=0, tier_b_count=0) == (0, 0)


def test_allocate_tier_budget_minimum_one_for_b_when_present():
    """Even at limit=1 with both tiers populated, tier B still gets a
    slot — otherwise a heavily-populated tier A could starve tier B
    indefinitely. The asymmetry is acceptable: 1 of the 3 hourly slots
    on slogging is the explicit design choice."""
    assert allocate_tier_budget(1, tier_a_count=10, tier_b_count=10) == (0, 1)


def test_allocate_tier_budget_scales_with_limit():
    """At higher limits the same 1/3 share holds (rounded down)."""
    a, b = allocate_tier_budget(10, tier_a_count=10, tier_b_count=10)
    assert (a, b) == (7, 3)
    assert a + b == 10


def test_allocate_tier_budget_custom_share():
    """Callers can override the default share — e.g. crank tier B up
    when tier A is mostly exhausted and we want to drain the slog list."""
    assert allocate_tier_budget(
        3, tier_a_count=10, tier_b_count=10, tier_b_share=0.5
    ) == (2, 1)
    assert allocate_tier_budget(
        4, tier_a_count=10, tier_b_count=10, tier_b_share=0.5
    ) == (2, 2)


# ── eyesonflock_hint ────────────────────────────────────────────


def test_eyesonflock_hint_matches_place_police_to_pd():
    """A registry entry with kind=place + role=police must look up
    against eyesonflock's PD records (city-keyed)."""
    entry = {
        "agency_id": "x",
        "agency_role": "police",
        "geo": {"kind": "place", "name": "Alameda", "state": "CA"},
    }
    eof_index = {("alameda", "CA", "PD"): "alameda-ca-pd"}
    assert eyesonflock_hint(entry, eof_index) == "alameda-ca-pd"


def test_eyesonflock_hint_matches_county_sheriff_to_sd():
    """kind=county + role=sheriff must look up against eyesonflock's SD
    records (county-keyed). Distinct from PD lookup so 'Alameda' the
    city and 'Alameda' the county don't collide."""
    entry = {
        "agency_id": "x",
        "agency_role": "sheriff",
        "geo": {"kind": "county", "name": "Alameda", "state": "CA"},
    }
    eof_index = {("alameda", "CA", "SD"): "alameda-county-ca-so"}
    assert eyesonflock_hint(entry, eof_index) == "alameda-county-ca-so"


def test_eyesonflock_hint_returns_none_for_other_kinds():
    """Conservative: only place/police and county/sheriff get matched.
    cousub township police, ambiguous boundaries, manual coordinates,
    state-level agencies — no eyesonflock counterpart, don't try to
    match. Wrong-match cost (poisoned slug promoted to registry) is
    higher than no-match cost (fall through to variant generation)."""
    eof_index = {("brownstown", "MI", "PD"): "should-not-match"}
    for kind in ("cousub", "manual", "state-only", "state", "ambiguous"):
        entry = {
            "agency_id": "x",
            "agency_role": "police",
            "geo": {"kind": kind, "name": "Brownstown", "state": "MI"},
        }
        assert eyesonflock_hint(entry, eof_index) is None, (
            f"kind={kind} should not match eyesonflock"
        )


def test_eyesonflock_hint_role_kind_must_align():
    """A place with sheriff role, or a county with police role — neither
    should match. The mismatch is suspicious enough to skip rather than
    cross-match against the wrong eyesonflock type."""
    eof_index = {
        ("alameda", "CA", "PD"): "alameda-ca-pd",
        ("alameda", "CA", "SD"): "alameda-county-ca-so",
    }
    place_sheriff = {
        "agency_id": "x",
        "agency_role": "sheriff",
        "geo": {"kind": "place", "name": "Alameda", "state": "CA"},
    }
    county_police = {
        "agency_id": "x",
        "agency_role": "police",
        "geo": {"kind": "county", "name": "Alameda", "state": "CA"},
    }
    assert eyesonflock_hint(place_sheriff, eof_index) is None
    assert eyesonflock_hint(county_police, eof_index) is None


def test_eyesonflock_hint_returns_none_when_index_empty():
    """Empty eof_index (e.g. lookup failed) is treated as 'no hint
    available' — slug_probe falls through to variant generation. The
    lookup is a hint, not a dependency."""
    entry = {
        "agency_id": "x",
        "agency_role": "police",
        "geo": {"kind": "place", "name": "Alameda", "state": "CA"},
    }
    assert eyesonflock_hint(entry, {}) is None
    assert eyesonflock_hint(entry, None) is None


def test_eyesonflock_hint_returns_none_without_geo_block():
    """Entries without geo data (some federal/test entries) can't be
    matched geographically and must skip the lookup."""
    eof_index = {("alameda", "CA", "PD"): "alameda-ca-pd"}
    assert eyesonflock_hint({"agency_role": "police", "geo": None}, eof_index) is None
    assert eyesonflock_hint({"agency_role": "police"}, eof_index) is None
    # Partial geo (no name) also doesn't match
    assert eyesonflock_hint(
        {"agency_role": "police", "geo": {"kind": "place", "state": "CA"}},
        eof_index,
    ) is None
