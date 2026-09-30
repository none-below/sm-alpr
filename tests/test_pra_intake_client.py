"""Tests for scripts/pra_intake/client.py, the upload library, end to end:
the library stages files in the in-memory S3 of pra_intake_fakes.py and the
real ingest Lambda copies them into evidence.

The library runs with exactly client.PERMISSIONS (the writer role) and the
Lambda with exactly ingest.PERMISSIONS. Each sidecar the library writes
queues an S3 event, and the library's sleep() runs the Lambda on what's
queued, so outcomes arrive as they do in AWS: after the commit, while the
library waits. Every test that runs the Lambda also checks the evidence
invariant the ingest tests check (it.check_evidence).
"""

import hashlib
import io
import json
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(Path(__file__).parent))

import test_pra_intake_ingest as it  # noqa: E402  (its buckets, grants and evidence check)
from pra_intake import client as c  # noqa: E402
from pra_intake import ingest as ig  # noqa: E402
from pra_intake import schema as s  # noqa: E402
from pra_intake_fakes import FakeClientError, b64_sha256, grants, sqs_message  # noqa: E402

STG, EVD, OPS = it.STG, it.EVD, it.OPS
WRITER = grants(c.PERMISSIONS, it.BUCKETS)
MiB, PART = s.MiB, s.PART_SIZE
URL = "https://cdn.muckrock.com/foia_files/2026/09/28/a.csv"
SOURCE = {
    "kind": "muckrock", "platform": "muckrock", "host": "www.muckrock.com",
    "agency": "San Mateo Police Department", "request_id": "12345",
    "request_url": "https://www.muckrock.com/foi/san-mateo-12345/",
    "doc_id": 987654, "filename": "a.csv", "title": "Audit log", "url": URL, "released_on": "2026-09-27",
}
REQUEST = {k: SOURCE[k] for k in ("kind", "platform", "host", "agency", "request_id", "request_url")}
CSV = b"case,plate,reason\n1,XYZ,investigation\n"
PDF = b"%PDF-1.7\n" + bytes(range(256)) * 8
HTML = b"<!doctype html><html><body>Session expired</body></html>"


def data(n, seed=0):
    return random.Random(seed).randbytes(n)


BIG = data(2 * PART + 3 * MiB, 1)  # three parts of the library's own layout


def md5(b):
    return hashlib.md5(b).hexdigest()


def sha(b):
    return hashlib.sha256(b).hexdigest()


def multipart_etag(b, part_size):
    return s.md5_multipart_etag([md5(b[i:i + part_size]) for i in range(0, len(b), part_size)] or [md5(b"")])


def response_for(body, *, etag="md5", length=True, extra=(), status=200, url=URL, redirects=None):
    headers = [("Content-Type", "text/csv"), ("Set-Cookie", "session=SENTINEL-cookie")]
    if length:
        headers.append(("Content-Length", str(len(body))))
    if etag == "md5":
        headers.append(("ETag", f'"{md5(body)}"'))
    elif etag is not None:
        headers.append(("ETag", etag))
    if redirects is None:
        redirects = ((302, "https://www.muckrock.com/foi/files/987654/?session=x", url),)
    return c.Response(status, tuple(headers) + tuple(extra), url, redirects)


class World:
    """The three buckets, the writer's Intake and the Lambda. The Intake's
    sleep() advances its clock and runs the Lambda on the queued events."""

    def __init__(self, *, lambda_runs=True, max_lambda_size=None, timeout=None, workers=c.WORKERS):
        self.fake = it.new_fake()
        self.queue, self.logs, self.lambda_logs, self.sleeps = [], [], [], []
        self.clock, self.lambda_runs = 0.0, lambda_runs
        self.fake.notify(STG, self.queue.append, prefix=s.STAGING_PREFIX, suffix=s.SIDECAR_SUFFIX)
        self.intake = c.Intake(self.fake.as_role(WRITER), staging_bucket=STG, evidence_bucket=EVD,
                               connector="muckrock", run_id="run-1", connector_version="abc1234",
                               now=lambda: self.fake.now, clock=lambda: self.clock, sleep=self.sleep,
                               log=self.logs.append, timeout=timeout, workers=workers)
        self.ingest = ig.Ingest(self.fake.as_role(it.LAMBDA), staging_bucket=STG, evidence_bucket=EVD,
                                ops_bucket=OPS, code_sha256=it.CODE, now=lambda: self.fake.now,
                                log=self.lambda_logs.append, max_lambda_size=max_lambda_size)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += seconds
        if self.lambda_runs:
            self.run_lambda()

    def run_lambda(self):
        """Process every queued event, as SQS delivers them; a transient
        failure stays queued for the next round."""
        queue, self.queue[:] = list(self.queue), []
        for event in queue:
            for key, principal in ig.staging_keys(sqs_message(event), STG):
                try:
                    self.ingest.process(key, principal=principal)
                except ig.Transient:
                    self.queue.append(event)
        it.check_evidence(self.fake)

    def evidence(self, sha256):
        return self.fake.current(EVD, s.blob_key(sha256))["data"]

    def staged(self):
        return self.fake.keys(STG)

    def open_uploads(self):
        return dict(self.fake.buckets[STG].uploads)


@pytest.fixture
def world():
    return World()


class Proxy:
    """A client whose named calls go through fn(original, **kwargs)."""

    def __init__(self, inner, **overrides):
        self._inner, self._overrides = inner, overrides

    def __getattr__(self, name):
        fn = getattr(self._inner, name)
        if name in self._overrides:
            return lambda **kw: self._overrides[name](fn, **kw)
        return fn


