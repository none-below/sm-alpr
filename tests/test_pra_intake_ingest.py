"""Tests for scripts/pra_intake/ingest.py, the ingest Lambda, against the
in-memory S3 in pra_intake_fakes.py.

stage() writes staging objects the way the upload library (PR 3) will: the
data object with S3 checksums and x-amz-meta, then the sidecar. The Lambda
runs with exactly the grants ingest.PERMISSIONS declares, so the README's
IAM list is what's tested. Every test reads the outcome from what's left in
the fake buckets, and the invariant the Lambda exists for is checked after
every run: each record in evidence parses as a stored record, and its blob
version holds exactly the bytes it names, under the lock it names.
"""

import copy
import hashlib
import json
import signal
import sys
import types
from datetime import timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(Path(__file__).parent))

from pra_intake import ingest as ig  # noqa: E402
from pra_intake import schema as s  # noqa: E402
from pra_intake_fakes import FakeClientError, FakeS3, b64_sha256, grants, sqs_message  # noqa: E402

ACCOUNT, REGION = "123456789012", "us-west-2"  # AWS's documented example account
STG = s.bucket_name("staging", "dev", ACCOUNT, REGION)
EVD = s.bucket_name("evidence", "dev", ACCOUNT, REGION)
OPS = s.bucket_name("ops", "dev", ACCOUNT, REGION)
BUCKETS = {"staging": STG, "evidence": EVD, "ops": OPS}
LAMBDA = grants(ig.PERMISSIONS, BUCKETS)
SWEEPER = grants(ig.SWEEP_PERMISSIONS, BUCKETS)
MiB = s.MiB
FETCH = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
CODE = "c" * 64
SENTINELS = ("SENTINEL-Jane-Doe", "sentinel-host.example.org", "SENTINEL-REQ-77")


def blob(n, seed=0):
    """n deterministic, non-repeating bytes."""
    out, i = bytearray(), seed * 1_000_000
    while len(out) < n:
        out += hashlib.sha256(i.to_bytes(8, "big")).digest()
        i += 1
    return bytes(out[:n])


CSV = b"case,plate,reason\n1,XYZ,investigation\n"
BIG = blob(11 * MiB, 1)  # three 5 MiB parts: the smallest file that exercises multipart


def multipart_etag(data, part_size):
    md5s = [hashlib.md5(data[i:i + part_size]).digest() for i in range(0, len(data), part_size)] or [
        hashlib.md5(b"").digest()]
    return f"{hashlib.md5(b''.join(md5s)).hexdigest()}-{len(md5s)}"


def make_sidecar(data, u, *, part_size=None, origin="live", mp=True, filename="a.csv"):
    """A consistent sidecar for `data`, as the library would write it."""
    sha, md5 = hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest()
    if part_size:
        parts = [hashlib.sha256(data[i:i + part_size]).hexdigest() for i in range(0, len(data), part_size)]
        upload = {"method": "multipart", "part_size": part_size, "part_sha256": parts}
        staging_etag = f'"{multipart_etag(data, part_size)}"'
    else:
        upload = {"method": "put", "part_size": None, "part_sha256": None}
        staging_etag = f'"{md5}"'
    live, backfill = origin == "live", origin in ("local-copy", "git")
    return {
        "schema": 1, "uuid": u, "content_kind": "file",
        "data": {
            "size": len(data), "sha256": sha, "md5": md5,
            "md5_multipart": ({"part_size": s.MUCKROCK_ETAG_PART_SIZE,
                               "etag": multipart_etag(data, s.MUCKROCK_ETAG_PART_SIZE)} if data and mp else None),
            "staging_etag": staging_etag, "upload": upload,
        },
        "source": {
            "kind": "muckrock", "platform": "muckrock", "host": "www.muckrock.com",
            "agency": "San Mateo Police Department", "request_id": "12345",
            "request_url": "https://www.muckrock.com/foi/san-mateo-12345/",
            "doc_id": "987654", "filename": filename, "title": "Audit log",
            "url": "https://cdn.muckrock.com/foia_files/2026/09/28/a.csv", "released_on": "2026-09-27",
        },
        "fetch": {
            "origin": origin, "fetch_id": FETCH, "run_id": "run-1", "work_id": None, "attempt": 1,
            "connector": "muckrock", "connector_version": "abc1234", "library_version": "1",
            "access": "anonymous", "started_at": "2026-09-28T18:00:00Z", "first_byte_at": "2026-09-28T18:00:00.5Z",
            "completed_at": "2026-09-28T18:00:01.25Z", "retries": 0, "ci_run": None, "approval": None,
            "legacy_path": "107949-roseville-police-department/a.csv" if backfill else None,
            "original_fetched_at": "2026-09-20T01:02:03Z" if backfill else None,
            "git_commit": "0" * 40 if origin == "git" else None, "stamp_ref": None,
        },
        "response": {
            "status": 200,
            "headers": {"etag": f'"{md5}"', "content-length": str(len(data)), "content-type": "text/csv"},
            "redirects": [{"status": 302, "url": "https://www.muckrock.com/foi/files/987654/"}],
            "final_url": "https://cdn.muckrock.com/foia_files/2026/09/28/a.csv",
        } if live else None,
        "listing": {"size": len(data), "date": "2026-09-27", "title": "Audit log"},
        "checks": {
            "declared_length": len(data) if live else None, "length": "ok" if live else "undeclared",
            "eof": "clean", "etag": "md5" if live else "absent", "content_md5": "absent",
            "sniffed_type": s.sniff_type(data[:s.SNIFF_BYTES]), "expect_types": None,
        },
    }


def staging_tag_policy(role, tags):
    """The pinned staging policy: the ingest role (and admins) set any tags;
    the sweep only its re-drive count, never an empty set."""
    return role is None or LAMBDA <= role or (SWEEPER <= role and bool(tags) and set(tags) == {ig.REDRIVES_TAG})


def new_fake(*, lock_mode="GOVERNANCE", lock_days=3650):
    f = FakeS3()
    f.create_bucket(STG, require_if_none_match=True).tag_policy = staging_tag_policy
    f.create_bucket(EVD, lock_days=lock_days, lock_mode=lock_mode, require_if_none_match=True)
    f.create_bucket(OPS)
    return f


@pytest.fixture
def fake():
    return new_fake()


_seq = iter(range(1, 10 ** 6))


def next_uuid():
    return f"{next(_seq):08x}-0000-4000-8000-000000000000"


def stage(fake, data, *, part_size=None, u=None, edit=None, sidecar=None, metadata=None, raw_sidecar=None,
          **kw):
    """Stage `data` as the library does: the data object (S3 checksums,
    x-amz-meta), then the sidecar. edit(sidecar) changes the sidecar after
    the upload, to make it lie; raw_sidecar replaces its bytes outright."""
    u = u or next_uuid()
    sc = sidecar or make_sidecar(data, u, part_size=part_size, **kw)
    sc["uuid"] = u
    meta = metadata if metadata is not None else s.staging_metadata(sc["source"], u, sc["fetch"]["fetch_id"])
    key = s.staging_data_key(u)
    if part_size:
        mpu = fake.create_multipart_upload(Bucket=STG, Key=key, ChecksumAlgorithm="SHA256", Metadata=meta)
        parts = []
        for n, i in enumerate(range(0, len(data), part_size), 1):
            chunk = data[i:i + part_size]
            got = fake.upload_part(Bucket=STG, Key=key, UploadId=mpu["UploadId"], PartNumber=n, Body=chunk,
                                   ChecksumSHA256=b64_sha256(chunk))
            parts.append({"PartNumber": n, "ETag": got["ETag"], "ChecksumSHA256": got["ChecksumSHA256"]})
        put = fake.complete_multipart_upload(Bucket=STG, Key=key, UploadId=mpu["UploadId"],
                                             MultipartUpload={"Parts": parts}, IfNoneMatch="*")
    else:
        put = fake.put_object(Bucket=STG, Key=key, Body=data, ContentLength=len(data),
                              ChecksumSHA256=b64_sha256(data), IfNoneMatch="*", Metadata=meta)
    assert put["ETag"] == sc["data"]["staging_etag"]
    if edit:
        edit(sc)
    body = raw_sidecar if raw_sidecar is not None else s.canonical_json(sc)
    fake.put_object(Bucket=STG, Key=s.staging_sidecar_key(u), Body=body, IfNoneMatch="*",
                    ContentType="application/json")
    return u, sc


def ingest(fake, *, role=LAMBDA, **kw):
    logs = []
    kw.setdefault("log", logs.append)
    kw.setdefault("now", lambda: fake.now)
    return ig.Ingest(fake.as_role(role), staging_bucket=STG, evidence_bucket=EVD, ops_bucket=OPS,
                     code_sha256=CODE, **kw), logs


def run(fake, u, **kw):
    allow_large = kw.pop("allow_large", False)
    ing, logs = ingest(fake, **kw)
    out = ing.process(s.staging_sidecar_key(u), principal=fake.principal, allow_large=allow_large)
    check_evidence(fake)
    return out, logs


def check_evidence(fake):
    """Every record parses as stored, from its own key, and its blob version
    holds exactly the bytes it names, under a lock at least as long and as
    strict as the one it names (a later sighting may have renewed it)."""
    for key in fake.keys(EVD, s.RECORD_PREFIX):
        record = s.parse_record(fake.current(EVD, key)["data"], stored=True, key=key)
        ev = record["evidence"]
        [version] = [v for v in fake.versions(EVD, ev["key"]) if v["version_id"] == ev["version_id"]]
        assert hashlib.sha256(version["data"]).hexdigest() == record["sha256"]
        assert version["checksum"] == ev["checksum_sha256"] and version["checksum_type"] == ev["checksum_type"]
        assert version["lock_until"] >= s.parse_timestamp(ev["retain_until"])
        assert version["lock_mode"] == ev["lock_mode"] or version["lock_mode"] == "COMPLIANCE"
    for key in fake.keys(EVD, s.BLOB_PREFIX):
        v = fake.current(EVD, key)
        assert v["content_type"] == s.EVIDENCE_CONTENT_TYPE
        assert v["content_disposition"] == s.EVIDENCE_CONTENT_DISPOSITION
        assert v["metadata"] == {} and v["tags"] == {}


def record_of(fake, u):
    obj = fake.current(EVD, s.record_key(u))
    return s.parse_record(obj["data"], stored=False, key=s.record_key(u)) if obj else None


def staging_tags(fake, u):
    return fake.tags(STG, s.staging_data_key(u)), fake.tags(STG, s.staging_sidecar_key(u))


def assert_rejected(fake, u, out, reason):
    assert (out.status, out.reason) == ("rejected", reason), out
    assert staging_tags(fake, u) == (s.rejected_tags(reason), s.rejected_tags(reason))
    assert record_of(fake, u) is None


def assert_untouched(fake, u):
    """A transient failure: nothing tagged, nothing recorded."""
    assert staging_tags(fake, u) == ({}, {})
    assert record_of(fake, u) is None


def blob_writes(fake):
    return [c for c in fake.calls if c[0] in ("put_object", "complete_multipart_upload") and c[1] == EVD
            and c[2].startswith(s.BLOB_PREFIX)]


def last_field(logs):
    return json.loads(logs[-1]).get("field")


def lie_md5(sc):
    sc["data"]["md5"] = "0" * 32
    sc["response"]["headers"]["etag"] = '"' + "0" * 32 + '"'


def lie_sha(sc):
    sc["data"]["sha256"] = hashlib.sha256(b"other").hexdigest()


def lie_mp(sc):
    sc["data"]["md5_multipart"]["etag"] = "0" * 32 + "-" + sc["data"]["md5_multipart"]["etag"].split("-")[1]


def lie_sniff(sc):
    sc["checks"]["sniffed_type"] = "pdf"


# --- The happy paths ---------------------------------------------------------------------


def test_a_single_put_is_stored_recorded_and_tagged(fake):
    u, sc = stage(fake, CSV)
    out, logs = run(fake, u)
    sha = sc["data"]["sha256"]
    assert out == ig.Outcome("stored", u, sha)
    assert fake.current(EVD, s.blob_key(sha))["data"] == CSV
    record = record_of(fake, u)
    assert record["sidecar"] == sc
    assert record["evidence"]["checksum_type"] == "FULL_OBJECT"
    assert record["ingest"] == {"deriver": 1, "code_sha256": CODE, "principal": fake.principal}
    assert record["staging"]["sidecar_sha256"] == hashlib.sha256(s.canonical_json(sc)).hexdigest()
    assert record["staging"]["data_checksum_type"] == "FULL_OBJECT"
    assert record["staging"]["data_last_modified"] == s.format_timestamp(
        fake.current(STG, s.staging_data_key(u))["last_modified"])
    assert staging_tags(fake, u) == (s.ingested_tags(sha), s.ingested_tags(sha))
    assert fake.keys(EVD) == sorted([s.blob_key(sha), s.record_key(u)])
    assert [json.loads(line)["event"] for line in logs] == ["stored"]


def test_a_multipart_upload_becomes_a_single_put_blob(fake):
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    out, _ = run(fake, u)
    assert out.status == "stored"
    record = record_of(fake, u)
    assert record["staging"]["data_checksum_type"] == "COMPOSITE"
    assert record["staging"]["data_checksum_sha256"] == s.composite_sha256(sc["data"]["upload"]["part_sha256"])
    assert record["evidence"]["checksum_type"] == "FULL_OBJECT" and record["evidence"]["part_sha256"] is None
    blob_obj = fake.current(EVD, s.blob_key(sc["data"]["sha256"]))
    assert blob_obj["data"] == BIG and blob_obj["etag"] == f'"{hashlib.md5(BIG).hexdigest()}"'


def test_an_empty_file_is_stored(fake):
    u, sc = stage(fake, b"")
    assert run(fake, u)[0].status == "stored"
    assert fake.current(EVD, s.blob_key(hashlib.sha256(b"").hexdigest()))["data"] == b""


def test_short_reads_from_s3_change_nothing(fake):
    fake.max_read = 1000
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    assert run(fake, u)[0].status == "stored"
    assert fake.current(EVD, s.blob_key(sc["data"]["sha256"]))["data"] == BIG


def test_the_sniffed_head_is_gathered_across_short_reads(fake):
    fake.max_read = 1000
    data = b"a" * 1019 + b"%PDF-" + b"b" * 100  # a PDF only to a sniffer that sees all 1024 bytes
    u, sc = stage(fake, data)
    assert sc["checks"]["sniffed_type"] == "pdf"
    assert run(fake, u)[0].status == "stored"


def test_a_compliance_lock_is_recorded_as_read():
    fake = new_fake(lock_mode="COMPLIANCE")
    u, _ = stage(fake, CSV)
    assert run(fake, u)[0].status == "stored"
    assert record_of(fake, u)["evidence"]["lock_mode"] == "COMPLIANCE"


def test_identical_bytes_from_a_second_source_add_only_a_record(fake):
    u1, sc1 = stage(fake, CSV)
    run(fake, u1)
    other = make_sidecar(CSV, "x", filename="copy.csv")
    other["source"].update(kind="portal", platform="nextrequest", host="sanmateo.nextrequest.com",
                           request_id="26-217", request_url="https://sanmateo.nextrequest.com/requests/26-217")
    u2, _ = stage(fake, CSV, sidecar=other)
    out, _ = run(fake, u2)
    assert out.status == "already_stored"
    assert len(fake.versions(EVD, s.blob_key(sc1["data"]["sha256"]))) == 1
    assert len(blob_writes(fake)) == 1
    assert record_of(fake, u2)["evidence"]["version_id"] == record_of(fake, u1)["evidence"]["version_id"]
    assert staging_tags(fake, u2)[1] == s.ingested_tags(sc1["data"]["sha256"])


