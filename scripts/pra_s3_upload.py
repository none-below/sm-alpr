"""Upload a collection of PRA assets to the write-once S3 bucket.

  AWS_PROFILE=sm-alpr-pra-writer uv run --with boto3 python scripts/pra_s3_upload.py \\
      pra-portals-ca [--dry-run]

Collections are pinned in COLLECTIONS below. A "checkout" collection is the
files under a folder of this repo at origin/main: the run fetches, resolves
origin/main to one commit, refuses unless that folder here is clean and
identical to the commit's, and checks each file's bytes against the commit's
blob id while hashing. So a stale or dirty checkout can't upload, untracked
files never do, and the git_commit a manifest records is exact. A "primary"
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
sha256, bytes, and mtime per file; for checkout collections, mtime is null (a
checkout's mtimes are just checkout time) and git_commit names the commit
whose bytes were verified). The tree is every file in the collection
except exclusions, OS metadata and (where configured) OCR sidecars. Nothing is
written while something can't be stored yet (exit 3):
  - a file that changed, appeared or vanished during the run;
  - primary collections: a symlink, or something that isn't a regular file;
  - checkout collections: a tracked file missing on disk (sparse checkout),
    or a symlink on disk where git has a regular file. A symlink git itself
    records is just a link, not a record: it's skipped and logged by name;
  - primary collections: a file modified in the last --min-age seconds, an
    unfinished download (.part, .crdownload, a Safari .download folder, ...)
    or open-document lock (~$...) not listed in the collection's "keep", or
    an unreadable folder.
Nothing is written after an upload error (exit 1) or for an unchanged tree
(exit 0). An empty tree, or a complete one with under half the files of the
last manifest, is refused before anything uploads (exit 2).

Browser-executable types (HTML, XML incl. SVG and RSS, JavaScript) are stored with
Content-Disposition: attachment, so if the bucket is ever served publicly
they download instead of running on its origin. Objects stored before that
rule (one .html in pra-portals-ca, 2026-09-25) keep their inline disposition,
since stored objects can't change; serve them through a CDN that sets the
header, or not at all.

The writer key can only PutObject (no list, no read), so what's stored is
tracked in a local ledger in the primary checkout's .claude/. Losing it costs
re-sending every file before S3 answers 412 (multipart files in full, since
the 412 only comes at completion), one redundant manifest, and the shrink
guard until the next manifest is written.

For primary collections, hashes are cached by file identity (device, inode,
size, mtime, ctime) in .claude/s3_hash_cache.json, a cache rewritten after each
run and safe to delete. That relies on a kernel-maintained ctime,
which APFS and ext4 have: tools that keep mtime across a rewrite (rsync -t,
cp -p, unzip) can't keep ctime, so changed bytes are re-hashed. FAT and exFAT
volumes don't keep a real ctime; for a collection on one, pass --rehash.

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

from sidecar_names import is_sidecar

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

MULTIPART_THRESHOLD = 64 * 1024 * 1024  # single PUT up to this size, multipart above
PART_SIZE = 16 * 1024 * 1024  # held in memory per worker while it uploads
# Types a browser would execute; stored as attachments (see docstring).
# Types a browser can run script in; plus any */*+xml (SVG, XHTML, RSS, ...).
ACTIVE_TYPES = {"text/html", "text/xml", "application/xml",
                "text/javascript", "application/javascript", "application/x-javascript"}
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
    """(files, commit): {path relative to root_rel: (git mode, blob id)} for the files
    under root_rel at origin/main, and that commit, after checking this
    checkout holds exactly that version of the folder. origin/main is resolved
    once, so a fetch by another session mid-run can't change which commit is
    checked and recorded."""
    def git(*args):
        p = subprocess.Popen(["git", "-C", str(repo), *args], text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = p.communicate()
        except BaseException:
            # subprocess.run would SIGKILL git here, stranding its lock files.
            p.terminate()
            try:
                p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
            raise
        if p.returncode:
            raise Refused(f"git {' '.join(args)}: {err.strip()}")
        return out

    if fetch:  # an explicit refspec: the remote's fetch config may not cover main
        git("fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main")
    # The full name: a local branch or tag called "origin/main" would win over
    # the remote-tracking ref if it were looked up by its short name.
    commit = git("rev-parse", "--verify", "refs/remotes/origin/main^{commit}").strip()
    dirty = git("status", "--porcelain", "--untracked-files=no", "--", root_rel)
    if dirty:
        raise Refused(f"uncommitted changes under {root_rel}:\n{dirty.rstrip()}")
    if git("rev-parse", f"HEAD:{root_rel}") != git("rev-parse", f"{commit}:{root_rel}"):
        raise Refused(f"{root_rel} here differs from origin/main (stale or unmerged); "
                      "run from a fresh worktree off origin/main")
    prefix = root_rel.rstrip("/") + "/"
    files = {}
    for line in git("ls-tree", "-r", "-z", commit, "--", root_rel).split("\0"):
        if not line:
            continue
        meta, path = line.split("\t", 1)
        mode, kind, oid = meta.split()
        if kind != "blob":
            raise Refused(f"{path} is a {kind}, not a file")
        files[path[len(prefix):]] = (mode, oid)
    return files, commit


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
    except OSError as e:  # e.g. a folder that can be listed but not entered
        held.append((rel, f"unreadable: {e.strerror}"))
        return False
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
    ignored  [(rel, why)] OS metadata; in tracked mode, symlinks git records

    With `tracked` ({rel: (git mode, blob id)}), only those files count and
    git vouches that they're finished, so their names and mtimes don't matter.
    Otherwise the folder is walked as it is on disk.
    """
    files, held, ignored = [], [], []
    if tracked is not None:
        for rel in sorted(tracked):
            if _matches(rel, excludes):
                continue
            if tracked[rel][0] == "120000":  # git records a symlink: a link, not a record
                ignored.append((rel, "committed symlink"))
                continue
            if classify(rel.rpartition("/")[2]) == "metadata":
                ignored.append((rel, "OS metadata"))
                continue
            try:
                st = (root / rel).lstat()
            except FileNotFoundError:
                held.append((rel, "tracked but not on disk (sparse checkout?)"))
                continue
            except OSError as e:
                held.append((rel, f"unreadable: {e.strerror}"))
                continue
            if stat.S_ISLNK(st.st_mode):
                held.append((rel, "a symlink on disk where git has a file "
                                  "(assume-unchanged or skip-worktree?)"))
            elif stat.S_ISREG(st.st_mode):
                files.append((rel, root / rel, st))
            else:
                held.append((rel, "not a regular file"))
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
                ignored.append((rel, "OS metadata"))
                continue
            if kind == "unfinished" and not _matches(rel, keep):
                held.append((rel, "unfinished download or open document (list it in keep if it's real)"))
                continue
            st = _regular(rel, path, held)
            if st is None:
                held.append((rel, "vanished while listing"))
            elif st and min_age is not None and -FUTURE_SLACK <= now - st.st_mtime < min_age:
                held.append((rel, f"modified in the last {min_age}s"))
            elif st:
                files.append((rel, path, st))
    for err in errors:
        rel = Path(err.filename).relative_to(root).as_posix() if err.filename else "."
        held.append((rel, f"unreadable folder: {err.strerror}"))
    files.sort()
    return files, held, ignored


