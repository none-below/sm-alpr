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
    hdr = HDR[:4] + [""] + HDR[5:]
    labels, phantom, unlabeled, note = repair_header(hdr, [ROW] * 20)
    assert not phantom and unlabeled == [5] and labels[4] == "column_5" and labels[5] == "License Plate"


def test_ambiguous_refused():
    # a header whose labels run in the opposite order to the data needs more repairs than a hand edit explains
    with pytest.raises(ValueError):
        repair_header(HDR[::-1], [ROW] * 20)
