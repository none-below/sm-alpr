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
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import pra_s3_upload as u  # noqa: E402
from ocr_sidecar import is_sidecar, sidecar_path_for  # noqa: E402

SHA = "ab" * 32


def _b64sha(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


class PreconditionFailed(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}}


class FakeS3:
    """Enough of the S3 client for the upload paths, with the bucket policy's
    rule built in: every write must carry If-None-Match: *."""

    def __init__(self):
        self.objects, self.mpus, self.calls, self.headers = {}, {}, [], {}

    def _create(self, key, data, if_none_match):
        assert if_none_match == "*", "write without If-None-Match"
        if key in self.objects:
            raise PreconditionFailed()
        self.objects[key] = data

    def put_object(self, *, Bucket, Key, Body, ContentType, ChecksumAlgorithm, ChecksumSHA256,
                   IfNoneMatch=None, ContentDisposition=None):
        data = Body if isinstance(Body, bytes) else Body.read()
        assert ChecksumAlgorithm == "SHA256"
        assert ChecksumSHA256 == _b64sha(data)
        self.calls.append(("put", Key))
        self._create(Key, data, IfNoneMatch)
        self.headers[Key] = {"ContentType": ContentType, "ContentDisposition": ContentDisposition}

    def create_multipart_upload(self, *, Bucket, Key, ContentType, ChecksumAlgorithm, ContentDisposition=None):
        assert ChecksumAlgorithm == "SHA256"
        upload_id = f"mpu{len(self.calls)}"
        self.mpus[upload_id] = {}
        self.calls.append(("create", Key))
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body, ChecksumSHA256):
        assert ChecksumSHA256 == _b64sha(Body), "part checksum doesn't match its bytes"
        self.mpus[UploadId][PartNumber] = Body
        self.calls.append(("part", Key))
        return {"ETag": f"etag{PartNumber}", "ChecksumSHA256": ChecksumSHA256}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload, IfNoneMatch=None):
        self.calls.append(("complete", Key))
        parts = self.mpus[UploadId]
        for p in MultipartUpload["Parts"]:
            assert p["ChecksumSHA256"] == _b64sha(parts[p["PartNumber"]]), "part checksum missing or wrong"
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


def _sync(s3, root, ledger_path, cache_path=None, **kw):
    logs = []
    ledger = u.Ledger(ledger_path, "b", log=logs.append)
    if cache_path is not None:
        kw["cache"] = u.HashCache(cache_path)
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


@pytest.mark.parametrize("env", [{}, {"PRA_S3_REGION": "eu-north-1", "PRA_S3_PREFIX": "other-pfx"},
                                 {"PRA_S3_REGION": "", "PRA_S3_PREFIX": ""}])
def test_bucket_name_matches_setup_script(env, monkeypatch):
    """Both scripts derive the bucket name; run the shell's own derivation."""
    shell_env = {k: v for k, v in os.environ.items() if not k.startswith("PRA_S3_")}
    shell_env |= env | {"PRA_S3_ACCOUNT_ID": "111122223333"}
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
        assert set(cfg) <= {"base", "root", "exclude", "keep", "skip_ocr_sidecars"}
        if cfg["base"] == "checkout":  # must be a tracked folder (it may be sparse on disk)
            tree = subprocess.run(["git", "-C", str(SCRIPT_DIR.parent), "ls-tree", "-d", "HEAD", cfg["root"]],
                                  capture_output=True, text=True).stdout
            assert tree.strip(), name


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


def test_safari_download_folder_holds_the_tree(tmp_path):
    _write(tmp_path, "done.pdf")
    _write(tmp_path, "report.pdf.download/report.pdf", b"partial")
    files, held, _ = u.collect(tmp_path)
    assert [rel for rel, _, _ in files] == ["done.pdf"]
    assert held == [("report.pdf.download", "unfinished download folder")]


def test_keep_lets_a_real_file_with_a_download_like_name_through(tmp_path):
    _write(tmp_path, "export.part")
    files, held, _ = u.collect(tmp_path, keep=["export.part"])
    assert [rel for rel, _, _ in files] == ["export.part"] and held == []


def test_tracked_mode_ignores_untracked_and_mtimes(tmp_path):
    _write(tmp_path, "W1/a.pdf", age=0)  # fresh checkout: every mtime is "now"
    _write(tmp_path, "W1/export.part", age=0)  # tracked, so finished whatever its name
    _write(tmp_path, "W1/untracked.png")
    tracked = {"W1/a.pdf", "W1/export.part", "W1/sparse_only.pdf"}
    files, held, _ = u.collect(tmp_path, min_age=600, tracked=tracked)
    assert [rel for rel, _, _ in files] == ["W1/a.pdf", "W1/export.part"]
    assert held == [("W1/sparse_only.pdf", "tracked but not on disk (sparse checkout?)")]


# --- OCR sidecars ------------------------------------------------------------------

def test_sidecar_rule_tells_sidecars_from_collision_renames():
    own = hashlib.md5(b"released text").hexdigest()
    assert is_sidecar("Audit.doc.1d511874.txt", "f" * 32)  # current or stale: base's MD5
    assert is_sidecar("Gone.pdf.1d511874.txt", "f" * 32)  # orphaned: base removed
    assert not is_sidecar(f"scan.pdf.{own[:8]}.txt", own)  # pra_download rename: its own MD5
    assert not is_sidecar("notes.1a2b3c4d.txt", "f" * 32)  # base has no OCR'd extension
    assert not is_sidecar("log.20260918.txt", "f" * 32)
    assert not is_sidecar("Audit.doc", "f" * 32)


