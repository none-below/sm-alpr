# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""A produced file listed in WITHHELD_ATTACHMENTS (e.g. one the agency released
with unredacted third-party PII) is matched by content hash, so the exact bytes
are withheld while a corrected re-upload under the same filename gets through."""
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pra_download as pd  # noqa: E402

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _withhold(monkeypatch, request_id, data, reason="third-party PII"):
    monkeypatch.setattr(pd, "WITHHELD_ATTACHMENTS",
                        {request_id: {hashlib.sha256(data).hexdigest(): reason}})


def test_matching_bytes_are_withheld(tmp_path, monkeypatch):
    _withhold(monkeypatch, "W000001-010126", b"AS-RELEASED")
    f = tmp_path / "Incident.pdf"
    f.write_bytes(b"AS-RELEASED")
    assert pd.withheld_reason("W000001-010126", f) == "third-party PII"


def test_corrected_copy_with_same_name_passes(tmp_path, monkeypatch):
    _withhold(monkeypatch, "W000001-010126", b"AS-RELEASED")
    f = tmp_path / "Incident.pdf"
    f.write_bytes(b"CORRECTED-REUPLOAD")
    assert pd.withheld_reason("W000001-010126", f) is None


def test_other_request_is_not_withheld(tmp_path, monkeypatch):
    _withhold(monkeypatch, "W000001-010126", b"AS-RELEASED")
    f = tmp_path / "Incident.pdf"
    f.write_bytes(b"AS-RELEASED")
    assert pd.withheld_reason("W000002-010126", f) is None


def test_registered_hashes_are_sha256():
    for request_id, withheld in pd.WITHHELD_ATTACHMENTS.items():
        assert pd.REQUEST_ID_RE.match(request_id), request_id
        for digest, reason in withheld.items():
            assert SHA256_RE.match(digest), (request_id, digest)
            assert reason


def test_no_withheld_file_is_committed():
    for request_id, withheld in pd.WITHHELD_ATTACHMENTS.items():
        folder = pd.PRA_ROOT / request_id
        if not folder.is_dir():
            continue
        for f in folder.iterdir():
            if f.is_file():
                digest = hashlib.sha256(f.read_bytes()).hexdigest()
                assert digest not in withheld, f"withheld file present: {f}"
