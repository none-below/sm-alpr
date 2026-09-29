"""The ingest Lambda: copies each staged file, write-once, into evidence.

S3 sends an event for every in/<uuid>.json (the sidecar, the commit marker)
to SQS, and SQS invokes handler(). For one staged file, Ingest.process:

  1. If _intake/<uuid>.json exists, checks it describes these staging
     objects and re-tags them: a retry, a duplicate event or a sweep
     re-drive. A record for other bytes under the uuid is uuid_reused.
  2. Validates the sidecar under write policy, read from the key the event
     named, never from a key a document names.
  3. Binds it to the data object with HeadObject alone: ETag, size,
     encryption, S3's own checksum of the upload, and the x-amz-meta.
  4. Checks what the file depends on: an approval over COST_GATE; for a
     fetch manifest, every sha256 and size it lists; for a backfill, its
     stamped manifest.
  5. Copies the bytes to evidence at sha256/<hex>, write-once:
     - up to SINGLE_PUT_MAX, one GET streamed into one PutObject carrying
       the claimed SHA-256, so S3 verifies every byte. The other claims
       about the bytes (MD5, the source's multipart ETag, the staging part
       layout, the sniffed type) are checked in the same pass, before the
       last bytes are sent, so a wrong claim fails the upload;
     - above it, the Lambda hashes the object and checks every claim, then
       copies it server side on the staging object's own part boundaries.
     A blob already at the key is never trusted: it's read back like a new
     one, and the staged bytes are read and every claim checked.
  6. Reads the blob back (checksum, size, Object Lock), writes the record
     write-once, and tags both staging objects so lifecycle removes them.

A problem with the upload itself is terminal: both staging objects are
tagged intake=rejected with a reason code and stay until someone looks.
Anything else (throttling, a 5xx, a conflict, a lock S3 doesn't show, a
bug) raises Transient and SQS retries it, then parks it in the DLQ. Logs
carry uuids, reason codes, field paths and sizes, never a filename, URL or
metadata value.
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import traceback
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus

from . import schema as s

# Above this a 15-minute Lambda may not finish (hashing runs at ~60 MB/s at
# 2048 MB; re-measure if the memory setting changes). The file is tagged
# intake=deferred and `python -m pra_intake.ingest process --allow-large`
# runs the same steps somewhere without the limit.
LAMBDA_MAX_SIZE = 20_000_000_000
READ_CHUNK = 8 * s.MiB
WORKERS = 16  # concurrent HeadObject / UploadPartCopy calls
DEFERRED_TAGS = {"intake": "deferred"}
MIN_REMAINING_MS = 60_000  # the handler takes no new message with less time left
REDRIVES_TAG = "redrives"
EX_TEMPFAIL = 75  # the job's exit code for a retryable failure (sysexits.h)
_FROM_ENV = object()
MAX_REDRIVES = 3  # sweep re-drives of one file before it's counted as stuck
SWEEP_RESERVE_MS = 30_000
RETENTION_TTL = timedelta(minutes=15)  # how long a warm container trusts the bucket's rule

# What the ingest role needs: {bucket role: {key prefix: actions}}, None for
# the bucket itself. PR 4 builds the policy from this, and the tests run the
# Lambda with exactly these grants. Without ListBucket a missing key reads as
# 403, not 404, and HeadBucket fails; ListBucket carries no s3:prefix
# condition (HeadBucket sends none). Without GetObjectRetention HeadObject
# hides the Object Lock headers. PutObjectRetention without
# s3:BypassGovernanceRetention can only lengthen a lock.
PERMISSIONS = {
    "staging": {None: ("s3:ListBucket",), s.STAGING_PREFIX: ("s3:GetObject", "s3:PutObjectTagging")},
    "evidence": {
        None: ("s3:ListBucket", "s3:GetBucketObjectLockConfiguration"),
        s.BLOB_PREFIX: ("s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectRetention", "s3:PutObject",
                        "s3:PutObjectRetention", "s3:AbortMultipartUpload"),
        s.RECORD_PREFIX: ("s3:GetObject", "s3:PutObject"),
    },
    "ops": {None: ("s3:ListBucket",), s.APPROVAL_PREFIX: ("s3:GetObject",)},
}
SWEEP_PERMISSIONS = {"staging": {None: ("s3:ListBucket",), s.STAGING_PREFIX: ("s3:GetObjectTagging",
                                                                             "s3:PutObjectTagging")}}


class Rejected(Exception):
    """The upload itself is wrong: terminal, tagged on the staging objects."""

    def __init__(self, reason, field, problem):
        super().__init__(f"{reason}: {field}: {problem}")
        self.reason, self.field, self.problem = reason, field, problem


class Transient(Exception):
    """Retry later. Nothing new was tagged, except a rejected or deferred tag
    written just before the failure; the retry settles the tags either way."""


class _ClaimMismatch(Exception):
    """Raised from inside an upload's body to fail the upload."""


@dataclass
class Outcome:
    status: str  # stored | already_stored | recorded | rejected | deferred | vanished
    uuid: str = None
    sha256: str = None
    reason: str = None


def _code(exc):
    return ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code")


def _http_status(exc):
    return ((getattr(exc, "response", None) or {}).get("ResponseMetadata") or {}).get("HTTPStatusCode")


def _is_missing(exc):
    """No such key. (A missing bucket is NoSuchBucket, or a bare 404 on
    HeadObject; from_env checks the buckets exist before any message.)"""
    return _code(exc) in ("404", "NoSuchKey", "NotFound")


def _is_precondition(exc):
    return _code(exc) == "PreconditionFailed" or _http_status(exc) == 412


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class _Parts:
    """Digests of consecutive parts of one size (the last may be short)."""

    def __init__(self, size, new):
        self._size, self._new, self._done = size, new, []
        self._hash, self._in = new(), 0

    def update(self, chunk):
        view = memoryview(chunk)
        while view:
            take = min(len(view), self._size - self._in)
            self._hash.update(view[:take])
            self._in, view = self._in + take, view[take:]
            if self._in == self._size:
                self._done.append(self._hash.hexdigest())
                self._hash, self._in = self._new(), 0

    def digests(self):
        return self._done + ([self._hash.hexdigest()] if self._in or not self._done else [])


