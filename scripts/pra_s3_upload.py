"""Upload a collection of PRA assets to the write-once S3 bucket.

  AWS_PROFILE=sm-alpr-pra-writer uv run --with boto3 python scripts/pra_s3_upload.py \\
      pra-portals-ca [--dry-run]

Each collection's folder and selection rules are pinned in COLLECTIONS below,
so a run can't upload the wrong folder under a collection's name, and the tree
means the same thing on every run.

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
sha256, bytes, mtime per file); the newest one is the current tree. The tree
is every regular file under the root except exclusions, OS metadata and (where
configured) OCR sidecars. No manifest is written while anything else is
present that can't be stored: a file modified in the last --min-age seconds,
an unfinished download (.part, .crdownload, ...) or open-document lock file, a
symlink, an unreadable folder, or a file that changed, appeared or vanished
during the run. None is written after an upload error, or when the tree is
unchanged since the last manifest. An empty tree, or one under half the size
of the last manifest, is refused before anything uploads.

The writer key can only PutObject (no list, no read), so what's stored is
tracked in a local ledger in the primary checkout's .claude/. A lost ledger
costs re-uploads that come back 412, nothing worse.

Exit status (--dry-run predicts the same): 0 tree stored (or unchanged),
1 errors, 2 refused, 3 incomplete (something held back), 130 interrupted
(Ctrl-C or SIGTERM).
"""
import argparse
import base64
import contextlib
import fnmatch
import hashlib
import json
import mimetypes
import os
import signal
import stat
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

# Where each collection lives and what belongs in it. "base" is "checkout"
# (the checkout this script is in) or "primary" (the primary checkout, which
# holds the gitignored .claude/). Change a collection here, in review, rather
# than on the command line.
COLLECTIONS = {
    "public-records": {
        "base": "checkout", "root": "assets/public-records", "skip_ocr_sidecars": True,
    },
    "san-mateo-public-records": {
        "base": "checkout", "root": "assets/san-mateo-public-records", "skip_ocr_sidecars": True,
    },
    "pra-portals-ca": {
        "base": "primary", "root": ".claude/local_evidence/pra-portals-ca",
        "exclude": ["discovery/*", "_forensics/*"],  # probe caches; analysis output
    },
}

PART_SIZE = 64 * 1024 * 1024  # single PUT up to this size, multipart above
SHRINK_LIMIT = 0.5  # refuse a tree with fewer files than this share of the last manifest's
FUTURE_SLACK = 60  # an mtime further ahead than this is restored metadata, not a live write
OS_METADATA = {".DS_Store", "Thumbs.db", "desktop.ini"}  # plus AppleDouble ._* files
# Unfinished work: a download still running (or stalled), or an open Office document.
UNFINISHED_PREFIXES = ("~$",)
UNFINISHED_SUFFIXES = (".part", ".partial", ".crdownload", ".download", ".tmp")

STOP = threading.Event()  # set on interrupt; checked between files and between parts


class Stopped(Exception):
    pass


class Changed(Exception):
    pass


class Entry(NamedTuple):
    rel: str
    path: Path
    size: int
    mtime_ns: int
    sha256: str
    md5: str | None
    key: str


def region():
    return os.environ.get("PRA_S3_REGION", DEFAULT_REGION)


def bucket_name(account_id):
    return f"{os.environ.get('PRA_S3_PREFIX', DEFAULT_PREFIX)}-{account_id}-{region()}-an"


def checkout_root():
    return Path(__file__).resolve().parent.parent


def primary_root():
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=Path(__file__).parent, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return Path(common).parent


def collection_root(name):
    cfg = COLLECTIONS[name]
    return (primary_root() if cfg["base"] == "primary" else checkout_root()) / cfg["root"]


def default_ledger():
    return primary_root() / ".claude" / "s3_upload_ledger.jsonl"


def object_key(collection, rel_path, sha256):
    parent, _, name = rel_path.rpartition("/")
    return "/".join(p for p in (collection, parent, sha256, name) if p)


def classify(name):
    """'metadata' (never stored, not part of the tree), 'unfinished' (holds the
    tree back), or None for an ordinary file."""
    if name in OS_METADATA or name.startswith("._"):
        return "metadata"
    if name.startswith(UNFINISHED_PREFIXES) or name.lower().endswith(UNFINISHED_SUFFIXES):
        return "unfinished"
    return None


