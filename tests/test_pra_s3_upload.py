"""Tests for scripts/pra_s3_upload.py.

The bucket is write-once, so a key that could name two different contents
would silently drop the second (its PUT comes back 412, read as "already
stored"). The key must carry the content hash; the filename stays last so
DuckDB globs by extension keep working. Every manifest is locked for years,
so one must only be written for a complete, changed, non-empty tree.
"""

import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import pra_s3_upload as u  # noqa: E402
from ocr_sidecar import sidecar_path_for  # noqa: E402

SHA = "ab" * 32


class PreconditionFailed(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}}


class FakeS3:
    """Enough of the S3 client for the upload paths, with the bucket policy's
    rule built in: every write must carry If-None-Match: *."""

    def __init__(self):
        self.objects, self.mpus, self.calls = {}, {}, []

    def _create(self, key, data, if_none_match):
        assert if_none_match == "*", "write without If-None-Match"
        if key in self.objects:
            raise PreconditionFailed()
        self.objects[key] = data

    def put_object(self, *, Bucket, Key, Body, ContentType, ChecksumSHA256, IfNoneMatch=None):
        data = Body if isinstance(Body, bytes) else Body.read()
        assert ChecksumSHA256 == base64.b64encode(hashlib.sha256(data).digest()).decode()
        self.calls.append(("put", Key))
        self._create(Key, data, IfNoneMatch)

    def create_multipart_upload(self, *, Bucket, Key, ContentType, ChecksumAlgorithm):
        upload_id = f"mpu{len(self.calls)}"
        self.mpus[upload_id] = {}
        self.calls.append(("create", Key))
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body, ChecksumSHA256):
        self.mpus[UploadId][PartNumber] = Body
        return {"ETag": f"etag{PartNumber}", "ChecksumSHA256": ChecksumSHA256}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload, IfNoneMatch=None):
        self.calls.append(("complete", Key))
        parts = self.mpus[UploadId]
        self._create(Key, b"".join(parts[p["PartNumber"]] for p in MultipartUpload["Parts"]), IfNoneMatch)
        del self.mpus[UploadId]

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self.calls.append(("abort", Key))
        self.mpus.pop(UploadId, None)

    def manifests(self):
        return sorted(k for k in self.objects if k.startswith("_manifests/"))


def _write(root, rel, data=b"x", age=3600):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    t = p.stat().st_mtime - age
    os.utime(p, (t, t))
    return p


def _sync(s3, root, ledger_path, **kw):
    logs = []
    ledger = u.Ledger(ledger_path, "b", log=logs.append)
    kw.setdefault("min_age", 600)
    return u.sync(s3, "b", "coll", root, ledger, workers=2, log=logs.append, **kw)


# --- keys ---------------------------------------------------------------------

def test_key_nests_hash_between_dir_and_filename():
    assert (u.object_key("pra-portals-ca", "nextrequest/h/25-70/files/123__Log.xlsx", SHA)
            == f"pra-portals-ca/nextrequest/h/25-70/files/{SHA}/123__Log.xlsx")


def test_key_for_file_at_root():
    assert u.object_key("pra-portals-ca", "MANIFEST.jsonl", SHA) == f"pra-portals-ca/{SHA}/MANIFEST.jsonl"


def test_changed_content_gets_a_new_key():
    rel = "W013261-081826/W013261-081826_Message_History.pdf"
    assert u.object_key("c", rel, "0" * 64) != u.object_key("c", rel, "1" * 64)


def test_defaults_match_setup_script():
    script = (SCRIPT_DIR / "setup_pra_assets_bucket.sh").read_text()
    assert re.search(r'REGION="\$\{PRA_S3_REGION:-([^}]+)\}"', script)[1] == u.DEFAULT_REGION
    assert re.search(r'PREFIX="\$\{PRA_S3_PREFIX:-([^}]+)\}"', script)[1] == u.DEFAULT_PREFIX


# --- file selection -------------------------------------------------------------