DUPLICATES = [  # (data, part_size, mp): every staging layout a duplicate can arrive in
    (CSV, None, False), (CSV, None, True), (BIG, 5 * MiB, False), (BIG, 5 * MiB, True)]


@pytest.mark.parametrize("data,part_size,mp", DUPLICATES, ids=["put", "put-mp", "multipart", "multipart-mp"])
def test_a_duplicates_bytes_are_read_and_its_claims_checked(fake, data, part_size, mp):
    u1, _ = stage(fake, data, part_size=part_size, mp=mp)
    run(fake, u1)
    u2, _ = stage(fake, data, part_size=part_size, mp=mp)
    fake.calls.clear()
    assert run(fake, u2)[0].status == "already_stored"
    assert fake.bytes_read[(STG, s.staging_data_key(u2))] == len(data)
    for lie in (lie_sniff, lie_mp if mp else None, lie_md5 if part_size else None):
        if lie:
            u, _ = stage(fake, data, part_size=part_size, mp=mp, edit=lie)
            assert_rejected(fake, u, run(fake, u)[0], "data_mismatch")


def test_a_retry_after_success_changes_nothing(fake):
    u, _ = stage(fake, CSV)
    run(fake, u)
    before = {k: len(fake.buckets[EVD].objects[k]) for k in fake.keys(EVD)}
    fake.calls.clear()
    out, _ = run(fake, u)
    assert out.status == "recorded"
    assert not [c for c in fake.calls if c[0] in ("put_object", "complete_multipart_upload")]
    assert {k: len(fake.buckets[EVD].objects[k]) for k in fake.keys(EVD)} == before


def test_a_record_whose_staging_objects_expired_is_done(fake):
    u, _ = stage(fake, CSV)
    run(fake, u)
    fake.delete_object(Bucket=STG, Key=s.staging_data_key(u))
    fake.delete_object(Bucket=STG, Key=s.staging_sidecar_key(u))
    assert run(fake, u)[0].status == "recorded"


def test_nothing_to_do_without_a_sidecar(fake):
    u = next_uuid()
    assert run(fake, u)[0] == ig.Outcome("vanished", u)
    ing, _ = ingest(fake)
    assert ing.process("in/not-a-uuid.json").status == "vanished"
    staged, _ = stage(fake, CSV)
    fake.calls.clear()
    assert ing.process(s.staging_data_key(staged)) == ig.Outcome("vanished", staged)
    assert not fake.calls and not fake.keys(EVD)


def test_an_unstorable_principal_is_left_out(fake):
    fake.principal = "AWS:bad\nprincipal"
    u, _ = stage(fake, CSV)
    run(fake, u)
    assert record_of(fake, u)["ingest"]["principal"] is None


# --- Configuration and entry points ---------------------------------------------------------


@pytest.mark.parametrize("change", [
    {"code_sha256": "3q2+7w" + "A" * 38},  # Lambda's own CodeSha256 is base64
    {"code_sha256": "C" * 64},
    {"staging_bucket": EVD},
    {"evidence_bucket": s.bucket_name("evidence", "prod", ACCOUNT, REGION)},
    {"ops_bucket": "sm-alpr-pra-assets"},
    {"evidence_bucket": s.bucket_name("evidence", "dev", ACCOUNT, "us-east-1")},
    {"evidence_bucket": s.bucket_name("evidence", "dev", "210987654321", REGION)},
    {"code_sha256": "c" * 65},
])
def test_a_configuration_that_would_fail_every_record_is_refused(fake, change):
    kw = dict(staging_bucket=STG, evidence_bucket=EVD, ops_bucket=OPS, code_sha256=CODE)
    with pytest.raises(ValueError):
        ig.Ingest(fake, **{**kw, **change})


class FakeSQS:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def change_message_visibility(self, **kw):
        self.calls.append(("change_message_visibility", kw))
        if self.fail:
            raise RuntimeError("sqs down")

    def send_message(self, **kw):
        self.calls.append(("send_message", kw))


QUEUE = "https://sqs.us-west-2.amazonaws.com/123456789012/intake"


@pytest.fixture
def aws(fake, monkeypatch):
    """_client() hands out role views of the fake, and the env names the buckets."""
    sqs = FakeSQS()
    monkeypatch.setattr(ig, "_client", lambda name: fake.as_role(LAMBDA | SWEEPER) if name == "s3" else sqs)
    for var, bucket in (("STAGING_BUCKET", STG), ("EVIDENCE_BUCKET", EVD), ("OPS_BUCKET", OPS)):
        monkeypatch.setenv(var, bucket)
    monkeypatch.setenv("CODE_SHA256", CODE)
    monkeypatch.setenv("QUEUE_URL", QUEUE)
    monkeypatch.setattr(ig, "_DEFAULT", None)
    return sqs


def test_no_code_hash_is_recorded_as_none(fake, aws, monkeypatch):
    monkeypatch.setenv("CODE_SHA256", "")
    assert ig.Ingest.from_env().code_sha256 is None
    u, _ = stage(fake, CSV)
    assert ig.handler({"Records": [sqs_message({"staging_key": s.staging_sidecar_key(u)})]}) == {
        "batchItemFailures": []}
    assert record_of(fake, u)["ingest"]["code_sha256"] is None


def test_from_env_refuses_an_evidence_bucket_without_default_retention(fake, aws):
    fake.buckets[EVD].lock_days = None
    with pytest.raises(Exception):
        ig.Ingest.from_env()


def test_a_cold_start_failure_retries_its_messages_soon(fake, aws):
    u, _ = stage(fake, CSV)
    fake.buckets.pop(OPS)
    event = {"Records": [sqs_message(fake.event(STG, s.staging_sidecar_key(u)), "m-a"),
                         sqs_message({"staging_key": s.staging_sidecar_key(u)}, "m-b")]}
    with pytest.raises(Exception):
        ig.handler(event)
    assert [c[1]["ReceiptHandle"] for c in aws.calls] == ["rh-m-a", "rh-m-b"]
    assert all(c[1]["VisibilityTimeout"] == 60 for c in aws.calls)


def test_from_env_checks_every_bucket_exists(fake, aws):
    ing = ig.Ingest.from_env()
    assert (ing.staging, ing.evidence, ing.ops, ing.code_sha256) == (STG, EVD, OPS, CODE)
    assert [c[1] for c in fake.ops("head_bucket")] == [STG, EVD, OPS]
    fake.buckets.pop(OPS)
    with pytest.raises(Exception):
        ig.Ingest.from_env()


def test_the_job_ingests_a_deferred_file_and_reports_by_exit_code(fake, aws, capsys, monkeypatch):
    monkeypatch.setattr(ig, "LAMBDA_MAX_SIZE", MiB)
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    assert run(fake, u, max_lambda_size=MiB)[0].status == "deferred"
    assert ig.main(["process", s.staging_sidecar_key(u)]) == 1  # the job's own limit still applies
    assert ig.main(["process", s.staging_sidecar_key(u), "--allow-large"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "stored"
    assert ig.main(["process", s.staging_sidecar_key(u)]) == 0  # recorded: nothing left to do
    u2, _ = stage(fake, b"x\n", raw_sidecar=b"{}")
    assert ig.main(["process", s.staging_sidecar_key(u2)]) == 1
    u3, _ = stage(fake, b"y\n")
    fake.fail("put_object", code="InternalError", status=500, when=lambda kw: kw["Key"].startswith(s.BLOB_PREFIX))
    assert ig.main(["process", s.staging_sidecar_key(u3)]) == ig.EX_TEMPFAIL
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "transient"
    assert ig.main(["process", s.staging_sidecar_key(next_uuid())]) == 1  # vanished


def test_the_counting_sweep_changes_nothing(fake, aws, capsys):
    u, _ = stage(fake, CSV)
    fake.tick(7200)
    fake.calls.clear()
    assert ig.main(["sweep"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["redriven"] == 1
    assert not fake.ops("put_object_tagging") and not aws.calls
    assert staging_tags(fake, u) == ({}, {})


def test_the_scheduled_sweep_redrives_through_the_queue(fake, aws):
    u, _ = stage(fake, CSV)
    fake.tick(7200)
    context = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 600_000)
    counts = ig.sweep_handler({}, context)
    assert counts["redriven"] == 1
    assert aws.calls == [("send_message", {"QueueUrl": QUEUE,
                                           "MessageBody": json.dumps({"staging_key": s.staging_sidecar_key(u)})})]


def test_the_scheduled_sweep_keeps_to_its_time_budget(fake, aws):
    stage(fake, CSV)
    fake.tick(7200)
    counts = ig.sweep_handler({}, types.SimpleNamespace(get_remaining_time_in_millis=lambda: ig.SWEEP_RESERVE_MS - 1))
    assert (counts["redriven"], counts["listed"]) == (0, False) and not aws.calls  # stopped before listing


def test_the_handler_builds_its_ingest_once_per_container(fake, aws):
    u, _ = stage(fake, CSV)
    event = {"Records": [sqs_message(fake.event(STG, s.staging_sidecar_key(u)))]}
    assert ig.handler(event) == {"batchItemFailures": []}
    assert ig.handler(event) == {"batchItemFailures": []}
    assert len(fake.ops("head_bucket")) == 3
    assert record_of(fake, u) is not None


def test_the_ingest_role_writes_only_blobs_and_records():
    writes = {(b, p) for a, b, p in LAMBDA if a == "s3:PutObject"}
    assert writes == {(EVD, s.BLOB_PREFIX), (EVD, s.RECORD_PREFIX)}
    assert not any(a.startswith("s3:Delete") or a == "s3:BypassGovernanceRetention" for a, _, _ in LAMBDA | SWEEPER)
    assert {(b, p) for a, b, p in LAMBDA | SWEEPER if a == "s3:PutObjectTagging"} == {(STG, s.STAGING_PREFIX)}


def test_the_boto3_client_is_configured_for_streamed_uploads(monkeypatch):
    seen = {}

    class Config:
        def __init__(self, **kw):
            seen.update(kw)
    config_module = types.ModuleType("botocore.config")
    config_module.Config = Config
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name, config: (name, config)))
    monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
    monkeypatch.setitem(sys.modules, "botocore.config", config_module)
    name, config = ig._client("s3")
    assert name == "s3" and isinstance(config, Config)
    assert seen["s3"] == {"payload_signing_enabled": False}  # the streamed body can't be hashed ahead
    assert seen["request_checksum_calculation"] == seen["response_checksum_validation"] == "when_required"
    assert seen["max_pool_connections"] > ig.WORKERS
    assert seen["retries"] == {"mode": "standard", "total_max_attempts": 3}


# --- Crashes and retries -----------------------------------------------------------------

STEPS = [
    ("get_object", lambda kw: kw["Key"].endswith(".json") and kw["Bucket"] == STG),
    ("get_object", lambda kw: kw["Key"].startswith(s.RECORD_PREFIX)),
    ("head_object", lambda kw: kw["Bucket"] == STG),
    ("head_object", lambda kw: kw["Bucket"] == EVD and kw["VersionId"] is None),
    ("get_object", lambda kw: kw["Key"].endswith(".bin")),
    ("put_object", lambda kw: kw["Key"].startswith(s.BLOB_PREFIX)),
    ("head_object", lambda kw: kw["Bucket"] == EVD and kw["VersionId"] is not None),
    ("put_object", lambda kw: kw["Key"].startswith(s.RECORD_PREFIX)),
    ("put_object_tagging", lambda kw: kw["Key"].endswith(".bin")),
    ("put_object_tagging", lambda kw: kw["Key"].endswith(".json")),
]


@pytest.mark.parametrize("op,when", STEPS, ids=[f"{op}-{i}" for i, (op, _) in enumerate(STEPS)])
def test_a_failure_at_any_step_is_retried_to_the_same_end(fake, op, when):
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    fake.fail(op, when=when)
    with pytest.raises(ig.Transient):
        run(fake, u)
    if record_of(fake, u) is None:  # never tagged ingested before the record exists
        assert staging_tags(fake, u) == ({}, {})
    else:
        assert all(t in ({}, s.ingested_tags(sc["data"]["sha256"])) for t in staging_tags(fake, u))
    out, _ = run(fake, u)
    assert out.status in ("stored", "already_stored", "recorded")
    assert len(fake.versions(EVD, s.blob_key(sc["data"]["sha256"]))) == 1
    assert record_of(fake, u)["sha256"] == sc["data"]["sha256"]
    assert staging_tags(fake, u) == (s.ingested_tags(sc["data"]["sha256"]),) * 2


def test_a_missing_bucket_is_never_read_as_a_missing_key(fake):
    u, _ = stage(fake, CSV)
    fake.fail("get_object", code="NoSuchBucket", status=404, when=lambda kw: kw["Key"] == s.record_key(u))
    with pytest.raises(ig.Transient):
        ingest(fake)[0].process(s.staging_sidecar_key(u))
    assert_untouched(fake, u)
    assert not blob_writes(fake)


def test_a_403_is_never_read_as_a_missing_key(fake):
    u, _ = stage(fake, CSV)
    ing, _ = ingest(fake, role=LAMBDA - {("s3:ListBucket", EVD, None)})
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u))
    assert_untouched(fake, u)
    assert not blob_writes(fake)


def test_a_schema_error_while_recording_is_retried_and_says_why(fake, monkeypatch):
    u, _ = stage(fake, CSV)

    def refuse(*a, **kw):
        raise s.SchemaError("invalid_metadata", "record.ingest.principal", "a problem")
    monkeypatch.setattr(s, "build_record", refuse)
    ing, logs = ingest(fake)
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u))
    line = json.loads(logs[-1])
    assert (line["event"], line["reason"], line["field"]) == ("transient", "invalid_metadata",
                                                              "record.ingest.principal")
    assert line["where"][-1].startswith("ingest.py:") and line["where"][-1].endswith(":_process")
    assert_untouched(fake, u)


def test_a_record_that_412s_then_cant_be_read_is_retried(fake):
    u, _ = stage(fake, CSV)
    other, _ = ingest(fake)

    def racer(kw):
        other.process(s.staging_sidecar_key(u))
        fake.fail("get_object", code="NoSuchKey", status=404, when=lambda k: k["Key"] == s.record_key(u))
    fake.before("put_object", racer, when=lambda kw: kw["Key"] == s.record_key(u))
    with pytest.raises(ig.Transient):
        ingest(fake)[0].process(s.staging_sidecar_key(u))


def test_a_racing_record_from_another_run_of_the_same_upload_is_quiet(fake):
    u, _ = stage(fake, CSV)
    other = ig.Ingest(fake.as_role(LAMBDA), staging_bucket=STG, evidence_bucket=EVD, ops_bucket=OPS,
                      code_sha256="d" * 64, now=lambda: fake.now, log=lambda line: None)
    fake.before("put_object", lambda kw: other.process(s.staging_sidecar_key(u)),
                when=lambda kw: kw["Key"] == s.record_key(u))
    out, logs = run(fake, u)
    assert out.status == "recorded"
    assert "record_core_differs" not in [json.loads(line)["event"] for line in logs]  # only `ingest` differs
    assert record_of(fake, u)["ingest"]["code_sha256"] == "d" * 64


def record_elsewhere(u, data, **kw):
    """The record another world wrote for uuid u over `data` (as a reused uuid would leave)."""
    other = new_fake()
    stage(other, data, u=u, **kw)
    run(other, u)
    return other.current(EVD, s.record_key(u))["data"]


def plant(fake, key, raw):
    fake.put_object(Bucket=EVD, Key=key, Body=raw, ChecksumSHA256=b64_sha256(raw), IfNoneMatch="*")


def test_a_racing_record_for_another_sidecar_over_the_same_bytes_is_a_reused_uuid(fake):
    u, _ = stage(fake, CSV)
    raw = record_elsewhere(u, CSV, filename="other.csv")
    fake.before("put_object", lambda kw: plant(fake, s.record_key(u), raw),
                when=lambda kw: kw["Key"] == s.record_key(u))
    ing, logs = ingest(fake)
    out = ing.process(s.staging_sidecar_key(u))
    assert (out.status, out.reason) == ("rejected", "uuid_reused")
    assert last_field(logs) == "staging.sidecar"


