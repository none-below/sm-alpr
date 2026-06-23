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


def test_multiset_diff_ignores_whitespace_reflow():
    removed, added = par.multiset_diff(
        ["a    b", "uuid 2602190261 stolen"],
        ["a b", "uuid stolen"],
    )
    # whitespace-only change ("a    b" vs "a b") is normalized away, not reported
    assert all(par.norm("a b") != r for r in removed)
    # the real token removal is reported
    assert "uuid 2602190261 stolen" in removed
    assert "uuid stolen" in added


def test_pair_by_leading_token():
    pairs, only_r, only_a = par.pair_by_leading_token(
        ["row1 Motor Vehicle Theft/Stolen - 2602190261"],
        ["row1 Motor Vehicle Theft/Stolen"],
    )
    assert pairs and pairs[0][0].endswith("2602190261")
    assert not only_r and not only_a


@pytest.mark.skipif(not EDITED.exists(), reason="sample edited audit PDF not present")
def test_recovers_removed_case_number(capsys):
    par.analyze(EDITED)
    out = capsys.readouterr().out
    assert "EDITED / RE-SAVED" in out
    assert "2602190261" in out  # case number removed in the produced version is recovered
    assert "last edited in Adobe" in out