def test_stale_and_orphaned_sidecars_are_skipped_real_files_kept(tmp_path):
    root = tmp_path / "tree"
    base = _write(root, "W1/Audit.doc", b"audit bytes")
    _write(root, f"W1/{sidecar_path_for(base).name}", b"ocr text")
    _write(root, "W1/Audit.doc.00000000.txt", b"stale ocr")
    _write(root, "W1/Withheld.pdf.1234abcd.txt", b"ocr of a withdrawn release")
    renamed = hashlib.md5(b"released scan text").hexdigest()[:8]
    _write(root, f"W1/scan.pdf.{renamed}.txt", b"released scan text")
    _write(root, "W1/notes.1a2b3c4d.txt", b"released notes")
    _write(root, "W1/log.20260918.txt", b"released log")
    s3 = FakeS3()
    assert _sync(s3, root, tmp_path / "ledger.jsonl", skip_ocr_sidecars=True) == 0
    assert s3.stored_names() == sorted([
        "Audit.doc", f"scan.pdf.{renamed}.txt", "notes.1a2b3c4d.txt", "log.20260918.txt"])


def test_sidecar_of_a_held_back_file_is_not_uploaded(tmp_path):
    root = tmp_path / "tree"
    base = _write(root, "W1/Audit.doc", b"audit bytes", age=5)  # still settling
    _write(root, f"W1/{sidecar_path_for(base).name}", b"ocr text")
    s3 = FakeS3()
    assert _sync(s3, root, tmp_path / "ledger.jsonl", skip_ocr_sidecars=True) == 3
    assert s3.objects == {}


def test_shrink_guard_counts_the_tree_without_sidecars(tmp_path):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    for i in range(10):
        base = _write(root, f"W{i}/doc.pdf", f"doc {i}".encode())
        _write(root, f"W{i}/{sidecar_path_for(base).name}", b"ocr")
    s3 = FakeS3()
    assert _sync(s3, root, ledger, skip_ocr_sidecars=True) == 0
    for i in range(6):  # lose 60% of the documents, sidecars and all
        for p in (root / f"W{i}").iterdir():
            p.unlink()
    calls = len(s3.calls)
    assert _sync(s3, root, ledger, skip_ocr_sidecars=True) == 2  # 8 files on disk, 4 in the tree
    assert len(s3.calls) == calls


# --- upload paths -----------------------------------------------------------------

def test_existing_key_reads_as_present(tmp_path):
    s3, p = FakeS3(), _write(tmp_path, "a.pdf", b"abc")
    assert u.upload(s3, "b", "k", p, 3, _sha(p)) == "uploaded"
    assert u.upload(s3, "b", "k", p, 3, _sha(p)) == "present"
    assert s3.objects["k"] == b"abc"


def test_multipart_upload_and_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "PART_SIZE", 4)
    monkeypatch.setattr(u, "MULTIPART_THRESHOLD", 4)
    s3, p = FakeS3(), _write(tmp_path, "big.zip", b"0123456789")
    assert u.upload(s3, "b", "k", p, 10, _sha(p)) == "uploaded"
    assert s3.objects["k"] == b"0123456789"
    assert u.upload(s3, "b", "k", p, 10, _sha(p)) == "present"
    assert s3.calls[-1] == ("abort", "k")  # the retry's parts don't linger
    assert s3.mpus == {}


def test_multipart_refuses_bytes_that_dont_match_the_key(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "PART_SIZE", 4)
    monkeypatch.setattr(u, "MULTIPART_THRESHOLD", 4)
    s3, p = FakeS3(), _write(tmp_path, "big.zip", b"0123456789")
    with pytest.raises(u.Changed):
        u.upload(s3, "b", "k", p, 10, "0" * 64)
    assert "k" not in s3.objects
    assert ("complete", "k") not in s3.calls
    assert s3.calls[-1] == ("abort", "k")


def test_interrupt_aborts_multipart_between_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "PART_SIZE", 4)
    monkeypatch.setattr(u, "MULTIPART_THRESHOLD", 4)
    s3, p = FakeS3(), _write(tmp_path, "big.zip", b"0123456789")
    sha = _sha(p)
    first_part = s3.upload_part

    def part_then_interrupt(**kw):
        result = first_part(**kw)
        u.STOP.set()  # Ctrl-C arrives while part 1 is in flight
        return result

    s3.upload_part = part_then_interrupt
    try:
        with pytest.raises(u.Stopped):
            u.upload(s3, "b", "k", p, 10, sha)
    finally:
        u.STOP.clear()
    assert [c for c in s3.calls if c[0] == "part"] == [("part", "k")]  # one part went, then it stopped
    assert "k" not in s3.objects and s3.calls[-1] == ("abort", "k")


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

    def flaky(path, md5=False, blob_size=None):
        if path.name == "gone.pdf":
            raise FileNotFoundError(path)
        return real(path, md5, blob_size)

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


def test_hash_cache_skips_unchanged_files_but_not_rewrites_that_keep_mtime(tmp_path, monkeypatch):
    root, ledger, cache = tmp_path / "tree", tmp_path / "ledger.jsonl", tmp_path / "cache.json"
    p = _write(root, "a.pdf", b"version 1")
    s3 = FakeS3()
    assert _sync(s3, root, ledger, cache) == 0
    reads = []
    real = u.file_digests
    monkeypatch.setattr(u, "file_digests",
                        lambda path, md5=False, blob_size=None: reads.append(path) or real(path, md5, blob_size))
    assert _sync(s3, root, ledger, cache) == 0
    assert reads == []  # identity unchanged: cached
    mtime = p.stat().st_mtime_ns
    p.write_bytes(b"version 2")  # same size, and put the old mtime back (rsync -t, cp -p)
    os.utime(p, ns=(mtime, mtime))
    assert _sync(s3, root, ledger, cache) == 0
    assert reads == [p] and len(s3.manifests()) == 2


