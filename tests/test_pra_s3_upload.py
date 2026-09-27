"""Tests for scripts/pra_s3_upload.py.

The bucket is write-once, so a key that could name two different contents
would silently drop the second (its PUT comes back 412, read as "already
stored"). The key must carry the content hash; the filename stays last so
DuckDB globs by extension keep working. Every manifest is locked for years
and claims to be the whole tree, so one must only be written for a complete,
settled, changed, non-empty tree.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
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

    def stored_names(self):
        return sorted(k.rsplit("/", 1)[1] for k in self.objects if not k.startswith("_manifests/"))


def _write(root, rel, data=b"x", age=3600):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def _sync(s3, root, ledger_path, **kw):
    logs = []
    ledger = u.Ledger(ledger_path, "b", log=logs.append)
    kw.setdefault("min_age", 600)
    return u.sync(s3, "b", "coll", root, ledger, workers=2, log=logs.append, **kw)


def _sha(p):
    return u.file_digests(p)[0]


# --- keys, names, collections -------------------------------------------------------

def test_key_nests_hash_between_dir_and_filename():
    assert (u.object_key("pra-portals-ca", "nextrequest/h/25-70/files/123__Log.xlsx", SHA)
            == f"pra-portals-ca/nextrequest/h/25-70/files/{SHA}/123__Log.xlsx")


def test_key_for_file_at_root():
    assert u.object_key("pra-portals-ca", "MANIFEST.jsonl", SHA) == f"pra-portals-ca/{SHA}/MANIFEST.jsonl"


def test_changed_content_gets_a_new_key():
    rel = "W013261-081826/W013261-081826_Message_History.pdf"
    assert u.object_key("c", rel, "0" * 64) != u.object_key("c", rel, "1" * 64)


@pytest.mark.parametrize("env", [{}, {"PRA_S3_REGION": "eu-north-1", "PRA_S3_PREFIX": "other-pfx"}])
def test_bucket_name_matches_setup_script(env, monkeypatch):
    """Both scripts derive the bucket name; run the shell's own derivation."""
    shell_env = {k: v for k, v in os.environ.items() if not k.startswith("PRA_S3_")}
    shell_env |= env | {"ACCOUNT_ID": "111122223333"}
    out = subprocess.run(["bash", str(SCRIPT_DIR / "setup_pra_assets_bucket.sh"), "--print-policies"],
                         env=shell_env, capture_output=True, text=True, check=True).stdout
    for k in ("PRA_S3_REGION", "PRA_S3_PREFIX"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert out.splitlines()[0] == f"# bucket: {u.bucket_name('111122223333')}"


def test_collections_are_well_formed():
    for name, cfg in u.COLLECTIONS.items():
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
        assert cfg["base"] in ("checkout", "primary")
        assert set(cfg) <= {"base", "root", "exclude", "skip_ocr_sidecars"}
        if cfg["base"] == "checkout":  # tracked folders must exist in every checkout
            assert u.collection_root(name).is_dir(), name


# --- file selection -------------------------------------------------------------

def test_metadata_is_ignored_but_unfinished_work_holds_the_tree(tmp_path):
    for rel in ("discovery/found.json", "W1/letter.pdf", "W1/.DS_Store", "W1/._letter.pdf",
                "W1/Thumbs.db", "W1/~$draft.docx", "W1/big.zip.part", "W1/audit.xlsx.crdownload",
                "W1/x.tmp"):
        _write(tmp_path, rel)
    files, held, ignored = u.collect(tmp_path, excludes=["discovery/*"])
    assert [rel for rel, _, _ in files] == ["W1/letter.pdf"]
    assert sorted(ignored) == ["W1/.DS_Store", "W1/._letter.pdf", "W1/Thumbs.db"]
    assert sorted(rel for rel, _ in held) == [
        "W1/audit.xlsx.crdownload", "W1/big.zip.part", "W1/x.tmp", "W1/~$draft.docx"]


def test_recent_files_are_held_but_future_dated_ones_are_not(tmp_path):
    _write(tmp_path, "old.pdf", age=3600)
    _write(tmp_path, "fresh.pdf", age=5)
    _write(tmp_path, "restored_from_zip.pdf", age=-86400)  # mtime a day ahead
    files, held, _ = u.collect(tmp_path, min_age=600)
    assert [rel for rel, _, _ in files] == ["old.pdf", "restored_from_zip.pdf"]
    assert [rel for rel, _ in held] == ["fresh.pdf"]


def test_symlinks_hold_the_tree_instead_of_vanishing(tmp_path):
    target = _write(tmp_path, "real.pdf")
    (tmp_path / "link.pdf").symlink_to(target)
    (tmp_path / "linkdir").symlink_to(tmp_path)
    files, held, _ = u.collect(tmp_path)
    assert [rel for rel, _, _ in files] == ["real.pdf"]
    assert sorted(rel for rel, _ in held) == ["link.pdf", "linkdir"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_folder_holds_the_tree(tmp_path):
    _write(tmp_path, "ok.pdf")
    _write(tmp_path, "locked/secret.pdf")
    (tmp_path / "locked").chmod(0)
    try:
        files, held, _ = u.collect(tmp_path)
    finally:
        (tmp_path / "locked").chmod(0o755)
    assert [rel for rel, _, _ in files] == ["ok.pdf"]
    assert held and held[0][0] == "locked" and "unreadable" in held[0][1]


def test_excluded_folders_are_not_walked(tmp_path):
    _write(tmp_path, "keep.pdf")
    _write(tmp_path, "discovery/locked/x.json")
    (tmp_path / "discovery" / "locked").chmod(0)
    try:
        files, held, _ = u.collect(tmp_path, excludes=["discovery/*"])
    finally:
        (tmp_path / "discovery" / "locked").chmod(0o755)
    assert [rel for rel, _, _ in files] == ["keep.pdf"] and held == []


# --- OCR sidecars ------------------------------------------------------------------

def test_only_true_ocr_sidecars_are_skipped(tmp_path):
    root = tmp_path / "tree"
    base = _write(root, "W1/Audit.doc", b"audit bytes")
    sidecar = sidecar_path_for(base)
    _write(root, f"W1/{sidecar.name}", b"ocr text")
    # pra_download's collision rename, a dated name, and a stale sidecar all look alike.
    _write(root, "W1/notes.1a2b3c4d.txt", b"released notes")
    _write(root, "W1/log.20260918.txt", b"released log")
    _write(root, "W1/Audit.doc.00000000.txt", b"stale ocr")
    s3 = FakeS3()
    assert _sync(s3, root, tmp_path / "ledger.jsonl", skip_ocr_sidecars=True) == 0
    assert s3.stored_names() == sorted([
        "Audit.doc", "notes.1a2b3c4d.txt", "log.20260918.txt", "Audit.doc.00000000.txt"])


def test_sidecar_of_a_held_back_file_waits(tmp_path):
    root = tmp_path / "tree"
    base = _write(root, "W1/Audit.doc", b"audit bytes", age=5)  # still settling
    _write(root, f"W1/{sidecar_path_for(base).name}", b"ocr text")
    s3 = FakeS3()
    assert _sync(s3, root, tmp_path / "ledger.jsonl", skip_ocr_sidecars=True) == 3
    assert s3.objects == {}


# --- upload paths -----------------------------------------------------------------

def test_existing_key_reads_as_present(tmp_path):
    s3, p = FakeS3(), _write(tmp_path, "a.pdf", b"abc")
    assert u.upload(s3, "b", "k", p, 3, _sha(p)) == "uploaded"
    assert u.upload(s3, "b", "k", p, 3, _sha(p)) == "present"
    assert s3.objects["k"] == b"abc"


def test_multipart_upload_and_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "PART_SIZE", 4)
    s3, p = FakeS3(), _write(tmp_path, "big.zip", b"0123456789")
    assert u.upload(s3, "b", "k", p, 10, _sha(p)) == "uploaded"
    assert s3.objects["k"] == b"0123456789"
    assert u.upload(s3, "b", "k", p, 10, _sha(p)) == "present"
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


def test_interrupt_aborts_multipart_between_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "PART_SIZE", 4)
    s3, p = FakeS3(), _write(tmp_path, "big.zip", b"0123456789")
    u.STOP.set()
    try:
        with pytest.raises(u.Stopped):
            u.upload(s3, "b", "k", p, 10, _sha(p))
    finally:
        u.STOP.clear()
    assert "k" not in s3.objects
    assert s3.calls[-1] == ("abort", "k")