def sidecar_of(result):
    return result.record["sidecar"]


def assert_nothing_staged(w):
    assert w.staged() == [] and w.open_uploads() == {}


# --- The happy paths ------------------------------------------------------------------------------


def test_a_small_live_file_is_held_with_what_the_library_checked(world):
    r = world.intake.upload(SOURCE, CSV, response=response_for(CSV))
    assert (r.status, r.sha256, r.size, r.reason, r.terminal) == ("held", sha(CSV), len(CSV), None, True)
    assert world.evidence(r.sha256) == CSV
    sc = sidecar_of(r)
    assert sc["data"]["upload"] == {"method": "put", "part_size": None, "part_sha256": None}
    assert sc["data"]["staging_etag"] == f'"{md5(CSV)}"' and sc["data"]["md5_multipart"] is None
    assert sc["source"] == {**SOURCE, "doc_id": "987654"}
    assert sc["checks"] == {"declared_length": len(CSV), "length": "ok", "eof": "clean", "etag": "md5",
                            "content_md5": "absent", "sniffed_type": "text", "expect_types": None}
    assert sc["response"] == {
        "status": 200, "headers": {"content-type": "text/csv", "content-length": str(len(CSV)),
                                   "etag": f'"{md5(CSV)}"'},  # the cookie is dropped
        "redirects": [{"status": 302, "url": URL}], "final_url": URL}
    fetch = sc["fetch"]
    assert (fetch["origin"], fetch["run_id"], fetch["connector"], fetch["connector_version"],
            fetch["library_version"]) == ("live", "run-1", "muckrock", "abc1234", c.LIBRARY_VERSION)
    assert fetch["started_at"] <= fetch["first_byte_at"] <= fetch["completed_at"]
    assert json.loads(world.lambda_logs[-1])["event"] == "stored"
    for key in (s.staging_data_key(r.uuid), s.staging_sidecar_key(r.uuid)):
        assert world.fake.tags(STG, key) == s.ingested_tags(r.sha256)


def test_the_data_object_carries_the_triage_metadata_and_no_tags(world):
    r = world.intake.upload(SOURCE, CSV, response=response_for(CSV))
    data = world.fake.current(STG, s.staging_data_key(r.uuid))
    assert data["metadata"] == s.staging_metadata(SOURCE | {"doc_id": "987654"}, r.uuid,
                                                  sidecar_of(r)["fetch"]["fetch_id"])
    assert data["checksum_type"] == "FULL_OBJECT"


def test_a_file_over_part_size_is_staged_in_parts_and_held(world):
    r = world.intake.upload(SOURCE, BIG, response=response_for(BIG))
    assert r.status == "held" and world.evidence(r.sha256) == BIG
    up = sidecar_of(r)["data"]["upload"]
    assert up == {"method": "multipart", "part_size": PART,
                  "part_sha256": [sha(BIG[i:i + PART]) for i in range(0, len(BIG), PART)]}
    assert sidecar_of(r)["data"]["staging_etag"] == f'"{multipart_etag(BIG, PART)}"'
    assert world.fake.current(STG, s.staging_data_key(r.uuid))["checksum_type"] == "COMPOSITE"


@pytest.mark.parametrize("size", [0, 1, PART - 1, PART, PART + 1, 2 * PART, 2 * PART + 1],
                         ids=["empty", "1", "part-1", "part", "part+1", "2parts", "2parts+1"])
def test_each_layout_boundary_is_held(world, size):
    body = data(size, size)
    r = world.intake.upload(SOURCE, body, response=response_for(body))
    assert r.status == "held" and world.evidence(r.sha256) == body
    assert sidecar_of(r)["data"]["upload"]["part_size"] == s.upload_part_size(size)
    if size == 0:
        assert sidecar_of(r)["checks"]["sniffed_type"] == "empty"


BODIES = {
    "bytes": lambda b: b,
    "bytearray": lambda b: bytearray(b),
    "file": lambda b: io.BytesIO(b),
    "short reads": lambda b: ShortReads(b, 7919),
    "chunks with empties": lambda b: iter([b"", b[:3], b"", b[3:PART + 5], b"", b[PART + 5:], b""]),
    "generator": lambda b: (b[i:i + 1_000_003] for i in range(0, len(b), 1_000_003)),
}


class ShortReads:
    def __init__(self, b, most):
        self._f, self._most = io.BytesIO(b), most

    def read(self, n=-1):
        return self._f.read(min(n, self._most) if n >= 0 else self._most)


@pytest.mark.parametrize("kind", sorted(BODIES))
def test_every_kind_of_body_stages_the_same_bytes(world, kind):
    r = world.intake.upload(SOURCE, BODIES[kind](BIG), response=response_for(BIG))
    assert (r.status, r.sha256) == ("held", sha(BIG))


def test_a_local_copy_is_backfilled_with_its_old_path(world, tmp_path):
    path = tmp_path / "a.csv"
    path.write_bytes(CSV)
    fetched = datetime(2026, 9, 20, 1, 2, 3, tzinfo=timezone.utc)
    r = world.intake.upload_file(path, {**SOURCE, "url": None}, legacy_path="107949-roseville/a.csv",
                                 original_fetched_at=fetched)
    sc = sidecar_of(r)
    assert r.status == "held" and sc["response"] is None
    assert (sc["fetch"]["origin"], sc["fetch"]["legacy_path"], sc["fetch"]["original_fetched_at"]) == (
        "local-copy", "107949-roseville/a.csv", "2026-09-20T01:02:03Z")
    assert (sc["checks"]["length"], sc["checks"]["declared_length"], sc["checks"]["eof"]) == ("ok", len(CSV), "clean")