def test_a_rejection_racing_a_record_for_other_bytes_ends_uuid_reused(fake):
    u, _ = stage(fake, CSV, raw_sidecar=b"{}")
    raw = record_elsewhere(u, b"other\n")
    fake.before("put_object_tagging", lambda kw: plant(fake, s.record_key(u), raw),
                when=lambda kw: kw["Key"] == s.staging_data_key(u))
    out = ingest(fake)[0].process(s.staging_sidecar_key(u))
    assert (out.status, out.reason) == ("rejected", "uuid_reused")
    assert staging_tags(fake, u) == (s.rejected_tags("uuid_reused"),) * 2


def test_the_data_object_is_tagged_before_the_sidecar(fake):
    u, _ = stage(fake, CSV)
    run(fake, u)
    tagged = [c[2] for c in fake.ops("put_object_tagging")]
    assert tagged == [s.staging_data_key(u), s.staging_sidecar_key(u)]


@pytest.mark.parametrize("path", ["sidecar", "fresh", "duplicate", "manifest", "large"])
def test_a_dropped_stream_is_retried_not_rejected(fake, monkeypatch, path):
    if path == "large":
        monkeypatch.setattr(s, "SINGLE_PUT_MAX", 10 * MiB)
    if path == "duplicate":
        first, _ = stage(fake, BIG, part_size=5 * MiB)
        run(fake, first)
    if path == "manifest":
        u, _ = stage_manifest(fake, [held(fake, CSV, "a.csv")])
    else:
        u, _ = stage(fake, BIG, part_size=5 * MiB)
    key = s.staging_sidecar_key(u) if path == "sidecar" else s.staging_data_key(u)
    fake.truncate(STG, key, 100)
    with pytest.raises(ig.Transient):
        run(fake, u)
    assert_untouched(fake, u)
    fake.truncate(STG, key, None)
    assert run(fake, u)[0].status in ("stored", "already_stored")


def test_a_failure_while_rejecting_is_retried(fake):
    u, _ = stage(fake, CSV, raw_sidecar=b"{}")
    fake.fail("put_object_tagging")
    with pytest.raises(ig.Transient):
        run(fake, u)
    assert_rejected(fake, u, run(fake, u)[0], "bad_sidecar")


@pytest.mark.parametrize("code,status", [("ConditionalRequestConflict", 409), ("InternalError", 500),
                                         ("AccessDenied", 403)])
def test_s3_errors_on_the_blob_write_are_retried(fake, code, status):
    u, _ = stage(fake, CSV)
    fake.fail("put_object", code=code, status=status, when=lambda kw: kw["Key"].startswith(s.BLOB_PREFIX))
    ing, logs = ingest(fake)
    fake.calls.clear()
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u))
    line = json.loads(logs[-1])
    assert (line["event"], line["code"], line["status"]) == ("transient", code, status)
    assert line["where"][-1].endswith(":_store_whole")
    assert len([c for c in fake.ops("get_object", STG) if c[2] == s.staging_data_key(u)]) == 1
    assert_untouched(fake, u)
    assert run(fake, u)[0].status == "stored"


@pytest.mark.parametrize("code,status", [("ConditionalRequestConflict", 409), ("InternalError", 500)])
def test_s3_errors_on_the_record_write_are_retried(fake, code, status):
    u, _ = stage(fake, CSV)
    fake.fail("put_object", code=code, status=status, when=lambda kw: kw["Key"] == s.record_key(u))
    ing, logs = ingest(fake)
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u))
    line = json.loads(logs[-1])
    assert (line["code"], line["status"]) == (code, status) and line["where"][-1].endswith(":_put_record")
    assert_untouched(fake, u)
    assert run(fake, u)[0].status == "already_stored"


def test_a_blob_the_read_back_cant_find_is_retried(fake):
    u, _ = stage(fake, CSV)
    fake.fail("head_object", code="404", status=404, when=lambda kw: kw["VersionId"] is not None)
    with pytest.raises(ig.Transient):
        run(fake, u)
    assert_untouched(fake, u)


def test_a_blob_stored_by_a_racing_writer_is_verified_and_used(fake):
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    key = s.blob_key(sc["data"]["sha256"])

    def racer(kw):
        fake.put_object(Bucket=EVD, Key=key, Body=BIG, ChecksumSHA256=b64_sha256(BIG), IfNoneMatch="*",
                        ContentType=s.EVIDENCE_CONTENT_TYPE, ContentDisposition=s.EVIDENCE_CONTENT_DISPOSITION)
    fake.before("put_object", racer, when=lambda kw: kw["Key"] == key)
    out, _ = run(fake, u)
    assert out.status == "already_stored"
    assert record_of(fake, u)["evidence"]["version_id"] == fake.current(EVD, key)["version_id"]
    assert fake.bytes_read[(STG, s.staging_data_key(u))] == 2 * len(BIG)  # the refused body, then the check


def test_a_record_written_by_a_racing_run_is_accepted(fake):
    u, sc = stage(fake, CSV)
    ing, _ = ingest(fake)
    fake.before("put_object", lambda kw: ing.process(s.staging_sidecar_key(u)),
                when=lambda kw: kw["Key"] == s.record_key(u))
    out, _ = run(fake, u)
    assert out.status == "recorded"
    assert staging_tags(fake, u) == (s.ingested_tags(sc["data"]["sha256"]),) * 2


def test_a_racing_record_that_didnt_tag_yet_is_finished(fake):
    u, sc = stage(fake, CSV)
    ing, _ = ingest(fake)

    def racer(kw):
        ing.process(s.staging_sidecar_key(u))
        for key in (s.staging_data_key(u), s.staging_sidecar_key(u)):  # as if it crashed before tagging
            fake.put_object_tagging(Bucket=STG, Key=key, Tagging={"TagSet": []})
    fake.before("put_object", racer, when=lambda kw: kw["Key"] == s.record_key(u))
    assert run(fake, u)[0].status == "recorded"
    assert staging_tags(fake, u) == (s.ingested_tags(sc["data"]["sha256"]),) * 2


def test_a_racing_record_for_another_blob_version_stands(fake):
    u, sc = stage(fake, CSV)
    key = s.blob_key(sc["data"]["sha256"])
    ing, _ = ingest(fake)

    def racer(kw):  # an admin re-versions the blob, then another run records the new version
        fake.delete_object(Bucket=EVD, Key=key)
        fake.put_object(Bucket=EVD, Key=key, Body=CSV, ChecksumSHA256=b64_sha256(CSV), IfNoneMatch="*",
                        ContentType=s.EVIDENCE_CONTENT_TYPE, ContentDisposition=s.EVIDENCE_CONTENT_DISPOSITION)
        ing.process(s.staging_sidecar_key(u))
    fake.before("put_object", racer, when=lambda kw: kw["Key"] == s.record_key(u))
    out, logs = run(fake, u)
    assert out.status == "recorded"
    assert "record_core_differs" in [json.loads(line)["event"] for line in logs]
    assert record_of(fake, u)["evidence"]["version_id"] == fake.current(EVD, key)["version_id"]


def test_a_rejection_that_loses_a_race_to_a_record_is_undone(fake):
    missing = blob(1000, 5)
    entry = s.manifest_entry("m.bin", status="held", sha256=hashlib.sha256(missing).hexdigest(), size=len(missing))
    u, sc = stage_manifest(fake, [entry])
    racer, _ = ingest(fake)

    def race(kw):  # the missing file lands, and a racing run records the manifest
        m, _ = stage(fake, missing)
        racer.process(s.staging_sidecar_key(m))
        assert racer.process(s.staging_sidecar_key(u)).status == "stored"
    fake.before("put_object_tagging", race, when=lambda kw: kw["Key"] == s.staging_data_key(u))
    out, _ = run(fake, u)
    assert out.status == "recorded"
    assert staging_tags(fake, u) == (s.ingested_tags(sc["data"]["sha256"]),) * 2


def test_a_deferral_that_loses_a_race_to_the_job_is_undone(fake):
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    job, _ = ingest(fake)
    fake.before("put_object_tagging", lambda kw: job.process(s.staging_sidecar_key(u), allow_large=True),
                when=lambda kw: kw["Key"] == s.staging_data_key(u))
    out, _ = run(fake, u, max_lambda_size=MiB)
    assert out.status == "recorded"
    assert staging_tags(fake, u) == (s.ingested_tags(sc["data"]["sha256"]),) * 2


def test_a_staging_object_replaced_mid_ingest_is_never_copied(fake):
    u, sc = stage(fake, BIG, part_size=5 * MiB, mp=False)

    def replace(kw):
        fake.delete_object(Bucket=STG, Key=s.staging_data_key(u))
        fake.put_object(Bucket=STG, Key=s.staging_data_key(u), Body=blob(len(BIG), 9), IfNoneMatch="*",
                        ChecksumSHA256=b64_sha256(blob(len(BIG), 9)),
                        Metadata=s.staging_metadata(sc["source"], u, FETCH))
    fake.before("get_object", replace, when=lambda kw: kw["Key"] == s.staging_data_key(u))
    with pytest.raises(ig.Transient):  # If-Match fails the GET
        run(fake, u)
    assert_rejected(fake, u, run(fake, u)[0], "data_mismatch")
    assert not fake.keys(EVD)


# --- Object Lock -----------------------------------------------------------------------------


def test_a_role_that_cant_see_the_lock_retries_instead_of_rejecting(fake):
    u, _ = stage(fake, CSV)
    ing, logs = ingest(fake, role=LAMBDA - {("s3:GetObjectRetention", EVD, s.BLOB_PREFIX)})
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u))
    assert "lock_missing" in json.loads(logs[-1])["error"]
    assert_untouched(fake, u)
    assert run(fake, u)[0].status == "already_stored"  # the grant fixed, the same upload goes through


def test_a_role_that_cant_read_versions_retries(fake):
    u, _ = stage(fake, CSV)
    ing, _ = ingest(fake, role=LAMBDA - {("s3:GetObjectVersion", EVD, s.BLOB_PREFIX)})
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u))
    assert_untouched(fake, u)


def test_a_bucket_without_object_lock_retries(fake):
    fake.buckets[EVD].lock_days = None
    u, _ = stage(fake, CSV)
    with pytest.raises(ig.Transient):
        run(fake, u)
    assert_untouched(fake, u)


def test_a_resighting_renews_a_lapsed_lock():
    """Dev's one-day lock: the unchanged manifest, re-uploaded on day 2, must
    still get a record (the cross-review's high finding)."""
    fake = new_fake(lock_days=1)
    u1, sc = stage(fake, CSV)
    run(fake, u1)
    key = s.blob_key(sc["data"]["sha256"])
    fake.tick(2 * 86400)
    assert fake.current(EVD, key)["lock_until"] < fake.now
    u2, _ = stage(fake, CSV)
    out, logs = run(fake, u2)
    assert out.status == "already_stored"
    blob_version = fake.current(EVD, key)
    assert blob_version["lock_mode"] == "GOVERNANCE" and blob_version["lock_until"] > fake.now + timedelta(hours=23)
    assert record_of(fake, u2)["evidence"]["retain_until"] == s.format_timestamp(blob_version["lock_until"])
    assert "lock_renewed" in [json.loads(line)["event"] for line in logs]


@pytest.mark.parametrize("elapsed,renewed", [(timedelta(hours=11), False), (timedelta(hours=13), True)])
def test_a_lock_is_renewed_once_under_half_its_period_is_left(elapsed, renewed):
    fake = new_fake(lock_days=1)
    u1, sc = stage(fake, CSV)
    run(fake, u1)
    fake.tick(int(elapsed.total_seconds()))
    u2, _ = stage(fake, CSV)
    fake.calls.clear()
    assert run(fake, u2)[0].status == "already_stored"
    assert bool(fake.ops("put_object_retention")) is renewed


def test_a_fresh_blob_is_never_renewed(fake):
    u, _ = stage(fake, CSV)
    run(fake, u)
    assert not fake.ops("put_object_retention")


def test_a_renewal_keeps_a_compliance_lock():
    fake = new_fake(lock_mode="COMPLIANCE", lock_days=1)
    u1, sc = stage(fake, CSV)
    run(fake, u1)
    fake.tick(2 * 86400)
    u2, _ = stage(fake, CSV)
    assert run(fake, u2)[0].status == "already_stored"
    assert record_of(fake, u2)["evidence"]["lock_mode"] == "COMPLIANCE"


def test_the_role_cannot_shorten_a_lock(fake):
    u, sc = stage(fake, CSV)
    run(fake, u)
    key = s.blob_key(sc["data"]["sha256"])
    v = fake.current(EVD, key)
    with pytest.raises(Exception) as e:
        fake.as_role(LAMBDA).put_object_retention(Bucket=EVD, Key=key, VersionId=v["version_id"], Retention={
            "Mode": "GOVERNANCE", "RetainUntilDate": fake.now + timedelta(days=1)}, BypassGovernanceRetention=True)
    assert ig._code(e.value) == "AccessDenied"


# --- The body and the hashes -----------------------------------------------------------------


class Chunks:
    def __init__(self, data):
        self._data = data

    def read(self, n):
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


def test_the_upload_body_can_be_asked_where_it_is_but_never_rewound():
    sc = make_sidecar(BIG, "x", part_size=5 * MiB)
    opened = []
    body = ig._CheckedBody(lambda: opened.append(1) or Chunks(BIG), sc)
    assert (body.tell(), body.seek(0), body.seek(0, 1), body.seek(-len(BIG), 2)) == (0, 0, 0, 0)
    assert len(opened) == 1  # staying put opens nothing
    assert body.read(0) == b"" and body.tell() == 0
    assert len(body.read(1000)) == 1000 and body.tell() == 1000 and body.seek(1000) == 1000
    for args in ((1,), (-1, 1), (0, 2), (999,)):
        with pytest.raises(OSError):
            body.seek(*args)
    assert body.seek(0) == 0 and body.tell() == 0 and len(opened) == 2  # botocore's retry: from the start again
    first, rest = body.read(), body.read(None)  # READ_CHUNK at most, however asked
    assert (len(first), first + rest) == (ig.READ_CHUNK, BIG)
    assert body.read() == b"" and body.tell() == len(BIG) and body.problem is None


def test_the_upload_body_checks_claims_before_its_last_bytes():
    sc = make_sidecar(BIG, "x", part_size=5 * MiB)
    lied = copy.deepcopy(sc)
    lie_md5(lied)
    body = ig._CheckedBody(lambda: Chunks(BIG), lied)
    got = 0
    with pytest.raises(ig._ClaimMismatch):
        while chunk := body.read(MiB):
            got += len(chunk)
    assert got == len(BIG) - MiB  # the last MiB was never handed over
    assert body.problem == ("data_mismatch", "sidecar.data.md5")
    with pytest.raises(OSError):
        body.seek(0)  # a body whose claims failed is never sent again
    short = ig._CheckedBody(lambda: Chunks(BIG[:-1]), sc)
    with pytest.raises(ig.Transient):
        while short.read(MiB):
            pass
    assert short.problem is None


@pytest.mark.parametrize("size,part_size", [(0, None), (1, None), (5 * MiB, 5 * MiB), (5 * MiB + 1, 5 * MiB),
                                            (10 * MiB, 5 * MiB), (11 * MiB, 6 * MiB)])
