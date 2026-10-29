# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) zero below
"""Promised-update dates in the PRA registry parser.

A "no later than X" date the regex misses leaves the previous promise as the
current one, so a request the City just re-dated displays as ``overdue``.
These are the date forms SMPD has actually written.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest

from build_pra_registry import promise_history


def _agency(ts, body):
    return {"ts": ts, "sender_role": "agency", "body": body}


@pytest.mark.parametrize("body, expected", [
    ("updates no later than 11/11/26.", "2026-11-11"),
    ("updates no later than 11/11/2026.", "2026-11-11"),
    # Yearless (W012686, 2026-10-28) — takes the message's year.
    ("updates no later than 11/11.", "2026-11-11"),
])
def test_numeric_promise_forms(body, expected):
    msgs = [_agency("2026-10-28T18:10:00", body)]
    assert promise_history(msgs, 2026)[-1]["promise_date"] == expected


def test_yearless_promise_rolls_into_next_year():
    msgs = [_agency("2026-12-30T10:00:00", "updates no later than 1/13.")]
    assert promise_history(msgs, 2026)[-1]["promise_date"] == "2027-01-13"


def test_yearless_promise_supersedes_prior_dated_one():
    msgs = [
        _agency("2026-10-14T14:33:02", "updates no later than 10/28/26."),
        _agency("2026-10-28T17:10:00", "updates no later than 11/11."),
    ]
    history = promise_history(msgs, 2026)
    assert [h["promise_date"] for h in history] == ["2026-10-28", "2026-11-11"]