def test_a_git_backfill_names_its_commit(world, tmp_path):
    path = tmp_path / "a.csv"
    path.write_bytes(CSV)
    r = world.intake.upload_file(path, SOURCE, origin="git", legacy_path="assets/a.csv", git_commit="1" * 40)
    assert r.status == "held" and sidecar_of(r)["fetch"]["git_commit"] == "1" * 40


# --- What the library records about the source's own checks -------------------------------------------


@pytest.mark.parametrize("part_size", c.ETAG_PART_SIZES, ids=lambda n: f"{n // MiB}MiB")
def test_a_source_multipart_etag_is_matched_at_its_part_size_and_verified_by_the_lambda(world, part_size):
    body = data(PART + 3 * MiB, 2)
    r = world.intake.upload(SOURCE, body, response=response_for(body, etag=f'"{multipart_etag(body, part_size)}"'))
    sc = sidecar_of(r)
    assert r.status == "held" and sc["checks"]["etag"] == "md5-multipart"
    assert sc["data"]["md5_multipart"] == {"part_size": part_size, "etag": multipart_etag(body, part_size)}


def test_an_undeclared_length_still_tries_every_etag_part_size(world):
    body = data(PART + 3 * MiB, 3)
    etag = f'"{multipart_etag(body, 8 * MiB)}"'
    r = world.intake.upload(SOURCE, iter([body]), response=response_for(body, etag=etag, length=False,
                                                                        extra=[("Transfer-Encoding", "chunked")]))
    sc = sidecar_of(r)
    assert sc["data"]["md5_multipart"]["part_size"] == 8 * MiB
    assert (sc["checks"]["length"], sc["checks"]["eof"]) == ("undeclared", "clean")  # chunked framing ends cleanly


@pytest.mark.parametrize("etag,check", [
    (f'"{"0" * 32}"', "unmatched"), (f'"{"0" * 32}-2"', "unmatched"), ('W/"abc"', "opaque"),
    ('"opaque-server-tag"', "opaque"), (None, "absent")])
def test_etag_checks_record_how_the_source_etag_relates(world, etag, check):
    r = world.intake.upload(SOURCE, CSV, response=response_for(CSV, etag=etag))
    assert r.status == "held" and sidecar_of(r)["checks"]["etag"] == check
    assert sidecar_of(r)["data"]["md5_multipart"] is None


@pytest.mark.parametrize("headers,check", [
    ((("Content-MD5", "{b64}"),), "match"), ((("Content-MD5", "{hex}"),), "match"),
    ((("x-ms-blob-content-md5", "{b64}"),), "match"), ((("Content-MD5", "AAAAAAAAAAAAAAAAAAAAAA=="),), "mismatch"),
    ((("Content-MD5", "{b64}"), ("x-ms-blob-content-md5", "AAAAAAAAAAAAAAAAAAAAAA==")), "mismatch")],
    ids=["b64", "hex", "azure", "wrong", "one-wrong"])
def test_content_md5_is_checked_and_recorded_not_refused(world, headers, check):
    import base64
    fill = {"b64": base64.b64encode(hashlib.md5(CSV).digest()).decode(), "hex": md5(CSV)}
    extra = tuple((n, v.format(**fill)) for n, v in headers)
    r = world.intake.upload(SOURCE, CSV, response=response_for(CSV, extra=extra))
    assert r.status == "held" and sidecar_of(r)["checks"]["content_md5"] == check


def test_a_compressed_response_has_no_declared_length_and_an_unknown_end(world):
    r = world.intake.upload(SOURCE, CSV, response=response_for(CSV, extra=[("Content-Encoding", "gzip")]))
    assert (sidecar_of(r)["checks"]["length"], sidecar_of(r)["checks"]["eof"]) == ("undeclared", "unknown")


def test_the_connector_may_vouch_for_a_clean_end(world):
    r = world.intake.upload(SOURCE, CSV, response=response_for(CSV, length=False), eof="clean")
    assert sidecar_of(r)["checks"]["eof"] == "clean"


def test_expected_types_are_recorded(world):
    r = world.intake.upload(SOURCE, PDF, response=response_for(PDF), expect_types=["pdf"])
    assert r.status == "held" and sidecar_of(r)["checks"]["expect_types"] == ["pdf"]


# --- Refused before anything is staged --------------------------------------------------------------


@pytest.mark.parametrize("body", [HTML, HTML + b" " * (PART + 5)], ids=["small", "multipart"])
def test_an_unexpected_type_is_refused_before_staging(world, body):
    r = world.intake.upload(SOURCE, body, response=response_for(body), expect_types=["pdf"])
    assert (r.status, r.reason, r.uuid, r.terminal) == ("failed", "unexpected_type", None, False)
    assert_nothing_staged(world)
    assert world.fake.ops("create_multipart_upload") == []


@pytest.mark.parametrize("size,declared,reason", [
    (100, 110, "truncated"), (100, 90, "overlong"), (PART + 1, PART, "overlong"), (PART + 1, 100, "overlong"),
    (PART + 100, 2 * PART, "truncated"), (2 * PART + 100, PART + 10, "overlong"), (3 * PART, 3 * PART + 1, "truncated")],
    ids=["put-short", "put-long", "put-declared-part", "put-declared-small", "parts-short", "parts-long",
         "parts-boundary-short"])
def test_a_body_that_isnt_its_declared_length_leaves_nothing_staged(world, size, declared, reason):
    body = data(size, 4)
    resp = response_for(body, length=False, extra=[("Content-Length", str(declared))])
    r = world.intake.upload(SOURCE, body, response=resp)
    assert (r.status, r.reason, r.terminal) == ("failed", reason, False)
    assert_nothing_staged(world)
    assert len(world.fake.ops("create_multipart_upload")) == len(world.fake.ops("abort_multipart_upload"))