def test_collect_excludes_and_junk(tmp_path):
    for rel in ("discovery/found.json", "W1/letter.pdf", "W1/.DS_Store", "W1/._letter.pdf",
                "W1/~$draft.docx", "W1/big.zip.part", "W1/audit.xlsx.crdownload", "W1/x.tmp"):
        _write(tmp_path, rel)
    files, held, junk = u.collect(tmp_path, excludes=["discovery/*"])
    assert [rel for rel, _, _ in files] == ["W1/letter.pdf"]
    assert held == []
    assert len(junk) == 6


def test_collect_holds_back_recent_files(tmp_path):
    _write(tmp_path, "old.pdf", age=3600)
    _write(tmp_path, "fresh.pdf", age=5)
    files, held, _ = u.collect(tmp_path, min_age=600)
    assert [rel for rel, _, _ in files] == ["old.pdf"]
    assert [rel for rel, _ in held] == ["fresh.pdf"]


def test_collect_skips_symlinks(tmp_path):
    target = _write(tmp_path, "real.pdf")
    (tmp_path / "link.pdf").symlink_to(target)
    files, _, _ = u.collect(tmp_path)
    assert [rel for rel, _, _ in files] == ["real.pdf"]


def test_only_true_ocr_sidecars_are_skipped(tmp_path):
    base = _write(tmp_path, "W1/Audit.doc", b"audit bytes")
    sidecar = sidecar_path_for(base)
    _write(tmp_path, f"W1/{sidecar.name}", b"ocr text")
    # pra_download's collision rename, a dated name, and a stale sidecar all look alike.
    _write(tmp_path, "W1/notes.1a2b3c4d.txt", b"released notes")
    _write(tmp_path, "W1/log.20260918.txt", b"released log")
    _write(tmp_path, "W1/Audit.doc.00000000.txt", b"stale ocr")
    files, _, _ = u.collect(tmp_path, skip_ocr_sidecars=True)
    kept = {rel for rel, _, _ in files}
    assert f"W1/{sidecar.name}" not in kept
    assert {"W1/Audit.doc", "W1/notes.1a2b3c4d.txt", "W1/log.20260918.txt",
            "W1/Audit.doc.00000000.txt"} <= kept


# --- upload paths -----------------------------------------------------------------

def test_existing_key_reads_as_present(tmp_path):
    s3, p = FakeS3(), _write(tmp_path, "a.pdf", b"abc")
    sha = u.sha256_file(p)
    assert u.upload(s3, "b", "k", p, 3, sha) == "uploaded"
    assert u.upload(s3, "b", "k", p, 3, sha) == "present"
    assert s3.objects["k"] == b"abc"


def test_multipart_upload_and_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "PART_SIZE", 4)
    s3, p = FakeS3(), _write(tmp_path, "big.zip", b"0123456789")
    sha = u.sha256_file(p)
    assert u.upload(s3, "b", "k", p, 10, sha) == "uploaded"
    assert s3.objects["k"] == b"0123456789"
    assert u.upload(s3, "b", "k", p, 10, sha) == "present"
    assert s3.calls[-1] == ("abort", "k")  # the retry's parts don't linger
    assert s3.mpus == {}


def test_multipart_refuses_bytes_that_dont_match_the_key(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "PART_SIZE", 4)
    s3, p = FakeS3(), _write(tmp_path, "big.zip", b"0123456789")
    with pytest.raises(u.Changed):
        u.upload(s3, "b", "k", p, 10, "0" * 64)
    assert "k" not in s3.objects
    assert ("complete", "k") not in s3.calls
    assert s3.calls[-1] == ("abort", "k")


# --- ledger ---------------------------------------------------------------------