# --- ledger ---------------------------------------------------------------------

def test_ledger_survives_a_torn_last_line_without_writing_on_read(tmp_path):
    path = tmp_path / "ledger.jsonl"
    torn = json.dumps({"bucket": "b", "key": "k1"}) + "\n" + '{"bucket": "b", "ke'
    path.write_text(torn)
    logs = []
    ledger = u.Ledger(path, "b", log=logs.append)
    assert ledger.stored == {"k1"}
    assert logs and "unreadable" in logs[0]
    assert path.read_text() == torn  # reading never writes
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

    calls = len(s3.calls)
    assert _sync(s3, root, ledger) == 0
    assert len(s3.calls) == calls  # nothing re-uploaded, no second manifest


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
    assert s3.stored_names() == ["done.pdf"]


def test_unfinished_download_blocks_the_manifest(tmp_path):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    _write(root, "done.pdf")
    _write(root, "big.zip.crdownload")
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 3
    assert s3.manifests() == [] and s3.stored_names() == ["done.pdf"]


def test_file_vanishing_before_hashing_holds_back(tmp_path, monkeypatch):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    _write(root, "a.pdf", b"a")
    _write(root, "gone.pdf", b"g")
    real = u.file_digests

    def flaky(path, md5=False):
        if path.name == "gone.pdf":
            raise FileNotFoundError(path)
        return real(path, md5)

    monkeypatch.setattr(u, "file_digests", flaky)
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 3
    assert s3.stored_names() == ["a.pdf"] and s3.manifests() == []


def test_file_changing_during_upload_holds_back_not_errors(tmp_path, monkeypatch):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    p = _write(root, "report.pdf", b"v1")

    def rewrite_then_fail(s3, bucket, key, path, size, sha256):
        _write(root, "report.pdf", b"v2 is longer", age=0)
        raise OSError("BadDigest: the SHA256 you specified did not match")

    monkeypatch.setattr(u, "upload", rewrite_then_fail)
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 3
    assert p.read_bytes() == b"v2 is longer" and s3.manifests() == []


def test_file_appearing_during_the_run_blocks_the_manifest(tmp_path, monkeypatch):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    _write(root, "a.pdf", b"a")
    real = u.upload

    def upload_and_drop_in_a_new_file(*args):
        _write(root, "late.pdf", b"late", age=3600)  # old mtime: only the re-check catches it
        return real(*args)

    monkeypatch.setattr(u, "upload", upload_and_drop_in_a_new_file)
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 3
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


def test_dry_run_predicts_the_real_status_and_writes_nothing(tmp_path):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    _write(root, "done.pdf")
    s3 = FakeS3()
    assert _sync(s3, root, ledger, dry_run=True) == 0
    _write(root, "downloading.pdf", age=5)
    assert _sync(s3, root, ledger, dry_run=True) == 3
    assert s3.calls == [] and not ledger.exists()
