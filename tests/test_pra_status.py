# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) zero below
"""Closure detection in the PRA registry parser.

A PRA whose closure sentence the regex misses falls through to
``needs_review`` and, if the metadata still carries a filing-time
``status_override``, silently displays as ``awaiting_initial`` — a request the
City answered weeks ago reads as one it never touched. These are the phrasings
SMPD has actually used, and the near-miss phrasings that must NOT count.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest

from build_pra_registry import CLOSED_RE


CLOSURES = [
    # Third person — the canonical portal template.
    "The City has completed its response to your request and now considers "
    "this record request W013283-082126 closed.",
    # First person plural — same meaning, different staff member (W013309).
    "The City has completed its response to your request and we now consider "
    "this record request W013309-082426 closed.",
    # The id wraps across a line break in the PDF text layer.
    "we now consider this record request W013309-\n082426 closed.",
    # No id at all (W013265).
    "The City has completed its response to your request and now considers "
    "this record request closed.",
]

NON_CLOSURES = [
    # Requester asking the agency to close a DIFFERENT request, echoed back
    # inside the quoted request body.
    "Please consider closing W012462-040226 so the other items can proceed.",
    # Rolling production — explicitly not closed.
    "The next batch of records has been released. We will further respond to "
    "your request with updates no later than 9/23/26.",
    # A closure of a parent request described in prose, without the
    # "consider(s) this/the ... closed" construction.
    "On 5/6 the Department closed W012297-030826 into W012462-040226.",
]


@pytest.mark.parametrize("body", CLOSURES)
def test_closure_phrasings_match(body):
    assert CLOSED_RE.search(body), f"closure not detected: {body!r}"


@pytest.mark.parametrize("body", NON_CLOSURES)
def test_near_miss_phrasings_do_not_match(body):
    assert not CLOSED_RE.search(body), f"false closure: {body!r}"
