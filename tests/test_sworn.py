"""Tests for the FBI sworn-officer estimate feature.

Covers the two pieces that feed the report's "people with access" number and
are easy to get subtly wrong:

  lib.sworn_for_oris / agency_sworn — the deduped sum across an agency's ORI
    list (umbrella agencies like CHP carry many ORIs; a shared ORI must count
    once; missing/null ORIs are "no data", not zero-counted silently).

  match_ori — the matcher guards: DA offices must never match a same-named
    city PD (FBI has no DA offices), umbrella roles grab their whole family,
    and a sub-agency qualifier (township/ISD/airport) blocks a parent-city
    collapse.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import lib  # noqa: E402
import match_ori as mo  # noqa: E402


# A fixture sworn table keyed by ORI, injected so tests don't depend on the
# live data/fbi/sworn_officers.json (which changes as the cache refreshes).
SWORN = {
    "CA0010000": {"year": 2024, "officers": 100, "civilians": 40, "total": 140},
    "CA0020000": {"year": 2024, "officers": 50, "civilians": 10, "total": 60},
    "CA0CHP001": {"year": 2024, "officers": 7000, "civilians": 3000, "total": 10000},
    "CA0CHP002": None,  # umbrella sub-unit that reports no PE series
    "CA0NULL00": None,  # in the FBI directory but no PE data
    "CA0OLD000": {"year": 2019, "officers": 30, "civilians": 5, "total": 35},  # stale
}


# ── lib.sworn_for_oris ───────────────────────────────────────────


def test_sum_and_data_counts():
    r = lib.sworn_for_oris(["CA0010000", "CA0020000"], sworn=SWORN)
    assert r["officers"] == 150
    assert r["civilians"] == 50
    assert r["total"] == 200
    assert r["oris_with_data"] == 2
    assert r["oris_no_data"] == 0


def test_dedup_counts_shared_ori_once():
    # A recipient list where the same ORI appears twice must not double-count.
    r = lib.sworn_for_oris(["CA0010000", "CA0010000", "CA0020000"], sworn=SWORN)
    assert r["officers"] == 150
    assert r["oris_with_data"] == 2


def test_umbrella_sum_with_null_subunits():
    # CHP-style: one HQ ORI carries the total, sibling sub-units report null.
    # The null sibling counts as "no data", not zero, and doesn't inflate.
    r = lib.sworn_for_oris(["CA0CHP001", "CA0CHP002"], sworn=SWORN)
    assert r["officers"] == 7000
    assert r["oris_with_data"] == 1
    assert r["oris_no_data"] == 1


def test_missing_and_null_are_no_data_not_zero():
    r = lib.sworn_for_oris(["CA0NULL00", "CA9999999"], sworn=SWORN)
    assert r["officers"] == 0
    assert r["oris_with_data"] == 0
    assert r["oris_no_data"] == 2


def test_agency_sworn_no_ori_returns_none():
    assert lib.agency_sworn({"agency_id": "x"}, sworn=SWORN) is None
    assert lib.agency_sworn({"agency_id": "x", "ori": []}, sworn=SWORN) is None


def test_agency_sworn_sums_ori_list():
    entry = {"agency_id": "x", "ori": ["CA0010000", "CA0020000"]}
    r = lib.agency_sworn(entry, sworn=SWORN)
    assert r["officers"] == 150 and r["total"] == 200


def test_min_year_excludes_stale():
    # A department whose latest FBI year predates min_year is stale, not summed.
    r = lib.sworn_for_oris(["CA0010000", "CA0OLD000"], sworn=SWORN, min_year=2024)
    assert r["officers"] == 100          # only CA0010000 (2024) counted
    assert r["oris_with_data"] == 1
    assert r["oris_stale"] == 1
    assert r["data_year"] == 2024
    # Without the cutoff, the stale agency is included.
    r2 = lib.sworn_for_oris(["CA0010000", "CA0OLD000"], sworn=SWORN)
    assert r2["officers"] == 130 and r2["oris_stale"] == 0


def test_sworn_latest_year():
    assert lib.sworn_latest_year(sworn=SWORN) == 2024


# ── match_ori guards ─────────────────────────────────────────────


def test_da_never_matches_non_da():
    # FBI lists no DA offices, so a DA registry entry must conflict with a
    # same-named city PD (place score is irrelevant — role gate is 0.0).
    assert mo._role_compat("da", "police") == 0.0
    assert mo._role_compat("da", "other") == 0.0
    assert mo._role_compat("da", "da") == 1.0


def test_police_sheriff_conflict():
    assert mo._role_compat("police", "sheriff") == 0.0
    assert mo._role_compat("sheriff", "sheriff") == 1.0


def _fbi(ori, name, type_name="State Police"):
    return {"ori": ori, "agency_name": name, "agency_type_name": type_name}


def test_umbrella_family_grabs_whole_set():
    cands = [
        _fbi("CA0019945", "Highway Patrol: Hayward Area Office"),
        _fbi("CA0019970", "Highway Patrol: Oakland Area Office"),
        _fbi("CA0411600", "San Mateo Police Department", "City"),
    ]
    fam = mo.umbrella_family("highway_patrol", cands)
    assert fam == ["CA0019945", "CA0019970"]  # sorted, city PD excluded
    # Non-umbrella role returns nothing.
    assert mo.umbrella_family("police", cands) == []


def test_qualifier_mismatch_blocks_subagency():
    # "Flint Township" must not silently collapse onto "Flint" city PD.
    reg = mo._raw_tokens("Flint Township MI PD")
    fbi = mo._raw_tokens("Flint Police Department")
    assert mo._qualifier_mismatch(reg, fbi) is not None
    # Same place, no qualifier on either side → no mismatch.
    reg2 = mo._raw_tokens("Corona CA PD")
    fbi2 = mo._raw_tokens("Corona Police Department")
    assert mo._qualifier_mismatch(reg2, fbi2) is None
    # University qualifier on the registry side only.
    assert mo._qualifier_mismatch(
        mo._raw_tokens("UC Riverside PD"), mo._raw_tokens("Riverside Police Department")
    ) is not None