def test_the_hashes_agree_with_what_s3_computes(size, part_size):
    data = blob(size, 7)
    sc = make_sidecar(data, "x", part_size=part_size)
    h = ig._Hashes(sc["data"], sha256=True)
    for i in range(0, len(data), 777_777):
        h.update(data[i:i + 777_777])
    assert h.md5.hexdigest() == hashlib.md5(data).hexdigest()
    assert h.sha256.hexdigest() == hashlib.sha256(data).hexdigest()
    assert h.head == data[:s.SNIFF_BYTES]
    assert h.contradiction(sc) is None


def test_the_first_contradicted_claim_is_the_one_reported():
    data = blob(11 * MiB, 3)
    sc = make_sidecar(data, "x", part_size=5 * MiB)
    order = [(lambda c: c["data"].update(size=c["data"]["size"] + 1), "data_mismatch", "sidecar.data.size"),
             (lie_sha, "sha_mismatch", "sidecar.data.sha256"),
             (lie_md5, "data_mismatch", "sidecar.data.md5"),
             (lie_mp, "data_mismatch", "sidecar.data.md5_multipart"),
             (lambda c: c["data"]["upload"]["part_sha256"].__setitem__(1, "0" * 64), "data_mismatch",
              "sidecar.data.upload.part_size"),
             (lie_sniff, "data_mismatch", "sidecar.checks.sniffed_type")]
    for i, (_, reason, field) in enumerate(order):
        lied = copy.deepcopy(sc)
        for edit, _, _ in order[i:]:
            edit(lied)
        h = ig._Hashes(lied["data"], sha256=True)
        h.update(data)
        assert h.contradiction(lied) == (reason, field)
    unhashed = ig._Hashes(sc["data"])
    unhashed.update(data)
    lied = copy.deepcopy(sc)
    lie_sha(lied)
    assert unhashed.contradiction(lied) is None  # SHA-256 is S3's to check on this path


# --- Rejections ----------------------------------------------------------------------------


def test_a_wrong_sha256_claim_is_refused_by_s3(fake):
    u, _ = stage(fake, BIG, part_size=5 * MiB, edit=lie_sha)
    assert_rejected(fake, u, run(fake, u)[0], "sha_mismatch")
    assert not fake.keys(EVD)


def test_a_wrong_sha256_claim_on_a_single_put_fails_the_binding(fake):
    u, _ = stage(fake, CSV, edit=lie_sha)
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "data_mismatch")
    assert last_field(logs) == "staging.data.checksum"


@pytest.mark.parametrize("edit,field", [
    (lie_md5, "sidecar.data.md5"),
    (lie_mp, "sidecar.data.md5_multipart"),
    (lie_sniff, "sidecar.checks.sniffed_type"),
])
def test_a_wrong_claim_fails_the_upload_before_it_completes(fake, edit, field):
    u, _ = stage(fake, BIG, part_size=5 * MiB, edit=edit)
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "data_mismatch")
    assert last_field(logs) == field
    assert not fake.keys(EVD)
    assert len(blob_writes(fake)) == 1  # attempted, never completed


@pytest.mark.parametrize("single_put_max", [s.SINGLE_PUT_MAX, 10 * MiB])  # streamed PUT, and hash-then-copy
def test_a_source_multipart_etag_is_checked_at_its_own_part_size(fake, monkeypatch, single_put_max):
    monkeypatch.setattr(s, "SINGLE_PUT_MAX", single_put_max)
    data = blob(11 * MiB, 20 + single_put_max % 7)
    at_8 = {"part_size": 8 * MiB, "etag": multipart_etag(data, 8 * MiB)}
    u, _ = stage(fake, data, part_size=5 * MiB, edit=lambda sc: sc["data"].update(md5_multipart=at_8))
    assert run(fake, u)[0].status == "stored"
    at_6 = {"part_size": 6 * MiB, "etag": multipart_etag(data, 5 * MiB + MiB // 2)}  # two parts either way
    u, _ = stage(fake, data, part_size=5 * MiB, edit=lambda sc: sc["data"].update(md5_multipart=at_6))
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "data_mismatch")
    assert last_field(logs) == "sidecar.data.md5_multipart"


def test_a_wrong_part_size_is_caught_on_the_bytes(fake, monkeypatch):
    """5.5 MiB parts claimed as 6 MiB: same part count, same digests, so only
    cutting the bytes at the claimed size shows the lie."""
    real = 5 * MiB + MiB // 2
    for seed, single_put_max in ((11, s.SINGLE_PUT_MAX), (12, 10 * MiB)):
        monkeypatch.setattr(s, "SINGLE_PUT_MAX", single_put_max)
        u, _ = stage(fake, blob(11 * MiB, seed), part_size=real,
                     edit=lambda sc: sc["data"]["upload"].update(part_size=6 * MiB))
        out, logs = run(fake, u)
        assert_rejected(fake, u, out, "data_mismatch")
        assert last_field(logs) == "sidecar.data.upload.part_size"
    assert not fake.keys(EVD) and not fake.ops("create_multipart_upload", EVD)


@pytest.mark.parametrize("raw,reason", [
    (b"{}", "bad_sidecar"),
    (b'{"schema": 1}', "bad_sidecar"),  # not canonical
    (b"\xff", "bad_sidecar"),
    (b'{"schema":2}\n', "schema_version"),
])
def test_a_sidecar_that_doesnt_parse_is_rejected(fake, raw, reason):
    u, _ = stage(fake, CSV, raw_sidecar=raw)
    assert_rejected(fake, u, run(fake, u)[0], reason)


def test_an_oversized_sidecar_is_rejected_without_reading_it_all(fake):
    raw = s.canonical_json({"pad": "x" * (2 * s.MAX_SIDECAR_BYTES)})
    u, _ = stage(fake, CSV, raw_sidecar=raw)
    assert_rejected(fake, u, run(fake, u)[0], "bad_sidecar")
    assert fake.bytes_read[(STG, s.staging_sidecar_key(u))] <= s.MAX_SIDECAR_BYTES + 1


@pytest.mark.parametrize("edit,reason", [
    (lambda sc: sc["source"].update(url="https://cdn.example.org/a.csv?X-Amz-Signature=abc"), "signed_url"),
    (lambda sc: sc["response"]["headers"].update({"set-cookie": "a=b"}), "forbidden_header"),
    (lambda sc: sc["data"].update(size=sc["data"]["size"] + 1), "invalid_metadata"),
])
def test_a_sidecar_against_write_policy_is_rejected(fake, edit, reason):
    u, _ = stage(fake, CSV, edit=edit)
    assert_rejected(fake, u, run(fake, u)[0], reason)


def test_a_sidecar_under_another_uuid_is_rejected(fake):
    u, sc = stage(fake, CSV, edit=lambda sc: sc.update(uuid="ffffffff-0000-4000-8000-000000000000"))
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "invalid_metadata")
    assert last_field(logs) == "sidecar.uuid"


def test_a_sidecar_without_its_data_is_rejected(fake):
    u = next_uuid()
    sc = make_sidecar(CSV, u)
    fake.put_object(Bucket=STG, Key=s.staging_sidecar_key(u), Body=s.canonical_json(sc), IfNoneMatch="*")
    out, _ = run(fake, u)
    assert (out.status, out.reason) == ("rejected", "no_data")
    assert fake.tags(STG, s.staging_sidecar_key(u)) == s.rejected_tags("no_data")


@pytest.mark.parametrize("field,edit", [
    ("staging.data.etag", lambda h: {**h, "ETag": '"' + "1" * 32 + '"'}),
    ("staging.data.size", lambda h: {**h, "ContentLength": h["ContentLength"] + 1}),
    ("staging.data.sse", lambda h: {**h, "ServerSideEncryption": "aws:kms"}),
    ("staging.data.checksum", lambda h: {**h, "ChecksumType": "FULL_OBJECT"}),
    ("staging.data.checksum", lambda h: {**h, "ChecksumSHA256": s.composite_sha256(["0" * 64] * 3)}),
    ("staging.data.checksum", lambda h: {k: v for k, v in h.items() if not k.startswith("Checksum")}),
    ("staging.metadata", lambda h: {**h, "Metadata": {**h["Metadata"], "request-id": "999"}}),
    ("staging.metadata", lambda h: {**h, "Metadata": {}}),
])
def test_a_data_object_that_isnt_the_sidecars_is_rejected(fake, field, edit):
    u, _ = stage(fake, BIG, part_size=5 * MiB)
    fake.edit_head(STG, s.staging_data_key(u), edit)
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "data_mismatch")
    assert last_field(logs) == field
    assert not fake.keys(EVD)


def restage(fake, u, data, **kw):
    """An admin clears a uuid's staging objects and a writer reuses the uuid."""
    fake.delete_object(Bucket=STG, Key=s.staging_data_key(u))
    fake.delete_object(Bucket=STG, Key=s.staging_sidecar_key(u))
    return stage(fake, data, u=u, **kw)


def test_a_reused_uuid_is_rejected_and_the_record_kept(fake):
    u, sc = stage(fake, CSV)
    run(fake, u)
    first = fake.current(EVD, s.record_key(u))["data"]
    restage(fake, u, b"other bytes\n")
    out, _ = run(fake, u)
    assert (out.status, out.reason) == ("rejected", "uuid_reused")
    assert staging_tags(fake, u) == (s.rejected_tags("uuid_reused"),) * 2
    assert fake.current(EVD, s.record_key(u))["data"] == first


def test_a_reused_uuid_with_the_same_bytes_is_caught_by_its_sidecar(fake):
    u, _ = stage(fake, CSV)
    run(fake, u)
    restage(fake, u, CSV, filename="renamed.csv")  # same data object ETag, another sidecar
    out, logs = run(fake, u)
    assert (out.status, out.reason) == ("rejected", "uuid_reused")
    assert last_field(logs) == "staging.sidecar"


def test_a_reused_uuid_is_caught_from_the_data_object_alone(fake):
    u, sc = stage(fake, CSV)
    run(fake, u)
    fake.delete_object(Bucket=STG, Key=s.staging_data_key(u))
    fake.delete_object(Bucket=STG, Key=s.staging_sidecar_key(u))
    fake.put_object(Bucket=STG, Key=s.staging_data_key(u), Body=b"x", ChecksumSHA256=b64_sha256(b"x"),
                    IfNoneMatch="*")
    out, logs = run(fake, u)
    assert (out.status, out.reason) == ("rejected", "uuid_reused")
    assert last_field(logs) == "staging.data"


def test_a_stored_record_that_doesnt_parse_is_retried_not_rejected(fake):
    u, _ = stage(fake, CSV)
    fake.put_object(Bucket=EVD, Key=s.record_key(u), Body=b"{}", ChecksumSHA256=b64_sha256(b"{}"),
                    IfNoneMatch="*")
    with pytest.raises(ig.Transient):
        ingest(fake)[0].process(s.staging_sidecar_key(u))
    assert staging_tags(fake, u) == ({}, {})


def test_a_record_under_another_uuids_key_is_retried(fake):
    u1, _ = stage(fake, CSV)
    run(fake, u1)
    u2, _ = stage(fake, b"second\n")
    raw = fake.current(EVD, s.record_key(u1))["data"]
    fake.put_object(Bucket=EVD, Key=s.record_key(u2), Body=raw, ChecksumSHA256=b64_sha256(raw), IfNoneMatch="*")
    with pytest.raises(ig.Transient):
        ingest(fake)[0].process(s.staging_sidecar_key(u2))
    assert staging_tags(fake, u2) == ({}, {})


def test_a_stored_record_stays_readable_after_write_policy_tightens(fake):
    u, _ = stage(fake, CSV)
    run(fake, u)
    record = record_of(fake, u)
    record["sidecar"]["response"]["headers"]["x-since-forbidden"] = "1"  # allowed once, say
    record["staging"]["sidecar_sha256"] = hashlib.sha256(s.canonical_json(record["sidecar"])).hexdigest()
    raw = s.canonical_json(record)
    with pytest.raises(s.SchemaError):
        s.parse_record(raw, stored=False)
    fake.delete_object(Bucket=EVD, Key=s.record_key(u))
    fake.put_object(Bucket=EVD, Key=s.record_key(u), Body=raw, ChecksumSHA256=b64_sha256(raw), IfNoneMatch="*")
    fake.delete_object(Bucket=STG, Key=s.staging_data_key(u))
    fake.delete_object(Bucket=STG, Key=s.staging_sidecar_key(u))
    assert run(fake, u)[0].status == "recorded"


def test_other_bytes_at_the_blob_key_are_a_conflict(fake):
    u, sc = stage(fake, CSV)
    planted = b"planted\n"
    fake.put_object(Bucket=EVD, Key=s.blob_key(sc["data"]["sha256"]), Body=planted,
                    ChecksumSHA256=b64_sha256(planted), IfNoneMatch="*")
    ing, _ = ingest(fake)  # not run(): the planted blob would fail check_evidence
    assert_rejected(fake, u, ing.process(s.staging_sidecar_key(u)), "evidence_conflict")


def test_a_delete_marker_over_the_blob_gets_a_new_version(fake):
    u1, sc = stage(fake, CSV)
    run(fake, u1)
    key = s.blob_key(sc["data"]["sha256"])
    fake.delete_object(Bucket=EVD, Key=key)  # an admin's delete marker
    u2, _ = stage(fake, CSV)
    assert run(fake, u2)[0].status == "stored"
    assert record_of(fake, u2)["evidence"]["version_id"] == fake.current(EVD, key)["version_id"]
    assert record_of(fake, u1)["evidence"]["version_id"] != record_of(fake, u2)["evidence"]["version_id"]


# --- Fetch manifests and backfills -------------------------------------------------------

MANIFEST_SOURCE = {"kind": "muckrock", "platform": "muckrock", "host": "www.muckrock.com",
                   "agency": "San Mateo", "request_id": "12345",
                   "request_url": "https://www.muckrock.com/foi/san-mateo-12345/"}


def stage_manifest(fake, entries, *, source=MANIFEST_SOURCE, body=None, part_size=None, edit=None):
    raw = body if body is not None else s.manifest_bytes(s.build_manifest(MANIFEST_SOURCE, entries))
    u = next_uuid()
    sc = make_sidecar(raw, u, origin="generated", mp=False, part_size=part_size)
    sc["content_kind"] = "fetch_manifest"
    sc["source"] = {**source, "doc_id": None, "filename": s.MANIFEST_FILENAME, "title": None, "url": None,
                    "released_on": None}
    sc["listing"] = None
    sc["checks"]["sniffed_type"] = "text"
    return stage(fake, raw, sidecar=sc, part_size=part_size, edit=edit)


def held(fake, data, name):
    u, sc = stage(fake, data)
    run(fake, u)
    return s.manifest_entry(name, status="held", sha256=sc["data"]["sha256"], size=len(data))


def test_a_manifest_of_held_files_is_stored(fake):
    entries = [held(fake, CSV, "a.csv"), held(fake, b"b\n", "b.txt"),
               s.manifest_entry("c.pdf", status="failed", reason="source_404")]
    u, sc = stage_manifest(fake, entries)
    assert run(fake, u)[0].status == "stored"
    assert record_of(fake, u)["sidecar"]["content_kind"] == "fetch_manifest"


@pytest.mark.parametrize("missing", [["00" * 32], ["ff" * 32], ["00" * 32, "ff" * 32]], ids=["first", "last", "both"])
def test_a_manifest_naming_a_file_not_held_is_rejected(fake, missing):
    entries = [held(fake, CSV, "a.csv")] + [
        s.manifest_entry(f"m{i}.txt", status="held", sha256=sha, size=2) for i, sha in enumerate(missing)]
    u, _ = stage_manifest(fake, sorted(entries, key=s.manifest_sort_key))
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "missing_blob")
    line = json.loads(logs[-1])
    assert (line["field"], line["problem"]) == ("manifest.files", f"{len(missing)} listed files aren't held")


def test_a_manifest_with_a_wrong_size_is_rejected(fake):
    entry = held(fake, CSV, "a.csv")
    u, _ = stage_manifest(fake, [{**entry, "size": entry["size"] + 1}])
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "bad_manifest")
    assert last_field(logs) == "manifest.files.size"


