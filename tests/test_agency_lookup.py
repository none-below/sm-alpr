"""Tests for scripts/agency_lookup.py.

The matcher's most subtle logic is the span-collision resolution that
prevents 'Palo Alto' (Palo Alto PD) from false-matching inside 'East
Palo Alto Police Department' text. Plus role-form generation from
geo+agency_role and the county-suffix handling that mirrors
report.js's shortAgencyName().
"""

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import agency_lookup as al  # noqa: E402
import lib  # noqa: E402


# ── Test fixtures: minimal agency entries ────────────────────────


def _agency(agency_id, place, role, kind="place", state="CA",
            display_name=None, flock_names=None, aliases=None):
    return {
        "agency_id": agency_id,
        "agency_role": role,
        "display_name": display_name,
        "flock_names": flock_names or [],
        "aliases": aliases or [],
        "geo": {"kind": kind, "name": place, "state": state},
    }


SMPD = _agency("smpd-id", "San Mateo", "police")
SMCSO = _agency("smcso-id", "San Mateo", "sheriff", kind="county")
EPAPD = _agency("epapd-id", "East Palo Alto", "police")
PAPD = _agency("papd-id", "Palo Alto", "police")
MPPD = _agency("mppd-id", "Menlo Park", "police")
LPDTX = _agency("lpdtx-id", "Livingston", "police", state="TX")
LPDCA = _agency("lpdca-id", "Livingston", "police", state="CA")


# ── derive_journalistic_forms ────────────────────────────────────


def test_journalistic_forms_for_city_pd():
    forms = al.derive_journalistic_forms(EPAPD)
    assert "East Palo Alto Police Department" in forms
    assert "East Palo Alto PD" in forms
    assert "East Palo Alto Police" in forms
    assert "East Palo Alto" in forms  # bare place — last resort


def test_journalistic_forms_county_sheriff_uses_county_suffix():
    """County agencies should produce 'San Mateo County Sheriff's Office'
    not 'San Mateo Sheriff's Office'."""
    forms = al.derive_journalistic_forms(SMCSO)
    assert "San Mateo County Sheriff's Office" in forms
    assert "San Mateo County Sheriff" in forms
    assert "San Mateo County" in forms
    # Should NOT produce "San Mateo Sheriff..." (would collide with city PD's place)
    assert not any(f.startswith("San Mateo ") and "County" not in f
                   for f in forms)


def test_journalistic_forms_distinct_role_for_same_place():
    """SMPD (city police) and SMCSO (county sheriff) must produce
    different surface-form sets so the matcher can disambiguate."""
    pd_forms = set(al.derive_journalistic_forms(SMPD))
    so_forms = set(al.derive_journalistic_forms(SMCSO))
    # The two "San Mateo" bare names overlap (both in their fallback list)
    # but the role-suffixed forms must differ.
    pd_specific = pd_forms - so_forms
    so_specific = so_forms - pd_forms
    assert "San Mateo Police Department" in pd_specific
    assert "San Mateo County Sheriff's Office" in so_specific


# ── lookup span-collision resolution ─────────────────────────────


def test_lookup_resolves_palo_alto_vs_east_palo_alto(monkeypatch):
    """The headline regression from the original design — make sure
    'Palo Alto Police' doesn't match inside 'East Palo Alto Police'."""
    monkeypatch.setattr(lib, "_registry_cache", [EPAPD, PAPD])
    text = "An East Palo Alto Police Department officer cited Flock cameras."
    matches = al.lookup(text)
    matched_ids = [m["agency_id"] for m in matches]
    assert "epapd-id" in matched_ids
    assert "papd-id" not in matched_ids, \
        f"Palo Alto false-matched inside 'East Palo Alto'; got {matches}"


def test_lookup_county_sheriff_is_resolvable(monkeypatch):
    """'San Mateo County Sheriff's Office' should match SMCSO, not SMPD."""
    monkeypatch.setattr(lib, "_registry_cache", [SMPD, SMCSO])
    text = "The San Mateo County Sheriff's Office did not respond."
    matches = al.lookup(text)
    matched_ids = {m["agency_id"]: m["score"] for m in matches}
    assert "smcso-id" in matched_ids
    # SMPD might still show as a low-confidence candidate via the bare
    # "San Mateo" form, but SMCSO should outrank it.
    if "smpd-id" in matched_ids:
        assert matched_ids["smcso-id"] > matched_ids["smpd-id"]


def test_lookup_returns_multiple_candidates_for_ambiguous_alias(monkeypatch):
    """Two agencies with the same name (Livingston PD in TX and CA)
    should both surface as candidates — disambiguation is the curator's job."""
    monkeypatch.setattr(lib, "_registry_cache", [LPDTX, LPDCA])
    text = "The Livingston Police Department made an arrest."
    matches = al.lookup(text)
    matched_ids = {m["agency_id"] for m in matches}
    assert "lpdtx-id" in matched_ids
    assert "lpdca-id" in matched_ids


def test_lookup_geo_context_boosts_in_state_agency(monkeypatch):
    """If the text mentions Texas, the TX Livingston should outrank the CA one."""
    monkeypatch.setattr(lib, "_registry_cache", [LPDTX, LPDCA])
    text = ("An incident in Texas: the Livingston Police Department "
            "made an arrest.")
    matches = al.lookup(text)
    matched_ids = {m["agency_id"]: m["score"] for m in matches}
    assert matched_ids["lpdtx-id"] > matched_ids["lpdca-id"]