def collect(root, excludes=(), min_age=0, now=None):
    """Walk root. Returns (files, held, ignored):

    files    [(rel, path, stat)] regular files to hash and store
    held     [(rel, reason)] present but not storable (recent or unfinished
             files, symlinks, unreadable folders); any of them means the tree
             isn't complete, so no manifest
    ignored  [rel] OS metadata
    """
    now = time.time() if now is None else now
    files, held, ignored, errors = [], [], [], []

    def excluded(rel):
        return any(fnmatch.fnmatchcase(rel, g) for g in excludes)

    for dirpath, dirnames, filenames in root.walk(on_error=errors.append):
        dirnames[:] = [d for d in dirnames
                       if not excluded((dirpath / d).relative_to(root).as_posix() + "/")]
        for name in filenames:  # Path.walk lists symlinks (to files or dirs) here
            path = dirpath / name
            rel = path.relative_to(root).as_posix()
            if excluded(rel):
                continue
            kind = classify(name)
            if kind == "metadata":
                ignored.append(rel)
                continue
            if kind == "unfinished":
                held.append((rel, "unfinished download or open document"))
                continue
            try:
                st = path.lstat()
            except FileNotFoundError:
                held.append((rel, "vanished while listing"))
                continue
            if stat.S_ISLNK(st.st_mode):
                held.append((rel, "symlink, not followed: exclude it or copy the file in"))
            elif not stat.S_ISREG(st.st_mode):
                held.append((rel, "not a regular file"))
            elif -FUTURE_SLACK <= now - st.st_mtime < min_age:
                held.append((rel, f"modified in the last {min_age}s"))
            else:
                files.append((rel, path, st))
    for err in errors:
        rel = Path(err.filename).relative_to(root).as_posix() if err.filename else "."
        held.append((rel, f"unreadable folder: {err.strerror}"))
    files.sort()
    return files, held, ignored


def file_digests(path, md5=False):
    """SHA-256 (and MD5 if asked) hex digests from a single read of the file."""
    sha, m = hashlib.sha256(), hashlib.md5() if md5 else None
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            sha.update(chunk)
            if m:
                m.update(chunk)
    return sha.hexdigest(), m.hexdigest() if m else None


def unchanged(path, size, mtime_ns):
    try:
        st = path.stat()
    except FileNotFoundError:
        return False
    return (st.st_size, st.st_mtime_ns) == (size, mtime_ns)


def split_ocr_sidecars(entries, held):
    """Separate the .txt sidecars ocr_sidecar.py wrote for files in this tree,
    using the MD5 computed while hashing. Returns (entries, sidecar rels, held)
    where a candidate whose base file is held back is held too."""
    from ocr_sidecar import sidecar_base_name, sidecar_name  # heavy imports; only when needed

    by_rel = {e.rel: e for e in entries}
    held_rels = {rel for rel, _ in held}
    keep, sidecars, waiting = [], set(), []
    for e in entries:
        parent, _, name = e.rel.rpartition("/")
        base = sidecar_base_name(name)
        base_rel = f"{parent}/{base}" if parent and base else base
        if base and base_rel in by_rel and sidecar_name(base, by_rel[base_rel].md5) == name:
            sidecars.add(e.rel)
        elif base and base_rel in held_rels:
            waiting.append((e.rel, "may be the OCR sidecar of a held-back file"))
        else:
            keep.append(e)
    return keep, sidecars, waiting


def tree_hash(pairs):
    """Identity of a tree: its (rel_path, sha256) pairs, order-independent."""
    h = hashlib.sha256()
    for rel, sha in sorted(pairs):
        h.update(f"{rel}\t{sha}\n".encode())
    return h.hexdigest()


def changes_since(root, excludes, min_age, entries, skip):
    """What differs between the tree now and the entries hashed at the start."""
    files, held, _ = collect(root, excludes, min_age)
    now = {rel: (st.st_size, st.st_mtime_ns) for rel, _, st in files}
    then = {e.rel: (e.size, e.mtime_ns) for e in entries}
    found = {rel: reason for rel, reason in held if rel not in skip}
    for rel in then.keys() - now.keys():
        found.setdefault(rel, "vanished during the run")
    for rel in now.keys() - then.keys() - skip:
        found.setdefault(rel, "appeared during the run")
    for rel in then.keys() & now.keys():
        if then[rel] != now[rel]:
            found.setdefault(rel, "changed during the run")
    return sorted(found.items())


def b64(hex_digest):
    return base64.b64encode(bytes.fromhex(hex_digest)).decode()


class Ledger:
    """Append-only JSONL of keys known to be stored, and of manifests written.
    Reading it never writes; a torn last line is fixed on the next append."""

    def __init__(self, path, bucket, log=print):
        self.path, self.bucket = path, bucket
        self.stored, self.manifests = set(), {}
        self._lock = threading.Lock()
        self._torn = False
        if not path.exists():
            return
        raw = path.read_bytes()
        self._torn = bool(raw) and not raw.endswith(b"\n")
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
                if self._torn:  # don't glue this row onto a torn one
                    f.write("\n")
                    self._torn = False
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


