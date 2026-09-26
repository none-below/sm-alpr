"""Tests for scripts/pra_s3_upload.py.

The bucket is write-once, so a key that could name two different contents
would silently drop the second (its PUT comes back 412, read as "already
stored"). The key must carry the content hash; the filename stays last so
DuckDB globs by extension keep working.
"""

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from pra_s3_upload import collect, object_key  # noqa: E402

SHA = "ab" * 32


def test_key_nests_hash_between_dir_and_filename():
    assert (object_key("pra-portals-ca", "nextrequest/h/25-70/files/123__Log.xlsx", SHA)
            == f"pra-portals-ca/nextrequest/h/25-70/files/{SHA}/123__Log.xlsx")


def test_key_for_file_at_root():
    assert object_key("pra-portals-ca", "MANIFEST.jsonl", SHA) == f"pra-portals-ca/{SHA}/MANIFEST.jsonl"


def test_changed_content_gets_a_new_key():
    rel = "W013261-081826/W013261-081826_Message_History.pdf"
    assert object_key("c", rel, "0" * 64) != object_key("c", rel, "1" * 64)


def _touch(root, rel, age=3600):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    t = 1_000_000 - age
    os.utime(p, (t, t))
    return p


def test_collect_applies_excludes_and_sidecar_filter(tmp_path):
    _touch(tmp_path, "discovery/nextrequest_found.json")
    _touch(tmp_path, "W1/letter.pdf")
    _touch(tmp_path, "W1/letter.pdf.1d511874.txt")
    _touch(tmp_path, "W1/released_notes.txt")
    _touch(tmp_path, "W1/.DS_Store")
    files, too_new = collect(tmp_path, excludes=["discovery/*"], skip_ocr_sidecars=True, now=1_000_000)
    assert [rel for rel, _, _ in files] == ["W1/letter.pdf", "W1/released_notes.txt"]
    assert too_new == []


def test_collect_keeps_sidecars_unless_asked(tmp_path):
    _touch(tmp_path, "W1/letter.pdf.1d511874.txt")
    files, _ = collect(tmp_path, now=1_000_000)
    assert [rel for rel, _, _ in files] == ["W1/letter.pdf.1d511874.txt"]


def test_collect_holds_back_files_still_being_written(tmp_path):
    _touch(tmp_path, "old.pdf", age=3600)
    _touch(tmp_path, "fresh.pdf", age=5)
    files, too_new = collect(tmp_path, min_age=600, now=1_000_000)
    assert [rel for rel, _, _ in files] == ["old.pdf"]
    assert too_new == ["fresh.pdf"]


def test_collect_skips_symlinks(tmp_path):
    target = _touch(tmp_path, "real.pdf")
    (tmp_path / "link.pdf").symlink_to(target)
    files, _ = collect(tmp_path, now=1_000_000)
    assert [rel for rel, _, _ in files] == ["real.pdf"]