def test_every_listed_size_is_checked(fake):
    entries = [held(fake, CSV, "a.csv"), held(fake, b"b\n", "b.txt")]
    u, _ = stage_manifest(fake, [entries[0], {**entries[1], "size": 3}])
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "bad_manifest")


def test_a_manifest_naming_a_pdf_is_still_text(fake):
    u, sc = stage_manifest(fake, [s.manifest_entry("Invoice %PDF-export.pdf", status="failed", reason="source_404")])
    assert s.sniff_type(fake.current(STG, s.staging_data_key(u))["data"][:s.SNIFF_BYTES]) == "pdf"
    assert run(fake, u)[0].status == "stored"


def test_a_multipart_manifests_hash_is_checked_before_its_body_is_trusted(fake):
    entries = [s.manifest_entry(f"{i:05d}-" + "f" * 4000, status="failed", reason="source_404") for i in range(1400)]
    u, _ = stage_manifest(fake, entries, part_size=5 * MiB, edit=lie_sha)
    assert fake.current(STG, s.staging_data_key(u))["etag"].endswith('-2"')
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "sha_mismatch")
    assert last_field(logs) == "sidecar.data.sha256"
    assert not blob_writes(fake)  # refused before the body was trusted, not by S3


def test_a_manifest_for_another_request_is_rejected(fake):
    u, _ = stage_manifest(fake, [held(fake, CSV, "a.csv")], source={**MANIFEST_SOURCE, "request_id": "999"})
    assert_rejected(fake, u, run(fake, u)[0], "manifest_mismatch")


def test_a_manifest_body_that_isnt_a_manifest_is_rejected(fake):
    u, _ = stage_manifest(fake, [], body=b'{"kind":"fetch_manifest","schema":1}\n')
    assert_rejected(fake, u, run(fake, u)[0], "bad_manifest")


def test_a_manifest_is_held_to_write_policy(fake):
    entry = held(fake, CSV, "a.csv")
    manifest = s.build_manifest(MANIFEST_SOURCE, [entry])
    manifest["files"][0]["url"] = "https://cdn.example.org/a.csv?X-Amz-Signature=abc"
    body = s.canonical_json(manifest)
    assert s.parse_manifest(body, stored=True)  # only write policy refuses it
    u, _ = stage_manifest(fake, [], body=body)
    assert_rejected(fake, u, run(fake, u)[0], "signed_url")


def test_a_backfill_needs_its_stamped_manifest_held(fake):
    stamp = held(fake, b"stamped manifest\n", "m.json")["sha256"]
    sc = make_sidecar(CSV, "x", origin="local-copy")
    sc["fetch"]["stamp_ref"] = stamp
    u, _ = stage(fake, CSV, sidecar=sc)
    assert run(fake, u)[0].status == "stored"
    sc2 = make_sidecar(CSV, "x", origin="local-copy")
    sc2["fetch"]["stamp_ref"] = "5" * 64
    u2, _ = stage(fake, CSV, sidecar=sc2)
    assert_rejected(fake, u2, run(fake, u2)[0], "missing_blob")


# --- The cost gate, deferral and the multipart path ----------------------------------------

APPROVAL = "approvals/aaaaaaaa-0000-4000-8000-000000000000.json"


def put_approval(fake, sc, **over):
    src = sc["source"]
    body = {"schema": 1, "kind": s.APPROVAL_KIND,
            "source": {k: src[k] for k in ("kind", "platform", "host", "request_id", "doc_id", "url")},
            "max_size": 60_000_000_000, "expires_at": "2026-10-28T00:00:00Z", "approved_by": "admin",
            "note": None, **over}
    fake.put_object(Bucket=OPS, Key=APPROVAL, Body=s.canonical_json(body))


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(s, "COST_GATE", 6 * MiB)


def over_gate(data=BIG):
    sc = make_sidecar(data, "x", part_size=5 * MiB)
    sc["fetch"]["approval"] = APPROVAL
    return sc


def test_a_file_over_the_gate_goes_through_with_its_approval(fake, gate):
    sc = over_gate()
    put_approval(fake, sc)
    u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=sc)
    assert run(fake, u)[0].status == "stored"


def test_a_file_at_the_gate_needs_no_approval(fake, monkeypatch):
    monkeypatch.setattr(s, "COST_GATE", len(BIG))
    u, _ = stage(fake, BIG, part_size=5 * MiB)
    assert run(fake, u)[0].status == "stored"


@pytest.mark.parametrize("over,field", [
    (None, "sidecar.fetch.approval"),
    ({"expires_at": "2026-09-01T00:00:00Z"}, "approval.expires_at"),
    ({"source": {"kind": "muckrock", "platform": "muckrock", "host": "www.muckrock.com", "request_id": "12345",
                 "doc_id": "111", "url": "https://cdn.muckrock.com/foia_files/2026/09/28/a.csv"}},
     "approval.source.doc_id"),
    ({"kind": "something_else"}, "approval.kind"),
])
def test_a_file_over_the_gate_without_a_covering_approval_is_too_large(fake, gate, over, field):
    sc = over_gate()
    if over is not None:
        put_approval(fake, sc, **over)
    u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=sc)
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "too_large")
    assert last_field(logs) == field and json.loads(logs[-1])["problem"]


def test_the_gate_and_the_binding_come_before_deferral(fake, gate):
    u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=over_gate())  # no approval written
    assert_rejected(fake, u, run(fake, u, max_lambda_size=MiB)[0], "too_large")
    u2, _ = stage(fake, blob(5 * MiB, 4), part_size=5 * MiB, metadata={})
    assert_rejected(fake, u2, run(fake, u2, max_lambda_size=MiB)[0], "data_mismatch")


def test_references_are_checked_before_deferral(fake):
    sc = make_sidecar(BIG, "x", part_size=5 * MiB, origin="local-copy")
    sc["fetch"]["stamp_ref"] = "5" * 64
    u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=sc)
    assert_rejected(fake, u, run(fake, u, max_lambda_size=MiB)[0], "missing_blob")


def test_an_approval_covers_an_upload_committed_before_it_expired(fake, gate):
    """The cross-review's repro: approved, uploaded, deferred; the approval
    runs out an hour later and the job runs two hours later."""
    sc = over_gate()
    put_approval(fake, sc, expires_at="2026-09-28T19:00:00Z")
    u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=sc)
    assert run(fake, u, max_lambda_size=MiB)[0].status == "deferred"
    fake.tick(2 * 3600)
    assert run(fake, u, allow_large=True)[0].status == "stored"


def test_an_upload_committed_after_its_approval_expired_is_too_large(fake, gate):
    sc = over_gate()
    put_approval(fake, sc, expires_at="2026-09-28T18:30:00Z")
    fake.tick(3600)  # the writer uploads after the approval ran out
    u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=sc)
    out, logs = run(fake, u, allow_large=True)
    assert_rejected(fake, u, out, "too_large")
    assert last_field(logs) == "approval.expires_at"


def test_the_commit_time_is_the_sidecars_not_the_data_objects(fake, gate):
    """A multipart data object's LastModified is when its upload began; the
    sidecar, written last, is when the upload was committed."""
    sc = over_gate()
    put_approval(fake, sc, expires_at="2026-09-28T18:00:30Z")
    fake.before("complete_multipart_upload", lambda kw: fake.tick(60), when=lambda kw: kw["Bucket"] == STG)
    u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=sc)
    expires = s.parse_timestamp("2026-09-28T18:00:30Z")
    assert fake.current(STG, s.staging_data_key(u))["last_modified"] < expires <= fake.current(
        STG, s.staging_sidecar_key(u))["last_modified"]
    assert_rejected(fake, u, run(fake, u, allow_large=True)[0], "too_large")


@pytest.mark.parametrize("margin,status", [(timedelta(seconds=1), "stored"), (timedelta(0), "rejected")])
def test_an_approval_covers_uploads_committed_until_it_expires(fake, gate, margin, status):
    sc = over_gate()
    u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=sc)
    committed = fake.current(STG, s.staging_sidecar_key(u))["last_modified"]
    put_approval(fake, sc, expires_at=s.format_timestamp(committed + margin))
    fake.tick(7200)
    assert run(fake, u, allow_large=True)[0].status == status


def test_the_approval_is_judged_by_s3s_clock_not_the_lambdas(fake, gate):
    sc = over_gate()
    u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=sc)
    committed = fake.current(STG, s.staging_sidecar_key(u))["last_modified"]
    put_approval(fake, sc, expires_at=s.format_timestamp(committed - timedelta(seconds=1)))
    ing, _ = ingest(fake, now=lambda: committed - timedelta(hours=1))  # a Lambda clock running behind
    assert_rejected(fake, u, ing.process(s.staging_sidecar_key(u)), "too_large")


def test_a_file_too_big_for_the_lambda_is_deferred_then_done_by_the_job(fake):
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    assert run(fake, u, max_lambda_size=len(BIG))[0].status == "stored"  # at the limit: not deferred
    u, sc = stage(fake, blob(11 * MiB, 2), part_size=5 * MiB)
    out, _ = run(fake, u, max_lambda_size=10 * MiB)
    assert out.status == "deferred"
    assert staging_tags(fake, u) == (ig.DEFERRED_TAGS,) * 2
    assert record_of(fake, u) is None
    out, _ = run(fake, u, max_lambda_size=10 * MiB, allow_large=True)
    assert out.status == "stored"
    assert staging_tags(fake, u) == (s.ingested_tags(sc["data"]["sha256"]),) * 2


@pytest.fixture
def large(monkeypatch):
    """Files over 10 MiB take the multipart path, as files over 5 GB do."""
    monkeypatch.setattr(s, "SINGLE_PUT_MAX", 10 * MiB)


def test_a_file_at_single_put_max_is_one_put(fake, monkeypatch):
    monkeypatch.setattr(s, "SINGLE_PUT_MAX", len(BIG))
    u, _ = stage(fake, BIG, part_size=5 * MiB)
    assert run(fake, u)[0].status == "stored"
    assert record_of(fake, u)["evidence"]["checksum_type"] == "FULL_OBJECT"


def test_a_large_file_is_copied_on_its_own_parts(fake, large):
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    out, _ = run(fake, u)
    assert out.status == "stored"
    ev, up = record_of(fake, u)["evidence"], sc["data"]["upload"]
    assert ev["checksum_type"] == "COMPOSITE"
    assert ev["checksum_sha256"] == record_of(fake, u)["staging"]["data_checksum_sha256"]
    assert (ev["part_size"], ev["part_sha256"]) == (up["part_size"], up["part_sha256"])
    assert fake.current(EVD, ev["key"])["data"] == BIG
    assert len(fake.ops("upload_part_copy")) == 3
    assert not fake.buckets[EVD].uploads


def test_a_large_duplicate_is_verified_and_not_copied(fake, large):
    u1, _ = stage(fake, BIG, part_size=5 * MiB)
    run(fake, u1)
    u2, _ = stage(fake, BIG, part_size=5 * MiB)
    fake.calls.clear()
    assert run(fake, u2)[0].status == "already_stored"
    assert not fake.ops("create_multipart_upload")
    assert ("get_object", STG, s.staging_data_key(u2)) in fake.calls


def test_a_large_duplicate_on_other_parts_is_a_layout_conflict(fake, large):
    """v1 records one layout per blob, so this sighting can't be recorded;
    it isn't a sign the stored bytes differ."""
    u1, _ = stage(fake, BIG, part_size=5 * MiB)
    run(fake, u1)
    u2, _ = stage(fake, BIG, part_size=6 * MiB)
    out, logs = run(fake, u2)
    assert_rejected(fake, u2, out, "layout_conflict")
    assert last_field(logs) == "evidence.part_size"


def plant_parts(fake, key, data, part_size, algorithm="SHA256"):
    """An admin writes `data` at an evidence key, multipart."""
    mpu = fake.create_multipart_upload(Bucket=EVD, Key=key, **({"ChecksumAlgorithm": algorithm} if algorithm else {}))
    parts = []
    for n, i in enumerate(range(0, len(data), part_size), 1):
        chunk = data[i:i + part_size]
        got = fake.upload_part(Bucket=EVD, Key=key, UploadId=mpu["UploadId"], PartNumber=n, Body=chunk,
                               **({"ChecksumSHA256": b64_sha256(chunk)} if algorithm else {}))
        parts.append({"PartNumber": n, "ETag": got["ETag"], **(
            {"ChecksumSHA256": got["ChecksumSHA256"]} if algorithm else {})})
    fake.complete_multipart_upload(Bucket=EVD, Key=key, UploadId=mpu["UploadId"],
                                   MultipartUpload={"Parts": parts}, IfNoneMatch="*")


def conflict(fake, u):
    ing, logs = ingest(fake)  # not run(): a planted blob would fail check_evidence
    out = ing.process(s.staging_sidecar_key(u))
    return out.reason, last_field(logs)


@pytest.mark.parametrize("plant,expected", [
    (lambda f, k: plant_parts(f, k, blob(len(BIG), 8), 5 * MiB), ("evidence_conflict", "evidence.checksum_sha256")),
    (lambda f, k: plant_parts(f, k, blob(8 * MiB, 8), 5 * MiB), ("evidence_conflict", "evidence.checksum_sha256")),
    (lambda f, k: f.put_object(Bucket=EVD, Key=k, Body=b"x" * 100, ChecksumSHA256=b64_sha256(b"x" * 100),
                               IfNoneMatch="*"), ("evidence_conflict", "evidence.checksum_sha256")),
    (lambda f, k: plant_parts(f, k, BIG, 5 * MiB, algorithm=None), ("evidence_conflict", "evidence.checksum_type")),
], ids=["other-bytes-same-parts", "other-size", "single-put", "no-sha256"])
def test_a_blob_that_isnt_these_bytes_is_an_evidence_conflict(fake, large, plant, expected):
    """layout_conflict only for a same-size SHA-256 composite on other
    boundaries; anything that provably differs is evidence_conflict."""
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    plant(fake, s.blob_key(sc["data"]["sha256"]))
    assert conflict(fake, u) == expected


def test_a_different_first_part_size_alone_is_a_layout_conflict(fake, large):
    real = 5 * MiB + MiB // 2
    u1, sc = stage(fake, BIG, part_size=real)  # two parts
    run(fake, u1)
    u2, _ = stage(fake, BIG, part_size=6 * MiB)  # two parts too, cut elsewhere
    assert conflict(fake, u2) == ("layout_conflict", "evidence.part_size")


def test_a_single_part_upload_compares_its_one_parts_real_size(fake, large):
    """Part size 12 MiB for an 11 MiB file: one part of 11 MiB. Other bytes
    in one 11 MiB part at the key are an evidence conflict, not a layout one."""
    u, sc = stage(fake, BIG, part_size=12 * MiB)
    plant_parts(fake, s.blob_key(sc["data"]["sha256"]), blob(len(BIG), 8), 12 * MiB)
    assert conflict(fake, u) == ("evidence_conflict", "evidence.checksum_sha256")


@pytest.mark.parametrize("stored_parts,expected", [(6 * MiB, "layout_conflict"), (5 * MiB, "evidence_conflict")])
def test_the_layout_is_read_from_the_blob_version_read_back(fake, large, stored_parts, expected):
    """A new version lands between the read-back HEAD and the layout HEAD:
    the layout must be the version whose checksum was compared."""
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    key = s.blob_key(sc["data"]["sha256"])
    plant_parts(fake, key, BIG if stored_parts == 6 * MiB else blob(len(BIG), 8), stored_parts)

    def replace(kw):
        fake.delete_object(Bucket=EVD, Key=key)
        plant_parts(fake, key, BIG, 5 * MiB if stored_parts == 6 * MiB else 6 * MiB)
    fake.before("head_object", replace, when=lambda kw: kw.get("PartNumber") == 1)
    assert conflict(fake, u)[0] == expected


