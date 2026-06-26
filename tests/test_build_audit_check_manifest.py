# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Tests for scripts/build_audit_check_manifest.py — the auto-generated picker
manifest for the PDF revision/edit checker."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_audit_check_manifest as bm  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FOLDER = REPO / "assets/san-mateo-public-records/W012541-041426"


def test_label_and_part_parsing():
    assert bm.label_for("1_1_2025-1_31_2025-San_Mateo_CA_PD-Audit__Part_1_.pdf")[0] == "Jan 2025 (pt1)"
    assert bm.label_for("12_1_2025-12_31_2025-San_Mateo_CA_PD_Audit.pdf")[0] == "Dec 2025"
    assert bm.label_for("2_1_2026-2_28_2026-San_Mateo_CA_PD-Audit__2_.pdf")[0] == "Feb 2026"


@pytest.mark.skipif(not FOLDER.exists(), reason="W012541 audit PDFs not present")
def test_manifest_flags_and_hygiene():
    man = bm.build_manifest(REPO)
    assert man, "manifest is empty"
    # every entry is path-based and points inside the repo's assets
    assert all(e["path"].startswith("assets/") and e["path"].endswith(".pdf") for e in man)
    # the non-audit production record must not leak into the picker
    assert not any("Message_History" in e["path"] for e in man)
    # chronological order (robust to how many months are present)
    assert man[0]["label"] == "Jan 2023"
    keys = [bm.label_for(Path(e["path"]).name)[1] for e in man]
    assert keys == sorted(keys)
    # chips carry flags ONLY — no edit detail, notes, or person names baked in
    assert all(set(e) <= {"label", "path", "edited", "flattened"} for e in man)

    feb = next(e for e in man if e["label"] == "Feb 2026")
    assert feb.get("edited") is True
    assert feb.get("flattened") is True  # Feb's base is a Print-to-PDF re-render

    # the set distinguishes recoverable edits from opaque (flattened) files
    assert any(e.get("flattened") and not e.get("edited") for e in man)
    assert any(e.get("edited") for e in man)