class _Hashes:
    """What the Lambda learns from reading the bytes once: MD5, optionally
    SHA-256, the source's multipart ETag at its part size, the SHA-256 of
    each staging part at the upload's part size, and the first SNIFF_BYTES."""

    def __init__(self, data, *, sha256=False):
        mp, up = data["md5_multipart"], data["upload"]
        self.size, self.head = 0, b""
        self.md5 = hashlib.md5(usedforsecurity=False)
        self.sha256 = hashlib.sha256() if sha256 else None
        self._mp = _Parts(mp["part_size"], lambda: hashlib.md5(usedforsecurity=False)) if mp else None
        self._up = _Parts(up["part_size"], hashlib.sha256) if up["method"] == "multipart" else None

    def update(self, chunk):
        if len(self.head) < s.SNIFF_BYTES:
            self.head += chunk[:s.SNIFF_BYTES - len(self.head)]
        self.size += len(chunk)
        self.md5.update(chunk)
        for h in (self.sha256, self._mp, self._up):
            if h is not None:
                h.update(chunk)

    def contradiction(self, sidecar):
        """The first claim in the sidecar these bytes contradict, as
        (reason, field), or None. SHA-256 only when it was hashed."""
        data = sidecar["data"]
        if self.size != data["size"]:
            return "data_mismatch", "sidecar.data.size"
        if self.sha256 is not None and self.sha256.hexdigest() != data["sha256"]:
            return "sha_mismatch", "sidecar.data.sha256"
        if self.md5.hexdigest() != data["md5"]:
            return "data_mismatch", "sidecar.data.md5"
        if self._mp is not None and s.md5_multipart_etag(self._mp.digests()) != data["md5_multipart"]["etag"]:
            return "data_mismatch", "sidecar.data.md5_multipart"
        if self._up is not None and self._up.digests() != data["upload"]["part_sha256"]:
            return "data_mismatch", "sidecar.data.upload.part_size"  # S3's composite already vouches for the digests
        if s.expected_sniff(sidecar["content_kind"], self.head) != sidecar["checks"]["sniffed_type"]:
            return "data_mismatch", "sidecar.checks.sniffed_type"
        return None


