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
   - **Up to 5 GB:** one GET streamed into one PutObject carrying the
     sidecar's claimed SHA-256 as `ChecksumSHA256`. S3 verifies every byte, so
     the claim is never trusted and a wrong one is rejected (`sha_mismatch`).
     The Lambda hashes MD5 in the same pass (and, when `md5_multipart` is
     set, the MD5 of parts at its part size), and a wrong claim is rejected
     (`data_mismatch`). The blob is a single-part object with S3's
     full-object SHA-256 whatever the staging layout was.
   - **Over 5 GB** (S3's single-request limit): the Lambda hashes the object
     itself, then copies it server-side on the staging object's own part
     boundaries, so the blob's composite checksum equals the staging object's.
     Hashing runs at roughly 60 MB/s in a 15-minute Lambda, so above
     `LAMBDA_MAX_SIZE` (20 GB) the Lambda tags the file `intake=deferred` and
     `python -m pra_intake.ingest process in/<uuid>.json --allow-large` runs
     the same steps where there's no time limit.
   - **Over 50 GB** (`COST_GATE`): only with `fetch.approval` naming an
     admin-written `approvals/<uuid>.json` in the ops bucket. The approval
     names one source (kind, platform, host, request, doc id, URL), a size
     ceiling and an expiry; the Lambda reads it and `check_approval` refuses
     a file from any other source (`too_large`). It covers every upload from
     its source until it expires, so keep expiries short.
4. It reads the blob back (checksum, size, Object Lock), then writes the
   intake record `_intake/<uuid>.json` (write-once, one per sighting). Last, it
   tags both staging objects `ingested=true`; lifecycle removes tagged objects.
5. The library returns once the record exists and its sha256 equals the hash
   the library computed itself.

Identical bytes from any source (MuckRock, a portal, a local copy) are stored
once. While the blob's current version exists, the second copy gets a 412,
is verified, and adds only a record. (If an admin's delete marker is current,
the write succeeds as a new version; read-back verifies it either way.)
Records don't say which sighting was first; the earliest record for a blob
version is.

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
| transient error | none | SQS retries after 60 s, then the DLQ |

A rejection is a fact about the upload: a bad sidecar, bytes that
contradict it, a data object that isn't the one it describes, a blob it
depends on that isn't held. Everything else is retried: throttling, 5xx, a
dropped stream, a stored record that doesn't parse, and an Object Lock S3
doesn't show (a missing `s3:GetObjectRetention`, a bucket without Object
Lock, a lapsed retention). Those are the deployment's problems, and the DLQ
and its alarm are where they surface; `lock_missing` stays in the vocabulary
but the Lambda doesn't tag it.

Every step can be re-run: writes carry If-None-Match, and a run that finds
its record re-tags and stops. A run that tags a file rejected or deferred
then looks for a record once more, so a racing run that recorded it wins.
The Lambda reads every staged byte once and checks every claim about the
bytes: the SHA-256 (S3 checks it on the evidence write up to 5 GB), the MD5,
the source's multipart ETag, the staging part layout, and the sniffed type.
Up to 5 GB that read is the evidence upload itself, and a wrong claim fails
it from inside its body before the last bytes are sent, so nothing is stored
for it. (Checked live 2026-09-28 with botocore's CRT and pure-Python
signers.) A duplicate's bytes are read and checked the same way.

`ingest.sweep` (scheduled) re-drives each untagged sidecar older than an hour,
newest first, at most `MAX_REDRIVES` times (counted in a `redrives` tag), and
logs counts of rejected, deferred, stuck, orphaned (data without a sidecar)
and stray keys, and how many it had no time to read. Logs hold uuids, reason
codes, field paths and sizes, never presented text, URLs or metadata values.

### Deployment (PR 4 pins these)

- The evidence bucket: Object Lock enabled (with versioning) and a default
  retention rule (mode and days). The Lambda sends no retention of its own
  and can't set one; without the rule every file ends in the DLQ, and adding
  it later doesn't lock blobs already written.
- IAM: `ingest.PERMISSIONS` for the ingest role and `ingest.SWEEP_PERMISSIONS`
  for the sweep, per bucket role. The tests run the Lambda with exactly these
  grants. Without `s3:ListBucket` a missing key reads as 403, not 404; without
  `s3:GetObjectRetention` HeadObject hides the lock. On ops, `s3:GetObject`
  is scoped to `approvals/*` and `s3:ListBucket` is on the bucket with no
  `s3:prefix` condition (`check_buckets` sends HeadBucket, which has none).
  SQS: `ReceiveMessage`, `DeleteMessage`, `GetQueueAttributes` (the event
  source) and `ChangeMessageVisibility` for the ingest role; `SendMessage`
  for the sweep's.
- The staging bucket policy: only the ingest role sets tags; the sweep role
  may set only the `redrives` key (`ForAllValues:StringEquals
  s3:RequestObjectTagKeys ["redrives"]` with `Null s3:RequestObjectTagKeys
  false`). Writers may set none.
- S3 notifications on the staging bucket for `ObjectCreated:*`, prefix `in/`,
  suffix `.json`, to the queue (whose policy lets S3 send from that bucket).
  The event source: `BatchSize` 1, `ReportBatchItemFailures`; function
  timeout 900 s; queue visibility timeout at least 900 s; `maxReceiveCount`
  at least 3, then a DLQ with an alarm.
- Environment: `STAGING_BUCKET`, `EVIDENCE_BUCKET`, `OPS_BUCKET`,
  `CODE_SHA256` and `QUEUE_URL` on the ingest function (without `QUEUE_URL`
  a failed message waits the full visibility timeout, not 60 s);
  `STAGING_BUCKET` and `QUEUE_URL` on the sweep.
- `CODE_SHA256`: the zip's SHA-256 in lower-case hex, not Lambda's base64
  `CodeSha256`. `Ingest` refuses anything else, and bucket names that aren't
  one environment's staging, evidence and ops buckets.
- Memory 2048 MB. `LAMBDA_MAX_SIZE` assumes the hashing rate measured at that
  setting, and a 64 MiB manifest needs about 750 MiB.
- boto3/botocore vendored in the zip, pinned exactly, botocore 1.36 or later
  (the `Config` checksum options; older versions fail every invocation).
- A lifecycle rule on the evidence bucket aborting incomplete multipart
  uploads after a day (a timeout mid-copy can't abort its own).

## Buckets

`<prefix>-<role>-<account>-<region>-an`. The prefix is `sm-alpr` (prod) or
`sm-alpr-dev` (dev). Roles:

| Role | Holds |
|---|---|
| staging | `in/<uuid>.bin`, `in/<uuid>.json` (SSE-S3, no Object Lock; tagged objects expire) |
| evidence | `sha256/<hex>`, `_intake/<uuid>.json`, `_errata/<uuid>/<nnnn>.json` (Object Lock) |
| ops | Lambda code, `approvals/<uuid>.json`, inventory reports (admin only) |
| derived | processor outputs and the provenance index (later) |

#822's bucket (`sm-alpr-pra-…`) is retired. No role can produce its name. A
record names one environment's evidence and staging buckets.

## Documents

All three serialize with `canonical_json` (sorted keys, ASCII, compact, one
trailing newline). Parsers insist on exactly those bytes, so a document's hash
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
  trigger path) and in the lock read back; a 412 compares `record_core`.
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
