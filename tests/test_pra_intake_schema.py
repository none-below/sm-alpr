"""Tests for scripts/pra_intake/schema.py, the intake contract.

Everything this module lays out is permanent: blobs and intake records sit in
a write-once, 10-year Object Lock bucket. So:
  - tests/fixtures/pra_intake/v1/ holds frozen schema-1 documents that must
    parse forever as stored documents; they are never regenerated;
  - vocabulary.json pins the constants and limits a v1 reader depends on;
  - the checksum formulas are pinned to values S3 itself returned in live
    probes on 2026-09-28, not to this code.
"""

import base64
import copy
import enum
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from pra_intake import schema as s  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "pra_intake"
V1 = FIXTURES / "v1"
ACCOUNT, REGION = "123456789012", "us-west-2"  # AWS's documented example account
U = "0b6b1c4e-3f7a-4c1d-9e2f-5a6b7c8d9e0f"
U2 = "2b6b1c4e-3f7a-4c1d-9e2f-5a6b7c8d9e0f"
FETCH = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
PII = "SENTINEL-Jane-Doe"  # must never appear in an error message
RLO, ZWSP, TAG_A, ARABIC_ONE = chr(0x202E), chr(0x200B), chr(0xE0041), chr(0x661)
E_ACUTE, GRIN, CJK = chr(0xE9), chr(0x1F600), chr(0x4E2D)
STG = s.bucket_name("staging", "prod", ACCOUNT, REGION)
EVD = s.bucket_name("evidence", "prod", ACCOUNT, REGION)
APPROVAL = s.approval_key(U2)


def blob(n, seed=0):
    """n deterministic, non-repeating bytes (the live probes used the same generator)."""
    out, i = bytearray(), seed * 1_000_000
    while len(out) < n:
        out += hashlib.sha256(i.to_bytes(8, "big")).digest()
        i += 1
    return bytes(out[:n])


def s3_multipart_etag(data, part_size):
    md5s = [hashlib.md5(data[i:i + part_size]).digest() for i in range(0, len(data), part_size)]
    return f"{hashlib.md5(b''.join(md5s)).hexdigest()}-{len(md5s)}"


def b64(d):
    return base64.b64encode(d).decode()


CSV = b"case,plate,reason\n1,XYZ,investigation\n"


def make_sidecar(data=CSV, *, part_size=None, origin="live"):
    """A consistent sidecar for `data`, as the library would write it."""
    sha, md5 = hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest()
    if part_size:
        parts = [hashlib.sha256(data[i:i + part_size]).hexdigest() for i in range(0, len(data), part_size)]
        upload = {"method": "multipart", "part_size": part_size, "part_sha256": parts}
        staging_etag = f'"{s3_multipart_etag(data, part_size)}"'
    else:
        upload = {"method": "put", "part_size": None, "part_sha256": None}
        staging_etag = f'"{md5}"'
    live = origin == "live"
    backfill = origin in ("local-copy", "git")
    return {
        "schema": 1,
        "uuid": U,
        "content_kind": "file",
        "data": {
            "size": len(data), "sha256": sha, "md5": md5,
            "md5_multipart": ({"part_size": s.MUCKROCK_ETAG_PART_SIZE,
                               "etag": s3_multipart_etag(data, s.MUCKROCK_ETAG_PART_SIZE)} if data else None),
            "staging_etag": staging_etag, "upload": upload,
        },
        "source": {
            "kind": "muckrock", "platform": "muckrock", "host": "www.muckrock.com",
            "agency": "San Mateo Police Department", "request_id": "12345",
            "request_url": "https://www.muckrock.com/foi/san-mateo-12345/",
            "doc_id": "987654", "filename": "a.csv", "title": "Audit log",
            "url": "https://cdn.muckrock.com/foia_files/2026/09/28/a.csv",
            "released_on": "2026-09-27",
        },
        "fetch": {
            "origin": origin, "fetch_id": FETCH, "run_id": "run-1", "work_id": None, "attempt": 1,
            "connector": "muckrock", "connector_version": "abc1234", "library_version": "1",
            "access": "anonymous",
            "started_at": "2026-09-28T18:00:00Z", "first_byte_at": "2026-09-28T18:00:00.5Z",
            "completed_at": "2026-09-28T18:00:01.25Z", "retries": 0, "ci_run": None, "approval": None,
            "legacy_path": {"local-copy": "107949-roseville-police-department/a.csv",
                            "git": "assets/public-records/a.csv"}.get(origin),
            "original_fetched_at": "2026-09-20T01:02:03Z" if backfill else None,
            "git_commit": "0" * 40 if origin == "git" else None,
            "stamp_ref": "5" * 64 if origin == "local-copy" else None,
        },
        "response": {
            "status": 200,
            "headers": {"etag": f'"{md5}"', "content-length": str(len(data)),
                        "content-type": "text/csv", "date": "Mon, 28 Sep 2026 18:00:00 GMT"},
            "redirects": [{"status": 302, "url": "https://www.muckrock.com/foi/files/987654/"}],
            "final_url": "https://cdn.muckrock.com/foia_files/2026/09/28/a.csv",
        } if live else None,
        "listing": {"size": len(data), "date": "2026-09-27", "title": "Audit log"},
        "checks": {
            "declared_length": len(data) if live else None, "length": "ok" if live else "undeclared",
            "eof": "clean", "etag": "md5" if live else "absent", "content_md5": "absent",
            "sniffed_type": s.sniff_type(data), "expect_types": None,
        },
    }


def make_big_sidecar(size=s.SINGLE_PUT_MAX + 1, *, approval=None):
    """A multipart sidecar of `size` bytes with synthetic part digests (nothing that size is hashed)."""
    parts = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(s.part_count(size, s.PART_SIZE))]
    sc = make_sidecar()
    sc["data"].update(size=size, md5_multipart=None, staging_etag=f'"{"0" * 32}-{len(parts)}"',
                      upload={"method": "multipart", "part_size": s.PART_SIZE, "part_sha256": parts})
    sc["checks"].update(declared_length=size, etag="opaque")
    sc["response"]["headers"].update({"content-length": str(size), "etag": '"0x8DCDEADBEEF1234"'})
    sc["fetch"]["approval"] = approval
    return sc


MANIFEST_SOURCE = {"kind": "muckrock", "platform": "muckrock", "host": "www.muckrock.com",
                   "agency": "San Mateo", "request_id": "12345",
                   "request_url": "https://www.muckrock.com/foi/san-mateo-12345/"}


def manifest_files():
    return [
        s.manifest_entry("b.txt", status="held", sha256="bb" * 32, size=2, doc_id="2"),
        s.manifest_entry("a.txt", status="held", sha256="aa" * 32, size=1, doc_id="1",
                         url="https://cdn.muckrock.com/foia_files/a.txt"),
        s.manifest_entry("c.pdf", status="failed", reason="source_404", doc_id="3"),
        s.manifest_entry("big.zip", status="needs_approval", size=60_000_000_000, reason="too_large"),
    ]


def make_manifest():
    return s.build_manifest(MANIFEST_SOURCE, manifest_files(), files_listed=4)


def make_manifest_sidecar(manifest=None):
    body = s.manifest_bytes(manifest or make_manifest())
    sc = make_sidecar(body, origin="generated")
    sc["content_kind"] = "fetch_manifest"
    sc["source"] = {**MANIFEST_SOURCE, "doc_id": None, "filename": s.MANIFEST_FILENAME, "title": None,
                    "url": None, "released_on": None}
    sc["listing"] = None
    return sc


def with_(obj, path, value):
    """A deep copy of obj with one dotted path set; value DELETE removes the key."""
    obj = copy.deepcopy(obj)
    *head, last = path.split(".")
    node = obj
    for k in head:
        node = node[int(k)] if isinstance(node, list) else node[k]
    if value is DELETE:
        del node[last]
    else:
        node[last] = value
    return obj


DELETE = object()


def rejects(fn, obj, reason, field_prefix):
    with pytest.raises(s.SchemaError) as e:
        fn(obj)
    assert e.value.reason == reason, e.value
    assert e.value.field.startswith(field_prefix), e.value
    return e.value


def evidence_for(sc, *, version_id="v1", lock_mode="GOVERNANCE"):
    d = sc["data"]
    size, sha, up = d["size"], d["sha256"], d["upload"]
    common = {"bucket": EVD, "key": s.blob_key(sha), "version_id": version_id,
              "lock_mode": lock_mode, "retain_until": "2036-09-28T18:00:03Z"}
    if size <= s.SINGLE_PUT_MAX:
        return {**common, "checksum_type": "FULL_OBJECT", "checksum_sha256": s.sha256_b64(sha),
                "part_size": None, "part_sha256": None}
    return {**common, "checksum_type": "COMPOSITE", "checksum_sha256": s.composite_sha256(up["part_sha256"]),
            "part_size": up["part_size"], "part_sha256": list(up["part_sha256"])}


def staging_for(sc):
    d, up = sc["data"], sc["data"]["upload"]
    st_type, st_sum = (("FULL_OBJECT", s.sha256_b64(d["sha256"])) if up["method"] == "put"
                       else ("COMPOSITE", s.composite_sha256(up["part_sha256"])))
    return {"bucket": STG, "data_key": s.staging_data_key(sc["uuid"]),
            "sidecar_key": s.staging_sidecar_key(sc["uuid"]), "data_etag": d["staging_etag"],
            "data_last_modified": "2026-09-28T18:00:02Z", "data_sse": "AES256",
            "data_checksum_type": st_type, "data_checksum_sha256": st_sum,
            "sidecar_sha256": hashlib.sha256(s.canonical_json(sc)).hexdigest()}


INGEST = {"deriver": 1, "code_sha256": "c" * 64, "principal": "AROAEXAMPLE:gha-123-1"}


def make_record(sc=None, *, version_id="v1", ingest=INGEST, lock_mode="GOVERNANCE"):
    sc = sc or make_sidecar()
    return s.build_record(sc, staging=staging_for(sc),
                          evidence=evidence_for(sc, version_id=version_id, lock_mode=lock_mode), ingest=ingest)


def reseal(record):
    """A record with its sidecar_sha256 recomputed after editing the sidecar."""
    record = copy.deepcopy(record)
    record["staging"]["sidecar_sha256"] = hashlib.sha256(s.canonical_json(record["sidecar"])).hexdigest()
    return record


# --- Values S3 returned in the live probes (2026-09-28) ---------------------------------


