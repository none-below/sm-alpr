"""The contract for PRA evidence intake: bucket names, keys, the sidecar, the
intake record and the fetch manifest.

The upload library, the ingest Lambda and the sweep all import this module,
so they agree byte for byte. It uses only the standard library, so it ships
unchanged in the Lambda zip; keep it free of syntax newer than Python 3.12,
and keep the source plain ASCII (build any non-ASCII characters with chr()).

Flow:
  1. A writer streams a file to the staging bucket as in/<uuid>.bin, then
     writes in/<uuid>.json, the sidecar, last. The sidecar is the commit
     marker and the only thing that triggers the Lambda.
  2. The Lambda copies the bytes to the evidence bucket at sha256/<hex>. Up to
     SINGLE_PUT_MAX it sends the sidecar's claimed SHA-256 as ChecksumSHA256,
     so S3 itself rejects a wrong claim. Above that it hashes the bytes itself
     and copies them part by part on the staging object's own part boundaries.
  3. It writes _intake/<uuid>.json, one record per sighting, then tags both
     staging objects so lifecycle removes them.

Evidence is content-addressed: identical bytes from any source are stored
once, and how they got there lives only in the records. Blobs and records sit
under a write-once Object Lock, so a layout or field never changes in place:
a change is a new schema version, and readers keep accepting every version
ever written (READABLE_SCHEMAS).

Two kinds of text are treated differently. Identifiers and the fetcher's own
fields (hosts, request ids, run ids, paths) are strict: visible, single-line,
and never a credential. Text an agency or portal presented (filenames, titles,
agency names) is stored exactly, whatever characters it holds: a reject is
terminal, so refusing odd text would lose evidence. canonical_json escapes
every non-ASCII character, and consumers must escape it again before showing
it (display_safe) or feeding it to a model.

A SchemaError carries a reason code and a field path, never the offending
value: sidecars hold filenames, titles and URLs, and error text reaches logs.
"""
import base64
import copy
import hashlib
import json
import math
import re
import uuid as _uuid
from datetime import datetime, timezone
from urllib.parse import unquote, unquote_plus, urlsplit, urlunsplit

SCHEMA_VERSION = 1  # what writers stamp on new sidecars, records and manifests
READABLE_SCHEMAS = frozenset({1})  # every version ever written; never remove one
DERIVER_VERSION = 1  # how the Lambda turns a sidecar into keys and a record

MiB = 1024 * 1024
PART_SIZE = 16 * MiB  # the library sends one PUT at or below this, multipart above
MUCKROCK_ETAG_PART_SIZE = 5 * MiB  # part size behind MuckRock's multipart ETags (checked 2026-09-28)
SINGLE_PUT_MAX = 5_000_000_000  # evidence gets one S3-verified PutObject at or below; decimal, under S3's 5 GiB
COST_GATE = 50_000_000_000  # above this a file needs a recorded approval (fetch.approval)
MIN_PART_SIZE = 5 * MiB  # except the last part
MAX_PART_SIZE = 5 * 1024 ** 3
MAX_PARTS = 10_000
MAX_OBJECT_SIZE = MAX_PARTS * MAX_PART_SIZE  # S3's largest multipart object (48.8 TiB, as of 2026)
MAX_OBSERVED_SIZE = 2 ** 53 - 1  # sizes a third party claims; the largest integer every JSON reader keeps exact
MAX_SIDECAR_BYTES = 1024 * 1024  # room for 10,000 part digests
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_FILES = 100_000
MAX_JSON_DEPTH = 16


class SchemaError(ValueError):
    """A contract violation. `reason` is one of REJECT_REASONS and `field` a
    dotted path; the message never includes the offending value."""

    def __init__(self, reason, field, problem):
        self.reason, self.field, self.problem = reason, field, problem
        super().__init__(f"{reason}: {field}: {problem}")


# Reason codes: used in SchemaError, in the staging reject tag and in logs.
REJECT_REASONS = frozenset({
    "bad_sidecar",  # not strict canonical UTF-8 JSON, too large, or not an object
    "bad_manifest",  # a fetch manifest body that isn't a valid manifest
    "schema_version",  # a schema version this code can't read
    "invalid_metadata",  # a field failed validation
    "signed_url",  # a URL or header carries signing, session or login data
    "forbidden_header",  # a response header outside ALLOWED_HEADERS
    "too_large",  # over COST_GATE without an approval
    "no_data",  # the sidecar's data object is missing
    "data_mismatch",  # the data object isn't the one the sidecar describes
    "sha_mismatch",  # S3 rejected the claimed SHA-256 (BadDigest), or the Lambda's own hash differs
    "uuid_reused",  # a uuid whose record describes different staging bytes
    "missing_blob",  # a fetch manifest names a sha256 evidence doesn't hold
    "manifest_mismatch",  # a manifest's sidecar and body describe different requests
    "evidence_conflict",  # the blob already at the key fails read-back
    "lock_missing",  # the stored blob doesn't carry the expected Object Lock
})


def _fail(field, problem, reason="invalid_metadata"):
    raise SchemaError(reason, field, problem)


def _char_class(*ranges):
    """A regex character class from (first, last) code points, so this source
    never contains the characters themselves."""
    return "[" + "".join(re.escape(chr(a)) + ("-" + re.escape(chr(b)) if b != a else "") for a, b in ranges) + "]"


# Invisible or direction-changing characters that make text display as
# something else: soft hyphen, Arabic letter mark, Mongolian vowel separator,
# zero-width and bidi marks, line and paragraph separators, bidi embeddings and
# overrides, word joiner and invisible operators, bidi isolates, BOM,
# interlinear annotations, and Unicode tag characters.
_INVISIBLE = ((0xAD, 0xAD), (0x61C, 0x61C), (0x180E, 0x180E), (0x200B, 0x200F), (0x2028, 0x202E),
              (0x2060, 0x2064), (0x2066, 0x206F), (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB), (0xE0000, 0xE007F))
_STRICT_BAD_RE = re.compile(_char_class((0x00, 0x1F), (0x7F, 0x9F), *_INVISIBLE))


def display_safe(text):
    """Text with every control and invisible character shown as <U+XXXX>, for
    showing presented text to a person or a model."""
    return _STRICT_BAD_RE.sub(lambda m: f"<U+{ord(m.group()):04X}>", text)


# --- Bucket names ------------------------------------------------------------

ENV_PREFIXES = {"prod": "sm-alpr", "dev": "sm-alpr-dev"}
BUCKET_ROLES = ("staging", "evidence", "ops", "derived")
RETIRED_BUCKET_PREFIX = "sm-alpr-pra"  # #822's bucket, retired 2026-09-28; no role may produce it
_ACCOUNT_RE = re.compile(r"[0-9]{12}")
_REGION_RE = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]{1,2}")
_BUCKET_NAME_RE = re.compile(
    r"(sm-alpr(?:-dev)?)-(staging|evidence|ops|derived)-([0-9]{12})-([a-z]{2}(?:-[a-z]+)+-[0-9]{1,2})-an")


def bucket_name(role, env, account_id, region):
    """<prefix>-<role>-<account>-<region>-an: S3's account-regional namespace,
    so no other AWS account can ever claim the name, even after deletion."""
    if env not in ENV_PREFIXES:
        raise ValueError(f"unknown env {env!r}")
    if role not in BUCKET_ROLES:
        raise ValueError(f"unknown bucket role {role!r}")
    if type(account_id) is not str or not _ACCOUNT_RE.fullmatch(account_id):
        raise ValueError("account id must be 12 ASCII digits")
    if type(region) is not str or not _REGION_RE.fullmatch(region):
        raise ValueError("bad region")
    name = f"{ENV_PREFIXES[env]}-{role}-{account_id}-{region}-an"
    if len(name) > 63:
        raise ValueError("bucket name over 63 characters")
    return name


