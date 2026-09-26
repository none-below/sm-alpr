"""Upload a local tree of PRA assets to the write-once S3 bucket.

The bucket (scripts/setup_pra_assets_bucket.sh) rejects any write that could
overwrite a key, and Object Lock keeps every stored version. Keys are
therefore immutable by construction:

    <collection>/<dir relative to root>/<sha256>/<filename>

A changed file (a re-fetched message history, a re-released spreadsheet) gets
a new key instead of colliding with the old one, and a 412 on PUT means the
identical bytes are already stored. The filename stays last so DuckDB globs by
extension still work:

    s3://<bucket>/pra-portals-ca/nextrequest/*/*/files/*/*.csv

Checksums: files up to 64MB go up in one PUT carrying their SHA-256, which S3
verifies and stores, so GetObjectAttributes returns the key's hash. Larger
files go up in parts, and S3 stores a COMPOSITE checksum (a hash of the part
hashes, with a -N suffix) that won't equal the key's hash. The uploader checks
the whole-file hash itself before completing those; to re-verify one later,
download it and hash it.

Manifests: a run that stores the whole tree writes
_manifests/<collection>/<UTC timestamp>-<tree hash>.jsonl (rel_path, key,
sha256, bytes, mtime per file); the newest one is the current tree. Nothing is
written when a file is held back (modified in the last --min-age seconds, or
changed or vanished mid-run), when an upload failed, or when the tree is
unchanged since the last manifest. An empty tree, or one under half the size
of the last manifest (a wrong root?), is refused before anything uploads.

The writer key can only PutObject (no list, no read), so what's stored is
tracked in a local ledger in the primary checkout's .claude/. A lost ledger
costs re-uploads that come back 412, nothing worse.

  AWS_PROFILE=sm-alpr-pra-writer uv run --with boto3 python scripts/pra_s3_upload.py \\
      pra-portals-ca /path/to/.claude/local_evidence/pra-portals-ca \\
      --exclude 'discovery/*' [--dry-run]

Exit status: 0 tree stored (or unchanged), 1 upload errors, 2 refused input,
3 incomplete (files held back), 130 interrupted (Ctrl-C or SIGTERM).
"""
import argparse
import base64
import contextlib
import fnmatch
import hashlib
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# Same env overrides and defaults as setup_pra_assets_bucket.sh (a test keeps them in sync).
DEFAULT_REGION = "us-west-2"
DEFAULT_PREFIX = "sm-alpr-pra"
PART_SIZE = 64 * 1024 * 1024  # single PUT up to this size, multipart above
SHRINK_LIMIT = 0.5  # refuse a tree with fewer files than this share of the last manifest's
# Never records: OS metadata, Office lock files, unfinished browser/tool downloads.
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
JUNK_PREFIXES = ("._", "~$")
JUNK_SUFFIXES = (".part", ".partial", ".crdownload", ".download", ".tmp")

STOP = threading.Event()  # set on interrupt; checked between files and between parts


class Stopped(Exception):
    pass


class Changed(Exception):
    pass


class Entry(NamedTuple):
    rel: str
    path: Path
    size: int
    mtime: float
    sha256: str
    key: str


def region():
    return os.environ.get("PRA_S3_REGION", DEFAULT_REGION)


def bucket_name(account_id):
    return f"{os.environ.get('PRA_S3_PREFIX', DEFAULT_PREFIX)}-{account_id}-{region()}-an"


def object_key(collection, rel_path, sha256):
    parent, _, name = rel_path.rpartition("/")
    return "/".join(p for p in (collection, parent, sha256, name) if p)


def is_junk(name):
    return (name in JUNK_NAMES or name.startswith(JUNK_PREFIXES)
            or name.lower().endswith(JUNK_SUFFIXES))


def is_ocr_sidecar(path):
    """True only for the sidecar ocr_sidecar.py would write for a sibling that exists.

    The name shape alone isn't enough: pra_download renames same-named
    attachments to <stem>.<8 hex>.<ext>, so a released notes.txt can become
    notes.1a2b3c4d.txt, and dated names like log.20260918.txt look the same.
    """
    parts = path.name.rsplit(".", 2)
    if len(parts) != 3 or parts[2] != "txt":
        return False
    base = path.with_name(parts[0])
    if not base.is_file():
        return False
    from ocr_sidecar import sidecar_path_for  # heavy imports; only when a candidate exists

    return sidecar_path_for(base) == path


def collect(root, excludes=(), skip_ocr_sidecars=False, min_age=0, now=None):
    """Walk root. Returns (files, held, junk):

    files  [(rel, path, stat)] ready to store
    held   [(rel, reason)] present but not storable yet, so the tree is incomplete
    junk   [rel] never stored (OS metadata, lock files, unfinished downloads)
    """
    now = time.time() if now is None else now
    files, held, junk = [], [], []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatchcase(rel, g) for g in excludes):
            continue
        if is_junk(path.name):
            junk.append(rel)
            continue
        try:
            if skip_ocr_sidecars and is_ocr_sidecar(path):
                continue
            st = path.stat()
        except FileNotFoundError:
            held.append((rel, "vanished while listing"))
            continue
        if now - st.st_mtime < min_age:
            held.append((rel, f"modified in the last {min_age}s"))
            continue
        files.append((rel, path, st))
    return files, held, junk


