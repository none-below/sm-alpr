"""_reconcile_sharing must classify each sharing target into exactly one of
added / removed / readded, so the map never paints a partner as both a live
edge and a crossed-out ghost.

Two cases drive the logic:
  - present -> removed -> re-added (different dates) is the churn the map got
    wrong; it must land in `readded` with both gap dates, and NOT in
    added/removed.
  - a portal relabel of the same partner ("Foo PD - CA" -> "Foo CA PD") shows
    up as a same-date remove+add for one resolved identity; it's a rename, not
    a sharing change, and must collapse to nothing.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import build_history

CUTOFF = "2026-01-01"  # well before every date used below


def _ev(date, added=None, removed=None, field="sharing_outbound"):
    return {
        "date": date,
        "prev_date": "",
        "field": field,
        "kind": "set",
        "added": added or [],
        "removed": removed or [],
    }


@pytest.fixture(autouse=True)
def stub_resolve(monkeypatch):
    """Resolve names by a fixed alias table — two labels can map to one slug
    (the rename case) — so the test pins reconciliation, not the registry."""
    aliases = {
        "El Cajon CA PD": "el-cajon-ca-pd",
        "Sacramento CA PD": "sacramento-ca-pd",
        "Sacramento PD - CA": "sacramento-ca-pd",  # relabel of the same agency
        "Newark CA PD": "newark-ca-pd",
    }

    def fake_resolve(name):
        slug = aliases.get(name)
        return {"name": name, "slug": slug, "agency_id": slug}  # agency_id mirrors slug

    monkeypatch.setattr(build_history, "_resolve_name", fake_resolve)


def _reconcile(events):
    added, removed, readded = build_history._reconcile_sharing(
        events, "sharing_outbound", CUTOFF
    )
    return added, removed, readded


def test_plain_add_is_added_only():
    added, removed, readded = _reconcile([_ev("2026-05-12", added=["Newark CA PD"])])
    assert [a["slug"] for a in added] == ["newark-ca-pd"]
    assert added[0]["date"] == "2026-05-12"
    assert removed == [] and readded == []


def test_plain_remove_is_removed_only():
    added, removed, readded = _reconcile([_ev("2026-05-12", removed=["Newark CA PD"])])
    assert [r["slug"] for r in removed] == ["newark-ca-pd"]
    assert removed[0]["date"] == "2026-05-12"
    assert added == [] and readded == []


def test_present_removed_readded_is_readded_with_dates():
    """The Pacifica -> El Cajon bug: removed 5/06, re-added 6/03."""
    events = [
        _ev("2026-05-06", removed=["El Cajon CA PD"]),
        _ev("2026-06-03", added=["El Cajon CA PD"]),
    ]
    added, removed, readded = _reconcile(events)
    assert added == [] and removed == []
    assert len(readded) == 1
    assert readded[0]["slug"] == "el-cajon-ca-pd"
    assert readded[0]["removed_date"] == "2026-05-06"
    assert readded[0]["readded_date"] == "2026-06-03"


def test_add_then_remove_ends_removed():
    events = [
        _ev("2026-05-06", added=["El Cajon CA PD"]),
        _ev("2026-06-03", removed=["El Cajon CA PD"]),
    ]
    added, removed, readded = _reconcile(events)
    assert added == [] and readded == []
    assert [r["slug"] for r in removed] == ["el-cajon-ca-pd"]
    assert removed[0]["date"] == "2026-06-03"


def test_same_date_rename_collapses_to_nothing():
    """Old label removed and new label added on the same scrape, both
    resolving to one slug, is a relabel — not a membership change."""
    events = [_ev("2026-05-12", added=["Sacramento CA PD"], removed=["Sacramento PD - CA"])]
    added, removed, readded = _reconcile(events)
    assert added == [] and removed == [] and readded == []


def test_window_cutoff_drops_old_events():
    events = [_ev("2025-01-01", added=["Newark CA PD"])]  # before cutoff
    added, removed, readded = _reconcile(events)
    assert added == [] and removed == [] and readded == []


def test_multiple_cycles_ending_present_is_readded_with_latest_pair():
    """removed -> added -> removed -> added: classified by current state
    (present => readded), surfacing the MOST RECENT removal/re-add pair."""
    events = [
        _ev("2026-03-10", removed=["El Cajon CA PD"]),
        _ev("2026-04-10", added=["El Cajon CA PD"]),
        _ev("2026-05-10", removed=["El Cajon CA PD"]),
        _ev("2026-06-10", added=["El Cajon CA PD"]),
    ]
    added, removed, readded = _reconcile(events)
    assert added == [] and removed == []
    assert len(readded) == 1
    assert readded[0]["removed_date"] == "2026-05-10"   # latest removal, not 03-10
    assert readded[0]["readded_date"] == "2026-06-10"   # latest re-add


def test_multiple_cycles_ending_absent_is_removed():
    """added -> removed -> added -> removed: current state is absent."""
    events = [
        _ev("2026-03-10", added=["El Cajon CA PD"]),
        _ev("2026-04-10", removed=["El Cajon CA PD"]),
        _ev("2026-05-10", added=["El Cajon CA PD"]),
        _ev("2026-06-10", removed=["El Cajon CA PD"]),
    ]
    added, removed, readded = _reconcile(events)
    assert added == [] and readded == []
    assert [r["slug"] for r in removed] == ["el-cajon-ca-pd"]
    assert removed[0]["date"] == "2026-06-10"   # latest removal
