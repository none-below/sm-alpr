"""The upload library: connectors hand it the files they fetch; it stages each
one for the ingest Lambda and waits for the outcome.

For each file it:
  1. checks what it was told about the file (source, fetch, response,
     listing) under write policy before reading a byte, so a credential in a
     URL is refused before anything moves;
  2. streams the bytes into the staging bucket as in/<uuid>.bin, hashing them
     as they pass: one PUT up to PART_SIZE, else a multipart upload cut at
     schema.upload_part_size(size), every request carrying its SHA-256.
     Nothing touches local disk; parts wait in memory, MAX_IN_FLIGHT bytes
     of them uploading plus the part being read;
  3. checks the first bytes against expect_types and the byte count against
     the declared length, and builds and validates the sidecar, all before
     the data becomes an object (a multipart upload is aborted instead), so a
     truncated download or a bad sidecar leaves nothing in staging;
  4. writes the sidecar in/<uuid>.json, the commit marker, last;
  5. waits for the Lambda: the intake record _intake/<uuid>.json, checked
     against what it hashed itself, or the staging tags for a file rejected
     or deferred, until its own timeout.

A Request groups one run's files for one PRA request. When its with block
ends it stores the fetch manifest: every file the run found, held (with its
sha256), failed (and why) or needing approval. A manifest lists a file held
only once the library has read its intake record.

The library can't read staging objects or list any bucket: it runs with
exactly PERMISSIONS, the writer role's grants. Logs carry uuids, sizes,
outcomes and reason codes, never a filename, URL or other presented text.
"""
import hashlib
import http.client
import io
import json
import os
import stat
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import schema as s

LIBRARY_VERSION = "1"  # fetch.library_version; bump when what the library writes or checks changes
# Part sizes a source's multipart ETag is tried at: MuckRock and rclone (5 MiB),
# the AWS CLI and boto3 (8 MiB), s3cmd (15 MiB), this library (16 MiB). With
# no declared length all four are hashed (MD5 at several hundred MB/s each,
# well above download speeds). checks.etag "unmatched" means no tried size
# reproduced the ETag (an uploader cutting other parts), not that the bytes
# changed: the evidence is the sha256 either way.
ETAG_PART_SIZES = (s.MUCKROCK_ETAG_PART_SIZE, 8 * s.MiB, 15 * s.MiB, 16 * s.MiB)
READ_CHUNK = s.MiB  # how much the library asks a body for at a time
MAX_EMPTY_CHUNKS = 1024  # empty chunks in a row from an iterable body before it's taken for a bug that never ends
WORKERS = 4  # concurrent UploadPart calls
MAX_IN_FLIGHT = 64 * s.MiB  # part bytes held while their uploads run (at least one part), plus the one being read
POLL_FIRST, POLL_MAX = 1.0, 15.0  # seconds between checks for outcomes, doubling
POLL_WORKERS = 16  # files checked at once
MAX_SKEW = timedelta(seconds=60)  # a connector's time this far ahead of the library's is the clock stepping back
BASE_TIMEOUT, TIMEOUT_RATE, MAX_TIMEOUT = 120.0, 30_000_000, 1200.0  # seconds, bytes/s, seconds
CONFLICT_RETRIES = 5  # a Complete answered 409 (its first attempt still running) is sent again, backing off

# The writer role's grants, {bucket role: {key prefix: actions}}; PR 4 builds
# its policy from this, and the tests run the library with exactly these.
# Without s3:ListBucket a record not written yet reads as 403, as the library
# expects. No s3:GetObject on staging (it never reads what it staged), and no
# tagging (the bucket policy lets only the Lambda and the sweep tag).
PERMISSIONS = {
    "staging": {s.STAGING_PREFIX: ("s3:PutObject", "s3:AbortMultipartUpload", "s3:GetObjectTagging")},
    "evidence": {s.RECORD_PREFIX: ("s3:GetObject",)},
}

# Outcomes. held, rejected, deferred and needs_approval are terminal: a
# connector acks its work item. pending and failed are not: fetch it again.
HELD, REJECTED, DEFERRED, NEEDS_APPROVAL = "held", "rejected", "deferred", "needs_approval"
PENDING, FAILED = "pending", "failed"
TERMINAL = frozenset({HELD, REJECTED, DEFERRED, NEEDS_APPROVAL})
# Reasons for a failed result, also a manifest's reason for a file not held.
TRUNCATED = "truncated"  # the body ended before its declared length
OVERLONG = "overlong"  # the body ran past its declared length
UNEXPECTED_TYPE = "unexpected_type"  # the first bytes aren't one of expect_types
UNDECLARED_LENGTH = "undeclared_length"  # too large to stage without knowing the size first
OVER_S3_LIMIT = "over_s3_limit"  # declared larger than S3 can hold (schema.MAX_OBJECT_SIZE)
PARTIAL_RESPONSE = "partial_response"  # a Content-Range that isn't the whole file
READ_ERROR = "read_error"  # the body raised while read (a Request records it; Intake.stage raises BodyError)
# A live response other than 200 or 203 fails as http_<status>, e.g. http_404.
RECORD_MISMATCH = "record_mismatch"  # unchanged() named another file's record
STILL_ENCODED = "still_encoded"  # gzip bytes under Content-Encoding: gzip (the raw body, not the file)
CHANGED = "changed"  # the file on disk changed while it was read
GIT_MISMATCH = "git_mismatch"  # a git backfill's bytes aren't the blob its commit names


_BYTES = (bytes, bytearray, memoryview)


class BodyError(Exception):
    """The body failed while the library read it, as a dropped connection
    does (its exception is the __cause__); nothing was committed. Any other
    exception from the body (a TypeError, an AttributeError: a connector
    bug) propagates as itself."""


class InvalidContext(s.SchemaError):
    """A SchemaError in what a connector said about one file (its source,
    fetch fields, response or listing), found before any byte moved. A
    Request records the file failed with its reason code instead of
    losing the whole manifest."""


class IntakeError(Exception):
    """S3 or the Lambda answered something the library can't reconcile with
    what it sent: never a property of the file, always worth a look."""


@dataclass(frozen=True)
class Result:
    status: str  # held | rejected | deferred | needs_approval | pending | failed
    uuid: str = None
    sha256: str = None  # as the library hashed it (None if it never finished reading)
    size: int = None  # bytes read, or the declared size of a file needing approval
    reason: str = None  # the reject code, or why it failed or needs approval
    record: dict = None  # the intake record, when held
    final: bool = False  # the connector gave up on it (Request.failed(..., final=True))

    @property
    def terminal(self):
        """Nothing to retry: ack the work item."""
        return self.status in TERMINAL or self.final