def file_digests(path, md5=False, blob_size=None):
    """(sha256, md5, git blob id) hex digests from a single read of the file;
    md5 only if asked, the blob id only given the size git will hash."""
    sha, m = hashlib.sha256(), hashlib.md5() if md5 else None
    blob = None
    if blob_size is not None:
        blob = hashlib.sha1(b"blob %d\0" % blob_size)
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            if STOP.is_set():
                raise Stopped(str(path))
            sha.update(chunk)
            if m:
                m.update(chunk)
            if blob:
                blob.update(chunk)
    return sha.hexdigest(), m.hexdigest() if m else None, blob.hexdigest() if blob else None


def unchanged(path, ident):
    try:
        return identity(path.stat()) == ident
    except OSError:  # gone, or no longer readable: either way not the file we hashed
        return False


def split_ocr_sidecars(entries):
    """Separate the OCR sidecars (current, stale or orphaned) using the MD5
    computed while hashing. Returns (entries, sidecar rel paths)."""
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


def changes_since(root, excludes, entries, skip, tracked=None, keep=()):
    """What differs between the tree now and the entries hashed at the start.
    Identity alone decides; the clock doesn't, so a future-dated file that
    drifts into the --min-age window isn't held back."""
    files, held, _ = collect(root, excludes, min_age=None, tracked=tracked, keep=keep)
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