@pytest.mark.parametrize("edit,reason", [(lie_sha, "sha_mismatch"), (lie_md5, "data_mismatch"),
                                         (lie_sniff, "data_mismatch")])
def test_a_large_files_claims_are_checked_before_any_copy(fake, large, edit, reason):
    u, _ = stage(fake, BIG, part_size=5 * MiB, edit=edit)
    assert_rejected(fake, u, run(fake, u)[0], reason)
    assert not fake.ops("create_multipart_upload", EVD)


def test_a_failed_part_copy_aborts_the_upload_and_is_retried(fake, large):
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    fake.fail("upload_part_copy", when=lambda kw: kw["PartNumber"] == 2)
    with pytest.raises(ig.Transient):
        run(fake, u, workers=1)
    assert not fake.buckets[EVD].uploads and not fake.keys(EVD)
    assert run(fake, u)[0].status == "stored"


def test_a_failed_part_stops_the_copy_without_waiting_for_earlier_parts(fake, large):
    """Part 2 fails while part 1 is still copying: the parts queued behind
    them never start (Executor.map would have waited for part 1, then run
    them all)."""
    import time
    data = blob(40 * MiB, 30)
    u, _ = stage(fake, data, part_size=5 * MiB)
    fake.before("upload_part_copy", lambda kw: time.sleep(0.5), when=lambda kw: kw["PartNumber"] == 1)
    fake.fail("upload_part_copy", when=lambda kw: kw["PartNumber"] == 2)
    with pytest.raises(ig.Transient):
        run(fake, u, workers=2)
    assert len(fake.ops("upload_part_copy")) <= 3  # parts 1 and 2, and at most one a free worker took first
    assert not fake.buckets[EVD].uploads and not fake.keys(EVD)


def test_the_upload_is_aborted_only_after_parts_in_flight_land(fake, large):
    import threading
    import time
    u, _ = stage(fake, BIG, part_size=5 * MiB)
    started, seen = threading.Event(), []
    fake.fail("upload_part_copy", when=lambda kw: kw["PartNumber"] == 1 and started.wait(5))

    def slow(kw):
        started.set()
        time.sleep(0.3)
        seen.append(bool(fake.buckets[EVD].uploads))  # the upload is still open as this part lands
    fake.before("upload_part_copy", slow, when=lambda kw: kw["PartNumber"] == 2)
    with pytest.raises(ig.Transient):
        run(fake, u, workers=2)
    assert seen == [True]
    assert not fake.buckets[EVD].uploads


def test_a_failed_complete_aborts_the_upload_and_is_retried(fake, large):
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    fake.fail("complete_multipart_upload", code="InternalError", status=500)
    with pytest.raises(ig.Transient):
        run(fake, u)
    assert not fake.buckets[EVD].uploads and not fake.keys(EVD)
    assert run(fake, u)[0].status == "stored"


def test_a_source_changed_during_the_copy_isnt_taken_as_stored(fake, large):
    u, sc = stage(fake, BIG, part_size=5 * MiB)

    def replace(kw):
        fake.delete_object(Bucket=STG, Key=s.staging_data_key(u))
        fake.put_object(Bucket=STG, Key=s.staging_data_key(u), Body=blob(len(BIG), 9), IfNoneMatch="*")
    fake.before("upload_part_copy", replace)
    with pytest.raises(ig.Transient):
        run(fake, u, workers=1)
    assert not fake.keys(EVD) and not fake.buckets[EVD].uploads


def test_a_412_from_a_part_copy_is_not_taken_as_stored(fake, large):
    u, _ = stage(fake, BIG, part_size=5 * MiB)
    fake.fail("upload_part_copy", code="PreconditionFailed", status=412)
    with pytest.raises(ig.Transient):
        run(fake, u, workers=1)
    assert not fake.keys(EVD) and not fake.buckets[EVD].uploads


def test_a_large_blob_completed_by_a_racing_writer_is_used(fake, large):
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    other, _ = ingest(fake)
    u2, _ = stage(fake, BIG, part_size=5 * MiB)
    fake.before("complete_multipart_upload", lambda kw: other.process(s.staging_sidecar_key(u2)),
                when=lambda kw: kw["Bucket"] == EVD)
    fake.fail("abort_multipart_upload", code="InternalError", status=500)  # lifecycle cleans up after it
    out, _ = run(fake, u)
    assert out.status == "already_stored"
    assert record_of(fake, u)["evidence"]["version_id"] == record_of(fake, u2)["evidence"]["version_id"]


def test_a_copied_part_s3_reports_differently_is_rejected(fake, large):
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    real = fake.upload_part_copy

    def wrong(**kw):
        out = real(**kw)
        out["CopyPartResult"]["ChecksumSHA256"] = b64_sha256(b"x")
        return out
    fake.upload_part_copy = wrong
    assert_rejected(fake, u, run(fake, u, workers=1)[0], "data_mismatch")
    assert not fake.buckets[EVD].uploads


# --- The handler ---------------------------------------------------------------------------


def events_from(fake):
    got = []
    fake.notify(STG, got.append, prefix=s.STAGING_PREFIX, suffix=s.SIDECAR_SUFFIX)
    return got


def test_the_handler_ingests_what_s3_announces(fake):
    events = events_from(fake)
    u, sc = stage(fake, CSV)
    ing, _ = ingest(fake)
    assert len(events) == 1  # the sidecar only
    assert ig.handler({"Records": [sqs_message(events[0])]}, ingest=ing) == {"batchItemFailures": []}
    assert record_of(fake, u)["ingest"]["principal"] == fake.principal


def test_the_handler_decodes_keys_and_skips_what_isnt_a_new_sidecar(fake):
    u, sc = stage(fake, CSV)
    ing, _ = ingest(fake)
    event = fake.event(STG, s.staging_sidecar_key(u))
    event["Records"][0]["s3"]["object"]["key"] = s.staging_sidecar_key(u).replace("-", "%2D")
    assert ig.staging_keys(sqs_message(event), STG) == [(s.staging_sidecar_key(u), fake.principal)]
    skipped = [
        {"Service": "Amazon S3", "Event": "s3:TestEvent", "Bucket": STG},
        fake.event(STG, s.staging_data_key(u)),
        fake.event(STG, s.staging_sidecar_key(u), name="ObjectTagging:Put"),  # the Lambda's own tagging
        {"Records": [{**fake.event(STG, s.staging_sidecar_key(u))["Records"][0], "s3": {
            "bucket": {"name": EVD}, "object": {"key": s.staging_sidecar_key(u)}}}]},
        {"Records": ["not a record", {"s3": "x"}, {"eventName": "ObjectCreated:Put", "s3": {"object": 5}}]},
        {"Records": [{"eventName": 5, "s3": {"bucket": {"name": STG}, "object": {"key": s.staging_sidecar_key(u)}}},
                     {"eventName": "ObjectCreated:Put", "s3": {"bucket": {"name": STG}, "object": {"key": 5}}},
                     {"eventName": "ObjectCreated:Put", "s3": {"bucket": {"name": STG}}}]},
        {"Records": "x"}, [], "text", {"staging_key": 5},
    ]
    for body in skipped:
        assert ig.staging_keys(sqs_message(body), STG) == []
    assert ig.staging_keys({"body": "{not json"}, STG) == []
    assert ig.staging_keys({}, STG) == []
    out = ig.handler({"Records": [sqs_message(b) for b in skipped]}, ingest=ing)
    assert out == {"batchItemFailures": []} and not fake.keys(EVD)


def test_the_handler_takes_sweep_redrives(fake):
    u, _ = stage(fake, CSV)
    ing, _ = ingest(fake)
    assert ig.handler({"Records": [sqs_message({"staging_key": s.staging_sidecar_key(u)})]}, ingest=ing) == {
        "batchItemFailures": []}
    assert record_of(fake, u) is not None


def test_the_handler_reports_only_the_failed_message_and_hastens_its_retry(fake, monkeypatch):
    monkeypatch.setenv("QUEUE_URL", QUEUE)
    u1, _ = stage(fake, CSV)
    u2, _ = stage(fake, b"second\n")
    fake.fail("put_object", when=lambda kw: kw["Key"] == s.record_key(u1))
    ing, logs = ingest(fake)
    sqs = FakeSQS()
    messages = [sqs_message(fake.event(STG, s.staging_sidecar_key(u)), f"m-{i}") for i, u in enumerate((u1, u2))]
    assert ig.handler({"Records": messages}, ingest=ing, sqs=sqs) == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    assert "message_error" not in [json.loads(line)["event"] for line in logs]  # a transient failure isn't a bug
    assert sqs.calls == [("change_message_visibility", {"QueueUrl": QUEUE, "ReceiptHandle": "rh-m-0",
                                                        "VisibilityTimeout": 60})]
    assert record_of(fake, u1) is None and record_of(fake, u2) is not None


def test_a_malformed_message_doesnt_sink_its_batch(fake):
    """The cross-review's repro: a bad record before a good one."""
    u, _ = stage(fake, CSV)
    ing, _ = ingest(fake)
    good = sqs_message(fake.event(STG, s.staging_sidecar_key(u)), "good")
    bad = sqs_message({"Records": [{"s3": "x"}]}, "bad")
    assert ig.handler({"Records": [bad, good]}, ingest=ing) == {"batchItemFailures": []}
    assert record_of(fake, u) is not None


def test_a_message_the_code_cant_handle_fails_alone(fake, monkeypatch):
    monkeypatch.setenv("QUEUE_URL", QUEUE)
    u1, _ = stage(fake, CSV)
    u2, _ = stage(fake, b"second\n")
    ing, logs = ingest(fake)
    sqs = FakeSQS()
    real = ig.staging_keys

    def buggy(message, bucket):
        if message["messageId"] == "m-0":
            raise KeyError("a bug")
        return real(message, bucket)
    monkeypatch.setattr(ig, "staging_keys", buggy)
    messages = [sqs_message(fake.event(STG, s.staging_sidecar_key(u)), f"m-{i}") for i, u in enumerate((u1, u2))]
    assert ig.handler({"Records": messages}, ingest=ing, sqs=sqs) == {"batchItemFailures": [{"itemIdentifier": "m-0"}]}
    line = json.loads(logs[0])
    assert (line["event"], line["message_id"], line["error"]) == ("message_error", "m-0", "KeyError")
    assert line["where"] == [line["where"][0]] and line["where"][0].endswith(":handler")
    assert sqs.calls == [("change_message_visibility", {"QueueUrl": QUEUE, "ReceiptHandle": "rh-m-0",
                                                        "VisibilityTimeout": 60})]
    assert record_of(fake, u1) is None and record_of(fake, u2) is not None


def test_a_failing_visibility_change_still_reports_the_failure(fake, monkeypatch):
    monkeypatch.setenv("QUEUE_URL", QUEUE)
    u, _ = stage(fake, CSV)
    fake.fail("put_object", when=lambda kw: kw["Key"] == s.record_key(u))
    ing, _ = ingest(fake)
    event = {"Records": [sqs_message(fake.event(STG, s.staging_sidecar_key(u)))]}
    assert ig.handler(event, ingest=ing, sqs=FakeSQS(fail=True)) == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}


def test_the_handler_takes_no_message_it_hasnt_time_for(fake):
    u, _ = stage(fake, CSV)
    ing, _ = ingest(fake)
    event = {"Records": [sqs_message(fake.event(STG, s.staging_sidecar_key(u)))]}
    context = types.SimpleNamespace(get_remaining_time_in_millis=lambda: ig.MIN_REMAINING_MS - 1)
    fake.calls.clear()
    assert ig.handler(event, context, ingest=ing) == {"batchItemFailures": [{"itemIdentifier": "m-1"}]}
    assert not fake.calls
    context = types.SimpleNamespace(get_remaining_time_in_millis=lambda: ig.MIN_REMAINING_MS)
    assert ig.handler(event, context, ingest=ing) == {"batchItemFailures": []}
    u2, u3 = stage(fake, b"2\n")[0], stage(fake, b"3\n")[0]
    budget = iter([ig.MIN_REMAINING_MS, ig.MIN_REMAINING_MS - 1])
    context = types.SimpleNamespace(get_remaining_time_in_millis=lambda: next(budget))
    batch = {"Records": [sqs_message({"staging_key": s.staging_sidecar_key(x)}, f"m-{x}") for x in (u2, u3)]}
    assert ig.handler(batch, context, ingest=ing) == {"batchItemFailures": [{"itemIdentifier": f"m-{u3}"}]}
    assert record_of(fake, u2) is not None and record_of(fake, u3) is None


# --- The sweep ------------------------------------------------------------------------------


def sweep(fake, now=None, lines=None, **kw):
    sent, logs = [], []
    counts = ig.sweep(fake.as_role(SWEEPER), staging_bucket=STG, send=sent.append, now=now or fake.now,
                      log=logs.append, **kw)
    assert json.loads(logs[-1]) == {"event": "sweep", **counts}
    if lines is not None:
        lines.extend(json.loads(line) for line in logs[:-1])
    return counts, sent


def test_the_sweep_redrives_lost_files_and_counts_the_rest(fake):
    fake.page_size = 2
    done, _ = stage(fake, CSV)
    run(fake, done)
    rejected, _ = stage(fake, b"r\n", raw_sidecar=b"{}")
    run(fake, rejected)
    no_data = next_uuid()
    fake.put_object(Bucket=STG, Key=s.staging_sidecar_key(no_data),
                    Body=s.canonical_json(make_sidecar(CSV, no_data)), IfNoneMatch="*")
    run(fake, no_data)
    deferred, _ = stage(fake, BIG, part_size=5 * MiB)
    run(fake, deferred, max_lambda_size=MiB)
    stuck, _ = stage(fake, b"stuck\n")
    fake.put_object_tagging(Bucket=STG, Key=s.staging_sidecar_key(stuck),
                            Tagging={"TagSet": [{"Key": ig.REDRIVES_TAG, "Value": str(ig.MAX_REDRIVES)}]})
    lost, _ = stage(fake, b"lost\n")
    orphan = next_uuid()
    fake.put_object(Bucket=STG, Key=s.staging_data_key(orphan), Body=b"o", IfNoneMatch="*")
    fake.put_object(Bucket=STG, Key="in/stray.txt", Body=b"s", IfNoneMatch="*")
    ingested, _ = stage(fake, b"ingested\n")
    run(fake, ingested)
    fake.delete_object(Bucket=STG, Key=s.staging_sidecar_key(ingested))  # lifecycle took the sidecar first
    edge, _ = stage(fake, b"edge\n")
    edge_time = fake.now
    edge_orphan = next_uuid()
    fake.put_object(Bucket=STG, Key=s.staging_data_key(edge_orphan), Body=b"o", IfNoneMatch="*")
    fake.now = edge_time
    fake.tick(3600)
    young, _ = stage(fake, b"young\n")
    young_orphan = next_uuid()
    fake.put_object(Bucket=STG, Key=s.staging_data_key(young_orphan), Body=b"o", IfNoneMatch="*")
    lines = []
    counts, sent = sweep(fake, now=edge_time + timedelta(hours=1), lines=lines)  # `edge` is exactly min_age old
    by = lambda d: json.dumps(d, sort_keys=True)  # noqa: E731
    assert sorted(lines, key=by) == sorted([
        {"event": "sweep_file", "uuid": rejected, "state": "rejected", "reason": "bad_sidecar"},
        {"event": "sweep_file", "uuid": no_data, "state": "rejected", "reason": "no_data"},
        {"event": "sweep_file", "uuid": deferred, "state": "deferred"},
        {"event": "sweep_file", "uuid": stuck, "state": "stuck"},
        {"event": "sweep_file", "uuid": orphan, "state": "orphan"},
    ], key=by)
    assert sent == [{"staging_key": s.staging_sidecar_key(lost)}]
    assert counts == {"redriven": 1, "rejected": 2, "deferred": 1, "stuck": 1, "orphans": 1, "stray": 1,
                      "unread": 0, "listed": True}
    assert fake.tags(STG, s.staging_sidecar_key(lost)) == {ig.REDRIVES_TAG: "1"}
    ing, _ = ingest(fake)
    assert ig.handler({"Records": [sqs_message(sent[0])]}, ingest=ing) == {"batchItemFailures": []}
    assert record_of(fake, lost) is not None


