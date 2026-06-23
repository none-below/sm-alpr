# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Tests for scripts/pdf_audit_revisions.py — the PDF revision/edit recovery tool."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pdf_audit_revisions as par  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
EDITED = REPO / "assets/san-mateo-public-records/W012541-041426/2_1_2026-2_28_2026-San_Mateo_CA_PD-Audit__2_.pdf"


def test_classify_known_tools():
    assert "Adobe" in par.classify("Adobe XMP Core 9.1-c001 79.675d0f7, 2023/06/11-19:21:16")
    assert "Microsoft" in par.classify("Microsoft: Print To PDF")
    assert "legacy" in par.classify("3.1-701")
    assert par.classify("") == ""
    assert par.classify("Some Unknown Producer 1.0") == ""


def test_revision_ends_counts_eof_markers():
    data = b"%PDF-1.4\nobjs\nxref\ntrailer\nstartxref\n%%EOF\nmore\nxref\ntrailer\nstartxref\n%%EOF\n"
    assert len(par.revision_ends(data)) == 2


def test_norm_collapses_whitespace():
    assert par.norm("a   b\tc\n") == "a b c"


def test_fmt_date():
    assert par.fmt_date("D:20260603170653-07'00'") == "2026-06-03 17:06:53 -07:00"
    assert par.fmt_date("2026-06-03T17:06:53-07:00") == "2026-06-03 17:06:53 -07:00"
    assert par.fmt_date("") == ""


def test_diff_pairs_changed_row_and_ignores_unchanged():
    # identical row cancels; only the row that lost its case number is reported
    a = {"lines": [
        "uuid1 *** 870 date Motor Vehicle Theft/Stolen - 2602190261",
        "uuid2 *** 5 date Traffic Infraction",
    ]}
    b = {"lines": [
        "uuid1 *** 870 date Motor Vehicle Theft/Stolen",
        "uuid2 *** 5 date Traffic Infraction",
    ]}
    changed, removed, added = par.diff_revisions(a, b)
    assert not removed and not added
    assert len(changed) == 1
    assert "2602190261" in changed[0][0] and "2602190261" not in changed[0][1]


def test_diff_is_order_independent():
    # reordered lines (as an Acrobat re-save can cause) must not register as changes
    a = {"lines": ["row A", "row B", "row C"]}
    b = {"lines": ["row C", "row A", "row B"]}
    changed, removed, added = par.diff_revisions(a, b)
    assert not changed and not removed and not added


def test_diff_lone_pair_fallback():
    a = {"lines": ["x", "Ivne"]}
    b = {"lines": ["x", "Investigation"]}
    changed, removed, added = par.diff_revisions(a, b)
    assert changed == [("Ivne", "Investigation")]
    assert not removed and not added


@pytest.mark.skipif(not EDITED.exists(), reason="sample edited audit PDF not present")
def test_recovers_removed_case_number_no_false_positives(capsys):
    par.analyze(EDITED)
    out = capsys.readouterr().out
    assert "EDITED / RE-SAVED" in out
    assert "edit timeline" in out and "EDIT 1" in out
    assert "last edited in Adobe" in out
    assert "2602190261" in out  # the real edit is recovered
    # unchanged neighbor row / reason must NOT be reported (the false-positive bug)
    assert "815fc148" not in out
    assert "Traffic Infraction" not in out