def parse_bucket_name(name):
    """(env, role, account, region) for a name bucket_name makes; SchemaError otherwise."""
    m = _BUCKET_NAME_RE.fullmatch(name) if type(name) is str else None
    if not m or len(name) > 63:
        _fail("bucket", "not an intake bucket name")
    env = {v: k for k, v in ENV_PREFIXES.items()}[m.group(1)]
    return env, m.group(2), m.group(3), m.group(4)


# --- Keys ----------------------------------------------------------------------

STAGING_PREFIX = "in/"
DATA_SUFFIX = ".bin"
SIDECAR_SUFFIX = ".json"
BLOB_PREFIX = "sha256/"
RECORD_PREFIX = "_intake/"
ERRATA_PREFIX = "_errata/"

_UUID4_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_HEX_RES = {n: re.compile(f"[0-9a-f]{{{n}}}") for n in (32, 40, 64)}


def new_uuid():
    return str(_uuid.uuid4())


def is_uuid4(value):
    """Lower-case canonical UUID version 4, the only form keys accept."""
    return type(value) is str and _UUID4_RE.fullmatch(value) is not None


def _require_uuid4(value, field="uuid"):
    if not is_uuid4(value):
        _fail(field, "not a lower-case UUID4")
    return value


def _require_hex(value, n, field):
    if type(value) is not str or not _HEX_RES[n].fullmatch(value):
        _fail(field, f"not {n} lower-case hex digits")
    return value


def staging_data_key(u):
    return f"{STAGING_PREFIX}{_require_uuid4(u)}{DATA_SUFFIX}"


def staging_sidecar_key(u):
    return f"{STAGING_PREFIX}{_require_uuid4(u)}{SIDECAR_SUFFIX}"


def parse_staging_key(key):
    """(uuid, "data" | "sidecar") for a key in the staging bucket.

    S3 event keys arrive URL-encoded; decode them (unquote_plus) first. Our
    keys are plain ASCII, so a key that needed decoding isn't one of ours."""
    if type(key) is str and key.startswith(STAGING_PREFIX):
        rest = key[len(STAGING_PREFIX):]
        for suffix, kind in ((DATA_SUFFIX, "data"), (SIDECAR_SUFFIX, "sidecar")):
            if rest.endswith(suffix) and is_uuid4(rest[:-len(suffix)]):
                return rest[:-len(suffix)], kind
    _fail("staging_key", "not in/<uuid4>.bin or in/<uuid4>.json")


def blob_key(sha256_hex):
    return f"{BLOB_PREFIX}{_require_hex(sha256_hex, 64, 'sha256')}"


def parse_blob_key(key):
    if type(key) is str and key.startswith(BLOB_PREFIX):
        sha = key[len(BLOB_PREFIX):]
        if _HEX_RES[64].fullmatch(sha):
            return sha
    _fail("blob_key", "not sha256/<64 hex>")


def record_key(u):
    return f"{RECORD_PREFIX}{_require_uuid4(u)}.json"


def parse_record_key(key):
    if type(key) is str and key.startswith(RECORD_PREFIX) and key.endswith(".json"):
        u = key[len(RECORD_PREFIX):-len(".json")]
        if is_uuid4(u):
            return u
    _fail("record_key", "not _intake/<uuid4>.json")


def errata_key(u, n):
    """_errata/<uuid>/<nnnn>.json: the nth correction to a record, applied in order."""
    if type(n) is not int or not 1 <= n <= 9999:
        raise ValueError("erratum number must be 1..9999")
    return f"{ERRATA_PREFIX}{_require_uuid4(u)}/{n:04d}.json"


# Evidence objects get headers that depend on nothing but their content: two
# sightings can name the same bytes differently, so the blob takes no name.
EVIDENCE_CONTENT_TYPE = "application/octet-stream"
EVIDENCE_CONTENT_DISPOSITION = "attachment"
STAGING_SSE = "AES256"  # staging is SSE-S3, where a single PUT's ETag is its MD5

# Staging tags. Only the ingest Lambda may set them (bucket policy). The
# lifecycle rule matches INGESTED_TAG exactly: S3 tag filters need key and value.
INGESTED_TAG = ("ingested", "true")


def ingested_tags(sha256_hex):
    return {INGESTED_TAG[0]: INGESTED_TAG[1], "sha256": _require_hex(sha256_hex, 64, "sha256")}


def rejected_tags(reason):
    if reason not in REJECT_REASONS:
        raise ValueError("unknown reject reason")
    return {"intake": "rejected", "reason": reason}


def staging_metadata(sidecar_source, u, fetch_id):
    """x-amz-meta for in/<uuid>.bin: just enough to triage a data object whose
    sidecar never arrived. The sidecar is authoritative for everything else.
    These fields are ASCII and short, so this stays under S3's 2 KB."""
    _require_uuid4(u)
    _require_uuid4(fetch_id, "fetch_id")
    src = _check_source(copy.deepcopy(sidecar_source), "source")
    return {
        "schema": str(SCHEMA_VERSION),
        "uuid": u,
        "fetch-id": fetch_id,
        "source-kind": src["kind"],
        "platform": src["platform"],
        "host": src["host"],
        "request-id": src["request_id"],
    }


# --- Canonical JSON --------------------------------------------------------------


def canonical_json(obj):
    """The one serialization for anything stored: sorted keys, ASCII only
    (every other character as a backslash-u escape), no spaces, one trailing
    newline. Parsers insist on it, so a stored document's hash is
    reproducible from its parsed form."""
    text = json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return (text + "\n").encode("ascii")


def _unique_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise SchemaError("bad_sidecar", "json", "duplicate key")
    return dict(pairs)


def _no_constants(name):
    raise SchemaError("bad_sidecar", "json", "NaN or Infinity")


def _no_floats(text):
    raise SchemaError("bad_sidecar", "json", "a number with a fraction or exponent")


_JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)


def _json_depth(text):
    depth = deepest = 0
    for ch in re.sub(r"[^\[\]{}]", "", _JSON_STRING_RE.sub("", text)):
        depth += 1 if ch in "[{" else -1
        deepest = max(deepest, depth)
    return deepest


def parse_strict_json(data, *, max_bytes, field="json", reason="bad_sidecar", canonical=True):
    """Parse stored JSON: UTF-8, within max_bytes and MAX_JSON_DEPTH, no
    duplicate keys, integers only, and (by default) exactly canonical_json's
    bytes, so the document's hash can be recomputed from what it says."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("expected bytes")
    data = bytes(data)
    if len(data) > max_bytes:
        _fail(field, "too large", reason)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail(field, "not UTF-8", reason)
    if _json_depth(text) > MAX_JSON_DEPTH:
        _fail(field, "nested too deeply", reason)
    try:
        obj = json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_no_constants, parse_float=_no_floats)
    except SchemaError as e:
        raise SchemaError(reason, field, e.problem) from None
    except (ValueError, RecursionError):
        _fail(field, "not strict JSON", reason)
    if canonical and canonical_json(obj) != data:
        _fail(field, "not canonical JSON", reason)
    return obj


# --- Timestamps --------------------------------------------------------------------

# One spelling per instant: UTC, a Z, and a fraction only when non-zero, with
# no trailing zeros. format_timestamp makes it; the validator insists on it.
_TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{0,5}[1-9])?Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def format_timestamp(dt):
    """The stored form of an aware datetime, e.g. 2026-09-28T18:05:19.5Z."""
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    dt = dt.astimezone(timezone.utc)
    frac = f"{dt.microsecond:06d}".rstrip("0")
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + (f".{frac}" if frac else "") + "Z"


def _timestamp(v, f):
    if type(v) is not str or not _TS_RE.fullmatch(v):
        _fail(f, "not a canonical UTC timestamp like 2026-09-28T18:05:19.5Z")
    try:
        datetime.strptime(v[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        _fail(f, "not a real date and time")
    return v


def parse_timestamp(v):
    """A validated timestamp as an aware datetime, for ordering checks."""
    _timestamp(v, "timestamp")
    return datetime.fromisoformat(v[:-1]).replace(tzinfo=timezone.utc)


def _date(v, f):
    if type(v) is not str or not _DATE_RE.fullmatch(v):
        _fail(f, "not a date like 2026-09-28")
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        _fail(f, "not a real date")
    return v


# --- Checksums -------------------------------------------------------------------


def sha256_b64(sha256_hex):
    """S3's ChecksumSHA256 for a full-object SHA-256."""
    return base64.b64encode(bytes.fromhex(_require_hex(sha256_hex, 64, "sha256"))).decode("ascii")