def test_interrupt_stops_hashing_mid_file(tmp_path, monkeypatch):
    p = _write(tmp_path, "big.bin", b"x" * (3 << 20))  # three 1 MiB chunks
    real, chunks = hashlib.sha256, []

    class Sha:  # Ctrl-C arrives while the first chunk is being hashed
        def __init__(self):
            self.h = real()

        def update(self, b):
            chunks.append(len(b))
            self.h.update(b)
            u.STOP.set()

        def hexdigest(self):
            return self.h.hexdigest()

    monkeypatch.setattr(u.hashlib, "sha256", Sha)
    try:
        with pytest.raises(u.Stopped):
            u.file_digests(p)
    finally:
        u.STOP.clear()
    assert chunks == [1 << 20]  # stopped after one of three chunks


def test_line_separator_in_a_filename_keeps_its_ledger_row_whole(tmp_path):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    _write(root, "odd\u2028name.pdf", b"odd")
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 0
    calls = len(s3.calls)
    assert _sync(s3, root, ledger) == 0
    assert len(s3.calls) == calls  # recognised as stored, not re-sent


# --- checkout collections ----------------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "assets/prs/W1/a.pdf", b"a")
    _write(repo, "assets/prs/W1/b.pdf", b"b")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def test_tracked_files_are_the_committed_ones(repo):
    _write(repo, "assets/prs/W1/untracked_screenshot.png")
    files, commit = u.tracked_files(repo, "assets/prs", fetch=False)
    assert set(files) == {"W1/a.pdf", "W1/b.pdf"}
    assert commit == subprocess.run(["git", "-C", str(repo), "rev-parse", "origin/main"],
                                    capture_output=True, text=True).stdout.strip()


def test_dirty_checkout_is_refused(repo):
    _write(repo, "assets/prs/W1/a.pdf", b"edited")
    with pytest.raises(u.Refused, match="uncommitted"):
        u.tracked_files(repo, "assets/prs", fetch=False)


def test_stale_checkout_is_refused(repo):
    _write(repo, "assets/prs/W2/new.pdf", b"new")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "newer on main")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "HEAD~1")
    with pytest.raises(u.Refused, match="differs from origin/main"):
        u.tracked_files(repo, "assets/prs", fetch=False)


def test_fresh_checkout_uploads_without_waiting_and_records_the_commit(repo, tmp_path):
    root = repo / "assets/prs"
    tracked, commit = u.tracked_files(repo, "assets/prs", fetch=False)
    s3 = FakeS3()
    assert _sync(s3, root, tmp_path / "ledger.jsonl", tracked=tracked, commit=commit, min_age=600) == 0
    assert s3.stored_names() == ["a.pdf", "b.pdf"]
    (manifest,) = s3.manifests()
    assert {json.loads(r)["git_commit"] for r in s3.objects[manifest].decode().splitlines()} == {commit}


def test_only_precondition_failed_means_present():
    class Denied(Exception):
        response = {"Error": {"Code": "AccessDenied"}}

    class Timeout(Exception):  # botocore's ReadTimeoutError carries response=None
        response = None

    for exc in (Denied(), Timeout(), OSError("reset")):
        def write(exc=exc):
            raise exc
        with pytest.raises(type(exc)):
            u._once(write)
    assert u._once(lambda: None) == "uploaded"


def test_manifest_rows_are_exact(tmp_path):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    p = _write(root, "W1/a.pdf", b"alpha")
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 0
    (manifest,) = s3.manifests()
    assert manifest.startswith("_manifests/coll/") and manifest.endswith(".jsonl")
    (row,) = [json.loads(r) for r in s3.objects[manifest].decode().splitlines()]
    sha = hashlib.sha256(b"alpha").hexdigest()
    mtime = p.stat().st_mtime_ns / 1e9
    assert row == {"collection": "coll", "rel_path": "W1/a.pdf", "key": f"coll/W1/{sha}/a.pdf",
                   "sha256": sha, "bytes": 5,
                   "mtime": u.datetime.fromtimestamp(mtime, u.timezone.utc).isoformat()}
    assert manifest.rsplit("-", 1)[1] == u.tree_hash([("W1/a.pdf", sha)])[:12] + ".jsonl"


def test_browser_executable_types_are_stored_as_attachments(tmp_path):
    root = tmp_path / "tree"
    _write(root, "portal.html", b"<script>alert(1)</script>")
    _write(root, "letter.pdf", b"%PDF")
    s3 = FakeS3()
    assert _sync(s3, root, tmp_path / "ledger.jsonl") == 0
    by_name = {k.rsplit("/", 1)[1]: h for k, h in s3.headers.items()}
    assert by_name["portal.html"] == {"ContentType": "text/html", "ContentDisposition": "attachment"}
    assert by_name["letter.pdf"] == {"ContentType": "application/pdf", "ContentDisposition": None}


# --- re-walk after upload --------------------------------------------------------

def _entries(root, rels):
    files, _, _ = u.collect(root, min_age=None)
    return [u._hash(item, "c", False, None)[0]
            for item in files if item[0] in rels]


def test_rewalk_reports_vanished_changed_appeared_and_unfinished(tmp_path):
    root = tmp_path / "tree"
    for rel in ("keep.pdf", "gone.pdf", "edited.pdf"):
        _write(root, rel, rel.encode())
    entries = _entries(root, {"keep.pdf", "gone.pdf", "edited.pdf"})
    (root / "gone.pdf").unlink()
    (root / "edited.pdf").write_bytes(b"edited")
    _write(root, "late.pdf")
    _write(root, "next.zip.part")
    assert dict(u.changes_since(root, (), entries, set())) == {
        "gone.pdf": "vanished during the run",
        "edited.pdf": "changed during the run",
        "late.pdf": "appeared during the run",
        "next.zip.part": "unfinished download or open document (list it in keep if it's real)",
    }