def test_checksums_match_what_s3_returned():
    small = blob(1000, 1)
    assert f'"{hashlib.md5(small).hexdigest()}"' == '"a92c87a4d13fb1d0f7a0bb85bb8c8f9d"'  # single PUT ETag
    assert s.sha256_b64(hashlib.sha256(small).hexdigest()) == "GfmSpRaMSIanjG9MMIsog5crNmxX5/nZxgr4ca4bhHE="
    big, ps = blob(11 * s.MiB, 2), 5 * s.MiB
    parts = [hashlib.sha256(big[i:i + ps]).hexdigest() for i in range(0, len(big), ps)]
    assert s.composite_sha256(parts) == "U2/+zodUSQAVJzurveGCe/nMVcbOLrNfv83n5DIxdHI=-3"  # HeadObject form
    md5s = [hashlib.md5(big[i:i + ps]).hexdigest() for i in range(0, len(big), ps)]
    assert s.md5_multipart_etag(md5s) == "803300097a27ff0da6ed704e711c26f6-3"  # multipart ETag
    assert s.sha256_b64(hashlib.sha256(big).hexdigest()) == "e6H4vS9AZl3lwQjsta2o9TTHdfG9GGFxHtL96AUkrfE="
    other = [hashlib.sha256(big[lo:hi]).hexdigest() for lo, hi in ((0, 7 * s.MiB), (7 * s.MiB, 11 * s.MiB))]
    assert s.composite_sha256(other) == "5RwJ2aD+LE5Sajw21qm/bKuMKad7ufvuahKrnuCR7C4=-2"  # UploadPartCopy


# --- Bucket names ------------------------------------------------------------------


def test_bucket_names_for_every_role_and_env():
    names = {(env, role): s.bucket_name(role, env, ACCOUNT, REGION)
             for env in s.ENV_PREFIXES for role in s.BUCKET_ROLES}
    assert len(set(names.values())) == len(names)
    retired = f"{s.RETIRED_BUCKET_PREFIX}-{ACCOUNT}-{REGION}-an"
    for (env, role), name in names.items():
        assert name != retired and not name.startswith(s.RETIRED_BUCKET_PREFIX + "-")
        assert len(name) <= 63 and name.isascii() and name == name.lower()
        assert s.parse_bucket_name(name) == (env, role, ACCOUNT, REGION)


@pytest.mark.parametrize("args", [
    ("staging", "stage", ACCOUNT, REGION), ("pra", "prod", ACCOUNT, REGION),
    ("staging", "prod", "12345678901", REGION), ("staging", "prod", int(ACCOUNT), REGION),
    ("staging", "prod", ARABIC_ONE * 12, REGION), ("staging", "prod", ACCOUNT, "US-WEST-2"),
    ("staging", "prod", ACCOUNT, "us-west"), ("staging", "prod", ACCOUNT, "us-west-" + ARABIC_ONE),
    ("evidence", "dev", ACCOUNT, "ap-southeast-verylongregionname-1"),
])
def test_bucket_name_rejects_bad_input(args):
    with pytest.raises(ValueError):
        s.bucket_name(*args)


@pytest.mark.parametrize("name", [
    f"sm-alpr-pra-{ACCOUNT}-{REGION}-an", f"sm-alpr-other-{ACCOUNT}-{REGION}-an",
    f"sm-alpr-staging-{ACCOUNT}-{REGION}", "Evidence", None, f"sm-alpr-staging-{ACCOUNT}-{REGION}-an\n",
    f"sm-alpr-dev-evidence-{ACCOUNT}-ap-southeast-verylongregionname-1-an",
])
def test_parse_bucket_name_rejects(name):
    with pytest.raises(s.SchemaError):
        s.parse_bucket_name(name)


# --- Keys, tags, metadata --------------------------------------------------------------


def test_keys_round_trip():
    sha = "ab" * 32
    assert s.staging_data_key(U) == f"in/{U}.bin" and s.staging_sidecar_key(U) == f"in/{U}.json"
    assert s.parse_staging_key(f"in/{U}.bin") == (U, "data")
    assert s.parse_staging_key(f"in/{U}.json") == (U, "sidecar")
    assert s.blob_key(sha) == f"sha256/{sha}" and s.parse_blob_key(f"sha256/{sha}") == sha
    assert s.record_key(U) == f"_intake/{U}.json" and s.parse_record_key(f"_intake/{U}.json") == U
    assert s.errata_key(U, 3) == f"_errata/{U}/0003.json" and s.errata_key(U, 9999).endswith("/9999.json")
    assert s.approval_key(U) == f"approvals/{U}.json"


def test_new_uuid_is_uuid4_and_fresh():
    a, b = s.new_uuid(), s.new_uuid()
    assert s.is_uuid4(a) and s.is_uuid4(b) and a != b


@pytest.mark.parametrize("key", [
    f"in/{U.upper()}.bin", "in/0b6b1c4e-3f7a-1c1d-9e2f-5a6b7c8d9e0f.bin",
    "in/0b6b1c4e-3f7a-4c1d-7e2f-5a6b7c8d9e0f.json", f"in/{U}.bin.json", f"in/{U}", f"out/{U}.bin",
    f"in/x/{U}.bin", f"x/in/{U}.bin", f"in/{U}.JSON", f"in%2F{U}.bin", "", None,
])
def test_parse_staging_key_rejects(key):
    with pytest.raises(s.SchemaError):
        s.parse_staging_key(key)


@pytest.mark.parametrize("fn,key", [
    (s.parse_blob_key, "sha256/" + "AB" * 32), (s.parse_blob_key, "sha256/" + "ab" * 31),
    (s.parse_blob_key, "sha1/" + "ab" * 32), (s.parse_blob_key, "sha256/" + "ab" * 32 + "junk"),
    (s.parse_blob_key, "sha256/" + "ab" * 32 + "\n"), (s.parse_blob_key, "sha256/" + "ab" * 32 + "/x"),
    (s.parse_record_key, f"_intake/{U}.JSON"), (s.parse_record_key, f"_intake/{U.upper()}.json"),
    (s.parse_record_key, f"intake/{U}.json"),
])
def test_key_parsers_reject(fn, key):
    with pytest.raises(s.SchemaError):
        fn(key)


@pytest.mark.parametrize("bad", ["AB" * 32, "ab" * 31, "ab" * 32 + "0", "zz" * 32, None])
def test_blob_key_needs_lowercase_sha256(bad):
    with pytest.raises(s.SchemaError):
        s.blob_key(bad)


def test_key_builders_need_uuid4():
    for fn in (s.staging_data_key, s.staging_sidecar_key, s.record_key, s.approval_key):
        with pytest.raises(s.SchemaError):
            fn(U.upper())


def test_errata_numbers_are_bounded():
    for n in (0, 10000, True, "1"):
        with pytest.raises(ValueError):
            s.errata_key(U, n)


def test_tags():
    assert s.ingested_tags("ab" * 32) == {"ingested": "true", "sha256": "ab" * 32}
    assert s.INGESTED_TAG == ("ingested", "true")
    assert s.rejected_tags("sha_mismatch") == {"intake": "rejected", "reason": "sha_mismatch"}
    with pytest.raises(ValueError):
        s.rejected_tags("nope")
    with pytest.raises(s.SchemaError):
        s.ingested_tags("AB" * 32)
    for reason in s.REJECT_REASONS:  # S3 tag values: letters, digits, spaces and _ . : / = + - @
        assert len(reason) <= 256 and all(c.isalnum() or c in "_.:/=+-@ " for c in reason)


def test_staging_metadata_is_small_ascii_and_checked():
    sc = make_sidecar()
    meta = s.staging_metadata(sc["source"], U, FETCH)
    assert meta == {"schema": "1", "uuid": U, "fetch-id": FETCH, "source-kind": "muckrock",
                    "platform": "muckrock", "host": "www.muckrock.com", "request-id": "12345"}
    assert all(k.isascii() and v.isascii() and k == k.lower() for k, v in meta.items())
    assert sum(len(k) + len(v) for k, v in meta.items()) < 2048
    assert s.check_staging_metadata(sc, meta) is sc
    rejects(lambda m: s.check_staging_metadata(sc, m), {**meta, "uuid": U2}, "data_mismatch", "staging.metadata")
    with pytest.raises(s.SchemaError):
        s.staging_metadata(with_(sc["source"], "kind", "portal"), U, FETCH)
    with pytest.raises(s.SchemaError):
        s.staging_metadata(sc["source"], U, "not-a-uuid")


def test_doc_id_text():
    assert s.doc_id_text(12345) == "12345" and s.doc_id_text("A-9") == "A-9"
    for bad in (1.0, True, None):
        with pytest.raises(s.SchemaError):
            s.doc_id_text(bad)


# --- Canonical JSON and strict parsing -------------------------------------------------


def test_canonical_json_is_sorted_ascii_compact_with_newline():
    a = s.canonical_json({"b": 1, "a": [E_ACUTE, None, True, GRIN]})
    b = s.canonical_json(dict(reversed(list({"b": 1, "a": [E_ACUTE, None, True, GRIN]}.items()))))
    assert a == b == b'{"a":["\\u00e9",null,true,"\\ud83d\\ude00"],"b":1}\n'
    with pytest.raises(ValueError):
        s.canonical_json({"x": float("nan")})


@pytest.mark.parametrize("raw", [
    b'{"a":1,"a":2}\n', b'{"a":NaN}\n', b'{"a":Infinity}\n', b'{"a":1.0}\n', b'{"a":1e400}\n',
    b"\xef\xbb\xbf{}\n", b"{\xff}\n", b"[" * 17 + b"]" * 17 + b"\n", b"[" * 100_000 + b"]" * 100_000,
    b"{", b"", b'{"a": 1}\n', b'{"b":1,"a":2}\n', b'{"a":1}', b'{"a":"' + E_ACUTE.encode() + b'"}\n',
    b'"' + b'\\"' * 50_000,
])
def test_parse_strict_json_rejects(raw):
    with pytest.raises(s.SchemaError) as e:
        s.parse_strict_json(raw, max_bytes=10_000_000)
    assert e.value.reason == "bad_sidecar"


def test_parse_strict_json_accepts_canonical_and_enforces_size():
    assert s.parse_strict_json(b'{"a":[1,{"b":null}]}\n', max_bytes=100) == {"a": [1, {"b": None}]}
    assert s.parse_strict_json(b"[" * 16 + b"]" * 16 + b"\n", max_bytes=100)
    with pytest.raises(s.SchemaError):
        s.parse_strict_json(b'{"a":"' + b"x" * 100 + b'"}\n', max_bytes=50)
    assert s.parse_strict_json(b'{"a": 1}', max_bytes=100, canonical=False) == {"a": 1}
    with pytest.raises(TypeError):
        s.parse_strict_json('{"a":1}\n', max_bytes=100)


# --- Timestamps ------------------------------------------------------------------------