class HashCache:
    """{path: identity and digests} for primary-collection files. A cache:
    rewritten whole after each run (atomically), and safe to lose; concurrent
    runs can only cost each other re-hashing."""

    def __init__(self, path):
        self.path, self.rows = path, {}
        with contextlib.suppress(OSError, ValueError):
            self.rows = json.loads(path.read_text())

    def get(self, path, ident, want_md5):
        r = self.rows.get(str(path))
        if r and tuple(r["ident"]) == ident and (r.get("md5") or not want_md5):
            return r["sha256"], r.get("md5")
        return None

    def put(self, path, ident, sha256, md5):
        self.rows[str(path)] = {"ident": list(ident), "sha256": sha256, "md5": md5}

    def save(self, root, seen=None):
        """Write, first dropping entries under root that this run didn't see
        (only when `seen` is the whole tree, not an interrupted pass)."""
        if seen is not None:
            prefix = str(root).rstrip("/") + "/"
            self.rows = {p: r for p, r in self.rows.items() if not p.startswith(prefix) or p in seen}
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self.rows))
        os.replace(tmp, self.path)


def default_hash_cache():
    return primary_root() / ".claude" / "s3_hash_cache.json"


class Ledger:
    """Append-only JSONL of keys known to be stored and manifests written.
    Reading never writes; a torn last line is fixed on the next append."""

    def __init__(self, path, bucket, log=print):
        self.path, self.bucket = path, bucket
        self.stored, self.manifests = set(), {}
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
        if r.get("bucket") != self.bucket or r.get("kind") == "hash":  # hash rows: older versions
            return
        elif r.get("kind") == "manifest":
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
                f.write(_jsonl(row))
            self._note(row)


def _once(write):
    """Run a conditional write: 'uploaded', or 'present' if the key already exists."""
    try:
        write()
    except Exception as e:
        error = (getattr(e, "response", None) or {}).get("Error") or {}
        if error.get("Code") == "PreconditionFailed":
            return "present"
        raise
    return "uploaded"


def headers_for(name):
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    headers = {"ContentType": ctype}
    if ctype in ACTIVE_TYPES or ctype.endswith("+xml"):
        headers["ContentDisposition"] = "attachment"
    return headers


def put_once(s3, bucket, key, body, headers, sha256):
    return _once(lambda: s3.put_object(Bucket=bucket, Key=key, Body=body, **headers,
                                       ChecksumAlgorithm="SHA256", ChecksumSHA256=b64(sha256),
                                       IfNoneMatch="*"))


def upload(s3, bucket, key, path, size, sha256):
    headers = headers_for(path.name)
    if size <= MULTIPART_THRESHOLD:
        with path.open("rb") as f:
            return put_once(s3, bucket, key, f, headers, sha256)
    return _once(lambda: _multipart(s3, bucket, key, path, headers, sha256))


