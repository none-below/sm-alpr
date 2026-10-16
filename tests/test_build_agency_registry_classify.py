# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""classify() tags federal agencies only on a whole-word federal token.

The federal pattern's `ICE\\b` alternative had no leading word boundary, so
it matched the tail of "Police", "Office" and "Service" and tagged county
DAs and city police departments as federal.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from build_agency_registry import classify  # noqa: E402


@pytest.mark.parametrize("name", [
    "1st Judicial District, District Attorney's Office TX",
    "Saint Paul Police Department (MN)",
    "Public Service Authority",
    "Cook County States Attorney's Office IL PD",
])
def test_ice_inside_a_word_is_not_federal(name):
    assert "federal" not in classify(name)["tags"]


@pytest.mark.parametrize("name", [
    "[Federal] Federal Bureau of Investigation (FBI)",
    "US Marshals Service",
    "DEA Los Angeles Field Division",
    "ICE ERO San Francisco",
    "US Secret Service",
])
def test_federal_tokens_are_federal(name):
    result = classify(name)
    assert result["agency_type"] == "federal"
    assert "federal" in result["tags"]