@dataclass(frozen=True)
class Response:
    """What the connector's HTTP client saw: the status and header pairs (as
    received, repeats included) of the response that delivered the bytes,
    the URL that answered, and each redirect hop on the way as (status, the
    URL that answered it, its Location)."""
    status: int
    headers: tuple
    url: str
    redirects: tuple = ()

    @classmethod
    def from_requests(cls, resp):
        """A requests.Response (history holds the hops). Headers come from
        urllib3's raw response, which keeps repeats apart: requests joins
        them, so a source sending the same ETag twice would read as opaque."""
        return cls(resp.status_code, _header_pairs(resp), resp.url,
                   tuple((h.status_code, h.url, h.headers.get("location", "")) for h in resp.history))

    @classmethod
    def from_playwright(cls, resp):
        """A Playwright APIResponse (context.request.get) or page Response. A
        page Response's hops come from its request's redirected_from chain;
        an APIResponse doesn't expose the hops it followed, so it has none."""
        pairs = resp.headers_array
        pairs = pairs() if callable(pairs) else pairs  # a method on Response, a property on APIResponse
        hops, req = [], getattr(resp, "request", None)
        prev = req.redirected_from if req is not None else None
        while prev is not None:
            answered = prev.response()
            if answered is not None:
                hops.append((answered.status, prev.url, answered.header_value("location") or ""))
            prev = prev.redirected_from
        return cls(resp.status, tuple((h["name"], h["value"]) for h in pairs), resp.url, tuple(reversed(hops)))

    @property
    def requested_url(self):
        """The URL the fetch began at, before any redirect: pass it as the
        source url (the library strips it to its stable form)."""
        return self.redirects[0][1] if self.redirects else self.url

    def sidecar(self):
        """The sidecar's response object: headers sanitized, URLs in their
        stored form (no query)."""
        return {"status": self.status, "headers": s.sanitize_headers(self.headers),
                "redirects": [{"status": st, "url": s.redirect_url(base, loc)} for st, base, loc in self.redirects],
                "final_url": s.redirect_url(self.url, self.url) or None}


class Staged:
    """A file handed to the library. result() waits for its outcome (at once
    for one refused before staging)."""

    def __init__(self, intake, u=None, *, sha256=None, size=None, sidecar_sha256=None, staging_etag=None,
                 timeout=None, result=None):
        self._intake, self.uuid, self.sha256, self.size = intake, u, sha256, size
        self._sidecar_sha256, self._staging_etag, self._timeout, self._result = (
            sidecar_sha256, staging_etag, timeout, result)

    def result(self):
        return self._intake.wait([self])[0]


_UNRESOLVED = object()  # a file whose check raised this round


class _Stop(Exception):
    """Ends staging early with a result: nothing is left behind."""

    def __init__(self, status, reason, size=None):
        super().__init__(reason)
        self.status, self.reason, self.size = status, reason, size


class _Reader:
    """A body as read_full(n): bytes, a file-like object whose read() gives
    bytes, or an iterable of bytes chunks; anything else (an int, a str, an
    array) is refused rather than turned into other bytes. Notes when the
    first byte arrived and when the body ended."""

    def __init__(self, body, now):
        self._now, self.first_at, self.end_at = now, None, None
        if isinstance(body, _BYTES):
            self._read = io.BytesIO(body if isinstance(body, (bytes, bytearray)) else bytes(body)).read
        elif hasattr(body, "read"):
            self._read = body.read
        elif isinstance(body, (str, dict)) or not hasattr(body, "__iter__"):
            raise TypeError("a body is bytes, a binary file-like object, or an iterable of bytes")
        else:
            self._read = _chunks_reader(iter(body))

    def read_full(self, n):
        """Up to n bytes: fewer only at the end of the body."""
        out = bytearray()
        while len(out) < n:
            try:
                chunk = self._read(min(READ_CHUNK, n - len(out)))
            except Exception as e:
                if not _transport_error(e):
                    raise
                raise BodyError(f"the body failed: {type(e).__name__}") from e
            if not isinstance(chunk, _BYTES):
                raise TypeError("a body gave something other than bytes")
            if not chunk:
                self.end_at = self.end_at or self._now()
                break
            if self.first_at is None:
                self.first_at = self._now()
            out += chunk
        return out


def _chunks_reader(chunks):
    carry, at, empty = b"", 0, 0

    def read(n):
        nonlocal carry, at, empty
        while at >= len(carry):
            try:
                chunk = next(chunks)
            except StopIteration:
                return b""
            if not isinstance(chunk, _BYTES):
                return chunk  # read_full refuses it (bytes(3) would be three zero bytes)
            carry, at = bytes(chunk), 0  # an empty chunk isn't the end...
            empty = 0 if carry else empty + 1
            if empty >= MAX_EMPTY_CHUNKS:  # ...but endless ones are (iter(read, '') never meets b"")
                raise TypeError("the body yields only empty chunks")
        out, at = carry[at:at + n], at + n  # an offset, not carry[n:]: one huge chunk isn't re-copied per read
        return out
    return read


def _read_all(stream, limit):
    """Up to `limit` bytes of a small S3 body (reads may come up short); its
    errors stay botocore's, so a dropped connection is retried."""
    chunks, got = [], 0
    while got < limit:
        chunk = stream.read(min(READ_CHUNK, limit - got))
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


class _Parts:
    """MD5s of consecutive parts of one size (the last may be short): a
    source's multipart ETag, if it was cut at this size."""

    def __init__(self, size):
        self.size, self._done, self._in = size, [], 0
        self._hash = hashlib.md5(usedforsecurity=False)

    def update(self, chunk):
        view = memoryview(chunk)
        while view:
            take = min(len(view), self.size - self._in)
            self._hash.update(view[:take])
            self._in, view = self._in + take, view[take:]
            if self._in == self.size:
                self._done.append(self._hash.hexdigest())
                self._hash, self._in = hashlib.md5(usedforsecurity=False), 0

    def etag(self):
        return s.md5_multipart_etag(self._done + ([self._hash.hexdigest()] if self._in or not self._done else []))


class _Digest:
    """What the library learns from the bytes as they pass, in order."""

    def __init__(self, etag_part_sizes, *, git_size=None):
        self.size, self.head = 0, b""
        self.sha256, self.md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
        self.etag_parts = [_Parts(n) for n in etag_part_sizes]
        self.git = None  # git's blob id: sha1 of "blob <size>\0" + the bytes, for a git backfill
        if git_size is not None:
            self.git = hashlib.sha1(b"blob %d\0" % git_size, usedforsecurity=False)

    def update(self, chunk):
        if len(self.head) < s.SNIFF_BYTES:
            self.head += bytes(chunk[:s.SNIFF_BYTES - len(self.head)])
        self.size += len(chunk)
        for h in (self.sha256, self.md5, self.git, *self.etag_parts):
            if h is not None:
                h.update(chunk)


def _code(exc):
    return ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code")


def _http_status(exc):
    return ((getattr(exc, "response", None) or {}).get("ResponseMetadata") or {}).get("HTTPStatusCode")


def _is_precondition(exc):
    return _code(exc) == "PreconditionFailed" or _http_status(exc) == 412


def _is_absent(exc):
    """No such key, or (without s3:ListBucket, as the writer runs) AccessDenied.
    Other 403s (a bad signature, an unknown key id) are errors."""
    return _code(exc) in ("NoSuchKey", "404", "NotFound", "AccessDenied", "403")


_THROTTLES = frozenset({"SlowDown", "Throttling", "ThrottlingException", "RequestTimeout", "RequestLimitExceeded",
                        "ServiceUnavailable", "InternalError"})
# botocore's errors for a connection that failed or dropped (checked against
# botocore 1.43): EndpointConnectionError and ConnectTimeoutError are
# ConnectionErrors; ConnectionClosedError, ReadTimeoutError and
# ResponseStreamingError are HTTPClientErrors. Its other errors without a
# response (NoCredentialsError, TokenRetrievalError, ParamValidationError...)
# won't fix themselves.
_TRANSPORT_ERRORS = frozenset({"ConnectionError", "HTTPClientError", "IncompleteReadError"})
# urllib3's own (its SSLError can escape StreamingBody.read, which wraps only
# ReadTimeoutError and ProtocolError) are transport errors too.


