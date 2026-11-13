# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""header_align on invented sheets: a well-formed header passes through; a phantom label is dropped instead of
shifting the data; an unlabeled column is kept; an ambiguous sheet is refused."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from header_align import repair_header  # noqa: E402

HDR = ["Name", "Org Name", "Total Networks Searched", "Total Devices Searched", "Time Frame", "License Plate",
       "Reason", "Case #", "Filters", "Search Time", "Search Type"]
ROW = ["REDACTED", "Example PD", "3", "41", "01/02/2026, 01:00:00 AM UTC\n02/02/2026, 01:00:00 AM UTC", "REDACTED",
       "example reason", "26-0001", "california", "02/02/2026, 03:04:05 AM UTC", "lookup"]


def test_well_formed_passes_through():
    labels, phantom, unlabeled, note = repair_header(HDR, [ROW] * 20)
    assert labels == HDR and not phantom and not unlabeled and note is None


def test_phantom_label_dropped_not_shifted():
    # the agency deleted the Total Devices column's data but left its label; the data is one column narrower
    rows = [ROW[:3] + ROW[4:] + [""] for _ in range(20)]
    labels, phantom, unlabeled, note = repair_header(HDR, rows)
    assert phantom == ["Total Devices Searched"]
    assert labels[:10] == HDR[:3] + HDR[4:] and labels[10] == "column_11"
    assert "phantom" in note


def test_unlabeled_column_kept():
    # a blank label standing over a data column shifts nothing (the header is as wide as the data): the column loads
    # under the loader's blank-label name and the repair stays out of it
    hdr = HDR[:4] + [""] + HDR[5:]
    labels, phantom, unlabeled, note = repair_header(hdr, [ROW] * 20)
    assert labels == hdr and note is None


def test_blank_trailing_label_alone_is_left_alone():
    # a blank label over an all-empty trailing column is nothing to repair
    labels, phantom, unlabeled, note = repair_header(HDR + [""], [ROW + [""]] * 20)
    assert labels == HDR + [""] and note is None


def test_ambiguous_refused():
    # a header whose labels run in the opposite order to the data needs more repairs than a hand edit explains
    with pytest.raises(ValueError):
        repair_header(HDR[::-1], [ROW] * 20)


def test_no_contradiction_means_no_repair():
    # Flock dates with a one-digit month, and San Jose's split date + time-of-day columns: nothing to repair
    row = ["REDACTED", "Example PD", "3", "41", "1/2/2026, 01:00:00 AM UTC\n2/2/2026, 01:00:00 AM UTC", "REDACTED",
           "reason", "26-0001", "california", "8/28/2024, 03:56:24 PM UTC", "lookup"]
    labels, phantom, unlabeled, note = repair_header(HDR, [row] * 20)
    assert labels == HDR and note is None
    hdr = HDR[:9] + ["Search Date", "Search Time"]
    row2 = row[:9] + ["08/28/2024", "08:00:39"]
    labels, phantom, unlabeled, note = repair_header(hdr, [row2] * 20)
    assert labels == hdr and note is None


def test_unknown_labels_never_trigger():
    hdr = ["ID"] + HDR
    row = ["0f3b2a1c-1111-4aaa-8bbb-000000000001"] + ROW
    labels, phantom, unlabeled, note = repair_header(hdr, [row] * 20)
    assert labels == hdr and note is None


def test_placeholders_say_nothing_about_a_column():
    # a count column holding only an em-dash placeholder (or its mojibake, or REDACTED) is not a contradiction
    for mask in ("——", "â€”â€”", "REDACTED", "***", "N/A"):
        row = ROW[:3] + [mask] + ROW[4:]
        labels, phantom, unlabeled, note = repair_header(HDR, [row] * 20)
        assert labels == HDR and note is None, mask


def test_genuine_shift_is_repaired():
    # an export that dropped the Filters cell: the Search Time date sits under "Filters", the type under "Search Time"
    rows = [ROW[:8] + ROW[9:] + [""] for _ in range(20)]
    labels, phantom, unlabeled, note = repair_header(HDR, rows)
    assert phantom == ["Filters"] and labels[8] == "Search Time" and labels[9] == "Search Type"


def test_shift_with_empty_search_time_column_is_repaired():
    # an export with an extra empty cell before Search Time: the date sits under "Search Type", the type under
    # "Text Prompt"; the Search Time column itself is empty, so the contradiction shows one column to the right
    hdr = ["ID"] + HDR + ["Text Prompt", "Moderation"]
    rows = [["0f3b2a1c-1111-4aaa-8bbb-000000000001"] + ROW[:7] + ["", "", "", ROW[9], ROW[10], ""] for _ in range(20)]
    labels, phantom, unlabeled, note = repair_header(hdr, rows)
    assert note and labels[11] == "Search Time" and labels[12] == "Search Type"