class Unread:
    def read(self, n=-1):
        raise AssertionError("the body was read")


def test_a_declared_file_over_the_cost_gate_needs_approval_before_a_byte_is_read(world):
    resp = response_for(b"", length=False, extra=[("Content-Length", str(s.COST_GATE + 1))])
    r = world.intake.upload(SOURCE, Unread(), response=resp)
    assert (r.status, r.size, r.reason, r.terminal) == ("needs_approval", s.COST_GATE + 1, "too_large", True)
    assert world.fake.calls == []


def test_an_undeclared_file_over_the_cost_gate_is_aborted_as_needing_approval(world, monkeypatch):
    monkeypatch.setattr(s, "COST_GATE", 2 * PART)
    body = data(2 * PART + 1, 5)
    r = world.intake.upload(SOURCE, iter([body]), response=response_for(body, length=False))
    assert (r.status, r.size, r.reason) == ("needs_approval", None, "too_large")
    assert_nothing_staged(world)
    assert len(world.fake.ops("abort_multipart_upload")) == 1


def test_an_approved_file_of_undeclared_length_stops_at_the_part_limit(world, monkeypatch):
    monkeypatch.setattr(s, "COST_GATE", PART)
    monkeypatch.setattr(s, "MAX_PARTS", 2)
    body = data(2 * PART + 1, 6)
    r = world.intake.upload(SOURCE, iter([body]), response=response_for(body, length=False),
                            approval=s.approval_key(s.new_uuid()))
    assert (r.status, r.reason) == ("failed", "undeclared_length")
    assert_nothing_staged(world)


def test_the_cost_gate_needs_approval_only_above_it(world, monkeypatch):
    monkeypatch.setattr(s, "COST_GATE", PART + 10)
    at, over = data(PART + 10, 8), data(PART + 11, 8)
    assert world.intake.upload(SOURCE, at, response=response_for(at)).status == "held"
    assert world.intake.upload(SOURCE, Unread(), response=response_for(over, length=False, extra=[
        ("Content-Length", str(len(over)))])).status == "needs_approval"


def test_an_approval_for_a_file_under_the_gate_is_left_out(world):
    r = world.intake.upload(SOURCE, CSV, response=response_for(CSV), approval=s.approval_key(s.new_uuid()))
    assert r.status == "held" and sidecar_of(r)["fetch"]["approval"] is None


def test_a_signed_url_is_refused_before_any_s3_call(world):
    with pytest.raises(s.SchemaError) as e:
        world.intake.upload({**SOURCE, "url": URL + "?X-Amz-Signature=abc"}, CSV, response=response_for(CSV))
    assert e.value.reason == "signed_url"
    assert world.fake.calls == []


@pytest.mark.parametrize("kw,error", [
    ({"origin": "local-copy", "legacy_path": "a.csv"}, s.SchemaError),  # a response on a backfill
    ({"origin": "local-copy", "response": None}, s.SchemaError),  # a backfill without its old path
    ({"response": None}, s.SchemaError),  # a live fetch without one
    ({"content_kind": "fetch_manifest"}, s.SchemaError),  # a manifest that isn't generated
    ({"response": response_for(CSV, status=206)}, s.SchemaError),
    ({"source": {**SOURCE, "url": None}}, s.SchemaError),  # a live fetch without its URL
    ({"expect_types": ["pdf", "docx"]}, ValueError),
    ({"eof": "maybe"}, ValueError),
    ({"declared_length": len(CSV) + 1}, ValueError),  # disagrees with Content-Length
    ({"started_at": datetime(2030, 1, 1, tzinfo=timezone.utc)}, ValueError),
    ({"started_at": datetime(2026, 1, 1)}, TypeError),
    ({"access": "someone"}, s.SchemaError),
    ({"listing": {"size": -1, "date": None, "title": None}}, s.SchemaError),
    ({"approval": "approvals/not-a-uuid.json"}, s.SchemaError)],
    ids=["backfill-response", "backfill-no-path", "live-no-response", "manifest-live", "partial", "live-no-url",
         "bad-type", "bad-eof", "declared",
         "future", "naive", "access", "listing", "approval"])
def test_arguments_the_contract_refuses_fail_before_any_s3_call(world, kw, error):
    kw = {"response": response_for(CSV), "source": SOURCE, **kw}
    with pytest.raises(error):
        world.intake.upload(kw.pop("source"), CSV, **kw)
    assert world.fake.calls == []


def test_a_start_in_the_future_is_refused_before_a_part_is_sent(world):
    """The sidecar check would refuse it too, but only after every part was
    uploaded."""
    with pytest.raises(ValueError):
        world.intake.upload(SOURCE, BIG, response=response_for(BIG), started_at=world.fake.now + timedelta(seconds=1))
    assert world.fake.calls == []


def test_a_sidecar_that_fails_validation_aborts_the_upload(world, monkeypatch):
    def refuse(obj):
        raise s.SchemaError("invalid_metadata", "sidecar", "refused")
    monkeypatch.setattr(s, "sidecar_bytes", refuse)
    for body in (CSV, BIG):
        with pytest.raises(s.SchemaError):
            world.intake.upload(SOURCE, body, response=response_for(body))
    assert_nothing_staged(world)
    assert world.fake.ops("complete_multipart_upload") == []


# --- S3 failures and retries ---------------------------------------------------------------------------