def _transport_error(exc):
    """A connection that failed, dropped or timed out: OSError (requests'
    exceptions are OSErrors), EOFError, http.client's, urllib3's, and
    botocore's transport errors. Not a programming error."""
    return isinstance(exc, (OSError, EOFError, http.client.HTTPException)) or any(
        (k.__module__ == "botocore.exceptions" and k.__name__ in _TRANSPORT_ERRORS)
        or (k.__module__ == "urllib3.exceptions" and k.__name__ == "HTTPError") for k in type(exc).__mro__)


def _retryable(exc):
    """A throttle, a 5xx or a dropped connection botocore gave up retrying:
    worth another look on the next round."""
    status = _http_status(exc)
    if status is not None:
        return status >= 500 or status == 429 or _code(exc) in _THROTTLES
    return _transport_error(exc)


def _etag_check(headers, md5_hex, md5_multipart):
    """checks.etag: how the source's ETag relates to these bytes."""
    if "etag" not in headers:
        return "absent"
    form, body = s.etag_form(headers["etag"])
    if form == "opaque":
        return "opaque"
    if form == "md5":
        return "md5" if body == md5_hex else "unmatched"
    return "md5-multipart" if md5_multipart is not None and md5_multipart["etag"] == body else "unmatched"


def _observed(body):
    """Whether reading `body` is watching the fetch: not for bytes already in
    memory (BytesIO included) or a regular file on disk, which arrived
    before the library saw them."""
    if isinstance(body, (*_BYTES, io.BytesIO)):
        return False
    try:
        return not stat.S_ISREG(os.fstat(body.fileno()).st_mode)
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return True


def _header_pairs(resp):
    raw = getattr(getattr(resp, "raw", None), "headers", None)
    return tuple(raw.items() if raw is not None and hasattr(raw, "items") else resp.headers.items())


def _check_legacy_path(path):
    """A backfill's old path as a repo- or archive-relative path, never an
    absolute one (it would store a home directory in a permanent record)."""
    if type(path) is not str:
        return  # validate_context refuses it
    parts = path.replace("\\", "/").split("/")
    if path.startswith(("/", "~")) or (len(path) > 1 and path[1] == ":") or ".." in parts or "." in parts:
        raise ValueError("legacy_path is relative: no leading / or ~, drive letter, . or ..")


def _doc_id(value):
    return None if value is None else s.doc_id_text(value)


def _stable(url):
    """The stored form of a source URL: strip_signing_params (no secret
    parameters, no session in the path), or None."""
    return None if url is None else s.strip_signing_params(url)


def _check_chain(source_url, response, resp):
    """A redirect chain begins where the source URL points and ends where
    the bytes came from: otherwise source.url is the wrong URL (the final,
    signed one, say) and where the fetch began is lost."""
    first = response.redirects[0][1]
    if source_url is None:  # validate_context refuses a live fetch without its URL
        return
    if s.redirect_url(first, first) != s.redirect_url(source_url, source_url):
        raise ValueError("source url isn't the URL the redirects began at (pass the URL requested)")
    if resp["redirects"][-1]["url"] != resp["final_url"]:
        raise ValueError("the last redirect doesn't lead to the response's url")


# A source that passes validation, to check an Intake's own fetch fields at construction.
_PROBE_SOURCE = {"kind": "own", "platform": "other", "host": "example.org", "agency": None, "request_id": "probe",
                 "request_url": None, "doc_id": None, "filename": "probe", "title": None, "url": None,
                 "released_on": None}


def _timestamp(value, name):
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError(f"{name} must be a timezone-aware datetime")
    return s.format_timestamp(value)


