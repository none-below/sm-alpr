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
MULTI_EDITED = REPO / "assets/san-mateo-public-records/W012541-041426/5_1_2024-5_31_2024-San_Mateo_CA_PD-Audit.pdf"

U1 = "11111111-1111-1111-1111-111111111111"
U2 = "22222222-2222-2222-2222-222222222222"


def _rev(text: str) -> dict:
    t = par.norm(text)
    return {"text": t, "rows": par.rows_of(t)}


def test_classify_known_tools():
    assert "Adobe" in par.classify("Adobe XMP Core 9.1-c001 79.675d0f7, 2023/06/11-19:21:16")
    assert "Microsoft" in par.classify("Microsoft: Print To PDF")
    assert "legacy" in par.classify("3.1-701")
    assert par.classify("") == ""
    assert par.classify("Some Unknown Producer 1.0") == ""


def test_revision_ends_counts_eof_markers():
    data = b"%PDF-1.4\n...\n%%EOF\nmore\n...\n%%EOF\n"
    assert len(par.revision_ends(data)) == 2


def test_revision_ends_drops_linearized_first_eof():
    # A linearized PDF's first %%EOF ends the first-page xref stub, not a real
    # revision; truncating there gives a rootless fragment ("Invalid Root reference").
    lin = b"%PDF-1.6\n1 0 obj<</Linearized 1/L 99>>endobj\nx\n%%EOF\nmore objs\n%%EOF\n"
    assert len(par.revision_ends(lin)) == 1
    # non-linearized files keep every marker
    assert len(par.revision_ends(b"%PDF-1.4\n...\n%%EOF\nmore\n%%EOF\n")) == 2


def test_is_flatten():
    assert par.is_flatten("Microsoft (Office / Print to PDF)")
    assert par.is_flatten("Apple Quartz / macOS")
    assert not par.is_flatten("Adobe Acrobat / Adobe PDF Library")
    assert not par.is_flatten("legacy XMP toolkit 3.1 (older library)")
    assert not par.is_flatten("")


def test_norm_and_fmt_date():
    assert par.norm("a   b\tc\n") == "a b c"
    assert par.fmt_date("D:20260603170653-07'00'") == "2026-06-03 17:06:53 -07:00"
    assert par.fmt_date("2026-06-03T17:06:53-07:00") == "2026-06-03 17:06:53 -07:00"
    assert par.fmt_date("") == ""


def test_is_trivial():
    assert par.is_trivial("-")
    assert par.is_trivial(U1)  # a bare UUID is handled by the row diff, not the word diff
    assert not par.is_trivial("2602190261")
    assert not par.is_trivial("Investigation")


def test_word_diff_detects_removed_case_number_only():
    a = _rev(f"{U1} *** 870 d Motor Vehicle Theft/Stolen - 2602190261 {U2} *** 5 d Traffic Infraction")
    b = _rev(f"{U1} *** 870 d Motor Vehicle Theft/Stolen {U2} *** 5 d Traffic Infraction")
    d = par.diff_revisions(a, b)
    removed_tokens = [t for t, _ in d["removed"]]
    assert "2602190261" in removed_tokens
    assert not d["added"] and not d["deleted_rows"] and not d["added_rows"]
    # the removed token is attributed to U1's row, never the unchanged U2 row
    assert all((row is None) or row[0] == U1 for _, row in d["removed"])


def test_same_token_removed_from_two_rows_attributes_both():
    # e.g. the same plate scrubbed from two rows in one save — previously "(no
    # unique row)"; every containing row is affected when the counts match
    a = _rev(f"{U1} *** 1 d investigation via license 8fpp888 {U2} *** 1 d investigation via license 8fpp888")
    b = _rev(f"{U1} *** 1 d investigation via license {U2} *** 1 d investigation via license")
    d = par.diff_revisions(a, b)
    assert sorted(row[0] for tok, row in d["removed"] if tok == "8fpp888") == [U1, U2]


def test_partial_removal_narrowed_by_other_side():
    # both rows contain "via license"; only U1 loses it — the survivor's same-UUID
    # row on the other side pins the change to U1
    a = _rev(f"{U1} *** 1 d investigation via license {U2} *** 1 d investigation via license")
    b = _rev(f"{U1} *** 1 d investigation {U2} *** 1 d investigation via license")
    d = par.diff_revisions(a, b)
    assert {tok for tok, _ in d["removed"]} == {"via", "license"}
    assert all(row is not None and row[0] == U1 for _, row in d["removed"])


def test_diff_is_order_independent():
    # rows reordered (as an Acrobat re-save can cause) => no spurious change
    a = _rev(f"{U1} *** 1 d Reason One {U2} *** 2 d Reason Two")
    b = _rev(f"{U2} *** 2 d Reason Two {U1} *** 1 d Reason One")
    d = par.diff_revisions(a, b)
    assert not d["removed"] and not d["added"] and not d["deleted_rows"] and not d["added_rows"]


def test_diff_detects_deleted_row():
    a = _rev(f"{U1} *** 1 d Reason One {U2} *** 2 d Reason Two")
    b = _rev(f"{U1} *** 1 d Reason One")
    d = par.diff_revisions(a, b)
    assert [r[0] for r in d["deleted_rows"]] == [U2]


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
    # single content edit — no net-change section
    assert "net change" not in out


@pytest.mark.skipif(not MULTI_EDITED.exists(), reason="sample multi-edited audit PDF not present")
def test_multi_save_edit_attributes_rows_and_sums_net_change(capsys):
    # May 2024: plate scrubbed from two rows (save 1), leftover "via license"
    # groomed out (saves 2-3). Every removal must carry row context, and the net
    # section must show each row's full original -> produced text.
    par.analyze(MULTI_EDITED)
    out = capsys.readouterr().out
    assert "EDIT 3" in out
    assert "no unique row" not in out and "ambiguous" not in out
    assert "net change" in out
    for uuid in ("1e318b30", "d19a461a"):
        assert f"ROW {uuid}" in out
    assert out.count("stolen plate/vehicle investigation via license # 8fpp888") >= 4  # 2 per-edit + 2 net