def _hash(item, collection, want_md5):
    rel, path, st = item
    if STOP.is_set():
        raise Stopped(rel)
    sha, md5 = file_digests(path, md5=want_md5)
    if not unchanged(path, st.st_size, st.st_mtime_ns):
        raise Changed("changed while hashing")
    return Entry(rel, path, st.st_size, st.st_mtime_ns, sha, md5, object_key(collection, rel, sha))


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
    files, held, ignored = collect(root, excludes, min_age)
    found = len(files) + len(held)
    if not found:
        log(f"{collection}: nothing under {root}; refusing to write an empty manifest")
        return 2
    last = ledger.manifests.get(collection)
    if last and not allow_shrink and found < SHRINK_LIMIT * last["files"]:
        log(f"{collection}: {found} files, down from {last['files']} in {last['key']}. "
            "Pass --allow-shrink if the tree really shrank.")
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

    run_pool(lambda item: _hash(item, collection, skip_ocr_sidecars), files, workers, hashed)
    entries.sort()
    sidecars = set()
    if skip_ocr_sidecars:
        entries, sidecars, waiting = split_ocr_sidecars(entries, held)
        held += waiting
    todo = [e for e in entries if e.key not in ledger.stored]
    todo_bytes = sum(e.size for e in todo)
    log(f"{collection}: {len(entries)} files, {human(sum(e.size for e in entries))}; "
        f"{len(entries) - len(todo)} already stored, {len(todo)} to upload ({human(todo_bytes)})")
    if sidecars:
        log(f"  OCR sidecars skipped: {len(sidecars)}")
    if ignored:
        log(f"  OS metadata ignored: {len(ignored)}")
    for rel, reason in held:
        log(f"  held back ({reason}): {rel}")
    logged = len(held)
    tree = tree_hash((e.rel, e.sha256) for e in entries)
    if dry_run:
        for e in todo[:5]:
            log(f"  would store {e.key}")
        if failed or held:
            log("  no manifest would be written")
            return 1 if failed else 3
        log(f"  manifest: {'unchanged' if last and last['tree'] == tree else 'would be written'}")
        return 0

    counts = {"uploaded": 0, "present": 0, "held": 0, "error": 0}
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
            if isinstance(exc, (Changed, FileNotFoundError)) or not unchanged(e.path, e.size, e.mtime_ns):
                counts["held"] += 1
                held.append((e.rel, "changed or vanished during upload"))
            else:
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
    if not failed and not held:
        held += changes_since(root, excludes, min_age, entries, sidecars)
    if failed:
        log(f"{failed} file(s) failed; no manifest written. Re-run to retry.")
        return 1
    if held:
        for rel, reason in held[logged:]:
            log(f"  held back ({reason}): {rel}")
        log(f"{len(held)} item(s) held back; no manifest written. Re-run once the tree settles.")
        return 3
    if last and last["tree"] == tree:
        log(f"manifest: tree unchanged since {last['key']}; not writing another")
        return 0
    run_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mkey = f"_manifests/{collection}/{run_at}-{tree[:12]}.jsonl"
    body = "".join(json.dumps({
        "collection": collection, "rel_path": e.rel, "key": e.key, "sha256": e.sha256,
        "bytes": e.size, "mtime": datetime.fromtimestamp(e.mtime_ns / 1e9, timezone.utc).isoformat(),
    }, ensure_ascii=False) + "\n" for e in entries).encode()
    status = put_once(s3, bucket, mkey, body, "application/x-ndjson", hashlib.sha256(body).hexdigest())
    ledger.record(kind="manifest", collection=collection, key=mkey, tree=tree,
                  files=len(entries), status=status)
    log(f"manifest: {mkey} ({len(entries)} files)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("collection", choices=sorted(COLLECTIONS))
    ap.add_argument("--min-age", type=int, default=600, metavar="SECONDS",
                    help="hold back files modified more recently than this (may still be downloading)")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="accept a tree under half the size of the last manifest")
    ap.add_argument("--bucket", help="default: $PRA_S3_PREFIX-<account>-$PRA_S3_REGION-an")
    ap.add_argument("--ledger", type=Path, help="default: <primary checkout>/.claude/s3_upload_ledger.jsonl")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = COLLECTIONS[args.collection]
    root = collection_root(args.collection)
    if not root.is_dir():
        ap.error(f"{root} is not a directory")

    import boto3
    from botocore.config import Config

    s3 = boto3.client("s3", region_name=region(), config=Config(
        retries={"mode": "adaptive", "max_attempts": 10}, max_pool_connections=args.workers * 2))
    bucket = args.bucket or bucket_name(boto3.client("sts").get_caller_identity()["Account"])
    ledger = Ledger(args.ledger or default_ledger(), bucket)

    signal.signal(signal.SIGTERM, signal.default_int_handler)  # stop on kill like on Ctrl-C
    try:
        status = sync(s3, bucket, args.collection, root, ledger, excludes=cfg.get("exclude", ()),
                      skip_ocr_sidecars=cfg.get("skip_ocr_sidecars", False), min_age=args.min_age,
                      workers=args.workers, allow_shrink=args.allow_shrink, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("interrupted: queued uploads cancelled, in-flight multipart uploads aborted. "
              "Re-run to resume.", file=sys.stderr)
        status = 130
    sys.exit(status)


if __name__ == "__main__":
    main()
