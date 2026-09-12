# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""A re-downloaded produced file whose content changed (e.g. SMPD re-uploads a
'fixed' copy) must never overwrite the original, and must warn loudly."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pra_download as pd  # noqa: E402


def test_changed_content_preserves_original_and_warns(tmp_path, capsys):
    (tmp_path / "audit.pdf").write_bytes(b"ORIGINAL-BYTES")
    out = pd._resolve_collision_path(tmp_path, "audit.pdf", b"REUPLOADED-DIFFERENT")
    names = sorted(p.name for p in tmp_path.iterdir())
    # original is preserved (renamed to a content-hashed sibling), never lost
    assert "audit.pdf" not in names
    assert any(n.startswith("audit.") and n.endswith(".pdf") for n in names)
    # the new copy gets its own distinct hashed path (written by the caller, not here)
    assert out.name.startswith("audit.") and out.name.endswith(".pdf")
    assert out.name not in names
    # and a loud warning was emitted
    combined = capsys.readouterr()
    assert "CONTENT CHANGED" in (combined.out + combined.err)


def test_identical_content_is_silent_and_keeps_name(tmp_path, capsys):
    (tmp_path / "audit.pdf").write_bytes(b"SAME-BYTES")
    out = pd._resolve_collision_path(tmp_path, "audit.pdf", b"SAME-BYTES")
    assert out.name == "audit.pdf"
    combined = capsys.readouterr()
    assert "CONTENT CHANGED" not in (combined.out + combined.err)