def test_rewalk_ignores_the_clock(tmp_path):
    root = tmp_path / "tree"
    _write(root, "future.pdf", age=-30)  # a future mtime drifting into the --min-age window
    entries = _entries(root, {"future.pdf"})
    assert u.changes_since(root, (), entries, set()) == []


# --- unreadable things -------------------------------------------------------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_listable_but_unenterable_folder_is_held_not_a_crash(tmp_path):
    _write(tmp_path, "ok.pdf")
    _write(tmp_path, "noexec/x.pdf")
    (tmp_path / "noexec").chmod(0o444)  # names readable, entries can't be stat'ed
    try:
        files, held, _ = u.collect(tmp_path)
    finally:
        (tmp_path / "noexec").chmod(0o755)
    assert [rel for rel, _, _ in files] == ["ok.pdf"]
    assert held and all("unreadable" in reason for _, reason in held)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_file_is_held_back_not_an_error(tmp_path):
    root = tmp_path / "tree"
    _write(root, "ok.pdf")
    locked = _write(root, "locked.pdf")
    locked.chmod(0)
    try:
        assert _sync(FakeS3(), root, tmp_path / "ledger.jsonl") == 3
    finally:
        locked.chmod(0o644)


def test_shrink_guard_waits_for_a_complete_tree(tmp_path):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    for i in range(10):
        _write(root, f"W{i}/a.pdf", str(i).encode())
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 0
    for i in range(6):
        (root / f"W{i}/a.pdf").unlink()
    _write(root, "W9/b.zip.crdownload")  # something unfinished: not a complete tree yet
    assert _sync(s3, root, ledger) == 3  # held back, not refused as a shrink


# --- setup script, run against a stand-in aws command --------------------------------

AWS_STUB = r"""
import csv, json, os, sys
path = os.environ["AWS_STUB_STATE"]
st = json.load(open(path))
argv = sys.argv[1:]
st["calls"].append(argv)


def done(out="", code=0, err=""):
    json.dump(st, open(path, "w"))
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)


def fail(code):
    done(code=254, err=f"An error occurred ({code}) when calling the operation: stub\n")


def opt(name):
    return argv[argv.index(name) + 1]


NOT_FOUND = {
    "get-public-access-block": "NoSuchPublicAccessBlockConfiguration",
    "get-bucket-ownership-controls": "OwnershipControlsNotFoundError",
    "get-bucket-encryption": "ServerSideEncryptionConfigurationNotFoundError",
    "get-object-lock-configuration": "ObjectLockConfigurationNotFoundError",
    "get-bucket-lifecycle-configuration": "NoSuchLifecycleConfiguration",
    "get-bucket-policy": "NoSuchBucketPolicy",
}
PUTS = {
    "put-public-access-block": ("get-public-access-block", "--public-access-block-configuration", None),
    "put-bucket-ownership-controls": ("get-bucket-ownership-controls", "--ownership-controls", None),
    "put-bucket-encryption": ("get-bucket-encryption", "--server-side-encryption-configuration", None),
    "put-object-lock-configuration": ("get-object-lock-configuration", "--object-lock-configuration", "Rule"),
    "put-bucket-lifecycle-configuration": ("get-bucket-lifecycle-configuration", "--lifecycle-configuration", "Rules"),
    "put-bucket-policy": ("get-bucket-policy", "--policy", None),
}
svc, cmd = argv[0], argv[1]
if cmd in st["errors"]:
    fail(st["errors"][cmd])
if svc == "sts" and cmd == "get-caller-identity":
    done("111122223333\n")
if svc == "s3api":
    if cmd == "head-bucket":
        done("{}") if st["bucket"] else fail("404")
    if cmd == "create-bucket":  # what a new bucket really comes with (seen live, 2026-09)
        st["bucket"] = True
        st["settings"].update({
            "get-public-access-block": {"BlockPublicAcls": True, "IgnorePublicAcls": True,
                                        "BlockPublicPolicy": True, "RestrictPublicBuckets": True},
            "get-bucket-ownership-controls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
            "get-bucket-encryption": {"Rules": [{
                "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                "BucketKeyEnabled": False,
                "BlockedEncryptionTypes": {"EncryptionType": ["SSE-C"]}}]},
        })
        if "--object-lock-enabled-for-bucket" in argv:
            st["settings"]["get-object-lock-configuration"] = None  # enabled, no default rule
        done("{}")
    if cmd in NOT_FOUND:
        if cmd not in st["settings"]:
            fail(NOT_FOUND[cmd])
        value = st["settings"][cmd]
        done(value if cmd == "get-bucket-policy" else json.dumps(value, indent=4))
    if cmd in PUTS:
        get, arg, sub = PUTS[cmd]
        value = opt(arg) if cmd == "put-bucket-policy" else json.loads(opt(arg))
        st["settings"][get] = value[sub] if sub else value
        done()
if svc == "iam":
    user = opt("--user-name")
    u = st["users"].get(user)
    if cmd == "get-user":
        done("{}") if u else fail("NoSuchEntity")
    if cmd == "create-user":
        st["users"][user] = {"policy": None, "keys": []}
        done("{}")
    if cmd == "get-user-policy":
        done(json.dumps(u["policy"])) if u and u["policy"] else fail("NoSuchEntity")
    if cmd == "put-user-policy":
        u["policy"] = json.loads(opt("--policy-document"))
        u["policy_name"] = opt("--policy-name")
        done()
    if cmd == "list-attached-user-policies":
        done("\t".join(u.get("managed", [])) + "\n")
    if cmd == "list-user-policies":
        done("\t".join(([u["policy_name"]] if u.get("policy") else []) + u.get("inline", [])) + "\n")
    if cmd == "list-groups-for-user":
        done("\t".join(u.get("groups", [])) + "\n")
    if cmd == "list-access-keys":
        done("\t".join(u["keys"]) + "\n")
    if cmd == "create-access-key":
        key_id = f"AKIASTUB{len(st['calls'])}"
        u["keys"].append(key_id)
        done(f"{key_id}\tSECRET-{key_id}\n")
    if cmd == "delete-access-key":
        u["keys"].remove(opt("--access-key-id"))
        done()
if svc == "configure":
    if cmd == "get" and argv[2] == "cli_history":
        done(st["cli_history"] + "\n") if st.get("cli_history") else done(code=1)
    if cmd == "get":
        key = st["profiles"].get(opt("--profile"))
        done(key + "\n") if key else done(code=1)
    if cmd == "import":
        if st.get("import_fails"):
            done(code=1, err="stub: import failed\n")
        for row in csv.DictReader(sys.stdin):
            st["profiles"][row["User name"]] = row["Access key ID"]
        done()
    if cmd == "set":
        done()
done(code=99, err=f"stub: unhandled {argv}\n")
"""