class Intake:
    """Stages files for one connector run. `s3` is a boto3 S3 client with the
    writer role's grants (from_env makes one); now, clock and sleep are the
    wall clock, a monotonic clock and time.sleep, replaceable for tests.
    `log` takes one JSON line at a time, from worker threads too."""

    def __init__(self, s3, *, staging_bucket, evidence_bucket, connector, access, run_id=None,
                 connector_version=None,
                 ci_run=None, workers=WORKERS, timeout=None, log=None,
                 now=lambda: datetime.now(timezone.utc), clock=time.monotonic, sleep=time.sleep):
        envs = set()
        for name, role in ((staging_bucket, "staging"), (evidence_bucket, "evidence")):
            try:
                env, got, account, region = s.parse_bucket_name(name)
            except s.SchemaError:
                raise ValueError(f"the {role} bucket isn't an intake bucket name") from None
            if got != role:
                raise ValueError(f"the {role} bucket is a {got} bucket")
            envs.add((env, account, region))
        if len(envs) != 1:
            raise ValueError("the buckets aren't one environment's")
        self.s3, self.staging, self.evidence = s3, staging_bucket, evidence_bucket
        self.connector, self.connector_version, self.ci_run = connector, connector_version, ci_run
        self.access = access  # anonymous, or logged in as the requester: stated, never assumed
        self.run_id = run_id if run_id is not None else s.new_uuid()
        self.workers, self.timeout, self._log = workers, timeout, log
        self._now, self._clock, self._sleep = now, clock, sleep
        stamp = s.format_timestamp(now())  # every sidecar carries the connector, run and CI ids: check them now
        s.validate_context("file", _PROBE_SOURCE, self._fetch("local-copy", s.new_uuid(), stamp, None, stamp,
                                                              access=access, legacy_path="probe"), None, None)

    @classmethod
    def from_env(cls, *, connector, env=None, profile=None, region=None, **kw):
        """An Intake for `env` (dev or prod; else PRA_INTAKE_ENV), with the
        buckets named for the caller's account and region (a profile, else
        PRA_INTAKE_PROFILE or the default chain) and the GitHub Actions run,
        if there is one."""
        env = env or os.environ.get("PRA_INTAKE_ENV")
        if env not in s.ENV_PREFIXES:
            raise ValueError("pass env=, or set PRA_INTAKE_ENV to dev or prod")
        import boto3  # only where AWS is; boto3 isn't a project dependency (uv run --with boto3)
        from botocore.config import Config

        session = boto3.Session(profile_name=profile or os.environ.get("PRA_INTAKE_PROFILE") or None,
                                region_name=region or os.environ.get("PRA_INTAKE_REGION") or None)
        account = session.client("sts").get_caller_identity()["Account"]
        # payload_signing_enabled=False: TLS and every request's ChecksumSHA256
        # already cover the body; signing it too would hash each byte again.
        s3 = session.client("s3", config=Config(
            retries={"mode": "standard", "max_attempts": 10}, max_pool_connections=max(kw.get("workers", WORKERS), POLL_WORKERS) + 4,
            s3={"payload_signing_enabled": False}))
        kw.setdefault("ci_run", _ci_run(os.environ))
        return cls(s3, staging_bucket=s.bucket_name("staging", env, account, session.region_name or ""),
                   evidence_bucket=s.bucket_name("evidence", env, account, session.region_name or ""),
                   connector=connector, **kw)

    def log(self, event, **fields):
        if self._log is not None:
            self._log(json.dumps({"event": event, **fields}, sort_keys=True))

    # --- Staging ------------------------------------------------------------------------------

    def upload(self, source, body, **kw):
        """stage() and wait for the outcome: a Result."""
        return self.stage(source, body, **kw).result()

    def upload_file(self, path, source, **kw):
        """stage_file() and wait for the outcome: a Result."""
        return self.stage_file(path, source, **kw).result()

    def stage_file(self, path, source, *, origin=None, **kw):
        """Stage a file already on disk: a backfill (origin local-copy or git,
        with legacy_path), whose size the file system declares and whose
        read the library times; or a live download the client saved (with its
        response), checked against the response's own Content-Length, whose
        fetch the library didn't see (pass completed_at, as for bytes)."""
        saved = kw.get("response") is not None
        origin = origin or ("live" if saved else "local-copy")
        with open(path, "rb") as f:
            before = os.fstat(f.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("not a regular file")

            def unchanged():  # an in-place rewrite of the same length changes its times
                after = os.fstat(f.fileno())
                return all(getattr(before, k) == getattr(after, k)
                           for k in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"))
            if not saved:
                kw.setdefault("declared_length", before.st_size)
            return self._stage_body(source, f, observed=not saved, origin=origin, unchanged_file=unchanged, **kw)

    def stage(self, source, body, **kw):
        """Stream `body` into staging and commit it; returns a Staged whose
        result() waits for the Lambda.

        `body` is bytes, a binary file-like object, or an iterable of bytes
        chunks. `source` is the sidecar's source object (doc_id may be an
        int); a live fetch passes its Response. Keywords: content_kind,
        origin, response, listing, expect_types, access, attempt, retries,
        work_id, started_at, completed_at, approval, declared_length, eof,
        legacy_path, original_fetched_at, git_commit, git_blob, stamp_ref.
        `access` defaults to the Intake's.

        Fetch times: for a stream the library times the fetch itself
        (started_at, if not given, is now; then the first byte and the end
        as it reads). For bytes it didn't see the fetch: first_byte_at is
        null, started_at is what's given (or null), and completed_at is
        what's given (when the download finished) or now.

        Refused before a byte is read (SchemaError, or ValueError for an
        argument the sidecar has no field for): what the contract refuses,
        such as a URL carrying a credential or a live fetch without its URL.
        A response other than 200 or 203 isn't read: its result is failed,
        http_<status>. Raises BodyError if the body raises, or whatever S3
        raises (a multipart upload is aborted first)."""
        if kw.get("content_kind") == "fetch_manifest" or kw.get("origin") == "generated":
            raise ValueError("a fetch manifest is written only by a Request")  # its entries are the library's
        return self._stage_body(source, body, observed=_observed(body), **kw)

    def _stage_body(self, source, body, *, observed, content_kind="file", origin="live", response=None,
                    listing=None, expect_types=None, access=None, attempt=1, retries=0, work_id=None,
                    started_at=None, completed_at=None, approval=None, declared_length=None, eof=None,
                    legacy_path=None, original_fetched_at=None, git_commit=None, git_blob=None, stamp_ref=None,
                    unchanged_file=None):
        access = self.access if access is None else access
        now = self._now()
        _timestamp(started_at, "started_at")
        _timestamp(completed_at, "completed_at")
        if started_at is not None and started_at > now + MAX_SKEW or completed_at is not None and (
                completed_at > now + MAX_SKEW):
            raise ValueError("started_at or completed_at is in the future")
        # a little ahead: the wall clock stepped back since the connector read it
        started_at = min(started_at, now) if started_at is not None else None
        completed_at = min(completed_at, now) if completed_at is not None else None
        if completed_at is not None and (observed or started_at is not None and completed_at < started_at):
            raise ValueError("completed_at is for bytes or a saved file, and not before started_at")
        _timestamp(original_fetched_at, "original_fetched_at")
        if original_fetched_at is not None and original_fetched_at > now + MAX_SKEW:
            raise ValueError("original_fetched_at is in the future")
        if declared_length is not None and origin == "live":
            raise ValueError("a live fetch's declared length is its response's Content-Length")
        if origin in ("local-copy", "git"):
            if started_at is not None or completed_at is not None:
                raise ValueError("a backfill's fetch is the library's read of it; the original time is "
                                 "original_fetched_at")
            _check_legacy_path(legacy_path)
        if (origin == "git") != (git_blob is not None):
            raise ValueError("a git backfill names the file's blob id (git rev-parse <commit>:<path>), and only it")
        started = started_at if started_at is not None or not observed else now
        stamp = _timestamp(started, "started_at")
        if eof not in (None, *s.EOF_CHECKS):
            raise ValueError("eof must be one of EOF_CHECKS")
        source = dict(source)
        source["doc_id"] = _doc_id(source.get("doc_id"))
        for k in ("url", "request_url"):
            source[k] = _stable(source.get(k))
        resp = response.sidecar() if response is not None else None
        if response is not None and response.redirects:
            _check_chain(source.get("url"), response, resp)
        status = resp["status"] if resp is not None else None
        # The source answering an error is a result, not a connector bug; but
        # the rest is still checked, and a status that isn't one is refused.
        source_error = (origin == "live" and type(status) is int and 100 <= status <= 599
                        and status not in s.LIVE_STATUSES)
        fetch = self._fetch(origin, s.new_uuid(), stamp, None, s.format_timestamp(now), work_id=work_id,
                            attempt=attempt, access=access, retries=retries, approval=approval,
                            legacy_path=legacy_path, git_commit=git_commit, stamp_ref=stamp_ref,
                            original_fetched_at=_timestamp(original_fetched_at, "original_fetched_at"))
        try:
            s.validate_context(content_kind, source, fetch, {**resp, "status": 200} if source_error else resp,
                               listing)
        except s.SchemaError as e:
            raise InvalidContext(e.reason, e.field, e.problem) from None
        if expect_types is not None:
            expect_types = list(expect_types)  # a generator would be used up by the check
            if not expect_types or not set(expect_types) <= set(s.SNIFF_TYPES):
                raise ValueError("expect_types must be a non-empty list of SNIFF_TYPES")
        if source_error:
            return self._not_staged(FAILED, f"http_{status}")

        declared = s.declared_length(resp["headers"]) if resp is not None else None
        if declared is None:
            declared = declared_length
        elif declared_length is not None and declared_length != declared:
            raise ValueError("declared_length disagrees with the Content-Length")
        headers = resp["headers"] if resp is not None else {}
        if declared is not None and declared > s.MAX_OBJECT_SIZE:
            return self._not_staged(FAILED, OVER_S3_LIMIT)
        if declared is not None and "content-range" in headers and not s._full_range(headers, declared):
            return self._not_staged(FAILED, PARTIAL_RESPONSE)
        if declared is not None and declared > s.COST_GATE and approval is None:
            return self._not_staged(NEEDS_APPROVAL, s.NEEDS_APPROVAL_REASON, declared)

        form, body_etag = s.etag_form(headers.get("etag"))
        candidates = ()
        if form == "md5-multipart":  # only part sizes that could give its part count
            n = int(body_etag.split("-")[1])
            candidates = tuple(p for p in ETAG_PART_SIZES if declared is None or s.part_count(declared, p) == n)
        ctx = {"content_kind": content_kind, "origin": origin, "source": source, "fetch": fetch, "response": resp,
               "listing": listing, "expect_types": list(expect_types) if expect_types is not None else None,
               "declared": declared, "approval": approval, "eof": eof, "headers": headers, "started": started,
               "observed": observed, "completed": completed_at or now, "git_blob": git_blob,
               "unchanged_file": unchanged_file}
        if git_blob is not None and declared is None:
            raise ValueError("a git backfill needs its size first (its blob id hashes the size)")
        mono = self._clock()  # times within the fetch: now plus monotonic elapsed, whatever the wall clock does
        reader, u = _Reader(body, lambda: now + timedelta(seconds=self._clock() - mono)), s.new_uuid()
        digest = _Digest(candidates, git_size=declared if git_blob is not None else None)
        try:
            return self._stage(u, reader, digest, ctx)
        except _Stop as stop:
            return self._not_staged(stop.status, stop.reason, stop.size)

    def _not_staged(self, status, reason, size=None):
        """A file refused before anything was committed: its result, at once."""
        self.log("not_staged", status=status, reason=reason, size=size)
        return Staged(self, result=Result(status, size=size, reason=reason))

    def _fetch(self, origin, fetch_id, started, first_byte, completed, *, access, work_id=None, attempt=1,
               retries=0, approval=None, legacy_path=None, original_fetched_at=None,
               git_commit=None, stamp_ref=None):
        return {
            "origin": origin, "fetch_id": fetch_id, "run_id": self.run_id, "work_id": work_id, "attempt": attempt,
            "connector": self.connector, "connector_version": self.connector_version,
            "library_version": LIBRARY_VERSION, "access": access, "started_at": started,
            "first_byte_at": first_byte, "completed_at": completed, "retries": retries, "ci_run": self.ci_run,
            "approval": approval, "legacy_path": legacy_path, "original_fetched_at": original_fetched_at,
            "git_commit": git_commit, "stamp_ref": stamp_ref,
        }

    def _stage(self, u, reader, digest, ctx):
        declared, key = ctx["declared"], s.staging_data_key(u)
        meta = s.staging_metadata(ctx["source"], u, ctx["fetch"]["fetch_id"])
        buf = reader.read_full(s.PART_SIZE + 1)
        self._check_type(ctx, buf[:s.SNIFF_BYTES])
        encoding = ctx["headers"].get("content-encoding", "").strip().lower()
        if encoding in ("gzip", "x-gzip") and s.sniff_type(bytes(buf[:s.SNIFF_BYTES])) == "gzip":
            raise _Stop(FAILED, STILL_ENCODED)  # e.g. requests' resp.raw: pass the decoded content
        if len(buf) <= s.PART_SIZE:  # the whole file: one PUT
            digest.update(buf)
            self._check_read(ctx, digest)
            sidecar, raw = self._sidecar(u, ctx, reader, digest, {"method": "put", "part_size": None,
                                                                   "part_sha256": None}, f'"{digest.md5.hexdigest()}"')
            got = self._put_new(key, bytes(buf), sidecar["data"]["sha256"], Metadata=meta)
            if got is not None and got.get("ETag") != sidecar["data"]["staging_etag"]:
                raise IntakeError("S3 stored the data object with another ETag")
        else:
            if declared is not None and declared <= s.PART_SIZE:
                raise _Stop(FAILED, OVERLONG, len(buf))  # read so far, as the other overlong path reports
            part_size = s.upload_part_size(declared) if declared is not None else s.PART_SIZE
            limit = declared if declared is not None else (
                s.COST_GATE if ctx["approval"] is None else s.PART_SIZE * s.MAX_PARTS)
            upload_id = self.s3.create_multipart_upload(
                Bucket=self.staging, Key=key, ChecksumAlgorithm="SHA256", Metadata=meta,
                ServerSideEncryption=s.STAGING_SSE)["UploadId"]
            try:
                parts = self._send_parts(key, upload_id, reader, buf, part_size, digest, limit, declared, ctx)
                self._check_read(ctx, digest)
                shas, md5s = [p[1] for p in parts], [p[2] for p in parts]
                sidecar, raw = self._sidecar(u, ctx, reader, digest, {
                    "method": "multipart", "part_size": part_size, "part_sha256": shas},
                    f'"{s.md5_multipart_etag(md5s)}"')
                self._complete(key, upload_id, parts, sidecar)
            except BaseException:
                self._abort(u, key, upload_id)
                raise
        raw_sha = hashlib.sha256(raw).hexdigest()
        self._put_new(s.staging_sidecar_key(u), raw, raw_sha, ContentType="application/json")
        data = sidecar["data"]
        self.log("staged", uuid=u, size=data["size"], method=data["upload"]["method"])
        return Staged(self, u, sha256=data["sha256"], size=data["size"],
                      sidecar_sha256=raw_sha, staging_etag=data["staging_etag"],
                      timeout=self._timeout_for(data["size"]))

    def _check_type(self, ctx, head):
        """Refuse a file whose first bytes aren't what the connector expected
        (an HTML error page saved where a PDF should be), before it's staged."""
        expect = ctx["expect_types"]
        if expect is not None and s.expected_sniff(ctx["content_kind"], bytes(head)) not in expect:
            raise _Stop(FAILED, UNEXPECTED_TYPE)

    @staticmethod
    def _check_read(ctx, digest):
        """What was read is the whole file the response declared, the file
        on disk didn't change while read, and a git backfill's bytes are the
        blob its commit names."""
        size, declared, headers = digest.size, ctx["declared"], ctx["headers"]
        if declared is not None and size != declared:
            raise _Stop(FAILED, TRUNCATED if size < declared else OVERLONG, size)
        if "content-range" in headers and not s._full_range(headers, size):  # e.g. a Content-Encoded slice
            raise _Stop(FAILED, PARTIAL_RESPONSE, size)
        if ctx["unchanged_file"] is not None and not ctx["unchanged_file"]():
            raise _Stop(FAILED, CHANGED, size)
        if ctx["git_blob"] is not None and digest.git.hexdigest() != ctx["git_blob"]:
            raise _Stop(FAILED, GIT_MISMATCH, size)

    def _send_parts(self, key, upload_id, reader, buf, part_size, digest, limit, declared, ctx):
        """Upload the body in parts of part_size, WORKERS at a time with at
        most MAX_IN_FLIGHT bytes uploading (plus the part being read);
        returns (part number, SHA-256, MD5) per part, in order."""
        parts, futures = [], {}
        pending, n = buf, 0
        with ThreadPoolExecutor(self.workers) as pool:
            try:
                while True:
                    if len(pending) < part_size:
                        pending += reader.read_full(part_size - len(pending))
                    if not pending:
                        break
                    with memoryview(pending) as view:
                        part = bytes(view[:part_size])  # one copy, not two
                    del pending[:part_size]
                    n += 1
                    digest.update(part)
                    if digest.size > limit:
                        if declared is not None:
                            raise _Stop(FAILED, OVERLONG, digest.size)
                        if ctx["approval"] is None:
                            raise _Stop(NEEDS_APPROVAL, s.NEEDS_APPROVAL_REASON)
                        raise _Stop(FAILED, UNDECLARED_LENGTH)
                    while futures and sum(futures.values()) + len(part) > max(part_size, MAX_IN_FLIGHT):
                        self._settle(futures, parts)
                    futures[pool.submit(self._upload_part, key, upload_id, n, part)] = len(part)
                    if len(part) < part_size:  # a short part is the last
                        break
                while futures:
                    self._settle(futures, parts)
            except BaseException:
                for f in futures:
                    f.cancel()
                raise
        return sorted(parts)

    @staticmethod
    def _settle(futures, parts):
        """Wait for one or more part uploads; raises the first failure."""
        done, _ = wait(futures, return_when=FIRST_COMPLETED)
        for f in done:
            parts.append(f.result())
            del futures[f]

    def _upload_part(self, key, upload_id, n, part):
        sha, md5 = hashlib.sha256(part).hexdigest(), hashlib.md5(part, usedforsecurity=False).hexdigest()
        got = self.s3.upload_part(Bucket=self.staging, Key=key, UploadId=upload_id, PartNumber=n, Body=part,
                                  ContentLength=len(part), ChecksumSHA256=s.sha256_b64(sha))
        if got.get("ETag") != f'"{md5}"' or got.get("ChecksumSHA256") != s.sha256_b64(sha):
            raise IntakeError(f"S3 stored part {n} with another ETag or checksum")
        return n, sha, md5

    def _complete(self, key, upload_id, parts, sidecar):
        """Complete the upload write-once. A 409 means an earlier attempt of
        this call is still completing it: ask again. After any other failure
        (412, NoSuchUpload, a dropped connection, a shape not seen yet), the
        data object being there is the proof it completed: the key is this
        call's own fresh uuid."""
        data = sidecar["data"]
        listed = {"Parts": [{"PartNumber": n, "ETag": f'"{md5}"', "ChecksumSHA256": s.sha256_b64(sha)}
                            for n, sha, md5 in parts]}
        for attempt in range(CONFLICT_RETRIES + 1):
            try:
                got = self.s3.complete_multipart_upload(Bucket=self.staging, Key=key, UploadId=upload_id,
                                                        IfNoneMatch="*", MultipartUpload=listed)
                break
            except Exception as e:
                if _code(e) == "ConditionalRequestConflict" and attempt < CONFLICT_RETRIES:
                    self._sleep(min(2 ** attempt, 8))  # botocore doesn't retry a 409
                    continue
                if self._landed(key):  # whatever the error said: the key is this call's fresh uuid
                    self.log("complete_retried", uuid=sidecar["uuid"], code=_code(e))
                    return
                raise
        want = s.composite_sha256(data["upload"]["part_sha256"])
        if got.get("ETag") != data["staging_etag"] or got.get("ChecksumSHA256") != want:
            raise IntakeError("S3 completed the upload with another ETag or checksum")

    def _sidecar(self, u, ctx, reader, digest, upload, staging_etag):
        """The sidecar for what was read, validated (under write policy) and
        serialized, before the data becomes an object."""
        size, md5, headers = digest.size, digest.md5.hexdigest(), ctx["headers"]
        md5_multipart = None
        form, body = s.etag_form(headers.get("etag"))
        if form == "md5-multipart" and size:
            n = int(body.split("-")[1])
            for parts in digest.etag_parts:
                if s.part_count(size, parts.size) == n and parts.etag() == body:
                    md5_multipart = {"part_size": parts.size, "etag": body}
                    break
        declared, origin = ctx["declared"], ctx["origin"]
        # clean: the declared length arrived, or the file was local. A chunked body that just stops can't be
        # told from one that ended; a connector whose client enforces chunk framing may pass eof="clean"
        eof = ctx["eof"] or ("clean" if declared is not None or origin != "live" else "unknown")
        # The wall clock can step back (an NTP step on a fresh runner): never
        # let that make the fetch's own times run backwards.
        if ctx["observed"]:
            first = max(reader.first_at, ctx["started"]) if reader.first_at is not None else None
            end = max(reader.end_at, first or ctx["started"])  # staging read to the end first
        else:  # bytes or a saved file: the library didn't see the fetch
            first, end = None, ctx["completed"]
        fetch = dict(ctx["fetch"], first_byte_at=_timestamp(first, "first_byte_at"),
                     completed_at=s.format_timestamp(end),
                     approval=ctx["approval"] if size > s.COST_GATE else None)
        sidecar = {
            "schema": s.SCHEMA_VERSION, "uuid": u, "content_kind": ctx["content_kind"],
            "data": {"size": size, "sha256": digest.sha256.hexdigest(), "md5": md5, "md5_multipart": md5_multipart,
                     "staging_etag": staging_etag, "upload": upload},
            "source": ctx["source"], "fetch": fetch, "response": ctx["response"], "listing": ctx["listing"],
            "checks": {
                "declared_length": declared, "length": "ok" if declared is not None else "undeclared", "eof": eof,
                "etag": _etag_check(headers, md5, md5_multipart), "content_md5": s.content_md5_check(headers, md5),
                "sniffed_type": s.expected_sniff(ctx["content_kind"], digest.head),
                "expect_types": ctx["expect_types"],
            },
        }
        return sidecar, s.sidecar_bytes(sidecar)

    def _put_new(self, key, body, sha256=None, **kw):
        """A write-once PUT carrying the body's SHA-256. A 412 means an earlier
        attempt of this call landed (the key is a fresh uuid's), and so does
        the object being there after any other failure (a 409, its first
        attempt still settling, is asked again first): None. `sha256` is the
        body's, when already known."""
        for attempt in range(CONFLICT_RETRIES + 1):
            try:
                return self.s3.put_object(Bucket=self.staging, Key=key, Body=body, ContentLength=len(body),
                                          ChecksumSHA256=s.sha256_b64(sha256 or hashlib.sha256(body).hexdigest()),
                                          IfNoneMatch="*", ServerSideEncryption=s.STAGING_SSE, **kw)
            except Exception as e:
                if _is_precondition(e):
                    self.log("put_retried", key_kind=s.parse_staging_key(key)[1])
                    return None
                conflict = _code(e) == "ConditionalRequestConflict"
                if conflict and attempt < CONFLICT_RETRIES:
                    self._sleep(min(2 ** attempt, 8))  # botocore doesn't retry a 409
                    continue
                if self._landed(key):  # it landed; only the answer was lost
                    self.log("put_landed", key_kind=s.parse_staging_key(key)[1], error=type(e).__name__)
                    return None
                raise

    def _landed(self, key):
        """Whether a write that failed left its object after all. A failure to
        tell counts as no: the original error is the one to raise."""
        try:
            return self._exists(key)
        except Exception:
            return False

    def _exists(self, key):
        try:
            self.s3.get_object_tagging(Bucket=self.staging, Key=key)
            return True
        except Exception as e:
            if _is_absent(e):
                return False
            raise

    def _abort(self, u, key, upload_id):
        """Best effort (the staging lifecycle aborts what's left after 7
        days), but logged: a failure that persists is a grant or an outage."""
        try:
            self.s3.abort_multipart_upload(Bucket=self.staging, Key=key, UploadId=upload_id)
        except Exception as e:
            self.log("abort_failed", uuid=u, error=type(e).__name__, code=_code(e), status=_http_status(e))

    def _timeout_for(self, size):
        if self.timeout is not None:
            return self.timeout
        return min(MAX_TIMEOUT, BASE_TIMEOUT + size / TIMEOUT_RATE)

    # --- Outcomes -----------------------------------------------------------------------------

    def wait(self, handles):
        """Results for Staged handles, in order: each waits until the Lambda
        records, rejects or defers its file, or its timeout passes (pending;
        waiting again looks again).
        The timeout runs from this call, not the commit, so a file staged
        early in a long run gets its full time once someone waits for it.
        Files are checked POLL_WORKERS at a time, so waiting for many costs
        about as long as the slowest. Raises IntakeError, or an S3 error that
        isn't a throttle, a 5xx or a dropped connection, after recording every
        other file's outcome from that round."""
        # pending isn't final, for a file this process staged (unchanged() has no hashes to check a record by)
        todo = [h for h in handles if h._result is None or h._result.status == PENDING and h._timeout is not None]
        delay, start = POLL_FIRST, self._clock()
        if todo:
            with ThreadPoolExecutor(min(POLL_WORKERS, len(todo))) as pool:
                while todo:
                    futures, outcomes, error = [pool.submit(self._outcome, h) for h in todo], [], None
                    for f in futures:  # keep every outcome this round found, then raise the first error
                        try:
                            outcomes.append(f.result())
                        except Exception as e:
                            outcomes.append(_UNRESOLVED)
                            error = error or e
                    now, left = self._clock(), []
                    for h, result in zip(todo, outcomes):
                        if result is _UNRESOLVED:
                            continue
                        if result is None and now >= start + h._timeout:
                            result = Result(PENDING, h.uuid, h.sha256, h.size, reason=PENDING)
                        if result is None:
                            left.append(h)
                        else:
                            h._result = result
                            self.log("outcome", uuid=h.uuid, status=result.status, reason=result.reason)
                    if error is not None:
                        raise error
                    todo = left
                    if todo:
                        self._sleep(delay)
                        delay = min(delay * 2, POLL_MAX)
        return [h._result for h in handles]

    def _outcome(self, h):
        """The file's outcome if there is one yet (None to look again). A
        record always wins: one written after a rejected or deferred tag (the
        one-off job, a retry) supersedes it."""
        try:
            record = self._record_for(h)
            if record is not None:
                return Result(HELD, h.uuid, h.sha256, h.size, record=record)
            tags = self._tags(h.uuid)  # read second: the Lambda records, then tags
            state, ingested = tags.get("intake"), tags.get(s.INGESTED_TAG[0]) == s.INGESTED_TAG[1]
            if not ingested and state not in (REJECTED, DEFERRED):
                return None
            record = self._record_for(h)
        except Exception as e:
            if isinstance(e, IntakeError) or not _retryable(e):
                raise
            self.log("poll_failed", uuid=h.uuid, error=type(e).__name__, code=_code(e), status=_http_status(e))
            return None
        if record is not None:
            return Result(HELD, h.uuid, h.sha256, h.size, record=record)
        if ingested:  # the Lambda tags only after the record exists
            raise IntakeError("the file is tagged ingested but its record reads as absent (a missing grant?)")
        if state == DEFERRED:
            return Result(DEFERRED, h.uuid, h.sha256, h.size, reason=DEFERRED)
        reason = tags.get("reason")
        return Result(REJECTED, h.uuid, h.sha256, h.size, reason=reason if reason in s.REJECT_REASONS else REJECTED)

    def _record_for(self, h):
        record = self.read_record(h.uuid)
        if record is None:
            return None
        st = record["staging"]
        if (record["sha256"], st["sidecar_sha256"], st["data_etag"]) != (h.sha256, h._sidecar_sha256, h._staging_etag):
            raise IntakeError("the intake record describes another upload")
        return record

    def _read_record_patiently(self, u, attempts=3):
        """read_record, trying again after a throttle, a 5xx or a dropped
        connection (as polling does), then raising what it got."""
        for attempt in range(attempts):
            try:
                return self.read_record(u)
            except Exception as e:
                if isinstance(e, IntakeError) or not _retryable(e) or attempt == attempts - 1:
                    raise
                self.log("poll_failed", uuid=u, error=type(e).__name__, code=_code(e), status=_http_status(e))
                self._sleep(2 ** attempt)

    def read_record(self, u):
        """The intake record for uuid `u` (parsed as stored), or None if there
        isn't one yet (or, as the writer can't list, it may not read it)."""
        try:
            got = self.s3.get_object(Bucket=self.evidence, Key=s.record_key(u))
        except Exception as e:
            if _is_absent(e):
                return None
            raise
        raw = _read_all(got["Body"], s.MAX_RECORD_BYTES + 1)
        try:
            return s.parse_record(raw, stored=True, key=s.record_key(u))
        except s.SchemaError as e:
            raise IntakeError(f"a stored record doesn't parse: {e.reason}") from None

    def _unrecorded(self, u):
        """The outcome so far of upload `u`, whose record reads as absent:
        from its staging tags. The writer can't list, so "absent" may be a
        denied read: with no staging objects either (ingested ones expire
        after a day), or tagged ingested (its record exists), that's an
        IntakeError, never a manifest entry claiming no record."""
        try:
            tags = self._tags(u)
        except Exception as e:
            if _is_absent(e):
                raise IntakeError("no record or staging objects for that upload: a record the writer can't "
                                  "read, or a uuid from other state") from None
            raise
        if tags.get(s.INGESTED_TAG[0]) == s.INGESTED_TAG[1]:
            raise IntakeError("the upload is tagged ingested but its record reads as absent (a missing grant?)")
        state = tags.get("intake")
        if state == DEFERRED:
            return Result(DEFERRED, u, reason=DEFERRED)
        if state == REJECTED:
            reason = tags.get("reason")
            return Result(REJECTED, u, reason=reason if reason in s.REJECT_REASONS else REJECTED)
        return Result(PENDING, u, reason=PENDING)

    def _tags(self, u):
        """The sidecar's tags (the Lambda tags it last). The library wrote the
        sidecar, so any error reading them, AccessDenied included (a missing
        s3:GetObjectTagging), is an error."""
        got = self.s3.get_object_tagging(Bucket=self.staging, Key=s.staging_sidecar_key(u))
        return {t["Key"]: t["Value"] for t in got.get("TagSet", [])}

    # --- Requests -------------------------------------------------------------------------------

    def request(self, *, kind, platform, host, request_id, agency=None, request_url=None, files_listed=None,
                access=None, work_id=None, attempt=1):
        """A Request for one PRA request's files in this run; see Request."""
        return Request(self, {"kind": kind, "platform": platform, "host": host, "agency": agency,
                              "request_id": request_id, "request_url": _stable(request_url)},
                       files_listed=files_listed, access=self.access if access is None else access, work_id=work_id,
                       attempt=attempt)


class Request:
    """One run's files for one PRA request:

        with intake.request(kind="muckrock", platform="muckrock", host=...,
                            request_id="12345", files_listed=10) as req:
            req.upload("a.pdf", body, url=..., response=Response(...))
            req.unchanged("b.pdf", record=uuid_from_an_earlier_run)
            req.failed("c.pdf", "source_404")

    upload() stages at once and returns a Staged (unchanged() and failed()
    return a Result); the outcomes are waited for together when the block
    ends. Then the fetch manifest (every file, held or failed and why) is
    uploaded like any file, and its Result is `manifest`. A block that
    raises writes no manifest (the staged files are still ingested), and
    neither does an exit that raises (then results may hold None). results
    holds (filename, Result) per file, in order. Not thread-safe: add every
    file from the thread that runs the block, before it ends."""

    def __init__(self, intake, source, *, files_listed, access, work_id, attempt):
        s.build_manifest(source, [], files_listed)  # the request's own fields, checked now
        stamp = s.format_timestamp(intake._now())
        s.validate_context("file", _PROBE_SOURCE, intake._fetch("local-copy", s.new_uuid(), stamp, None, stamp,
                                                                work_id=work_id, attempt=attempt, access=access,
                                                                legacy_path="probe"), None, None)
        self._intake, self.source, self.files_listed = intake, source, files_listed
        self._defaults = {"access": access, "work_id": work_id, "attempt": attempt}  # each file's, unless given
        self._items, self._bytes = [], 0  # (filename, doc_id, url, Staged); the manifest's size so far, at most
        self.manifest, self._closed = None, False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._closed = True
        if exc_type is not None:
            return False
        results = self._intake.wait([item[3] for item in self._items])
        latest = {}  # a file tried again in this run: its last outcome stands
        for (f, d, url, _), r in zip(self._items, results):
            latest[(f, d, url)] = _manifest_entry(f, d, url, r)
        entries = list(latest.values())
        if self.files_listed is not None and len(entries) != self.files_listed:
            self._intake.log("listing_mismatch", listed=self.files_listed, entries=len(entries))
        raw = s.manifest_bytes(s.build_manifest(self.source, entries, self.files_listed))
        source = {**self.source, "doc_id": None, "filename": s.MANIFEST_FILENAME, "title": None, "url": None,
                  "released_on": None}
        self.manifest = self._intake._stage_body(
            source, raw, observed=False, content_kind="fetch_manifest", origin="generated",
            declared_length=len(raw), **self._defaults).result()
        return False

    @property
    def results(self):
        return [(f, h._result) for f, _, _, h in self._items]

    def _file_source(self, filename, doc_id, title, url, released_on):
        doc_id = _doc_id(doc_id)
        url = _stable(url)
        self._check_entry(filename, doc_id, url)
        return {**self.source, "doc_id": doc_id, "filename": filename, "title": title, "url": url,
                "released_on": released_on}

    def _check_entry(self, filename, doc_id, url, reason="x"):
        """The file can be listed in the manifest (errors name file.<field>),
        and the manifest still fits its caps (checked now, not after every
        file has been staged and waited for)."""
        if self._closed:
            raise RuntimeError("the request's block has ended: its manifest is written")
        entry = s.manifest_entry(filename, status="failed", reason=reason, doc_id=doc_id, url=url)
        try:
            s.build_manifest(self.source, [entry])
        except s.SchemaError as e:
            raise s.SchemaError(e.reason, e.field.replace("manifest.files[0]", "file"), e.problem) from None
        size = len(s.canonical_json({**entry, "sha256": "0" * 64, "size": s.MAX_OBSERVED_SIZE})) + 1
        if len(self._items) >= s.MAX_MANIFEST_FILES or self._bytes + size > s.MAX_MANIFEST_BYTES - 64 * 1024:
            raise ValueError("the request's manifest is full (MAX_MANIFEST_FILES or MAX_MANIFEST_BYTES)")
        self._bytes += size

    def _add(self, filename, doc_id, url, staged):
        self._items.append((filename, doc_id, url, staged))
        return staged

    def upload(self, filename, body, *, doc_id=None, title=None, url=None, released_on=None, **kw):
        """Stage one file of this request (Intake.stage's keywords). Returns
        its Staged. A body that raises fails only this file (read_error)."""
        _live_only(kw)
        source = self._file_source(filename, doc_id, title, url, released_on)
        kw = {**self._defaults, **kw}
        return self._add(filename, source["doc_id"], source["url"], self._read_errors(self._intake.stage, source,
                                                                                       body, kw))

    def upload_file(self, path, filename, *, doc_id=None, title=None, url=None, released_on=None, **kw):
        """Stage one file of this request the client saved to disk, with its
        response (Intake.stage_file)."""
        _live_only(kw)
        source = self._file_source(filename, doc_id, title, url, released_on)
        kw = {**self._defaults, **kw}
        return self._add(filename, source["doc_id"], source["url"], self._read_errors(self._intake.stage_file, path,
                                                                                       source, kw))

    def _read_errors(self, stage, first, *args):
        *rest, kw = args
        try:
            return stage(first, *rest, **kw)
        except BodyError as e:  # the connection dropped mid-body: one failed file, not a lost manifest
            self._intake.log("read_error", error=type(e.__cause__).__name__)
            return Staged(self._intake, result=Result(FAILED, reason=READ_ERROR))
        except InvalidContext as e:  # e.g. an agency's date that isn't one: this file fails, the rest go on
            self._intake.log("invalid_file", reason=e.reason, field=e.field)
            return Staged(self._intake, result=Result(FAILED, reason=e.reason))

    def unchanged(self, filename, *, record, doc_id=None, url=None):
        """A file the connector recognized as unchanged since an earlier
        upload, whose uuid is `record`. Listed held only if that intake
        record exists and is this file's: the same doc id when both name
        one, else the same url when both name one, else the same filename;
        else failed (record_mismatch: fetch it again). With no record yet,
        its outcome so far: deferred or rejected (terminal), or pending.
        IntakeError if neither the record nor its staging objects can be
        read (a denied read, or a uuid from other state).
        ValueError if the record is another request's or a manifest's (a
        uuid from the wrong state). Returns its Result."""
        doc_id = _doc_id(doc_id)
        url = _stable(url)
        self._check_entry(filename, doc_id, url)
        found = self._intake._read_record_patiently(record)
        if found is None:
            result = self._intake._unrecorded(record)
        else:
            src = found["sidecar"]["source"]
            if any(src[k] != self.source[k] for k in ("kind", "platform", "host", "request_id")):
                raise ValueError("that record belongs to another request")
            if found["sidecar"]["content_kind"] != "file":
                raise ValueError("that record is a fetch manifest's")
            if doc_id is not None and src["doc_id"] is not None:  # the file is its doc id...
                same = src["doc_id"] == doc_id
            elif url is not None and src["url"] is not None:  # ...else its url (a renamed file is the same)...
                same = src["url"] == url
            else:  # ...else its name
                same = src["filename"] == filename
            result = (Result(HELD, record, found["sha256"], found["size"], record=found) if same
                      else Result(FAILED, record, reason=RECORD_MISMATCH))
            if same:  # the manifest names the file as fully as its record does
                doc_id, url = doc_id or src["doc_id"], url or src["url"]
        self._add(filename, doc_id, url, Staged(self._intake, record, result=result))
        return result

    def failed(self, filename, reason, *, doc_id=None, url=None, final=False):
        """A file the connector couldn't fetch; `reason` is a code like
        source_404 (lower-case letters, digits, _). final=True: the
        connector gives up on it (after its attempt cap), so the result is
        terminal. Returns its Result."""
        doc_id = _doc_id(doc_id)
        url = _stable(url)
        self._check_entry(filename, doc_id, url, reason=reason)
        result = Result(FAILED, reason=reason, final=final)
        self._add(filename, doc_id, url, Staged(self._intake, result=result))
        return result


def _live_only(kw):
    """A request's manifest says what the source listed this run; a backfill
    never asked the source, so it goes through Intake.upload_file instead."""
    if kw.get("origin", "live") != "live" or kw.get("response") is None:
        raise ValueError("a request's files are live fetches with their response; backfills go through Intake")


def _manifest_entry(filename, doc_id, url, result):
    if result.status == HELD:
        return s.manifest_entry(filename, status="held", sha256=result.sha256, size=result.size, doc_id=doc_id,
                                url=url)
    if result.status == NEEDS_APPROVAL:
        return s.manifest_entry(filename, status="needs_approval", reason=s.NEEDS_APPROVAL_REASON,
                                size=result.size, doc_id=doc_id, url=url)
    return s.manifest_entry(filename, status="failed", reason=result.reason, doc_id=doc_id, url=url)


def _ci_run(environ):
    """The GitHub Actions run attempt this process belongs to, if any."""
    if environ.get("GITHUB_ACTIONS") != "true":
        return None
    server, repo, run = (environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    if not (server and repo and run):
        return None
    return f"{server}/{repo}/actions/runs/{run}/attempts/{environ.get('GITHUB_RUN_ATTEMPT') or '1'}"