def test_the_sweep_gives_up_on_a_file_after_max_redrives(fake):
    u, _ = stage(fake, CSV)
    fake.tick(7200)
    for n in range(1, ig.MAX_REDRIVES + 1):
        counts, sent = sweep(fake)
        assert counts["redriven"] == 1 and fake.tags(STG, s.staging_sidecar_key(u)) == {ig.REDRIVES_TAG: str(n)}
    counts, sent = sweep(fake)
    assert (counts["redriven"], counts["stuck"], sent) == (0, 1, [])


@pytest.mark.parametrize("value", ["x", "-1", "\u00b2", ""])
def test_a_garbled_redrive_count_waits_for_a_person(fake, value):
    u, _ = stage(fake, CSV)
    fake.put_object_tagging(Bucket=STG, Key=s.staging_sidecar_key(u),
                            Tagging={"TagSet": [{"Key": ig.REDRIVES_TAG, "Value": value}]})
    fake.tick(7200)
    counts, sent = sweep(fake)
    assert (counts["stuck"], counts["redriven"], sent) == (1, 0, [])


def test_a_failed_send_gives_the_redrive_back_and_still_logs(fake):
    u, _ = stage(fake, CSV)
    fake.tick(7200)
    seen, logs = [], []

    def send(msg):
        seen.append(fake.tags(STG, s.staging_sidecar_key(u)))  # counted before it goes out
        raise RuntimeError("sqs down")
    with pytest.raises(RuntimeError):
        ig.sweep(fake.as_role(SWEEPER), staging_bucket=STG, send=send, now=fake.now, log=logs.append)
    assert seen == [{ig.REDRIVES_TAG: "1"}]
    assert fake.tags(STG, s.staging_sidecar_key(u)) == {ig.REDRIVES_TAG: "0"}  # the sweep may write only its own tag
    assert json.loads(logs[-1])["event"] == "sweep"


def test_a_failed_listing_still_logs_the_counts(fake):
    stage(fake, CSV)
    fake.fail("list_objects_v2", code="InternalError", status=500)
    logs = []
    with pytest.raises(Exception):
        ig.sweep(fake.as_role(SWEEPER), staging_bucket=STG, send=lambda m: None, now=fake.now, log=logs.append)
    line = json.loads(logs[-1])
    assert (line["event"], line["listed"], line["error"]) == ("sweep", False, "FakeClientError")


def test_a_sweep_that_fails_midway_says_what_it_didnt_read(fake):
    for i in range(3):
        stage(fake, f"{i}\n".encode())
    fake.tick(7200)
    fake.fail("get_object_tagging", code="InternalError", status=500)
    logs = []
    with pytest.raises(Exception):
        ig.sweep(fake.as_role(SWEEPER), staging_bucket=STG, send=lambda m: None, now=fake.now, log=logs.append,
                 workers=1)
    line = json.loads(logs[-1])
    assert (line["listed"], line["error"], line["unread"]) == (True, "FakeClientError", 3)


def test_the_sweep_reads_tags_concurrently_and_keeps_to_its_budget(fake):
    lost = [stage(fake, f"lost {i}\n".encode())[0] for i in range(10)]
    fake.tick(7200)
    counts, sent = sweep(fake, workers=4)
    assert counts["redriven"] == 10
    assert sorted(m["staging_key"] for m in sent) == sorted(s.staging_sidecar_key(u) for u in lost)
    fresh = new_fake()
    for i in range(10):
        stage(fresh, f"lost {i}\n".encode())
    fresh.tick(7200)
    budget = iter([ig.SWEEP_RESERVE_MS] * 2 + [ig.SWEEP_RESERVE_MS - 1])  # the listing, one chunk, then out
    counts, sent = sweep(fresh, remaining_ms=lambda: next(budget), workers=4)
    assert (counts["redriven"], counts["unread"], len(sent)) == (4, 6, 4)


def test_the_sweep_stops_listing_when_out_of_time(fake):
    fake.page_size = 1
    stage(fake, CSV)
    stage(fake, b"2\n")
    fake.tick(7200)
    budget = iter([ig.SWEEP_RESERVE_MS, ig.SWEEP_RESERVE_MS - 1])
    counts, sent = sweep(fake, remaining_ms=lambda: next(budget))
    assert counts["listed"] is False and sent == []
    assert len(fake.ops("list_objects_v2")) == 1


def test_the_sweep_skips_a_file_removed_before_its_redrive(fake):
    u, _ = stage(fake, CSV)
    fake.tick(7200)
    fake.before("put_object_tagging", lambda kw: fake.delete_object(Bucket=STG, Key=kw["Key"]))
    counts, sent = sweep(fake)
    assert (counts["redriven"], sent) == (0, [])


def test_the_sweep_skips_a_file_removed_after_the_listing(fake):
    u, _ = stage(fake, CSV)
    lost, _ = stage(fake, b"lost\n")
    fake.tick(7200)
    fake.before("get_object_tagging", lambda kw: fake.delete_object(Bucket=STG, Key=kw["Key"]),
                when=lambda kw: kw["Key"] == s.staging_sidecar_key(u))
    lines = []
    counts, sent = sweep(fake, lines=lines)
    assert sent == [{"staging_key": s.staging_sidecar_key(lost)}]
    assert lines == []


def test_the_sweep_logs_only_known_reject_codes(fake):
    u, _ = stage(fake, CSV)
    for key in (s.staging_data_key(u), s.staging_sidecar_key(u)):
        fake.put_object_tagging(Bucket=STG, Key=key, Tagging={"TagSet": [
            {"Key": "intake", "Value": "rejected"}, {"Key": "reason", "Value": SENTINELS[0]}]})
    fake.tick(7200)
    lines = []
    counts, _ = sweep(fake, lines=lines)
    assert counts["rejected"] == 1
    assert lines == [{"event": "sweep_file", "uuid": u, "state": "rejected", "reason": None}]


def test_the_sweep_goes_newest_first_and_stops_in_time(fake):
    old, _ = stage(fake, b"old\n")
    fake.tick(600)
    new, _ = stage(fake, b"new\n")
    fake.tick(7200)
    budget = iter([ig.SWEEP_RESERVE_MS] * 2 + [ig.SWEEP_RESERVE_MS - 1])  # the listing, the newest file, then out
    counts, sent = sweep(fake, remaining_ms=lambda: next(budget), workers=1)
    assert sent == [{"staging_key": s.staging_sidecar_key(new)}]
    assert (counts["redriven"], counts["unread"]) == (1, 1)


def test_a_counting_sweep_changes_nothing(fake):
    u, _ = stage(fake, CSV)
    fake.tick(7200)
    fake.calls.clear()
    counts, sent = sweep(fake, redrive=False)
    assert counts["redriven"] == 1 and sent == [] and not fake.ops("put_object_tagging")


# --- What the logs may say ---------------------------------------------------------------


def test_logs_never_carry_presented_text_or_source_identifiers(fake):
    def mark(sc):
        sc["source"].update(filename=SENTINELS[0] + ".pdf", agency=SENTINELS[0], title=SENTINELS[0],
                            request_id=SENTINELS[2], host=SENTINELS[1],
                            request_url=f"https://{SENTINELS[1]}/r/{SENTINELS[2]}")
    lines = []
    for edit in (mark, lambda sc: (mark(sc), sc["data"].update(md5="0" * 32))):
        sc = make_sidecar(BIG, "x", part_size=5 * MiB)
        edit(sc)
        u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=sc)
        lines += run(fake, u)[1]
    u, _ = stage(fake, b"t\n", sidecar=None, edit=mark)
    fake.fail("put_object_tagging")
    with pytest.raises(ig.Transient):
        run(fake, u, log=lines.append)
    assert lines
    for line in lines:
        json.loads(line)
        assert not any(x in line for x in SENTINELS), line


# --- The review of b05fe2513 (.claude/reviews/pr-841-review.md) -------------------------------


def test_a_warm_container_locks_new_blobs_by_the_rule_as_it_is_now():  # M1
    fake = new_fake(lock_days=3650)
    ing, logs = ingest(fake)
    ing.check_buckets()  # a cold start reads the rule, as from_env does
    u1, _ = stage(fake, CSV)
    assert ing.process(s.staging_sidecar_key(u1)).status == "stored"
    fake.buckets[EVD].lock_days = 1  # an admin shortens the rule while the container is warm
    u2, sc2 = stage(fake, b"new content\n")
    fake.calls.clear()
    assert ing.process(s.staging_sidecar_key(u2)).status == "stored"
    assert not fake.ops("put_object_retention")  # S3 locked it by the current rule; nothing to renew
    key = s.blob_key(sc2["data"]["sha256"])
    assert fake.current(EVD, key)["lock_until"] < fake.now + timedelta(days=2)
    fake.tick(20 * 3600)
    u3, _ = stage(fake, b"new content\n")
    assert ing.process(s.staging_sidecar_key(u3)).status == "already_stored"
    until = fake.current(EVD, key)["lock_until"]
    assert fake.now + timedelta(hours=23) < until < fake.now + timedelta(days=2)  # renewed by the new rule


def test_a_version_this_run_wrote_is_never_renewed(fake):  # M1
    """S3 locked it under the rule as it was at the write, whatever its lock
    looks like now."""
    u, sc = stage(fake, CSV)
    fake.edit_head(EVD, s.blob_key(sc["data"]["sha256"]),
                   lambda h: {**h, "ObjectLockRetainUntilDate": fake.now + timedelta(hours=1)})
    assert run(fake, u)[0].status == "stored"
    assert not fake.ops("put_object_retention")


def test_a_sidecar_written_by_multipart_is_rejected(fake):  # L1
    u, sc = stage(fake, CSV)
    key = s.staging_sidecar_key(u)
    fake.delete_object(Bucket=STG, Key=key)
    body = s.canonical_json(sc)
    mpu = fake.create_multipart_upload(Bucket=STG, Key=key, ChecksumAlgorithm="SHA256")
    got = fake.upload_part(Bucket=STG, Key=key, UploadId=mpu["UploadId"], PartNumber=1, Body=body,
                           ChecksumSHA256=b64_sha256(body))
    fake.complete_multipart_upload(Bucket=STG, Key=key, UploadId=mpu["UploadId"], IfNoneMatch="*", MultipartUpload={
        "Parts": [{"PartNumber": 1, "ETag": got["ETag"], "ChecksumSHA256": got["ChecksumSHA256"]}]})
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "bad_sidecar")
    assert last_field(logs) == "staging.sidecar"


def test_ctrl_c_during_the_copy_aborts_the_upload(fake, large):  # L2
    import os
    import signal
    import time
    u, _ = stage(fake, BIG, part_size=5 * MiB)

    def interrupt(kw):
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.2)
    fake.before("upload_part_copy", interrupt, when=lambda kw: kw["PartNumber"] == 1)
    prior = signal.signal(signal.SIGINT, signal.default_int_handler)  # even if the runner ignores SIGINT
    try:
        with pytest.raises(KeyboardInterrupt):
            ingest(fake, workers=2)[0].process(s.staging_sidecar_key(u))
    finally:
        signal.signal(signal.SIGINT, prior)
    assert not fake.buckets[EVD].uploads
    assert len(fake.ops("abort_multipart_upload")) == 1


def lapsing(fake):
    """A blob stored under a one-day lock, 20 hours on: a sighting renews it."""
    u, sc = stage(fake, CSV)
    run(fake, u)
    fake.tick(20 * 3600)
    return s.blob_key(sc["data"]["sha256"])


def test_a_renewal_that_loses_to_a_longer_one_is_accepted():  # L3
    fake = new_fake(lock_days=1)
    key = lapsing(fake)

    def longer(kw):  # a racing sighting renews first, a few seconds further
        v = fake.current(EVD, key)
        fake.put_object_retention(Bucket=EVD, Key=key, VersionId=v["version_id"], Retention={
            "Mode": "GOVERNANCE", "RetainUntilDate": fake.now + timedelta(days=1, seconds=5)})
    fake.before("put_object_retention", longer)
    u2, _ = stage(fake, CSV)
    out, logs = run(fake, u2)
    assert out.status == "already_stored"
    assert "lock_renewed" not in [json.loads(line)["event"] for line in logs]


def test_a_renewal_the_role_cant_make_is_retried():  # L3, T9
    fake = new_fake(lock_days=1)
    lapsing(fake)
    fake.fail("put_object_retention", code="AccessDenied", status=403)
    u2, _ = stage(fake, CSV)
    with pytest.raises(ig.Transient):
        ingest(fake)[0].process(s.staging_sidecar_key(u2))
    assert_untouched(fake, u2)


@pytest.mark.parametrize("config,expected", [
    ({"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Years": 2}}},
     ("GOVERNANCE", timedelta(days=730))),
    ({"ObjectLockEnabled": "Enabled"}, None),
], ids=["years", "no-rule"])
def test_the_default_rule_in_either_unit_or_none(fake, config, expected):  # T9
    fake.buckets[EVD].lock_config = config
    ing, _ = ingest(fake)
    if expected is None:
        with pytest.raises(RuntimeError):
            ing.check_buckets()
    else:
        assert ing._retention() == expected


@pytest.mark.parametrize("break_it", ["grant", "rule"])
def test_the_job_exits_tempfail_when_it_cant_start(fake, aws, monkeypatch, capsys, break_it):  # L4
    if break_it == "rule":
        fake.buckets[EVD].lock_days = None
    else:
        role = (LAMBDA | SWEEPER) - {("s3:GetBucketObjectLockConfiguration", EVD, None)}
        monkeypatch.setattr(ig, "_client", lambda name: fake.as_role(role) if name == "s3" else aws)
    u, _ = stage(fake, CSV)
    assert ig.main(["process", s.staging_sidecar_key(u)]) == ig.EX_TEMPFAIL
    line = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert line["status"] == "transient" and line["where"] and line["where"][-1].endswith(":_retention")
    if break_it == "grant":
        assert (line["code"], line["status_code"]) == ("AccessDenied", 403)
    assert_untouched(fake, u)


def test_the_job_names_a_missing_variable(fake, aws, monkeypatch, capsys):  # R6
    monkeypatch.delenv("OPS_BUCKET")
    assert ig.main(["process", s.staging_sidecar_key(next_uuid())]) == ig.EX_TEMPFAIL
    line = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert (line["error"], line["variable"]) == ("KeyError", "OPS_BUCKET")


def test_a_checksum_hidden_on_this_runs_own_write_is_retried(fake):  # L6
    u, sc = stage(fake, CSV)
    fake.edit_head(EVD, s.blob_key(sc["data"]["sha256"]),
                   lambda h: {k: v for k, v in h.items() if not k.startswith("Checksum")})
    ing, logs = ingest(fake)
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u))
    assert "checksum_missing" in json.loads(logs[-1])["error"]
    assert_untouched(fake, u)


def test_botocores_retry_resends_the_whole_body(fake):  # L7
    fake.sdk_attempts = 3
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    fake.fail("put_object_body", code="InternalError", status=500, when=lambda kw: kw["Key"].startswith(s.BLOB_PREFIX))
    assert run(fake, u)[0].status == "stored"
    assert fake.bytes_read[(STG, s.staging_data_key(u))] == 2 * len(BIG)  # the staging GET, opened again
    assert fake.current(EVD, s.blob_key(sc["data"]["sha256"]))["data"] == BIG