def sha256_file(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def tree_hash(pairs):
    """Identity of a tree: its (rel_path, sha256) pairs, order-independent."""
    h = hashlib.sha256()
    for rel, sha in sorted(pairs):
        h.update(f"{rel}\t{sha}\n".encode())
    return h.hexdigest()


def b64(hex_digest):
    return base64.b64encode(bytes.fromhex(hex_digest)).decode()


def default_ledger():
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=Path(__file__).parent, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return Path(common).parent / ".claude" / "s3_upload_ledger.jsonl"


class Ledger:
    """Append-only JSONL of keys known to be stored, and of manifests written."""

    def __init__(self, path, bucket, log=print):
        self.path, self.bucket = path, bucket
        self.stored, self.manifests = set(), {}
        self._lock = threading.Lock()
        if not path.exists():
            return
        raw = path.read_bytes()
        for n, line in enumerate(raw.decode(errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                log(f"ledger line {n} unreadable (interrupted write?); ignoring it")
                continue
            if r.get("bucket") == bucket:
                self._note(r)
        if raw and not raw.endswith(b"\n"):
            # A torn last line would otherwise swallow the next row appended to it.
            with path.open("ab") as f:
                f.write(b"\n")

    def _note(self, r):
        if r.get("kind") == "manifest":
            self.manifests[r["collection"]] = r
        elif r.get("key"):
            self.stored.add(r["key"])

    def record(self, **row):
        row = {"bucket": self.bucket, **row, "at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._note(row)


def _precondition_failed(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code") == "PreconditionFailed"


def put_once(s3, bucket, key, body, ctype, sha256):
    """PutObject that never overwrites: 'uploaded', or 'present' if key exists."""
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=ctype,
                      ChecksumSHA256=b64(sha256), IfNoneMatch="*")
    except Exception as e:
        if _precondition_failed(e):
            return "present"
        raise
    return "uploaded"


def upload(s3, bucket, key, path, size, sha256):
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if size <= PART_SIZE:
        with path.open("rb") as f:
            return put_once(s3, bucket, key, f, ctype, sha256)
    try:
        _multipart(s3, bucket, key, path, ctype, sha256)
    except Exception as e:
        if _precondition_failed(e):
            return "present"
        raise
    return "uploaded"


def _multipart(s3, bucket, key, path, ctype, sha256):
    upload_id = s3.create_multipart_upload(
        Bucket=bucket, Key=key, ContentType=ctype, ChecksumAlgorithm="SHA256",
    )["UploadId"]
    try:
        parts, whole = [], hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(PART_SIZE):
                if STOP.is_set():
                    raise Stopped(key)
                whole.update(chunk)
                n = len(parts) + 1
                r = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=n,
                                   Body=chunk, ChecksumSHA256=b64(hashlib.sha256(chunk).hexdigest()))
                parts.append({"PartNumber": n, "ETag": r["ETag"], "ChecksumSHA256": r["ChecksumSHA256"]})
        # The key names this hash; never store different bytes under it.
        if whole.hexdigest() != sha256:
            raise Changed(f"{path} changed during upload")
        s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id,
                                     MultipartUpload={"Parts": parts}, IfNoneMatch="*")
    except BaseException:
        with contextlib.suppress(Exception):
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise


def run_pool(fn, items, workers, on_done):
    """Call on_done(item, future) as each fn(item) finishes. On interrupt, cancel
    queued work, let in-flight work stop at its next STOP check, and re-raise."""
    pool = ThreadPoolExecutor(workers)
    try:
        futures = {pool.submit(fn, item): item for item in items}
        for fut in as_completed(futures):
            on_done(futures[fut], fut)
    except BaseException:
        STOP.set()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown()


def _hash(item, collection):
    rel, path, st = item
    if STOP.is_set():
        raise Stopped(rel)
    sha = sha256_file(path)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
        raise Changed("changed while hashing")
    return Entry(rel, path, st.st_size, st.st_mtime, sha, object_key(collection, rel, sha))


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def sync(s3, bucket, collection, root, ledger, *, excludes=(), skip_ocr_sidecars=False,
         min_age=600, workers=8, allow_shrink=False, dry_run=False, log=print):
    """Store root under <collection>/ and, once the whole tree is stored, write its
    manifest. Returns the exit status described in the module docstring."""
    STOP.clear()
    files, held, junk = collect(root, excludes, skip_ocr_sidecars, min_age)
    found = len(files) + len(held)
    if not found:
        log(f"{collection}: nothing to store under {root}; refusing to write an empty manifest")
        return 2
    last = ledger.manifests.get(collection)
    if last and not allow_shrink and found < SHRINK_LIMIT * last["files"]:
        log(f"{collection}: {found} files, down from {last['files']} in {last['key']}. "
            "Wrong root? Pass --allow-shrink if the tree really shrank.")
        return 2

    entries, failed = [], 0

    def hashed(item, fut):
        nonlocal failed
        try:
            entries.append(fut.result())
        except FileNotFoundError:
            held.append((item[0], "vanished before hashing"))
        except Changed as e:
            held.append((item[0], str(e)))
        except Exception as e:
            failed += 1
            log(f"  ERROR {item[0]}: {e}")

    run_pool(lambda item: _hash(item, collection), files, workers, hashed)
    entries.sort()
    todo = [e for e in entries if e.key not in ledger.stored]
    todo_bytes = sum(e.size for e in todo)
    log(f"{collection}: {len(entries)} files, {human(sum(e.size for e in entries))}; "
        f"{len(entries) - len(todo)} already stored, {len(todo)} to upload ({human(todo_bytes)})")
    if junk:
        log(f"  never stored (OS metadata, lock files, unfinished downloads): {len(junk)}, e.g. {junk[0]}")
    for rel, reason in held:
        log(f"  held back ({reason}): {rel}")
    if dry_run:
        for e in todo[:5]:
            log(f"  would store {e.key}")
        return 0

    counts = {"uploaded": 0, "present": 0, "error": 0}
    done_bytes, started, last_report = 0, time.time(), 0.0

    def put(e):
        status = upload(s3, bucket, e.key, e.path, e.size, e.sha256)
        ledger.record(key=e.key, sha256=e.sha256, bytes=e.size, path=str(e.path), status=status)
        return status

    def uploaded(e, fut):
        nonlocal done_bytes, last_report
        try:
            counts[fut.result()] += 1
        except Exception as exc:
            counts["error"] += 1
            log(f"  ERROR {e.rel}: {exc}")
        done_bytes += e.size
        now = time.time()
        if now - last_report > 30 or sum(counts.values()) == len(todo):
            last_report = now
            rate = done_bytes / max(now - started, 1e-9)
            log(f"  {sum(counts.values())}/{len(todo)} files, {human(done_bytes)}/{human(todo_bytes)} "
                f"({human(rate)}/s) {counts}")

    run_pool(put, todo, workers, uploaded)

    failed += counts["error"]
    if failed:
        log(f"{failed} file(s) failed; no manifest written. Re-run to retry.")
        return 1
    if held:
        log(f"{len(held)} file(s) held back; no manifest written. Re-run once they settle.")
        return 3
    tree = tree_hash((e.rel, e.sha256) for e in entries)
    if last and last["tree"] == tree:
        log(f"manifest: tree unchanged since {last['key']}; not writing another")
        return 0
    run_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mkey = f"_manifests/{collection}/{run_at}-{tree[:12]}.jsonl"
    body = "".join(json.dumps({
        "collection": collection, "rel_path": e.rel, "key": e.key, "sha256": e.sha256,
        "bytes": e.size, "mtime": datetime.fromtimestamp(e.mtime, timezone.utc).isoformat(),
    }, ensure_ascii=False) + "\n" for e in entries).encode()
    status = put_once(s3, bucket, mkey, body, "application/x-ndjson", hashlib.sha256(body).hexdigest())
    ledger.record(kind="manifest", collection=collection, key=mkey, tree=tree,
                  files=len(entries), status=status)
    log(f"manifest: {mkey} ({len(entries)} files)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("collection", help="top-level key prefix, e.g. pra-portals-ca")
    ap.add_argument("root", type=Path, help="local directory to upload")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip rel paths matching GLOB (repeatable; * crosses /)")
    ap.add_argument("--skip-ocr-sidecars", action="store_true",
                    help="skip .txt sidecars ocr_sidecar.py wrote next to existing files")
    ap.add_argument("--min-age", type=int, default=600, metavar="SECONDS",
                    help="hold back files modified more recently than this (may still be downloading)")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="accept a tree under half the size of the last manifest")
    ap.add_argument("--bucket", help="default: $PRA_S3_PREFIX-<account>-$PRA_S3_REGION-an")
    ap.add_argument("--ledger", type=Path, help="default: <primary checkout>/.claude/s3_upload_ledger.jsonl")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.collection):
        ap.error("collection must be lowercase letters, digits and dashes")
    root = args.root.resolve()
    if not root.is_dir():
        ap.error(f"{args.root} is not a directory")

    import boto3
    from botocore.config import Config

    s3 = boto3.client("s3", region_name=region(), config=Config(
        retries={"mode": "adaptive", "max_attempts": 10}, max_pool_connections=args.workers * 2))
    bucket = args.bucket or bucket_name(boto3.client("sts").get_caller_identity()["Account"])
    ledger = Ledger(args.ledger or default_ledger(), bucket)

    signal.signal(signal.SIGTERM, signal.default_int_handler)  # stop on kill like on Ctrl-C
    try:
        status = sync(s3, bucket, args.collection, root, ledger, excludes=args.exclude,
                      skip_ocr_sidecars=args.skip_ocr_sidecars, min_age=args.min_age,
                      workers=args.workers, allow_shrink=args.allow_shrink, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("interrupted: queued uploads cancelled, in-flight multipart uploads aborted. "
              "Re-run to resume.", file=sys.stderr)
        status = 130
    sys.exit(status)


if __name__ == "__main__":
    main()