def test_format_timestamp():
    t = datetime(2026, 9, 28, 18, 5, 19, 500000, tzinfo=timezone.utc)
    assert s.format_timestamp(t) == "2026-09-28T18:05:19.5Z"
    assert s.format_timestamp(t.replace(microsecond=0)) == "2026-09-28T18:05:19Z"
    assert s.format_timestamp(t.replace(microsecond=1)) == "2026-09-28T18:05:19.000001Z"
    assert s.format_timestamp(datetime(987, 1, 2, tzinfo=timezone.utc)) == "0987-01-02T00:00:00Z"
    pdt = timezone(timedelta(hours=-7))
    assert s.format_timestamp(datetime(2026, 9, 28, 11, 5, 19, tzinfo=pdt)) == "2026-09-28T18:05:19Z"
    with pytest.raises(ValueError):
        s.format_timestamp(datetime(2026, 9, 28))
    assert s.parse_timestamp("2026-09-28T18:05:19.5Z") == t


@pytest.mark.parametrize("ts", [
    "2026-09-28T18:05:19.50Z", "2026-09-28T18:05:19.0Z", "2026-09-28T18:05:19.Z", "2026-09-28T18:05:19+00:00",
    "2026-09-28 18:05:19Z", "2026-09-28T24:00:00Z", "2026-09-28T23:59:60Z", "0000-01-01T00:00:00Z",
    "2026-02-30T00:00:00Z", "2026-09-28T18:05:19.1234567Z", "2026-09-28T18:05:1" + ARABIC_ONE + "Z",
])
def test_timestamps_have_one_spelling(ts):
    with pytest.raises(s.SchemaError):
        s.parse_timestamp(ts)


# --- Checksums and ETags --------------------------------------------------------------


def test_composite_sha256_matches_independent_computation():
    data, ps = blob(11 * s.MiB, 5), 5 * s.MiB
    digests = [hashlib.sha256(data[i:i + ps]).digest() for i in range(0, len(data), ps)]
    assert s.composite_sha256([d.hex() for d in digests]) == b64(hashlib.sha256(b"".join(digests)).digest()) + "-3"
    one = s.composite_sha256([hashlib.sha256(data).hexdigest()])
    assert one.endswith("-1") and one[:-2] != s.sha256_b64(hashlib.sha256(data).hexdigest())
    for fn in (s.composite_sha256, s.md5_multipart_etag):
        with pytest.raises(ValueError):
            fn([])


@pytest.mark.parametrize("size,part_size,n", [
    (0, 5 * s.MiB, 1), (1, 5 * s.MiB, 1), (5 * s.MiB, 5 * s.MiB, 1), (5 * s.MiB + 1, 5 * s.MiB, 2),
    (10 * s.MiB, 5 * s.MiB, 2), (10 * s.MiB + 1, 5 * s.MiB, 3),
])
def test_part_count(size, part_size, n):
    assert s.part_count(size, part_size) == n


@pytest.mark.parametrize("etag,expected", [
    ('"d41d8cd98f00b204e9800998ecf8427e"', ("md5", "d41d8cd98f00b204e9800998ecf8427e")),
    ("D41D8CD98F00B204E9800998ECF8427E", ("md5", "d41d8cd98f00b204e9800998ecf8427e")),
    ('"9b2cf535f27731c974343645a3985328-12"', ("md5-multipart", "9b2cf535f27731c974343645a3985328-12")),
    ('W/"d41d8cd98f00b204e9800998ecf8427e"', ("opaque", None)), ('"0x8DCDEADBEEF1234"', ("opaque", None)),
    ('"d41d8cd98f00b204e9800998ecf8427e-0"', ("opaque", None)), (None, ("opaque", None)),
])
def test_etag_form(etag, expected):
    assert s.etag_form(etag) == expected


def test_content_md5_digest_reads_base64_and_hex():
    d = hashlib.md5(b"x").digest()
    assert s.content_md5_digest(b64(d)) == d and s.content_md5_digest(d.hex().upper()) == d
    for bad in ("not base64!", b64(b"short"), "", None):
        assert s.content_md5_digest(bad) is None


# --- Sniffing ------------------------------------------------------------------------


