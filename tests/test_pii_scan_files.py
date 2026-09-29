# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""`pii_scan.py --files` gets every PDF a change touches, including ones it
deletes. A deleted path has nothing to scan and must not crash the scan."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pii_scan  # noqa: E402


def test_files_mode_skips_deleted_paths(monkeypatch, tmp_path, capsys):
    gone = tmp_path / "deleted-in-this-change.pdf"
    monkeypatch.setattr(sys, "argv", ["pii_scan.py", "--files", str(gone)])
    with pytest.raises(SystemExit) as exc:
        pii_scan.main()
    assert exc.value.code == 0
    assert "0 PDFs clean" in capsys.readouterr().out
