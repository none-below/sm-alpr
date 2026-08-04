# Redwood City PD — ALPR network-audit logs (PRA 26-217 + 26-741)

Flock "Network Audit" exports produced by the Redwood City Police Department
under the California Public Records Act (requests **26-217** and **26-741**),
released as public records. Each row is one search that touched Redwood City's
ALPR camera network — run by RCPD or one of its sharing partners.

## Format

One **gzipped NDJSON** file per PRA release (`*.ndjson.gz`): one JSON object per
line, one line per search. Keys are the column headers exactly as the department
produced them (e.g. `"Org Name"`, `"Search Time"`, `"Reason"`). Empty cells are
omitted; the department's `***` redaction markers are preserved verbatim. Read a
file with e.g. `gzip -dc PRA_26_217_2024_Q1.ndjson.gz | jq .`

`_manifest.json` lists every file with its row count, gzipped size, and source
workbook.

## Coverage

- **26-217:** Sept 2023 – March 9 2026 (25 releases; file numbers are *not*
  months — the ranges are contiguous by production batch, not by name).
- **26-741:** May 10 – July 14 2026 (a follow-up for "March 2026 to present").
- **Gap:** March 10 – May 9 2026 is covered by neither production.
- ~10.1M rows total.

## Fields & redaction (varies by release)

Two schema eras. Earlier releases carry `Total Devices Searched`; the "6th
Release"-era files (mid-2025 onward) instead add `ID`, `Text Prompt` (Flock's
free-text / natural-language search) and `Moderation` (Flock's allow/warn/block
label on that prompt).

RCPD's cover letter describes the redactions: license plate and other PII removed
per Gov. Code §§ 7922.000 / 7927.705 and Civil Code § 1798.90.55; the **reason**
field withheld under Gov. Code §§ 7923.600 / 7922.000; and searcher name / plate
/ case # / filter dropped from the newer "network audit" export format. In
practice the `Reason` field is present only in the fall-2023 releases and the
Jan 1–15 2025 file (`PRA_26_217_2025_1`, where it survives under a saved
autofilter); it is empty elsewhere. `Case #` is present in the Jan–July 2025
releases. Searcher `Name` is kept **as produced** — initials for outside
agencies, full names where RCPD disclosed them for its own officers.

## Provenance

Generated from the **pristine, RCPD-created workbooks** (`docProps` author =
RCPD, never re-saved by a third party), not from any downstream copy. The raw
~700 MB of `.xlsx` are kept local (too large for git); this parsed NDJSON is the
committed, machine-readable record.

Regenerate with:

```
uv run --with openpyxl python scripts/xlsx_to_audit_ndjson.py \
    --out assets/redwood-city-pras/json <workbook.xlsx> ...
```