def test_a_failed_part_aborts_the_upload_and_raises(world):
    world.fake.fail("upload_part", code="InternalError", status=500, when=lambda kw: kw["PartNumber"] == 2)
    with pytest.raises(FakeClientError):
        world.intake.upload(SOURCE, BIG, response=response_for(BIG))
    assert_nothing_staged(world)
    assert len(world.fake.ops("abort_multipart_upload")) == 1


def test_a_body_that_raises_aborts_the_upload(world):
    def broken():
        yield BIG[:PART + 5]
        raise ConnectionError("the connection dropped")
    with pytest.raises(ConnectionError):
        world.intake.upload(SOURCE, broken(), response=response_for(BIG))
    assert_nothing_staged(world)


def test_an_abort_that_fails_is_logged_and_the_first_error_raised(world):
    world.fake.fail("upload_part", code="InternalError", status=500)
    world.fake.fail("abort_multipart_upload", code="SlowDown", status=503)
    with pytest.raises(FakeClientError, match="upload_part"):
        world.intake.upload(SOURCE, BIG, response=response_for(BIG))
    assert json.loads(world.logs[-1])["event"] == "abort_failed"


class LandsThenRetries:
    """A writer client whose `op` succeeds, loses its response, and is sent
    again, as botocore retries a request that timed out after S3 stored it."""

    def __init__(self, inner, op, when):
        self._inner, self._op, self._when = inner, op, when

    def __getattr__(self, name):
        fn = getattr(self._inner, name)
        if name != self._op:
            return fn

        def retried(**kw):
            if not self._when(kw):
                return fn(**kw)
            self._when = lambda kw: False
            fn(**kw)
            return fn(**kw)
        return retried


@pytest.mark.parametrize("key_kind", ["data", "sidecar"])
def test_a_put_whose_first_attempt_landed_is_accepted(key_kind):
    w = World()
    suffix = s.DATA_SUFFIX if key_kind == "data" else s.SIDECAR_SUFFIX
    w.intake.s3 = LandsThenRetries(w.intake.s3, "put_object", lambda kw: kw["Key"].endswith(suffix))
    r = w.intake.upload(SOURCE, CSV, response=response_for(CSV))
    assert r.status == "held"
    assert any(json.loads(line) == {"event": "put_retried", "key_kind": key_kind} for line in w.logs)


def test_a_complete_whose_first_attempt_landed_is_accepted():
    w = World()
    w.intake.s3 = LandsThenRetries(w.intake.s3, "complete_multipart_upload", lambda kw: True)
    r = w.intake.upload(SOURCE, BIG, response=response_for(BIG))
    assert r.status == "held" and w.evidence(r.sha256) == BIG
    assert any(json.loads(line)["event"] == "complete_retried" for line in w.logs)


def test_a_complete_that_fails_without_landing_raises(world):
    world.fake.fail("complete_multipart_upload", code="PreconditionFailed", status=412)
    with pytest.raises(FakeClientError):
        world.intake.upload(SOURCE, BIG, response=response_for(BIG))
    assert_nothing_staged(world)


OTHER_ETAG = '"' + "0" * 32 + '"'


@pytest.mark.parametrize("op,body", [
    ("upload_part", BIG), ("complete_multipart_upload", BIG), ("put_object", CSV)])
def test_s3_answering_for_other_bytes_is_an_intake_error_and_commits_nothing(world, op, body):
    def other(fn, **kw):
        got = fn(**kw)
        return {**got, "ETag": OTHER_ETAG} if not kw["Key"].endswith(".json") else got
    world.intake.s3 = Proxy(world.intake.s3, **{op: other})
    with pytest.raises(c.IntakeError):
        world.intake.upload(SOURCE, body, response=response_for(body))
    assert [k for k in world.staged() if k.endswith(".json")] == [] and world.open_uploads() == {}


def test_in_flight_parts_are_bounded(monkeypatch):
    monkeypatch.setattr(c, "MAX_IN_FLIGHT", 2 * PART)
    w = World(workers=8)
    live, most, lock = [0], [0], threading.Lock()

    def slow(fn, **kw):
        with lock:
            live[0] += 1
            most[0] = max(most[0], live[0])
        time.sleep(0.3)  # long enough for the next part to be read and sent
        try:
            return fn(**kw)
        finally:
            with lock:
                live[0] -= 1
    w.intake.s3 = Proxy(w.intake.s3, upload_part=slow)
    body = data(5 * PART + 1, 7)
    assert w.intake.upload(SOURCE, body, response=response_for(body)).status == "held"
    assert most[0] == 2


# --- Outcomes -----------------------------------------------------------------------------------------


def test_a_rejected_file_ends_the_wait_with_its_reason():
    w = World()
    missing = sha(b"a stamped manifest nobody uploaded")
    r = w.intake.upload({**SOURCE, "url": None}, CSV, origin="local-copy", legacy_path="a.csv",
                        declared_length=len(CSV), stamp_ref=missing)
    assert (r.status, r.reason, r.terminal, r.sha256) == ("rejected", "missing_blob", True, sha(CSV))


def test_a_deferred_file_ends_the_wait_and_is_recorded_later_by_the_job():
    w = World(max_lambda_size=PART)
    r = w.intake.upload(SOURCE, BIG, response=response_for(BIG))
    assert (r.status, r.reason, r.terminal) == ("deferred", "deferred", True)
    job = ig.Ingest(w.fake.as_role(it.LAMBDA), staging_bucket=STG, evidence_bucket=EVD, ops_bucket=OPS,
                    now=lambda: w.fake.now, log=lambda line: None)
    assert job.process(s.staging_sidecar_key(r.uuid), allow_large=True).status == "stored"
    assert w.intake.read_record(r.uuid)["sha256"] == sha(BIG)


