# PRA evidence intake

Downloaders ("connectors", one per PRA platform) hand each file to an upload
library. The library streams it into a **staging** bucket. A Lambda then copies
it, write-once, into a content-addressed **evidence** bucket and records how it
got there. `schema.py` is the contract all three share. The library, the Lambda
and the infrastructure come in later PRs.

## Flow

1. The library streams the file to staging as `in/<uuid>.bin`: one PUT up to
   16 MiB, multipart above. Every part carries its SHA-256, and nothing touches
   local disk. Before completing, it checks the byte count against the declared
   length and aborts on a mismatch, so a truncated download never becomes an
   object.
2. It writes the sidecar `in/<uuid>.json` last. The sidecar is the commit
   marker and the only trigger.
3. The Lambda copies the bytes to evidence at `sha256/<hex>`:
   - **Up to 5 GB:** one PutObject carrying the sidecar's claimed SHA-256 as
     `ChecksumSHA256`. S3 verifies every byte, so the claim is never trusted,
     and a wrong claim is rejected (`sha_mismatch`).
   - **Over 5 GB:** the Lambda hashes the object itself, then copies it
     server-side on the staging object's own part boundaries. The blob's
     composite checksum then equals the staging object's.
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
| ops | Lambda code, inventory reports (admin only) |
| derived | processor outputs and the provenance index (later) |

#822's bucket (`sm-alpr-pra-…`) is retired. No role can produce its name. A
record names one environment's evidence and staging buckets.

## Documents

All three serialize with `canonical_json` (sorted keys, ASCII, compact, one
trailing newline). Parsers insist on exactly those bytes, so a document's hash
can always be recomputed from what it says.

- **Sidecar**: the writer's claim. It holds:
  - **data:** size, sha256, md5, the source's multipart-ETag form (at the part
    size that reproduced it), and how the file was uploaded (method, parts);
  - **source:** kind, platform, host, agency, request id and URL, doc id,
    filename, title, stable URL, release date;
  - **fetch:** origin (`live`, `local-copy`, `git`, or `generated` for a fetch
    manifest), ids, connector, timings, and the approval for a file over the
    50 GB cost gate;
  - **response:** status, allow-listed headers, the redirect chain, the final
    URL;
  - **listing:** the entry as the source listed it;
  - **checks:** everything the library verified. A source ETag or Content-MD5
    that disagrees is recorded (`unmatched`, `mismatch`), not refused, because
    the bytes are still evidence.

  Every key is required (`null` when absent), and unknown keys are refused.
- **Intake record**: the sidecar verbatim plus what the Lambda verified: the
  blob's version, checksum and lock, and the staging object's ETag, encryption,
  checksum, and the hash of the sidecar's bytes. It has no clock and no request
  id. Two writes for one sighting can differ only in `ingest` (code version and
  trigger path), which `record_core` leaves out when a 412 is compared.
- **Fetch manifest**: what one run found for one request. Each file gets its
  name, doc id, URL, sha256, size and status (`held`, `failed`,
  `needs_approval`). It is stored as a blob like any file (`origin:
  generated`), and the Lambda checks that every sha256 it names is held. It
  has no clock or run id, and `held` doesn't say whether this run downloaded
  the file or recognized it as unchanged, so an unchanged listing is stored
  once. The run lives in the manifest's record: `fetch.run_id` joins it to
  the file records the same run wrote.

`fetch_id` is one upload call, `run_id` one connector run, and `work_id` one
queue item.

## Rules

- **Permanent means versioned.** Changing a key layout, a field, the
  vocabulary or the serialization needs a new schema version. Readers keep
  every version in `READABLE_SCHEMAS`, and record readers accept every deriver
  up to `DERIVER_VERSION`. `tests/fixtures/pra_intake/` pins the vocabulary and
  the exact bytes of each document kind. The checksum formulas are pinned to
  values S3 returned in live probes.
- **Deploy the Lambda before the library** whenever the schema moves.
- **Presented text is kept exactly.** Filenames, titles and agency names may
  hold any character. `canonical_json` escapes non-ASCII; use `display_safe`
  before showing such text to a person or a model. Identifiers and the
  fetcher's own fields are strict: visible and single-line.
- **No credentials stored.** URLs and fetcher-written fields are refused
  (`signed_url`) if they carry signing or session parameters (also
  percent-encoded or nested), ASP.NET cookieless or Java path sessions, or
  user info. Response headers outside `ALLOWED_HEADERS` are refused
  (`forbidden_header`). Connectors canonicalize with `strip_signing_params`
  and `sanitize_headers`; whatever `sanitize_headers` returns passes
  validation.
- **No values in errors.** A `SchemaError` carries a reason code (one of
  `REJECT_REASONS`, also used as the reject tag) and a field path, never a
  value or a header name.
- **Standard library only**, plain-ASCII source, and no syntax newer than
  Python 3.12, so the module ships unchanged in the Lambda zip.