def composite_sha256(part_sha256_hex):
    """S3's composite ChecksumSHA256 for a multipart object, as HeadObject and
    CompleteMultipartUpload report it: base64(sha256(concatenated raw part
    digests)) + "-<parts>". GetObjectAttributes omits the suffix. (Checked
    live 2026-09-28.)"""
    if not part_sha256_hex:
        raise ValueError("no parts")
    raw = b"".join(bytes.fromhex(_require_hex(p, 64, "part_sha256")) for p in part_sha256_hex)
    return f"{base64.b64encode(hashlib.sha256(raw).digest()).decode('ascii')}-{len(part_sha256_hex)}"


def md5_multipart_etag(part_md5_hex):
    """An S3 multipart ETag body: md5(concatenated raw part MD5s) + "-<parts>"."""
    if not part_md5_hex:
        raise ValueError("no parts")
    raw = b"".join(bytes.fromhex(_require_hex(p, 32, "part_md5")) for p in part_md5_hex)
    return f"{hashlib.md5(raw).hexdigest()}-{len(part_md5_hex)}"


def part_count(size, part_size):
    """Parts in a multipart upload of `size` bytes cut at `part_size`."""
    return max(1, math.ceil(size / part_size))


_ETAG_MD5_RE = re.compile(r"[0-9a-f]{32}")
_ETAG_MULTIPART_RE = re.compile(r"[0-9a-f]{32}-[1-9][0-9]{0,4}")


def etag_form(etag):
    """Classify an HTTP ETag: ("md5", hex), ("md5-multipart", "hex-N") or
    ("opaque", None). Only the shape is checked: the ETag of an encrypted S3
    object has the md5 shape without being one, so a mismatch isn't proof of
    corruption, and a multipart ETag depends on the uploader's part size."""
    if type(etag) is not str:
        return "opaque", None
    body = etag.strip()
    if body.startswith("W/"):
        return "opaque", None  # weak validators promise equivalence, not bytes
    if len(body) >= 2 and body[0] == body[-1] == '"':
        body = body[1:-1]
    body = body.lower()
    if _ETAG_MD5_RE.fullmatch(body):
        return "md5", body
    if _ETAG_MULTIPART_RE.fullmatch(body):
        return "md5-multipart", body
    return "opaque", None


def content_md5_digest(value):
    """The 16-byte digest in a Content-MD5 style header (base64, or 32 hex
    digits as some servers send it); None if it is neither."""
    if type(value) is not str:
        return None
    v = value.strip()
    if _ETAG_MD5_RE.fullmatch(v.lower()):
        return bytes.fromhex(v.lower())
    try:
        raw = base64.b64decode(v, validate=True)
    except ValueError:
        return None
    return raw if len(raw) == 16 else None


# --- Content sniffing --------------------------------------------------------------

SNIFF_BYTES = 1024  # the PDF spec lets the header sit anywhere in the first 1024 bytes
SNIFF_TYPES = ("empty", "pdf", "zip", "ole2", "gzip", "7z", "rar", "xz", "bzip2", "sqlite",
               "png", "jpeg", "gif", "tiff", "bmp", "isobmff", "riff", "mp3", "asf", "matroska",
               "ogg", "flac", "rtf", "html", "xml", "text", "unknown")