@pytest.mark.parametrize("head,expected", [
    (b"", "empty"), (b"%PDF-1.7\n", "pdf"), (b"\x00" * 1000 + b"%PDF-1.4", "pdf"), (b" " * 1019 + b"%PDF-", "pdf"),
    (b"\x00" * 1020 + b"%PDF-1.4", "unknown"),  # past the 1024-byte window
    (b"PK\x03\x04" + b"\x00" * 20, "zip"), (b"PK\x05\x06" + b"\x00" * 18, "zip"), (b"PK\x07\x08" + b"\x00" * 20, "zip"),
    (bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 8, "ole2"), (b"\x1f\x8b\x08\x00", "gzip"),
    (bytes.fromhex("377abcaf271c0004"), "7z"), (b"Rar!\x1a\x07\x00", "rar"), (bytes.fromhex("fd377a585a0000"), "xz"),
    (b"BZh91AY", "bzip2"), (b"SQLite format 3\x00", "sqlite"),
    (b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff\xe0", "jpeg"), (b"GIF89a", "gif"),
    (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"), (b"BM\x36\x00\x0c\x00\x00\x00\x00\x00", "bmp"),
    (b"BMW,plate\n1,ABC\n", "text"), (b"\x00\x00\x00\x18ftypmp42", "isobmff"), (b"RIFF\x24\x00\x00\x00WAVE", "riff"),
    (b"ID3\x04\x00", "mp3"), (bytes.fromhex("3026b2758e66cf11a6d9"), "asf"), (bytes.fromhex("1a45dfa3a3"), "matroska"),
    (b"OggS\x00", "ogg"), (b"fLaC\x00", "flac"), (b"{\\rtf1\\ansi", "rtf"),
    (b"\xef\xbb\xbf\n  <!DOCTYPE HTML>\n<html>", "html"), (b"<html><body>Session expired</body></html>", "html"),
    (b"<head><title>x</title>", "html"), (b"<body>", "html"),
    (b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">', "html"),
    (b'<?xml version="1.0"?><root/>', "xml"), (b"case,plate\r\n1,ABC\r\n", "text"), (b"a\tb\n1\t2\n", "text"),
    (("caf" + E_ACUTE + ";r" + E_ACUTE + "sum" + E_ACUTE + "\n").encode("cp1252"), "text"),
    (b"\xff\xfe" + "a,b\r\n1,2\r\n".encode("utf-16-le"), "text"),
    (b"\xfe\xff" + "<html><body>x".encode("utf-16-be"), "html"),
    (b"\x01\x02\x03binary", "unknown"), (b"text then \x00 a NUL", "unknown"),
])
def test_sniff_type(head, expected):
    assert s.sniff_type(head) == expected
    assert expected in s.SNIFF_TYPES


def test_display_safe_shows_invisible_characters():
    assert s.display_safe(f"a{RLO}b{ZWSP}c\n") == "a<U+202E>b<U+200B>c<U+000A>"
    assert s.display_safe(f"x{TAG_A}{chr(0x85)}{chr(0x2066)}{chr(0xFE0F)}") == "x<U+E0041><U+0085><U+2066><U+FE0F>"


# --- URLs ------------------------------------------------------------------------------

URL_OK = "https://cdn.muckrock.com/foia_files/2026/09/28/a.csv"


@pytest.mark.parametrize("param", sorted(s.SECRET_PARAMS))
def test_every_secret_param_is_refused(param):
    for url in (f"https://portal.example.gov/a.pdf?{param}=x", f"https://portal.example.gov/a.pdf?id=1&{param.upper()}=x",
                f"https://portal.example.gov/a.pdf?id=1;{param}=x"):
        rejects(s.validate_sidecar, with_(make_sidecar(), "source.url", url), "signed_url", "sidecar.source.url")
    assert s.is_secret_param(param) and s.is_secret_param(param.upper())


@pytest.mark.parametrize("name", ["accessToken", "access_token", "Access-Token", "session_id", "idToken",
                                  "clientSecret", "API_KEY", "X-Goog-Signature", "x-amz-date", "__token__"])
def test_secret_param_spellings(name):
    assert s.is_secret_param(name)
    rejects(s.validate_sidecar, with_(make_sidecar(), "source.url", f"https://p.example.gov/a?{name}=x"),
            "signed_url", "sidecar.source.url")


@pytest.mark.parametrize("param", sorted(s.COMPANION_PARAMS))
def test_companion_params_alone_are_ordinary(param):
    url = f"https://portal.example.gov/list?page=2&{param}=10"
    assert s.is_companion_param(param) and not s.is_secret_param(param)
    assert s.validate_sidecar(with_(make_sidecar(), "source.url", url))
    assert s.strip_signing_params(url) == url
    assert s.strip_signing_params(url + "&sig=abc") == "https://portal.example.gov/list?page=2"


@pytest.mark.parametrize("url", [
    "https://bucket.s3.amazonaws.com/a.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=ab",
    "https://bucket.s3.amazonaws.com/a.pdf?%58-Amz-Signature=ab",  # an encoded name
    "https://storage.googleapis.com/b/a.pdf?X-Goog-Signature=ab",
    "https://d1.cloudfront.net/a.pdf?Expires=1&Signature=x&Key-Pair-Id=K",
    "https://acct.blob.core.windows.net/c/a.pdf?sv=2021-08-06&se=2026-01-01&sr=b&sp=r&sig=abc",
    "https://sanmateo.govqa.us/WEBAPP/(S(abcdef123))/rs/Doc.aspx?rid=5",
    "https://sanmateo.govqa.us/WEBAPP/(F(ticket))/rs/Doc.aspx",
    "https://sanmateo.govqa.us/WEBAPP/(A(anon))/rs/Doc.aspx",
    "https://sanmateo.govqa.us/WEBAPP/(X(1)S(abc)F(def))/rs/Doc.aspx",
    "https://sanmateo.govqa.us/WEBAPP/%28S%28abc%29%29/rs/Doc.aspx",
    "https://portal.example.gov/Doc.aspx?rid=5&sSessionID=123",
    "https://portal.example.gov/doc;jsessionid=ABC?id=1",
    "https://user:pw@portal.example.gov/a.pdf",
    "https://portal.example.gov/go?u=https%3A%2F%2Fb.s3.amazonaws.com%2Fa%3FX-Amz-Signature%3D1",
    "https://portal.example.gov/go?u=https%253A%252F%252Fh.example.gov%252Fa%253Fsig%253D1",
    "https://tenant.sharepoint.com/_layouts/15/download.aspx?UniqueId=x&tempauth=abc",
    "https://bucket.s3.amazonaws.com/a.pdf?AWSAccessKeyId=AKIA&Expires=1&Signature=x",
    "https://onedrive.live.com/download?cid=1&authkey=abc",
    "https://portal.example.gov/dl/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig",
    "https://portal.example.gov/a?k=AKIAIOSFODNN7EXAMPLE",
])
def test_signed_or_session_urls_are_refused(url):
    rejects(s.validate_sidecar, with_(make_sidecar(), "source.url", url), "signed_url", "sidecar.source.url")


@pytest.mark.parametrize("url", [
    "ftp://cdn.muckrock.com/a.csv", "https://cdn.muckrock.com/a.csv#p2", "https://cdn.muckrock.com/a b.csv",
    "https://cdn.muckrock.com/r" + E_ACUTE + "sum" + E_ACUTE + ".csv", "https://CDN.muckrock.com/a.csv",
    "HTTPS://cdn.muckrock.com/a.csv", "https://10.0.0.1/a.csv", "https://0x7f.0x1/a.csv", "https://[::1]/a.csv",
    "https://cdn.muckrock.com:0/a.csv", "https://cdn.muckrock.com:99999/a.csv", "https://cdn.muckrock.com:443/a.csv",
    "http://cdn.muckrock.com:80/a.csv", "https://localhost/a.csv", "https:///a.csv", "https://cdn.muckrock.com/a.csv#",
    "https://cdn.muckrock.com", "https://cdn.muckrock.com/a.csv?",
])
def test_malformed_urls_are_refused(url):
    rejects(s.validate_sidecar, with_(make_sidecar(), "source.url", url), "invalid_metadata", "sidecar.source.url")


@pytest.mark.parametrize("url", [
    "https://sanmateo.govqa.us/WEBAPP/_rs/Doc.aspx?rid=5", "https://cityofmodestoca.nextrequest.com/documents/123?page=2",
    "https://portal.example.gov:8443/a.pdf", "http://portal.example.gov/a.pdf", "https://legacy_bucket.s3.amazonaws.com/a.pdf",
    "https://portal.example.gov/a%20b.pdf?q=a%20b", "https://portal.example.gov/list?page=2&st=10&sr=name",
    "https://portal.example.gov/a?q=a;b", "https://portal.example.gov/a?x=1&&y=2", "https://portal.example.gov/",
])
def test_stable_urls_are_accepted(url):
    assert s.validate_sidecar(with_(make_sidecar(), "source.url", url))


def test_strip_signing_params():
    assert (s.strip_signing_params("https://b.s3.amazonaws.com/a.pdf?rid=5&X-Amz-Signature=1&sig=2&page=3#frag")
            == "https://b.s3.amazonaws.com/a.pdf?rid=5&page=3")
    for kept in ("https://p.example.gov/a?q=a%20b+c&z=%2F", "https://p.example.gov/a?n=caf%E9",
                 "https://p.example.gov/a?q=a;b", "https://p.example.gov/a?x=1&&y=2", "https://p.example.gov/a?download"):
        assert s.strip_signing_params(kept) == kept  # nothing to drop: byte for byte
    assert s.strip_signing_params("https://p.example.gov/a?id=1;token=x;v=2") == "https://p.example.gov/a?id=1;v=2"
    assert s.strip_signing_params("https://p.example.gov/a?token=x") == "https://p.example.gov/a"
    assert s.strip_signing_params("HTTPS://user:pw@P.Example.GOV:8443/a") == "https://p.example.gov:8443/a"
    assert s.strip_signing_params("https://p.example.gov:443") == "https://p.example.gov/"
    govqa = s.strip_signing_params("https://SanMateo.GovQA.us/WEBAPP/_rs/(X(1)S(abc)F(t))/Doc.aspx?rid=5&sSessionID=9")
    assert govqa == "https://sanmateo.govqa.us/WEBAPP/_rs/Doc.aspx?rid=5"
    assert s.strip_signing_params("https://h.example.gov/app/%28S%28abc%29%29/Doc.aspx") == "https://h.example.gov/app/Doc.aspx"
    assert s.strip_signing_params("https://h.example.gov\\user:pw@evil.example.gov/x").startswith("https://")
    assert s.strip_path_session("/app;jsessionid=AB12/x") == "/app/x" and s.strip_path_session("/(S(abc))") == "/"
    for bad in ("https://p.example.gov/dl/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.x", "ftp://p.example.gov/a",
                "https://p.example.gov:99999/a", "https://p.example.gov/a b"):
        with pytest.raises(s.SchemaError):
            s.strip_signing_params(bad)


def test_url_without_query_and_redirects():
    assert s.url_without_query("HTTPS://u:p@B.example.gov:8443/(S(x))/a?q=1#f") == "https://b.example.gov:8443/a"
    assert s.url_without_query("/rs/(S(x))/Doc.aspx?rid=5") == "/rs/Doc.aspx"
    assert s.url_without_query("Doc.aspx?rid=5") == ""
    assert s.url_without_query("//h.example.gov/a?x=1") == "//h.example.gov/a"
    assert s.url_without_query("/\\user:pass@evil.example.gov/x") == "//evil.example.gov/x"
    assert s.url_without_query("http://h.example.gov:99999/a") == ""
    assert s.url_without_query("https://[::1]:8443/a") == "https://[::1]:8443/a"
    assert s.redirect_url("https://h.example.gov/a/b?t=1", "../c?x=1") == "https://h.example.gov/c"
    assert s.redirect_url("https://h.example.gov/a", "https://cdn.example.gov/f.pdf?sig=1") == "https://cdn.example.gov/f.pdf"


# --- Response headers --------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "set-cookie", "cookie", "authorization", "proxy-authorization", "x-amz-security-token", "x-api-key",
    "x-csrf-token", "x-auth-token", "access-token", "x-subject-token", "x-amzn-remapped-authorization",
    "x-requestverificationtoken", "x-amz-meta-owner", "www-authenticate", "link",
])
def test_headers_outside_the_allowlist_are_refused(name):
    e = rejects(s.validate_sidecar, with_(make_sidecar(), f"response.headers.{name}", "x"),
                "forbidden_header", "sidecar.response.headers")
    assert name not in str(e)
    rejects(s.validate_sidecar, with_(make_sidecar(), "response.headers", {"ETag": "x"}), "invalid_metadata",
            "sidecar.response.headers")


@pytest.mark.parametrize("name,value", [
    ("location", "https://b.s3.amazonaws.com/a.pdf?X-Amz-Signature=1"), ("location", "https://b.s3.amazonaws.com/a.pdf?x=1"),
    ("content-location", "/rs/(S(abc))/Doc.aspx"), ("content-location", "/a?x=1"),
    ("location", "https://user:pw@b.example.gov/a"), ("location", "//u:p@evil.example.gov/a"),
    ("via", "https://b.example.gov/x?sig=1"),
    ("content-disposition", 'attachment; filename="a.pdf"; x=https://h.example.gov/?tempauth=1'),
])
def test_credentials_in_allowed_headers_are_refused(name, value):
    rejects(s.validate_sidecar, with_(make_sidecar(), f"response.headers.{name}", value),
            "signed_url", "sidecar.response.headers")


def test_header_values_are_bounded_and_single_line():
    rejects(s.validate_sidecar, with_(make_sidecar(), "response.headers.server", "a\x01b"), "invalid_metadata",
            "sidecar.response.headers")
    rejects(s.validate_sidecar, with_(make_sidecar(), "response.headers.server", "a\nb"), "invalid_metadata",
            "sidecar.response.headers")
    rejects(s.validate_sidecar, with_(make_sidecar(), "response.headers.server", "x" * 8193), "invalid_metadata",
            "sidecar.response.headers")
    rejects(s.validate_sidecar, with_(make_sidecar(), "response.headers.server", 5), "invalid_metadata",
            "sidecar.response.headers")
    assert s.validate_sidecar(with_(make_sidecar(), "response.headers.server", "a\tb" + E_ACUTE + chr(0x85)))


def test_sanitize_headers():
    utf8_name = ("r" + E_ACUTE + "sum" + E_ACUTE + ".pdf").encode("utf-8").decode("latin-1")  # a client's view of raw UTF-8
    got = s.sanitize_headers([
        ("Set-Cookie", "session=1"), ("ETag", '"abc"'), ("Via", "1.1 a"), ("via", "1.1 b"), ("VIA", "1.1 a"),
        ("Location", "https://u:p@B.s3.amazonaws.com/(S(x))/a.pdf?X-Amz-Signature=1#f"),
        ("Content-Disposition", f'attachment; filename="{utf8_name}"'),
        ("X-Amz-Request-Id", "a\r\n b"), ("Server", "x" * 9000), ("Authorization", "Bearer t"),
        ("X-Powered-By", "https://h.example.gov/?sig=1"), (b"Content-Type", b"text/csv"),
        ("Content-Length", "38"), ("content-length", "38"),
    ])
    assert got == {"etag": '"abc"', "via": "1.1 a, 1.1 b", "location": "https://b.s3.amazonaws.com/a.pdf",
                   "content-disposition": 'attachment; filename="r' + E_ACUTE + "sum" + E_ACUTE + '.pdf"',
                   "x-amz-request-id": "a   b", "server": s.OMITTED_LONG, "x-powered-by": s.OMITTED_CREDENTIAL,
                   "content-type": "text/csv", "content-length": "38"}
    assert s.validate_sidecar(with_(make_sidecar(), "response.headers", {**got, "etag": make_sidecar()["response"]["headers"]["etag"]}))


def test_sanitize_headers_output_always_validates():
    cases = [
        [("Via", "x" * 5000), ("via", "y" * 5000)],
        [("Location", "http://h.example.gov:99999/a?x=1")], [("Location", b"\xff\xfe/\x00weird")],
        [("Content-Disposition", "a\x01b\x7fc")], [("etag", "")],
        [("Server", "a\ud800b")], [("Via", "x\udc92y")], [("Server", "\x00" * 10)],
        [(n, GRIN * 2048) for n in sorted(s.ALLOWED_HEADERS)],  # every name at the cap: the total gives
        [(n, chr(0x85) * 4000) for n in sorted(s.ALLOWED_HEADERS)],
    ]
    for pairs in cases:
        out = s.sanitize_headers(pairs)
        s._check_headers(out, "h")
        assert sum(s._json_len(v) for v in out.values()) <= s.MAX_HEADERS_BYTES


# --- The sidecar ---------------------------------------------------------------------


@pytest.mark.parametrize("make", [
    lambda: make_sidecar(),
    lambda: with_(make_sidecar(b""), "response.headers.etag", f'"{hashlib.md5(b"").hexdigest()}"'),
    lambda: make_sidecar(blob(11 * s.MiB), part_size=5 * s.MiB),
    lambda: make_sidecar(blob(10 * s.MiB), part_size=5 * s.MiB),
    lambda: make_sidecar(blob(20 * s.MiB)),  # any S3-legal layout: a 20 MiB single PUT
    lambda: make_sidecar(CSV, part_size=5 * s.MiB),  # a one-part multipart upload
    lambda: make_sidecar(origin="local-copy"),
    lambda: make_sidecar(origin="git"),
    lambda: with_(make_sidecar(), "fetch.access", "requester"),
    lambda: make_manifest_sidecar(),
    lambda: make_big_sidecar(),
])
def test_valid_sidecars(make):
    sc = make()
    assert s.validate_sidecar(sc) is sc
    raw = s.sidecar_bytes(sc)
    assert s.parse_sidecar(raw, key=s.staging_sidecar_key(sc["uuid"])) == sc


def test_presented_text_is_stored_as_is():
    for value in (f"a{RLO}fdp.exe", f"line\nbreak{ZWSP}.csv", f"x{TAG_A}.pdf", E_ACUTE * 100, "\x00weird", CJK * 1365):
        sc = with_(with_(make_sidecar(), "source.filename", value), "source.title", value)
        sc = with_(with_(sc, "source.agency", value[:300]), "listing.title", value)
        assert s.parse_sidecar(s.sidecar_bytes(sc), key=s.staging_sidecar_key(U))["source"]["filename"] == value
    assert s.validate_sidecar(with_(make_sidecar(), "source.filename", "x" * 4096))
    assert s.validate_sidecar(with_(make_sidecar(), "source.filename", E_ACUTE * 2048))
    rejects(s.validate_sidecar, with_(make_sidecar(), "source.filename", E_ACUTE * 2049), "invalid_metadata",
            "sidecar.source.filename")


SIDECAR_MUTATIONS = [
    # (path, value, reason, field prefix)
    ("schema", 2, "schema_version", "sidecar.schema"), ("schema", True, "schema_version", "sidecar.schema"),
    ("schema", "1", "schema_version", "sidecar.schema"), ("extra", 1, "invalid_metadata", "sidecar.<unknown>"),
    ("data.upload.method", DELETE, "invalid_metadata", "sidecar.data.upload.method"),
    ("fetch.work_id", DELETE, "invalid_metadata", "sidecar.fetch.work_id"),
    ("uuid", U.upper(), "invalid_metadata", "sidecar.uuid"),
    ("data.sha256", "AB" * 32, "invalid_metadata", "sidecar.data.sha256"),
    ("data.size", -1, "invalid_metadata", "sidecar.data.size"), ("data.size", True, "invalid_metadata", "sidecar.data.size"),
    ("data.staging_etag", '"' + "0" * 32 + '"', "invalid_metadata", "sidecar.data.staging_etag"),
    ("data.md5_multipart", "0" * 32 + "-1", "invalid_metadata", "sidecar.data.md5_multipart"),
    ("source.kind", "portal", "invalid_metadata", "sidecar.source.platform"),
    ("source.platform", "govqa", "invalid_metadata", "sidecar.source.platform"),
    ("source.host", "cdn.muckrock.com", "invalid_metadata", "sidecar.source.host"),
    ("source.host", "WWW.muckrock.com", "invalid_metadata", "sidecar.source.host"),
    ("source.host", "localhost", "invalid_metadata", "sidecar.source.host"),
    ("source.request_id", "12 345", "invalid_metadata", "sidecar.source.request_id"),
    ("source.request_id", "12345" + ZWSP, "invalid_metadata", "sidecar.source.request_id"),
    ("source.doc_id", 987654, "invalid_metadata", "sidecar.source.doc_id"),
    ("source.filename", "", "invalid_metadata", "sidecar.source.filename"),
    ("source.filename", "x" * 4097, "invalid_metadata", "sidecar.source.filename"),
    ("source.filename", "\ud800.csv", "invalid_metadata", "sidecar.source.filename"),
    ("source.url", None, "invalid_metadata", "sidecar.source.url"),
    ("source.released_on", "2026-02-30", "invalid_metadata", "sidecar.source.released_on"),
    ("fetch.completed_at", "2026-09-28T17:59:59Z", "invalid_metadata", "sidecar.fetch.completed_at"),
    ("fetch.first_byte_at", "2026-09-28T18:00:02Z", "invalid_metadata", "sidecar.fetch.completed_at"),
    ("fetch.started_at", "2026-09-28T18:00:00.6Z", "invalid_metadata", "sidecar.fetch.first_byte_at"),
    ("fetch.connector", "MuckRock", "invalid_metadata", "sidecar.fetch.connector"),
    ("fetch.attempt", 0, "invalid_metadata", "sidecar.fetch.attempt"),
    ("fetch.git_commit", "0" * 40, "invalid_metadata", "sidecar.fetch.git_commit"),
    ("fetch.legacy_path", "old/a.csv", "invalid_metadata", "sidecar.fetch.legacy_path"),
    ("fetch.stamp_ref", "5" * 64, "invalid_metadata", "sidecar.fetch.original_fetched_at"),
    ("fetch.original_fetched_at", "2026-09-20T01:02:03Z", "invalid_metadata", "sidecar.fetch.original_fetched_at"),
    ("fetch.run_id", None, "invalid_metadata", "sidecar.fetch.run_id"),
    ("fetch.run_id", "run?token=abc", "signed_url", "sidecar.fetch.run_id"),
    ("fetch.run_id", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.x", "signed_url", "sidecar.fetch.run_id"),
    ("fetch.ci_run", "https://ci.example.gov/r?sig=1", "signed_url", "sidecar.fetch.ci_run"),
    ("fetch.work_id", "w" + chr(0x85), "invalid_metadata", "sidecar.fetch.work_id"),
    ("fetch.approval", APPROVAL, "invalid_metadata", "sidecar.fetch.approval"),
    ("fetch.approval", "issue 12", "invalid_metadata", "sidecar.fetch.approval"),
    ("fetch.origin", "generated", "invalid_metadata", "sidecar.fetch.origin"),
    ("content_kind", "fetch_manifest", "invalid_metadata", "sidecar.fetch.origin"),
    ("response", None, "invalid_metadata", "sidecar.response"),
    ("response.status", 206, "invalid_metadata", "sidecar.response.status"),
    ("response.status", 204, "invalid_metadata", "sidecar.response.status"),
    ("response.status", 404, "invalid_metadata", "sidecar.response.status"),
    ("response.status", 99, "invalid_metadata", "sidecar.response.status"),
    ("response.final_url", None, "invalid_metadata", "sidecar.response.final_url"),
    ("response.headers.content-range", "bytes 0-9/100", "invalid_metadata", "sidecar.response.headers"),
    ("response.redirects.0.url", "https://www.muckrock.com/foi/files/9/?x=1", "invalid_metadata",
     "sidecar.response.redirects[0].url"),
    ("response.redirects.0.status", 200, "invalid_metadata", "sidecar.response.redirects[0].status"),
    ("response.redirects.0.status", 300, "invalid_metadata", "sidecar.response.redirects"),
    ("response.final_url", "https://cdn.muckrock.com/a.csv?X-Amz-Signature=1", "signed_url", "sidecar.response.final_url"),
    ("checks.length", "undeclared", "invalid_metadata", "sidecar.checks.declared_length"),
    ("checks.declared_length", 1, "invalid_metadata", "sidecar.checks.declared_length"),
    ("response.headers.content-length", "1", "invalid_metadata", "sidecar.checks.length"),
    ("checks.etag", "absent", "invalid_metadata", "sidecar.checks.etag"),
    ("checks.etag", "opaque", "invalid_metadata", "sidecar.checks.etag"),
    ("checks.etag", "unmatched", "invalid_metadata", "sidecar.checks.etag"),
    ("checks.etag", "md5-multipart", "invalid_metadata", "sidecar.checks.etag"),
    ("response.headers.etag", '"' + "f" * 32 + '"', "invalid_metadata", "sidecar.checks.etag"),
    ("checks.content_md5", "match", "invalid_metadata", "sidecar.checks.content_md5"),
    ("checks.content_md5", "mismatch", "invalid_metadata", "sidecar.checks.content_md5"),
    ("response.headers.content-md5", "AAAAAAAAAAAAAAAAAAAAAA==", "invalid_metadata", "sidecar.checks.content_md5"),
    ("checks.sniffed_type", "empty", "invalid_metadata", "sidecar.checks.sniffed_type"),
    ("checks.expect_types", ["pdf"], "invalid_metadata", "sidecar.checks.sniffed_type"),
    ("checks.expect_types", [], "invalid_metadata", "sidecar.checks.expect_types"),
    ("checks.sniffed_type", "exe", "invalid_metadata", "sidecar.checks.sniffed_type"),
]


@pytest.mark.parametrize("path,value,reason,field", SIDECAR_MUTATIONS)
def test_sidecar_rules(path, value, reason, field):
    rejects(s.validate_sidecar, with_(make_sidecar(), path, value), reason, field)


def test_strict_fields_refuse_every_invisible_character():
    for c in (0x85, 0x9F, 0xAD, 0x61C, 0x2028, 0x200F, 0x2066, 0xFE0F, 0xFEFF, 0xE0041, 0xFFFE):
        rejects(s.validate_sidecar, with_(make_sidecar(), "source.doc_id", "9" + chr(c)), "invalid_metadata",
                "sidecar.source.doc_id")
    assert s.validate_sidecar(with_(make_sidecar(), "fetch.work_id", "x" * 256))
    rejects(s.validate_sidecar, with_(make_sidecar(), "fetch.work_id", "x" * 257), "invalid_metadata", "sidecar.fetch.work_id")


def test_live_length_rules():
    ok = make_sidecar()
    und = with_(with_(ok, "checks.length", "undeclared"), "checks.declared_length", None)
    rejects(s.validate_sidecar, und, "invalid_metadata", "sidecar.checks.length")  # the response has a Content-Length
    no_len = {k: v for k, v in ok["response"]["headers"].items() if k != "content-length"}
    assert s.validate_sidecar(with_(und, "response.headers", no_len))
    assert s.validate_sidecar(with_(und, "response.headers.content-encoding", "gzip"))  # counts other bytes
    assert s.validate_sidecar(with_(und, "response.headers.transfer-encoding", "chunked"))
    for same in ("38, 38", "0038", " 38 "):
        assert s.validate_sidecar(with_(ok, "response.headers.content-length", same))
    assert s.validate_sidecar(with_(ok, "response.headers.content-range", "bytes 0-37/38"))  # the whole file
    rejects(s.validate_sidecar, with_(ok, "response.headers.content-range", "bytes 0-36/38"), "invalid_metadata",
            "sidecar.response.headers")
    assert s.validate_sidecar(with_(ok, "response.status", 203))


def test_origin_rules_hold_in_both_directions():
    ok = make_sidecar()
    assert s.validate_sidecar(with_(ok, "fetch.first_byte_at", ok["fetch"]["started_at"]))  # equal times
    empty = with_(make_sidecar(b""), "response.headers.etag", f'"{hashlib.md5(b"").hexdigest()}"')
    rejects(s.validate_sidecar, with_(empty, "checks.sniffed_type", "text"), "invalid_metadata", "sidecar.checks.sniffed_type")
    lc = make_sidecar(origin="local-copy")
    rejects(s.validate_sidecar, with_(lc, "fetch.legacy_path", None), "invalid_metadata", "sidecar.fetch.legacy_path")
    rejects(s.validate_sidecar, with_(lc, "response", ok["response"]), "invalid_metadata", "sidecar.response")
    rejects(s.validate_sidecar, with_(lc, "fetch.stamp_ref", lc["data"]["sha256"]), "invalid_metadata", "sidecar.fetch.stamp_ref")
    assert s.validate_sidecar(with_(lc, "fetch.run_id", None))  # a backfill needn't belong to a run
    git = make_sidecar(origin="git")
    rejects(s.validate_sidecar, with_(git, "fetch.git_commit", None), "invalid_metadata", "sidecar.fetch.git_commit")
    rejects(s.validate_sidecar, with_(git, "fetch.legacy_path", None), "invalid_metadata", "sidecar.fetch.legacy_path")


def test_etag_and_content_md5_claims_are_checked_by_value():
    data = blob(12 * s.MiB)
    sc = make_sidecar(data, part_size=s.PART_SIZE)
    mp = sc["data"]["md5_multipart"]["etag"]
    ok = with_(with_(sc, "response.headers.etag", f'"{mp}"'), "checks.etag", "md5-multipart")
    assert s.validate_sidecar(ok)
    rejects(s.validate_sidecar, with_(ok, "response.headers.etag", f'"{"e" * 32}-3"'), "invalid_metadata", "sidecar.checks.etag")
    unmatched = with_(with_(sc, "response.headers.etag", '"' + "e" * 32 + '-9"'), "checks.etag", "unmatched")
    assert s.validate_sidecar(unmatched)  # e.g. a source that used another part size, or SSE-KMS
    rejects(s.validate_sidecar, with_(unmatched, "response.headers.etag", f'"{mp}"'), "invalid_metadata", "sidecar.checks.etag")
    opaque = with_(with_(sc, "response.headers.etag", '"0x8DCDEADBEEF1234"'), "checks.etag", "opaque")
    assert s.validate_sidecar(opaque)
    rejects(s.validate_sidecar, with_(opaque, "checks.etag", "unmatched"), "invalid_metadata", "sidecar.checks.etag")
    no_etag = {k: v for k, v in opaque["response"]["headers"].items() if k != "etag"}
    rejects(s.validate_sidecar, with_(opaque, "response.headers", no_etag), "invalid_metadata", "sidecar.checks.etag")
    own = hashlib.md5(data).digest()
    for header, value in (("x-ms-blob-content-md5", b64(own)), ("content-md5", own.hex())):
        good = with_(with_(opaque, f"response.headers.{header}", value), "checks.content_md5", "match")
        assert s.validate_sidecar(good)
        wrong = with_(with_(opaque, f"response.headers.{header}", b64(b"\x00" * 16)), "checks.content_md5", "mismatch")
        assert s.validate_sidecar(wrong)  # recorded, not refused: the bytes are still evidence
        rejects(s.validate_sidecar, with_(wrong, "checks.content_md5", "match"), "invalid_metadata", "sidecar.checks.content_md5")
    both = with_(with_(opaque, "response.headers.content-md5", b64(own)), "response.headers.x-ms-blob-content-md5", b64(b"\x01" * 16))
    rejects(s.validate_sidecar, with_(both, "checks.content_md5", "match"), "invalid_metadata", "sidecar.checks.content_md5")
    assert s.validate_sidecar(with_(both, "checks.content_md5", "mismatch"))
    assert s.validate_sidecar(with_(with_(opaque, "response.headers.content-md5", ""), "checks.content_md5", "mismatch"))


def test_multipart_sidecar_rules():
    sc = make_sidecar(blob(11 * s.MiB), part_size=5 * s.MiB)
    parts = sc["data"]["upload"]["part_sha256"]
    for bad in (parts[:2], parts + [parts[0]]):
        rejects(s.validate_sidecar, with_(sc, "data.upload.part_sha256", bad), "invalid_metadata", "sidecar.data.upload.part_sha256")
    rejects(s.validate_sidecar, with_(sc, "data.md5_multipart.part_size", 8 * s.MiB), "invalid_metadata",
            "sidecar.data.md5_multipart")  # 11 MiB is 2 parts at 8 MiB, not the recorded 3
    rejects(s.validate_sidecar, with_(sc, "data.upload.part_size", None), "invalid_metadata", "sidecar.data.upload")
    rejects(s.validate_sidecar, with_(sc, "data.upload.part_size", s.MiB), "invalid_metadata", "sidecar.data.upload.part_size")
    rejects(s.validate_sidecar, with_(sc, "data.staging_etag", '"' + "0" * 32 + '-2"'), "invalid_metadata",
            "sidecar.data.staging_etag")
    one = make_sidecar(CSV, part_size=5 * s.MiB)
    rejects(s.validate_sidecar, with_(one, "data.staging_etag", one["data"]["staging_etag"].replace('-1"', '-11"')),
            "invalid_metadata", "sidecar.data.staging_etag")
    rejects(s.validate_sidecar, with_(make_sidecar(), "data.upload.part_size", 5 * s.MiB), "invalid_metadata", "sidecar.data.upload")
    rejects(s.validate_sidecar, with_(make_big_sidecar(), "data.upload.method", "put"), "invalid_metadata", "sidecar.data.upload")
    put = with_(with_(make_big_sidecar(5_200_000_000), "data.upload", {"method": "put", "part_size": None, "part_sha256": None}),
                "data.staging_etag", '"' + make_sidecar()["data"]["md5"] + '"')
    e = rejects(s.validate_sidecar, put, "invalid_metadata", "sidecar.data.upload.method")
    assert e.field == "sidecar.data.upload.method"
    huge = with_(make_big_sidecar(60_000_000_000, approval=APPROVAL), "data.md5_multipart",
                 {"part_size": s.MUCKROCK_ETAG_PART_SIZE, "etag": "0" * 32 + "-11445"})
    rejects(s.validate_sidecar, huge, "invalid_metadata", "sidecar.data.md5_multipart")  # over S3's 10,000 parts


def test_cost_gate_needs_an_approval():
    rejects(s.validate_sidecar, make_big_sidecar(s.COST_GATE + 1), "too_large", "sidecar.data.size")
    assert s.validate_sidecar(make_big_sidecar(s.COST_GATE + 1, approval=APPROVAL))
    assert s.validate_sidecar(make_big_sidecar(s.COST_GATE))
    rejects(s.validate_sidecar, make_big_sidecar(s.COST_GATE, approval=APPROVAL), "invalid_metadata", "sidecar.fetch.approval")


def test_generated_manifest_sidecar_rules():
    sc = make_manifest_sidecar()
    for path, value in (("source.url", URL_OK), ("source.filename", "x.json"), ("source.doc_id", "1"),
                        ("listing", {"size": None, "date": None, "title": None})):
        rejects(s.validate_sidecar, with_(sc, path, value), "invalid_metadata", "sidecar.source")
    rejects(s.validate_sidecar, with_(sc, "fetch.origin", "live"), "invalid_metadata", "sidecar")
    rejects(s.validate_sidecar, with_(sc, "content_kind", "file"), "invalid_metadata", "sidecar.fetch.origin")
    rejects(s.validate_sidecar, with_(sc, "checks.sniffed_type", "pdf"), "invalid_metadata", "sidecar.checks.sniffed_type")
    rejects(s.validate_sidecar, with_(sc, "fetch.run_id", None), "invalid_metadata", "sidecar.fetch.run_id")


@pytest.mark.parametrize("value", [1.0, "1", True, None, [1]])
def test_type_confusion_is_refused(value):
    rejects(s.validate_sidecar, with_(make_sidecar(), "fetch.attempt", value), "invalid_metadata", "sidecar.fetch.attempt")


def test_enum_subclasses_are_refused():
    class Status(str, enum.Enum):
        OK = "ok"
    rejects(s.validate_sidecar, with_(make_sidecar(), "checks.length", Status.OK), "invalid_metadata", "sidecar.checks.length")


def test_errors_never_echo_values():
    cases = [
        ("source.request_id", PII + " x"), ("source.url", f"https://portal.example.gov/{PII}?sig=1"),
        ("response.headers.set-cookie", PII), (f"response.headers.x-{PII.lower()}", "1"),
        (PII.lower(), 1), ("fetch.run_id", PII + "\x01"), ("source.doc_id", PII + RLO),
        ("response.headers.location", f"https://h.example.gov/{PII}?x=1"),
    ]
    for path, value in cases:
        with pytest.raises(s.SchemaError) as e:
            s.validate_sidecar(with_(make_sidecar(), path, value))
        assert PII.lower() not in str(e.value).lower()


def test_sidecar_key_and_canonical_bytes_are_enforced():
    sc = make_sidecar()
    raw = s.sidecar_bytes(sc)
    rejects(lambda r: s.parse_sidecar(r, key=s.staging_sidecar_key(U2)), raw, "invalid_metadata", "sidecar.uuid")
    rejects(lambda r: s.parse_sidecar(r, key=s.staging_data_key(U)), raw, "invalid_metadata", "sidecar.uuid")
    with pytest.raises(TypeError):
        s.parse_sidecar(raw)  # the key is required
    rejects(lambda r: s.parse_sidecar(r, key=s.staging_sidecar_key(U)), json.dumps(sc).encode(), "bad_sidecar", "sidecar")
    rejects(lambda r: s.parse_sidecar(r, key=s.staging_sidecar_key(U)), raw.rstrip(b"\n"), "bad_sidecar", "sidecar")


def test_the_worst_case_sidecar_and_record_fit():
    """Every field at its cap, with the characters that grow most when escaped,
    and 10,000 parts: it still serializes and parses, so validation and the
    byte limits can never disagree."""
    worst = make_big_sidecar(s.MAX_PARTS * s.PART_SIZE, approval=APPROVAL)
    assert len(worst["data"]["upload"]["part_sha256"]) == s.MAX_PARTS
    ctl = "\x01"  # 1 UTF-8 byte, 6 bytes escaped
    worst["source"].update(filename=ctl * 4096, title=ctl * 65536, agency=ctl * 1024)
    worst["listing"].update(date=ctl * 256, title=ctl * 65536)
    keep = {k: worst["response"]["headers"][k] for k in ("etag", "content-length")}
    filler = s.sanitize_headers([(n, chr(0x85) * 4096) for n in sorted(s.ALLOWED_HEADERS - {
        "etag", "content-length", "content-md5", "x-ms-blob-content-md5", "content-range", "content-encoding",
        "transfer-encoding"})])
    worst["response"]["headers"] = {**filler, **keep}
    worst["response"]["redirects"] = [{"status": 302, "url": "https://www.muckrock.com/" + "a" * 2000}] * s.MAX_REDIRECTS
    raw = s.sidecar_bytes(worst)
    assert len(raw) < s.MAX_SIDECAR_BYTES
    assert s.parse_sidecar(raw, key=s.staging_sidecar_key(U)) == worst
    record = make_record(worst)
    assert len(s.record_bytes(record)) < s.MAX_RECORD_BYTES


def test_writers_and_readers_agree_on_size():
    m = s.build_manifest(MANIFEST_SOURCE, [s.manifest_entry(f"{i:06d}-" + ("\x01" * 4000), status="failed", reason="x")
                                            for i in range(300)])
    assert len(s.manifest_bytes(m)) > 64  # fine at this size
    orig = s.MAX_MANIFEST_BYTES
    try:
        s.MAX_MANIFEST_BYTES = len(s.canonical_json(m)) - 1
        rejects(s.manifest_bytes, m, "bad_manifest", "manifest")
        rejects(s.validate_manifest, m, "bad_manifest", "manifest")
    finally:
        s.MAX_MANIFEST_BYTES = orig


# --- The intake record ------------------------------------------------------------------


@pytest.mark.parametrize("make", [
    lambda: make_sidecar(),
    lambda: make_sidecar(blob(11 * s.MiB), part_size=5 * s.MiB),
    lambda: make_sidecar(origin="local-copy"),
    lambda: make_manifest_sidecar(),
])
def test_records_up_to_single_put_max_are_full_object(make):
    r = make_record(make())
    assert r["evidence"]["checksum_type"] == "FULL_OBJECT"
    raw = s.record_bytes(r)
    assert s.parse_record(raw) == r and s.parse_record(raw, key=s.record_key(r["uuid"])) == r
    rejects(lambda b: s.parse_record(b, key=s.record_key(U2)), raw, "invalid_metadata", "record.uuid")


def test_record_over_single_put_max_is_bound_to_the_staging_parts():
    r = make_record(make_big_sidecar())
    assert r["evidence"]["checksum_type"] == "COMPOSITE"
    assert r["evidence"]["checksum_sha256"] == r["staging"]["data_checksum_sha256"]
    parts = r["evidence"]["part_sha256"]
    rejects(s.validate_record, with_(r, "evidence.part_sha256", parts[:1] + ["ee" * 32] + parts[2:]), "invalid_metadata",
            "record.evidence.part_sha256")
    rejects(s.validate_record, with_(r, "evidence.part_size", s.PART_SIZE * 2), "invalid_metadata", "record.evidence.part_sha256")
    rejects(s.validate_record, with_(r, "evidence.checksum_sha256", s.sha256_b64(r["sha256"])), "invalid_metadata",
            "record.evidence.checksum_sha256")
    rejects(s.validate_record, with_(r, "evidence.checksum_type", "FULL_OBJECT"), "invalid_metadata", "record.evidence.checksum_type")


def test_single_put_max_is_the_exact_threshold():
    at = make_big_sidecar(s.SINGLE_PUT_MAX)
    assert make_record(at)["evidence"]["checksum_type"] == "FULL_OBJECT"
    over = make_big_sidecar(s.SINGLE_PUT_MAX + 1)
    sha = over["data"]["sha256"]
    full = {"bucket": EVD, "key": s.blob_key(sha), "version_id": "v1", "checksum_type": "FULL_OBJECT",
            "checksum_sha256": s.sha256_b64(sha), "part_size": None, "part_sha256": None,
            "lock_mode": "GOVERNANCE", "retain_until": "2036-09-28T18:00:03Z"}
    rejects(lambda o: s.build_record(o, staging=staging_for(o), evidence=full, ingest=INGEST), over,
            "invalid_metadata", "record.evidence.checksum_type")


OTHER_ACCOUNT_EVD = s.bucket_name("evidence", "prod", "210987654321", REGION)
OTHER_REGION_EVD = s.bucket_name("evidence", "prod", ACCOUNT, "us-east-1")
RECORD_MUTATIONS = [
    ("schema", 2, "schema_version", "record.schema"), ("sha256", "cd" * 32, "invalid_metadata", "record.sha256"),
    ("size", 1, "invalid_metadata", "record.sha256"), ("uuid", U2, "invalid_metadata", "record.uuid"),
    ("evidence.key", "sha256/" + "cd" * 32, "invalid_metadata", "record.evidence.key"),
    ("evidence.checksum_type", "COMPOSITE", "invalid_metadata", "record.evidence.checksum_sha256"),
    ("evidence.checksum_sha256", s.sha256_b64("cd" * 32), "invalid_metadata", "record.evidence.checksum_sha256"),
    ("evidence.part_size", s.PART_SIZE, "invalid_metadata", "record.evidence.part_sha256"),
    ("evidence.lock_mode", "NONE", "invalid_metadata", "record.evidence.lock_mode"),
    ("evidence.retain_until", "2026-09-28T18:00:01Z", "invalid_metadata", "record.evidence.retain_until"),
    ("evidence.retain_until", "2026-09-28T18:00:02Z", "invalid_metadata", "record.evidence.retain_until"),
    ("evidence.bucket", f"sm-alpr-pra-{ACCOUNT}-{REGION}-an", "invalid_metadata", "record.evidence.bucket"),
    ("evidence.bucket", STG, "invalid_metadata", "record.evidence.bucket"),
    ("evidence.bucket", s.bucket_name("evidence", "dev", ACCOUNT, REGION), "invalid_metadata", "record.staging.bucket"),
    ("evidence.bucket", OTHER_ACCOUNT_EVD, "invalid_metadata", "record.staging.bucket"),
    ("evidence.bucket", OTHER_REGION_EVD, "invalid_metadata", "record.staging.bucket"),
    ("staging.bucket", EVD, "invalid_metadata", "record.staging.bucket"),
    ("staging.bucket", "some-other-bucket", "invalid_metadata", "record.staging.bucket"),
    ("staging.data_key", f"in/{U2}.bin", "invalid_metadata", "record.staging"),
    ("staging.sidecar_key", f"in/{U2}.json", "invalid_metadata", "record.staging"),
    ("staging.data_etag", '"' + "0" * 32 + '"', "invalid_metadata", "record.staging.data_etag"),
    ("staging.data_checksum_type", "COMPOSITE", "invalid_metadata", "record.staging.data_checksum_sha256"),
    ("staging.data_checksum_sha256", s.sha256_b64("cd" * 32), "invalid_metadata", "record.staging.data_checksum_sha256"),
    ("staging.sidecar_sha256", "cd" * 32, "invalid_metadata", "record.staging.sidecar_sha256"),
    ("staging.data_sse", "aws:kms", "invalid_metadata", "record.staging.data_sse"),
    ("ingest.deriver", 2, "schema_version", "record.ingest.deriver"),
    ("ingest.deriver", 0, "invalid_metadata", "record.ingest.deriver"),
    ("ingest.at", "2026-09-28T00:00:00Z", "invalid_metadata", "record.ingest.<unknown>"),
    ("sidecar.source.url", "https://x.example.gov/a?sig=1", "signed_url", "record.sidecar.source.url"),
    ("sidecar.source.title", "changed", "invalid_metadata", "record.staging.sidecar_sha256"),
]


@pytest.mark.parametrize("path,value,reason,field", RECORD_MUTATIONS)
def test_record_rules(path, value, reason, field):
    rejects(s.validate_record, with_(make_record(), path, value), reason, field)


def test_record_core():
    sc = make_sidecar()
    a = make_record(sc)
    assert s.record_bytes(a) == s.record_bytes(make_record(copy.deepcopy(sc)))
    assert tuple(s.record_core(a)) == s.RECORD_CORE_FIELDS
    assert set(a["ingest"]) == {"deriver", "code_sha256", "principal"}  # no clock, no request id
    for path in ("ingest.code_sha256", "ingest.principal", "evidence.retain_until", "evidence.lock_mode"):
        assert s.record_core(with_(a, path, None if path.startswith("ingest") else "x")) == s.record_core(a)
    for path, value in (("uuid", U2), ("sha256", "cd" * 32), ("size", 9), ("evidence.key", "k"),
                        ("evidence.version_id", "v2"), ("staging.data_etag", '"x"'), ("staging.sidecar_sha256", "0" * 64)):
        assert s.record_core(with_(a, path, value)) != s.record_core(a), path


def test_build_record_copies_and_stamps_the_current_deriver():
    sc = make_sidecar()
    staging, evidence, ingest = staging_for(sc), evidence_for(sc), dict(INGEST)
    r = s.build_record(sc, staging=staging, evidence=evidence, ingest=ingest)
    sc["source"]["title"], staging["bucket"], evidence["version_id"], ingest["principal"] = "x", "y", "z", "w"
    assert s.validate_record(r) and r["sidecar"]["source"]["title"] == "Audit log" and r["ingest"]["principal"] != "w"
    with pytest.raises(s.SchemaError):
        make_record(ingest={**INGEST, "deriver": 0})
    assert make_record(lock_mode="COMPLIANCE")


def test_stored_documents_are_read_without_write_policy():
    """Tightening the write policy later must never make stored evidence unreadable."""
    for path, value, reason, field in (
            ("response.headers.x-custom", "kept by an older writer", "forbidden_header", "record.sidecar.response.headers"),
            ("fetch.run_id", "run" + ZWSP, "invalid_metadata", "record.sidecar.fetch.run_id"),
            ("source.url", "https://cdn.muckrock.com/a.csv?token=x", "signed_url", "record.sidecar.source.url")):
        r = reseal(with_(make_record(), f"sidecar.{path}", value))
        raw = s.canonical_json(r)
        assert s.parse_record(raw) == r  # stored: invariants only
        rejects(lambda b: s.parse_record(b, stored=False), raw, reason, field)
    tampered = with_(r, "sidecar.source.title", "changed")  # invariants still bind
    rejects(s.parse_record, s.canonical_json(tampered), "invalid_metadata", "record.staging.sidecar_sha256")
    m = make_manifest()
    m["files"][0]["url"] = "https://cdn.muckrock.com/a?token=x"
    raw = s.canonical_json(m)
    assert s.parse_manifest(raw, stored=True) == m
    rejects(s.parse_manifest, raw, "signed_url", "manifest.files[0].url")


# --- The fetch manifest ------------------------------------------------------------------


def test_manifest_bytes_ignore_input_order_and_repeats():
    files = manifest_files()
    a = s.manifest_bytes(s.build_manifest(MANIFEST_SOURCE, files, files_listed=4))
    b = s.manifest_bytes(s.build_manifest(MANIFEST_SOURCE, list(reversed(files)) + [files[0]], files_listed=4))
    assert a == b
    assert [e["filename"] for e in json.loads(a)["files"]] == ["a.txt", "b.txt", "big.zip", "c.pdf"]
    assert s.parse_manifest(a) == json.loads(a)
    assert b"fetched_at" not in a and b"run" not in a  # the request, not the run: unchanged listings dedupe
    assert s.manifest_shas(json.loads(a)) == ["aa" * 32, "bb" * 32]


def test_manifest_order_is_by_code_point_nulls_first_sizes_numeric():
    files = [s.manifest_entry(chr(0xE000), status="failed", reason="x"),
             s.manifest_entry(GRIN, status="failed", reason="x"),
             s.manifest_entry("a", status="held", sha256="aa" * 32, size=10),
             s.manifest_entry("a", status="held", sha256="aa" * 32, size=10, doc_id="1"),
             s.manifest_entry("b", status="held", sha256="bb" * 32, size=9),
             s.manifest_entry("b", status="held", sha256="cc" * 32, size=10)]
    m = s.build_manifest(MANIFEST_SOURCE, list(reversed(files)))
    assert [(e["filename"], e["doc_id"], e["sha256"]) for e in m["files"]] == [
        ("a", None, "aa" * 32), ("a", "1", "aa" * 32), ("b", None, "bb" * 32), ("b", None, "cc" * 32),
        (chr(0xE000), None, None), (GRIN, None, None)]


def test_build_manifest_copies_its_inputs():
    source, files = copy.deepcopy(MANIFEST_SOURCE), manifest_files()
    m = s.build_manifest(source, files, files_listed=4)
    source["request_id"], files[0]["filename"] = "999", "z"
    assert m["source"]["request_id"] == "12345" and "z" not in [e["filename"] for e in m["files"]]


@pytest.mark.parametrize("mutate,field", [
    (lambda m: m["files"].reverse(), "manifest.files"),
    (lambda m: m["files"].append(dict(m["files"][-1])), "manifest.files"),
    (lambda m: m["files"][0].update(sha256=None), "manifest.files[0]"),
    (lambda m: m["files"][0].update(size=None), "manifest.files[0]"),
    (lambda m: m["files"][0].update(reason="x"), "manifest.files[0].reason"),
    (lambda m: m["files"][1].update(sha256="aa" * 32, size=99), "manifest.files[1].size"),
    (lambda m: m["files"][3].update(reason=None), "manifest.files[3].reason"),
    (lambda m: m["files"][3].update(sha256="aa" * 32), "manifest.files[3].sha256"),
    (lambda m: m["files"][3].update(reason="Not Found"), "manifest.files[3].reason"),
    (lambda m: m["files"][2].update(reason="cost_gate"), "manifest.files[2].reason"),
    (lambda m: m["files"][0].update(status="stored"), "manifest.files[0].status"),
    (lambda m: m["source"].update(platform="govqa"), "manifest.source.platform"),
    (lambda m: m["source"].update(host="cdn.muckrock.com"), "manifest.source.host"),
    (lambda m: m.update(kind="file"), "manifest.kind"),
])
def test_manifest_rules(mutate, field):
    m = make_manifest()
    mutate(m)
    rejects(s.validate_manifest, m, "invalid_metadata", field)


def test_parse_manifest_reports_bad_manifest():
    m = make_manifest()
    m["files"].reverse()
    rejects(s.parse_manifest, s.canonical_json(m), "bad_manifest", "manifest.files")
    rejects(s.parse_manifest, b"not json", "bad_manifest", "manifest")
    rejects(s.parse_manifest, s.canonical_json({**make_manifest(), "schema": 2}), "schema_version", "manifest.schema")


def test_manifest_sidecar_must_describe_the_same_request():
    m, sc = make_manifest(), make_manifest_sidecar()
    assert s.validate_manifest_sidecar(sc, m)
    for k, v in (("request_id", "999"), ("host", "h.example.gov"), ("agency", None), ("kind", "portal")):
        rejects(lambda x: s.validate_manifest_sidecar(x, m), with_(sc, f"source.{k}", v), "manifest_mismatch",
                f"sidecar.source.{k}")
    rejects(lambda x: s.validate_manifest_sidecar(x, m), make_sidecar(), "manifest_mismatch", "sidecar.content_kind")


# --- The source stays ASCII --------------------------------------------------------------


def test_module_source_is_plain_ascii():
    """Invisible or bidi characters in the contract's source could make a check
    read differently than it runs; escapes and chr() only."""
    for path in (SCRIPT_DIR / "pra_intake").glob("*.py"):
        assert path.read_bytes().isascii(), path


# --- Fixtures: the permanent vocabulary and frozen v1 documents -------------------------------


def vocabulary():
    return {
        "SCHEMA_VERSION": s.SCHEMA_VERSION, "READABLE_SCHEMAS": sorted(s.READABLE_SCHEMAS),
        "DERIVER_VERSION": s.DERIVER_VERSION,
        "limits": {n: getattr(s, n) for n in (
            "PART_SIZE", "MUCKROCK_ETAG_PART_SIZE", "SINGLE_PUT_MAX", "COST_GATE", "MIN_PART_SIZE", "MAX_PART_SIZE",
            "MAX_PARTS", "MAX_OBJECT_SIZE", "MAX_OBSERVED_SIZE", "MAX_SIDECAR_BYTES", "MAX_RECORD_BYTES",
            "MAX_MANIFEST_BYTES", "MAX_MANIFEST_FILES", "MAX_JSON_DEPTH", "SNIFF_BYTES", "MAX_HEADERS",
            "MAX_HEADER_BYTES", "MAX_HEADERS_BYTES", "MAX_REDIRECTS")},
        "FIELD_LIMITS": s.FIELD_LIMITS,
        "REJECT_REASONS": sorted(s.REJECT_REASONS), "SECRET_PARAMS": sorted(s.SECRET_PARAMS),
        "SECRET_PARAM_PREFIXES": list(s.SECRET_PARAM_PREFIXES), "COMPANION_PARAMS": sorted(s.COMPANION_PARAMS),
        "ALLOWED_HEADERS": sorted(s.ALLOWED_HEADERS), "URL_HEADERS": sorted(s.URL_HEADERS),
        "SNIFF_TYPES": list(s.SNIFF_TYPES), "SOURCE_KINDS": list(s.SOURCE_KINDS), "PLATFORMS": list(s.PLATFORMS),
        "ORIGINS": list(s.ORIGINS), "CONTENT_KINDS": list(s.CONTENT_KINDS), "UPLOAD_METHODS": list(s.UPLOAD_METHODS),
        "ETAG_CHECKS": list(s.ETAG_CHECKS), "CONTENT_MD5_CHECKS": list(s.CONTENT_MD5_CHECKS),
        "LENGTH_CHECKS": list(s.LENGTH_CHECKS), "EOF_CHECKS": list(s.EOF_CHECKS), "ACCESS": list(s.ACCESS),
        "LIVE_STATUSES": list(s.LIVE_STATUSES), "REDIRECT_STATUSES": list(s.REDIRECT_STATUSES),
        "CHECKSUM_TYPES": list(s.CHECKSUM_TYPES), "LOCK_MODES": list(s.LOCK_MODES),
        "MANIFEST_STATUSES": list(s.MANIFEST_STATUSES), "NEEDS_APPROVAL_REASON": s.NEEDS_APPROVAL_REASON,
        "RECORD_CORE_FIELDS": list(s.RECORD_CORE_FIELDS), "INGESTED_TAG": list(s.INGESTED_TAG),
        "MANIFEST_FILENAME": s.MANIFEST_FILENAME, "STAGING_SSE": s.STAGING_SSE,
        "EVIDENCE_CONTENT_TYPE": s.EVIDENCE_CONTENT_TYPE, "EVIDENCE_CONTENT_DISPOSITION": s.EVIDENCE_CONTENT_DISPOSITION,
        "bucket_names": {f"{env}/{role}": s.bucket_name(role, env, ACCOUNT, REGION)
                         for env in sorted(s.ENV_PREFIXES) for role in s.BUCKET_ROLES},
        "keys": {"staging_data": s.staging_data_key(U), "staging_sidecar": s.staging_sidecar_key(U),
                 "blob": s.blob_key("ab" * 32), "record": s.record_key(U), "errata": s.errata_key(U, 1),
                 "approval": s.approval_key(U)},
    }


def documents():
    """Every kind of stored document, as this code writes it."""
    manifest = make_manifest()
    presented = with_(with_(make_sidecar(), "source.title", f"R{E_ACUTE}sum{E_ACUTE} {GRIN} {RLO}x\n{TAG_A}"),
                      "source.filename", f"{CJK}.csv")
    return {
        "sidecar_put.json": s.sidecar_bytes(make_sidecar()),
        "sidecar_multipart.json": s.sidecar_bytes(make_sidecar(blob(11 * s.MiB, 2), part_size=5 * s.MiB)),
        "sidecar_local_copy.json": s.sidecar_bytes(make_sidecar(origin="local-copy")),
        "sidecar_git.json": s.sidecar_bytes(make_sidecar(origin="git")),
        "sidecar_presented_text.json": s.sidecar_bytes(presented),
        "sidecar_requester.json": s.sidecar_bytes(with_(make_sidecar(), "fetch.access", "requester")),
        "sidecar_fetch_manifest.json": s.sidecar_bytes(make_manifest_sidecar(manifest)),
        "record_put.json": s.record_bytes(make_record()),
        "record_multipart_staging.json": s.record_bytes(make_record(make_sidecar(blob(11 * s.MiB, 2), part_size=5 * s.MiB))),
        "record_composite.json": s.record_bytes(make_record(make_big_sidecar())),
        "record_compliance.json": s.record_bytes(make_record(lock_mode="COMPLIANCE")),
        "manifest.json": s.manifest_bytes(manifest),
    }


def _parser(name):
    if name.startswith("manifest"):
        return lambda raw: s.parse_manifest(raw, stored=True)
    if name.startswith("record"):
        return s.parse_record
    return lambda raw: s.validate_sidecar(s.parse_strict_json(raw, max_bytes=s.MAX_SIDECAR_BYTES), stored=True)


def test_vocabulary_is_pinned():
    """If this fails, the permanent vocabulary changed. That needs a new schema
    version (with the old one still readable), not a regenerated fixture."""
    assert json.loads((FIXTURES / "vocabulary.json").read_text()) == vocabulary()


V1_DOCUMENTS = sorted(p.name for p in V1.glob("*.json"))


def test_v1_fixtures_exist():
    assert set(V1_DOCUMENTS) >= set(documents())


@pytest.mark.parametrize("name", V1_DOCUMENTS)
def test_frozen_v1_documents_stay_readable(name):
    """Frozen when schema 1 shipped and never regenerated: every future version
    of this module must still read them, exactly."""
    raw = (V1 / name).read_bytes()
    assert s.canonical_json(_parser(name)(raw)) == raw


@pytest.mark.parametrize("name", sorted(documents()))
def test_writers_reproduce_v1_documents(name):
    """While writers stamp schema 1, they write exactly the frozen bytes."""
    assert s.SCHEMA_VERSION == 1
    assert documents()[name] == (V1 / name).read_bytes()


if __name__ == "__main__":  # write only what's missing: python tests/test_pra_intake_schema.py
    V1.mkdir(parents=True, exist_ok=True)
    if not (FIXTURES / "vocabulary.json").exists():
        (FIXTURES / "vocabulary.json").write_text(json.dumps(vocabulary(), indent=2, sort_keys=True) + "\n")
    for name, raw in documents().items():
        if not (V1 / name).exists():
            (V1 / name).write_bytes(raw)
