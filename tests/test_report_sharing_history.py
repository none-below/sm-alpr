# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""build_report_data._load_sharing_history reconciles outbound history into
added / removed / readds (flapping) plus an un-windowed raw-removal view.
It's a parallel implementation of build_history._reconcile_sharing (which
feeds the map), keyed by agency_id and reading the on-disk history files —
this test guards the two from drifting.

Focus: a partner dropped then re-added in-window lands in `readds` (with the
full shared->dropped->reshared sequence) and NOT in added/removed, while a
same-date portal relabel of one agency collapses to nothing.

Every call injects an explicit cutoff so the hardcoded event dates never age
out of the default today-minus-90-days window (which would make the suite
fail by itself as the calendar advances).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import build_report_data

CUTOFF = "2026-01-01"


def _hist(tmp, slug, events):
    (tmp / f"{slug}.json").write_text(json.dumps({"slug": slug, "events": events}))


def _set_ev(date, added=None, removed=None):
    return {"date": date, "field": "sharing_outbound", "kind": "set",
            "added": added or [], "removed": removed or []}


@pytest.fixture(autouse=True)
def patched(tmp_path, monkeypatch):
    monkeypatch.setattr(build_report_data, "HISTORY_DIR", tmp_path)
    aliases = {
        "El Cajon CA PD": "el-cajon",
        "Newark CA PD": "newark",
        "Sacramento CA PD": "sacramento",
        "Sacramento PD - CA": "sacramento",  # relabel of the same agency
    }

    def fake_resolve(name):
        aid = aliases.get(name)
        return {"agency_id": aid} if aid else None

    monkeypatch.setattr(build_report_data, "resolve_agency", fake_resolve)
    return tmp_path


def test_drop_then_readd_is_flapping_with_sequence(patched):
    _hist(patched, "agency-x", [
        _set_ev("2026-05-06", removed=["El Cajon CA PD"]),
        _set_ev("2026-06-03", added=["El Cajon CA PD"]),
    ])
    added, removed, readds, removed_all = build_report_data._load_sharing_history(
        {"slug": "agency-x"}, cutoff=CUTOFF
    )
    assert added == {} and removed == []
    assert "el-cajon" in readds
    rd = readds["el-cajon"]
    assert rd["removed_on"] == "2026-05-06"
    assert rd["readded_on"] == "2026-06-03"
    assert [s["action"] for s in rd["sequence"]] == ["removed", "added"]
    # The raw view keeps the flapping partner's removal — consumers guard
    # on current membership themselves.
    assert [(e["agency_id"], e["date"]) for e in removed_all] == \
        [("el-cajon", "2026-05-06")]


def test_plain_add_is_added_not_flapping(patched):
    _hist(patched, "agency-x", [_set_ev("2026-05-12", added=["Newark CA PD"])])
    added, removed, readds, removed_all = build_report_data._load_sharing_history(
        {"slug": "agency-x"}, cutoff=CUTOFF
    )
    assert added == {"newark": "2026-05-12"}
    assert removed == [] and readds == {} and removed_all == []


def test_drop_only_is_removed_not_flapping(patched):
    _hist(patched, "agency-x", [_set_ev("2026-05-12", removed=["Newark CA PD"])])
    added, removed, readds, removed_all = build_report_data._load_sharing_history(
        {"slug": "agency-x"}, cutoff=CUTOFF
    )
    assert readds == {} and added == {}
    assert [(e["agency_id"], e["date"]) for e in removed] == [("newark", "2026-05-12")]
    assert [(e["agency_id"], e["date"]) for e in removed_all] == \
        [("newark", "2026-05-12")]


def test_same_date_relabel_collapses(patched):
    _hist(patched, "agency-x", [
        _set_ev("2026-05-12", added=["Sacramento CA PD"], removed=["Sacramento PD - CA"]),
    ])
    added, removed, readds, removed_all = build_report_data._load_sharing_history(
        {"slug": "agency-x"}, cutoff=CUTOFF
    )
    assert added == {} and removed == [] and readds == {}
    # The raw view records the old label's removal; the consumer's
    # currently-shared guard keeps it off the report.
    assert [(e["agency_id"], e["date"]) for e in removed_all] == \
        [("sacramento", "2026-05-12")]


def test_pre_window_removal_still_feeds_flagged_view(patched):
    """A removal older than the window must vanish from the netted views but
    survive in removed_all_events: removed_flagged_recipients has no window,
    so a flagged partner dropped long ago stays on the report."""
    _hist(patched, "agency-x", [_set_ev("2025-10-01", removed=["Newark CA PD"])])
    added, removed, readds, removed_all = build_report_data._load_sharing_history(
        {"slug": "agency-x"}, cutoff=CUTOFF
    )
    assert added == {} and removed == [] and readds == {}
    assert [(e["agency_id"], e["date"]) for e in removed_all] == \
        [("newark", "2025-10-01")]