class _CheckedBody:
    """The evidence PutObject's body: the staging GET, hashed as it passes.
    It checks the sidecar's claims before handing over the last bytes, so a
    wrong claim raises inside the upload, which then never completes.
    `open_stream()` opens the GET, pinned to the bound ETag."""

    def __init__(self, open_stream, sidecar):
        self._open, self._sidecar, self.problem, self.reopen_error = open_stream, sidecar, None, None
        self._stream = None
        self._restart()

    def _restart(self):
        """A new staging GET, then the state swapped in all at once (a GET
        that fails leaves the body as it was), then the old stream closed."""
        stream = self._open()
        old, self._stream, self._touched = self._stream, stream, False
        self.hashes, self._left = _Hashes(self._sidecar["data"]), self._sidecar["data"]["size"]
        close = getattr(old, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass

    def readable(self):
        return True

    def tell(self):
        return self._sidecar["data"]["size"] - self._left

    def seek(self, offset, whence=io.SEEK_SET):
        """Back to the start: botocore's only seek, `reset_stream`'s seek(0)
        before each of its own retries (urllib3 only asks tell()). The
        staging GET is opened again (If-Match: the same bytes) and the hashes
        start over, also when the stream failed before its first byte. A
        seek to where it is, untouched, does nothing. After a claim failed
        it raises, and _store_whole rejects the upload (body.problem); if
        the new GET fails, its error is kept for the log (reopen_error),
        since botocore reports only UnseekableStreamError."""
        target = {io.SEEK_SET: offset, io.SEEK_CUR: self.tell() + offset,
                  io.SEEK_END: self._sidecar["data"]["size"] + offset}[whence]
        if target == self.tell() and not (target == 0 and self._touched):
            return target
        if target == 0 and self.problem is None:
            try:
                self._restart()
            except Exception as e:
                self.reopen_error = e
                raise
            return 0
        raise io.UnsupportedOperation("the body can only be rewound to its start")

    def read(self, n=-1):
        """Up to n bytes (READ_CHUNK for n < 0 or None; nothing for 0)."""
        if n == 0:
            return b""
        if self._left == 0:
            self._check()
            return b""
        self._touched = True
        chunk = self._stream.read(min(n if n and n > 0 else READ_CHUNK, self._left))
        if not chunk:  # HeadObject gave the size and If-Match pins the object: the connection dropped
            raise Transient("the staging stream ended early")
        self.hashes.update(chunk)
        self._left -= len(chunk)
        if self._left == 0:
            self._check()  # before these last bytes go out
        return chunk

    def _check(self):
        self.problem = self.problem or self.hashes.contradiction(self._sidecar)
        if self.problem:
            raise _ClaimMismatch(self.problem[0])


class Ingest:
    def __init__(self, s3, *, staging_bucket, evidence_bucket, ops_bucket, code_sha256=None,
                 now=lambda: datetime.now(timezone.utc), max_lambda_size=None, workers=WORKERS,
                 log=print):
        """Refuses a configuration that would fail every record after its
        blob is stored: bucket names that aren't one environment's staging,
        evidence and ops buckets, or a code hash that isn't lower-case hex
        (Lambda's own CodeSha256 is base64: pass the zip's hex SHA-256)."""
        envs = set()
        for name, role in ((staging_bucket, "staging"), (evidence_bucket, "evidence"), (ops_bucket, "ops")):
            try:
                env, got, account, region = s.parse_bucket_name(name)
            except s.SchemaError:
                raise ValueError(f"the {role} bucket isn't an intake bucket name") from None
            if got != role:
                raise ValueError(f"the {role} bucket is a {got} bucket")
            envs.add((env, account, region))
        if len(envs) != 1:
            raise ValueError("the buckets aren't one environment's")
        if code_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", str(code_sha256)):
            raise ValueError("code_sha256 must be the Lambda zip's SHA-256 in lower-case hex")
        self.s3, self.staging, self.evidence, self.ops = s3, staging_bucket, evidence_bucket, ops_bucket
        self.code_sha256, self.now, self.workers = code_sha256, now, workers
        self._default_retention = None
        self.max_lambda_size = LAMBDA_MAX_SIZE if max_lambda_size is None else max_lambda_size
        self._log = log

    @classmethod
    def from_env(cls, *, code_sha256=_FROM_ENV):
        """The Lambda's Ingest. The one-off job passes code_sha256 itself (None
        unless it runs the published zip), never the shell's CODE_SHA256."""
        if code_sha256 is _FROM_ENV:
            code_sha256 = os.environ.get("CODE_SHA256") or None
        ingest = cls(_client("s3"), staging_bucket=os.environ["STAGING_BUCKET"],
                     evidence_bucket=os.environ["EVIDENCE_BUCKET"], ops_bucket=os.environ["OPS_BUCKET"],
                     code_sha256=code_sha256)
        ingest.check_buckets()
        return ingest

    def check_buckets(self):
        """Each bucket exists and the role can list it, so a missing key
        reads as a missing key (404) and never as a missing bucket; and the
        evidence bucket has a default retention rule."""
        for bucket in (self.staging, self.evidence, self.ops):
            self.s3.head_bucket(Bucket=bucket)
        self._retention()

    def _retention(self, *, fresh=False):
        """The evidence bucket's default retention, (mode, period): read at
        cold start, again after RETENTION_TTL, and again before any renewal
        (fresh). Fixed retention only: a rule with a default event hold
        (variable retention) fails closed, since renewing a held blob with
        fixed retention may release its hold."""
        stale = self._default_retention is None or self.now() - self._default_retention[2] > RETENTION_TTL
        if stale or fresh:
            config = self.s3.get_object_lock_configuration(Bucket=self.evidence).get("ObjectLockConfiguration") or {}
            rule = (config.get("Rule") or {}).get("DefaultRetention") or {}
            days = rule.get("Days") or 365 * (rule.get("Years") or 0)
            if rule.get("Mode") not in s.LOCK_MODES or not days:
                raise RuntimeError("the evidence bucket has no default retention rule")
            if rule.get("DefaultEventHold"):
                raise RuntimeError("the evidence bucket's rule sets an event hold; fixed retention only")
            self._default_retention = (rule["Mode"], timedelta(days=days), self.now())
        return self._default_retention[:2]

    def log(self, event, **fields):
        self._log(json.dumps({"event": event, **fields}, sort_keys=True))

    def process(self, sidecar_key, *, principal=None, allow_large=False):
        """Ingest the staged file whose sidecar is `sidecar_key` (decoded).
        Returns an Outcome, or raises Transient to be retried."""
        try:
            u, kind = s.parse_staging_key(sidecar_key)
        except s.SchemaError:
            self.log("ignored")
            return Outcome("vanished")
        if kind != "sidecar":
            return Outcome("vanished", u)
        try:
            try:
                return self._process(u, principal, allow_large)
            except Rejected as e:
                return self._reject(u, e)
        except Transient as e:
            self.log("transient", uuid=u, error=str(e))
            raise
        except Exception as e:  # S3, the network, or a bug: retried, then the DLQ
            extra = {"reason": e.reason, "field": e.field} if isinstance(e, s.SchemaError) else {}
            self.log("transient", uuid=u, error=type(e).__name__, code=_code(e), status=_http_status(e),
                     where=_where(e), **extra)
            raise Transient(type(e).__name__) from e

    def _reject(self, u, e):
        self._tag(u, s.rejected_tags(e.reason))
        self.log("rejected", uuid=u, reason=e.reason, field=e.field, problem=e.problem)
        if e.reason != "uuid_reused":
            try:
                done = self._if_recorded(u)
            except Rejected as again:  # only uuid_reused, so this ends
                return self._reject(u, again)
            if done:
                return done
        return Outcome("rejected", u, reason=e.reason)

    def _if_recorded(self, u):
        """After tagging a file rejected or deferred: if a racing run has
        recorded it meanwhile, finish as that run did. Tag first, then look,
        so whichever run tags last tags what the evidence holds."""
        raw = self._read(self.evidence, s.record_key(u), s.MAX_RECORD_BYTES)
        if raw is None:
            return None
        return self._recorded(u, raw, self._read(self.staging, s.staging_sidecar_key(u), s.MAX_SIDECAR_BYTES))

    # --- the steps ------------------------------------------------------------------

    def _process(self, u, principal, allow_large):
        sidecar_raw, sidecar_meta = self._read_doc(self.staging, s.staging_sidecar_key(u), s.MAX_SIDECAR_BYTES)
        record_raw = self._read(self.evidence, s.record_key(u), s.MAX_RECORD_BYTES)
        if record_raw is not None:
            return self._recorded(u, record_raw, sidecar_raw)
        if sidecar_raw is None:
            self.log("vanished", uuid=u)
            return Outcome("vanished", u)
        if "-" in (sidecar_meta.get("ETag") or ""):  # multipart: its LastModified is when it began, not the commit
            raise Rejected("bad_sidecar", "staging.sidecar", "not written by a single PUT")
        sidecar = _checked(s.parse_sidecar, sidecar_raw, key=s.staging_sidecar_key(u))
        data = sidecar["data"]
        head = self._bind(u, sidecar)
        committed = sidecar_meta.get("LastModified")
        self._check_gate(sidecar, _aware(committed) if committed else self.now())
        self._check_references(u, sidecar, head)
        if data["size"] > self.max_lambda_size and not allow_large:
            self._tag(u, DEFERRED_TAGS)
            self.log("deferred", uuid=u, size=data["size"])
            return self._if_recorded(u) or Outcome("deferred", u, data["sha256"])
        if data["size"] <= s.SINGLE_PUT_MAX:
            evidence, status = self._store_whole(u, head, sidecar)
        else:
            evidence, status = self._store_parts(u, head, sidecar)
        staging = {
            "bucket": self.staging, "data_key": s.staging_data_key(u), "sidecar_key": s.staging_sidecar_key(u),
            "data_etag": head["ETag"], "data_last_modified": s.format_timestamp(_aware(head["LastModified"])),
            "data_sse": head["ServerSideEncryption"], "data_checksum_type": head["ChecksumType"],
            "data_checksum_sha256": head["ChecksumSHA256"],
            "sidecar_sha256": hashlib.sha256(sidecar_raw).hexdigest(),
        }
        ingest = {"deriver": s.DERIVER_VERSION, "code_sha256": self.code_sha256, "principal": _principal(principal)}
        record = s.build_record(sidecar, staging=staging, evidence=evidence, ingest=ingest)
        if not self._put_record(u, record):
            raw = self._read(self.evidence, s.record_key(u), s.MAX_RECORD_BYTES)
            return self._recorded(u, raw, sidecar_raw, expected=record)
        self._tag(u, s.ingested_tags(data["sha256"]))
        self.log(status, uuid=u, size=data["size"])
        return Outcome(status, u, data["sha256"])

    def _recorded(self, u, record_raw, sidecar_raw, expected=None):
        """A record exists: done if it describes the staging objects there
        now. `expected` is the record this run lost a write race with; the
        stored one stands."""
        if record_raw is None:
            raise Transient("the record vanished")
        try:
            record = s.parse_record(record_raw, stored=True, key=s.record_key(u))
        except s.SchemaError as e:
            raise Transient(f"a stored record doesn't parse: {e.reason}") from None
        st = record["staging"]
        if sidecar_raw is not None and hashlib.sha256(sidecar_raw).hexdigest() != st["sidecar_sha256"]:
            raise Rejected("uuid_reused", "staging.sidecar", "the uuid's record describes another sidecar")
        head = self._head(self.staging, s.staging_data_key(u))
        if head is not None and head.get("ETag") != st["data_etag"]:
            raise Rejected("uuid_reused", "staging.data", "the uuid's record describes another data object")
        if expected is not None and s.record_core(record) != s.record_core(expected):
            self.log("record_core_differs", uuid=u)  # another blob version; the first record stands
        self._tag(u, s.ingested_tags(record["sha256"]))
        self.log("recorded", uuid=u)
        return Outcome("recorded", u, record["sha256"])

    def _bind(self, u, sidecar):
        """The data object is the one the sidecar describes (HeadObject only)."""
        data, up = sidecar["data"], sidecar["data"]["upload"]
        head = self._head(self.staging, s.staging_data_key(u), checksum=True)
        if head is None:
            raise Rejected("no_data", "staging.data", "no data object")
        if up["method"] == "put":
            checksum = ("FULL_OBJECT", s.sha256_b64(data["sha256"]))
        else:
            checksum = ("COMPOSITE", s.composite_sha256(up["part_sha256"]))
        for field, got, want in (
                ("staging.data.etag", head.get("ETag"), data["staging_etag"]),
                ("staging.data.size", head.get("ContentLength"), data["size"]),
                ("staging.data.sse", head.get("ServerSideEncryption"), s.STAGING_SSE),
                ("staging.data.checksum", (head.get("ChecksumType"), head.get("ChecksumSHA256")), checksum)):
            if got != want:
                raise Rejected("data_mismatch", field, "not the object the sidecar describes")
        _checked(s.check_staging_metadata, sidecar, head.get("Metadata") or {})
        return head

    def _check_gate(self, sidecar, committed):
        """A file over COST_GATE needs an approval that covers it: its one
        source, uploads committed (the sidecar written, by S3's clock) before
        it expires. So a file deferred to the job, or retried, isn't refused
        because it was processed after the approval ran out."""
        if sidecar["data"]["size"] <= s.COST_GATE:
            return
        name = sidecar["fetch"]["approval"]
        raw = self._read(self.ops, name, s.MAX_APPROVAL_BYTES) if name else None
        if raw is None:
            raise Rejected("too_large", "sidecar.fetch.approval", "no such approval")
        try:
            s.check_approval(s.parse_approval(raw), sidecar, committed)
        except s.SchemaError as e:
            raise Rejected("too_large", e.field, e.problem) from None

    def _check_references(self, u, sidecar, head):
        """Evidence already holds what the file refers to."""
        data, stamp = sidecar["data"], sidecar["fetch"]["stamp_ref"]
        if stamp is not None and self._head(self.evidence, s.blob_key(stamp)) is None:
            raise Rejected("missing_blob", "sidecar.fetch.stamp_ref", "the stamped manifest isn't held")
        if sidecar["content_kind"] != "fetch_manifest":
            return
        raw = _read_up_to(self._get(s.staging_data_key(u), head), data["size"])
        if len(raw) < data["size"]:
            raise Transient("the staging stream ended early")
        if hashlib.sha256(raw).hexdigest() != data["sha256"]:
            raise Rejected("sha_mismatch", "sidecar.data.sha256", "not the manifest's hash")
        manifest = _checked(s.parse_manifest, raw, stored=False)
        _checked(s.validate_manifest_sidecar, sidecar, manifest)
        sizes = {e["sha256"]: e["size"] for e in manifest["files"] if e["sha256"] is not None}
        shas = s.manifest_shas(manifest)
        with ThreadPoolExecutor(self.workers) as pool:
            heads = list(pool.map(lambda sha: self._head(self.evidence, s.blob_key(sha)), shas))
        missing = sum(h is None for h in heads)
        if missing:
            raise Rejected("missing_blob", "manifest.files", f"{missing} listed files aren't held")
        if any(sizes[sha] is not None and h.get("ContentLength") != sizes[sha] for sha, h in zip(shas, heads)):
            raise Rejected("bad_manifest", "manifest.files.size", "a listed size isn't the held blob's")

    def _store_whole(self, u, head, sidecar):
        """One GET streamed into one PutObject that S3 verifies."""
        data = sidecar["data"]
        key, sha = s.blob_key(data["sha256"]), data["sha256"]
        status, version = "already_stored", None
        if self._head(self.evidence, key) is None:
            body = _CheckedBody(lambda: self._get(s.staging_data_key(u), head), sidecar)
            try:
                put = self.s3.put_object(
                    Bucket=self.evidence, Key=key, Body=body, ContentLength=data["size"],
                    ChecksumSHA256=s.sha256_b64(sha), IfNoneMatch="*",
                    ContentType=s.EVIDENCE_CONTENT_TYPE, ContentDisposition=s.EVIDENCE_CONTENT_DISPOSITION)
                status, version = "stored", put.get("VersionId")
            except Exception as e:
                if body.problem:
                    raise Rejected(*body.problem, "the bytes contradict the sidecar") from None
                if body.reopen_error is not None:  # botocore said only UnseekableStreamError
                    raise body.reopen_error from e
                if _code(e) == "BadDigest":
                    raise Rejected("sha_mismatch", "sidecar.data.sha256", "S3 refused the claimed SHA-256") from None
                if not _is_precondition(e):
                    raise
        if status == "already_stored":
            self._verify(u, head, sidecar)  # no upload, or a 412 before S3 checked the SHA-256: check every claim
        return self._read_back(u, key, version, data["size"], "FULL_OBJECT", s.sha256_b64(sha), None, None,
                               fresh=status == "stored"), status

    def _store_parts(self, u, head, sidecar):
        """Over SINGLE_PUT_MAX: check every claim, then copy server side
        part by part, on the staging object's own boundaries."""
        data, up = sidecar["data"], sidecar["data"]["upload"]
        key, parts, part_size = s.blob_key(data["sha256"]), up["part_sha256"], up["part_size"]
        self._verify(u, head, sidecar)
        composite = s.composite_sha256(parts)
        status, version = "already_stored", None
        if self._head(self.evidence, key) is None:
            upload_id = self.s3.create_multipart_upload(
                Bucket=self.evidence, Key=key, ChecksumAlgorithm="SHA256",
                ContentType=s.EVIDENCE_CONTENT_TYPE, ContentDisposition=s.EVIDENCE_CONTENT_DISPOSITION)["UploadId"]
            pool = ThreadPoolExecutor(self.workers)
            try:
                try:
                    copies = [pool.submit(self._copy_part, u, head, data, key, upload_id, n)
                              for n in range(1, len(parts) + 1)]
                    wait(copies, return_when=FIRST_EXCEPTION)  # whichever part fails first, not the next in order
                finally:
                    pool.shutdown(cancel_futures=True)  # no more parts start; those in flight land before an abort
                done = [c.result() for c in copies]
            except BaseException:  # a failed part, or anything else (Ctrl-C in the job)
                self._abort(key, upload_id)
                raise
            try:
                complete = self.s3.complete_multipart_upload(
                    Bucket=self.evidence, Key=key, UploadId=upload_id, MultipartUpload={"Parts": done},
                    IfNoneMatch="*")
                status, version = "stored", complete.get("VersionId")
            except Exception as e:
                self._abort(key, upload_id)
                if not _is_precondition(e):  # a 412 here means another writer stored the blob first
                    raise
        return self._read_back(u, key, version, data["size"], "COMPOSITE", composite, part_size, parts,
                               fresh=status == "stored"), status

    def _copy_part(self, u, head, data, key, upload_id, n):
        up = data["upload"]
        lo = (n - 1) * up["part_size"]
        hi = min(lo + up["part_size"], data["size"]) - 1
        got = self.s3.upload_part_copy(
            Bucket=self.evidence, Key=key, UploadId=upload_id, PartNumber=n,
            CopySource={"Bucket": self.staging, "Key": s.staging_data_key(u)},
            CopySourceRange=f"bytes={lo}-{hi}", CopySourceIfMatch=head["ETag"])["CopyPartResult"]
        if got.get("ChecksumSHA256") != s.sha256_b64(up["part_sha256"][n - 1]):
            raise Rejected("data_mismatch", "sidecar.data.upload.part_sha256", "a copied part differs")
        return {"PartNumber": n, "ETag": got["ETag"], "ChecksumSHA256": got["ChecksumSHA256"]}

    def _verify(self, u, head, sidecar):
        """Read the staged bytes once and check every claim about them."""
        hashes = _Hashes(sidecar["data"], sha256=True)
        stream = self._get(s.staging_data_key(u), head)
        for chunk in iter(lambda: stream.read(READ_CHUNK), b""):
            hashes.update(chunk)
        if hashes.size < sidecar["data"]["size"]:
            raise Transient("the staging stream ended early")
        problem = hashes.contradiction(sidecar)
        if problem:
            raise Rejected(*problem, "the bytes contradict the sidecar")

    def _read_back(self, u, key, version, size, checksum_type, checksum, part_size, parts, *, fresh):
        """The blob at `key` is these bytes, under Object Lock. `fresh`: this
        run just wrote the version, so S3 locked it under the current rule.
        Any other sighting of a blob with less than half the bucket's default
        retention left (it lapsed, or the rule is short, as dev's one day is)
        renews the lock to a full period from now, by the rule as it is now;
        this role can lengthen a lock, never shorten it. A lock or checksum S3
        doesn't show on this run's own write (no s3:GetObjectRetention, no
        Object Lock, an SSE-KMS key the role can't use) is the deployment's
        problem, not the upload's: Transient, so the DLQ and its alarm catch it."""
        head = self._head(self.evidence, key, checksum=True, version=version)
        if head is None:
            raise Transient("the blob isn't readable")
        if (head.get("ChecksumType"), head.get("ChecksumSHA256"), head.get("ContentLength")) != (
                checksum_type, checksum, size):
            if not head.get("ChecksumSHA256"):
                if fresh or head.get("ServerSideEncryption") not in (None, "AES256"):
                    # this run's own write (S3 checked the SHA-256 as it stored it), or SSE-KMS hiding it
                    raise Transient("checksum_missing: the blob's SHA-256 isn't visible")
                raise Rejected("evidence_conflict", "evidence.checksum_type", "the stored blob has no SHA-256 to verify")
            if (checksum_type == head.get("ChecksumType") == "COMPOSITE" and head.get("ContentLength") == size
                    and self._layout(key, head) != (len(parts), min(part_size, size))):
                raise Rejected("layout_conflict", "evidence.part_size",
                               "the blob at the key was stored on other part boundaries (its bytes aren't compared)")
            raise Rejected("evidence_conflict", "evidence.checksum_sha256", "the stored blob isn't these bytes")
        mode, until = head.get("ObjectLockMode"), head.get("ObjectLockRetainUntilDate")
        until, now = _aware(until) if until else None, self.now()
        held = head.get("ObjectLockEventHold") == "ON"  # an admin's event hold: it can't lapse, and isn't renewed
        if not fresh and not held and (until is None or until < now + self._retention()[1] / 2):
            default_mode, period = self._retention(fresh=True)  # the rule as it is now, not as this container found it
            if until is None or until < now + period / 2:
                denied = None
                try:
                    live = mode in s.LOCK_MODES and until is not None and until > now  # a lapsed lock takes today's mode
                    self.s3.put_object_retention(
                        Bucket=self.evidence, Key=key, VersionId=head["VersionId"], Retention={
                            "Mode": mode if live else default_mode,
                            "RetainUntilDate": (now + period).replace(microsecond=0)})
                except Exception as e:  # another sighting renewed it further first, making this a shortening?
                    if _code(e) != "AccessDenied":
                        raise
                    denied = e
                head = self._head(self.evidence, key, version=head["VersionId"])
                if head is None:
                    raise Transient("the blob isn't readable")
                mode, until = head.get("ObjectLockMode"), head.get("ObjectLockRetainUntilDate")
                until = _aware(until) if until else None
                if denied is not None and (until is None or until < now + period / 2):
                    raise denied
                self.log("lock_renewed" if denied is None else "lock_renewed_elsewhere", uuid=u)
        if mode not in s.LOCK_MODES or until is None or until <= now:
            # under an admin's event hold the blob is safe, but a v1 record needs a date still to come
            raise Transient("lock_held: an event hold with a lapsed date" if held
                            else "lock_missing: the blob's Object Lock isn't visible or has lapsed")
        return {"bucket": self.evidence, "key": key, "version_id": head.get("VersionId") or "null",
                "checksum_type": checksum_type, "checksum_sha256": checksum,
                "part_size": part_size, "part_sha256": list(parts) if parts else None,
                "lock_mode": mode, "retain_until": s.format_timestamp(until)}

    def _layout(self, key, head):
        """(parts, first part's size) of the composite blob version `head`
        describes. The same bytes cut the same way have the same composite."""
        kw = {"Bucket": self.evidence, "Key": key, "PartNumber": 1}
        if head.get("VersionId"):
            kw["VersionId"] = head["VersionId"]
        first = self.s3.head_object(**kw)
        return first.get("PartsCount"), first.get("ContentLength")

    def _put_record(self, u, record):
        """Write the record once. False if one is already there."""
        raw = s.record_bytes(record)
        try:
            self.s3.put_object(Bucket=self.evidence, Key=s.record_key(u), Body=raw, ContentLength=len(raw),
                               ChecksumSHA256=s.sha256_b64(hashlib.sha256(raw).hexdigest()), IfNoneMatch="*",
                               ContentType="application/json")
            return True
        except Exception as e:
            if _is_precondition(e):
                return False
            raise

    # --- S3 helpers ---------------------------------------------------------------------

    def _head(self, bucket, key, *, checksum=False, version=None):
        kw = {"Bucket": bucket, "Key": key}
        if checksum:
            kw["ChecksumMode"] = "ENABLED"
        if version:
            kw["VersionId"] = version
        try:
            return self.s3.head_object(**kw)
        except Exception as e:
            if _is_missing(e):
                return None
            raise

    def _get(self, key, head):
        """The staged data object's body, only if it's still the one bound."""
        return self.s3.get_object(Bucket=self.staging, Key=key, IfMatch=head["ETag"])["Body"]

    def _read(self, bucket, key, limit):
        """Up to limit + 1 bytes of a small document (so an oversized one
        fails its parser), or None if there's no such object."""
        return self._read_doc(bucket, key, limit)[0]

    def _read_doc(self, bucket, key, limit):
        """_read, and the GET's LastModified and ETag."""
        try:
            got = self.s3.get_object(Bucket=bucket, Key=key)
        except Exception as e:
            if _is_missing(e):
                return None, {}
            raise
        want = min(got["ContentLength"], limit + 1)
        raw = _read_up_to(got["Body"], want)
        if len(raw) < want:
            raise Transient("a document's stream ended early")
        return raw, {"LastModified": got.get("LastModified"), "ETag": got.get("ETag")}

    def _tag(self, u, tags):
        """Tag the data object, then the sidecar (which the sweep reads)."""
        tagset = {"TagSet": [{"Key": k, "Value": v} for k, v in sorted(tags.items())]}
        for key in (s.staging_data_key(u), s.staging_sidecar_key(u)):
            try:
                self.s3.put_object_tagging(Bucket=self.staging, Key=key, Tagging=tagset)
            except Exception as e:
                if not _is_missing(e):
                    raise

    def _abort(self, key, upload_id):
        try:
            self.s3.abort_multipart_upload(Bucket=self.evidence, Key=key, UploadId=upload_id)
        except Exception:
            pass  # the bucket's lifecycle aborts incomplete uploads anyway


def _where(exc):
    """Where in this package an exception came from, innermost last: file,
    line and function only, never a value (logs carry no presented text)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return [f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in traceback.extract_tb(exc.__traceback__)
            if os.path.dirname(os.path.abspath(f.filename)) == here][-6:]


def _read_up_to(stream, n):
    """n bytes, or fewer if the stream ends first (reads may come up short)."""
    chunks, got = [], 0
    while got < n:
        chunk = stream.read(min(READ_CHUNK, n - got))
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _checked(fn, *args, **kw):
    """Call a schema check; its SchemaError rejects the upload."""
    try:
        return fn(*args, **kw)
    except s.SchemaError as e:
        raise Rejected(e.reason, e.field, e.problem) from None


def _principal(value):
    """The S3 event's principalId, when it's storable."""
    try:
        return s._check_strict(value, "principal", max_bytes=s.FIELD_LIMITS["principal"])
    except s.SchemaError:
        return None


def _client(name):
    import boto3  # only where AWS is: the Lambda and the one-off job; needs botocore >= 1.36
    from botocore.config import Config
    return boto3.client(name, config=Config(
        retries={"mode": "standard", "total_max_attempts": 3}, max_pool_connections=WORKERS + 4,
        s3={"payload_signing_enabled": False},  # a streamed body can't be hashed ahead; TLS and ChecksumSHA256 cover it
        request_checksum_calculation="when_required", response_checksum_validation="when_required"))


# --- Entry points -------------------------------------------------------------------------


def _field(obj, *path):
    for name in path:
        obj = obj.get(name) if isinstance(obj, dict) else None
    return obj


def staging_keys(message, staging_bucket):
    """(sidecar key, principal) pairs an SQS message asks to process: an
    ObjectCreated notification (keys decoded; other buckets, other events,
    data objects and the s3:TestEvent skipped) or a sweep re-drive
    {"staging_key": ...}."""
    try:
        body = json.loads(message.get("body") or "")
    except ValueError:
        return []
    if not isinstance(body, dict) or body.get("Event") == "s3:TestEvent":
        return []
    if isinstance(body.get("staging_key"), str):
        return [(body["staging_key"], None)]
    out = []
    for r in body.get("Records") if isinstance(body.get("Records"), list) else []:
        key = _field(r, "s3", "object", "key")
        name = _field(r, "eventName")
        if (_field(r, "s3", "bucket", "name") != staging_bucket or not isinstance(key, str)
                or not isinstance(name, str) or not name.startswith("ObjectCreated:")):
            continue
        key = unquote_plus(key)
        if key.startswith(s.STAGING_PREFIX) and key.endswith(s.SIDECAR_SUFFIX):
            principal = _field(r, "userIdentity", "principalId")
            out.append((key, principal if isinstance(principal, str) else None))
    return out


_DEFAULT = None


def handler(event, context=None, *, ingest=None, sqs=None, retry_after=60):
    """The SQS event source (BatchSize 1, ReportBatchItemFailures on). A
    message that fails transiently, or that there's no time left for, is
    reported back and made visible again after retry_after seconds rather
    than after the queue's full visibility timeout."""
    global _DEFAULT
    messages = event.get("Records") or []
    if ingest is None:
        try:
            _DEFAULT = _DEFAULT or Ingest.from_env()
        except Exception:  # the invocation fails (and counts as an error); its messages come back in retry_after
            for message in messages:
                sqs = _retry_soon(message, sqs, retry_after)
            raise
        ingest = _DEFAULT
    failures = []
    for message in messages:
        try:
            if context is not None and context.get_remaining_time_in_millis() < MIN_REMAINING_MS:
                raise Transient("no time left in this invocation")
            for key, principal in staging_keys(message, ingest.staging):
                ingest.process(key, principal=principal)
        except Exception as e:  # only this message is retried; the rest of the batch goes on
            if not isinstance(e, Transient):
                ingest.log("message_error", message_id=message.get("messageId"), error=type(e).__name__,
                           where=_where(e))
            failures.append({"itemIdentifier": message.get("messageId")})
            sqs = _retry_soon(message, sqs, retry_after)
    return {"batchItemFailures": failures}


def _retry_soon(message, sqs, retry_after):
    """Make a failed message visible again after retry_after seconds rather
    than the queue's full visibility timeout. Best effort."""
    queue_url = os.environ.get("QUEUE_URL")
    if queue_url and message.get("receiptHandle"):
        try:
            sqs = sqs or _client("sqs")
            sqs.change_message_visibility(QueueUrl=queue_url, ReceiptHandle=message["receiptHandle"],
                                          VisibilityTimeout=retry_after)
        except Exception:
            pass  # the queue's own timeout applies
    return sqs


def sweep(s3, *, staging_bucket, send, now, min_age=timedelta(hours=1), remaining_ms=None, redrive=True,
          workers=WORKERS, log=print):
    """Re-drive every staged file whose sidecar is older than min_age and
    untagged (a lost event, or retries that ran out), at most MAX_REDRIVES
    times each (counted in a tag); then count it as stuck. Also count what
    needs a person: rejected, deferred and stuck files, data objects whose
    sidecar never came (not ingested ones whose sidecar lifecycle took
    first), and keys that aren't ours. Newest first, `workers` tag reads at
    a time, so a lost event goes out on the next run however many old files
    wait (about 20 per second per worker: tens of thousands of staged files a
    run, and lifecycle removes ingested ones after a day). It stops
    SWEEP_RESERVE_MS before remaining_ms() runs out, and logs its counts
    however it ends: `listed` says whether the whole listing was read,
    `error` names what ended it early, and `unread` counts the files it never
    reached or failed on. `send(dict)` queues one re-drive and is called
    from `workers` threads at once, so it must be thread-safe (boto3 clients
    are); redrive=False only counts."""
    counts = {"redriven": 0, "rejected": 0, "deferred": 0, "stuck": 0, "orphans": 0, "stray": 0, "unread": 0,
              "listed": False}
    out_of_time = lambda: remaining_ms is not None and remaining_ms() < SWEEP_RESERVE_MS  # noqa: E731
    waiting, start, failed = [], 0, 0
    try:
        found, token = {}, None
        while True:
            if out_of_time():
                return counts
            page = s3.list_objects_v2(Bucket=staging_bucket, Prefix=s.STAGING_PREFIX,
                                      **({"ContinuationToken": token} if token else {}))
            for obj in page.get("Contents") or []:
                try:
                    u, kind = s.parse_staging_key(obj["Key"])
                except s.SchemaError:
                    counts["stray"] += 1
                    continue
                found.setdefault(u, {})[kind] = obj
            if not page.get("IsTruncated"):
                break
            token = page["NextContinuationToken"]
        counts["listed"] = True
        cutoff = now - min_age
        for objs in found.values():
            obj = objs.get("sidecar") or objs["data"]  # a data object alone is an orphan, unless ingested
            if _aware(obj["LastModified"]) < cutoff:
                waiting.append(obj)
        waiting.sort(key=lambda o: (_aware(o["LastModified"]), o["Key"]), reverse=True)
        size = max(1, workers)
        with ThreadPoolExecutor(size) as pool:
            while start < len(waiting) and not out_of_time():
                chunk = waiting[start:start + size]
                futures = [pool.submit(_sweep_one, s3, staging_bucket, o["Key"], send, redrive) for o in chunk]
                wait(futures)
                first = None
                for obj, future in zip(chunk, futures):  # tally every file the chunk finished, then raise
                    if future.exception() is not None:
                        first, failed = first or future.exception(), failed + 1
                        continue
                    count, state = future.result()
                    if count:
                        counts[count] += 1
                    if state:  # one line per file that needs a person, for whoever triages or runs the job
                        log(json.dumps({"event": "sweep_file", "uuid": s.parse_staging_key(obj["Key"])[0],
                                        **state}, sort_keys=True))
                start += len(chunk)
                if first is not None:
                    raise first
    except Exception as e:
        counts["error"] = type(e).__name__
        raise
    finally:
        counts["unread"] = len(waiting) - start + failed  # never reached, or failed
        log(json.dumps({"event": "sweep", **counts}, sort_keys=True))
    return counts


def _sweep_one(s3, bucket, key, send, redrive):
    """(what to count, the file's log line) for one waiting key; (None, None)
    when it needs nothing (ingested, or removed after the listing)."""
    try:
        tagset = s3.get_object_tagging(Bucket=bucket, Key=key).get("TagSet") or []
    except Exception as e:
        if _is_missing(e):
            return None, None  # lifecycle removed it after the listing
        raise
    tags = {t["Key"]: t["Value"] for t in tagset}
    if tags.get(s.INGESTED_TAG[0]) == s.INGESTED_TAG[1]:
        return None, None
    if tags.get("intake") == "rejected":
        reason = tags.get("reason")
        return "rejected", {"state": "rejected", "reason": reason if reason in s.REJECT_REASONS else None}
    if tags.get("intake") == "deferred":
        return "deferred", {"state": "deferred"}
    if key.endswith(s.DATA_SUFFIX):
        return "orphans", {"state": "orphan"}
    tries = tags.get(REDRIVES_TAG, "0")
    tries = int(tries) if tries.isascii() and tries.isdigit() else MAX_REDRIVES  # garbled: a person looks
    if tries >= MAX_REDRIVES:
        return "stuck", {"state": "stuck"}
    if redrive:
        try:
            s3.put_object_tagging(Bucket=bucket, Key=key,
                                  Tagging={"TagSet": [{"Key": REDRIVES_TAG, "Value": str(tries + 1)}]})
        except Exception as e:
            if _is_missing(e):
                return None, None
            raise
        try:
            send({"staging_key": key})
        except Exception:
            try:  # the re-drive didn't happen, so it doesn't count (the sweep may write only its own tag)
                s3.put_object_tagging(Bucket=bucket, Key=key,
                                      Tagging={"TagSet": [{"Key": REDRIVES_TAG, "Value": str(tries)}]})
            except Exception:
                pass
            raise
    return "redriven", None


def sweep_handler(event, context=None):
    """The scheduled sweep: re-drives through the ingest queue."""
    sqs, queue_url = _client("sqs"), os.environ["QUEUE_URL"]
    return sweep(_client("s3"), staging_bucket=os.environ["STAGING_BUCKET"], now=datetime.now(timezone.utc),
                 send=lambda msg: sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(msg)),
                 remaining_ms=context.get_remaining_time_in_millis if context is not None else None)