def test_ledger_survives_a_torn_last_line(tmp_path):
    path = tmp_path / "ledger.jsonl"
    good = json.dumps({"bucket": "b", "key": "k1"})
    path.write_text(good + "\n" + '{"bucket": "b", "ke')
    logs = []
    ledger = u.Ledger(path, "b", log=logs.append)
    assert ledger.stored == {"k1"}
    assert logs and "unreadable" in logs[0]
    ledger.record(key="k2", sha256=SHA, bytes=1, path="p", status="uploaded")
    assert u.Ledger(path, "b", log=lambda _: None).stored == {"k1", "k2"}


def test_ledger_ignores_other_buckets(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"bucket": "other", "key": "k"}) + "\n")
    assert u.Ledger(path, "b").stored == set()


# --- runs and manifests -----------------------------------------------------------

def test_full_run_writes_one_manifest_then_none_while_unchanged(tmp_path):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    _write(root, "W1/a.pdf", b"a")
    _write(root, "W1/b.pdf", b"b")
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 0
    (manifest,) = s3.manifests()
    rows = [json.loads(line) for line in s3.objects[manifest].decode().splitlines()]
    assert [r["rel_path"] for r in rows] == ["W1/a.pdf", "W1/b.pdf"]
    assert all(r["key"] in s3.objects for r in rows)

    puts_before = len(s3.calls)
    assert _sync(s3, root, ledger) == 0
    assert len(s3.calls) == puts_before  # nothing re-uploaded, no second manifest


def test_changed_file_gets_new_key_and_new_manifest(tmp_path):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    _write(root, "W1/history.pdf", b"v1")
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 0
    _write(root, "W1/history.pdf", b"v2")
    assert _sync(s3, root, ledger) == 0
    assert len(s3.manifests()) == 2
    stored = [k for k in s3.objects if k.startswith("coll/")]
    assert sorted(s3.objects[k] for k in stored) == [b"v1", b"v2"]


def test_held_back_file_blocks_the_manifest(tmp_path):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    _write(root, "done.pdf", age=3600)
    _write(root, "downloading.pdf", age=5)
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 3
    assert s3.manifests() == []
    assert any(k.endswith("/done.pdf") for k in s3.objects)


def test_file_vanishing_mid_run_holds_back_instead_of_crashing(tmp_path, monkeypatch):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    _write(root, "a.pdf", b"a")
    _write(root, "gone.pdf", b"g")
    real = u.sha256_file

    def flaky(path):
        if path.name == "gone.pdf":
            raise FileNotFoundError(path)
        return real(path)

    monkeypatch.setattr(u, "sha256_file", flaky)
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 3
    assert any(k.endswith("/a.pdf") for k in s3.objects)
    assert s3.manifests() == []


def test_empty_root_is_refused(tmp_path):
    (tmp_path / "tree").mkdir()
    s3 = FakeS3()
    assert _sync(s3, tmp_path / "tree", tmp_path / "ledger.jsonl") == 2
    assert s3.objects == {}


def test_tree_that_shrank_sharply_is_refused_before_uploading(tmp_path):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    for i in range(10):
        _write(root, f"W{i}/a.pdf", str(i).encode())
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 0
    wrong_root = root / "W0"
    _write(wrong_root, "new.pdf", b"new")
    calls = len(s3.calls)
    assert _sync(s3, wrong_root, ledger) == 2
    assert len(s3.calls) == calls
    assert _sync(s3, wrong_root, ledger, allow_shrink=True) == 0


def test_upload_error_blocks_the_manifest(tmp_path, monkeypatch):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    _write(root, "a.pdf", b"a")

    def broken(*args):
        raise OSError("network down")

    monkeypatch.setattr(u, "upload", broken)
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 1
    assert s3.manifests() == []


def test_interrupt_aborts_multipart_between_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "PART_SIZE", 4)
    s3, p = FakeS3(), _write(tmp_path, "big.zip", b"0123456789")
    u.STOP.set()
    try:
        with pytest.raises(u.Stopped):
            u.upload(s3, "b", "k", p, 10, u.sha256_file(p))
    finally:
        u.STOP.clear()
    assert "k" not in s3.objects
    assert s3.calls[-1] == ("abort", "k")
