# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""`ocr_sidecar.py --files` gets every asset a change touches, including ones
it deletes. A deleted path has nothing to OCR and must not crash the step."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ocr_sidecar  # noqa: E402


def test_files_mode_skips_deleted_paths(monkeypatch, tmp_path, capsys):
    gone = tmp_path / "deleted-in-this-change.pdf"
    monkeypatch.setattr(sys, "argv", ["ocr_sidecar.py", "--files", str(gone)])
    ocr_sidecar.main()
    assert "0 written, 0 skipped" in capsys.readouterr().out


def _text_pdf(path):
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Plate Reads Retained 1,234")
    doc.save(path)


def test_portal_render_pdfs_get_no_sidecar(tmp_path):
    control = tmp_path / "assets/public-records/W1/doc.pdf"
    control.parent.mkdir(parents=True)
    _text_pdf(control)
    assert ocr_sidecar.generate_sidecar(control) is not None  # text PDFs elsewhere do get one
    for d in ("assets/transparency.flocksafety.com/x-ca-pd", "assets/evidence.com/xpdca"):
        pdf = tmp_path / d / "2026-10-13.pdf"
        pdf.parent.mkdir(parents=True)
        _text_pdf(pdf)
        assert ocr_sidecar.generate_sidecar(pdf) is None
        assert list(pdf.parent.iterdir()) == [pdf]