_MAGIC = (
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"PK\x07\x08", "zip"),
    (bytes.fromhex("d0cf11e0a1b11ae1"), "ole2"),  # legacy .doc/.xls/.msg
    (b"\x1f\x8b", "gzip"),
    (bytes.fromhex("377abcaf271c"), "7z"),
    (b"Rar!\x1a\x07", "rar"),
    (bytes.fromhex("fd377a585a00"), "xz"),
    (b"BZh", "bzip2"),
    (b"SQLite format 3\x00", "sqlite"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"), (b"GIF89a", "gif"),
    (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"),
    (b"RIFF", "riff"),  # WAV, AVI, WebP
    (b"ID3", "mp3"), (b"\xff\xfb", "mp3"), (b"\xff\xf3", "mp3"), (b"\xff\xf2", "mp3"),
    (bytes.fromhex("3026b2758e66cf11"), "asf"),  # WMV, WMA
    (bytes.fromhex("1a45dfa3"), "matroska"),  # MKV, WebM
    (b"OggS", "ogg"),
    (b"fLaC", "flac"),
    (b"{\\rtf", "rtf"),
)
# Bytes that don't occur in text in any single-byte encoding or UTF-8: C0
# controls other than tab, LF, FF, CR, SUB (DOS end of file) and ESC.
_BINARY_BYTES = frozenset(set(range(0, 9)) | {11} | set(range(14, 26)) | set(range(28, 32)))


def sniff_type(head):
    """A coarse type from a file's first bytes: one of SNIFF_TYPES. Used to catch
    a portal answering 200 with an HTML error page where a PDF should be."""
    head = bytes(head[:SNIFF_BYTES])
    if not head:
        return "empty"
    for magic, name in _MAGIC:
        if head.startswith(magic):
            return name
    if head[4:8] == b"ftyp":
        return "isobmff"  # MP4, MOV, M4A, HEIC
    if head.startswith(b"BM") and head[6:10] == b"\x00\x00\x00\x00":
        return "bmp"
    start = head[3:] if head.startswith(b"\xef\xbb\xbf") else head
    start = start.lstrip(b" \t\r\n\x0c").lower()
    if start.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        return "html"
    if start.startswith(b"<?xml"):
        return "html" if b"<html" in head.lower() else "xml"
    if b"%PDF-" in head:
        return "pdf"
    if not any(b in _BINARY_BYTES for b in head):
        return "text"
    return "unknown"


# --- Field checks ------------------------------------------------------------------

_HOST_LABEL = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
_HOST_RE = re.compile(rf"(?=.{{4,253}}\Z){_HOST_LABEL}(?:\.{_HOST_LABEL})+")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ETAG_VALUE_RE = re.compile(r'"[0-9a-f]{32}(?:-[1-9][0-9]{0,4})?"')
_B64_SHA256_RE = re.compile(r"[A-Za-z0-9+/]{43}=(?:-[1-9][0-9]{0,4})?")


def _check_type(v, f, t, what):
    if type(v) is not t:  # exact: a str or int subclass (an Enum, a bool) serializes differently
        _fail(f, f"not {what}")


def _check_presented(v, f, *, max_bytes):
    """Text an agency or portal presented: stored exactly. Only a lone
    surrogate (no UTF-8 form) and size are refused."""
    _check_type(v, f, str, "a string")
    if not v:
        _fail(f, "empty")
    try:
        size = len(v.encode("utf-8"))
    except UnicodeEncodeError:
        _fail(f, "not encodable as UTF-8")
    if size > max_bytes:
        _fail(f, "too long")
    return v


# Credentials anywhere in a string: signing or session query parameters, a
# cookieless ASP.NET session segment, a Java path session, or user info.
SIGNING_PARAMS = frozenset({
    "signature", "expires", "key-pair-id", "policy",  # S3 SigV2, CloudFront
    "awsaccesskeyid", "googleaccessid",  # SigV2-style access-key identity
    "sig", "se", "sp", "sv", "sr", "st", "spr", "srt", "ss", "skoid", "sktid", "skt", "ske", "sks",
    "skv", "ses",  # Azure SAS
    "tempauth",  # SharePoint/OneDrive
    "__token__", "hdnts", "hdnea",  # Akamai
    "token", "access_token", "id_token", "refresh_token", "client_secret", "apikey", "api_key",
    "sessionid", "ssessionid", "jsessionid", "phpsessid",
})
_SIGNING_NAMES = "|".join(re.escape(p) for p in sorted(SIGNING_PARAMS, key=len, reverse=True))
_CREDENTIAL_RE = re.compile(
    rf"[?&;](?:x-amz-[a-z0-9-]+|x-goog-[a-z0-9-]+|{_SIGNING_NAMES})="  # a signing parameter
    r"|/\([a-z]\("  # ASP.NET cookieless /(S(..)) /(F(..)) /(X(..)S(..)) ...
    r"|;jsessionid="
    r"|://[^/?#\s]*@",  # user info
    re.IGNORECASE)


def is_signing_param(name):
    n = name.lower()
    return n.startswith(("x-amz-", "x-goog-")) or n in SIGNING_PARAMS


def _credential_in(text):
    """True if text, or text percent-decoded up to three times, carries a credential."""
    for _ in range(4):
        if _CREDENTIAL_RE.search(text):
            return True
        decoded = unquote(text)
        if decoded == text:
            return False
        text = decoded
    return True  # still encoded after three rounds: refuse rather than guess


def _check_strict(v, f, *, max_bytes, pattern=None, what=None):
    """An identifier or a field the fetcher writes: visible single-line text
    that carries no credential."""
    _check_type(v, f, str, "a string")
    if not v:
        _fail(f, "empty")
    if len(v.encode("utf-8", "surrogatepass")) > max_bytes:
        _fail(f, "too long")
    if _STRICT_BAD_RE.search(v):
        _fail(f, "control or invisible character")
    if pattern is not None and not pattern.fullmatch(v):
        _fail(f, f"not {what}")
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        _fail(f, "not encodable as UTF-8")
    if _credential_in(v):
        _fail(f, "carries a credential", "signed_url")
    return v


def _strict(max_bytes, pattern=None, what=None):
    return lambda v, f: _check_strict(v, f, max_bytes=max_bytes, pattern=pattern, what=what)


def _presented(max_bytes):
    return lambda v, f: _check_presented(v, f, max_bytes=max_bytes)


def _int(lo, hi):
    def check(v, f):
        _check_type(v, f, int, "an integer")
        if not lo <= v <= hi:
            _fail(f, "out of range")
        return v
    return check


def _enum(values):
    values = tuple(values)

    def check(v, f):
        if type(v) is not str or v not in values:
            _fail(f, "not an allowed value")
        return v
    return check


def _in_set(values):
    def check(v, f):
        if type(v) is not int or v not in values:
            _fail(f, "not an allowed value")
        return v
    return check


def _regex(pattern, what):
    def check(v, f):
        if type(v) is not str or not pattern.fullmatch(v):
            _fail(f, f"not {what}")
        return v
    return check


def _hex(n):
    return lambda v, f: _require_hex(v, n, f)


def _uuid4(v, f):
    return _require_uuid4(v, f)


def _host(v, f):
    if type(v) is not str or not _HOST_RE.fullmatch(v):
        _fail(f, "not a lower-case DNS host name")
    if v.rsplit(".", 1)[1].isdigit():
        _fail(f, "an IP address, not a host name")
    return v


def _nullable(check):
    return lambda v, f: None if v is None else check(v, f)


def _list_of(check, max_items, min_items=0):
    def run(v, f):
        _check_type(v, f, list, "a list")
        if not min_items <= len(v) <= max_items:
            _fail(f, "wrong number of items")
        for i, item in enumerate(v):
            check(item, f"{f}[{i}]")
        return v
    return run


def _check_obj(v, field, spec):
    """Every key in spec is required (null where a field is absent) and no
    other key is allowed: records are permanent, so there's no room for
    'missing' meaning something different from null."""
    _check_type(v, field, dict, "an object")
    if set(v) - set(spec):
        _fail(f"{field}.<unknown>", "unknown field")  # a key name is writer data too, so never echo it
    for name, check in spec.items():
        if name not in v:
            _fail(f"{field}.{name}", "missing")
        check(v[name], f"{field}.{name}")
    return v


def _obj(spec):
    return lambda v, f: _check_obj(v, f, spec)


# --- URLs and response headers ------------------------------------------------------

_COOKIELESS_SEGMENT_RE = re.compile(r"/\((?:[A-Za-z]\([^()/]*\))+\)(?=/|$)")
_JSESSION_RE = re.compile(r";jsessionid=[^/?#;]*", re.IGNORECASE)


def _check_url(v, f, *, allow_query=True):
    """A stable http(s) URL: ASCII, a lower-case host name, no user info,
    fragment, session or signing parameter anywhere (also percent-encoded or
    nested in another parameter)."""
    _check_type(v, f, str, "a string")
    if not v or len(v) > 2048 or not v.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in v):
        _fail(f, "not a bounded ASCII URL")
    try:
        parts = urlsplit(v)
        port = parts.port
    except ValueError:
        _fail(f, "not a URL")
    if parts.scheme not in ("http", "https"):
        _fail(f, "not http(s)")
    if _credential_in(v) or "@" in parts.netloc:
        _fail(f, "carries a credential", "signed_url")
    for seg in re.split(r"[&;]", parts.query):
        if seg and is_signing_param(unquote_plus(seg.split("=", 1)[0])):
            _fail(f, "signing or session parameter", "signed_url")
    if port is not None and not 1 <= port <= 65535:
        _fail(f, "bad port")
    _host(parts.hostname, f)
    expected_netloc = parts.hostname + (f":{port}" if port is not None else "")
    if parts.netloc != expected_netloc:
        _fail(f, "host name not lower-case, or an odd port form")
    if parts.fragment or "#" in v:
        _fail(f, "fragment in URL")
    if not allow_query and (parts.query or "?" in v):
        _fail(f, "query in URL")
    return v


def _url(allow_query=True):
    return lambda v, f: _check_url(v, f, allow_query=allow_query)


def strip_path_session(path):
    """A URL path without ASP.NET cookieless segments or Java ;jsessionid."""
    return _JSESSION_RE.sub("", _COOKIELESS_SEGMENT_RE.sub("", path)) or "/"


def strip_signing_params(url):
    """The stable form of a source URL: scheme and host lower-cased, no user
    info or fragment, no session path segments, and no signing or session
    query parameters. The parameters kept are kept byte for byte, in order."""
    parts = urlsplit(url)
    netloc = (parts.hostname or "") + (f":{parts.port}" if parts.port is not None else "")
    kept = [seg for seg in re.split(r"[&;]", parts.query)
            if seg and not is_signing_param(unquote_plus(seg.split("=", 1)[0]))]
    return urlunsplit((parts.scheme.lower(), netloc, strip_path_session(parts.path), "&".join(kept), ""))


