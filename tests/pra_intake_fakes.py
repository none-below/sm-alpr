"""An in-memory S3 for the PRA intake tests: just the calls the ingest Lambda
and the upload library make, with the behavior they rely on, as S3 and
botocore showed it in the 2026-09-28 live probes and documentation:

  - versioning, Object Lock default retention, delete markers;
  - the bucket policies: writes must carry If-None-Match (412 when the key's
    current version exists), and no writer may set tags on upload;
  - IAM, for a client from as_role(): each call needs its action; without
    s3:ListBucket a missing key is a 403, not a 404; without
    s3:GetObjectRetention HeadObject leaves out the Object Lock headers; a
    versioned read needs s3:GetObjectVersion;
  - checksums: a PUT's ChecksumSHA256 is verified (BadDigest), HeadObject
    with ChecksumMode reports FULL_OBJECT for a single PUT and
    b64(sha256(part digests))-N COMPOSITE for multipart; UploadPartCopy
    reports the SHA-256 of the copied range;
  - ETags: MD5 for a single PUT (SSE-S3), md5(part MD5s)-N for multipart;
    a multipart object's LastModified is when its upload was created, and
    HeadObject with PartNumber reports that part's size and PartsCount;
  - If-Match on GET and CopySourceIfMatch on UploadPartCopy; ranged GETs;
  - event notifications for created objects, with S3's key URL-encoding;
  - a streamed PUT body is read like botocore/urllib3 do: it must have
    seek() and tell() (the CRT signer needs them); it is read to EOF even
    when S3 answers early (a 412 arrives only after the body is drained);
    an exception before the declared length is sent fails the upload, but
    once S3 has every byte the object is stored whatever the client does.

Faults and races are injected with fail() and before().
"""
import base64
import contextlib
import copy
import hashlib
import io
import itertools
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

MiB = 1024 * 1024
MIN_PART = 5 * MiB
HTTP_BLOCK = 16384  # how much urllib3 asks a file-like body for at a time