def test_an_error_botocore_gave_up_on_is_logged_with_its_code(fake):  # L7
    fake.sdk_attempts = 3
    u, _ = stage(fake, CSV)
    fake.fail("put_object_body", code="SlowDown", status=503, times=3,
              when=lambda kw: kw["Key"].startswith(s.BLOB_PREFIX))
    ing, logs = ingest(fake)
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u))
    line = json.loads(logs[-1])
    assert (line["code"], line["status"]) == ("SlowDown", 503)
    assert_untouched(fake, u)


def test_the_job_records_a_code_hash_only_when_given_one(fake, aws):  # L9
    u, _ = stage(fake, CSV)
    assert ig.main(["process", s.staging_sidecar_key(u)]) == 0  # CODE_SHA256 is in the env, as a shell might have it
    assert record_of(fake, u)["ingest"]["code_sha256"] is None
    u2, _ = stage(fake, b"2\n")
    assert ig.main(["process", s.staging_sidecar_key(u2), "--code-sha256", "d" * 64]) == 0
    assert record_of(fake, u2)["ingest"]["code_sha256"] == "d" * 64


def test_a_hand_written_approval_is_accepted(fake, gate):  # L13
    sc = over_gate()
    src = sc["source"]
    body = {"schema": 1, "kind": s.APPROVAL_KIND, "max_size": 60_000_000_000, "note": None,
            "source": {k: src[k] for k in ("kind", "platform", "host", "request_id", "doc_id", "url")},
            "expires_at": "2026-10-28T00:00:00Z", "approved_by": "admin"}
    fake.put_object(Bucket=OPS, Key=APPROVAL, Body=json.dumps(body, indent=2).encode())
    u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=sc)
    assert run(fake, u)[0].status == "stored"


@pytest.mark.parametrize("outcome", ["rejected", "deferred"])
def test_a_racing_record_for_another_sidecar_over_the_same_bytes_leaves_it_uuid_reused(fake, outcome):  # T1
    if outcome == "rejected":
        u, _ = stage(fake, CSV, edit=lambda sc: sc["source"].update(url="https://x.example.org/a?X-Amz-Signature=1"))
        raw = record_elsewhere(u, CSV, filename="other.csv")
        ing, _ = ingest(fake)
    else:
        u, _ = stage(fake, BIG, part_size=5 * MiB)
        raw = record_elsewhere(u, BIG, part_size=5 * MiB, filename="other.csv")
        ing, _ = ingest(fake, max_lambda_size=MiB)
    fake.before("put_object_tagging", lambda kw: plant(fake, s.record_key(u), raw),
                when=lambda kw: kw["Key"] == s.staging_data_key(u))
    out = ing.process(s.staging_sidecar_key(u))
    assert (out.status, out.reason) == ("rejected", "uuid_reused")
    assert staging_tags(fake, u) == (s.rejected_tags("uuid_reused"),) * 2


@pytest.mark.parametrize("path", ["whole", "parts"])
def test_a_read_back_checks_the_version_it_wrote(fake, monkeypatch, path):  # T2
    if path == "parts":
        monkeypatch.setattr(s, "SINGLE_PUT_MAX", 10 * MiB)
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    key = s.blob_key(sc["data"]["sha256"])

    def replace(kw):  # between the write and its read-back: the same bytes, re-stored on other boundaries
        fake.delete_object(Bucket=EVD, Key=key)
        plant_parts(fake, key, BIG, 6 * MiB)
    heads = iter(range(100))  # the blob's second HEAD is the read-back, whatever version it asks for
    fake.before("head_object", replace, when=lambda kw: kw["Key"] == key and not kw.get("PartNumber")
                and next(heads) == 1)
    out = ingest(fake)[0].process(s.staging_sidecar_key(u))
    assert out.status == "stored"
    assert record_of(fake, u)["evidence"]["version_id"] == fake.versions(EVD, key)[0]["version_id"]


def test_a_hook_never_lends_its_rights_to_another_thread(fake):  # T7
    import threading
    u, _ = stage(fake, CSV)
    view = fake.as_role(LAMBDA - {("s3:PutObjectTagging", STG, s.STAGING_PREFIX)})
    inside, done = threading.Barrier(2, timeout=5), threading.Event()

    def hook(kw):
        inside.wait()
        done.wait(5)  # stay inside while the other thread calls
    fake.before("head_object", hook)
    thread = threading.Thread(target=lambda: view.head_object(Bucket=STG, Key=s.staging_data_key(u)))
    thread.start()
    inside.wait()
    try:
        with pytest.raises(FakeClientError) as e:
            view.put_object_tagging(Bucket=STG, Key=s.staging_data_key(u), Tagging={"TagSet": []})
    finally:
        done.set()
        thread.join()
    assert ig._code(e.value) == "AccessDenied"


def test_the_fake_wants_every_part_checksummed_on_a_sha256_upload(fake):  # T8
    key = s.blob_key("a" * 64)
    mpu = fake.create_multipart_upload(Bucket=EVD, Key=key, ChecksumAlgorithm="SHA256")
    got = fake.upload_part(Bucket=EVD, Key=key, UploadId=mpu["UploadId"], PartNumber=1, Body=b"x" * 10)
    with pytest.raises(FakeClientError):
        fake.complete_multipart_upload(Bucket=EVD, Key=key, UploadId=mpu["UploadId"], IfNoneMatch="*",
                                       MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": got["ETag"]}]})


# --- The re-check of 22a9320c2 (.claude/reviews/pr-841-recheck.md) -------------------------------


def hide_checksum(fake, key, sse=None):
    fake.edit_head(EVD, key, lambda h: {**{k: v for k, v in h.items() if not k.startswith("Checksum")},
                                        **({"ServerSideEncryption": sse} if sse else {})})


def test_a_checksum_sse_kms_hides_is_retried_every_time(fake):  # P2
    u, sc = stage(fake, CSV)
    hide_checksum(fake, s.blob_key(sc["data"]["sha256"]), sse="aws:kms")
    ing, _ = ingest(fake)
    for _ in range(3):  # the first run's own write, then the SQS retries that find it already stored
        with pytest.raises(ig.Transient):
            ing.process(s.staging_sidecar_key(u))
        assert_untouched(fake, u)


def test_a_sse_s3_blob_without_a_checksum_is_still_a_conflict_on_the_retry(fake):  # P2
    u, sc = stage(fake, CSV)
    hide_checksum(fake, s.blob_key(sc["data"]["sha256"]))
    ing, _ = ingest(fake)
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u))
    assert ing.process(s.staging_sidecar_key(u)).reason == "evidence_conflict"


def test_a_failed_reopen_is_logged_with_its_own_code(fake):  # P3
    fake.sdk_attempts = 3
    u, _ = stage(fake, BIG, part_size=5 * MiB)
    key = s.staging_data_key(u)
    fake.drop(STG, key, 100)
    gets = iter(range(100))
    fake.fail("get_object", code="SlowDown", status=503, when=lambda kw: kw["Key"] == key and next(gets) == 1)
    ing, logs = ingest(fake)
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u))
    line = json.loads(logs[-1])
    assert (line["code"], line["status"]) == ("SlowDown", 503)
    assert_untouched(fake, u)


@pytest.mark.parametrize("at", [0, 100, 6 * MiB], ids=["byte-0", "early", "mid-body"])
def test_a_dropped_staging_stream_is_reopened_within_the_call(fake, at):  # R1, RT2
    fake.sdk_attempts = 3
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    fake.drop(STG, s.staging_data_key(u), at)
    assert run(fake, u)[0].status == "stored"
    assert len([c for c in fake.ops("get_object", STG) if c[2] == s.staging_data_key(u)]) == 2
    assert fake.current(EVD, s.blob_key(sc["data"]["sha256"]))["data"] == BIG


def test_a_body_whose_first_read_failed_starts_over():  # R1
    sc = make_sidecar(BIG, "x", part_size=5 * MiB)

    class Dead:
        def read(self, n):
            raise ConnectionError("reset")
    opened = []
    body = ig._CheckedBody(lambda: (opened.append(1), Dead() if len(opened) == 1 else Chunks(BIG))[1], sc)
    with pytest.raises(ConnectionError):
        body.read(1000)
    assert body.tell() == 0 and body.seek(0) == 0 and len(opened) == 2  # untouched position, but a new stream
    assert b"".join(iter(lambda: body.read(3 * MiB), b"")) == BIG


def test_a_failure_in_a_sweep_chunk_still_counts_the_rest(fake):  # R2
    lost = [stage(fake, f"lost {i}\n".encode())[0] for i in range(8)]
    fake.tick(7200)
    bad = s.staging_sidecar_key(lost[5])
    fake.fail("get_object_tagging", code="InternalError", status=500, when=lambda kw: kw["Key"] == bad)
    sent, logs = [], []
    with pytest.raises(FakeClientError):
        ig.sweep(fake.as_role(SWEEPER), staging_bucket=STG, send=sent.append, now=fake.now, log=logs.append,
                 workers=4)
    line = json.loads(logs[-1])
    assert line["redriven"] == len(sent)
    assert line["unread"] == 8 - len(sent)  # the failed file, and any chunk never reached
    assert bad not in [m["staging_key"] for m in sent]


def test_the_sweep_really_reads_tags_concurrently(fake):  # RT3
    import threading
    lost = [stage(fake, f"lost {i}\n".encode())[0] for i in range(8)]
    fake.tick(7200)
    barrier = threading.Barrier(4, timeout=5)  # four tag reads must be in flight at once
    for u in lost:
        k = s.staging_sidecar_key(u)
        fake.before("get_object_tagging", lambda kw: barrier.wait(), when=lambda kw, k=k: kw["Key"] == k)
    counts, sent = sweep(fake, workers=4)
    assert counts["redriven"] == 8


@pytest.mark.parametrize("blob_mode,default_mode,lapsed,expected", [
    ("COMPLIANCE", "GOVERNANCE", True, "GOVERNANCE"),  # a lapsed lock takes today's mode
    ("GOVERNANCE", "COMPLIANCE", True, "COMPLIANCE"),
    ("COMPLIANCE", "GOVERNANCE", False, "COMPLIANCE"),  # a live COMPLIANCE lock can't change mode
])
def test_a_renewal_uses_the_blobs_mode_only_while_its_lock_is_live(blob_mode, default_mode, lapsed, expected):  # R3
    fake = new_fake(lock_mode=default_mode, lock_days=1)
    u1, sc = stage(fake, CSV)
    run(fake, u1)
    key = s.blob_key(sc["data"]["sha256"])
    v = fake.current(EVD, key)
    v["lock_mode"] = blob_mode
    fake.tick(2 * 86400 if lapsed else 20 * 3600)
    u2, _ = stage(fake, CSV)
    assert run(fake, u2)[0].status == "already_stored"
    assert record_of(fake, u2)["evidence"]["lock_mode"] == expected


def test_a_warm_container_sees_a_lengthened_rule_after_its_ttl():  # R4
    fake = new_fake(lock_days=2)
    ing, _ = ingest(fake)
    ing.check_buckets()
    u1, sc = stage(fake, CSV)
    ing.process(s.staging_sidecar_key(u1))
    key = s.blob_key(sc["data"]["sha256"])
    fake.buckets[EVD].lock_days = 3650
    fake.tick(int(1.5 * 86400))  # past the TTL; half a day left: fine under 2 days, short under 3650
    u2, _ = stage(fake, CSV)
    assert ing.process(s.staging_sidecar_key(u2)).status == "already_stored"
    assert fake.current(EVD, key)["lock_until"] > fake.now + timedelta(days=3000)


def test_a_blob_under_an_event_hold_is_never_renewed(fake):  # R5
    u1, sc = stage(fake, CSV)
    run(fake, u1)
    v = fake.current(EVD, s.blob_key(sc["data"]["sha256"]))
    v["event_hold"], v["lock_until"] = "ON", fake.now + timedelta(hours=3)  # well under half the period left
    u2, _ = stage(fake, CSV)
    fake.calls.clear()
    assert ingest(fake)[0].process(s.staging_sidecar_key(u2)).status == "already_stored"  # not run(): the test shortened it
    assert not fake.ops("put_object_retention")
    fake.tick(4 * 3600)  # its fixed date passes: safe under the hold, but v1 can't record it
    u3, _ = stage(fake, CSV)
    ing, logs = ingest(fake)
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u3))
    assert "lock_held" in json.loads(logs[-1])["error"] and not fake.ops("put_object_retention")


def test_a_rule_with_a_default_event_hold_fails_closed(fake):  # R5
    fake.buckets[EVD].lock_config = {"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {
        "Mode": "GOVERNANCE", "Days": 365, "DefaultEventHold": {"Days": 90}}}}
    with pytest.raises(RuntimeError):
        ingest(fake)[0].check_buckets()


def test_a_renewal_that_fails_otherwise_is_retried_not_logged_renewed():  # RT1 (a)
    fake = new_fake(lock_days=1)
    lapsing(fake)
    fake.fail("put_object_retention", code="ServiceUnavailable", status=503)
    u2, _ = stage(fake, CSV)
    ing, logs = ingest(fake)
    with pytest.raises(ig.Transient):
        ing.process(s.staging_sidecar_key(u2))
    assert "lock_renewed" not in [json.loads(line)["event"] for line in logs]
    assert_untouched(fake, u2)


def test_the_large_path_neither_renews_its_own_write_nor_rejects_a_hidden_checksum(fake, large):  # RT1 (b)
    u, sc = stage(fake, BIG, part_size=5 * MiB)
    key = s.blob_key(sc["data"]["sha256"])
    fake.edit_head(EVD, key, lambda h: {**{k: v for k, v in h.items() if not k.startswith("Checksum")},
                                        "ObjectLockRetainUntilDate": fake.now + timedelta(hours=1)})
    with pytest.raises(ig.Transient):
        ingest(fake)[0].process(s.staging_sidecar_key(u))
    assert not fake.ops("put_object_retention")
    assert_untouched(fake, u)


def test_the_renewed_lock_is_read_back_from_the_version_renewed():  # RT1 (c)
    fake = new_fake(lock_days=1)
    key = lapsing(fake)
    old = fake.current(EVD, key)["version_id"]

    def restore(kw):  # a delete marker and the same bytes again, between the renewal and its read-back
        fake.delete_object(Bucket=EVD, Key=key)
        fake.put_object(Bucket=EVD, Key=key, Body=CSV, ChecksumSHA256=b64_sha256(CSV), IfNoneMatch="*",
                        ContentType=s.EVIDENCE_CONTENT_TYPE, ContentDisposition=s.EVIDENCE_CONTENT_DISPOSITION)
    fake.before("put_object_retention", restore)
    u2, _ = stage(fake, CSV)
    assert ingest(fake)[0].process(s.staging_sidecar_key(u2)).status == "already_stored"
    assert record_of(fake, u2)["evidence"]["version_id"] == old


def test_an_approval_with_a_duplicate_key_is_refused(fake, gate):  # RT5
    sc = over_gate()
    src = sc["source"]
    source = json.dumps({k: src[k] for k in ("kind", "platform", "host", "request_id", "doc_id", "url")})
    raw = ('{"schema": 1, "kind": "cost_gate_approval", "max_size": 60000000000, "note": null, '
           f'"source": {source}, "approved_by": "admin", '
           '"expires_at": "2026-09-01T00:00:00Z", "expires_at": "2099-01-01T00:00:00Z"}').encode()
    fake.put_object(Bucket=OPS, Key=APPROVAL, Body=raw)
    u, _ = stage(fake, BIG, part_size=5 * MiB, sidecar=sc)
    out, logs = run(fake, u)
    assert_rejected(fake, u, out, "too_large")
    line = json.loads(logs[-1])
    assert (line["field"], line["problem"]) == ("approval", "duplicate key")