def main(argv=None):
    """The one-off job for deferred files, and a counting-only sweep.
    Buckets come from STAGING_BUCKET, EVIDENCE_BUCKET and OPS_BUCKET."""
    p = argparse.ArgumentParser(prog="python -m pra_intake.ingest")
    sub = p.add_subparsers(dest="command", required=True)
    one = sub.add_parser("process", help="ingest one staged file (in/<uuid>.json)")
    one.add_argument("sidecar_key")
    one.add_argument("--allow-large", action="store_true", help="past the Lambda size limit")
    one.add_argument("--code-sha256", default=None,
                     help="the published zip's SHA-256, only when running exactly that code (else recorded as null)")
    sub.add_parser("sweep", help="count what the sweep would re-drive, changing nothing")
    args = p.parse_args(argv)
    if args.command == "process":
        try:
            ingest = Ingest.from_env(code_sha256=args.code_sha256)
            outcome = ingest.process(args.sidecar_key, allow_large=args.allow_large)
        except Exception as e:  # retryable: fix the cause (the role, a grant, the env) and run it again
            extra = {"variable": e.args[0]} if isinstance(e, KeyError) and e.args else {}
            print(json.dumps({"status": "transient", "error": type(e).__name__, "code": _code(e),
                              "status_code": _http_status(e), "where": _where(e), **extra}, sort_keys=True))
            return EX_TEMPFAIL
        print(json.dumps(outcome.__dict__, sort_keys=True))
        return 0 if outcome.status in ("stored", "already_stored", "recorded") else 1  # rejected, deferred, vanished
    sweep(_client("s3"), staging_bucket=os.environ["STAGING_BUCKET"], now=datetime.now(timezone.utc),
          send=lambda msg: None, redrive=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