def url_without_query(url):
    """scheme://host[:port]/path of an absolute URL, or the bare path of a
    relative one, with sessions removed: the form redirect hops and URL
    headers are stored in. Anything unparseable becomes "" (never an error)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme and parts.netloc:
        netloc = (parts.hostname or "") + (f":{port}" if port is not None else "")
        return urlunsplit((parts.scheme.lower(), netloc, strip_path_session(parts.path), "", ""))
    return strip_path_session(parts.path) if parts.path.startswith("/") else ""


# Response headers kept in the sidecar. Anything else (cookies, tokens, any
# vendor header that could hold a credential) is dropped by sanitize_headers
# and refused by the validator.
ALLOWED_HEADERS = frozenset({
    "accept-ranges", "age", "cache-control", "content-disposition", "content-encoding",
    "content-language", "content-length", "content-location", "content-md5", "content-range",
    "content-type", "date", "etag", "expires", "last-modified", "location", "server",
    "transfer-encoding", "vary", "via", "x-aspnet-version", "x-cache", "x-content-type-options",
    "x-powered-by",
    "x-amz-cf-id", "x-amz-cf-pop", "x-amz-id-2", "x-amz-request-id", "x-amz-server-side-encryption",
    "x-amz-storage-class", "x-amz-version-id",
    "x-ms-blob-content-md5", "x-ms-blob-type", "x-ms-creation-time", "x-ms-request-id",
    "x-ms-server-encrypted", "x-ms-version", "x-ms-version-id",
    "x-goog-generation", "x-goog-hash", "x-goog-stored-content-encoding", "x-goog-stored-content-length",
})
URL_HEADERS = frozenset({"location", "content-location"})
MAX_HEADERS = len(ALLOWED_HEADERS)
MAX_HEADER_BYTES = 8192
_HEADER_BAD_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # C0 controls (CR, LF) other than tab
OMITTED_LONG = "[omitted: over 8192 bytes]"
OMITTED_CREDENTIAL = "[omitted: credential]"


def _check_header_value(name, value, f):
    _check_type(value, f, str, "a string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        _fail(f, "not encodable as UTF-8")
    if size > MAX_HEADER_BYTES:
        _fail(f, "too long")
    if _HEADER_BAD_RE.search(value):
        _fail(f, "control character")
    if _credential_in(value):
        _fail(f, "carries a credential", "signed_url")
    if name in URL_HEADERS and ("?" in value or "#" in value):
        _fail(f, "query or fragment in a URL header", "signed_url")


def _check_headers(v, f):
    _check_type(v, f, dict, "an object")
    if len(v) > MAX_HEADERS:
        _fail(f, "too many headers")
    for name in v:
        field = f"{f}.<header>"  # names come from the server, so never echo them
        if type(name) is not str or name not in ALLOWED_HEADERS:
            _fail(field, "a header outside ALLOWED_HEADERS", "forbidden_header")
        _check_header_value(name, v[name], field)
    return v


def _header_text(value):
    """A header value as text. HTTP clients decode header bytes as Latin-1; a
    value that is really UTF-8 (as raw filenames in Content-Disposition often
    are) is decoded as UTF-8 instead."""
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        try:
            raw = str(value).encode("latin-1")
        except UnicodeEncodeError:
            raw = None
    if raw is not None:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    else:
        text = str(value)
    return _HEADER_BAD_RE.sub(" ", text).strip()  # CR/LF from obsolete line folding


def sanitize_headers(pairs):
    """Response headers in the form the sidecar stores: names lower-cased, only
    ALLOWED_HEADERS, repeats joined with ", ", URL headers without query,
    fragment, user info or session, oversized or credential-bearing values
    replaced by a marker. The result always passes validation."""
    out = {}
    for name, value in pairs:
        name = (name.decode("latin-1") if isinstance(name, (bytes, bytearray)) else str(name)).strip().lower()
        if name not in ALLOWED_HEADERS:
            continue
        value = _header_text(value)
        if name in URL_HEADERS:
            value = url_without_query(value)
        out[name] = f"{out[name]}, {value}" if name in out else value
    for name, value in out.items():
        if len(value.encode("utf-8")) > MAX_HEADER_BYTES:
            out[name] = OMITTED_LONG
        elif _credential_in(value):
            out[name] = OMITTED_CREDENTIAL
    return out


# --- The sidecar --------------------------------------------------------------------

SOURCE_KINDS = ("muckrock", "portal", "own")
PLATFORMS = ("muckrock", "nextrequest", "govqa", "justfoia", "logikcull", "email", "fileshare", "other")
ORIGINS = ("live", "local-copy", "git", "generated")  # generated: a fetch manifest the library wrote
CONTENT_KINDS = ("file", "fetch_manifest")
UPLOAD_METHODS = ("put", "multipart")
ETAG_CHECKS = ("md5", "md5-multipart", "unmatched", "opaque", "absent")
CONTENT_MD5_CHECKS = ("match", "mismatch", "absent")
LENGTH_CHECKS = ("ok", "undeclared")
EOF_CHECKS = ("clean", "unknown")
ACCESS = ("anonymous", "requester")
MANIFEST_FILENAME = "fetch_manifest.json"
MAX_REDIRECTS = 30

_SOURCE_SPEC = {
    "kind": _enum(SOURCE_KINDS),
    "platform": _enum(PLATFORMS),
    # The platform instance a request id belongs to (request ids repeat across
    # agencies): the request_url's host when there is one; for email, the
    # sender's domain.
    "host": _host,
    "agency": _nullable(_presented(1024)),
    "request_id": _strict(128, _REQUEST_ID_RE, "a request id"),
    "request_url": _nullable(_url()),
    "doc_id": _nullable(_strict(256)),  # the platform's document id; integers as decimal strings
    "filename": _presented(4096),  # as the platform presents it, before any local renaming
    "title": _nullable(_presented(65536)),
    "url": _nullable(_url()),  # the stable URL (strip_signing_params); never a signed redirect
    "released_on": _nullable(_date),
}
_MD5_MULTIPART_SPEC = {  # the source's multipart ETag form, at the part size that reproduced it
    "part_size": _int(MIN_PART_SIZE, MAX_PART_SIZE),
    "etag": _regex(_ETAG_MULTIPART_RE, "an md5-N multipart ETag"),
}
_UPLOAD_SPEC = {  # how the library sent the bytes to staging
    "method": _enum(UPLOAD_METHODS),
    "part_size": _nullable(_int(MIN_PART_SIZE, MAX_PART_SIZE)),
    "part_sha256": _nullable(_list_of(_hex(64), MAX_PARTS, 1)),
}
_DATA_SPEC = {
    "size": _int(0, MAX_OBJECT_SIZE),
    "sha256": _hex(64),  # the claim S3 verifies on the evidence write
    "md5": _hex(32),  # checked by S3 for a single PUT (the staging ETag); the library's own for multipart
    "md5_multipart": _nullable(_obj(_MD5_MULTIPART_SPEC)),
    "staging_etag": _regex(_ETAG_VALUE_RE, "a quoted S3 ETag"),
    "upload": _obj(_UPLOAD_SPEC),
}
_FETCH_SPEC = {
    "origin": _enum(ORIGINS),
    "fetch_id": _uuid4,  # one per upload call; a retry of the same fetch keeps it
    "run_id": _nullable(_strict(128)),  # one per connector run; joins a manifest to its files' records
    "work_id": _nullable(_strict(256)),  # the queue item this upload serves
    "attempt": _int(1, 1000),
    "connector": _strict(64, _SLUG_RE, "a connector slug"),
    "connector_version": _nullable(_strict(64)),
    "library_version": _strict(64),
    "access": _enum(ACCESS),  # anonymous, or logged in as the requester
    "started_at": _nullable(_timestamp),
    "first_byte_at": _nullable(_timestamp),
    "completed_at": _timestamp,
    "retries": _int(0, 100_000),
    "ci_run": _nullable(_strict(256)),
    "approval": _nullable(_strict(256)),  # who approved a file over COST_GATE, and where
    "legacy_path": _nullable(_strict(4096)),  # the old local or repo path (origin local-copy or git)
    "original_fetched_at": _nullable(_timestamp),
    "git_commit": _nullable(_hex(40)),
    "stamp_ref": _nullable(_hex(64)),  # sha256 of a stamped manifest that already lists this file
}
_REDIRECT_SPEC = {"status": _int(300, 399), "url": _url(allow_query=False)}  # url_without_query form
_RESPONSE_SPEC = {  # the final response that delivered the bytes
    "status": _int(100, 599),
    "headers": _check_headers,  # sanitize_headers form
    "redirects": _list_of(_obj(_REDIRECT_SPEC), MAX_REDIRECTS),
    "final_url": _nullable(_url(allow_query=False)),  # where the bytes came from, url_without_query form
}
_LISTING_SPEC = {  # the listing entry as the source showed it, to compare with what came
    "size": _nullable(_int(0, MAX_OBSERVED_SIZE)),
    "date": _nullable(_presented(256)),
    "title": _nullable(_presented(65536)),
}
_CHECKS_SPEC = {  # what the library verified before committing the upload
    "declared_length": _nullable(_int(0, MAX_OBSERVED_SIZE)),
    "length": _enum(LENGTH_CHECKS),
    "eof": _enum(EOF_CHECKS),
    "etag": _enum(ETAG_CHECKS),
    "content_md5": _enum(CONTENT_MD5_CHECKS),
    "sniffed_type": _enum(SNIFF_TYPES),
    "expect_types": _nullable(_list_of(_enum(SNIFF_TYPES), len(SNIFF_TYPES), 1)),
}
_SIDECAR_SPEC = {
    "schema": _in_set(READABLE_SCHEMAS),
    "uuid": _uuid4,
    "content_kind": _enum(CONTENT_KINDS),
    "data": _obj(_DATA_SPEC),
    "source": lambda v, f: _check_source(v, f),
    "fetch": _obj(_FETCH_SPEC),
    "response": _nullable(_obj(_RESPONSE_SPEC)),
    "listing": _nullable(_obj(_LISTING_SPEC)),
    "checks": _obj(_CHECKS_SPEC),
}


def _require_schema(obj, field, reason):
    if type(obj) is not dict:
        _fail(field, "not an object", reason)
    if type(obj.get("schema")) is not int or obj["schema"] not in READABLE_SCHEMAS:
        _fail(f"{field}.schema", "a schema version this code can't read", "schema_version")


def _check_source(src, field):
    _check_obj(src, field, _SOURCE_SPEC)
    if (src["kind"] == "muckrock") != (src["platform"] == "muckrock"):
        _fail(f"{field}.platform", "muckrock kind and platform go together")
    if src["request_url"] is not None and urlsplit(src["request_url"]).hostname != src["host"]:
        _fail(f"{field}.host", "not the request_url's host")
    return src


def _content_md5_headers(headers):
    return [headers[n] for n in ("content-md5", "x-ms-blob-content-md5") if n in headers]


def validate_sidecar(obj):
    """Check a parsed sidecar; return it unchanged or raise SchemaError.
    Checks shape and formats, and that the fields agree with each other: the
    size and parts, the origin, and every check the writer claims against
    the response it recorded."""
    _require_schema(obj, "sidecar", "bad_sidecar")
    _check_obj(obj, "sidecar", _SIDECAR_SPEC)
    data, src, fetch, checks, resp = obj["data"], obj["source"], obj["fetch"], obj["checks"], obj["response"]
    size, up, origin = data["size"], data["upload"], fetch["origin"]

    if size > COST_GATE and fetch["approval"] is None:
        _fail("sidecar.data.size", "over the cost gate without an approval", "too_large")

    etag_body = data["staging_etag"][1:-1]
    if up["method"] == "put":
        if up["part_size"] is not None or up["part_sha256"] is not None:
            _fail("sidecar.data.upload", "a single PUT has no parts")
        if size > SINGLE_PUT_MAX:
            _fail("sidecar.data.upload.method", "too large for one PUT")
        if etag_body != data["md5"]:  # staging is SSE-S3
            _fail("sidecar.data.staging_etag", "a single PUT's ETag is the file's MD5")
    else:
        if up["part_size"] is None or up["part_sha256"] is None:
            _fail("sidecar.data.upload", "multipart needs part_size and part_sha256")
        if size == 0:
            _fail("sidecar.data.upload.method", "an empty file is one PUT")
        if len(up["part_sha256"]) != part_count(size, up["part_size"]):
            _fail("sidecar.data.upload.part_sha256", "part count doesn't match size / part_size")
        if not etag_body.endswith(f"-{len(up['part_sha256'])}"):
            _fail("sidecar.data.staging_etag", "a multipart ETag ends in its part count")
    mp = data["md5_multipart"]
    if mp is not None:
        if size == 0:
            _fail("sidecar.data.md5_multipart", "an empty file has no parts")
        if int(mp["etag"].split("-")[1]) != part_count(size, mp["part_size"]):
            _fail("sidecar.data.md5_multipart", "part count doesn't match size / part_size")

    if (obj["content_kind"] == "fetch_manifest") != (origin == "generated"):
        _fail("sidecar.fetch.origin", "a fetch manifest, and only one, is generated")
    backfill = origin in ("local-copy", "git")
    if backfill != (fetch["legacy_path"] is not None):
        _fail("sidecar.fetch.legacy_path", "set exactly for local copies and git")
    if (origin == "git") != (fetch["git_commit"] is not None):
        _fail("sidecar.fetch.git_commit", "set exactly when origin is git")
    if not backfill and (fetch["original_fetched_at"] is not None or fetch["stamp_ref"] is not None):
        _fail("sidecar.fetch.original_fetched_at", "only for local copies and git")
    if origin == "live":
        if src["url"] is None:
            _fail("sidecar.source.url", "a live fetch needs its URL")
        if resp is None:
            _fail("sidecar.response", "a live fetch records its response")
        if not 200 <= resp["status"] <= 299 or resp["status"] == 206:
            _fail("sidecar.response.status", "not a complete 2xx response")
        if "content-range" in resp["headers"]:
            _fail("sidecar.response.headers", "a partial response")
    elif resp is not None:
        _fail("sidecar.response", "only a live fetch has a response")
    if origin == "generated":
        if src["url"] is not None or obj["listing"] is not None or src["filename"] != MANIFEST_FILENAME:
            _fail("sidecar.source", "a generated manifest has no url or listing and a fixed filename")
        if src["doc_id"] is not None or src["title"] is not None or src["released_on"] is not None:
            _fail("sidecar.source", "a generated manifest names no document")

    stamps = [(n, fetch[n]) for n in ("started_at", "first_byte_at", "completed_at") if fetch[n] is not None]
    for (n1, t1), (n2, t2) in zip(stamps, stamps[1:]):
        if parse_timestamp(t1) > parse_timestamp(t2):
            _fail(f"sidecar.fetch.{n2}", f"earlier than {n1}")

    headers = resp["headers"] if resp is not None else {}
    encoded = headers.get("content-encoding", "identity").strip().lower() not in ("", "identity")
    if checks["length"] == "ok":
        if checks["declared_length"] != size:
            _fail("sidecar.checks.declared_length", "doesn't equal data.size")
        if "content-length" in headers and not encoded and headers["content-length"].strip() != str(size):
            _fail("sidecar.checks.length", "the Content-Length header disagrees")
    else:
        if checks["declared_length"] is not None:
            _fail("sidecar.checks.declared_length", "set while length is undeclared")
        if "content-length" in headers and not encoded:
            _fail("sidecar.checks.length", "undeclared, but the response has a Content-Length")
    if checks["expect_types"] is not None and checks["sniffed_type"] not in checks["expect_types"]:
        _fail("sidecar.checks.sniffed_type", "not one of expect_types")
    if (size == 0) != (checks["sniffed_type"] == "empty"):
        _fail("sidecar.checks.sniffed_type", "empty exactly when size is 0")

    etag_check = checks["etag"]
    form, body = etag_form(headers.get("etag"))
    if (etag_check == "absent") != ("etag" not in headers):
        _fail("sidecar.checks.etag", "absent exactly when the response has no ETag")
    if etag_check == "opaque" and form != "opaque":
        _fail("sidecar.checks.etag", "the ETag has an MD5 shape")
    if etag_check == "md5" and (form != "md5" or body != data["md5"]):
        _fail("sidecar.checks.etag", "the ETag isn't this file's MD5")
    if etag_check == "md5-multipart" and (form != "md5-multipart" or mp is None or body != mp["etag"]):
        _fail("sidecar.checks.etag", "the ETag isn't this file's recorded multipart MD5")
    if etag_check == "unmatched":
        if form == "opaque":
            _fail("sidecar.checks.etag", "an opaque ETag can't be unmatched")
        if body == data["md5"] or (mp is not None and body == mp["etag"]):
            _fail("sidecar.checks.etag", "the ETag matches")
    md5_values = _content_md5_headers(headers)
    own = bytes.fromhex(data["md5"])
    agree = [content_md5_digest(v) == own for v in md5_values]
    expected = "absent" if not md5_values else "match" if all(agree) else "mismatch"
    if checks["content_md5"] != expected:
        _fail("sidecar.checks.content_md5", f"the Content-MD5 headers say {expected}")
    return obj


def check_sidecar_key(sidecar, key):
    """The sidecar was read from its own key, in/<its uuid>.json."""
    u, kind = parse_staging_key(key)
    if kind != "sidecar" or u != sidecar["uuid"]:
        _fail("sidecar.uuid", "not the uuid in the key it was read from")
    return sidecar


def parse_sidecar(data, key=None):
    """Bytes of in/<uuid>.json to a validated sidecar; with `key`, also check
    the sidecar belongs to it."""
    obj = parse_strict_json(data, max_bytes=MAX_SIDECAR_BYTES, field="sidecar")
    validate_sidecar(obj)
    return check_sidecar_key(obj, key) if key is not None else obj


def sidecar_bytes(obj):
    """Validate a sidecar and serialize it the one way it may be stored."""
    raw = canonical_json(validate_sidecar(obj))
    if len(raw) > MAX_SIDECAR_BYTES:
        _fail("sidecar", "too large", "bad_sidecar")
    return raw


# --- The intake record ------------------------------------------------------------

CHECKSUM_TYPES = ("FULL_OBJECT", "COMPOSITE")
LOCK_MODES = ("GOVERNANCE", "COMPLIANCE")

_EVIDENCE_SPEC = {
    "bucket": _strict(63),
    "key": _strict(128),
    "version_id": _strict(1024),
    "checksum_type": _enum(CHECKSUM_TYPES),
    "checksum_sha256": _regex(_B64_SHA256_RE, "an S3 ChecksumSHA256"),  # as HeadObject reports it
    "part_size": _nullable(_int(MIN_PART_SIZE, MAX_PART_SIZE)),
    "part_sha256": _nullable(_list_of(_hex(64), MAX_PARTS, 1)),
    "lock_mode": _enum(LOCK_MODES),
    "retain_until": _timestamp,
}
_STAGING_SPEC = {
    "bucket": _strict(63),
    "data_key": _strict(64),
    "sidecar_key": _strict(64),
    "data_etag": _regex(_ETAG_VALUE_RE, "a quoted S3 ETag"),
    "data_last_modified": _timestamp,
    "data_sse": _enum((STAGING_SSE,)),
    "data_checksum_type": _enum(CHECKSUM_TYPES),
    "data_checksum_sha256": _regex(_B64_SHA256_RE, "an S3 ChecksumSHA256"),
    "sidecar_sha256": _hex(64),  # of the sidecar's stored (canonical) bytes
}
_INGEST_SPEC = {
    "deriver": _int(1, DERIVER_VERSION),  # readers accept every deriver so far
    "code_sha256": _nullable(_hex(64)),  # the deployed Lambda zip
    "principal": _nullable(_strict(256)),  # who uploaded, from the S3 event
}
_RECORD_SPEC = {
    "schema": _in_set(READABLE_SCHEMAS),
    "uuid": _uuid4,
    "sha256": _hex(64),
    "size": _int(0, MAX_OBJECT_SIZE),
    "evidence": _obj(_EVIDENCE_SPEC),
    "staging": _obj(_STAGING_SPEC),
    "sidecar": lambda v, f: v,  # checked by validate_sidecar below
    "ingest": _obj(_INGEST_SPEC),
}


def validate_record(obj):
    """Check an intake record; return it unchanged or raise SchemaError.

    Beyond shape, it enforces what the record exists to state: the blob key
    is the sha256; up to SINGLE_PUT_MAX the blob carries S3's full-object
    SHA-256 of exactly that sha256, and above it the staging object's own
    parts, so its composite equals the staging composite; the staging objects
    are the sidecar's; and the buckets are one environment's staging and
    evidence buckets. A record holds no clock or request id: two writes for
    one sighting differ at most in `ingest` (code and trigger path), which
    record_core leaves out."""
    _require_schema(obj, "record", "bad_sidecar")
    _check_obj(obj, "record", _RECORD_SPEC)
    sidecar = obj["sidecar"]
    try:
        validate_sidecar(sidecar)
    except SchemaError as e:
        raise SchemaError(e.reason, "record." + e.field, e.problem) from None
    u, sha, size = obj["uuid"], obj["sha256"], obj["size"]
    ev, st, data = obj["evidence"], obj["staging"], sidecar["data"]
    up = data["upload"]

    if sidecar["uuid"] != u:
        _fail("record.uuid", "differs from the sidecar's")
    if data["sha256"] != sha or data["size"] != size:
        _fail("record.sha256", "differs from the sidecar's claim")
    try:
        ev_env, ev_role, ev_acct, ev_region = parse_bucket_name(ev["bucket"])
        st_env, st_role, st_acct, st_region = parse_bucket_name(st["bucket"])
    except SchemaError:
        _fail("record.evidence.bucket", "not intake bucket names")
    if ev_role != "evidence" or st_role != "staging":
        _fail("record.evidence.bucket", "not an evidence and a staging bucket")
    if (ev_env, ev_acct, ev_region) != (st_env, st_acct, st_region):
        _fail("record.staging.bucket", "not the same environment as the evidence bucket")
    if ev["key"] != blob_key(sha):
        _fail("record.evidence.key", "not sha256/<the record's sha256>")

    if up["method"] == "put":
        staging_sum = ("FULL_OBJECT", sha256_b64(sha))
    else:
        staging_sum = ("COMPOSITE", composite_sha256(up["part_sha256"]))
    if (st["data_checksum_type"], st["data_checksum_sha256"]) != staging_sum:
        _fail("record.staging.data_checksum_sha256", "doesn't match how the sidecar says it was uploaded")
    if size <= SINGLE_PUT_MAX:
        if ev["checksum_type"] != "FULL_OBJECT" or ev["checksum_sha256"] != sha256_b64(sha):
            _fail("record.evidence.checksum_sha256", "not S3's full-object SHA-256 of sha256")
        if ev["part_size"] is not None or ev["part_sha256"] is not None:
            _fail("record.evidence.part_sha256", "a single-PUT blob has no parts")
    else:
        if ev["checksum_type"] != "COMPOSITE":
            _fail("record.evidence.checksum_type", "a blob over SINGLE_PUT_MAX is a composite")
        if ev["part_size"] != up["part_size"] or ev["part_sha256"] != up["part_sha256"]:
            _fail("record.evidence.part_sha256", "not copied on the staging object's parts")
        if ev["checksum_sha256"] != staging_sum[1]:
            _fail("record.evidence.checksum_sha256", "not the staging object's composite")

    if st["data_key"] != staging_data_key(u) or st["sidecar_key"] != staging_sidecar_key(u):
        _fail("record.staging", "keys don't belong to this uuid")
    if st["data_etag"] != data["staging_etag"]:
        _fail("record.staging.data_etag", "not the ETag the sidecar names")
    if st["sidecar_sha256"] != hashlib.sha256(canonical_json(sidecar)).hexdigest():
        _fail("record.staging.sidecar_sha256", "not the hash of the sidecar's canonical bytes")
    if parse_timestamp(ev["retain_until"]) <= parse_timestamp(st["data_last_modified"]):
        _fail("record.evidence.retain_until", "not after the upload")
    return obj


def build_record(sidecar, *, staging, evidence, ingest):
    """Assemble and validate the record the Lambda writes for one sighting.
    Inputs are copied, so later changes to them can't reach the record."""
    sidecar, staging, evidence, ingest = copy.deepcopy((sidecar, staging, evidence, ingest))
    if type(ingest) is not dict or ingest.get("deriver") != DERIVER_VERSION:
        _fail("record.ingest.deriver", "a new record uses the current deriver")
    record = {
        "schema": SCHEMA_VERSION,
        "uuid": sidecar["uuid"],
        "sha256": sidecar["data"]["sha256"],
        "size": sidecar["data"]["size"],
        "evidence": evidence,
        "staging": staging,
        "sidecar": sidecar,
        "ingest": ingest,
    }
    return validate_record(record)


