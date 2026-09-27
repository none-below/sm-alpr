"""Upload a collection of PRA assets to the write-once S3 bucket.

  AWS_PROFILE=sm-alpr-pra-writer uv run --with boto3 python scripts/pra_s3_upload.py \\
      pra-portals-ca [--dry-run]

Collections are pinned in COLLECTIONS below. A "checkout" collection is the
git-tracked files under a folder of this repo: the run fetches origin/main and
refuses unless that folder here is clean and identical to origin/main's, so a
stale or dirty checkout can't upload and untracked files never do. A "primary"
collection is a local-only folder under the primary checkout's .claude/, taken
as it is on disk.

The bucket (scripts/setup_pra_assets_bucket.sh) rejects any write that could
overwrite a key, and Object Lock keeps every stored version. Keys are
therefore immutable by construction:

    <collection>/<dir relative to root>/<sha256>/<filename>

A changed file (a re-fetched message history, a re-released spreadsheet) gets
a new key instead of colliding with the old one, and a 412 on PUT means the
identical bytes are already stored.

Reading back: a glob such as s3://<bucket>/pra-portals-ca/**/*.csv matches
every version ever stored, old ones included. For the current tree, take the
keys from the newest manifest; manifest names start with a UTC timestamp, so
the largest name is the newest:

    SELECT key FROM read_json('s3://<bucket>/_manifests/pra-portals-ca/*.jsonl', filename = true)
    WHERE filename = (SELECT max(file) FROM glob('s3://<bucket>/_manifests/pra-portals-ca/*.jsonl'))

Checksums: files up to 64MB go up in one PUT carrying their SHA-256, which S3
verifies and stores, so GetObjectAttributes returns the key's hash. Larger
files go up in parts, and S3 stores a COMPOSITE checksum (a hash of the part
hashes, with a -N suffix) that won't equal the key's hash. The uploader checks
the whole-file hash itself before completing those; to re-verify one later,
download it and hash it.

Manifests: a run that stores the whole tree writes
_manifests/<collection>/<UTC timestamp>-<tree hash>.jsonl (rel_path, key,
sha256, bytes, mtime per file). The tree is every file in the collection
except exclusions, OS metadata and (where configured) OCR sidecars. Nothing is
written while something can't be stored yet (exit 3):
  - a file that changed, appeared or vanished during the run;
  - a symlink, or something that isn't a regular file;
  - checkout collections: a tracked file missing on disk (sparse checkout);
  - primary collections: a file modified in the last --min-age seconds, an
    unfinished download (.part, .crdownload, a Safari .download folder, ...)
    or open-document lock (~$...) not listed in the collection's "keep", or
    an unreadable folder.
Nothing is written after an upload error (exit 1) or for an unchanged tree
(exit 0). An empty tree, or one under half the files of the last manifest, is
refused before anything uploads (exit 2).

The writer key can only PutObject (no list, no read), so what's stored is
tracked in a local ledger in the primary checkout's .claude/. A lost ledger
costs re-uploads that come back 412, nothing worse. The ledger also caches
hashes by file identity (device, inode, size, mtime, ctime); tools that keep
mtime across a rewrite (rsync -t, cp -p, unzip) can't keep ctime, so changed
bytes are always re-hashed.

Exit status (--dry-run predicts the same): 0 tree stored (or unchanged),
1 errors, 2 refused, 3 incomplete (something held back), 130 interrupted
(Ctrl-C or SIGTERM).
"""
import argparse
import base64
import contextlib
import fnmatch
import functools
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

# Where each collection lives and what belongs in it. Change these in review,
# not on the command line.
#   base     "checkout": git-tracked files in this repo, verified against origin/main
#            "primary": a folder in the primary checkout (gitignored .claude/), as on disk
#   root     folder, relative to base
#   exclude  globs of rel paths to leave out (* crosses /; "dir/*" skips the folder)
#   keep     globs of real files whose names look unfinished (e.g. a released export.part)
#   skip_ocr_sidecars  leave out the .txt sidecars scripts/ocr_sidecar.py writes
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

STOP = threading.Event()  # set on interrupt; checked between files, parts and read chunks


class Stopped(Exception):
    pass


class Changed(Exception):
    pass


class Refused(Exception):
    """The run must not start; the message says why."""


class Entry(NamedTuple):
    rel: str
    path: Path
    ident: tuple  # see identity()
    sha256: str
    md5: str | None
    key: str

    @property
    def size(self):
        return self.ident[2]


