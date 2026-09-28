# PRA evidence intake

Downloaders ("connectors", one per PRA platform) hand each file to an upload
library. The library streams it into a **staging** bucket. A Lambda then copies
it, write-once, into a content-addressed **evidence** bucket and records how it
got there. `schema.py` is the contract all three share. The library, the Lambda
and the infrastructure come in later PRs.

## Flow

1. The library streams the file to staging as `in/<uuid>.bin`: by default one
   PUT up to 16 MiB and multipart above, though the contract accepts any
   S3-legal layout. Every part carries its SHA-256, and nothing touches local
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
     The Lambda hashes MD5 in the same pass, and a wrong MD5 claim is rejected
     (`data_mismatch`). The blob is a single-part object with S3's
     full-object SHA-256 whatever the staging layout was.
   - **Over 5 GB** (S3's single-request limit): the Lambda hashes the object
     itself, then copies it server-side on the staging object's own part
     boundaries, so the blob's composite checksum equals the staging object's.
   - **Over 50 GB** (`COST_GATE`): only with `fetch.approval` naming an
     admin-written `approvals/<uuid>.json` in the ops bucket, which the Lambda
     checks exists. Such a file can outrun a 15-minute Lambda, so it runs as a
     one-off job.
4. It reads the blob back (checksum, size, Object Lock), then writes the
   intake record `_intake/<uuid>.json` (write-once, one per sighting). Last, it
   tags both staging objects `ingested=true`; lifecycle removes tagged objects.
5. The library returns once the record exists and its sha256 equals the hash
   the library computed itself.

Identical bytes from any source (MuckRock, a portal, a local copy) are stored
once. The second copy gets a 412, is verified, and adds only a record.
Records don't say which sighting was first; the earliest record for a blob
version is.

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
can always be recomputed from what it says. Every field has a byte cap, and
the document limits sit above the worst case the caps allow, so a document
that validates always serializes.

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
  sha256 it names is held. The body has no clock or run id, and `held`
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
  the cost gate) applies when a document is written. Reading a stored one
  (`parse_record`, `parse_manifest(..., stored=True)`) skips it, so tightening
  the policy can never make stored evidence unreadable.
- **Permanent means versioned.** Each schema version has its own validator,
  and `READABLE_SCHEMAS` keeps every version ever written; record readers
  accept every deriver up to `DERIVER_VERSION`. Never change what an existing
  version accepts: add a version, and deploy the Lambda before the library.
  `tests/fixtures/pra_intake/v1/` holds frozen schema-1 documents that must
  parse forever and are never regenerated; `vocabulary.json` pins the
  constants and limits. The checksum formulas are pinned to values S3
  returned in live probes.
- **Presented text is kept exactly.** Filenames, titles and agency names may
  hold any character. `canonical_json` escapes non-ASCII; use `display_safe`
  before showing such text to a person or a model. Identifiers and the
  fetcher's own fields are strict: visible and single-line.
- **No credentials stored** (write policy):
  - Secret query parameters (`sig`, `Signature`, any `X-Amz-*`/`X-Goog-*`,
    tokens, session ids; compared ignoring case, `-` and `_`) are refused,
    also percent-encoded or nested. A signed URL's companions (`st`, `se`,
    `sr`, `Expires`, `Policy`, key ids…) go with its secret; alone they are
    ordinary parameters.
  - ASP.NET cookieless and Java path sessions, user info, and well-known
    token shapes (JWTs, AWS key ids) are refused.
  - Response headers outside `ALLOWED_HEADERS` are refused
    (`forbidden_header`).
  - Connectors canonicalize with `strip_signing_params` (it returns a storable
    URL, keeping the rest byte for byte, or raises), `redirect_url` and
    `sanitize_headers` (its output always validates).
- **No values in errors.** A `SchemaError` carries a reason code (one of
  `REJECT_REASONS`, also used as the reject tag) and a field path, never a
  value or a header name.
- **Standard library only**, plain-ASCII source, and no syntax newer than
  Python 3.12, so the module ships unchanged in the Lambda zip.