def record_core(record):
    """The fields that must agree when two writers race for one record key and
    the second gets a 412: what was stored, and from which staging bytes. It
    reads the raw fields, so it works on records of any schema version."""
    return {
        "uuid": record["uuid"],
        "sha256": record["sha256"],
        "size": record["size"],
        "evidence_key": record["evidence"]["key"],
        "evidence_version_id": record["evidence"]["version_id"],
        "staging_data_etag": record["staging"]["data_etag"],
        "staging_sidecar_sha256": record["staging"]["sidecar_sha256"],
    }


def record_bytes(record):
    return canonical_json(validate_record(record))


def parse_record(data):
    obj = parse_strict_json(data, max_bytes=MAX_RECORD_BYTES, field="record")
    return validate_record(obj)


# --- The fetch manifest -----------------------------------------------------------

# What one fetch run found for one request, stored as a blob like any file
# (content_kind fetch_manifest, origin generated). The body describes the
# request, not the run: no clock, no run id, and "held" whether this run
# downloaded a file or recognized it as unchanged. So an unchanged listing
# serializes to the same bytes and is stored once; each later run only adds
# a record, whose sidecar carries the run (fetch.run_id also marks the file
# records that run wrote).
MANIFEST_STATUSES = ("held", "failed", "needs_approval")