def _env(name, default):
    return os.environ.get(name) or default  # empty counts as unset, as in the shell's ${VAR:-x}


def region():
    return _env("PRA_S3_REGION", DEFAULT_REGION)


def bucket_name(account_id):
    return f"{_env('PRA_S3_PREFIX', DEFAULT_PREFIX)}-{account_id}-{region()}-an"


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


def tracked_files(repo, root_rel, fetch=True):
    """The git-tracked files under root_rel, as paths relative to it, after
    checking that this checkout holds exactly origin/main's version of it."""
    def git(*args):
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if r.returncode:
            raise Refused(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout

    if fetch:
        git("fetch", "--quiet", "origin", "main")
    dirty = git("status", "--porcelain", "--untracked-files=no", "--", root_rel)
    if dirty:
        raise Refused(f"uncommitted changes under {root_rel}:\n{dirty.rstrip()}")
    if git("rev-parse", f"HEAD:{root_rel}") != git("rev-parse", f"origin/main:{root_rel}"):
        raise Refused(f"{root_rel} here differs from origin/main (stale or unmerged); "
                      "run from a fresh worktree off origin/main")
    prefix = root_rel.rstrip("/") + "/"
    return {p[len(prefix):] for p in git("ls-files", "-z", "--", root_rel).split("\0") if p}


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


def identity(st):
    """What must stay the same for a file's bytes to be taken as unchanged. ctime
    can't be set from user space and moves on every write."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _matches(rel, globs):
    return any(fnmatch.fnmatchcase(rel, g) for g in globs)


def _regular(rel, path, held):
    """The lstat of a regular file; False if held back as something else; None if gone."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        held.append((rel, "symlink, not followed: exclude it or copy the file in"))
    elif not stat.S_ISREG(st.st_mode):
        held.append((rel, "not a regular file"))
    else:
        return st
    return False


def collect(root, excludes=(), min_age=0, now=None, tracked=None, keep=()):
    """List the collection. Returns (files, held, ignored):

    files    [(rel, path, stat)] regular files to hash and store
    held     [(rel, reason)] present but not storable; any of them means the
             tree isn't complete, so no manifest
    ignored  [rel] OS metadata

    With `tracked` (a set of rel paths from git), only those files count and
    git vouches that they're finished, so their names and mtimes don't matter.
    Otherwise the folder is walked as it is on disk.
    """
    files, held, ignored = [], [], []
    if tracked is not None:
        for rel in sorted(tracked):
            if _matches(rel, excludes):
                continue
            if classify(rel.rpartition("/")[2]) == "metadata":
                ignored.append(rel)
                continue
            st = _regular(rel, root / rel, held)
            if st is None:
                held.append((rel, "tracked but not on disk (sparse checkout?)"))
            elif st:
                files.append((rel, root / rel, st))
        return files, held, ignored

    now = time.time() if now is None else now
    errors = []
    for dirpath, dirnames, filenames in root.walk(on_error=errors.append):
        subdirs = []
        for d in dirnames:
            rel = (dirpath / d).relative_to(root).as_posix()
            if _matches(rel + "/", excludes):
                continue
            if classify(d) == "unfinished" and not _matches(rel, keep):  # e.g. Safari's x.pdf.download/
                held.append((rel, "unfinished download folder"))
                continue
            subdirs.append(d)
        dirnames[:] = subdirs
        for name in filenames:  # Path.walk lists symlinks (to files or dirs) here
            path = dirpath / name
            rel = path.relative_to(root).as_posix()
            if _matches(rel, excludes):
                continue
            kind = classify(name)
            if kind == "metadata":
                ignored.append(rel)
                continue
            if kind == "unfinished" and not _matches(rel, keep):
                held.append((rel, "unfinished download or open document (list it in keep if it's real)"))
                continue
            st = _regular(rel, path, held)
            if st is None:
                held.append((rel, "vanished while listing"))
            elif st and -FUTURE_SLACK <= now - st.st_mtime < min_age:
                held.append((rel, f"modified in the last {min_age}s"))
            elif st:
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
            if STOP.is_set():
                raise Stopped(str(path))
            sha.update(chunk)
            if m:
                m.update(chunk)
    return sha.hexdigest(), m.hexdigest() if m else None


def unchanged(path, ident):
    try:
        return identity(path.stat()) == ident
    except FileNotFoundError:
        return False


def split_ocr_sidecars(entries):
    """Separate the OCR sidecars (current, stale or orphaned) using the MD5
    computed while hashing. Returns (entries, sidecar rel paths)."""
    from ocr_sidecar import is_sidecar  # heavy imports; only when needed

    keep, sidecars = [], set()
    for e in entries:
        if is_sidecar(e.rel.rpartition("/")[2], e.md5):
            sidecars.add(e.rel)
        else:
            keep.append(e)
    return keep, sidecars


def tree_hash(pairs):
    """Identity of a tree: its (rel_path, sha256) pairs, order-independent."""
    h = hashlib.sha256()
    for rel, sha in sorted(pairs):
        h.update(f"{rel}\t{sha}\n".encode())
    return h.hexdigest()


def changes_since(root, excludes, min_age, entries, skip, tracked=None, keep=()):
    """What differs between the tree now and the entries hashed at the start."""
    files, held, _ = collect(root, excludes, min_age, tracked=tracked, keep=keep)
    now = {rel: identity(st) for rel, _, st in files}
    then = {e.rel: e.ident for e in entries}
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


def _jsonl(row):
    # ASCII-only, so no raw U+2028 or other line separator can split a row.
    return json.dumps(row, ensure_ascii=True) + "\n"


class Ledger:
    """Append-only JSONL: keys known to be stored, manifests written, and a
    hash cache by file identity. Reading never writes; a torn last line is
    fixed on the next append."""

    def __init__(self, path, bucket, log=print):
        self.path, self.bucket = path, bucket
        self.stored, self.manifests, self.hashes = set(), {}, {}
        self._lock = threading.Lock()
        self._torn = False
        if not path.exists():
            return
        raw = path.read_bytes()
        self._torn = bool(raw) and not raw.endswith(b"\n")
        for n, line in enumerate(raw.decode(errors="replace").split("\n"), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                log(f"ledger line {n} unreadable (interrupted write?); ignoring it")
                continue
            self._note(r)

    def _note(self, r):
        if r.get("kind") == "hash":
            self.hashes[r["path"]] = r
        elif r.get("bucket") != self.bucket:
            return
        elif r.get("kind") == "manifest":
            self.manifests[r["collection"]] = r
        elif r.get("key"):
            self.stored.add(r["key"])

    def cached(self, path, ident, want_md5):
        r = self.hashes.get(str(path))
        if r and tuple(r["ident"]) == ident and (r.get("md5") or not want_md5):
            return r["sha256"], r.get("md5")
        return None

    def record(self, **row):
        row = {"bucket": self.bucket, **row, "at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                if self._torn:  # don't glue this row onto a torn one
                    f.write("\n")
                    self._torn = False
                f.write(_jsonl(row))
            self._note(row)


def _once(write):
    """Run a conditional write: 'uploaded', or 'present' if the key already exists."""
    try:
        write()
    except Exception as e:
        if getattr(e, "response", {}).get("Error", {}).get("Code") == "PreconditionFailed":
            return "present"
        raise
    return "uploaded"


def put_once(s3, bucket, key, body, ctype, sha256):
    return _once(lambda: s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=ctype,
                                       ChecksumSHA256=b64(sha256), IfNoneMatch="*"))


def upload(s3, bucket, key, path, size, sha256):
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if size <= PART_SIZE:
        with path.open("rb") as f:
            return put_once(s3, bucket, key, f, ctype, sha256)
    return _once(lambda: _multipart(s3, bucket, key, path, ctype, sha256))


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


def _hash(item, collection, want_md5, ledger):
    """Returns (entry, freshly hashed?)."""
    rel, path, st = item
    if STOP.is_set():
        raise Stopped(rel)
    ident = identity(st)
    hit = ledger.cached(path, ident, want_md5)
    sha, md5 = hit or file_digests(path, md5=want_md5)
    if not hit and not unchanged(path, ident):
        raise Changed("changed while hashing")
    return Entry(rel, path, ident, sha, md5, object_key(collection, rel, sha)), not hit


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def sync(s3, bucket, collection, root, ledger, *, excludes=(), keep=(), tracked=None,
         skip_ocr_sidecars=False, min_age=600, workers=8, allow_shrink=False, dry_run=False,
         log=print):
    """Store root under <collection>/ and, once the whole tree is stored, write its
    manifest. Returns the exit status described in the module docstring."""
    STOP.clear()
    files, held, ignored = collect(root, excludes, min_age, tracked=tracked, keep=keep)
    if not files and not held:
        log(f"{collection}: nothing under {root}; refusing to write an empty manifest")
        return 2

    entries, failed = [], 0

    def hashed(item, fut):
        nonlocal failed
        try:
            entry, fresh = fut.result()
        except FileNotFoundError:
            held.append((item[0], "vanished before hashing"))
        except Changed as e:
            held.append((item[0], str(e)))
        except Exception as e:
            failed += 1
            log(f"  ERROR {item[0]}: {e}")
        else:
            entries.append(entry)
            if fresh and not dry_run:
                ledger.record(kind="hash", path=str(entry.path), ident=entry.ident,
                              sha256=entry.sha256, md5=entry.md5)

    run_pool(lambda item: _hash(item, collection, skip_ocr_sidecars, ledger), files, workers, hashed)
    entries.sort()
    sidecars = set()
    if skip_ocr_sidecars:
        entries, sidecars = split_ocr_sidecars(entries)

    # Count the tree as the manifest will, sidecars out, before anything uploads.
    last = ledger.manifests.get(collection)
    found = len(entries) + len(held)
    if last and not allow_shrink and found < SHRINK_LIMIT * last["files"]:
        log(f"{collection}: {found} files, down from {last['files']} in {last['key']}. "
            "Pass --allow-shrink if the tree really shrank.")
        return 2

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
            if isinstance(exc, (Changed, FileNotFoundError)) or not unchanged(e.path, e.ident):
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
        held += changes_since(root, excludes, min_age, entries, sidecars, tracked=tracked, keep=keep)
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
    body = "".join(_jsonl({
        "collection": collection, "rel_path": e.rel, "key": e.key, "sha256": e.sha256,
        "bytes": e.size, "mtime": datetime.fromtimestamp(e.ident[3] / 1e9, timezone.utc).isoformat(),
    }) for e in entries).encode()
    status = put_once(s3, bucket, mkey, body, "application/x-ndjson", hashlib.sha256(body).hexdigest())
    ledger.record(kind="manifest", collection=collection, key=mkey, tree=tree,
                  files=len(entries), status=status)
    log(f"manifest: {mkey} ({len(entries)} files)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("collection", choices=sorted(COLLECTIONS))
    ap.add_argument("--min-age", type=int, default=600, metavar="SECONDS",
                    help="primary collections: hold back files modified more recently than this")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="accept a tree under half the size of the last manifest")
    ap.add_argument("--bucket", help="default: $PRA_S3_PREFIX-<account>-$PRA_S3_REGION-an")
    ap.add_argument("--ledger", type=Path, help="default: <primary checkout>/.claude/s3_upload_ledger.jsonl")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    log = functools.partial(print, flush=True)  # progress must show up in logs of background runs
    cfg = COLLECTIONS[args.collection]
    root = collection_root(args.collection)
    tracked = None
    if cfg["base"] == "checkout":
        try:
            tracked = tracked_files(checkout_root(), cfg["root"])
        except Refused as e:
            log(f"{args.collection}: refused: {e}")
            sys.exit(2)
    elif not root.is_dir():
        ap.error(f"{root} is not a directory")

    import boto3
    from botocore.config import Config

    s3 = boto3.client("s3", region_name=region(), config=Config(
        retries={"mode": "adaptive", "max_attempts": 10}, max_pool_connections=args.workers * 2))
    bucket = args.bucket or bucket_name(boto3.client("sts").get_caller_identity()["Account"])
    ledger = Ledger(args.ledger or default_ledger(), bucket, log=log)

    signal.signal(signal.SIGTERM, signal.default_int_handler)  # stop on kill like on Ctrl-C
    try:
        status = sync(s3, bucket, args.collection, root, ledger, excludes=cfg.get("exclude", ()),
                      keep=cfg.get("keep", ()), tracked=tracked,
                      skip_ocr_sidecars=cfg.get("skip_ocr_sidecars", False), min_age=args.min_age,
                      workers=args.workers, allow_shrink=args.allow_shrink, dry_run=args.dry_run, log=log)
    except KeyboardInterrupt:
        print("interrupted: queued uploads cancelled, in-flight multipart uploads aborted. "
              "Re-run to resume.", file=sys.stderr, flush=True)
        status = 130
    sys.exit(status)


if __name__ == "__main__":
    main()
