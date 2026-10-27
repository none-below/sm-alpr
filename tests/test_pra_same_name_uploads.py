# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Two different attachments uploaded under one filename must both be kept.

W012541 (10/24/2026) listed two anchors named 7_16_2026-7_31_2026-...-Audit.pdf.
The audit PDFs have no text layer, so both text fingerprints were md5("") and
the second file was dropped as "unchanged"; and once one copy was on disk,
every anchor with that label was skipped by name."""
import ast
import hashlib
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pra_download as pd  # noqa: E402


def _no_text_pdf(fill: float) -> bytes:
    """A one-page PDF with a filled rectangle and no extractable text."""
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(50, 50, 300, 300), color=(0, 0, 0), fill=(fill, fill, fill))
    data = doc.tobytes()
    doc.close()
    return data


def test_no_text_pdf_has_no_fingerprint():
    assert pd.pdf_text_fingerprint(_no_text_pdf(0.2)) is None


def test_different_no_text_pdfs_with_same_name_are_both_kept(tmp_path, capsys):
    first, second = _no_text_pdf(0.2), _no_text_pdf(0.8)
    assert first != second
    (tmp_path / "audit.pdf").write_bytes(first)
    out, state = pd._store_bytes(second, "audit.pdf", None, tmp_path)
    assert state == "new"
    kept = sorted(p.read_bytes() for p in tmp_path.iterdir())
    assert kept == sorted([first, second])
    combined = capsys.readouterr()
    assert "CONTENT CHANGED" in (combined.out + combined.err)


def test_withheld_upload_does_not_rename_kept_file(tmp_path):
    kept, unwanted = _no_text_pdf(0.2), _no_text_pdf(0.8)
    (tmp_path / "audit.pdf").write_bytes(kept)
    withheld = {hashlib.sha256(unwanted).hexdigest(): "test"}
    out, state = pd._store_bytes(unwanted, "audit.pdf", None, tmp_path, withheld)
    assert state == "withheld"
    assert [p.name for p in tmp_path.iterdir()] == ["audit.pdf"]
    assert (tmp_path / "audit.pdf").read_bytes() == kept


def test_skip_by_name_single_anchor():
    assert pd.skip_by_name({"audit.pdf"}, "audit.pdf", 1)
    assert not pd.skip_by_name(set(), "audit.pdf", 1)


def test_skip_by_name_counts_copies_for_repeated_label():
    assert not pd.skip_by_name({"audit.pdf"}, "audit.pdf", 2)
    assert not pd.skip_by_name({"audit.pdf", "other.1a2b3c4d.pdf"}, "audit.pdf", 2)
    assert pd.skip_by_name({"audit.pdf", "audit.5e6f7a8b.pdf"}, "audit.pdf", 2)
    # after a collision both copies are hash-suffixed siblings
    assert pd.skip_by_name({"audit.1a2b3c4d.pdf", "audit.5e6f7a8b.pdf"}, "audit.pdf", 2)


def test_withheld_attachments_has_no_duplicate_request_keys():
    """A repeated key in the dict literal silently drops the earlier entry."""
    tree = ast.parse(Path(pd.__file__).read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == "WITHHELD_ATTACHMENTS"):
            keys = [k.value for k in node.value.keys]
            assert len(keys) == len(set(keys)), keys
            return
    raise AssertionError("WITHHELD_ATTACHMENTS not found")