_MANIFEST_SOURCE_SPEC = {k: _SOURCE_SPEC[k] for k in ("kind", "platform", "host", "agency", "request_id", "request_url")}
_MANIFEST_ENTRY_SPEC = {
    "filename": _presented(4096),
    "doc_id": _nullable(_strict(256)),
    "url": _nullable(_url()),
    "sha256": _nullable(_hex(64)),
    "size": _nullable(_int(0, MAX_OBSERVED_SIZE)),
    "status": _enum(MANIFEST_STATUSES),
    "reason": _nullable(_regex(_REASON_RE, "a reason code")),
}
_MANIFEST_SPEC = {
    "schema": _in_set(READABLE_SCHEMAS),
    "kind": _enum(("fetch_manifest",)),
    "source": _obj(_MANIFEST_SOURCE_SPEC),
    "files_listed": _nullable(_int(0, MAX_MANIFEST_FILES)),  # what the listing said; may differ from files
    "files": _list_of(_obj(_MANIFEST_ENTRY_SPEC), MAX_MANIFEST_FILES),
}


def manifest_sort_key(entry):
    """Typed, so the order never depends on how a value prints."""
    return tuple((0, "") if entry[k] is None else (1, entry[k])
                 for k in ("filename", "doc_id", "url", "sha256", "status", "reason", "size"))


