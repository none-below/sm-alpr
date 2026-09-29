# PRA evidence intake

Downloaders ("connectors", one per PRA platform) hand each file to an upload
library. The library streams it into a **staging** bucket. A Lambda then copies
it, write-once, into a content-addressed **evidence** bucket and records how it
got there. `schema.py` is the contract all three share; `ingest.py` is the
Lambda. The library and the infrastructure come in later PRs.

## Flow

1. The library streams the file to staging as `in/<uuid>.bin`: by default one
   PUT up to 16 MiB and multipart above, though the contract accepts any
   S3-legal layout with one part size. Every part carries its SHA-256, and nothing touches local
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
     admin-written `approvals/<uuid>.json` in the ops bucket: strict JSON (no
     duplicate keys, no floats) with every key of `_APPROVAL_SPEC` present
     (`note` may be null) and `expires_at` in the canonical form
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
aren't compared). The library makes the part size a function of the file
size alone (PR 3 puts it in `schema.py` and pins it), so this only happens
if that function changes. A stored blob of another size, or without a
SHA-256 composite, is `evidence_conflict`.

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
doesn't show (a missing `s3:GetObjectRetention`, a bucket without Object
Lock; the Lambda first asks for the default lock, which S3 refuses if it
would shorten or weaken one), and a checksum S3 doesn't show on a blob this
run just wrote (SSE-KMS with a key the role can't use). Those are the
deployment's problems, and the DLQ and its alarm are where they surface;
`lock_missing` stays in the vocabulary but the Lambda doesn't tag it.

A sighting of an existing blob with less than half the bucket's default
retention left renews its lock to a full period from now (`lock_renewed`), by
the rule as it is at that moment (re-read before each renewal; a version this
run just wrote is never renewed, since S3 locked it under the current rule).
So a blob stays locked for at least half a period after its latest sighting,
and deduplication keeps working after a lock would have lapsed (dev's one-day
lock, or prod's in ten years). The code only ever asks for `now + period`;
without `s3:BypassGovernanceRetention` S3 lets the role lengthen a lock, never
shorten or weaken one, and the evidence bucket policy caps how far (see
Deployment). A renewal that loses a race to a longer one is accepted.
Records state the lock as read back when they were written; a later renewal
only lengthens it.

Every step can be re-run: writes carry If-None-Match, and a run that finds
its record re-tags and stops. A run that tags a file rejected or deferred
then looks for a record once more, so a racing run that recorded it wins.
The Lambda reads every staged byte once (a fetch manifest twice: once to
parse it) and checks every claim about the bytes: the SHA-256 (S3 checks it on the evidence write up to 5 GB), the MD5,
the source's multipart ETag, the staging part layout, and the sniffed type.
Up to 5 GB that read is the evidence upload itself, and a wrong claim fails
it from inside its body before the last bytes are sent, so nothing is stored
for it. (Checked live 2026-09-28 with botocore's CRT and pure-Python
signers.) botocore's own retries rewind that body: the staging GET is opened
again, pinned to the same ETag, and the hashes start over, so a throttled or
reset upload is retried in the call and, if it still fails, logged with S3's
code and status. A duplicate's bytes are read and checked the same way.

`ingest.sweep` (scheduled) re-drives each untagged sidecar older than an hour,
newest first, at most `MAX_REDRIVES` times (counted in a `redrives` tag). It
reads tags `WORKERS` at a time (about 20 a second each, so a run covers tens
of thousands of staged files; lifecycle removes ingested ones after a day).
It logs one `sweep_file` line (uuid, state, reject reason) for each file that
needs a person (rejected, deferred, stuck, orphaned data without a sidecar),
then the counts, including stray keys, how many it had no time to read,
whether it read the whole listing (`listed`), and the error that ended it
early (`error`).
Logs hold uuids, SQS message ids, reason codes, field paths, sizes and code
locations (file:line:function of this package), never presented text, URLs,
keys that aren't ours, or metadata values. An unexpected error fails only its
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
  retention rule (mode and days). New blobs get it from the rule; the
  Lambda reads the rule at cold start (a bucket without one fails every
  invocation) and again before each renewal. Adding the rule later doesn't
  lock blobs already written until they are next sighted. Encryption SSE-S3
  only: deny `s3:PutObject` when `s3:x-amz-server-side-encryption` is present
  and not `AES256`.
- The evidence bucket policy caps what the ingest role's retention grant
  could do, for every principal but admins, on `sha256/*`: deny
  `s3:PutObjectRetention` when `NumericGreaterThan
  s3:object-lock-remaining-retention-days` exceeds the default days plus
  one, and when `StringEquals s3:object-lock-mode` is `COMPLIANCE` (with a
  GOVERNANCE default); and deny `s3:PutObject` when `Null
  s3:object-lock-mode` is false (the Lambda never sends lock headers; don't
  use `StringNotEquals`, which also matches an absent key and would deny
  every write). No role here gets `s3:PutObjectLegalHold`.
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
- The writer (library) role: `s3:PutObject` and `s3:AbortMultipartUpload` on
  `in/*` (If-None-Match is enforced by the bucket policy below; don't put an
  if-none-match condition on the writer's own policy, which would deny its
  multipart uploads), `s3:GetObjectTagging` on `in/*` to read the Lambda's
  outcome, and `s3:GetObject` on evidence `_intake/*` to read its records
  (without `s3:ListBucket` there, a record not written yet reads as 403: not
  yet). Sidecars are written with one PUT; the Lambda rejects any other.
- The ops bucket: no lifecycle rule may touch `approvals/` (an approval must
  outlive the job for every file it covers).
- The staging bucket policy, as Denies (within one account an Allow in a
  bucket policy restricts no one): deny `s3:PutObjectTagging` on `in/*` to
  every principal but the ingest role, the sweep role and admins; and for
  the sweep role two more Denies (their conditions are ANDed, so one can't
  hold both): `ForAnyValue:StringNotEquals s3:RequestObjectTagKeys
  ["redrives"]`, and `Null s3:RequestObjectTagKeys` true. Writers may set no
  tags (the sweep never writes an empty tag set).
- Staging lifecycle: expire objects tagged `ingested=true` after 1 day, and
  abort incomplete multipart uploads under `in/` after 1 day.
- If-None-Match, on the staging and evidence buckets: deny `s3:PutObject`
  when `Null s3:if-none-match` is true **and** `Bool
  s3:ObjectCreationOperation` is true (checked live 2026-09-28). The second
  condition matters: UploadPart and UploadPartCopy are also `s3:PutObject`
  and carry no If-None-Match, so a policy without it blocks every multipart
  upload, including every copy over 5 GB.
- S3 notifications on the staging bucket for `ObjectCreated:*`, prefix `in/`,
  suffix `.json`, to the queue. The queue policy allows S3 to send for that
  bucket (`aws:SourceArn`, `aws:SourceAccount`) and denies `sqs:SendMessage`
  to every other principal but the sweep role. A record's `ingest.principal`
  is what the message's S3 event reported; the sweep role could forge one
  (it sends only `{"staging_key": ...}`, which carries none), so the
  principal is as trustworthy as that role.
  The event source: `BatchSize` 1, `ReportBatchItemFailures`; function
  timeout 900 s; queue visibility timeout at least six times that (5400 s)
  and `maxReceiveCount` at least 5, as AWS advises for Lambda consumers, then
  a DLQ with an alarm. Handled failures come back after 60 s regardless.
- The sweep function: an hourly schedule, timeout 900 s (it stops
  `SWEEP_RESERVE_MS` before the end and logs what it didn't read), 512 MB.
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
library's role writes to staging, and once PR 3 pins the part-size function
the Lambda can refuse other layouts.

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
trailing newline); approvals, written by hand, need only be strict JSON. Parsers insist on exactly those bytes, so a document's hash
can always be recomputed from what it says. Every field has a byte cap. For
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
  id. Two writes for one sighting can differ only in `ingest` (code version,
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
