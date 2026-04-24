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
    extract_hints,
    generate_candidates,
    normalize_name,
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