def test_with_no_outcome_by_its_timeout_a_file_is_pending():
    w = World(lambda_runs=False, timeout=30)
    r = w.intake.upload(SOURCE, CSV, response=response_for(CSV))
    assert (r.status, r.reason, r.terminal, r.sha256) == ("pending", "pending", False, sha(CSV))
    assert w.sleeps == [1.0, 2.0, 4.0, 8.0, 15.0] and w.clock >= 30
    w.run_lambda()  # the file is still ingested; a later run finds its record
    assert w.intake.read_record(r.uuid)["sha256"] == sha(CSV)


def test_the_default_timeout_grows_with_size():
    w = World()
    assert w.intake._timeout_for(0) == c.BASE_TIMEOUT
    assert w.intake._timeout_for(3_000_000_000) == c.BASE_TIMEOUT + 100
    assert w.intake._timeout_for(10 ** 12) == c.MAX_TIMEOUT


def test_a_record_written_after_a_rejected_tag_wins(world):
    """A racing run tags the file rejected after another recorded it: the
    library reads the record once more before believing the tag."""
    staged = world.intake.stage(SOURCE, CSV, response=response_for(CSV))

    def race(kw):
        world.run_lambda()
        for key in (s.staging_data_key(staged.uuid), s.staging_sidecar_key(staged.uuid)):
            world.fake.put_object_tagging(Bucket=STG, Key=key, Tagging=ig._tagset(s.rejected_tags("no_data")))
    world.fake.before("get_object_tagging", race)
    assert staged.result().status == "held"


def test_a_reject_tag_outside_the_vocabulary_reads_as_rejected():
    w = World(lambda_runs=False)
    staged = w.intake.stage(SOURCE, CSV, response=response_for(CSV))
    w.fake.put_object_tagging(Bucket=STG, Key=s.staging_sidecar_key(staged.uuid), Tagging=ig._tagset(
        {"intake": "rejected", "reason": "Something Else"}))
    assert (staged.result().status, staged.result().reason) == ("rejected", "rejected")


def test_a_record_for_other_staging_bytes_is_an_intake_error(world):
    staged = world.intake.stage(SOURCE, CSV, response=response_for(CSV))
    staged._sidecar_sha256 = "0" * 64
    with pytest.raises(c.IntakeError):
        staged.result()


def test_a_throttled_check_is_logged_and_tried_again(world):
    world.fake.fail("get_object", times=2, when=lambda kw: kw["Key"].startswith(s.RECORD_PREFIX))
    world.fake.fail("get_object_tagging", times=1)
    assert world.intake.upload(SOURCE, CSV, response=response_for(CSV)).status == "held"
    assert sum(json.loads(line)["event"] == "poll_failed" for line in world.logs) >= 2


def test_a_record_that_doesnt_parse_is_an_intake_error():
    w = World(lambda_runs=False)
    staged = w.intake.stage(SOURCE, CSV, response=response_for(CSV))
    w.fake.put_object(Bucket=EVD, Key=s.record_key(staged.uuid), Body=b"{}\n", IfNoneMatch="*",
                    ChecksumSHA256=b64_sha256(b"{}\n"))
    with pytest.raises(c.IntakeError):
        staged.result()


def test_many_files_are_waited_for_together(world):
    handles = [world.intake.stage({**SOURCE, "filename": f"{i}.csv"}, CSV + bytes([i]), response=response_for(
        CSV + bytes([i]))) for i in range(5)]
    assert [r.status for r in world.intake.wait(handles)] == ["held"] * 5
    assert world.sleeps == [1.0]


ACTIONS = {"put_object": "s3:PutObject", "create_multipart_upload": "s3:PutObject", "upload_part": "s3:PutObject",
           "complete_multipart_upload": "s3:PutObject", "abort_multipart_upload": "s3:AbortMultipartUpload",
           "get_object": "s3:GetObject", "get_object_tagging": "s3:GetObjectTagging"}


def test_every_call_the_library_makes_is_within_the_writer_grants(world):
    """The fake refuses anything else, but the library reads a 403 on a
    record or a tag as "not yet", so a call outside the grants could fail
    quietly: check each one against PERMISSIONS instead."""
    calls = []

    def recorded(name):
        return lambda fn, **kw: calls.append((name, kw["Bucket"], kw["Key"])) or fn(**kw)
    world.intake.s3 = LandsThenRetries(Proxy(world.intake.s3, **{n: recorded(n) for n in ACTIONS}),
                                       "complete_multipart_upload", lambda kw: True)
    world.fake.fail("upload_part", code="InternalError", status=500)
    with pytest.raises(FakeClientError):
        world.intake.upload(SOURCE, BIG, response=response_for(BIG))  # aborted
    with world.intake.request(**REQUEST) as req:
        req.upload("a.bin", BIG, url=URL, response=response_for(BIG))  # its Complete is retried
        req.upload("b.csv", CSV, url=URL, response=response_for(CSV))
    assert req.manifest.status == "held"
    assert {name for name, _, _ in calls} == set(ACTIONS)
    for name, bucket, key in calls:
        assert any(a == ACTIONS[name] and b == bucket and key.startswith(p) for a, b, p in WRITER), (name, key)


def test_the_writer_grants_are_pinned():
    assert c.PERMISSIONS == {
        "staging": {"in/": ("s3:PutObject", "s3:AbortMultipartUpload", "s3:GetObjectTagging")},
        "evidence": {"_intake/": ("s3:GetObject",)}}


