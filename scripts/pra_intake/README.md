# PRA evidence intake

Downloaders ("connectors", one per PRA platform) hand each file to an upload
library. The library streams it into a **staging** bucket. A Lambda then copies
it, write-once, into a content-addressed **evidence** bucket and records how it
got there. `schema.py` is the contract all three share; `client.py` is the
library and `ingest.py` the Lambda. The infrastructure comes in a later PR.

## Flow

1. The library streams the file to staging as `in/<uuid>.bin`: one PUT up to
   `PART_SIZE` (16 MiB), else multipart cut at `upload_part_size(size)`,
   though the contract accepts any S3-legal layout with one part size. Every part carries its SHA-256, and nothing touches local
   disk. Before completing, it checks the byte count against the declared
   length and aborts on a mismatch, so a truncated download never becomes an
   object.
2. It writes the sidecar `in/<uuid>.json` last. The sidecar is the commit
   marker and the only trigger.
3. The Lambda derives every key from the decoded S3 event key, never from the
   sidecar's body. It checks the sidecar belongs to that key and to the data
   object (ETag, size, parts, x-amz-meta), then copies the bytes to evidence
   at `sha256/<hex>`:
   - **Up to 5 GB** (`SINGLE_PUT_MAX`, just under S3's 5 GiB single-request
     limit): one GET streamed into one PutObject carrying the sidecar's claimed
     SHA-256 as `ChecksumSHA256`. S3 verifies every byte, so the claim is
     never trusted and a wrong one is rejected: `sha_mismatch`, or for a file
     staged by one PUT (whose SHA-256 S3 already holds) `data_mismatch` on
     `staging.data.checksum`, before any copy.
     The Lambda hashes MD5 in the same pass (and, when `md5_multipart` is
     set, the MD5 of parts at its part size), and a wrong claim is rejected
     (`data_mismatch`). The blob is a single-part object with S3's
     full-object SHA-256 whatever the staging layout was.
   - **Over 5 GB**: the Lambda hashes the object
     itself, then copies it server-side on the staging object's own part
     boundaries, so the blob's composite checksum equals the staging object's.
     Hashing runs at roughly 60 MB/s in a 15-minute Lambda, so above
     `LAMBDA_MAX_SIZE` (20 GB) the Lambda tags the file `intake=deferred` and
     `python -m pra_intake.ingest process in/<uuid>.json --allow-large` runs
     the same steps where there's no time limit.
   - **Over 50 GB** (`COST_GATE`): only with `fetch.approval` naming an
     admin-written `approvals/<uuid>.json` in the ops bucket: strict JSON in
     UTF-8 without a BOM (no duplicate keys, no floats) with every key of
     `_APPROVAL_SPEC` present and no others (`note` may be null; an unknown
     key is refused as `too_large`) and `expires_at` in the canonical form
     (`2026-10-28T00:00:00Z`). The approval names one source (kind, platform,
     host, request, doc id, URL), a size ceiling and an expiry; the Lambda
     reads it and `check_approval` refuses a file from any other source
     (`too_large`, with the field and problem in the log). It covers every
     upload from its source committed before it expires (the sidecar's write
     time, by S3's clock; a sidecar must be one PUT), however late the Lambda
     or the one-off job gets to it. Keep expiries short, but past the end of
     the whole fetch and upload (about 52 minutes per 50 GB at 16 MB/s): the
     clock stops only when the sidecar lands. Every file over `COST_GATE` is
     also over `LAMBDA_MAX_SIZE`, so it waits for the job, which reads the
     approval again: keep the approval object until every file it covers has
     a record. Expiry limits when an upload may be committed, not how long
     the object is kept. A file refused for a lapsed approval needs no
     re-upload: extend the same `approvals/<uuid>.json` and run
     `process in/<uuid>.json --allow-large`.
4. It reads the blob back (checksum, size, Object Lock), then writes the
   intake record `_intake/<uuid>.json` (write-once, one per sighting). Last, it
   tags both staging objects `ingested=true`; lifecycle removes tagged objects.
5. The library waits for one of three outcomes. It succeeds when
   `_intake/<uuid>.json` exists and its sha256 equals the hash the library
   computed itself. It stops early when the staging objects are tagged
   `intake=deferred` or `intake=rejected` (with `reason`): the outcome so
   far. A record written later (the one-off job, or a retry after a
   transient error) supersedes those tags, so a record is always the answer
   and a later run lists the file `held` once one exists. With no outcome by
   its own timeout, it stops without one. It reads the outcome from the tags
   and records, never infers it from the file's size.

Identical bytes from any source (MuckRock, a portal, a local copy) are stored
once. While the blob's current version exists, the second copy is found by
HeadObject (a 412 only when two writers race), its staged bytes are read and
checked, and it adds only a record. (If an admin's delete marker is current,
the write succeeds as a new version; read-back verifies it either way.)
Records don't say which sighting was first; the earliest record for a blob
version is. One exception: a blob over 5 GB records the part boundaries it
was copied on, and v1 records one layout per blob, so the same bytes staged
on other boundaries are rejected `layout_conflict` (the bytes themselves
aren't compared). The library cuts parts at `upload_part_size(size)`, a
function of the size alone, pinned in `tests/fixtures/pra_intake/part_sizes.json`,
so this only happens if that function changes. A stored blob of another size, or without a
SHA-256 composite, is `evidence_conflict`.

## The library

`client.Intake` stages files for one connector run.
`Intake.from_env(connector=..., env="dev")` names the buckets from the
caller's AWS account and region (boto3 isn't a project dependency:
`uv run --with boto3`). A connector works one PRA request at a time:

```python
intake = Intake.from_env(connector="muckrock", connector_version=commit)
with intake.request(kind="muckrock", platform="muckrock", host="www.muckrock.com",
                    request_id="12345", request_url=url, files_listed=len(files)) as req:
    for f in files:
        if f.id in state and state[f.id].change_key == f.change_key:
            req.unchanged(f.name, record=state[f.id].uuid, doc_id=f.id)
            continue
        started = datetime.now(timezone.utc)
        with session.get(f.url, stream=True, timeout=(10, 300)) as resp:  # requests has no default timeout
            req.upload(f.name, resp.iter_content(1 << 20), doc_id=f.id, url=f.url, started_at=started,
                       response=Response.from_requests(resp), expect_types=["pdf"])
for name, result in req.results:
    ...  # keep result.uuid (whenever there is one) and an attempt count per file
done = req.manifest.terminal and all(r.terminal for _, r in req.results)
```

**What to pass.** `url` is the URL requested, before any redirect; the
library stores it through `strip_signing_params` (no signature or session
parameters), and refuses one whose redirect chain began elsewhere (passing
the final, signed URL would lose where the fetch began). The body is what
a browser would save (content-decoded: `resp.iter_content`, not `resp.raw`,
whose gzip bytes are refused as `still_encoded`), as bytes, a binary file
object, or an iterable of bytes chunks; anything else (a str, ints, endless
empty chunks such as `iter(partial(f.read, n), '')` gives) raises
`TypeError` rather than become other bytes or spin. `Response.from_requests` and
`Response.from_playwright` (a page `Response`, or the `APIResponse` of
`context.request.get`, which exposes no redirect hops) record what the
client saw; or build a `Response` from the status, the header pairs as
received, the final URL, and each redirect as (status, the URL that
answered, its Location). A Playwright `Download` has neither status nor
headers: fetch through `context.request.get`, or capture the page
`Response` that delivered it. A download the client saved to disk goes
through `upload_file(path, name, response=...)` (origin `live`), checked
against the response's Content-Length. A live fetch's declared length is
always its response's Content-Length (`declared_length=` is for backfills).
Fetch manifests are written only by a `Request`: `stage()` refuses one.

Times are timezone-aware datetimes. For a stream the library times the
fetch itself: `started_at` (pass it from before the request; else it's when
`stage()` was called), then the first byte and the end as it reads. For
bytes, a `BytesIO` or a regular file (already fetched) it didn't see the
fetch: `first_byte_at` is null, and
`started_at` and `completed_at` are what the connector passes (else null,
and when the library got the bytes). A time up to a minute ahead of the
library's clock is taken as the clock stepping back.

Values: `kind` is one of `muckrock`, `portal`, `own` (`SOURCE_KINDS`);
`platform` one of `PLATFORMS` (`muckrock`, `nextrequest`, `govqa`,
`justfoia`, `logikcull`, `email`, `fileshare`, `other`); `access`
`anonymous` or `requester`; `released_on` a date like `2026-09-28`;
`expect_types` from `SNIFF_TYPES` (a CSV is `text`); reason codes for
`failed()` are lower-case letters, digits and `_`.

**What the library checks.** Before reading a byte: the source, fetch,
response and listing under write policy (`validate_context`); a response
other than 200 or 203 fails as `http_<status>`, a declared size S3 couldn't
hold as `over_s3_limit`, a Content-Range that isn't the whole file as
`partial_response`, and a size over `COST_GATE` without an approval is
`needs_approval`. While reading, it hashes in memory, never on disk: about
32 MiB for a file that goes in one PUT and about 96 MiB for a multipart one
(`MAX_IN_FLIGHT`, 64 MiB of parts uploading, plus the part being read;
more only for an approved file over about 167 GB). The type, the length and
the whole sidecar are checked before the data becomes an object (a
multipart upload is aborted instead), so a refused file leaves nothing in
staging. The data object is written, then the sidecar; a PUT that landed
but lost its answer (a dropped connection, a 5xx) counts as written once
the object is seen there. Only a failure between the two (S3, or the
process killed) leaves data without a sidecar, which the sweep reports.

**Waiting.** `Intake.upload()` returns a `Result`; `Intake.stage()` and
`Request.upload()` return a `Staged` at the commit, whose `result()` waits.
`wait(handles)` checks files 16 at a time, so waiting for many costs about
as long as the slowest. Each file's timeout runs from when the wait starts,
and a `pending` result is looked at again by the next wait (the end of a
request's block included). A request waits for all its files when its block
ends, then stores the fetch manifest and waits for that too
(`req.manifest`); afterwards it takes no more files. A block that raises
stores no manifest. A request isn't thread-safe.

| `status` | Meaning | The connector |
|---|---|---|
| `held` | the intake record exists and names the sha256 the library computed (`record` holds it) | acks |
| `rejected` | the Lambda refused the upload; `reason` is the reject code | acks; the file waits in staging for a person |
| `deferred` | over `LAMBDA_MAX_SIZE`: the one-off job records it | acks |
| `needs_approval` | over `COST_GATE` with no approval (`reason` `too_large`) | acks and reports it |
| `pending` | no outcome by its timeout (120 s plus 1 s per 30 MB, at most 20 minutes); it's committed and will be ingested | keeps the uuid; next run, `unchanged()` |
| `failed` | nothing held; `reason` says why (below) | fetches again, up to a cap, then `failed(..., final=True)` |

`terminal` is true for the first four and for a file given up on with
`failed(..., final=True)`. Failure reasons: `http_<status>`, `truncated`,
`overlong`, `partial_response`, `still_encoded`, `unexpected_type`,
`undeclared_length`, `over_s3_limit`; `read_error` (the body raised: in a
request, only that file fails); a schema reason code such as
`invalid_metadata` (in a request, a bad value in one file's source, such as
an agency's timestamp as `released_on`, fails only that file and logs
`invalid_file` with the field); `no_record` or `record_mismatch` from
`unchanged()`; or the connector's own from `failed()`. Some come from the
source and repeat on every fetch (a 404, an HTML login page), hence the
cap: count attempts (`attempt=`).

**Between runs.** Keep each file's uuid whenever its result has one, and
call `unchanged(name, record=uuid, ...)` for a file the listing shows
unchanged. It lists the file `held` if the record exists and is this
file's (the same doc id when both name one, else the same url when both
name one, else the same filename; a renamed file with the same url is the
same file), filling in the doc id and url from the record; `failed`,
`record_mismatch` if it's another file's (fetch it again). With no record
yet it reports the outcome so far (`deferred`, `rejected`, `pending`) or
`failed`, `no_record`, for an upload staging doesn't know. A record of
another request or a manifest's raises `ValueError`: a uuid from the wrong
state. A file tried more than once in a run is listed once, as it ended; a
listing count that doesn't match the entries is logged
(`listing_mismatch`).

**Exceptions.** `SchemaError`, `ValueError` or `TypeError` is a connector
bug; before any byte moves except a `TypeError` from a body that turns out
not to be bytes (its multipart upload is aborted). A body whose read fails
as a connection does is `BodyError` from `Intake.stage` (a request records
`read_error`); any other exception from the body propagates as itself. An
S3 error (after botocore's retries) aborts the file and propagates: retry
the work item. While waiting, a throttle, a 5xx or a dropped connection is
logged (`poll_failed`) and looked at again; any other S3 error ends the
wait (other files' outcomes that round are kept), AccessDenied on the
file's own tags included (a missing grant would otherwise leave every file
`pending`). `IntakeError` means S3 or the Lambda answered for bytes other
than the ones sent, or tagged a file ingested whose record the library
can't read: don't ack; alert.

Backfills use `upload_file(path, source, legacy_path=...,
original_fetched_at=...)` (origin `local-copy`, or `git` with
`git_commit`), and `stamp_ref` names a stamped manifest already held.

Known limit: a 200 with a Content-Encoding and a Content-Range covering the
whole *encoded* file fails as `partial_response` (schema 1 compares the
range to the decoded size). Fetch such a file without `Accept-Encoding`.

## The Lambda

`ingest.handler` takes SQS messages carrying S3 `ObjectCreated` events for
`in/*.json`, or a sweep's re-drive `{"staging_key": ...}`; anything else
(tagging events, the `s3:TestEvent`, other buckets) is ignored. Each staged
file ends one of four ways:

| Outcome | Staging tags | Next |
|---|---|---|
| `stored`, `already_stored`, `recorded` | `ingested=true`, `sha256=<hex>` | lifecycle removes both objects |
| `rejected` | `intake=rejected`, `reason=<code>` | kept for a person; the reason is one of `REJECT_REASONS` |
| `deferred` | `intake=deferred` | the one-off job above |
| transient error | none new, or a `rejected`/`deferred` tag written just before it | SQS retries after 60 s, then the DLQ; the retry settles the tags |

A rejection is a fact about the upload: a bad sidecar, bytes that
contradict it, a data object that isn't the one it describes, a blob it
depends on that isn't held. Everything else is retried: throttling, 5xx, a
dropped stream, a stored record that doesn't parse, and an Object Lock S3
doesn't show (a missing `s3:GetObjectRetention`; a bucket without Object
Lock fails at cold start; on an existing blob whose HeadObject shows no lock the Lambda first asks
GetObjectRetention, which fails the same way, then requests the default
lock, which S3 refuses if it would shorten or weaken one), and a
checksum S3 doesn't show on a blob this run just wrote or on one encrypted
with SSE-KMS (a key the role can't use). Those are the deployment's
problems, and the DLQ and its alarm are where they surface; `lock_missing`
stays in the vocabulary but the Lambda doesn't tag it.

A sighting of an existing blob with less than half the bucket's default
retention left renews its lock to a full period from now (`lock_renewed`), in
the blob's own mode while its lock is live (S3 won't change a live
COMPLIANCE lock) and in the rule's mode once it has lapsed. The rule is
re-read before each renewal and at most `RETENTION_TTL` (15 minutes) after a
container last read it, so a shortened rule applies at once and a lengthened
one within 15 minutes. A version this run just wrote is never renewed (S3
locked it under the current rule), and neither is a blob under an admin's
event hold: it can't lapse, and while the hold is on S3 reports a computed
date still to come, which the record states. (A held blob showing a date
already past, which S3 doesn't produce, would be retried as `lock_held`.) So a blob stays locked
for at least half a period after its latest sighting, and deduplication
keeps working after a lock would have lapsed (dev's one-day lock, or prod's
in ten years). The code only ever asks for `now + period`; without
`s3:BypassGovernanceRetention` S3 lets the role lengthen a lock, never
shorten or weaken one, and the evidence bucket policy caps how far (see
Deployment). A renewal that loses a race to a longer one is accepted
(`lock_renewed_elsewhere`). Records state the lock as read back when they
were written; a later renewal only lengthens it.

Every step can be re-run: writes carry If-None-Match, and a run that finds
its record re-tags and stops. A run that tags a file rejected or deferred
then looks for a record once more, so a racing run that recorded it wins.
The Lambda reads every staged byte once per attempt (a fetch manifest is read
whole once, checked in memory and sent from memory; botocore's own retries,
up to 3, and a 412 race read a file again, so near `LAMBDA_MAX_SIZE` the
worst case is several reads) and checks every claim about the bytes: the SHA-256 (S3 checks it on the evidence write up to 5 GB), the MD5,
the source's multipart ETag, the staging part layout, and the sniffed type.
Up to 5 GB that read is the evidence upload itself, and a wrong claim fails
it from inside its body before the last bytes are sent, so nothing is stored
for it. (Checked live 2026-09-28 with botocore's CRT and pure-Python
signers.) botocore's own retries rewind that body: the staging GET is opened
again, pinned to the same ETag, and the hashes start over (also when the
stream failed before its first byte), so a throttled or reset upload is
retried in the call and, if it still fails (or the staging GET can't be
reopened), logged with the failing call's S3 code and status. A duplicate's bytes are read and checked the same way.

`ingest.sweep` (scheduled) re-drives each untagged sidecar older than an hour,
newest first, at most `MAX_REDRIVES` times (counted in a `redrives` tag). It
reads tags `WORKERS` at a time (about 20 a second each, so a run covers tens
of thousands of staged files; lifecycle removes ingested ones after a day).
It logs one `sweep_file` line (uuid, state, reject reason) for each file that
needs a person (rejected, deferred, stuck, orphaned data without a sidecar),
then the counts, including stray keys, how many it didn't read (`unread`:
files it had no time to reach, the ones whose read failed, and every file
after the chunk an error ended), whether it read the whole listing
(`listed`), and the error that ended it early (`error`).
The sweep's `redrives` write replaces the sidecar's tag set (S3 has no
conditional tagging), so an outcome tag the Lambda writes between the
sweep's read and its write is lost; the same run re-drives the file, and
processing it tags the outcome again. After a failed send the sweep gives
the re-drive back only if no outcome was tagged meanwhile.
Logs hold uuids, SQS message ids, reason codes, field paths, problem
descriptions (fixed text), sizes and code locations (file:line:function of
this package), never presented text, URLs, keys that aren't ours, or
metadata values. Best-effort calls that fail are logged too: an abort
(`abort_failed`) or a visibility change (`visibility_failed`). An unexpected error fails only its
own message and is logged as `message_error` (or `transient` with `where`)
rather than failing the invocation, so alarm on those lines as well as on the
DLQ.

**Order of writes.** Whatever a file refers to must be recorded before the
file is staged: the Lambda rejects a fetch manifest naming a sha256 evidence
doesn't hold, or a backfill whose `stamp_ref` isn't held (`missing_blob`),
and never retries it. So a manifest lists a file `held` only once the
library has seen that file's intake record. A file the Lambda deferred has
no record yet, so the manifest lists it `failed` with reason `deferred`; a
rejected file is listed `failed` with its reject code as the reason, and one
with no outcome by the library's timeout `failed` with reason `pending`. A
later run lists any of them `held` once a record exists. An admin's delete
marker over a blob hides it from the Lambda too: a manifest naming it is
rejected `missing_blob` until the bytes are uploaded again (a new version),
so a connector re-fetches such a file rather than list it from an old
record.

### Deployment (PR 4 pins these)

- The evidence bucket: Object Lock enabled (with versioning) and a default
  retention rule (mode and days or years), fixed retention only: no
  `DefaultEventHold` (the Lambda refuses such a rule at cold start). New
  blobs get it from the rule; the Lambda reads the rule at cold start (a
  bucket without one fails every invocation), within `RETENTION_TTL` after,
  and before each renewal. Adding the rule later doesn't lock blobs already
  written until they are next sighted. Default encryption SSE-S3 (AES256) in
  the bucket's encryption configuration, and deny `s3:PutObject` when
  `s3:x-amz-server-side-encryption` is present and not `AES256`.
- The evidence bucket policy caps what the ingest role's retention grant
  could do. Conditions within one statement are ANDed, so these are
  separate Deny statements, each for every principal but admins, on
  `sha256/*`:
  1. `s3:PutObjectRetention` when `NumericGreaterThan
     s3:object-lock-remaining-retention-days` exceeds D + 1, where D is the
     rule's Days (or 365 x Years). Raise D here before, or in the same
     change as, lengthening the rule, or renewals are denied and those
     files wait in the DLQ.
  2. `s3:PutObjectRetention` when `StringEquals s3:object-lock-mode` is
     `COMPLIANCE`, with a GOVERNANCE default only. The Lambda renews a live
     lock in its own mode, so a COMPLIANCE-locked blob's duplicates then
     wait in the DLQ until it lapses: keep a blob with a legal hold, not
     COMPLIANCE.
  3. `s3:PutObjectRetention` and `s3:PutObject` when `Null
     s3:object-lock-event-hold` is false: the request sets an event hold at
     all, on or off. The Lambda never sends one (S3 evaluates the key against
     the request, not a default rule's hold), and admins, exempt, can still
     place or release a hold. PR 4 re-runs the live probe's renewal case with
     this policy applied, to confirm a plain renewal carries no such key.
  4. `s3:PutObject` when `Null s3:object-lock-mode` is false (the Lambda never
     sends lock headers; don't use `StringNotEquals`, which also matches an
     absent key and would deny every write).

  No role here gets `s3:PutObjectLegalHold`.
- IAM: `ingest.PERMISSIONS` for the ingest role and `ingest.SWEEP_PERMISSIONS`
  for the sweep, per bucket role and key prefix (`None` is the bucket
  itself: `ListBucket` with no `s3:prefix` condition, since HeadBucket sends
  none). The tests run the Lambda with exactly these grants, and it writes
  nothing outside `sha256/` and `_intake/`. Without `s3:ListBucket` a missing
  key reads as 403, not 404; without `s3:GetObjectRetention` HeadObject
  hides the lock. No role here gets `s3:BypassGovernanceRetention` or any
  delete. SQS: `ReceiveMessage`, `DeleteMessage`, `GetQueueAttributes` (the
  event source) and `ChangeMessageVisibility` for the ingest role;
  `SendMessage` for the sweep's.
- The one-off job (`process --allow-large`) runs as the ingest role
  (assumed), since the records and tags it writes are the Lambda's; under
  any other principal it can store and record but not tag. It exits 0 when
  the file is recorded, 1 when rejected, deferred or vanished, 75
  (`EX_TEMPFAIL`) on a retryable error, at startup too (the tags settle when
  it is run again after the cause is fixed); argparse's usage errors exit 2.
  It records `--code-sha256` only when given (pass it only when running the
  published zip); otherwise the record says null. Records the Lambda writes
  name its published zip (below).
- The writer (library) role, `client.PERMISSIONS` (the tests run the library
  with exactly these): `s3:PutObject` and `s3:AbortMultipartUpload` on
  `in/*` (If-None-Match is enforced by the bucket policy below; don't put an
  if-none-match condition on the writer's own policy, which would deny its
  multipart uploads), `s3:GetObjectTagging` on `in/*` to read the Lambda's
  outcome, and `s3:GetObject` on evidence `_intake/*` to read its records
  (without `s3:ListBucket` there, a record not written yet reads as 403: not
  yet). Sidecars are written with one PUT; the Lambda rejects any other.
- The ops bucket: no lifecycle rule may touch `approvals/` (an approval must
  outlive the job for every file it covers).
- The staging bucket policy, as Denies (within one account an Allow in a
  bucket policy restricts no one), each naming both `s3:PutObjectTagging`
  and `s3:PutObjectVersionTagging` (a tagging request with a versionId, even
  `null`, is authorised as the latter): deny them on `in/*` to every
  principal but the ingest role, the sweep role and admins; and for the
  sweep role two more Denies (their conditions are ANDed, so one can't hold
  both): `ForAnyValue:StringNotEquals s3:RequestObjectTagKeys ["redrives"]`,
  and `Null s3:RequestObjectTagKeys` true. Writers may set no tags (the
  sweep never writes an empty tag set).
- Staging lifecycle: expire objects tagged `ingested=true` after 1 day; and,
  as its own rule (S3 won't combine it with a tag filter), abort incomplete
  multipart uploads under `in/` after 7 days. A staging upload must finish
  within that window (about 9.6 TB at 16 MB/s): don't approve a `max_size`
  the uploader can't upload in time.
- If-None-Match, on the staging and evidence buckets: deny `s3:PutObject`
  when `Null s3:if-none-match` is true **and** `Bool
  s3:ObjectCreationOperation` is true (checked live 2026-09-28). The second
  condition matters: UploadPart and UploadPartCopy are also `s3:PutObject`
  and carry no If-None-Match, so a policy without it blocks every multipart
  upload, including every copy over 5 GB.
- S3 notifications on the staging bucket for `ObjectCreated:*`, prefix `in/`,
  suffix `.json`, to the queue. The queue policy allows S3 to send for that
  bucket (`aws:SourceArn`, `aws:SourceAccount`), and has one Deny on
  `sqs:SendMessage` with two conditions, ANDed: `Bool aws:PrincipalIsAWSService`
  false, and `ArnNotEquals aws:PrincipalArn` [the sweep role, admin roles]
  (a lone `ArnNotEquals` would also deny S3, whose service principal has no
  `aws:PrincipalArn`; admins must be exempt to redrive the DLQ). A record's
  `ingest.principal` is what the message's S3 event reported; the sweep role
  and admins could forge one (the sweep sends only `{"staging_key": ...}`,
  which carries none), so the principal is as trustworthy as they are.
  The event source: `BatchSize` 1, `ReportBatchItemFailures`; function
  timeout 900 s; queue visibility timeout at least six times that (5400 s)
  and `maxReceiveCount` at least 5, as AWS advises for Lambda consumers, then
  a DLQ with an alarm. Handled failures come back after 60 s regardless.
- The sweep function: an hourly schedule, timeout 900 s (it stops
  `SWEEP_RESERVE_MS` before the end and logs what it didn't read), 512 MB,
  and an async invoke config with `MaximumRetryAttempts` 0 (the next hourly
  run is the retry; Lambda's own retries would re-drive the same files within
  minutes and use up their `redrives`). Alarm on its Errors metric, or on
  the `error` key in its count line.
- Environment: `STAGING_BUCKET`, `EVIDENCE_BUCKET`, `OPS_BUCKET`,
  `CODE_SHA256` and `QUEUE_URL` on the ingest function (without `QUEUE_URL`
  a failed message waits the full visibility timeout, not 60 s);
  `STAGING_BUCKET` and `QUEUE_URL` on the sweep.
- `CODE_SHA256`: the zip's SHA-256 in lower-case hex, not Lambda's base64
  `CodeSha256`. `Ingest` refuses anything else, and bucket names that aren't
  one environment's staging, evidence and ops buckets. The event source
  invokes an alias pointing at a published version, never `$LATEST`, and
  `CODE_SHA256` is set before that version is published (a version's
  environment can't change), so a write-once record names the zip that
  wrote it.
- Memory 2048 MB. `LAMBDA_MAX_SIZE` assumes the hashing rate measured at that
  setting, and a 64 MiB manifest needs about 750 MiB.
- boto3/botocore vendored in the zip, pinned exactly, botocore 1.36 or later
  (the `Config` checksum options; older versions fail every invocation).
- A lifecycle rule on the evidence bucket aborting incomplete multipart
  uploads after a day (a timeout mid-copy can't abort its own).

Known limit: a principal that can write to staging can stage a file over
5 GB on its own part boundaries first, so honest copies of the same bytes
are rejected `layout_conflict` until an admin replaces the blob. Only the
library's role writes to staging. The Lambda could refuse every layout other
than `upload_part_size`'s; it doesn't yet.

## Buckets

`<prefix>-<role>-<account>-<region>-an`. The prefix is `sm-alpr` (prod) or
`sm-alpr-dev` (dev). Roles:

| Role | Holds |
|---|---|
| staging | `in/<uuid>.bin`, `in/<uuid>.json` (SSE-S3, no Object Lock; tagged objects expire) |
| evidence | `sha256/<hex>`, `_intake/<uuid>.json`, `_errata/<uuid>/<nnnn>.json` (SSE-S3, Object Lock) |
| ops | Lambda code, `approvals/<uuid>.json`, inventory reports (admin only) |
| derived | processor outputs and the provenance index (later) |

#822's bucket (`sm-alpr-pra-…`) is retired. No role can produce its name. A
record names one environment's evidence and staging buckets.

## Documents

All three serialize with `canonical_json` (sorted keys, ASCII, compact, one
trailing newline). Parsers insist on exactly those bytes, so a document's
hash can always be recomputed from what it says. (Approvals, written by hand,
need only be strict JSON: no approval's hash is recorded.) Every field has a byte cap. For
sidecars and records the document limits sit above the worst case the caps
allow, so one that validates always serializes; a manifest is capped at
`MAX_MANIFEST_BYTES`, and validation says so before anything is written.

- **Sidecar**: the writer's claim. It holds:
  - **data:** size, sha256, md5, the source's multipart-ETag form (at the part
    size that reproduced it), and how the file was uploaded (method, parts);
  - **source:** kind, platform, host, agency, request id and URL, doc id,
    filename, title, stable URL, release date;
  - **fetch:** origin (`live`, `local-copy`, `git`, or `generated` for a fetch
    manifest), ids, connector, timings, and the approval for a file over the
    cost gate;
  - **response:** status (200 or 203), allow-listed headers, the redirect chain
    (301, 302, 303, 307 or 308, each hop resolved to an absolute URL), and
    the final URL;
  - **listing:** the entry as the source listed it;
  - **checks:** everything the library verified. A source ETag or Content-MD5
    that disagrees is recorded (`unmatched`, `mismatch`), not refused, because
    the bytes are still evidence.

  Every key is required (`null` when absent), and unknown keys are refused.
- **Intake record**: the sidecar verbatim plus what the Lambda verified: the
  blob's version, checksum and lock, and the staging object's ETag, encryption,
  checksum, and the hash of the sidecar's bytes. It has no clock and no request
  id. `staging.data_last_modified` is S3's LastModified for the data object,
  which for a multipart upload is when the upload began, not when it
  completed; the commit time is the sidecar's LastModified, which schema 1
  doesn't record. Two writes for one sighting can differ only in `ingest` (code version,
  trigger path) and in the lock read back. After a 412 the stored record
  stands if it describes the same staging objects (sidecar hash, data
  ETag); a different `record_core` (another blob version) is logged.
- **Fetch manifest**: what one run found for one request. Each file gets its
  name, doc id, URL, sha256, size and status (`held`, `failed`, or
  `needs_approval` with reason `too_large`). It is stored as a blob like any
  file (`origin: generated`). The Lambda checks the manifest's sidecar
  describes the same request (`validate_manifest_sidecar`) and that every
  sha256 it names is held (`missing_blob`); a backfill's `stamp_ref` is
  checked the same way. The body has no clock or run id, and `held`
  doesn't say whether this run downloaded the file or recognized it as
  unchanged, so an unchanged listing is stored once. The run lives in the
  manifest's record: `fetch.run_id` joins it to the file records the same
  run wrote. Files are ordered by (filename, doc_id, url, sha256, status,
  reason, size), null first, strings by Unicode code point, size by number;
  identical entries collapse, and `files_listed` keeps the listing's own
  count.

`fetch_id` is one upload call, `run_id` one connector run (required for live
and generated uploads), and `work_id` one queue item.

## Rules

- **Invariants always; write policy only when writing.** Shape, formats,
  limits, and the fields agreeing with each other and with the hashes hold
  for every document. Write policy (no credentials, the header allow-list,
  visible single-line strict fields, the cost gate and its approvals)
  applies when a document is written. Reading a stored one skips it, so
  tightening the policy can never make stored evidence unreadable.
  `parse_record` and `parse_manifest` take a required `stored=` so every
  caller says which it is.
- **Permanent means versioned.** Each schema version has its own validator,
  and `READABLE_SCHEMAS` keeps every version ever written. Records dispatch on
  (schema, deriver), and a newer deriver reads as `schema_version`. Never
  change what an existing version accepts: add a version, and deploy the
  Lambda before the library (staging uploads already written keep their own
  version). `tests/fixtures/pra_intake/v1/` holds frozen schema-1 documents
  that must parse forever and are never regenerated. `vocabulary.json` pins
  the invariants (changing them needs a new version); `policy.json` pins the
  write policy (tightening it is fine: update it deliberately). The checksum
  formulas are pinned to values S3 returned in live probes.
- **The sniff is part of the schema.** `checks.sniffed_type` is
  `expected_sniff(content_kind, first min(size, SNIFF_BYTES) bytes)`, and the
  Lambda recomputes it. `tests/fixtures/pra_intake/sniff_v1.json` pins what
  `sniff_type` returns: changing that needs a new schema version.
- **Presented text is kept exactly.** Filenames, titles and agency names may
  hold any character. `canonical_json` escapes non-ASCII; use `display_safe`
  before showing such text to a person or a model. Identifiers and the
  fetcher's own fields are strict: visible and single-line.
- **No credentials stored** (write policy):
  - Secret query parameters are refused, also percent-encoded or nested:
    `sig`, `Signature`, any `X-Amz-*`/`X-Goog-*`/`X-Oss-*`/`oauth*`, and any
    name ending in `token`, `secret`, `password`, `sessionid`, `sessid`,
    `apikey`, `accesskey` or `secretkey`, plus `sid`, `ticket`, `CFID`,
    `CFTOKEN` and a few others. In a URL a secret name counts even without
    `=`; in other text only as `name=`. Names are
    compared ignoring case, `-` and `_`. Pagination cursors (`pageToken`,
    `nextToken`, `resumptionToken`…) are not credentials. A signed URL's companions (`st`, `se`,
    `sr`, `Expires`, `Policy`, key ids…) go with its secret; alone they are
    ordinary parameters.
  - ASP.NET cookieless and Java path sessions, user info, and well-known
    token shapes (JWTs, AWS key ids) are refused.
  - Response headers outside `ALLOWED_HEADERS` are refused
    (`forbidden_header`).
  - AWS key ids are refused anywhere in a field the fetcher writes, but only
    as a parameter value in text that can hold an agency's filename (URLs,
    headers, doc ids, old paths), where an upper-case name could look like
    one.
  - Connectors canonicalize with `strip_signing_params` (pass the URL the
    HTTP client prepared, not a raw href; it returns a storable URL or
    raises, keeping visible ASCII byte for byte and percent-encoding the
    rest as clients do; a non-ASCII host must already be in A-label form), `redirect_url` (never raises; a hop always
    validates, keeping only scheme and host when its path carries a
    credential or is too long, and is "" only for a Location no HTTP client
    could follow) and `sanitize_headers` (its output always validates). Hand
    `sanitize_headers` the wire bytes, or the Latin-1 view `http.client` and
    requests give; a client that decodes UTF-8 itself (httpx) should pass
    its raw header bytes.
  - A capability carried in the URL path (Dropbox `/s/`, OneDrive `1drv.ms`,
    SharePoint `/:x:/` share links) can't be told from an ordinary path. The
    path is how the agency published the document, so it is stored as
    found; prefer a non-capability URL for `source.url` when one exists.
  - Redirect hops and `final_url` record where the bytes actually came from,
    so they may name an IP address or a single-label host; `source.url` and
    `request_url` need a DNS name.
- **No values in errors.** A `SchemaError` carries a reason code (one of
  `REJECT_REASONS`, also used as the reject tag) and a field path, never a
  value or a header name.
- **Standard library only**, plain-ASCII source, and no syntax newer than
  Python 3.12, so the module ships unchanged in the Lambda zip.