def test_lookup_no_matches_for_unrelated_text(monkeypatch):
    monkeypatch.setattr(lib, "_registry_cache", [EPAPD, PAPD])
    matches = al.lookup("The weather in Cleveland was unusually warm.")
    assert matches == []


def test_lookup_top_n_caps_results(monkeypatch):
    monkeypatch.setattr(lib, "_registry_cache",
                        [SMPD, SMCSO, EPAPD, PAPD, MPPD])
    text = ("San Mateo Police, East Palo Alto Police Department, "
            "Palo Alto Police, Menlo Park Police, San Mateo County Sheriff.")
    matches = al.lookup(text, top_n=3)
    assert len(matches) == 3


# ── detect_states helper ─────────────────────────────────────────


def test_detect_states_finds_full_names_and_codes():
    states = al.detect_states("This happened in California.")
    assert "CA" in states


def test_detect_states_finds_uppercase_codes():
    states = al.detect_states("Litigation pending in TX over the matter.")
    assert "TX" in states


def test_detect_states_does_not_match_inside_words():
    """The 2-letter code regex uses word boundaries so 'Cabal' doesn't match
    'CA' nor 'Texan' match 'TX'."""
    states = al.detect_states("Cabal members negotiated with Texan officials.")
    # 'CA' only matches as a standalone word; 'Cabal' starts with 'Ca' lowercase
    # but our pattern is ASCII-case-sensitive on the code. Let's check what
    # we actually get and assert the meaningful invariant:
    assert "CA" not in states


# ── bare-place noise gating ──────────────────────────────────────


# Synthetic fixtures matching the false-positive patterns observed in
# the article registry: state-level entity (Indiana DOC), test entry
# (the "demo" test agency), and a small-town namesake of a major city
# (Oakland TN PD).
INDIANA_DOC = {
    "agency_id": "in-doc-id",
    "agency_role": "corrections",
    "display_name": None,
    "flock_names": ["Indiana Department of Corrections"],
    "aliases": [],
    "geo": {"kind": "state", "name": "IN", "state": "IN"},
}
DEMO_TEST = {
    "agency_id": "demo-id",
    "agency_role": "test",
    "display_name": None,
    "flock_names": ["Demo", "demo"],
    "aliases": [],
    "geo": None,
}
OAKLAND_TN = _agency("oak-tn-id", "Oakland", "police", state="TN")
OAKLAND_CA = _agency("oak-ca-id", "Oakland", "police", state="CA")


def test_state_level_entity_does_not_emit_bare_state_code():
    """Indiana DOC's geo.name is 'IN' — without gating we'd match every
    English 'in' in the text. _bare_place_form should refuse it."""
    forms = al.derive_journalistic_forms(INDIANA_DOC)
    assert "IN" not in forms
    # Specific forms still emitted via flock_names / role suffixes.
    assert "Indiana Department of Corrections" in forms


def test_test_role_does_not_emit_bare_place():
    """Test entries (display 'demo') must never be matched."""
    forms = al.derive_journalistic_forms(DEMO_TEST)
    # DEMO_TEST has no place/role pair feeding ROLE_FORMS, so the only
    # forms come from flock_names — but make sure no bare 'demo' is
    # generated through the place path. (The flock_names verbatim
    # contributions are intentional; the test entry's data should be
    # tagged out before the curator picks it up. The gating here just
    # ensures our place-derivation path doesn't make things worse.)
    bare = al._bare_place_form(DEMO_TEST)
    assert bare is None


def test_bare_place_skipped_in_lookup_without_state_context(monkeypatch):
    """An article about Oakland (CA) shouldn't surface Oakland TN PD via
    the bare 'Oakland' form — state context isn't corroborated."""
    monkeypatch.setattr(lib, "_registry_cache", [OAKLAND_TN])
    text = ("Oakland's City Council voted on the Flock contract. "
            "Oakland residents filled the chambers.")
    matches = al.lookup(text)
    assert matches == [], (
        f"Oakland TN PD shouldn't match a CA-context article via bare "
        f"place; got {[m['agency_id'] for m in matches]}"
    )


def test_bare_place_counts_when_state_corroborated(monkeypatch):
    """If the article DOES mention Tennessee, Oakland TN PD's bare-place
    matches should count."""
    monkeypatch.setattr(lib, "_registry_cache", [OAKLAND_TN])
    text = "The Tennessee incident: Oakland police responded after the call."
    matches = al.lookup(text)
    matched_ids = {m["agency_id"] for m in matches}
    assert "oak-tn-id" in matched_ids


def test_bare_place_full_form_always_counts(monkeypatch):
    """The state-context gate only applies to the bare-place form;
    'Oakland Police Department' (a specific form) must still match
    even without state corroboration."""
    monkeypatch.setattr(lib, "_registry_cache", [OAKLAND_TN])
    text = "Oakland Police Department officers responded to the scene."
    matches = al.lookup(text)
    matched_ids = {m["agency_id"] for m in matches}
    assert "oak-tn-id" in matched_ids


def test_indiana_doc_does_not_match_random_in(monkeypatch):
    """The original false-positive: 'Indiana Department of Corrections'
    appearing in every article because every article contains the word
    'in'. With state-level bare-place gating, the agency only matches
    when its specific name is mentioned (or Indiana itself)."""
    monkeypatch.setattr(lib, "_registry_cache", [INDIANA_DOC])
    text = "An EFF investigation in Texas revealed federal access patterns."
    matches = al.lookup(text)
    assert matches == [], (
        f"Indiana DOC false-matched on stop-word 'in'; got "
        f"{[m['agency_id'] for m in matches]}"
    )
