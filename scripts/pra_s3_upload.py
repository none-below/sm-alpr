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

A run that stores every eligible file also writes
_manifests/<collection>/<UTC timestamp>.jsonl (rel_path, key, sha256, bytes,
mtime per file), so the bucket describes itself: the newest manifest is the
current tree, all of them together are its history.

The writer key can only PutObject (no list, no read), so what's already stored
is tracked in a local ledger in the primary checkout's .claude/. A lost ledger
costs re-uploads that come back 412, nothing worse.

  AWS_PROFILE=sm-alpr-pra-writer uv run --with boto3 python scripts/pra_s3_upload.py \\
      pra-portals-ca /path/to/.claude/local_evidence/pra-portals-ca \\
      --exclude 'discovery/*' [--dry-run]
"""
import argparse
import base64
import contextlib
import fnmatch
import hashlib
import json
import mimetypes
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BUCKET_PREFIX = "sm-alpr-pra"  # matches PREFIX in setup_pra_assets_bucket.sh
REGION = "us-west-2"
PART_SIZE = 64 * 1024 * 1024  # single PUT up to this size, multipart above
ALWAYS_EXCLUDE = {".DS_Store"}
# OCR sidecars next to repo PDFs (<name>.<ext>.<8 hex>.txt): derived, and in git.
OCR_SIDECAR = re.compile(r"\.[0-9a-f]{8}\.txt$")


def object_key(collection, rel_path, sha256):
    parent, _, name = rel_path.rpartition("/")
    return "/".join(p for p in (collection, parent, sha256, name) if p)


def collect(root, excludes=(), skip_ocr_sidecars=False, min_age=0, now=None):
    """Return ([(rel_path, path, stat)], [rel_path too recently modified])."""
    now = time.time() if now is None else now
    files, too_new = [], []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.name in ALWAYS_EXCLUDE:
            continue
        rel = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatchcase(rel, g) for g in excludes):
            continue
        if skip_ocr_sidecars and OCR_SIDECAR.search(path.name):
            continue
        st = path.stat()
        if now - st.st_mtime < min_age:
            too_new.append(rel)
            continue
        files.append((rel, path, st))
    return files, too_new


def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
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
    """Append-only record of keys known to be stored, plus a hash cache."""

    def __init__(self, path, bucket):
        self.path, self.bucket = path, bucket
        self.stored, self.hashes = set(), {}
        self._lock = threading.Lock()
        if path.exists():
            for line in path.open():
                r = json.loads(line)
                self.hashes[(r["path"], r["bytes"], r["mtime_ns"])] = r["sha256"]
                if r["bucket"] == bucket:
                    self.stored.add(r["key"])

    def record(self, **row):
        row = {"bucket": self.bucket, **row, "at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self.stored.add(row["key"])


def upload(s3, bucket, key, path, size, sha256):
    """Store key exactly once: 'uploaded', or 'present' if it already exists."""
    from botocore.exceptions import ClientError

    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        if size <= PART_SIZE:
            with path.open("rb") as f:
                s3.put_object(Bucket=bucket, Key=key, Body=f, ContentType=ctype,
                              ChecksumSHA256=b64(sha256), IfNoneMatch="*")
        else:
            _multipart(s3, bucket, key, path, ctype, sha256)
    except ClientError as e:
        if e.response["Error"]["Code"] == "PreconditionFailed":
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
                whole.update(chunk)
                n = len(parts) + 1
                r = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=n,
                                   Body=chunk, ChecksumSHA256=b64(hashlib.sha256(chunk).hexdigest()))
                parts.append({"PartNumber": n, "ETag": r["ETag"], "ChecksumSHA256": r["ChecksumSHA256"]})
        # The key names this hash; never store different bytes under it.
        if whole.hexdigest() != sha256:
            raise RuntimeError(f"{path} changed during upload")
        s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id,
                                     MultipartUpload={"Parts": parts}, IfNoneMatch="*")
    except BaseException:
        with contextlib.suppress(Exception):
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        raise


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("collection", help="top-level key prefix, e.g. pra-portals-ca")
    ap.add_argument("root", type=Path, help="local directory to upload")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip rel paths matching GLOB (repeatable; * crosses /)")
    ap.add_argument("--skip-ocr-sidecars", action="store_true",
                    help="skip <file>.<8 hex>.txt OCR sidecars")
    ap.add_argument("--min-age", type=int, default=600, metavar="SECONDS",
                    help="skip files modified more recently than this (may still be downloading)")
    ap.add_argument("--bucket", help="default: sm-alpr-pra-<account>-us-west-2-an")
    ap.add_argument("--ledger", type=Path, help="default: <primary checkout>/.claude/s3_upload_ledger.jsonl")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.collection):
        ap.error("collection must be lowercase letters, digits and dashes")
    root = args.root.resolve()

    import boto3
    from botocore.config import Config

    s3 = boto3.client("s3", region_name=REGION, config=Config(
        retries={"mode": "adaptive", "max_attempts": 10}, max_pool_connections=args.workers * 2))
    bucket = args.bucket or (
        f"{BUCKET_PREFIX}-{boto3.client('sts').get_caller_identity()['Account']}-{REGION}-an")
    ledger = Ledger(args.ledger or default_ledger(), bucket)

    files, too_new = collect(root, args.exclude, args.skip_ocr_sidecars, args.min_age)

    def plan(item):
        rel, path, st = item
        sha = ledger.hashes.get((str(path), st.st_size, st.st_mtime_ns)) or sha256_file(path)
        return rel, path, st, sha, object_key(args.collection, rel, sha)

    with ThreadPoolExecutor(args.workers) as pool:
        entries = list(pool.map(plan, files))
    todo = [e for e in entries if e[4] not in ledger.stored]
    todo_bytes = sum(e[2].st_size for e in todo)
    print(f"{args.collection}: {len(entries)} files, {human(sum(e[2].st_size for e in entries))}; "
          f"{len(entries) - len(todo)} already stored, {len(todo)} to upload ({human(todo_bytes)})")
    for rel in too_new:
        print(f"  skipped, modified < {args.min_age}s ago: {rel}")
    if args.dry_run:
        for e in todo[:5]:
            print(f"  would store {e[4]}")
        return

    counts = {"uploaded": 0, "present": 0, "error": 0}
    done_bytes, started, last_report = 0, time.time(), 0.0

    def put(e):
        _, path, st, sha, key = e
        status = upload(s3, bucket, key, path, st.st_size, sha)
        ledger.record(key=key, sha256=sha, bytes=st.st_size, path=str(path),
                      mtime_ns=st.st_mtime_ns, status=status)
        return status

    with ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(put, e): e for e in todo}
        for fut in as_completed(futures):
            e = futures[fut]
            try:
                counts[fut.result()] += 1
            except Exception as exc:
                counts["error"] += 1
                print(f"  ERROR {e[0]}: {exc}", file=sys.stderr)
            done_bytes += e[2].st_size
            now = time.time()
            if now - last_report > 30 or sum(counts.values()) == len(todo):
                last_report = now
                rate = done_bytes / max(now - started, 1e-9)
                print(f"  {sum(counts.values())}/{len(todo)} files, {human(done_bytes)}/{human(todo_bytes)} "
                      f"({human(rate)}/s) {counts}", flush=True)

    if counts["error"]:
        print(f"{counts['error']} failed; no manifest written. Re-run to retry.", file=sys.stderr)
        sys.exit(1)

    run_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = "".join(json.dumps({
        "collection": args.collection, "rel_path": rel, "key": key, "sha256": sha,
        "bytes": st.st_size, "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
    }, ensure_ascii=False) + "\n" for rel, _, st, sha, key in entries).encode()
    mkey = f"_manifests/{args.collection}/{run_at}.jsonl"
    s3.put_object(Bucket=bucket, Key=mkey, Body=manifest, ContentType="application/x-ndjson",
                  ChecksumSHA256=b64(hashlib.sha256(manifest).hexdigest()), IfNoneMatch="*")
    print(f"manifest: {mkey} ({len(entries)} files)")


if __name__ == "__main__":
    main()