def _setup(tmp_path, state=None, *args, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    aws = bin_dir / "aws"
    aws.write_text(f"#!{sys.executable}\n{AWS_STUB}")
    aws.chmod(0o755)
    state_path = tmp_path / "aws_state.json"
    if state is None:  # carry on from the last run's state, or start empty
        state = json.loads(state_path.read_text()) if state_path.exists() else {
            "bucket": False, "settings": {}, "users": {}, "profiles": {}, "errors": {}}
    state_path.write_text(json.dumps({**state, "calls": []}))  # each run gets its own call log
    run_env = {k: v for k, v in os.environ.items()
               if not k.startswith(("AWS_", "PRA_S3_", "REAPPLY", "MINT_KEYS", "ACCOUNT_ID"))}
    run_env |= {"PATH": f"{bin_dir}:{Path(sys.executable).parent}:/usr/bin:/bin",
                "AWS_STUB_STATE": str(state_path), **env}
    r = subprocess.run(["bash", str(SCRIPT_DIR / "setup_pra_assets_bucket.sh"), *args],
                       env=run_env, capture_output=True, text=True)
    return r, json.loads(state_path.read_text())


def _writes(state):
    return [c for c in state["calls"] if c[1].startswith(("put-", "create-", "delete-")) or c[0] == "configure" and c[1] == "import"]


def test_setup_first_run_sets_everything_then_rerun_is_all_ok(tmp_path):
    r, st = _setup(tmp_path)
    assert r.returncode == 0, r.stderr
    # A new bucket already has AWS's defaults for these three; they match the script's.
    for name in ("public access block", "object ownership", "encryption"):
        assert f"{name}: ok" in r.stdout
    for name in ("object lock default retention", "lifecycle rules", "bucket policy"):
        assert f"{name}: set" in r.stdout
    assert st["bucket"] and len(st["settings"]) == 6
    assert all(u_["policy"] for u_ in st["users"].values())
    r, st = _setup(tmp_path)
    assert r.returncode == 0 and r.stdout.count(": ok") == 8
    assert _writes(st) == []


def test_setup_leaves_a_weakened_policy_and_says_so(tmp_path):
    _setup(tmp_path)
    st = json.loads((tmp_path / "aws_state.json").read_text())
    policy = json.loads(st["settings"]["get-bucket-policy"])
    policy["Statement"][1]["Condition"]["StringNotEquals"] = {"aws:username": "someone"}
    weakened = json.dumps(policy)
    st["settings"]["get-bucket-policy"] = weakened
    r, st = _setup(tmp_path, st)
    assert r.returncode == 3 and "bucket policy: differs" in r.stderr
    assert st["settings"]["get-bucket-policy"] == weakened and _writes(st) == []
    r, st = _setup(tmp_path, None, REAPPLY="1")
    assert r.returncode == 0 and "Condition" in st["settings"]["get-bucket-policy"]
    assert "someone" not in st["settings"]["get-bucket-policy"]


def test_setup_stops_on_a_failed_read_before_writing(tmp_path):
    _setup(tmp_path)
    st = json.loads((tmp_path / "aws_state.json").read_text())
    opened = {"BlockPublicAcls": False, "IgnorePublicAcls": False,
              "BlockPublicPolicy": False, "RestrictPublicBuckets": False}
    st["settings"]["get-public-access-block"] = opened
    st["errors"] = {"get-public-access-block": "SlowDown"}
    r, st = _setup(tmp_path, st)
    assert r.returncode == 1 and "SlowDown" in r.stderr
    assert st["settings"]["get-public-access-block"] == opened and _writes(st) == []


def test_setup_check_writes_nothing(tmp_path):
    r, st = _setup(tmp_path, None, "--check")
    assert r.returncode == 3 and "would create" in r.stdout and _writes(st) == []
    _setup(tmp_path)
    r, st = _setup(tmp_path, None, "--check")
    assert r.returncode == 0 and _writes(st) == []


@pytest.mark.parametrize("arg", ["--dry-run", "--help-me", "extra"])
def test_setup_rejects_unknown_arguments_without_calling_aws(tmp_path, arg):
    r, st = _setup(tmp_path, None, arg)
    assert r.returncode == 2 and st["calls"] == []


def test_setup_print_policies_calls_no_aws(tmp_path):
    r, st = _setup(tmp_path, None, "--print-policies")
    assert r.returncode == 0 and st["calls"] == []
    assert r.stdout.startswith("# bucket: sm-alpr-pra-<account-id>-us-west-2-an")


def test_mint_key_recognises_a_held_key_among_several(tmp_path):
    _setup(tmp_path)
    st = json.loads((tmp_path / "aws_state.json").read_text())
    for user in st["users"]:
        st["users"][user]["keys"] = [f"AKIAOLD{user}", f"AKIANEW{user}"]
        st["profiles"][user] = f"AKIANEW{user}"  # the second of two tab-separated IDs
    r, st = _setup(tmp_path, st, MINT_KEYS="1")
    assert r.returncode == 0 and r.stdout.count("local profile already holds its key") == 2
    assert _writes(st) == []


def test_mint_key_keeps_the_secret_off_every_command_line(tmp_path):
    r, st = _setup(tmp_path, None, MINT_KEYS="1")
    assert r.returncode == 0
    assert set(st["profiles"]) == {"sm-alpr-pra-writer", "sm-alpr-pra-reader"}
    assert not any("SECRET" in arg for call in st["calls"] for arg in call)


def test_mint_key_deletes_a_key_it_could_not_save(tmp_path):
    _setup(tmp_path)
    st = json.loads((tmp_path / "aws_state.json").read_text())
    st["import_fails"] = True
    r, st = _setup(tmp_path, st, MINT_KEYS="1")
    assert r.returncode != 0 and "deleted it from IAM" in r.stderr
    assert all(u_["keys"] == [] for u_ in st["users"].values())


def _policies(tmp_path):
    r, _ = _setup(tmp_path, None, "--print-policies", PRA_S3_ACCOUNT_ID="111122223333")
    text, docs, i = r.stdout.split("\n", 1)[1], [], 0
    dec = json.JSONDecoder()
    while text[i:].strip():
        doc, n = dec.raw_decode(text[i:].lstrip())
        docs.append(doc)
        i += len(text[i:]) - len(text[i:].lstrip()) + n
    return docs


def test_setup_policies_say_what_they_should(tmp_path):
    bucket_policy, writer, reader = _policies(tmp_path)
    arn = "arn:aws:s3:::sm-alpr-pra-111122223333-us-west-2-an"
    assert bucket_policy["Statement"] == [
        {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
         "Resource": [arn, f"{arn}/*"], "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
        {"Sid": "DenyEncryptionOtherThanSSES3", "Effect": "Deny", "Principal": "*",
         "Action": "s3:PutObject", "Resource": f"{arn}/*",
         "Condition": {"Null": {"s3:x-amz-server-side-encryption": "false"},
                       "StringNotEquals": {"s3:x-amz-server-side-encryption": "AES256"}}},
        {"Sid": "DenyWritesWithoutIfNoneMatch", "Effect": "Deny", "Principal": "*",
         "Action": "s3:PutObject", "Resource": f"{arn}/*",
         "Condition": {"Null": {"s3:if-none-match": "true"},
                       "Bool": {"s3:ObjectCreationOperation": "true"}}},
    ]
    assert writer["Statement"] == [{"Sid": "WriteOnly", "Effect": "Allow",
                                    "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                                    "Resource": f"{arn}/*"}]
    assert reader["Statement"] == [
        {"Sid": "ListBucket", "Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
         "Resource": arn},
        {"Sid": "ReadObjects", "Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectAttributes"],
         "Resource": f"{arn}/*"},
    ]


def test_setup_reports_permissions_granted_elsewhere(tmp_path):
    _setup(tmp_path)
    st = json.loads((tmp_path / "aws_state.json").read_text())
    st["users"]["sm-alpr-pra-writer"]["managed"] = ["arn:aws:iam::aws:policy/AmazonS3FullAccess"]
    st["users"]["sm-alpr-pra-reader"]["groups"] = ["admins"]
    r, st = _setup(tmp_path, st, "--check")
    assert r.returncode == 3
    assert "AmazonS3FullAccess" in r.stderr and "groups: admins" in r.stderr
    assert _writes(st) == []


def test_setup_refuses_to_mint_while_cli_history_records_responses(tmp_path):
    _setup(tmp_path)
    st = json.loads((tmp_path / "aws_state.json").read_text())
    st["cli_history"] = "enabled"
    r, st = _setup(tmp_path, st, MINT_KEYS="1")
    assert r.returncode == 1 and "cli_history" in r.stderr
    assert not any(c[1] == "create-access-key" for c in st["calls"])


def test_setup_rejects_a_bucket_name_over_63_characters(tmp_path):
    r, st = _setup(tmp_path, None, PRA_S3_PREFIX="x" * 40)
    assert r.returncode == 2 and "63" in r.stderr
    assert [c[:2] for c in st["calls"]] == [["sts", "get-caller-identity"]]  # nothing else ran


# --- ocr_sidecar cleanup ------------------------------------------------------------

def test_sidecar_cleanup_spares_a_released_file_with_a_sidecar_like_name(tmp_path):
    from ocr_sidecar import remove_stale_sidecars
    base = _write(tmp_path, "Audit.pdf", b"current audit")
    current = _write(tmp_path, sidecar_path_for(base).name, b"ocr")
    stale = _write(tmp_path, "Audit.pdf.00000000.txt", b"old ocr")
    released = hashlib.md5(b"released text").hexdigest()[:8]
    real = _write(tmp_path, f"Audit.pdf.{released}.txt", b"released text")
    assert remove_stale_sidecars(base, keep=current) == [stale]
    assert current.exists() and real.exists() and not stale.exists()


# --- more uploader edges ------------------------------------------------------------

def test_xml_types_are_attachments_too():
    for name in ("feed.xml", "style.xsl", "drawing.svg", "page.xhtml"):
        assert u.headers_for(name).get("ContentDisposition") == "attachment", name
    assert "ContentDisposition" not in u.headers_for("audit.csv")


def test_a_tree_of_only_sidecars_is_refused(tmp_path):
    root = tmp_path / "tree"
    _write(root, "W1/Gone.pdf.1234abcd.txt", b"ocr of a removed file")
    s3 = FakeS3()
    assert _sync(s3, root, tmp_path / "ledger.jsonl", skip_ocr_sidecars=True) == 2
    assert s3.objects == {}


def test_hashing_errors_are_errors_not_a_shrink(tmp_path, monkeypatch):
    root, ledger = tmp_path / "tree", tmp_path / "ledger.jsonl"
    for i in range(10):
        _write(root, f"W{i}/a.pdf", str(i).encode())
    s3 = FakeS3()
    assert _sync(s3, root, ledger) == 0
    real = u.file_digests

    def flaky(path, md5=False, blob_size=None):
        if path.parent.name != "W9":
            raise RuntimeError("disk error")
        return real(path, md5, blob_size)

    monkeypatch.setattr(u, "file_digests", flaky)
    assert _sync(s3, root, ledger, rehash=True) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_stat_error_during_upload_holds_the_file(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    _write(root, "locked/a.pdf", b"a")

    def lock_then_fail(s3, bucket, key, path, size, sha256):
        path.parent.chmod(0)  # the file can no longer be stat'ed
        raise OSError("connection reset")

    monkeypatch.setattr(u, "upload", lock_then_fail)
    try:
        assert _sync(FakeS3(), root, tmp_path / "ledger.jsonl") == 3
    finally:
        (root / "locked").chmod(0o755)


def test_interrupt_cancels_queued_work_and_waits_for_running():
    started, release = [], threading.Event()

    def fn(i):
        started.append(i)
        if i:
            while not u.STOP.is_set():  # a long upload that notices the stop
                time.sleep(0.01)
        return i

    def on_done(item, fut):
        raise KeyboardInterrupt  # Ctrl-C as the first result comes in

    before = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            u.run_pool(fn, range(20), 2, on_done, log=lambda _: None)
        assert u.STOP.is_set() and len(started) < 20  # queued work never started
        assert signal.getsignal(signal.SIGINT) is before
    finally:
        u.STOP.clear()


def test_git_blob_id_matches_git(tmp_path):
    p = _write(tmp_path, "a.bin", b"hello\n")
    oid = subprocess.run(["git", "hash-object", str(p)], capture_output=True, text=True).stdout.strip()
    assert u.file_digests(p, blob_size=p.stat().st_size)[2] == oid


# --- main(), end to end against a real origin ------------------------------------------

@pytest.fixture
def cloned(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _write(work, ".gitignore", b"_pending/\n")
    _write(work, "assets/prs/W1/a.pdf", b"a")
    base = _write(work, "assets/prs/W1/b.pdf", b"b")
    _write(work, f"assets/prs/W1/{sidecar_path_for(base).name}", b"ocr")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "origin", "main")
    _write(work, "assets/prs/_pending/draft_letter.md", b"not for upload")  # gitignored
    _write(work, "assets/prs/W1/screenshot.png", b"untracked")
    monkeypatch.setattr(u, "COLLECTIONS", {"prs": {"base": "checkout", "root": "assets/prs",
                                                    "skip_ocr_sidecars": True}})
    monkeypatch.setattr(u, "checkout_root", lambda: work)
    monkeypatch.setattr(u, "primary_root", lambda: tmp_path)
    return origin, work


def _main(tmp_path, s3, *args):
    with pytest.raises(SystemExit) as exit_:
        u.main(["prs", "--bucket", "b", "--ledger", str(tmp_path / "ledger.jsonl"), *args], s3=s3)
    return exit_.value.code


def test_main_uploads_exactly_the_committed_files(cloned, tmp_path):
    origin, work = cloned
    s3 = FakeS3()
    assert _main(tmp_path, s3) == 0
    assert s3.stored_names() == ["a.pdf", "b.pdf"]  # no drafts, no screenshot, no sidecar
    (manifest,) = s3.manifests()
    rows = [json.loads(r) for r in s3.objects[manifest].decode().splitlines()]
    head = subprocess.run(["git", "-C", str(origin), "rev-parse", "main"], capture_output=True, text=True).stdout.strip()
    assert {r["git_commit"] for r in rows} == {head} and {r["mtime"] for r in rows} == {None}


def test_main_fetches_and_refuses_a_checkout_behind_origin(cloned, tmp_path):
    origin, work = cloned
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _write(other, "assets/prs/W2/new.pdf", b"new")
    _git(other, "add", ".")
    _git(other, "commit", "-q", "-m", "newer")
    _git(other, "push", "-q", "origin", "main")
    s3 = FakeS3()
    assert _main(tmp_path, s3) == 2  # only the fetch can know origin moved
    assert s3.objects == {}


def test_main_refuses_staged_changes(cloned, tmp_path):
    _, work = cloned
    _write(work, "assets/prs/W1/a.pdf", b"edited")
    _git(work, "add", "assets/prs/W1/a.pdf")
    s3 = FakeS3()
    assert _main(tmp_path, s3) == 2 and s3.objects == {}


def test_main_checks_bytes_against_the_commit(cloned, tmp_path):
    _, work = cloned
    _git(work, "update-index", "--assume-unchanged", "assets/prs/W1/a.pdf")
    _write(work, "assets/prs/W1/a.pdf", b"edited behind git's back")  # git status stays clean
    s3 = FakeS3()
    assert _main(tmp_path, s3) == 3
    assert s3.manifests() == [] and "a.pdf" not in s3.stored_names()


# --- round five: reproductions ------------------------------------------------------

def test_setup_a_failed_grants_listing_is_not_read_as_none(tmp_path):
    _setup(tmp_path)
    st = json.loads((tmp_path / "aws_state.json").read_text())
    st["users"]["sm-alpr-pra-writer"]["inline"] = ["allow-delete"]
    st["errors"] = {"list-user-policies": "Throttling"}
    r, st = _setup(tmp_path, st, "--check")
    assert r.returncode not in (0, 3) and "Throttling" in r.stderr


def test_sidecar_cleanup_leaves_other_files_sidecars_alone(tmp_path):
    from ocr_sidecar import remove_stale_sidecars
    base = _write(tmp_path, "foo.pdf", b"foo")
    other = _write(tmp_path, "foo.pdf.pdf", b"a different document")
    others_sidecar = _write(tmp_path, sidecar_path_for(other).name, b"its ocr")
    star = _write(tmp_path, "a*.pdf", b"starred")
    unrelated = _write(tmp_path, "abc.pdf", b"abc")
    unrelated_sidecar = _write(tmp_path, sidecar_path_for(unrelated).name, b"abc ocr")
    assert remove_stale_sidecars(base, keep=sidecar_path_for(base)) == []
    assert remove_stale_sidecars(star, keep=sidecar_path_for(star)) == []
    assert others_sidecar.exists() and unrelated_sidecar.exists()


def test_setup_mint_keys_fails_when_it_could_not_provide_a_key(tmp_path):
    _setup(tmp_path)
    st = json.loads((tmp_path / "aws_state.json").read_text())
    for user in st["users"]:
        st["users"][user]["keys"] = [f"AKIAELSEWHERE{user}"]  # in IAM, not in the local profile
    r, st = _setup(tmp_path, st, MINT_KEYS="1")
    assert r.returncode != 0 and "aren't in the local profile" in r.stderr


def test_fetch_updates_origin_main_even_without_a_refspec_for_it(cloned, tmp_path):
    origin, work = cloned
    _git(work, "config", "remote.origin.fetch", "+refs/heads/other:refs/remotes/origin/other")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _write(other, "assets/prs/W2/new.pdf", b"new")
    _git(other, "add", ".")
    _git(other, "commit", "-q", "-m", "newer")
    _git(other, "push", "-q", "origin", "main")
    s3 = FakeS3()
    assert _main(tmp_path, s3) == 2 and s3.objects == {}


def test_bucket_policy_refuses_kms_encrypted_uploads(tmp_path):
    bucket_policy, _, _ = _policies(tmp_path)
    arn = "arn:aws:s3:::sm-alpr-pra-111122223333-us-west-2-an"
    assert {"Sid": "DenyEncryptionOtherThanSSES3", "Effect": "Deny", "Principal": "*",
            "Action": "s3:PutObject", "Resource": f"{arn}/*",
            "Condition": {"Null": {"s3:x-amz-server-side-encryption": "false"},
                          "StringNotEquals": {"s3:x-amz-server-side-encryption": "AES256"}}} in bucket_policy["Statement"]


def test_setup_ignores_a_stray_account_id_variable(tmp_path):
    r, _ = _setup(tmp_path, None, "--check", ACCOUNT_ID="999999999999")
    assert "sm-alpr-pra-111122223333-us-west-2-an" in r.stdout and "999999999999" not in r.stdout


def test_committed_symlink_is_skipped_not_held(cloned, tmp_path):
    origin, work = cloned
    (work / "assets/prs/W1/link.pdf").symlink_to("a.pdf")
    _git(work, "add", "assets/prs/W1/link.pdf")
    _git(work, "commit", "-q", "-m", "a link")
    _git(work, "push", "-q", "origin", "main")
    s3 = FakeS3()
    assert _main(tmp_path, s3) == 0
    assert s3.stored_names() == ["a.pdf", "b.pdf"]


def test_hash_cache_forgets_files_that_are_gone_and_survives_corruption(tmp_path):
    root, cache_path = tmp_path / "tree", tmp_path / "cache.json"
    a, b = _write(root, "a.pdf", b"a"), _write(root, "b.pdf", b"b")
    assert _sync(FakeS3(), root, tmp_path / "l1.jsonl", cache_path) == 0
    assert set(json.loads(cache_path.read_text())) == {str(a), str(b)}
    b.unlink()
    assert _sync(FakeS3(), root, tmp_path / "l2.jsonl", cache_path) == 0
    assert set(json.loads(cache_path.read_text())) == {str(a)}
    cache_path.write_text("{not json")
    assert _sync(FakeS3(), root, tmp_path / "l3.jsonl", cache_path) == 0  # a cache, safe to lose


def test_interrupt_handler_ignores_a_duplicate_and_quits_on_a_later_one(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(u.time, "monotonic", lambda: clock[0])
    exits = []
    monkeypatch.setattr(u.os, "_exit", exits.append)
    handler = u._interrupt_handler()
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGINT, None)  # first: graceful stop
    clock[0] += 0.2
    handler(signal.SIGINT, None)  # uv run's duplicate: ignored
    assert exits == []
    clock[0] += 5
    handler(signal.SIGINT, None)  # a deliberate second Ctrl-C: quit now
    assert exits == [130]


def test_uploader_does_not_load_the_ocr_libraries():
    code = ("import sys; sys.path.insert(0, %r); import pra_s3_upload; "
            "print(sorted(m for m in ('fitz', 'pytesseract', 'PIL') if m in sys.modules))" % str(SCRIPT_DIR))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