class FakeClientError(Exception):
    """Shaped like botocore's ClientError: .response carries the code and status."""

    def __init__(self, code, status, op=""):
        super().__init__(f"{op}: {code} ({status})")
        self.response = {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


def b64_sha256(data):
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def grants(role_permissions, buckets):
    """{(action, bucket)} from a {bucket role: actions} map and {role: bucket name}."""
    return frozenset((a, buckets[role]) for role, actions in role_permissions.items() for a in actions)


class FakeStream:
    """A GetObject body. max_read caps each read (short reads happen);
    truncate_at ends the stream early, like a dropped connection."""

    def __init__(self, data, *, max_read=None, truncate_at=None, on_read=None):
        self._data, self._pos, self._max, self._on_read = data, 0, max_read, on_read
        self._end = len(data) if truncate_at is None else min(truncate_at, len(data))

    def read(self, n=-1):
        left = self._end - self._pos
        n = left if n is None or n < 0 else min(n, left)
        if self._max:
            n = min(n, self._max)
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        if self._on_read:
            self._on_read(len(chunk))
        return chunk


class Bucket:
    def __init__(self, name, *, versioned, lock_days, lock_mode, require_if_none_match, sse):
        self.name, self.versioned, self.lock_days, self.lock_mode = name, versioned or bool(lock_days), lock_days, lock_mode
        self.require_if_none_match, self.sse = require_if_none_match, sse
        self.objects = {}  # key -> [version dicts], oldest first
        self.uploads = {}  # upload id -> {"key", "algorithm", "parts": {n: part}, ...}


class FakeS3:
    def __init__(self, *, now=datetime(2026, 9, 28, 18, 0, 0, tzinfo=timezone.utc), page_size=1000):
        self._clock, self.page_size = [now], page_size
        self.buckets, self.calls, self.principal = {}, [], "AWS:AROAEXAMPLE:gha-123-1"
        self.role = None  # None: an admin, allowed everything; else a set of (action, bucket)
        self.max_read, self.bytes_read = None, {}
        self._faults, self._hooks, self._subscribers, self._head_edits, self._truncate = [], [], [], {}, {}
        self._ids = itertools.count(1)

    @property
    def now(self):
        return self._clock[0]

    @now.setter
    def now(self, value):
        self._clock[0] = value  # shared with every as_role() view

    def as_role(self, role):
        """A client for the same buckets with `role`'s grants, for code
        under test (and its worker threads) while tests act as an admin."""
        view = copy.copy(self)
        view.role = frozenset(role)
        return view

    # --- setup ---------------------------------------------------------------------

    def create_bucket(self, name, *, versioned=False, lock_days=None, lock_mode="GOVERNANCE",
                      require_if_none_match=False, sse="AES256"):
        self.buckets[name] = Bucket(name, versioned=versioned, lock_days=lock_days, lock_mode=lock_mode,
                                    require_if_none_match=require_if_none_match, sse=sse)
        return self.buckets[name]

    @contextlib.contextmanager
    def acting_as(self, role):
        """Calls inside run with `role`'s grants (None: an admin)."""
        previous, self.role = self.role, role
        try:
            yield self
        finally:
            self.role = previous

    def fail(self, op, *, code="SlowDown", status=503, times=1, when=lambda kw: True):
        """The next `times` calls of `op` for which when(kwargs) holds raise."""
        self._faults.append({"op": op, "code": code, "status": status, "left": times, "when": when})

    def before(self, op, fn, *, times=1, when=lambda kw: True):
        """Run fn(kwargs) as an admin before the next `times` matching calls of `op` (a race)."""
        self._hooks.append({"op": op, "fn": fn, "left": times, "when": when})

    def notify(self, bucket, callback, *, prefix="", suffix=""):
        """callback(event) for each object created in `bucket` under prefix/suffix."""
        self._subscribers.append((bucket, prefix, suffix, callback))

    def edit_head(self, bucket, key, fn):
        """HeadObject of bucket/key returns fn(response): S3 misbehaving, for one check at a time."""
        self._head_edits[(bucket, key)] = fn

    def truncate(self, bucket, key, at):
        """GetObject bodies of bucket/key end after `at` bytes (None: whole)."""
        self._truncate[(bucket, key)] = at

    def tick(self, seconds=1):
        self.now += timedelta(seconds=seconds)
        return self.now

    # --- inspection ------------------------------------------------------------------

    def current(self, bucket, key):
        versions = self.buckets[bucket].objects.get(key) or []
        return versions[-1] if versions and not versions[-1]["delete_marker"] else None

    def versions(self, bucket, key):
        return [v for v in self.buckets[bucket].objects.get(key) or [] if not v["delete_marker"]]

    def keys(self, bucket, prefix=""):
        return sorted(k for k in self.buckets[bucket].objects if k.startswith(prefix) and self.current(bucket, k))

    def tags(self, bucket, key):
        obj = self.current(bucket, key)
        return dict(obj["tags"]) if obj else None

    def ops(self, op=None, bucket=None):
        return [c for c in self.calls if (op is None or c[0] == op) and (bucket is None or c[1] == bucket)]

    # --- the API ---------------------------------------------------------------------

    def _enter(self, op, kw):
        self.calls.append((op, kw.get("Bucket"), kw.get("Key")))
        for h in self._hooks:
            if h["op"] == op and h["left"] and h["when"](kw):
                h["left"] -= 1
                with self.acting_as(None):
                    h["fn"](kw)
        for f in self._faults:
            if f["op"] == op and f["left"] and f["when"](kw):
                f["left"] -= 1
                raise FakeClientError(f["code"], f["status"], op)

    def _allowed(self, action, bucket):
        return self.role is None or (action, bucket) in self.role

    def _need(self, action, bucket, op, *, head=False):
        if not self._allowed(action, bucket):
            raise FakeClientError("403" if head else "AccessDenied", 403, op)

    def _bucket(self, name, op, *, head=False):
        if name not in self.buckets:
            raise FakeClientError("404" if head else "NoSuchBucket", 404, op)
        return self.buckets[name]

    def _version(self, b, key, version_id, op, *, head=False):
        versions = b.objects.get(key) or []
        if version_id is not None:
            self._need("s3:GetObjectVersion", b.name, op, head=head)
            found = [v for v in versions if v["version_id"] == version_id and not v["delete_marker"]]
            if not found:
                raise FakeClientError("404" if head else "NoSuchVersion", 404, op)
            return found[0]
        if not versions or versions[-1]["delete_marker"]:
            if not self._allowed("s3:ListBucket", b.name):  # S3 won't say whether the key exists
                raise FakeClientError("403" if head else "AccessDenied", 403, op)
            raise FakeClientError("404" if head else "NoSuchKey", 404, op)
        return versions[-1]

    def _check_create(self, b, key, if_none_match, op):
        if b.require_if_none_match and if_none_match != "*":
            raise FakeClientError("AccessDenied", 403, op)
        if if_none_match == "*" and self.current(b.name, key) is not None:
            raise FakeClientError("PreconditionFailed", 412, op)

    def _store(self, b, key, data, *, etag, checksum_type, checksum, metadata, content_type,
               content_disposition, op, part_sizes=None, created=None):
        if b.lock_days and checksum is None and op == "PutObject":
            raise FakeClientError("InvalidRequest", 400, op)  # Object Lock needs Content-MD5 or a checksum
        self.tick()
        version = {
            "data": data, "etag": etag, "checksum_type": checksum_type, "checksum": checksum,
            "metadata": dict(metadata or {}), "tags": {}, "content_type": content_type or "binary/octet-stream",
            "content_disposition": content_disposition, "last_modified": created or self.now,
            "part_sizes": part_sizes, "delete_marker": False,
            "version_id": f"v{next(self._ids)}" if b.versioned else "null",
            "lock_mode": b.lock_mode if b.lock_days else None,
            "lock_until": self.now + timedelta(days=b.lock_days) if b.lock_days else None,
        }
        if b.versioned:
            b.objects.setdefault(key, []).append(version)
        else:
            b.objects[key] = [version]
        for bucket, prefix, suffix, callback in self._subscribers:
            if bucket == b.name and key.startswith(prefix) and key.endswith(suffix):
                callback(self.event(b.name, key, version))
        return version

    def event(self, bucket, key, version=None, *, name="ObjectCreated:Put"):
        version = version or self.current(bucket, key)
        return {"Records": [{
            "eventVersion": "2.1", "eventSource": "aws:s3", "eventName": name,
            "userIdentity": {"principalId": self.principal},
            "s3": {"bucket": {"name": bucket},
                   "object": {"key": quote_plus(key, safe="/"), "size": len(version["data"]),
                              "eTag": version["etag"].strip('"'), "versionId": version["version_id"],
                              "sequencer": f"{next(self._ids):016X}"}},
        }]}

    def head_bucket(self, *, Bucket):
        self._enter("head_bucket", dict(Bucket=Bucket))
        self._bucket(Bucket, "HeadBucket", head=True)
        self._need("s3:ListBucket", Bucket, "HeadBucket", head=True)
        return {}

    def put_object(self, *, Bucket, Key, Body=b"", ContentLength=None, ChecksumSHA256=None, IfNoneMatch=None,
                   ContentType=None, ContentDisposition=None, Metadata=None, ServerSideEncryption=None,
                   Tagging=None, **extra):
        kw = dict(Bucket=Bucket, Key=Key, IfNoneMatch=IfNoneMatch, Metadata=Metadata)
        self._enter("put_object", kw)
        assert not extra, f"unmodeled PutObject parameters: {sorted(extra)}"
        streamed = not isinstance(Body, (bytes, bytearray))
        if streamed:  # botocore's CRT signer wraps a body without seek() as bytes, which fails (live, 2026-09-28)
            assert hasattr(Body, "seek") and hasattr(Body, "tell"), "a streamed body needs seek() and tell()"
            assert Body.tell() == 0 and Body.seek(0) == 0
        try:
            b = self._bucket(Bucket, "PutObject")
            self._need("s3:PutObject", Bucket, "PutObject")
            if Tagging is not None:
                raise FakeClientError("AccessDenied", 403, "PutObject")  # policy: no tags on upload
            if ServerSideEncryption not in (None, b.sse):
                raise FakeClientError("AccessDenied", 403, "PutObject")
            if streamed and ContentLength is None:  # botocore sizes a body by seeking to its end...
                try:
                    at = Body.tell()
                    Body.seek(0, io.SEEK_END)
                    ContentLength = Body.tell() - at
                    Body.seek(at)
                except io.UnsupportedOperation:  # ...or sends it chunked, which S3 refuses for PutObject
                    raise FakeClientError("MissingContentLength", 411, "PutObject") from None
            self._check_create(b, Key, IfNoneMatch, "PutObject")
        except FakeClientError:
            if streamed:  # urllib3 2 sends the whole body even after an early reply
                for _ in iter(lambda: Body.read(HTTP_BLOCK), b""):
                    pass
            raise
        if not streamed:
            data = bytes(Body)
        else:  # S3 reads the declared length; an exception before that fails the upload
            chunks, got = [], 0
            while ContentLength is None or got < ContentLength:
                chunk = Body.read(HTTP_BLOCK)
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
            data = b"".join(chunks)
        if ContentLength is not None and len(data) != ContentLength:
            raise FakeClientError("IncompleteBody", 400, "PutObject")
        if ChecksumSHA256 is not None and ChecksumSHA256 != b64_sha256(data):
            raise FakeClientError("BadDigest", 400, "PutObject")
        self._check_create(b, Key, IfNoneMatch, "PutObject")  # a writer that raced in during the body
        v = self._store(b, Key, data, etag=f'"{hashlib.md5(data).hexdigest()}"',
                        checksum_type="FULL_OBJECT" if ChecksumSHA256 else None, checksum=ChecksumSHA256,
                        metadata=Metadata, content_type=ContentType, content_disposition=ContentDisposition,
                        op="PutObject")
        if streamed and ContentLength is not None:
            Body.read(HTTP_BLOCK)  # the client's EOF read: too late to stop a stored object
        out = {"ETag": v["etag"]}
        if b.versioned:
            out["VersionId"] = v["version_id"]
        if ChecksumSHA256:
            out.update(ChecksumSHA256=ChecksumSHA256, ChecksumType="FULL_OBJECT")
        return out

    def _describe(self, b, v, checksum_mode):
        out = {"ContentLength": len(v["data"]), "ETag": v["etag"], "LastModified": v["last_modified"],
               "Metadata": dict(v["metadata"]), "ServerSideEncryption": b.sse, "ContentType": v["content_type"]}
        if v["content_disposition"]:
            out["ContentDisposition"] = v["content_disposition"]
        if b.versioned:
            out["VersionId"] = v["version_id"]
        if v["lock_mode"] and self._allowed("s3:GetObjectRetention", b.name):
            out.update(ObjectLockMode=v["lock_mode"], ObjectLockRetainUntilDate=v["lock_until"])
        if checksum_mode == "ENABLED" and v["checksum"]:
            out.update(ChecksumSHA256=v["checksum"], ChecksumType=v["checksum_type"])
        return out

    def head_object(self, *, Bucket, Key, ChecksumMode=None, VersionId=None, IfMatch=None, PartNumber=None):
        self._enter("head_object", dict(Bucket=Bucket, Key=Key, VersionId=VersionId, PartNumber=PartNumber))
        b = self._bucket(Bucket, "HeadObject", head=True)
        self._need("s3:GetObject", Bucket, "HeadObject", head=True)
        v = self._version(b, Key, VersionId, "HeadObject", head=True)
        if IfMatch is not None and IfMatch != v["etag"]:
            raise FakeClientError("412", 412, "HeadObject")
        out = self._describe(b, v, ChecksumMode)
        if PartNumber is not None:
            sizes = v["part_sizes"] or [len(v["data"])]
            if not 1 <= PartNumber <= len(sizes):
                raise FakeClientError("416", 416, "HeadObject")
            out["ContentLength"] = sizes[PartNumber - 1]
            if v["part_sizes"]:
                out["PartsCount"] = len(sizes)
        edit = self._head_edits.get((Bucket, Key))
        return edit(out) if edit else out

    def get_object(self, *, Bucket, Key, IfMatch=None, VersionId=None, ChecksumMode=None, Range=None):
        self._enter("get_object", dict(Bucket=Bucket, Key=Key, IfMatch=IfMatch))
        b = self._bucket(Bucket, "GetObject")
        self._need("s3:GetObject", Bucket, "GetObject")
        v = self._version(b, Key, VersionId, "GetObject")
        if IfMatch is not None and IfMatch != v["etag"]:
            raise FakeClientError("PreconditionFailed", 412, "GetObject")
        data = v["data"]
        if Range is not None:
            lo, hi = (int(x) for x in Range.removeprefix("bytes=").split("-"))
            data = data[lo:hi + 1]

        def count(n):
            self.bytes_read[(Bucket, Key)] = self.bytes_read.get((Bucket, Key), 0) + n
        out = self._describe(b, v, ChecksumMode)
        out.update(ContentLength=len(data), Body=FakeStream(data, max_read=self.max_read, on_read=count,
                                                            truncate_at=self._truncate.get((Bucket, Key))))
        return out

    def put_object_tagging(self, *, Bucket, Key, Tagging):
        self._enter("put_object_tagging", dict(Bucket=Bucket, Key=Key, Tagging=Tagging))
        b = self._bucket(Bucket, "PutObjectTagging")
        self._need("s3:PutObjectTagging", Bucket, "PutObjectTagging")
        v = self._version(b, Key, None, "PutObjectTagging")
        tags = {t["Key"]: t["Value"] for t in Tagging["TagSet"]}
        assert len(tags) == len(Tagging["TagSet"]) <= 10, "S3 allows 10 unique tags"
        v["tags"] = tags
        return {}

    def get_object_tagging(self, *, Bucket, Key):
        self._enter("get_object_tagging", dict(Bucket=Bucket, Key=Key))
        b = self._bucket(Bucket, "GetObjectTagging")
        self._need("s3:GetObjectTagging", Bucket, "GetObjectTagging")
        v = self._version(b, Key, None, "GetObjectTagging")
        return {"TagSet": [{"Key": k, "Value": t} for k, t in sorted(v["tags"].items())]}

    def delete_object(self, *, Bucket, Key, VersionId=None, BypassGovernanceRetention=False):
        """Admin only (no role the intake code runs as may delete)."""
        self._enter("delete_object", dict(Bucket=Bucket, Key=Key))
        b = self._bucket(Bucket, "DeleteObject")
        self._need("s3:DeleteObject", Bucket, "DeleteObject")
        if VersionId is None and b.versioned:
            self.tick()
            b.objects.setdefault(Key, []).append({"delete_marker": True, "version_id": f"v{next(self._ids)}"})
            return {"DeleteMarker": True}
        versions = b.objects.get(Key) or []
        target = [v for v in versions if VersionId in (None, v["version_id"])]
        for v in target:
            if v.get("lock_until") and v["lock_until"] > self.now and not BypassGovernanceRetention:
                raise FakeClientError("AccessDenied", 403, "DeleteObject")
        b.objects[Key] = [v for v in versions if v not in target]
        return {}

    def list_objects_v2(self, *, Bucket, Prefix="", ContinuationToken=None, MaxKeys=None):
        self._enter("list_objects_v2", dict(Bucket=Bucket, Prefix=Prefix))
        self._bucket(Bucket, "ListObjectsV2")
        self._need("s3:ListBucket", Bucket, "ListObjectsV2")
        keys = self.keys(Bucket, Prefix)
        start = int(ContinuationToken or 0)
        size = min(MaxKeys or self.page_size, self.page_size)
        page = keys[start:start + size]
        out = {"Contents": [{"Key": k, "LastModified": self.current(Bucket, k)["last_modified"],
                             "Size": len(self.current(Bucket, k)["data"]),
                             "ETag": self.current(Bucket, k)["etag"]} for k in page],
               "IsTruncated": start + size < len(keys), "KeyCount": len(page)}
        if out["IsTruncated"]:
            out["NextContinuationToken"] = str(start + size)
        return out

    # --- multipart ---------------------------------------------------------------------

    def create_multipart_upload(self, *, Bucket, Key, ChecksumAlgorithm=None, ContentType=None,
                                ContentDisposition=None, Metadata=None, ServerSideEncryption=None, **extra):
        self._enter("create_multipart_upload", dict(Bucket=Bucket, Key=Key))
        assert not extra, f"unmodeled CreateMultipartUpload parameters: {sorted(extra)}"
        b = self._bucket(Bucket, "CreateMultipartUpload")
        self._need("s3:PutObject", Bucket, "CreateMultipartUpload")
        if ServerSideEncryption not in (None, b.sse):
            raise FakeClientError("AccessDenied", 403, "CreateMultipartUpload")
        upload_id = f"upload-{next(self._ids)}"
        b.uploads[upload_id] = {"key": Key, "algorithm": ChecksumAlgorithm, "parts": {}, "metadata": Metadata,
                                "content_type": ContentType, "content_disposition": ContentDisposition,
                                "created": self.tick()}
        return {"UploadId": upload_id, "Bucket": Bucket, "Key": Key}

    def _upload(self, b, key, upload_id, op):
        upload = b.uploads.get(upload_id)
        if upload is None or upload["key"] != key:
            raise FakeClientError("NoSuchUpload", 404, op)
        return upload

    def _add_part(self, upload, n, data):
        part = {"data": data, "etag": f'"{hashlib.md5(data).hexdigest()}"',
                "checksum": b64_sha256(data) if upload["algorithm"] == "SHA256" else None}
        upload["parts"][n] = part
        return part

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body, ContentLength=None, ChecksumSHA256=None):
        self._enter("upload_part", dict(Bucket=Bucket, Key=Key, PartNumber=PartNumber))
        b = self._bucket(Bucket, "UploadPart")
        self._need("s3:PutObject", Bucket, "UploadPart")
        upload = self._upload(b, Key, UploadId, "UploadPart")
        data = bytes(Body) if isinstance(Body, (bytes, bytearray)) else Body.read()
        if ContentLength is not None and len(data) != ContentLength:
            raise FakeClientError("IncompleteBody", 400, "UploadPart")
        if ChecksumSHA256 is not None and ChecksumSHA256 != b64_sha256(data):
            raise FakeClientError("BadDigest", 400, "UploadPart")
        part = self._add_part(upload, PartNumber, data)
        return {"ETag": part["etag"], **({"ChecksumSHA256": part["checksum"]} if part["checksum"] else {})}

    def upload_part_copy(self, *, Bucket, Key, UploadId, PartNumber, CopySource, CopySourceRange=None,
                         CopySourceIfMatch=None):
        self._enter("upload_part_copy", dict(Bucket=Bucket, Key=Key, PartNumber=PartNumber))
        b = self._bucket(Bucket, "UploadPartCopy")
        self._need("s3:PutObject", Bucket, "UploadPartCopy")
        upload = self._upload(b, Key, UploadId, "UploadPartCopy")
        src_bucket = self._bucket(CopySource["Bucket"], "UploadPartCopy")
        self._need("s3:GetObject", CopySource["Bucket"], "UploadPartCopy")
        src = self._version(src_bucket, CopySource["Key"], CopySource.get("VersionId"), "UploadPartCopy")
        if CopySourceIfMatch is not None and CopySourceIfMatch != src["etag"]:
            raise FakeClientError("PreconditionFailed", 412, "UploadPartCopy")
        data = src["data"]
        if CopySourceRange:
            lo, hi = (int(x) for x in CopySourceRange.removeprefix("bytes=").split("-"))
            if not 0 <= lo <= hi < len(data):
                raise FakeClientError("InvalidRange", 416, "UploadPartCopy")
            data = data[lo:hi + 1]
        part = self._add_part(upload, PartNumber, data)
        result = {"ETag": part["etag"], "LastModified": self.now}
        if part["checksum"]:
            result["ChecksumSHA256"] = part["checksum"]
        return {"CopyPartResult": result}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload, IfNoneMatch=None, **extra):
        self._enter("complete_multipart_upload", dict(Bucket=Bucket, Key=Key, IfNoneMatch=IfNoneMatch))
        assert not extra, f"unmodeled CompleteMultipartUpload parameters: {sorted(extra)}"
        b = self._bucket(Bucket, "CompleteMultipartUpload")
        self._need("s3:PutObject", Bucket, "CompleteMultipartUpload")
        upload = self._upload(b, Key, UploadId, "CompleteMultipartUpload")
        self._check_create(b, Key, IfNoneMatch, "CompleteMultipartUpload")
        listed = MultipartUpload["Parts"]
        numbers = [p["PartNumber"] for p in listed]
        if not listed or numbers != sorted(set(numbers)):
            raise FakeClientError("InvalidPartOrder", 400, "CompleteMultipartUpload")
        parts = []
        for i, p in enumerate(listed):
            part = upload["parts"].get(p["PartNumber"])
            if part is None or part["etag"] != p["ETag"]:
                raise FakeClientError("InvalidPart", 400, "CompleteMultipartUpload")
            if part["checksum"] and p.get("ChecksumSHA256") != part["checksum"]:
                raise FakeClientError("InvalidPart", 400, "CompleteMultipartUpload")
            if i < len(listed) - 1 and len(part["data"]) < MIN_PART:
                raise FakeClientError("EntityTooSmall", 400, "CompleteMultipartUpload")
            parts.append(part)
        md5s = b"".join(bytes.fromhex(p["etag"].strip('"')) for p in parts)
        etag = f'"{hashlib.md5(md5s).hexdigest()}-{len(parts)}"'
        checksum = None
        if upload["algorithm"] == "SHA256":
            digests = b"".join(base64.b64decode(p["checksum"]) for p in parts)
            checksum = f"{base64.b64encode(hashlib.sha256(digests).digest()).decode()}-{len(parts)}"
        del b.uploads[UploadId]
        v = self._store(b, Key, b"".join(p["data"] for p in parts), etag=etag,
                        checksum_type="COMPOSITE" if checksum else None, checksum=checksum,
                        metadata=upload["metadata"], content_type=upload["content_type"],
                        content_disposition=upload["content_disposition"], op="CompleteMultipartUpload",
                        part_sizes=[len(p["data"]) for p in parts], created=upload["created"])
        out = {"ETag": etag, "Bucket": Bucket, "Key": Key}
        if b.versioned:
            out["VersionId"] = v["version_id"]
        if checksum:
            out.update(ChecksumSHA256=checksum, ChecksumType="COMPOSITE")
        return out

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self._enter("abort_multipart_upload", dict(Bucket=Bucket, Key=Key))
        b = self._bucket(Bucket, "AbortMultipartUpload")
        self._need("s3:AbortMultipartUpload", Bucket, "AbortMultipartUpload")
        b.uploads.pop(UploadId, None)
        return {}


def sqs_message(body, message_id="m-1"):
    """An SQS record as the Lambda event source delivers it."""
    return {"messageId": message_id, "receiptHandle": f"rh-{message_id}", "body": json.dumps(body),
            "eventSource": "aws:sqs", "attributes": {"ApproximateReceiveCount": "1"}}
