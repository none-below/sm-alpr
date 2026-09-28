# Layer 1: `truth.duckdb`

This is the data dictionary for the verbatim layer. It says exactly what each stored value is and where it came from:
every table and column, how each source is loaded, the three authored fact files, how the file is built, and the
invariants you can rely on when you quote a row. To go from a row to a citation, see [provenance.md](provenance.md).
For what the values mean in practice (redaction markers, time zones, network versus own-search logs), see
[semantics.md](semantics.md).

Examples assume the standard session in [README.md](README.md). Counts marked *as of build* come from the build
recorded in `truth.build_info` (`built_at_utc` = `2026-09-26T20:38:17Z`, repo inputs at commit `e138455bb`) and change
on rebuild. Totals per producer and log are in [coverage.md](coverage.md), and corpus statistics are in
[stats.md](stats.md). Both are regenerated with every build.

## What truth is

- **One row per released row.** Nothing is merged or de-duplicated. San Mateo PD (SMPD) has one row per search-id
  block printed in its PDFs, so a search printed twice is two rows. When an agency produced the same month twice,
  both copies are stored as separate releases, because re-releases can differ (masking, truncation). Grouping
  happens at read time (`release_content_groups`, see [derived.md](derived.md)).
- **Values as released, as text.** Every released cell or printed line is a `VARCHAR`. Nothing is parsed, trimmed,
  cast or cleaned after staging. What "as released" means for each source is set out in
  [What "verbatim" means for each source](#what-verbatim-means-for-each-source).
- **Authored facts are separate and cited.** Some facts come from documents rather than rows: who produced a
  MuckRock release, cover-letter withholdings, and rows released under the wrong column labels. They come from three
  JSON files, and every entry carries a `source` citation. They never change a released value.
- **Frozen after build.** `derived.duckdb` attaches truth `READ_ONLY`. The build only inserts rows. Nothing is
  updated after insert.

## Tables

| Table | Grain | Key | Rows (as of build) |
|---|---|---|---:|
| `releases` | one released file, workbook sheet or SMPD PDF | `release_id` (PRIMARY KEY, enforced) | 907 |
| `flock_audit_rows` | one non-blank data row of a network or own-search audit | (`release_id`, `row_no`), unique (verified, not declared) | 139,827,534 |
| `flock_event_rows` | one non-blank data row of a Flock event log | (`release_id`, `row_no`), unique (verified) | 11,608 |
| `smpd_pdf_rows` | one search-id block printed in one SMPD PDF | (`release_id`, `row_no`), unique (verified) | 110,705 |
| `release_dispositions` | one authored withheld or redacted claim | (`release_pattern`, `field`) | 2 |
| `release_layouts` | one authored column-shift correction for a row range or a single row | (`release_pattern`, `src_row_from`) | 22 |
| `build_info` | one fact about the build | `key` | 10 |

No foreign keys are declared. Row tables join to `releases` on `release_id`. `release_dispositions` and
`release_layouts` apply to every release whose `release_id` is `LIKE` their `release_pattern`.

## Sources and `release_id` formats

| Prefix | Source | Loaded by | Format | Example |
|---|---|---|---|---|
| `rwc:` | Redwood City PD network audits, PRA 26-217 and 26-741, committed as NDJSON | `build_truth.py` | `rwc:<ndjson stem>` | `rwc:PRA_26_217_2025_1` |
| `la:` | Los Altos PD network and own-search audits, PRA 25-312 and 26-366, committed as NDJSON (one file per workbook sheet) | `build_truth.py` | `la:<pra>:<ndjson stem>` | `la:25-312:Los_Altos_CA_PD_NETWORK_AUDIT_2024__APRIL` |
| `mr:` | MuckRock California corpus: every tabular network audit, own-search audit and event log | `muckrock_ingest.py` | `mr:<request>:<container>[!<member>]#<sheet>` | `mr:214823:26-996_2026-09-01_00_44_52_-0700.zip!REDACTED_5_1_2026-5_31_2026-Santa Rosa CA PD-Network-Audit.xlsx#5_1_2026-5_31_2026-Santa Rosa C` |
| `smpd:` | San Mateo PD own-search log, PRA W012541 and W012818: the committed PDFs SMPD produced, one release per PDF | `build_truth.py` with `smpd_pdf_loader.py` | `smpd:<request folder>:<pdf file name>` | `smpd:W012818-053026:4_1_2026-4_30_2026-San_Mateo_CA_PD-Audit__1_.pdf` |

The parts of an `mr:` id:

- `<request>` is the MuckRock request number (the same number as in `pra_id`).
- `<container>` is the downloaded file's name, which is the last segment of `container_path`.
- `!<member>` is present when the table is a file inside a zip. It is the evidence catalog's member name, and in 13
  releases (as of build) it is truncated. It is always a prefix of the `member` column.
- `#<sheet>` is the workbook sheet name (Excel caps sheet names at 31 characters). CSV files get the literal `#csv`,
  and their `sheet` column is `NULL`.

Treat `release_id` as an opaque key. To locate a file, read `container_path`, `member` and `sheet`.

## `releases`

One row per released unit: a CSV, a workbook sheet (a zip member's sheet counts), a committed NDJSON file (one agency
workbook sheet), or an SMPD PDF.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_id` | VARCHAR | Release key | NOT NULL, PRIMARY KEY. Format by source is in the table above. |
| `producer` | VARCHAR | The Flock organization whose log this is, spelled as in the registry's `flock_names` (e.g. `San Mateo CA PD`) | Not a slug. It is not the searching organization: a network audit holds searches by many orgs (`Org Name`). See [Producer attribution](#producer-attribution). |
| `producer_agency_id` | VARCHAR | UUID of the producer in the repo's `assets/agency_registry.json` | Resolved through `flock_names` and `aliases`. `NULL` if unresolved, and the build then prints a warning (none as of build). |
| `producer_basis` | VARCHAR | How `producer` was decided | Enumeration below. |
| `producer_source` | VARCHAR | The `source` citation of the `producers.json` entry that set `producer` | Set exactly when `producer_basis` = `producers.json`. A per-file entry that sets `org` supplies its own citation. Otherwise the request entry's citation is used. |
| `audit` | VARCHAR | Log type | `network`, `own` or `event` (below). |
| `pra_id` | VARCHAR | Records request | `muckrock-<request>`; `26-217` or `26-741` (RWC); `25-312` or `26-366` (Los Altos); `W012541-041426` or `W012818-053026` (SMPD: the repo folder name, which is the request number plus a date). |
| `container_root` | VARCHAR | Which tree `container_path` is relative to | `repo`: the checkout in `build_info.repo_checkout`. `evidence`: `build_info.evidence_dir`. |
| `container_path` | VARCHAR | The file as stored: the downloaded file or zip (`evidence`), or the committed NDJSON or PDF (`repo`) | Evidence paths start with `<request>-<agency-slug>/`. SMPD paths start with `assets/san-mateo-public-records/<request folder>/`. |
| `member` | VARCHAR | Full path inside the zip | `mr:` only. `NULL` when the container is the file itself. May include folders (`Organization/4_1_2024-…-Audit.csv`). |
| `member_sha256` | VARCHAR | SHA-256 of the zip member's decompressed bytes, i.e. of the file a reader gets by extracting it | Set exactly when `member` is set (535 `mr:` releases as of build). Lets a reader verify an extracted member without the zip. |
| `sheet` | VARCHAR | Full workbook sheet name, verbatim (trailing spaces kept, e.g. `APRIL `) | `mr:` workbooks and `la:`. `NULL` for CSV, RWC and SMPD. |
| `source_file` | VARCHAR | The file a reader would open | `mr:`: base name of the member or the downloaded file. `rwc:`/`la:`: the agency workbook the NDJSON was converted from (from the conversion manifest; the workbook itself is not in the repo). `smpd:`: the PDF file name. |
| `container_sha256` | VARCHAR | SHA-256 of the bytes of `container_path` at build time | `evidence` files match `MANIFEST_v2.txt` in the evidence directory (RFC 3161 and OpenTimestamps stamped). `repo` hashes are of the committed copy: for `rwc:`/`la:` a conversion, not the agency's workbook. |
| `header` | VARCHAR[] | Column names used to load the rows, position by position | Canonical names. `NULL` for the two image-only SMPD PDFs. See [`header` and `header_raw`](#header-and-header_raw). |
| `header_raw` | VARCHAR[] | The released or printed header | See [`header` and `header_raw`](#header-and-header_raw). `NULL` for the header-less RWC export, for SMPD continuation parts and for the image-only PDFs. |
| `header_basis` | VARCHAR | How `header` and `header_raw` were obtained, in words | One text per case, listed in [`header` and `header_raw`](#header-and-header_raw). |
| `n_rows` | BIGINT | Rows loaded for this release | Equals the release's row count in its row table (verified, 0 mismatches). `0` for the image-only SMPD PDFs. |
| `content_sha256` | VARCHAR | Fingerprint of the released content, independent of the container | Set for every release. See [Hashes](#hashes-container_sha256-member_sha256-content_sha256). |
| `released_on` | DATE | When the release was produced | `mr:`: MuckRock attachment date. `la:`: the PRA's closed date from the committed Los Altos README. `NULL` for `rwc:` and `smpd:`. |
| `released_on_basis` | VARCHAR | Source of `released_on`, or why it is missing | `MuckRock attachment date`; `Los Altos README: PRA 25-312 closed 2025-08-13`; `Los Altos README: PRA 26-366 closed 2026-07-09`; `not recorded in manifest (RWC release dates estimated elsewhere from workbook creation)`; `rolling production; the date each PDF was released is not recorded here (see the request's message-history PDF in the same folder)` (`smpd:`). |
| `source_url` | VARCHAR | Public download URL of the container (the zip, if zipped) | `mr:` only: `https://cdn.muckrock.com/foia_files/…`, or `https://cdn.muckrock.com/inbound_request_attachments/<agency>/<request>/…` for files the agency uploaded directly (37 releases as of build). `NULL` for `repo` releases, whose link is built in `release_sources`. |
| `request_url` | VARCHAR | The request's public page | `mr:`: the MuckRock request. `la:`: the Los Altos NextRequest page, from the committed README (`https://losaltosca.nextrequest.com/requests/<pra>`). `NULL` for `rwc:` and `smpd:`. |
| `src_row_basis` | VARCHAR | How a row's locator maps to what a reader sees in the original | Values below. |

### Producer attribution

For `mr:` releases the rules are tried in order, and the first that yields an org wins: a per-file `producers.json`
entry, the request's `producers.json` entry, the file name, the dominant own-search `Org Name`, and finally the
MuckRock agency name.

| `producer_basis` | Applies to | Rule | Releases (as of build) |
|---|---|---|---:|
| `fixed (dedicated loader)` | `rwc:`, `la:` | Hard-coded `Redwood City CA PD` or `Los Altos CA PD` | 102 |
| `fixed (SMPD PRA)` | `smpd:` | Hard-coded `San Mateo CA PD` | 34 |
| `producers.json` | `mr:` | Authored per request or per file, with a citation in `producer_source` (see [`producers.json`](#producersjson)) | 139 |
| `filename` | `mr:` | `<digits>[-_]<Org Name>[-_ ][Network-]Audit` in the file or member name; the org must contain `CA`, `PD`, `SO`, `FD`, `Sheriff` or `Police` as a word | 442 |
| `own-search dominant Org Name` | `mr:` | The most frequent non-blank `Org Name` in the first ~2,000 data rows of each own-search file in the same request | 190 |
| `MuckRock agency name` | `mr:` | Last resort: the agency name on the MuckRock request | 0 |

`producers.json` can also override `audit`. It sets `network` for three requests (196330, 196355, 196367) whose
titles ask for network-audit records (the file profiler had classed 7 of their 8 files as own-search), and for the
Denver file in 188086.

### `audit`

| `audit` | Meaning | Row table |
|---|---|---|
| `network` | Network audit: every search that touched the producer's cameras, by any organization, including the producer's own searches | `flock_audit_rows` |
| `own` | Own-search ("organization") audit: searches by the producer's users | `flock_audit_rows`; `smpd_pdf_rows` for `smpd:` |
| `event` | Event log: administrative events (users created or deleted, network-sharing changes, hotlist edits) | `flock_event_rows` |

For `mr:` releases, `audit` comes from the evidence catalog's file profile (`network_audit`, `search_audit_own`,
`event_log`) unless `producers.json` overrides it.

### `header` and `header_raw`

`header` always holds canonical names, one per loaded column. For `mr:`, and for `rwc:` releases that have a header,
it has the same length as `header_raw`, and position *i* in each is column *i* of the file. For workbooks, that means
column *i* of the sheet's used range. For `la:` and `smpd:` the lengths can differ (see below). `canonical` (in
`muckrock_ingest.py`) applies these rules in order:

1. **Trim** surrounding whitespace. Many Flock exports put a space in front of labels (`' Org Name'` appears in 637
   `mr:` releases as of build).
2. **Aliases** (case-insensitive): `Reason_1` → `Reason`, `Test Prompt` → `Text Prompt`, `License Plates` →
   `License Plate`, `Case Number` → `Case #`, `Search Date` → `Search Time`. As of build, the first three occur, in 14,
   2 and 2 releases. `Case Number` and a lone `Search Date` do not occur.
3. **San Jose split.** If the header also has a `Search Time` column, `Search Date` keeps its own name instead of the
   alias. It then falls outside the 14 Flock columns and is stored in `extra` (10 San Jose releases, where the date
   is in `Search Date` and the time of day in `Search Time`).
4. **Blank labels** become `column<NN>`, where NN is the 0-based position as two digits (`column07`, `column09`,
   `column10`, `column13`, `column14` as of build).
5. **Duplicates** after mapping get `_2`, `_3`, … appended. None occur as of build.

Canonical names that match a Flock column (or, for event logs, an event column) load into that column. Every other
name goes to `extra`.

Per source (`header_basis` states the same in words):

| Source | `header_raw` | `header` | `header_basis` begins |
|---|---|---|---|
| `mr:` | The released header row exactly as released: the first row with at least three cells containing a letter, including leading spaces and blank labels | `canonical(header_raw)` | `header_raw = the released header row` |
| `rwc:` | The NDJSON keys in the workbook's column order. Each line lists its keys in column order but omits empty cells, so the builder merges the key sequences (topological order, ties broken by first appearance). The result is deterministic. A column left blank in every row has no key and is absent, so it reads as not exported rather than empty. | `canonical(header_raw)` (equal as of build) | `the converter manifest records no header row` |
| `rwc:` header-less export (`rwc:PRA_26_217_4th_Release_Dec2023`) | `NULL`: the export has no header row | The keys the converter gave the columns, in the standard network-audit order (`SCHEMA_A` in `scripts/xlsx_to_audit_ndjson.py`) | `no header row: the export is header-less` |
| `la:` | The released header row as the conversion manifest records it: `''` for blank labels, phantom labels kept (a *phantom* is a label with no data column under it) | `canonical` of the keys the converter actually wrote: phantom labels dropped, blank or missing labels as `column_<N>` (N = 1-based data column), unlabeled data columns past the header appended. Position *i* is the *i*-th data column. | `header_raw = the released header row, from the converter manifest`, with the sheet's phantom labels and unlabeled columns in parentheses |
| `smpd:` | The header lines printed on page 1 before the first search id, one entry per text-layer line, verbatim. Two labels can share an entry (`userID networkCount`), leading spaces are kept (`' Search Time'`), and labels can be truncated as printed (`NetworkCo Search Time`). `NULL` for a continuation part, which prints no header. | The five columns in Flock superset names: `[ID, Name, Total Networks Searched, Search Time, Reason]` for the printed `ID`, `userID`, `networkCount`, `Search Time`, `Reason` | `header_raw = the header row as printed on page 1`; `no header row printed (a continuation part)`; `image-only PDF: no text layer; OCR rows not loaded yet` |

Examples as of build: the September 2025 own-search sheet in 26-366 has a phantom `Total Devices Searched` (dropped
from `header`) and two trailing blank labels (`column_11`, `column_12` in `header`, with no data). In the November
2025 own-search sheet, the first and fifth labels are blank, so `header` starts `[column_1, Org Name, …, column_5, …]`
(see [Known gaps](#known-gaps-in-this-build)).

### `src_row_basis` values

| Value (as stored) | Sources | How to locate a row |
|---|---|---|
| `spreadsheet row number in the named sheet (1-based, as shown by Excel)` | `mr:` workbooks (291 as of build) | Open the sheet and go to row `src_row`. |
| `CSV record number, header = row 1 (the row shown when the file is opened in a spreadsheet); decoded utf-8` | `mr:` CSV (480) | Open the file in a spreadsheet application and go to row `src_row`. Quoted cells contain line breaks (`Time Frame` always does), so the record number is **not** the line number a text editor shows. The text ends `decoded cp1252` when UTF-8 decoding failed (no release as of build). |
| `workbook row = NDJSON line + 1 (header on row 1); the committed conversion (scripts/xlsx_to_audit_ndjson.py) drops all-empty rows, so exact unless the workbook has blank rows mid-sheet; it trims cell text and omits empty cells` | `rwc:`, `la:` (101) | `src_row` is the workbook row, but the workbook is not in the repo. The committed NDJSON line is `src_row` − 1. |
| `workbook row = NDJSON line (no header row: the export is header-less); …` (rest as above) | `rwc:PRA_26_217_4th_Release_Dec2023` (1) | `src_row` = NDJSON line = workbook row. |
| `PDF page src_page, line src_line of that page; the search id is printed there` | `smpd:` (34) | Use `smpd_pdf_rows.src_page` and search that page for `id`. SMPD rows have no `src_row`. |

### Hashes: `container_sha256`, `member_sha256`, `content_sha256`

- `container_sha256` proves which file you have. For `evidence` releases it is the SHA-256 of the MuckRock download
  (the zip, if zipped), and it matches the timestamped `MANIFEST_v2.txt`. Several releases share one container when
  a zip holds several files or a workbook holds several sheets (771 `mr:` releases come from 184 container files as
  of build). Two of those files have identical bytes: Arcadia's `event-logs.csv` and `event-logs_OFfjQQv.csv`
  (request 209838).
- `member_sha256` proves which zip member you have, after extracting it. It is computed during staging. A
  `staged.json` written before staging hashed members is completed at load, and the build prints a `NOTE`.
- `content_sha256` identifies the same content across containers and productions:

  | Source | `content_sha256` is the SHA-256 of |
  |---|---|
  | `mr:` CSV | The CSV file's bytes, header included. It equals `container_sha256` when unzipped and `member_sha256` when zipped (verified for all 480). |
  | `mr:` workbook sheet | The non-blank data rows as staged: each row's cell strings joined with U+001F and ended with `\n`. The header is excluded. |
  | `rwc:`, `la:` | The decompressed NDJSON, so it is independent of gzip settings. Two conversions group only if they are byte-identical (same keys, values and order). |
  | `smpd:` | The PDF, so it equals `container_sha256`. |

  A CSV and a workbook holding the same rows never share a value, and neither do two productions that differ in one
  cell. As of build, 101 values are shared by 219 releases: 88 groups of `mr:` releases (193), each spanning more than
  one container, and 13 Los Altos pairs of 25-312 and 26-366. The Los Altos pairs are January–July 2025 network and
  January–April, June and July 2025 own-search. May 2025 own-search does not pair (the Los Altos README reports four
  `Reason` cells that differ), and neither does August 2025, which 25-312 produced mid-month.

## `flock_audit_rows`

One row per non-blank data row of every `rwc:`, `la:` and `mr:` network and own-search release, in file order. The 14
Flock columns are the superset of labels seen in Flock audit exports. A release fills only the columns its `header`
has, and every other column is `NULL` for all of its rows. To tell "empty cell" from "column not released", check
`list_contains(releases.header, '<column>')` (derived does this in `release_meta`).

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_id` | VARCHAR | Release the row belongs to | Joins `releases`. |
| `row_no` | BIGINT | 1-based position among the release's stored rows, in file order | Dense 1…`n_rows`. Unique with `release_id`. |
| `src_row` | BIGINT | Row number of this row in the original as a reader sees it | Interpret with `releases.src_row_basis`. Never `NULL` as of build. Gaps mark blank rows that were not stored. |
| `ID` | VARCHAR | Flock's search UUID | Only in newer exports (header of 400 of 857 releases as of build). The same search carries the same UUID in every log that recorded it, including SMPD's `smpd_pdf_rows.id`. This is the basis of tier-1 linking ([linking.md](linking.md)). Text form `8-4-4-4-12`, lower-case hex. |
| `Name` | VARCHAR | The Flock user who ran the search, as exported | Full name, initial plus surname, or a mask (`***` from Flock, or agency marks such as `###` and `REDACTED`). Police employees; see [pii.md](pii.md). |
| `Org Name` | VARCHAR | The Flock organization of the user who ran the search | Absent from 68 own-search releases' headers (as of build); derived then falls back to `producer`. |
| `Total Networks Searched` | VARCHAR | Number of organizations' camera networks the search queried | Integer text. Two RWC releases hold floats such as `123.0` (the converter wrote JSON floats). |
| `Total Devices Searched` | VARCHAR | Number of cameras (devices) queried | Older exports only (header of 240 releases as of build). |
| `Time Frame` | VARCHAR | The period searched | Start and end timestamps in Flock text form, separated by a line feed. Lodi's release (`mr:173098:…`) separates them with ` to ` instead. |
| `License Plate` | VARCHAR | Plate (full or partial) searched for, if any | **Civilian data.** Some releases have it unmasked, others masked (`***`, `###`, `[REDACTED]`, `* * *`). One producer's own-search releases type an exemption citation, `7923.600 GC`, into this cell. |
| `Reason` | VARCHAR | Free-text reason the searcher entered | Can hold case numbers, offense codes, names or plates. Redwood City withheld it (see `release_dispositions`). |
| `Case #` | VARCHAR | Case or incident number the searcher entered | Optional in Flock; often blank. |
| `Filters` | VARCHAR | Search filters applied (vehicle attributes, lists, plate terms) | Free text; can contain plate fragments, sometimes glued onto a vehicle attribute ([pii.md](pii.md)). |
| `Search Time` | VARCHAR | When the search was run | Flock text form `MM/DD/YYYY, HH:MM:SS AM UTC` (month and day not zero-padded in some exports). The 10 San Jose split releases hold only `HH:MM:SS` here, with the date in `extra`. Search times are UTC ([semantics.md](semantics.md)). |
| `Search Type` | VARCHAR | Flock's search-type label | For example `lookup`, `search`, `lookup - Mobile`, `convoy`, `freeform`, `multiGeo`. The full census is in [stats.md](stats.md#search-types). |
| `Text Prompt` | VARCHAR | The prompt of a natural-language (free-form) search | Newer exports only; mostly `NULL`. |
| `Moderation` | VARCHAR | Flock's moderation verdict on that prompt | Newer exports only. Values as released: `allow`, `block`, `warn`, and the agency mask `###` (4 releases). It is empty on non-freeform rows. Rows covered by a `release_layouts` entry can hold other values here (in Santa Rosa May 2026, search types). |
| `extra` | JSON | Released columns outside the 14 | Object of canonical name → cell text. See below. |

Four release patterns hold cells under the wrong label for some rows. They are recorded in `release_layouts` and
corrected in `flock_rows`, never here. Truth also keeps every non-blank row after the header, even when it is not a
search. For example, `rwc:PRA_26_217_4th_Release_Dec2023` `row_no` 16485 repeats the header labels in its cells.

### `extra`

- **`mr:`**: one key per non-Flock column in `header`. `extra` is `NULL` when all of those cells are empty in that
  row, so blank padding columns cost nothing.
- **`rwc:`, `la:`**: one key per NDJSON key outside the 14. `extra` is `NULL` when all of those cells are empty in
  that row.

Keys that occur, as of build (scans the `extra` column of every row; seconds):

```python
con.sql("""
SELECT k AS extra_key, count(DISTINCT release_id) AS releases, count(*) AS rows_
FROM (SELECT release_id, unnest(json_keys(extra)) AS k
      FROM truth.flock_audit_rows WHERE extra IS NOT NULL)
GROUP BY 1 ORDER BY 3 DESC""").show()
```

| Key | Releases | Rows | What it is |
|---|---:|---:|---|
| `Search Date` | 10 | 6,614,929 | San Jose split timestamp: the date (`YYYY-MM-DD`). `Search Time` holds the time of day. |
| `column_5` | 2 | 1,317 | Los Altos 26-366 own-search, October and November 2025: an unlabeled fifth column (where `License Plate` sits in other months). Every cell is `REDACTED`. |
| `column_1` | 1 | 747 | Los Altos 26-366 own-search, November 2025: an unlabeled first column (where `Name` sits in other months). Every cell is `REDACTED`. |
| `column09`, `column10` | 6 | 6 | Mountain View and Pasadena: one row per release overflows into two unlabeled trailing columns |
| `column13`, `column14` | 2 | 4 | Santa Rosa February 2026 network audit: unlabeled trailing columns. Rows 391827 and 391828 hold `allow` here (left as released; see the `layouts.json` citation). |

### What "verbatim" means for each source

| Source | A cell is… | Empty cells |
|---|---|---|
| `mr:` CSV | The decoded text of the cell, unchanged. Whitespace-only cells stay as they are. | `NULL` |
| `mr:` workbook | The stored cell value as read by `python-calamine` and rendered by Python `str()`. Whole-number floats are written as integers (`4656`, not `4656.0`). Date and time cells come out as `YYYY-MM-DD HH:MM:SS[.ffffff]`, not in Excel's display format. Flock's own timestamps are text cells, so this only affects cells an agency converted, such as San Jose's split date and time and the Riverside event log's `Timestamp`. | `NULL` |
| `rwc:`, `la:` | The value the committed conversion (`scripts/xlsx_to_audit_ndjson.py`, openpyxl) wrote. Strings are trimmed of surrounding whitespace, numbers are JSON numbers (so a float cell reads `123.0`), and dates go through `str()`. | Omitted from the NDJSON, so `NULL` |
| `smpd:` | A text line of the PDF's text layer exactly as pymupdf `page.get_text()` returns it. Only non-empty lines count, and trailing spaces are kept (13,452 `reason_line` values end in whitespace as of build). Other extractors split and space lines differently: poppler's `pdftotext -raw` drops spaces pymupdf keeps, so compare modulo whitespace. | No line is printed, so `NULL` |

`mr:` staging also decides where the table starts. The header is the first row with at least three cells containing
a letter. Rows above it are skipped (as of build, every `mr:` release has its header on row 1). Rows with no
non-blank cell are not stored, but `src_row` keeps the original numbering. As of build this leaves gaps in two audit
releases (`mr:196409:…CPRA_25-948.xlsx#Sheet1`, `mr:196397:PRA25-746.csv#csv`) and in the Riverside event log.

## `flock_event_rows`

Flock event logs: one row per non-blank data row, loaded like `mr:` audits (all 16 releases as of build are `mr:`).
Derived exposes them parsed and cited as `event_log`.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_id` | VARCHAR | Release the row belongs to | `releases.audit` = `event` |
| `row_no` | BIGINT | 1-based position in file order | Unique with `release_id` |
| `src_row` | BIGINT | Row as a reader sees it | Per `src_row_basis` |
| `Timestamp` | VARCHAR | When the event happened | ISO 8601 ending `Z` in 15 releases, except 49 of San Joaquin's 749 rows, which end `+00:00`. Riverside's `CPRA_C001586_Hotlist.xlsx` has `YYYY-MM-DD HH:MM:SS.ffffff` (a workbook datetime, no zone). |
| `User` | VARCHAR | Account that performed the action | Names as exported, plus 10 rows with an e-mail address (as of build) |
| `Event Type` | VARCHAR | Action | `create`, `update`, `delete` as of build |
| `Entity Type` | VARCHAR | What was acted on | As of build: `Custom Hotlist Entry`, `customHotlist`, `networkShare`, `networkShareSettings`, `role`, `user`, `stream`, `location`, `organization`, `integration`, `export`, `accountContact` |
| `Entity Details` | VARCHAR | Description of the entity | Free text. Treat as civilian data: as of build, 1,191 `Custom Hotlist Entry` rows hold a value shaped like a California plate (`9AAA999`). |
| `Event Id` | VARCHAR | Flock's event UUID | `NULL` for the Riverside release, which has no such column |
| `extra` | JSON | Columns outside the six | `NULL` in every row as of build |

## `smpd_pdf_rows`

San Mateo PD's own-search log, read from the PDFs it produced for PRA W012541 and W012818. These are Flock
search-audit exports printed to PDF. They are committed under `assets/san-mateo-public-records/W012541-*/` and
`W012818-*/`: every `.pdf` whose name contains `audit` (case-insensitive), which excludes the message-history PDFs.
There is one `releases` row per PDF (34 as of build), and one table row per search-id block in the PDF's text layer.
No block is ever dropped.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_id` | VARCHAR | `smpd:<request folder>:<pdf file name>` | Joins `releases`, where `container_path` is the PDF |
| `row_no` | BIGINT | 1-based order of the id lines in the text layer | Dense 1…`n_rows`. It follows (`src_page`, `src_line`). PDF order is not time order. |
| `src_page` | INTEGER | 1-based PDF page the id line is printed on | Never `NULL` |
| `src_line` | INTEGER | 1-based position of the id line among that page's non-empty text-layer lines | A line index into `page.get_text()`, not a visual line count. On some pages the text order differs from the printed order (see `parse_note`). To find the row in the PDF, search `src_page` for `id`. |
| `id` | VARCHAR | Flock search UUID, as printed (the `ID` column) | Lower-case `8-4-4-4-12`, with no surrounding whitespace as of build. It is the same UUID as `ID` in other agencies' logs. It is not unique: as of build, 179 blocks repeat an id within the same PDF (3 PDFs), each identical to the first in every line, and no id appears in two PDFs. |
| `user_line` | VARCHAR | The `userID` cell as printed | `***` (Flock's own redaction) or a name (police employees; [pii.md](pii.md)). Never `NULL` as of build. |
| `count_time_line` | VARCHAR | `networkCount` and `Search Time`, printed as one text line | `<networks> MM/DD/YYYY, HH:MM:SS AM UTC`, e.g. `3 04/27/2026, 02:42:09 AM UTC`. Every row has this shape as of build. The time is UTC. Derived splits the line into `nets` and `t` (`SMPD_COUNT_TIME_RE` in `sql_templates.py`). |
| `reason_line` | VARCHAR | The `Reason` cell as printed | `NULL` when the cell is blank, because then no line is printed. Several lines are joined with `\n` only in a text-order fallback block (none as of build). Free text: can hold case numbers, names or plates. |
| `parse_note` | VARCHAR | `NULL` when the block's text order matched its printed row; otherwise how its cells were read | Forms below. |

A blank `Reason` cell prints no line (`pdftotext -layout` agrees: nothing is printed after the timestamp), so its
`reason_line` is `NULL`. The PDFs are split by Pacific-time month and `Search Time` is UTC, so each month's last
evening falls in the next UTC month (the May 2025 PDF has 23 rows dated `06/01/2025`). January 2025 and January 2026
are split into parts, and a continuation part prints no header.

A search printed in two PDFs would be two rows, and so two sightings of one event. Count searches as distinct `id`
(or `event_id` in derived), not rows. Derived reads this table in `sightings_smpd` and `sighting_sources_smpd`
([derived.md](derived.md)).

### How a block is read (`parse_note`)

`smpd_pdf_loader.parse_pdf` reads each PDF with pymupdf, which gives every text line with its bounding box. A block
is an id line plus the lines that follow it up to the next id: normally `userID`, the count and time line, and one
`Reason` line if the cell is not blank. Each non-id line is also assigned to the id whose printed row contains the
line's vertical centre. The printed row is the vertical band of the id line's box. That gives two readings of every
block, and they agree on untouched pages.

They disagree on pages whose text layer lists cells out of printed order: `Reason` lines pile up after the page's
last row. A text-order reader then gives one row no reason and another several, or swallows the next row. The printed
position is still right, so the positional reading wins whenever it has the expected shape. The `parse_note` forms
are:

| Form | Meaning | Rows (as of build) |
|---|---|---:|
| `text order differs from the printed row: cells read by position on page P (userID line A, count/time line B, reason line C)`, optionally ending `; Reason cell blank` | Read by position. A, B and C are `src_line`-style indices on page P. | varies by build |
| `read in text order only (position not used: page P its text and layout disagree)` or `(… it has text outside every printed row)`, optionally followed by `; unexpected block: K line(s) between this id and the next` | No trustworthy positions on the page, so the cells were read in text order | 0 |
| `printed row holds K line(s) besides the id; read in text order`, optionally followed by `(K line(s) between this id and the next)` | The printed row is not the expected shape, so the cells were read in text order | 0 |
| any of the above plus `; block continues on page Q in text order` | The block runs across a page break | 0 |

Derived trusts `user_line` only when `parse_note` is `NULL` or starts `text order differs from the printed row`,
because a text-order fallback block may hold a neighbour's line.

### The superseded merged JSON

Until this build, SMPD was loaded from the repo's merged JSON
(`assets/transparency.flocksafety.com/san-mateo-ca-pd/pra-W012541-041426.json` and `pra-W012818-053026.json`, built
by `scripts/parse_pra_audit.py` and `scripts/import_pra_audit.py`). The build no longer reads it, because:

- **It is stale.** The W012541 file was built from 28 of that request's 32 PDFs (its `integrity.source_pdfs`). It
  lacks June 2023 (1,864 rows), May 2025 (5,856 rows) and the image-only June 2024 pair.
- **It is wrong on Acrobat-edited pages.** Compared by `id` with this table: 35 of its reasons differ from the
  printed row (33 are the next row's search UUID and 2 are a neighbour's reason), and 33 printed rows are missing.
  All of them are on pages where `parse_note` is set.
- **It cannot be cited.** It reformats the time as `YYYY-MM-DDTHH:MM:SSZ` and merges, de-duplicates and re-sorts
  the rows, so a row no longer points to a page.

## Authored facts

Three JSON files beside the build scripts carry facts taken from documents. Each entry must cite its source: the
request, cover letter or README, and for corrections, how the claim was checked against other logs. They are loaded
as written and change no released value.

### `producers.json`

This file is not a table. It feeds the `releases` columns `producer`, `producer_basis` = `producers.json`,
`producer_source` and `audit`.

```json
"196330": {"org": "West Sacramento CA PD", "audit": "network",
           "source": "MuckRock 196330, 'Three Flock Safety Network Audit Records (West Sacramento Police Department)': ..."},
"188086": {"org": "Pasadena CA PD", "source": "MuckRock 188086, Pasadena Police Department ...",
           "files": {"SAMPLES/Denver_ALPR_Network_Searches_1.xlsx":
                     {"org": "Denver Police Department", "audit": "network", "source": "..."}}}
```

| Field | Required | Meaning |
|---|---|---|
| key | yes | MuckRock request number, as a string. Keys starting with `_` are comments and are ignored (`_doc`). |
| `org` | yes | The Flock organization whose export the request produced, spelled as in the registry's `flock_names`. Otherwise `producer_agency_id` is `NULL` and the build warns. |
| `audit` | no | Overrides the file profile's log type for every file in the request: `network`, `own` or `event`. |
| `source` | yes | Citation: MuckRock request number, agency, and the wording that settles it. Loaded into `releases.producer_source`. |
| `files` | no | Per-file overrides for a file that is not the answering agency's own log: `{<name>: {org, audit, source}}`. `<name>` is matched against the full zip-member path, the member or file name, or the container file name. A file entry wins over the request entry field by field. **A file entry that sets `org` must carry its own `source`**, or the build exits. |

As of build: 25 requests, one file entry, 25 producers and 139 releases. The file entry is MuckRock 188086's
`SAMPLES/Denver_ALPR_Network_Searches_1.xlsx`. It has 1,000,000 rows, none by `Pasadena CA PD`, and is attributed to
`Denver Police Department` (network). The attribution rests on the file name. The file has no `ID` column, so it
cannot be checked by UUID. Its `producer_source` gives the evidence.

### `release_dispositions` (from `dispositions.json`)

Withheld or redacted fields that an agency declared, usually in a cover letter. Derived uses them to class a blank
cell as withheld rather than empty (`is_withheld`, `cell_state`; see [derived.md](derived.md)).

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_pattern` | VARCHAR | SQL `LIKE` pattern matched against `release_id` | `_` matches any one character and `%` any run |
| `field` | VARCHAR | Canonical Flock column name (e.g. `Reason`) | |
| `disposition` | VARCHAR | `withheld_blanked`: the agency blanked the field and said so, so blank cells mean withheld. `redacted`: the agency declared masking; informational only, derived reads only `withheld_blanked`. | |
| `source` | VARCHAR | Citation | |

As of build: `rwc:PRA_26_%` has `Reason` `withheld_blanked` and `License Plate` `redacted`, both citing RCPD's
cover letter (PRA 26-217 and 26-741). Both patterns match all 28 RWC releases.

### `release_layouts` (from `layouts.json`)

Rows whose cells sit under the wrong header label as released. Truth rows stay as released. Derived's `flock_rows`
reads each listed field from the label named here for the covered rows and sets `layout_corrected`.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_pattern` | VARCHAR | SQL `LIKE` pattern matched against `release_id` | The build warns if it matches no release |
| `src_row_from` | BIGINT | First affected row, as a `src_row` (the row a reader sees), not a `row_no` | Inclusive |
| `src_row_to` | BIGINT | Last affected row | Inclusive; `NULL` = to the end |
| `mapping` | MAP(VARCHAR, VARCHAR) | Canonical field → the header label whose cell holds it in these rows | A `NULL` value means the field has no cell in these rows, so derived reads it as not exported. A value can also be an `extra` key of an unlabeled cell (e.g. `column13`). Fields not listed read from their own label. |
| `source` | VARCHAR | Citation and verification | Records how the shift was confirmed against other agencies' logs, usually by Flock search UUID |

A `layouts.json` entry gives either a range (`src_row_from`, `src_row_to`) or a list of single rows (`src_rows`).
Each listed row becomes one table row with `src_row_from` = `src_row_to`. The build warns if a listed row is missing
from a matched release.

As of build (4 entries, 22 table rows):

| Pattern | Rows | What is shifted |
|---|---|---|
| `mr:214823:%REDACTED_5_1_2026-5_31_2026-Santa Rosa CA PD-Network-Audit.xlsx%` | from `src_row` 92527, both productions (455,584 rows each) | Two exports stacked without a second header row: from `Total Networks Searched` on, values sit under other labels (the time under `Search Type`, the search type under `Moderation`) |
| `mr:196397:PRA25-746.csv%` | from `src_row` 2 (Cathedral City, 3 rows) | No `Case #` cell, so every value from `Filters` on sits one label to the left |
| `mr:205259:%/4_1_2024-4_30_2024-San Bruno CA PD-Audit.csv%` | from `src_row` 2 (San Bruno own-search, April 2024, 1,840 rows) | From `Filters` on, every value sits one label to the right: the time is under `Search Type`, the search type under `Text Prompt`, and no cell holds `Moderation` |
| `mr:214823:%REDACTED_2_1_2026-2_28_2026-Santa Rosa CA PD-Network-Audit.xlsx%` | 19 single rows (`src_rows`), both productions | Freeform rows with the `Moderation` verdict under `Search Time`. No cell holds the time, so `Search Time` maps to `NULL` and these rows have no `t` by design |

The released cells under the shifted labels (Cathedral City):

```python
con.sql("""
SELECT row_no, src_row, "Filters", "Search Time", "Search Type"
FROM truth.flock_audit_rows
WHERE release_id = 'mr:196397:PRA25-746.csv#csv' ORDER BY row_no""").show()
```

```
│ row_no │ src_row │           Filters           │ Search Time │ Search Type │
│      1 │       2 │ 01/23/2025, 03:26:37 PM UTC │ lookup      │ NULL        │
│      2 │       4 │ 01/28/2025, 01:29:20 PM UTC │ lookup      │ NULL        │
│      3 │       7 │ 01/31/2025, 02:26:06 PM UTC │ lookup      │ NULL        │
```

### Adding an entry

1. Edit the JSON file in `<code>` (in a worktree, through a PR). Give the citation, and for a layout or disposition, the check that
   supports it (for example, the other log's value for the same search UUID). Use `%` in `release_pattern` so that
   every production of the same file matches.
2. Rebuild truth with `build_truth.py` (below). Staging is not needed unless the evidence or `muckrock_ingest.py`
   changed, but the staged units must still be in `<T>/muckrock_units/`. Check the build output for `WARNING` lines
   (unmatched layout pattern, missing `src_rows`, unresolved producer).
3. Rebuild derived (`rm derived.duckdb` first). The cache fingerprint covers `producer`, `audit`, `header`, the
   release hashes, `release_layouts` and `release_dispositions` (all but their `source` text), and the parsed SMPD
   lines, so `check_cache.py` reports a cache built before the edit as stale ([derived.md](derived.md)).
4. Run `gen_coverage.py`, `gen_stats.py` and `check_docs.py`.

## `build_info`

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `key` | VARCHAR | Fact name | |
| `value` | VARCHAR | Fact value, as text | |

| Key | Meaning |
|---|---|
| `built_at_utc` | When truth was built (`YYYY-MM-DDTHH:MM:SSZ`). Quote counts "as of" this. |
| `repo_checkout` | Absolute path of the checkout the repo inputs were read from (a local path) |
| `repo_commit` | That checkout's `HEAD`. Repo permalinks use this commit. It also identifies the build code: `build_truth.py` refuses to run from a different checkout than `--repo`. The 2026-09-26 baseline build (repo commit `e138455bb`) predates the code's move into git: the code it ran is commit `92befd9a0`, byte-identical to the local copy. |
| `repo_inputs_dirty` | `True` if `git status` showed uncommitted changes under `assets/redwood-city-pras`, `assets/los-altos-pras`, the SMPD PDF folders (`assets/san-mateo-public-records/W012541-*`, `W012818-*`), `assets/agency_registry.json` or the build code (`scripts/audit_db`). Builds from before the code moved into git (up to 2026-09-26) did not check the code. |
| `commits_behind_local_origin_main` | Commits between `HEAD` and the checkout's *local* `origin/main` ref, which is only as fresh as the last `git fetch`. `None` if it could not be computed. |
| `evidence_dir` | Absolute path of the MuckRock evidence directory |
| `repo_web_url` | GitHub base URL derived from the `origin` remote, for `<repo_web_url>/blob/<repo_commit>/<container_path>` |
| `duckdb_version` | DuckDB Python package version that built truth |
| `python_version` | Python version that ran the build |
| `pymupdf_version` | pymupdf version that read the SMPD PDFs. `smpd_pdf_rows` is pymupdf's text layer, so a different version can change it. |

```python
con.sql("SELECT key, value FROM truth.build_info").show()
```

## How truth is built

Inputs:

- **Repo checkout** (`--repo`; the default is the git checkout containing the current directory):
  - `assets/redwood-city-pras/json/*.ndjson.gz` and `_manifest.json`
  - `assets/los-altos-pras/json/pra-*/*.ndjson.gz`, each `_manifest.json`, and `assets/los-altos-pras/json/README.md`
    (production dates and request URLs)
  - the SMPD audit PDFs under `assets/san-mateo-public-records/W012541-*/` and `W012818-*/`
  - `assets/agency_registry.json`
- **MuckRock evidence**: `<primary checkout>/.claude/local_evidence/muckrock-ca-audit-logs`, found through git's
  common directory, so a worktree build still reads the primary checkout's copy. It holds the downloaded originals,
  `catalog.json` + `catalog2.json` (one entry per downloaded file, with a per-file/member/sheet profile),
  `catalog_requests.json` (request pages), and `MANIFEST_v2.txt` with its `.tsr`/`.ots` stamps.
- **Authored facts**: `producers.json`, `dispositions.json`, `layouts.json` in `<code>`, beside the build scripts.
- **Tools**: `uv`, with the pinned environment in `<code>` (`pyproject.toml` + `uv.lock`: Python, DuckDB, `pymupdf`
  for the truth build, `python-calamine`, `openpyxl` and `pyxlsb` for staging). The versions are exact because each
  one changes what a build produces.

Two steps. Run them from a fresh worktree off a freshly fetched `origin/main`, niced. `C` is `<code>` in that
worktree, `A` is `<audit_db>`, and `T` is a scratch directory with about 25 GB free (the build README's figure):

```sh
setopt interactivecomments 2>/dev/null || true   # zsh: lets the trailing # comments paste into an interactive shell
bgpy() { nice -n 19 taskpolicy -b uv run --locked --project "$C" python "$@"; }   # a function, so bash and zsh both run it (interactive zsh: after the setopt above)
bgpy "$C/muckrock_ingest.py" stage <evidence dir> "$T"                  # README estimate: ~6 min
bgpy "$C/build_truth.py" "$A/truth_new.duckdb" "$T" \
  && mv "$A/truth_new.duckdb" "$A/truth.duckdb"                         # README estimate: ~10 min (SMPD PDFs: 2-3 min)
```

1. **Staging** (`muckrock_ingest.py stage`, two worker processes). It lists units (file, zip member, sheet) from the
   catalogs, keeping those profiled as `network_audit`, `search_audit_own` or `event_log` whose headers have
   `Org Name` + `Search Time` or `Search Time` + `Total Networks Searched` (audits), or `Event Type` + `Entity Type`
   (events). PDFs are skipped. Each unit is written as a gzip-compressed UTF-8 CSV,
   `<T>/muckrock_units/<hash>.csv.gz`, whose first column `__src_row` is the original row number. Uncompressed, the
   staged CSVs would need about 19 GB. Staging also records each zip member's SHA-256, and it writes its index to
   `<T>/muckrock_units/staged.json`. Re-stage whenever the evidence directory or `muckrock_ingest.py` changes: the
   build hashes containers live but reads the rows from the staged files.
2. **Load** (`build_truth.py OUT TMP [--repo CHECKOUT]`). This step:
   - creates the tables;
   - loads the RWC and Los Altos NDJSON (`read_ndjson_objects`, row order = line order), with the Los Altos
     production dates and request URLs from the README;
   - loads every staged MuckRock unit (`read_csv` on the `.csv.gz` with `all_varchar=true` and `parallel=false`, so
     `row_no` follows file order);
   - parses the SMPD PDFs with pymupdf in two spawned worker processes;
   - loads `dispositions.json` and `layouts.json`;
   - writes `build_info`.

   A MuckRock unit that fails to load is logged (`load error`) and skipped. As of build, all 771 staged units loaded.
   `AUDIT_DB_THREADS` and `AUDIT_DB_MEMORY` raise the defaults of 4 threads and 6 GB.

The build prints a warning when:

- the checkout is behind its local `origin/main`;
- a producer is not in the registry;
- a `layouts.json` pattern matches no release, or a listed `src_rows` row is missing;
- an SMPD PDF yields 0 rows (image-only);
- a Los Altos NDJSON has keys that are not in its manifest header.

It also prints each SMPD PDF's row count and how many rows were read by position.

After a truth rebuild, rebuild derived and regenerate coverage and stats ([derived.md](derived.md)).
`verify_provenance.py` checks rows against the originals ([provenance.md](provenance.md)).

## Invariants and checks

Checked as of build. Each query takes seconds in the standard session.

- **(`release_id`, `row_no`) is unique, and `row_no` runs 1…`n_rows` in each release.** In `flock_audit_rows`, if
  `row_no − rowid` is constant within a release then no two rows share a `row_no`, and a minimum of 1 with a maximum
  of `n` then makes it exactly 1…`n`:

  ```python
  con.sql("""
  SELECT count(*) AS releases,
         count(*) FILTER (WHERE min_off <> max_off OR min_no <> 1 OR max_no <> n) AS violations
  FROM (SELECT release_id, count(*) AS n, min(row_no) AS min_no, max(row_no) AS max_no,
               min(row_no - rowid) AS min_off, max(row_no - rowid) AS max_off
        FROM truth.flock_audit_rows GROUP BY release_id)""").show()   # 857 releases, 0 violations
  con.sql("""
  SELECT 'flock_event_rows' AS t, count(*) = count(DISTINCT (release_id, row_no)) AS unique_key FROM truth.flock_event_rows
  UNION ALL SELECT 'smpd_pdf_rows', count(*) = count(DISTINCT (release_id, row_no)) FROM truth.smpd_pdf_rows""").show()  # all true
  ```

- **`n_rows` matches the row tables** (0 mismatches; the image-only SMPD releases have `n_rows` 0 and no rows):

  ```python
  con.sql("""
  SELECT count(*) AS mismatches FROM truth.releases r
  LEFT JOIN (SELECT release_id, count(*) AS n FROM truth.flock_audit_rows GROUP BY 1
             UNION ALL SELECT release_id, count(*) FROM truth.flock_event_rows GROUP BY 1
             UNION ALL SELECT release_id, count(*) FROM truth.smpd_pdf_rows GROUP BY 1) c USING (release_id)
  WHERE coalesce(c.n, 0) <> r.n_rows""").show()
  ```

- **`row_no` is file order.** In 855 of the 857 audit releases, `src_row − row_no` is the same for every row: 1, or 0
  for the header-less RWC export. In the other two, the offset grows only at blank rows. In `smpd_pdf_rows`, `row_no`
  increases with (`src_page`, `src_line`).
- **Hashes are consistent.** `content_sha256` = `container_sha256` for every `smpd:` release and every unzipped `mr:`
  CSV, and `content_sha256` = `member_sha256` for every zipped `mr:` CSV. `member_sha256` is set exactly when `member`
  is.
- **Rows are where truth says they are.** `verify_provenance.py` opens the originals with readers independent of
  staging. For sampled `mr:` rows, it compares every cell (including `extra`) exactly, with no trimming, and it also
  checks each header row against `header_raw`. It samples `rwc:`/`la:` rows against the committed NDJSON line. For
  SMPD, it re-reads every PDF with poppler's `pdftotext -raw`: on each page, the ids printed must equal the ids truth
  puts there (every row, not a sample), and a sample of stored lines must appear on `src_page` or the next page,
  compared ignoring whitespace. It re-hashes every container, zip member, CSV content and NDJSON content against
  `releases` and `MANIFEST_v2.txt`. Scope and the latest result are in [provenance.md](provenance.md).

Finding a row's file (filtering on one `release_id` is fast):

```python
con.sql("""
SELECT a.release_id, a.row_no, a.src_row, r.container_root, r.container_path, r.member, r.sheet, r.source_url
FROM truth.flock_audit_rows a JOIN truth.releases r USING (release_id)
WHERE a.release_id = 'mr:196397:PRA25-746.csv#csv' AND a.row_no = 2""").show()
# -> src_row 4 of 196397-cathedral-city-police-department/PRA25-746.csv,
#    https://cdn.muckrock.com/foia_files/2025/11/06/PRA25-746.csv
```

Finding an SMPD row's page:

```python
con.sql("""
SELECT s.row_no, s.src_page, s.src_line, s.count_time_line, s.parse_note, r.container_path
FROM truth.smpd_pdf_rows s JOIN truth.releases r USING (release_id)
WHERE s.release_id = 'smpd:W012818-053026:4_1_2026-4_30_2026-San_Mateo_CA_PD-Audit__1_.pdf' AND s.row_no = 1""").show()
# -> page 1, line 5, '3 04/27/2026, 02:42:09 AM UTC', parse_note NULL,
#    assets/san-mateo-public-records/W012818-053026/4_1_2026-4_30_2026-San_Mateo_CA_PD-Audit__1_.pdf
```

For a ready-made citation, use `sighting_sources` ([provenance.md](provenance.md)), not these tables.

## Known gaps in this build

Verified 2026-09-26 against the build above. These are facts about truth's contents, not errors in how it was loaded.

- **SMPD, June 2024: image-only PDFs, not loaded.** `6_1_2024-6_30_2024-San_Mateo_CA_PD-Audit_-_PART_1.pdf` and
  `…PART_2.pdf` have no text layer. Their releases have `n_rows` 0,
  `header` `NULL`, and `header_basis` `image-only PDF: no text layer; OCR rows not loaded yet`. They have not been
  OCRed, so their row count is unknown and any SMPD total from truth undercounts June 2024.
- **Redwood City, `rwc:PRA_26_217_2025_7`: 71,799 repeated rows.** The release has 465,690 rows but 393,891
  distinct full rows. It has no `ID` column, so a repeat cannot be told from two searches that look the same in the
  exported columns ([semantics.md](semantics.md) §15).
- **Los Altos 26-366, own-search October and November 2025: unlabeled columns.** The cells are in `extra` as
  `column_5` (both months) and `column_1` (November), and every one is `REDACTED`. They sit where `License Plate` and
  `Name` sit in other months. No `layouts.json` entry assigns them, so derived reads `License Plate` (and `Name` in
  November) as not exported for those releases, not as redacted.
- **Not loaded from MuckRock.** Audit and event logs released as PDF (74 profiled units from 14 requests, as of
  build); units the profiler classed as audits or event logs whose headers lack the required columns (23); and other
  kinds (sharing lists, hotlists, user lists).
- **Agency markers stored as values.** For example, one producer's `License Plate` = `7923.600 GC`. How derived
  classifies markers is in [semantics.md](semantics.md).

## Public-safe versus local-only

- `truth.duckdb` is **local-only** and is never published as a file. `flock_audit_rows`, `flock_event_rows` and
  `smpd_pdf_rows` hold civilian data as agencies released it. `License Plate` is unmasked in some releases, and
  plates or other civilian identifiers can appear in free text (`Reason`, `Filters`, `Text Prompt`,
  `Entity Details`, `reason_line`). Officer names (`Name`, `user_line`) are public-employee data under the owner's
  policy, but these docs do not print them.
- The public export reads only `sightings_public`, which tokenizes plates ([pii.md](pii.md)).
- `releases`, `release_dispositions`, `release_layouts` and `build_info` hold no row content: only file names, public
  URLs, hashes, citations and local paths (`build_info`). Whether they ship with the export is decided in
  [pii.md](pii.md).