def without(action):
    return grants({role: {p: tuple(a for a in acts if a != action) for p, acts in scopes.items()}
                   for role, scopes in c.PERMISSIONS.items()}, it.BUCKETS)


def test_without_the_tagging_grant_a_rejection_goes_unseen():
    w = World(timeout=10)
    w.intake.s3 = w.fake.as_role(without("s3:GetObjectTagging"))
    r = w.intake.upload({**SOURCE, "url": None}, CSV, origin="local-copy", legacy_path="a.csv",
                        declared_length=len(CSV), stamp_ref=sha(b"missing"))
    assert r.status == "pending" and w.fake.tags(STG, s.staging_sidecar_key(r.uuid))["intake"] == "rejected"


def test_without_the_record_grant_nothing_is_held():
    w = World(timeout=10)
    w.intake.s3 = w.fake.as_role(without("s3:GetObject"))
    r = w.intake.upload(SOURCE, CSV, response=response_for(CSV))
    assert r.status == "pending" and w.fake.keys(EVD, s.RECORD_PREFIX) == [s.record_key(r.uuid)]


def test_logs_hold_no_presented_text(world):
    sentinel = {**SOURCE, "agency": "SENTINEL-Jane-Doe", "filename": "SENTINEL-a.csv", "title": "SENTINEL-title",
                "request_id": "SENTINEL-REQ-77", "url": "https://sentinel-host.example.org/a.csv"}
    with world.intake.request(**{**REQUEST, "agency": "SENTINEL-Jane-Doe", "request_id": "SENTINEL-REQ-77"}) as req:
        req.upload("SENTINEL-a.csv", CSV, url=sentinel["url"], title="SENTINEL-title",
                   response=response_for(CSV, url=sentinel["url"], redirects=()))
        req.failed("SENTINEL-b.csv", "source_404")
    text = "\n".join(world.logs)
    assert "SENTINEL" not in text.upper() and "sentinel" not in text


# --- Requests and their fetch manifests ---------------------------------------------------------


def manifest_of(w, req):
    assert req.manifest.status == "held", req.manifest
    return s.parse_manifest(w.evidence(req.manifest.sha256), stored=True)


def test_a_request_stores_a_manifest_of_every_file_after_their_outcomes(world):
    earlier = world.intake.upload(SOURCE, PDF, response=response_for(PDF))
    with world.intake.request(**REQUEST, files_listed=6) as req:
        a = req.upload("a.csv", CSV, doc_id=1, url=URL, response=response_for(CSV))
        b = req.upload("b.bin", BIG, doc_id=2, url=URL, response=response_for(BIG))
        req.unchanged("c.pdf", record=earlier.uuid, doc_id=3)
        req.failed("d.pdf", "source_404", doc_id=4)
        req.upload("e.pdf", HTML, doc_id=5, url=URL, response=response_for(HTML), expect_types=["pdf"])
        req.upload("f.zip", Unread(), doc_id=6, url=URL, response=response_for(b"", length=False,
                                                                     extra=[("Content-Length", str(s.COST_GATE + 1))]))
        assert a.uuid and b.uuid and world.fake.keys(EVD, s.RECORD_PREFIX) == [s.record_key(earlier.uuid)]
    m = manifest_of(world, req)
    assert m["files_listed"] == 6 and m["source"] == REQUEST
    got = {e["filename"]: (e["status"], e["sha256"], e["size"], e["reason"], e["doc_id"]) for e in m["files"]}
    assert got == {
        "a.csv": ("held", sha(CSV), len(CSV), None, "1"), "b.bin": ("held", sha(BIG), len(BIG), None, "2"),
        "c.pdf": ("held", sha(PDF), len(PDF), None, "3"), "d.pdf": ("failed", None, None, "source_404", "4"),
        "e.pdf": ("failed", None, None, "unexpected_type", "5"),
        "f.zip": ("needs_approval", None, s.COST_GATE + 1, "too_large", "6")}
    sc = sidecar_of(req.manifest)
    assert (sc["content_kind"], sc["fetch"]["origin"], sc["fetch"]["run_id"], sc["source"]["filename"]) == (
        "fetch_manifest", "generated", "run-1", s.MANIFEST_FILENAME)
    assert [f for f, r in req.results] == ["a.csv", "b.bin", "c.pdf", "d.pdf", "e.pdf", "f.zip"]
    assert [r.status for _, r in req.results] == ["held", "held", "held", "failed", "failed", "needs_approval"]


def test_a_manifest_lists_rejected_and_deferred_files_as_failed_with_why():
    w = World(max_lambda_size=PART)
    with w.intake.request(**REQUEST) as req:
        req.upload("big.bin", BIG, url=URL, response=response_for(BIG))
        req.upload_file(_tmp_file(CSV), "a.csv", origin="local-copy", legacy_path="a.csv", stamp_ref=sha(b"x"))
    got = {e["filename"]: (e["status"], e["reason"]) for e in manifest_of(w, req)["files"]}
    assert got == {"big.bin": ("failed", "deferred"), "a.csv": ("failed", "missing_blob")}


def _tmp_file(body):
    import tempfile
    f = tempfile.NamedTemporaryFile(delete=False)
    f.write(body)
    f.close()
    return f.name


def test_with_the_lambda_down_a_manifest_lists_its_files_pending():
    w = World(lambda_runs=False, timeout=5)
    with w.intake.request(**REQUEST) as req:
        req.upload("a.csv", CSV, url=URL, response=response_for(CSV))
    assert req.manifest.status == "pending"
    m = s.parse_manifest(w.fake.current(STG, s.staging_data_key(req.manifest.uuid))["data"], stored=True)
    assert [(e["status"], e["reason"]) for e in m["files"]] == [("failed", "pending")]