def manifest_entry(filename, *, status, sha256=None, size=None, doc_id=None, url=None, reason=None):
    """One file of a fetch manifest. A held file names its sha256 (for one this
    run recognized as unchanged: the last one seen); a failed or
    needs_approval file has no sha256 and says why."""
    return {"filename": filename, "doc_id": doc_id, "url": url, "sha256": sha256,
            "size": size, "status": status, "reason": reason}


def validate_manifest(obj):
    _require_schema(obj, "manifest", "bad_manifest")
    _check_obj(obj, "manifest", _MANIFEST_SPEC)
    src = obj["source"]
    if (src["kind"] == "muckrock") != (src["platform"] == "muckrock"):
        _fail("manifest.source.platform", "muckrock kind and platform go together")
    if src["request_url"] is not None and urlsplit(src["request_url"]).hostname != src["host"]:
        _fail("manifest.source.host", "not the request_url's host")
    files = obj["files"]
    for i, e in enumerate(files):
        f = f"manifest.files[{i}]"
        if e["status"] == "held":
            if e["sha256"] is None or e["size"] is None:
                _fail(f, "a held file names its sha256 and size")
            if e["reason"] is not None:
                _fail(f"{f}.reason", "only for files not held")
        else:
            if e["sha256"] is not None:
                _fail(f"{f}.sha256", "a file not held has no sha256")
            if e["reason"] is None:
                _fail(f"{f}.reason", "a file not held says why")
    keys = [manifest_sort_key(e) for e in files]
    if keys != sorted(keys):
        _fail("manifest.files", "not in canonical order")
    if len(set(keys)) != len(keys):
        _fail("manifest.files", "duplicate entry")
    return obj


def build_manifest(source, files, files_listed=None):
    """A validated manifest: inputs copied, identical entries collapsed (a
    listing can repeat a file), files in canonical order."""
    source, files = copy.deepcopy((source, list(files)))
    unique = {}
    for e in files:
        unique.setdefault(json.dumps(e, sort_keys=True, default=str), e)
    manifest = {
        "schema": SCHEMA_VERSION,
        "kind": "fetch_manifest",
        "source": source,
        "files_listed": files_listed,
        "files": list(unique.values()),
    }
    _check_obj(manifest, "manifest", _MANIFEST_SPEC)  # type-check before sorting
    manifest["files"].sort(key=manifest_sort_key)
    return validate_manifest(manifest)


def manifest_bytes(manifest):
    return canonical_json(validate_manifest(manifest))


def parse_manifest(data):
    obj = parse_strict_json(data, max_bytes=MAX_MANIFEST_BYTES, field="manifest", reason="bad_manifest")
    try:
        return validate_manifest(obj)
    except SchemaError as e:
        reason = e.reason if e.reason in ("schema_version", "signed_url") else "bad_manifest"
        raise SchemaError(reason, e.field, e.problem) from None


def validate_manifest_sidecar(sidecar, manifest):
    """The sidecar that carried a fetch manifest describes the same request."""
    if sidecar["content_kind"] != "fetch_manifest":
        _fail("sidecar.content_kind", "not a fetch manifest", "manifest_mismatch")
    for k in _MANIFEST_SOURCE_SPEC:
        if sidecar["source"][k] != manifest["source"][k]:
            _fail(f"sidecar.source.{k}", "differs from the manifest's", "manifest_mismatch")
    return sidecar


def manifest_shas(manifest):
    """The sha256s a manifest says evidence holds; the Lambda checks each exists."""
    return sorted({e["sha256"] for e in manifest["files"] if e["sha256"] is not None})
