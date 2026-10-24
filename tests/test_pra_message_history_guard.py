# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""A downloaded Message History must be a PDF headed by its own request id.
The scraper has saved a portal error page as the PDF, and filed one request's
history and production under another id after clicking the wrong card; both
are rejected so the previous copy stands."""
import sys
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pra_download as pd  # noqa: E402


def _mh_pdf(heading: str) -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), heading)
    data = doc.tobytes()
    doc.close()
    return data


def test_own_history_passes():
    data = _mh_pdf("W000001-010126 - California Public Records Request")
    assert pd.message_history_problem(data, "W000001-010126") is None


def test_other_requests_history_is_rejected():
    data = _mh_pdf("W000002-010226 - California Public Records Request")
    problem = pd.message_history_problem(data, "W000001-010126")
    assert problem and "W000002-010226" in problem


def test_html_error_page_is_rejected():
    page = b"<!DOCTYPE html><html><body>Session expired</body></html>"
    assert pd.message_history_problem(page, "W000001-010126")


def test_committed_histories_match_their_folder():
    for folder in sorted(pd.PRA_ROOT.glob("W*")):
        mh = folder / f"{folder.name}_Message_History.pdf"
        if mh.is_file():
            problem = pd.message_history_problem(mh.read_bytes(), folder.name)
            assert problem is None, f"{mh}: {problem}"


def test_uppercase_pdf_extension_counts_as_existing(tmp_path):
    (tmp_path / "_INVIT_1.PDF").write_bytes(b"%PDF")
    (tmp_path / "letter.pdf").write_bytes(b"%PDF")
    assert pd.local_pdf_names(tmp_path) == {"_INVIT_1.PDF", "letter.pdf"}