def _multipart(s3, bucket, key, path, headers, sha256):
    upload_id = s3.create_multipart_upload(
        Bucket=bucket, Key=key, **headers, ChecksumAlgorithm="SHA256",
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


def run_pool(fn, items, workers, on_done, log=print):
    """Call on_done(item, future) as each fn(item) finishes. On interrupt, cancel
    queued work, let in-flight work stop at its next STOP check, and re-raise.

    What a second Ctrl-C does while this waits is main()'s signal handler's call."""
    pool, futures = ThreadPoolExecutor(workers), {}
    try:
        for item in items:
            futures[pool.submit(fn, item)] = item
        for fut in as_completed(futures):
            on_done(futures[fut], fut)
    except BaseException:
        STOP.set()
        busy = sum(f.running() for f in futures)
        if busy:
            # stdout may be a pipe whose reader (tee) got the same Ctrl-C;
            # a failed message must not skip the wait below.
            with contextlib.suppress(OSError):
                log(f"stopping: waiting for {busy} in-flight task(s); Ctrl-C again quits now")
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown()


def _hash(item, collection, want_md5, cache, expect_blob=None, rehash=False):
    """Returns (entry, freshly hashed and worth caching?). With expect_blob (a
    tracked file), the bytes must match that git blob, and nothing is cached."""
    rel, path, st = item
    if STOP.is_set():
        raise Stopped(rel)
    ident = identity(st)
    hit = None if expect_blob or rehash or cache is None else cache.get(path, ident, want_md5)
    if hit:
        sha, md5 = hit
    else:
        sha, md5, blob = file_digests(path, md5=want_md5,
                                      blob_size=st.st_size if expect_blob else None)
        if not unchanged(path, ident):
            raise Changed("changed while hashing")
        if expect_blob and blob != expect_blob:
            raise Changed("bytes differ from the verified commit (assume-unchanged or edited?)")
    entry = Entry(rel, path, ident, sha, md5, object_key(collection, rel, sha))
    return entry, not hit and not expect_blob


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def sync(s3, bucket, collection, root, ledger, *, cache=None, excludes=(), keep=(), tracked=None,
         commit=None, skip_ocr_sidecars=False, min_age=600, workers=8, allow_shrink=False,
         rehash=False, dry_run=False, log=print):
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
        except PermissionError as e:
            held.append((item[0], f"unreadable: {e.strerror}"))
        except Changed as e:
            held.append((item[0], str(e)))
        except Exception as e:
            failed += 1
            log(f"  ERROR {item[0]}: {e}")
        else:
            entries.append(entry)
            if fresh and cache is not None:
                cache.put(entry.path, entry.ident, entry.sha256, entry.md5)

    def hash_one(item):
        expect = tracked[item[0]][1] if tracked is not None else None
        return _hash(item, collection, skip_ocr_sidecars, cache, expect, rehash)

    complete = False
    try:
        run_pool(hash_one, files, workers, hashed, log)
        complete = True
    finally:
        if cache is not None and not dry_run:
            try:
                cache.save(root, {str(e.path) for e in entries} if complete else None)
            except OSError as e:  # a cache: never worth failing the run over
                with contextlib.suppress(OSError):
                    log(f"  hash cache not saved ({e.strerror}); the next run re-hashes")
    entries.sort()
    sidecars = set()
    if skip_ocr_sidecars:
        entries, sidecars = split_ocr_sidecars(entries)
    if not entries and not held and not failed:
        log(f"{collection}: nothing but OCR sidecars under {root}; refusing to write an empty manifest")
        return 2

    # Compare the tree the way its manifest would count it (sidecars out), before
    # anything uploads. Only a complete tree can be judged; with anything held
    # back or failed, no manifest will be written anyway, and the run exits 3 or 1.
    last = ledger.manifests.get(collection)
    if last and not held and not failed and not allow_shrink and len(entries) < SHRINK_LIMIT * last["files"]:
        log(f"{collection}: {len(entries)} files, down from {last['files']} in {last['key']}. "
            "Pass --allow-shrink if the tree really shrank.")
        return 2

    todo = [e for e in entries if e.key not in ledger.stored]
    todo_bytes = sum(e.size for e in todo)
    log(f"{collection}: {len(entries)} files, {human(sum(e.size for e in entries))}; "
        f"{len(entries) - len(todo)} already stored, {len(todo)} to upload ({human(todo_bytes)})")
    if sidecars:
        log(f"  OCR sidecars skipped: {len(sidecars)}")
    if metadata := [rel for rel, why in ignored if why == "OS metadata"]:
        log(f"  OS metadata ignored: {len(metadata)}")
    for rel, why in ignored:
        if why != "OS metadata":
            log(f"  skipped ({why}): {rel}")
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
        if STOP.is_set():
            raise Stopped(e.rel)
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

    run_pool(put, todo, workers, uploaded, log)

    failed += counts["error"]
    if not failed and not held:
        held += changes_since(root, excludes, entries, sidecars, tracked=tracked, keep=keep)
    for rel, reason in held[logged:]:
        log(f"  held back ({reason}): {rel}")
    if failed:
        log(f"{failed} file(s) failed; no manifest written. Re-run to retry.")
        return 1
    if held:
        log(f"{len(held)} item(s) held back; no manifest written. Re-run once the tree settles.")
        return 3
    if last and last["tree"] == tree:
        log(f"manifest: tree unchanged since {last['key']}; not writing another")
        return 0
    # Microseconds: "largest name is newest" must hold even for runs in the same second.
    run_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    mkey = f"_manifests/{collection}/{run_at}-{tree[:12]}.jsonl"
    body = "".join(_jsonl({
        "collection": collection, "rel_path": e.rel, "key": e.key, "sha256": e.sha256,
        "bytes": e.size,
        "mtime": None if tracked is not None else datetime.fromtimestamp(e.ident[3] / 1e9, timezone.utc).isoformat(),
        **({"git_commit": commit} if commit else {}),
    }) for e in entries).encode()
    status = put_once(s3, bucket, mkey, body, {"ContentType": "application/x-ndjson"},
                      hashlib.sha256(body).hexdigest())
    ledger.record(kind="manifest", collection=collection, key=mkey, tree=tree,
                  files=len(entries), status=status)
    log(f"manifest: {mkey} ({len(entries)} files)")
    return 0


def _interrupt_handler():
    """SIGINT/SIGTERM handler for a whole run: the first raises KeyboardInterrupt
    (a graceful stop); a repeat within a second is ignored, since `uv run`
    forwards the terminal's Ctrl-C and the child gets it twice; a later one
    quits at once, e.g. while a PUT sits in botocore's retries on a dead
    network (open multipart uploads then expire under the 7-day lifecycle rule)."""
    first = []

    def handler(signum, frame):
        now = time.monotonic()
        if not first:
            first.append(now)
            raise KeyboardInterrupt
        if now - first[0] > 1:
            # uv run forwards this same Ctrl-C to us; exiting before it does
            # makes uv lose track of the child and report 2 instead of 130.
            time.sleep(0.25)
            os._exit(130)

    return handler


def main(argv=None, s3=None):
    """Command line. `s3` lets tests pass a stand-in client (then --bucket is required)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("collection", choices=sorted(COLLECTIONS))
    ap.add_argument("--min-age", type=int, default=600, metavar="SECONDS",
                    help="primary collections: hold back files modified more recently than this")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="accept a tree with under half the files of the last manifest")
    ap.add_argument("--bucket", help="default: $PRA_S3_PREFIX-<account>-$PRA_S3_REGION-an")
    ap.add_argument("--ledger", type=Path, help="default: <primary checkout>/.claude/s3_upload_ledger.jsonl")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rehash", action="store_true",
                    help="ignore the hash cache (needed on FAT/exFAT, where ctime isn't kept)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    log = functools.partial(print, flush=True)  # progress must show up in logs of background runs
    handler = _interrupt_handler()
    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        status = _run(args, s3, log)
    except KeyboardInterrupt:
        with contextlib.suppress(OSError):
            print("interrupted: queued uploads cancelled, in-flight ones finished or aborted. "
                  "Re-run to resume.", file=sys.stderr, flush=True)
        _exit(130)  # our handler stays: a forwarded duplicate must not kill us with 143
    for sig, h in previous.items():
        signal.signal(sig, h)
    _exit(status)


def _exit(status):
    """sys.exit, except that stdout's reader having gone (tee killed by the
    same Ctrl-C) can't turn the status into Python's 120."""
    try:
        sys.stdout.flush()
    except OSError:
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    sys.exit(status)


def _run(args, s3, log):
    error = functools.partial(print, file=sys.stderr, flush=True)
    cfg = COLLECTIONS[args.collection]
    root = collection_root(args.collection)
    tracked = commit = None
    if cfg["base"] == "checkout":
        try:
            tracked, commit = tracked_files(checkout_root(), cfg["root"])
        except Refused as e:
            log(f"{args.collection}: refused: {e}")
            return 2
    elif not root.is_dir():
        error(f"{root} is not a directory")
        return 2

    if s3 is None:
        s3, bucket = _aws_client(args)
    elif not args.bucket:
        error("--bucket is required with a stand-in client")
        return 2
    else:
        bucket = args.bucket
    ledger = Ledger(args.ledger or default_ledger(), bucket, log=log)
    cache = HashCache(default_hash_cache()) if tracked is None else None
    return sync(s3, bucket, args.collection, root, ledger, cache=cache, excludes=cfg.get("exclude", ()),
                keep=cfg.get("keep", ()), tracked=tracked, commit=commit,
                skip_ocr_sidecars=cfg.get("skip_ocr_sidecars", False), min_age=args.min_age,
                workers=args.workers, allow_shrink=args.allow_shrink, rehash=args.rehash,
                dry_run=args.dry_run, log=log)


def _aws_client(args):
    import boto3
    from botocore.config import Config

    # payload_signing_enabled=False: over HTTPS the body's integrity is already
    # covered by ChecksumSHA256, which S3 verifies; signing it too hashes every
    # byte a second time.
    s3 = boto3.client("s3", region_name=region(), config=Config(
        retries={"mode": "adaptive", "max_attempts": 10}, max_pool_connections=args.workers * 2,
        s3={"payload_signing_enabled": False}))
    bucket = args.bucket or bucket_name(boto3.client("sts").get_caller_identity()["Account"])
    return s3, bucket


if __name__ == "__main__":
    main()