def test_an_unchanged_listing_stores_one_manifest_blob_and_a_record_per_run(world):
    first = world.intake.upload(SOURCE, CSV, response=response_for(CSV))
    shas = set()
    for _ in range(2):
        with world.intake.request(**REQUEST, files_listed=1) as req:
            req.unchanged("a.csv", record=first.uuid, url=URL)
        shas.add(req.manifest.sha256)
    assert len(shas) == 1 and len(world.fake.keys(EVD, s.BLOB_PREFIX)) == 2
    assert len(world.fake.keys(EVD, s.RECORD_PREFIX)) == 3


def test_unchanged_names_a_record_of_this_request(world):
    other = world.intake.upload({**SOURCE, "request_id": "999"}, CSV, response=response_for(CSV))
    with world.intake.request(**REQUEST) as req:
        with pytest.raises(ValueError):
            req.unchanged("a.csv", record=other.uuid)
        missing = req.unchanged("b.csv", record=s.new_uuid())
    assert (missing.status, missing.reason, missing.terminal) == ("failed", "no_record", False)
    assert [(e["filename"], e["reason"]) for e in manifest_of(world, req)["files"]] == [("b.csv", "no_record")]


def test_a_block_that_raises_stores_no_manifest(world):
    with pytest.raises(RuntimeError):
        with world.intake.request(**REQUEST) as req:
            req.upload("a.csv", CSV, url=URL, response=response_for(CSV))
            raise RuntimeError("the connector crashed")
    assert req.manifest is None
    world.run_lambda()
    assert len(world.fake.keys(EVD, s.RECORD_PREFIX)) == 1  # the staged file is still ingested


@pytest.mark.parametrize("call", [
    lambda req: req.failed("a.pdf", "Not a Code"),
    lambda req: req.failed("", "source_404"),
    lambda req: req.upload("a.csv", CSV, url=URL + "?token=abc", response=response_for(CSV)),
    lambda req: req.unchanged("a.csv", record="not-a-uuid")],
    ids=["reason", "filename", "signed-url", "uuid"])
def test_a_bad_entry_is_refused_at_once(world, call):
    with pytest.raises((s.SchemaError, ValueError)):
        with world.intake.request(**REQUEST) as req:
            call(req)
    assert world.fake.calls == []


def test_a_request_checks_its_own_fields_at_once(world):
    with pytest.raises(s.SchemaError):
        world.intake.request(**{**REQUEST, "host": "other.example.org"})  # not the request_url's host


# --- Construction and adapters ------------------------------------------------


def test_intake_refuses_buckets_that_arent_one_environments():
    prod = s.bucket_name("evidence", "prod", it.ACCOUNT, it.REGION)
    for staging, evidence in ((STG, prod), (EVD, EVD), ("my-bucket", EVD), (STG, OPS)):
        with pytest.raises(ValueError):
            c.Intake(object(), staging_bucket=staging, evidence_bucket=evidence, connector="muckrock")


def test_intake_refuses_a_bad_connector_or_run_id_at_once():
    for kw in ({"connector": "Muck Rock"}, {"connector": "muckrock", "run_id": "a\nb"},
               {"connector": "muckrock", "ci_run": "https://x.example/?token=abc"}):
        with pytest.raises(s.SchemaError):
            c.Intake(object(), staging_bucket=STG, evidence_bucket=EVD, **kw)


def test_from_env_refuses_an_unknown_environment_before_touching_aws(monkeypatch):
    monkeypatch.delenv("PRA_INTAKE_ENV", raising=False)
    for env in (None, "staging"):
        with pytest.raises(ValueError):
            c.Intake.from_env(connector="muckrock", env=env)


def test_ci_run_names_the_actions_run_attempt():
    env = {"GITHUB_ACTIONS": "true", "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/r",
           "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "2"}
    assert c._ci_run(env) == "https://github.com/o/r/actions/runs/42/attempts/2"
    assert c._ci_run({}) is None and c._ci_run({**env, "GITHUB_RUN_ID": ""}) is None


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_a_requests_response_becomes_the_sidecar_response():
    hop = Obj(status_code=301, url="https://www.muckrock.com/foi/files/9/?session=x",
              headers={"location": "/dl/a.csv?token=abc"})
    resp = Obj(status_code=200, url=URL + "?X-Amz-Signature=abc", history=[hop],
               headers={"Content-Type": "text/csv", "Set-Cookie": "a=b"})
    assert c.Response.from_requests(resp).sidecar() == {
        "status": 200, "headers": {"content-type": "text/csv"},
        "redirects": [{"status": 301, "url": "https://www.muckrock.com/dl/a.csv"}], "final_url": URL}


def test_a_playwright_response_becomes_the_sidecar_response():
    resp = Obj(status=200, url=URL, headers_array=[{"name": "ETag", "value": '"x"'}, {"name": "etag", "value": '"x"'}])
    assert c.Response.from_playwright(resp).sidecar() == {
        "status": 200, "headers": {"etag": '"x"'}, "redirects": [], "final_url": URL}


def test_timestamps_follow_the_fetch(world):
    start = world.fake.now - timedelta(seconds=30)
    r = world.intake.upload(SOURCE, BIG, response=response_for(BIG), started_at=start)
    f = sidecar_of(r)["fetch"]
    assert f["started_at"] == s.format_timestamp(start)
    assert s.parse_timestamp(f["started_at"]) < s.parse_timestamp(f["first_byte_at"]) <= s.parse_timestamp(
        f["completed_at"])
