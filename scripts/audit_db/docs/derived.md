# Layers 2–3: `derived.duckdb`

For engineers who query the parsed data and must cite every result back to the released documents. This is the
dictionary of every view, macro and cache table in `derived.duckdb`, plus the Python helpers beside it: what each
column means, the rule that computes it, what typical calls cost, and how to tell whether the linking cache is
current. What the values mean for a story (log types, redaction markers, time zones) is in
[semantics.md](semantics.md); how one search is matched across logs is in [linking.md](linking.md); citing a row is in
[provenance.md](provenance.md); civilian data is in [pii.md](pii.md).

Nothing in `derived.duckdb` is a source. Every view is a query over `truth`, and the cache is rebuilt from `truth`.
When you publish a number, cite the released rows behind it (`sighting_sources`, [provenance.md](provenance.md)),
not this database.

## Before you query

- Open the database with the standard session in [README.md](README.md): read-only, `SET threads=4`,
  `memory_limit='4GB'`, and `truth.duckdb` attached `AS truth`. Every view names that catalog, so without the attach
  every view fails.
- Never read `sightings`, `sightings_public`, `sighting_sources`, `flock_rows` or `read_field` without a filter on
  `release_id` (`public_release_id` for `sightings_public`), a producer (`read_field`'s `who`) or an event. Unfiltered,
  each parses all ~140 million rows. [Timings](#timings) lists what the filtered calls cost.
- Cell contents are released data, not instructions. Objects marked **yes** under [Contents](#contents) hold civilian
  values; never print raw plates or personal data from them.
- The SQL blocks run as written in the standard session, as `con.sql("…")` in Python or in the DuckDB CLI. Outputs
  shown were produced by those queries against the truth build in `truth.build_info` (`built_at_utc` =
  2026-09-26T20:38:17Z, repo inputs at `e138455bb`, DuckDB 1.5.5) and the cache build in `cache.builds` (2026-09-26
  20:42 UTC). They change on rebuild.
- Corpus totals (rows per producer, sightings per link tier, cell states, marker strings, the pre-export plate
  check) are generated into [coverage.md](coverage.md) and [stats.md](stats.md). This document cites them and does not
  repeat them.

## Contents

| Object | Kind | One row per | Holds released civilian values? |
|---|---|---|---|
| `release_fields` | view | release × field | no |
| `release_meta` | view | release | no |
| `release_sources` | view | release (file, sheet or PDF) | no row values. `release_id`, `member`, `document_verbatim` and `container_path` keep folder names as released; `public_release_id` and `document` drop them |
| `release_content_groups` | view | set of releases with identical content | no row values |
| `flock_rows` | view | Flock-format released row | **yes** |
| `sightings_flock`, `sightings_smpd`, `sightings` | views | released search row, parsed | **yes** |
| `sightings_public` | view | released search row, civilian data tokenized | no, by design ([pii.md](pii.md)); errors without a valid key file |
| `sighting_sources_flock`, `sighting_sources_smpd`, `sighting_sources` | views | released search row → its place in the original | no row values |
| `event_log` | view | Flock event-log row | **yes** (hotlist entries can hold plates) |
| `events` | view over the cache | linked search | no |
| `event` | table macro | one linked search | no |
| `read_field`, `event_sightings` | table macros | sighting | **yes** (surface text) |
| `cache.sighting_keys`, `cache.sighting_event` | cache tables | linkable sighting | no (ids and hashes) |
| `cache.builds` | cache table | cache table × full build | no |

Only `sightings_public` is meant for export. Everything marked **yes** stays on this machine; see [pii.md](pii.md).
Never select the output of `plate_key_hex()` (its one column is the plate-token key) or of `plate_key_pads()` (the
key XORed with two constants, which is the same as the key).

Python files in `<code>` that belong to this layer: `sql_templates.py` (the parse and citation SQL,
[below](#branch-views-and-sql_templatespy)), `audit_client.py` ([row and event lookups](#audit_clientpy)),
`cache_fingerprint.py` and `check_cache.py` ([Staleness](#staleness)).

## How the layers fit

| Layer | Where | Holds | Stored |
|---|---|---|---|
| 1 | `truth.duckdb` | Released rows verbatim, plus authored facts with citations ([truth.md](truth.md)) | yes, and it is the only ground truth |
| 2 | `derived.duckdb`, schema `main` | The views and macros below | only their SQL; results are computed on every read |
| 3 | `derived.duckdb`, schema `cache` | Event linking (`cache.sighting_keys`, `cache.sighting_event`) and the build log `cache.builds` | yes, because linking every sighting takes minutes |

`truth` is attached `READ_ONLY`, and the derived build never writes to it. A corrected reading, such as a
column-layout correction, is a view that reads `truth` differently. It is never a changed row.

### Building

```sh
# full build: layer-2 views and macros, then recompute the cache
rm -f derived.duckdb && nice -n 19 taskpolicy -b uv run --project <code> python <code>/build_derived.py truth.duckdb derived.duckdb
# views and macros only: keeps the cache tables and cache.builds as they are
nice -n 19 taskpolicy -b uv run --project <code> python <code>/build_derived.py truth.duckdb derived.duckdb --views-only
```

Run both from `<audit_db>`. The build reads `AUDIT_DB_THREADS` and `AUDIT_DB_MEMORY` (defaults 4 and
6GB); the 2026-09-26 full build ran its steps in 258 s at 8 threads and 14GB (`ingest.log`). It executes
`public_macros.sql`, which holds the civilian-PII macros and `sightings_public`.

The build stops with an error, before any cache work, if `release_sources.public_release_id` is NULL or not unique.

`--views-only` re-creates every layer-2 view and macro with `CREATE OR REPLACE` and re-runs `public_macros.sql`. If
`cache.sighting_event` exists, it also re-creates the objects that read the cache (`events`, `event`, `field_of`,
`read_field`, `event_sightings`). Then it exits. It never writes `cache.sighting_keys`, `cache.sighting_event` or
`cache.builds`. Use it after editing a view the cache does not read, such as `sightings_public`, `sighting_sources` or
`event_log`. If you edited anything linking reads (listed under [Staleness](#staleness)), `check_cache.py` then
reports the cache stale, and you need a full build.

### Branch views and `sql_templates.py`

The search rows come from two truth tables with different shapes: Flock-format exports in
`truth.flock_audit_rows` (one column per Flock header label) and San Mateo PD's own log in `truth.smpd_pdf_rows`
(one row per search-id block printed in the PDFs it produced: `id`, `user_line`, `count_time_line`, `reason_line`,
located by `src_page` and `src_line`; [truth.md](truth.md)). Each has its own branch view, and the view you query is
the union of the two:

- `sightings` = `sightings_flock` `UNION ALL BY NAME` `sightings_smpd`
- `sighting_sources` = `sighting_sources_flock` `UNION ALL BY NAME` `sighting_sources_smpd`

Both branches return the same columns with the same types.

**Row lookups prune.** A filter on `release_id` (or on `release_id` and `row_no`) passes through the union into each
branch and down to the truth table scan, where DuckDB skips row groups whose statistics rule the value out. The
per-release flags are pre-collected in `release_meta`, so each truth row needs one equality join. A filter on a value
the view computes, such as `sighting_id`, `t` or `org`, cannot use storage statistics, so the view parses every row
to evaluate it.

```sql
-- prunes
SELECT row_no, src_row, org, t, nets, reason_state FROM sightings WHERE release_id = 'mr:196397:PRA25-746.csv#csv';
-- does not prune: sighting_id is computed on read, so every row is parsed first
-- SELECT row_no, src_row, org, t, nets, reason_state FROM sightings WHERE sighting_id = 4748357201167110158;
```
```text
(1, 2, 'Miami-Dade FL SO', 2025-01-23 15:26:37, 5889, 'value')
(2, 4, 'Palos Heights IL PD', 2025-01-28 13:29:20, 5927, 'value')
(3, 7, 'Marshall County AL SO', 2025-01-31 14:26:06, 6076, 'value')
```

If you only have a `sighting_id`, look it up in `sighting_sources`, which hashes the key columns but parses no cells,
then query `sightings` by the `release_id` and `row_no` it returns. For a list of scattered rows, use
`audit_client.sightings_for` ([below](#audit_clientpy)).

**One definition of the parsing rules.** The SQL that parses and cites rows lives once, in `sql_templates.py`, as
five functions. Each returns SQL over a relation you name (a table, a view, a CTE name or a parenthesized subquery):

| Function | Input shaped like | Returns the columns of |
|---|---|---|
| `flock_rows_sql(src)` | `truth.flock_audit_rows` | `flock_rows` (fields read from the label that holds them, plus `release_meta` flags) |
| `sightings_flock_sql(src)` | `flock_rows` | `sightings_flock` |
| `sources_flock_sql(src)` | `truth.flock_audit_rows` | `sighting_sources_flock` |
| `sightings_smpd_sql(src)` | `truth.smpd_pdf_rows` | `sightings_smpd` |
| `sources_smpd_sql(src)` | `truth.smpd_pdf_rows` | `sighting_sources_smpd` |

`LAYOUT_OF` is the SQL expression that picks the `truth.release_layouts` mapping covering a row: the first entry of
`release_meta.layouts` whose range contains the row's `src_row`. Entries are listed latest-starting first, so where
two ranges overlap, the later-starting one applies.

Why the module exists: a lookup of a few rows is fast only when it reaches `truth` through literal `release_id` and
`row_no` filters. Joining a list of rows to the full `sightings` view on `sighting_id` parses every row instead. With
the rules in one importable module, a lookup selects its truth rows first and then applies exactly the SQL the full
views apply. It is used three ways:

1. by `build_derived.py`, over the full tables, to define `flock_rows`, `sightings_flock`, `sightings_smpd`,
   `sighting_sources_flock` and `sighting_sources_smpd`;
2. inside `read_field` and `event_sightings`, over only the truth rows of the selected events (`SEMI JOIN` on
   `(release_id, row_no)`);
3. by `audit_client.py`, over constant `IN`-lists of rows.

An edit reaches `audit_client.py` at once (it imports the module), but the stored views, `read_field` and
`event_sightings` only at the next build (full or `--views-only`); until then the two can disagree. Importing the
module runs nothing. The SQL it returns reads derived's views and macros
(`release_meta`, `release_sources`, `cell_state`, `flock_ts`, `tf_bound`, `clean_value`, `cite_text`), so run it on a
connection to `derived.duckdb` with `truth` attached.

```python
import sys; sys.path.insert(0, "<code>")
from sql_templates import flock_rows_sql, sightings_flock_sql
raw = "(SELECT * FROM truth.flock_audit_rows WHERE release_id = 'mr:196397:PRA25-746.csv#csv' AND row_no IN (1, 3))"
con.sql(sightings_flock_sql(f"({flock_rows_sql(raw)})")).select("row_no, t, nets, reason_state").fetchall()
```
```text
[(1, 2025-01-23 15:26:37, 5889, 'value'), (3, 2025-01-31 14:26:06, 6076, 'value')]
```

## Views: releases

### `release_fields`

One row per release and per field, for seven fields: `ID`, `Name`, `Org Name`, `License Plate`, `Reason`,
`Case #`, `Time Frame`. It says whether the field was in the released header and whether a cover letter says its
values were withheld.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_id` | VARCHAR | The release | = `truth.releases.release_id` |
| `producer` | VARCHAR | Flock organization whose log this is | from `truth.releases` |
| `audit` | VARCHAR | `network`, `own` or `event` | from `truth.releases` |
| `field` | VARCHAR | Canonical Flock field name | one of the seven above |
| `in_header` | BOOLEAN | The field is in the release's header | `list_contains(truth.releases.header, field)` |
| `withheld` | BOOLEAN | An authored disposition says the field was withheld | a `truth.release_dispositions` row with `disposition = 'withheld_blanked'` whose `release_pattern` matches (`release_id LIKE release_pattern`). Other dispositions, such as `redacted`, do not set it |

As of this build, 28 release × field rows have `withheld` true, all for `Reason` in Redwood City releases.
`in_header` ignores layout corrections; `flock_rows` applies them.

### `release_meta`

One row per release: what row parsing needs from the release, collected once so that each truth row joins it by
`release_id` alone.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_id` | VARCHAR | The release | |
| `producer` | VARCHAR | Flock organization whose log this is | |
| `audit` | VARCHAR | `network`, `own` or `event` | |
| `Org Name_h`, `Reason_h`, `Case #_h`, `Name_h`, `License Plate_h` | BOOLEAN | The field is in the release's header | `list_contains(header, '<field>')` |
| `Org Name_w`, `Reason_w`, `Case #_w`, `Name_w`, `License Plate_w` | BOOLEAN | The field is withheld per a disposition | `is_withheld(release_id, '<field>')` |
| `layouts` | STRUCT(`src_row_from` BIGINT, `src_row_to` BIGINT, `mapping` MAP(VARCHAR, VARCHAR))[] | Layout corrections for this release: rows released under the wrong labels | every `truth.release_layouts` row whose `release_pattern` matches, latest `src_row_from` first. NULL when there are none |

A `mapping` value is the Flock header label that actually holds the field, or the key of an unlabeled cell kept in
`extra` (such as `column13`), or NULL when those rows have no such cell. As of this build 6 releases have `layouts`:
Cathedral City's `PRA25-746.csv`, San Bruno's April 2024 own-search CSV, and the Santa Rosa February 2026 and May
2026 network-audit sheets in each of the two MuckRock 214823 productions. The February sheets list 19 single-row
entries each. The evidence for every entry is in `truth.release_layouts` ([truth.md](truth.md)).

### `release_sources`

One row per release: everything needed to name, fetch and verify the original. Most columns pass through from
`truth.releases` ([truth.md](truth.md)); how to use them is in [provenance.md](provenance.md).

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_id` | VARCHAR | The release (layer-1 key) | for a zip member it contains the member's folder path as released. Local |
| `public_release_id` | VARCHAR | The release id to publish | `public_rid(release_id, pra_id, container_path, member)`: the zip member's folders removed (`mr:<request>:<zip file>!<member file>#<sheet>`); otherwise equal to `release_id`. Unique and non-NULL, or the build stops (907 of 907 in this build; 325 differ from `release_id`) |
| `producer` | VARCHAR | Flock organization whose log this is | |
| `producer_agency_id` | VARCHAR | The producer's `agency_id` (UUID) in `assets/agency_registry.json` | not a slug |
| `audit` | VARCHAR | `network`, `own` or `event` | |
| `pra_id` | VARCHAR | Request id | `muckrock-<n>` for MuckRock requests |
| `request_label` | VARCHAR | Human-readable request name | computed: `MuckRock request <n>`; `San Mateo public records request <pra_id>` for SMPD; otherwise `<producer> public records request <pra_id>` |
| `request_url` | VARCHAR | The request's web page | set for MuckRock and Los Altos releases; NULL for Redwood City and SMPD |
| `released_on` | DATE | Production date | NULL when not recorded: as of this build every Redwood City and SMPD release. Los Altos dates come from the committed README |
| `released_on_basis` | VARCHAR | Where `released_on` comes from, or why it is NULL | |
| `container_file` | VARCHAR | File name of the container (the file itself, or the zip) | `basename(container_path)` |
| `member` | VARCHAR | Full path of the file inside a zip, as released | NULL when not zipped. Can hold folder names. Local |
| `member_file` | VARCHAR | File name of the zip member | `basename(member)`; NULL when not zipped |
| `sheet` | VARCHAR | Spreadsheet sheet name | for Los Altos NDJSON, the workbook sheet it was converted from (verbatim, trailing spaces included); NULL for CSV, SMPD and the Redwood City NDJSON |
| `source_file` | VARCHAR | Name of the released file | the file (or zip member) name for MuckRock; for repo NDJSON, the agency workbook it was converted from; for SMPD, the PDF |
| `document` | VARCHAR | The document a reader opens, public form | `<container_file> > <member_file>` for zip members, else `source_file`, else `container_file` |
| `document_verbatim` | VARCHAR | The same with the member's full path | `<container_file> > <member>` for zip members, else as `document`. Local |
| `container_root` | VARCHAR | `evidence` (MuckRock evidence directory) or `repo` (committed in the repo) | |
| `container_path` | VARCHAR | Path under that root | |
| `source_url` | VARCHAR | Download URL of the original | set for MuckRock releases only |
| `repo_url` | VARCHAR | GitHub permalink to the committed file | computed for `repo` releases: `<repo_web_url>/blob/<repo_commit>/<url_path(container_path)>` from `truth.build_info`; NULL for `evidence` |
| `container_sha256` | VARCHAR | SHA-256 of the container file | for MuckRock files it matches `MANIFEST_v2.txt` |
| `member_sha256` | VARCHAR | SHA-256 of the zip member's bytes | NULL when not zipped |
| `content_sha256` | VARCHAR | Hash of the release's content | set for every release; what it hashes per source is in [truth.md](truth.md). Groups re-releases (`release_content_groups`) |
| `src_row_basis` | VARCHAR | How `src_row` relates to what a reader sees in the original | |
| `link` | VARCHAR | Where to get the document and the hashes to verify it | MuckRock: `Download: <source_url> (SHA-256 <container_sha256>)`, plus `; <member_file> inside it: SHA-256 <member_sha256>` for a zip member. Repo NDJSON (Los Altos, Redwood City): `Committed conversion of the workbook (scripts/xlsx_to_audit_ndjson.py: cell text trimmed, empty cells omitted): <repo_url> (SHA-256 of the conversion <container_sha256>)`. SMPD: `Document: <repo_url> (SHA-256 <container_sha256>)` |

```sql
SELECT request_label, released_on, document, coalesce(source_url, repo_url) AS open_url
FROM release_sources WHERE release_id = 'mr:196397:PRA25-746.csv#csv';
```
```text
('MuckRock request 196397', 2025-11-06, 'PRA25-746.csv', 'https://cdn.muckrock.com/foia_files/2025/11/06/PRA25-746.csv')
```

### `release_content_groups`

One row per `content_sha256` that two or more releases share: the same content produced more than once. Because
`content_sha256` hashes content, not necessarily file bytes ([truth.md](truth.md)), a group means identical content,
not necessarily identical files.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `content_sha256` | VARCHAR | The shared content hash | |
| `n_releases` | BIGINT | Number of releases with it | always ≥ 2 |
| `releases` | VARCHAR[] | Their `release_id`s | ordered by `released_on` (NULLs last), then `release_id` |
| `released_on` | DATE[] | Their production dates | same order as `releases` |

As of this build there are 101 groups covering 219 releases, at most 3 per group. 13 of them pair a Los Altos month
of 2025 produced under both PRA 25-312 and PRA 26-366. Nothing is collapsed at read time unless you do it. See
[semantics.md](semantics.md) for how to count without double-counting re-releases.

## Views: parsed search rows

### `flock_rows`

Every row of `truth.flock_audit_rows`, with each of the 14 Flock fields read from the header label that actually
holds it, plus the release's header and withheld flags. This is where layout corrections apply.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_id` | VARCHAR | The release | |
| `row_no` | BIGINT | Row's position in the release as loaded (1-based) | with `release_id`, the row's key |
| `src_row` | BIGINT | Row number a reader sees in the original | how it maps is in `release_sources.src_row_basis` |
| `layout_corrected` | BOOLEAN | A layout-correction range of its release covers this row | `LAYOUT_OF` found a mapping |
| `ID`, `Name`, `Org Name`, `Total Networks Searched`, `Total Devices Searched`, `Time Frame`, `License Plate`, `Reason`, `Case #`, `Filters`, `Search Time`, `Search Type`, `Text Prompt`, `Moderation` | VARCHAR | The released value for that field | uncorrected rows: the truth cell under that label, verbatim. Corrected rows: the cell under the label (or `extra` key) that `mapping[field]` names; a field missing from the mapping keeps its own label; a field mapped to NULL is NULL |
| `extra` | JSON | Released columns outside the 14 Flock fields | e.g. San Jose's `Search Date` |
| `producer` | VARCHAR | Flock organization whose log this is | from `release_meta` |
| `audit` | VARCHAR | `network` or `own` | from `release_meta` |
| `Org Name_h`, `Org Name_w`, `Reason_h`, `Reason_w`, `Case #_h`, `Case #_w`, `Name_h`, `Name_w`, `License Plate_h`, `License Plate_w` | BOOLEAN | Header and withheld flags of the release | from `release_meta`, except that a layout mapping a field to NULL sets that field's `_h` flag false for the covered rows, so its state is `not_exported` |

As of this build `layout_corrected` is true for 913,049 rows: 455,584 in each of the two Santa Rosa May 2026 sheets
(from `src_row` 92527 on), 19 in each of the two Santa Rosa February 2026 sheets, all 1,840 rows of San Bruno's April
2024 own-search CSV, and all 3 rows of Cathedral City's `PRA25-746.csv`.

Two consequences to know: Cathedral City's `Case #` maps to NULL (the data rows have no such cell), so its
`case_state` is `not_exported` although the header lists `Case #`. The 19 Santa Rosa February 2026 rows released the
Moderation verdict under `Search Time` and have no time cell, so their `t` is NULL by design.

### `sightings`, `sightings_flock`, `sightings_smpd`

One row per released search row, parsed. A **sighting** is one log's record of one search. The same search appears
once in each log that recorded it, and once more for each re-release, and none of these rows are merged. For SMPD,
one sighting is one printed search-id block: a search printed in two PDFs, or twice in one PDF, is two sightings of
one event. Count distinct `flock_id` or `event_id`, not rows.

`sightings_flock` parses `flock_rows`; `sightings_smpd` parses `truth.smpd_pdf_rows`; `sightings` is their union.
Row count = rows of `truth.flock_audit_rows` + rows of `truth.smpd_pdf_rows` (totals in [coverage.md](coverage.md)).

| Column | Type | Meaning | Flock branch (`sightings_flock`) | SMPD branch (`sightings_smpd`) |
|---|---|---|---|---|
| `sighting_id` | UBIGINT | Row id | `hash(release_id, row_no)` | same |
| `release_id` | VARCHAR | The release | | |
| `row_no` | BIGINT | Row position in the release | | |
| `src_row` | BIGINT | Row number in the original | from truth | NULL: SMPD rows are located by page and line (`truth.smpd_pdf_rows.src_page`, `src_line`; `sighting_sources.locator`) |
| `producer` | VARCHAR | Flock organization whose log this is | from `release_meta` | from `truth.releases` (`San Mateo CA PD`) |
| `audit` | VARCHAR | `network` or `own` | from `release_meta` | from `truth.releases` (`own`) |
| `org` | VARCHAR | Organization that ran the search | trimmed `Org Name` if not blank; else the producer when an `own` release has no `Org Name` column; else NULL | the producer |
| `org_basis` | VARCHAR | How `org` was set | `released`, or `producer (own-search log without an Org Name column)`, or NULL when `org` is NULL | `producer (SMPD PDF export has no org field)` |
| `t` | TIMESTAMP | Search time, UTC, stored without a zone | `flock_ts("Search Time")`; if that fails, `flock_ts(extra."Search Date" \|\| ' ' \|\| "Search Time")` for San Jose's split date and time | `flock_ts` of the time in `count_time_line` (`<networks> MM/DD/YYYY, HH:MM:SS AM\|PM UTC`: the PDF prints both cells on one line); NULL when the line has another shape |
| `nets` | INTEGER | Networks searched | `flock_int("Total Networks Searched")` | the leading integer of `count_time_line` |
| `devices` | INTEGER | Devices searched | `flock_int("Total Devices Searched")` | NULL |
| `tf_start` | TIMESTAMP | Start of the searched time window | `tf_bound("Time Frame", 1)` | NULL |
| `tf_end` | TIMESTAMP | End of the searched time window | `tf_bound("Time Frame", 2)` | NULL |
| `flock_id` | VARCHAR | Flock's search UUID | trimmed `ID`, NULL when blank or `***` | trimmed `id`, same rule |
| `search_type` | VARCHAR | Flock search type, e.g. `search`, `lookup` | trimmed `Search Type` | NULL |
| `reason_surface` | VARCHAR | Reason as released | `Reason` | `reason_line` (verbatim, trailing spaces kept) |
| `reason_state` | VARCHAR | What the Reason cell holds | `cell_state("Reason", "Reason_h", "Reason_w", false, true)` | `cell_state(reason_line, true, false, false, true)` |
| `case_surface` | VARCHAR | Case number as released | `Case #` | NULL |
| `case_state` | VARCHAR | What the Case # cell holds | `cell_state("Case #", "Case #_h", "Case #_w", false, true)` | `'not_exported'` |
| `name_surface` | VARCHAR | Searcher (user) as released | `Name` | `user_line` when `parse_note` is NULL or starts with `text order differs from the printed row` (cells read by printed position); otherwise NULL (state `empty`), because a text-order fallback block can hold a neighbour's line. As of this build every block qualifies |
| `name_state` | VARCHAR | What the Name cell holds | `cell_state("Name", "Name_h", "Name_w", producer = 'Redwood City CA PD', false)`: initials count as `partial` only for Redwood City | `cell_state(name_surface, true, false, false, false)` |
| `plate_surface` | VARCHAR | Licence plate as released | `License Plate` | NULL |
| `plate_state` | VARCHAR | What the plate cell holds | `cell_state("License Plate", "License Plate_h", "License Plate_w", false, false)` | `'not_exported'` |
| `text_prompt` | VARCHAR | Free-text search prompt as released | `Text Prompt` | NULL |
| `filters` | VARCHAR | Search filters as released | `Filters` | NULL |
| `layout_corrected` | BOOLEAN | Fields were re-read per a layout correction | from `flock_rows` | false |
| `reason` | VARCHAR | Usable Reason text | `clean_value(reason_surface, reason_state)`: trimmed text when the state is `value`, else NULL | same |
| `case_no` | VARCHAR | Usable case number | `clean_value(case_surface, case_state)` | same |

Notes:

- `*_surface` is always the released text, including masks such as `***`. Use `reason` and `case_no` to count real
  values, and the `*_state` columns to count masks, blanks and withholdings. The states are listed under
  [Enumerations](#enumerations) and explained in [semantics.md](semantics.md); per-field totals are in
  [stats.md](stats.md) ("Cell states by field").
- `t`, `tf_start` and `tf_end` are naive `TIMESTAMP`s that mean UTC ([semantics.md](semantics.md)).
- `org` is a released value, not a mask: no `Org Name` cell is a mask in this build ([stats.md](stats.md), "Org Name
  cells").

### `sightings_public`

The same rows as `sightings`, named by `public_release_id`, with civilian data made safe to publish: plates become
HMAC tokens (`p1_` + 16 hex), and other civilian identifiers in free text are replaced by placeholders. It is the
only view meant for export. The policy and the token scheme are in [pii.md](pii.md); the corpus-wide pre-export
residue check is in [stats.md](stats.md).

**It fails loudly without a valid key.** The view cross-joins `plate_key_pads()`, which reads the key through
`plate_key_hex()`. A missing or empty key file, or one that is not 64 hex digits after whitespace is removed, raises
`plate token key missing, empty or not 64 hex digits: ~/.config/sm-alpr/plate_token_key (…)`. The error names the
file, never the key. The view's `WHERE k.pi IS NOT NULL` makes every read, including a bare `count(*)`, evaluate the
key. There is no empty-result or raw-plate fallback.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `sighting_id`, `row_no`, `src_row`, `producer`, `audit`, `org`, `org_basis`, `t`, `nets`, `devices`, `tf_start`, `tf_end`, `flock_id`, `layout_corrected` | as in `sightings` | as in `sightings` | passed through |
| `public_release_id` | VARCHAR | The release, public form | from `release_sources`. There is no `release_id` column; filter on this one, which prunes like `release_id` |
| `search_type` | VARCHAR | `sightings.search_type` when it looks like a search type | kept only when it matches `[A-Za-z][A-Za-z0-9 -]{0,40}` and holds no `9AAA999` plate shape, else NULL, so a cell misread from a shifted column never passes (`apiV1` is kept) |
| `reason` | VARCHAR | Released Reason text, scrubbed, plates tokenized | `tokenize_in(scrub_civilian(reason_surface), plate_candidates(scrub_civilian(reason_surface)), true, pi, po)`. Built from `reason_surface`, so masks and placeholders are kept. It is **not** `sightings.reason`: read it with `reason_state` |
| `reason_state` | VARCHAR | as in `sightings` | |
| `case_no` | VARCHAR | Released Case # text, plate-shaped values tokenized | `tokenize_in(case_surface, plate_candidates(case_surface), true, pi, po)`. Not scrubbed. Built from the surface, unlike `sightings.case_no` |
| `case_state` | VARCHAR | as in `sightings` | |
| `searcher_name` | VARCHAR | `name_surface` with plate-shaped words tokenized | `tokenize_in(name_surface, plate_candidates(name_surface), true, pi, po)`. Police employees' names pass verbatim (public-employee data, owner policy); a stray plate does not |
| `name_state` | VARCHAR | as in `sightings` | |
| `plate` | VARCHAR | Plate token, or a known mask as released | `plate_public(plate_surface, plate_state, pi, po)`: a token for `value`; NULL, blank, `***`, agency masks and exemption citations as released; anything else (such as `partial`) NULL |
| `plate_state` | VARCHAR | as in `sightings` | |
| `text_prompt` | VARCHAR | Prompt text, scrubbed, plates tokenized | `tokenize_in(scrub_civilian(text_prompt), plate_candidates(scrub_civilian(text_prompt)), true, pi, po)` |
| `filters` | VARCHAR | Filters with plate search terms tokenized | `tokenize_in(filters, filter_plate_candidates(filters), false, pi, po)` |

`pi` and `po` come from one `plate_key_pads()` row cross-joined into the query, so the key file is read once per
query, not per row.

```sql
SELECT count(*) FROM sightings_public WHERE public_release_id = 'mr:196397:PRA25-746.csv#csv';
```
```text
(3,)
```

## Views: from a row to the original

### `sighting_sources`, `sighting_sources_flock`, `sighting_sources_smpd`

One row per sighting: where the row is in the released original, and a ready-to-paste citation. `sighting_sources`
is the union of the Flock branch and the SMPD branch. How to use it, and how to verify a citation against the
timestamped evidence manifest, is in [provenance.md](provenance.md).

| Column | Type | Meaning | Flock branch | SMPD branch |
|---|---|---|---|---|
| `sighting_id` | UBIGINT | Same as `sightings.sighting_id` | `hash(release_id, row_no)` | same |
| `release_id` | VARCHAR | The release (layer-1 key; local) | | |
| `public_release_id` | VARCHAR | The release, public form | from `release_sources` | same |
| `row_no` | BIGINT | Row position in the release | | |
| `src_row` | BIGINT | Row number in the original | from truth | NULL |
| `pdf_pages` | VARCHAR | The PDF page the block is printed on | NULL | `<document> page <src_page>` |
| `producer` | VARCHAR | Flock organization whose log this is | from `release_sources` | same |
| `request_label` | VARCHAR | Request name | from `release_sources` | same |
| `request_url` | VARCHAR | Request web page | from `release_sources` | same (NULL) |
| `released_on` | DATE | Production date | from `release_sources` | same (NULL) |
| `document` | VARCHAR | Document to open, public form | `release_sources.document` | same (the PDF's name) |
| `document_verbatim` | VARCHAR | Document with the zip member's full path | `release_sources.document_verbatim`. Local | same |
| `sheet` | VARCHAR | Sheet name | `release_sources.sheet` | NULL |
| `layout_corrected` | BOOLEAN | A layout correction covers the row | `LAYOUT_OF` found a mapping | false |
| `locator` | VARCHAR | Where in the document | `row <src_row>`, plus `; cells released under other column labels (see release_layouts)` when `layout_corrected` | `page <src_page> (search id <id>)` |
| `open_url` | VARCHAR | Link to fetch the document | `source_url`, else `repo_url` | `repo_url`: GitHub permalink to the PDF at the build's commit |
| `sha256` | VARCHAR | SHA-256 of what `open_url` serves | `container_sha256` (the zip, for a zip member) | the PDF's SHA-256 |
| `member_sha256` | VARCHAR | SHA-256 of the zip member | from `release_sources`; NULL when not zipped | NULL |
| `src_row_basis` | VARCHAR | How `src_row` (or the page and line) maps to the original | from `release_sources` | same |
| `link` | VARCHAR | Link sentence used in the citation | `release_sources.link` | same |
| `citation` | VARCHAR | Full citation | `cite_text(producer, request_label, request_url, released_on, document, sheet, locator, link)` | same |

The citation uses only public forms (`document`, never `document_verbatim`). For a zip member it cites the zip's URL
and SHA-256 and the member's SHA-256; for repo NDJSON it says it is a committed conversion; and a layout-corrected
row is flagged in its locator.

```sql
SELECT citation FROM sighting_sources
WHERE release_id = 'mr:214820:ALPR_PRA_Response.07-28-2026T01-47-15_PDT.zip!ALPR PRA Response/Network Audit 6_6_2026-7_6_2026.csv#csv'
  AND row_no = 100;
```
```text
Cotati CA PD. MuckRock request 214820 (https://www.muckrock.com/foi/cotati-3170/flock-safety-alpr-records-contracts-audit-logs-sb-34-communications-cotati-police-department-214820/), produced 2026-07-28. ALPR_PRA_Response.07-28-2026T01-47-15_PDT.zip > Network Audit 6_6_2026-7_6_2026.csv, row 101. Download: https://cdn.muckrock.com/foia_files/2026/07/28/ALPR_PRA_Response.07-28-2026T01-47-15_PDT.zip (SHA-256 bfcc537f4fea43c04fad6edc22c745e6096fb602afa829161262a6379fb02b72); Network Audit 6_6_2026-7_6_2026.csv inside it: SHA-256 7423eda1281971377abff021d013cb1e437c942c693628bbbddfd5ca354f5f12
```

```sql
SELECT locator FROM sighting_sources WHERE release_id = 'mr:196397:PRA25-746.csv#csv' AND row_no = 1;
```
```text
row 2; cells released under other column labels (see release_layouts)
```

```sql
SELECT locator, citation FROM sighting_sources
WHERE release_id = 'smpd:W012541-041426:1_1_2023-1_31_2023-San_Mateo_CA_PD-Audit2.pdf' AND row_no = 1;
```
```text
page 1 (search id 4a324acc-eaf6-5b00-a9d9-2fa8c105130a)
San Mateo CA PD. San Mateo public records request W012541-041426, produced (production date not recorded). 1_1_2023-1_31_2023-San_Mateo_CA_PD-Audit2.pdf, page 1 (search id 4a324acc-eaf6-5b00-a9d9-2fa8c105130a). Document: https://github.com/none-below/sm-alpr/blob/e138455bb5582041266ea613ccfb74d8fcf737c4/assets/san-mateo-public-records/W012541-041426/1_1_2023-1_31_2023-San_Mateo_CA_PD-Audit2.pdf (SHA-256 9cbe411b77f2158d5471c36799fe89cca3cef19f56a4449f6a93764648812ee6)
```

## Views: event logs and linked searches

### `event_log`

One row per Flock event-log row: user administration, network-sharing changes and hotlist edits, with a parsed
timestamp and a citation. What the event types mean is in [semantics.md](semantics.md).

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_id` | VARCHAR | The release (local) | |
| `public_release_id` | VARCHAR | The release, public form | from `release_sources` |
| `row_no` | BIGINT | Row position in the release | |
| `src_row` | BIGINT | Row number in the original | |
| `producer` | VARCHAR | Flock organization whose log this is | from `release_sources` |
| `ts` | TIMESTAMP | Event time, UTC, stored without a zone | `coalesce(flock_ts("Timestamp"), iso_ts_utc("Timestamp"))`: an ISO 8601 value with `Z` or an offset is converted to UTC |
| `user` | VARCHAR | `User`, verbatim | the account that acted |
| `event_type` | VARCHAR | `Event Type`, verbatim | e.g. `create`, `update`, `delete` |
| `entity_type` | VARCHAR | `Entity Type`, verbatim | e.g. `networkShare`, `user`, `Custom Hotlist Entry` |
| `entity_details` | VARCHAR | `Entity Details`, verbatim | can hold plate numbers: as of this build 1,381 of 6,155 `Custom Hotlist Entry` rows contain a `plate_candidates()` match (1,191 an upper-case `9AAA999` word), and 2,330 contain the word "plate". Local only |
| `event_log_id` | VARCHAR | `Event Id`, verbatim | |
| `extra` | JSON | Other released columns | |
| `citation` | VARCHAR | Full citation | `cite_text(...)` with locator `row <src_row>` and `release_sources.link`. All 16 event-log releases are MuckRock files |

As of this build, 9,528 `Timestamp` values are ISO 8601 with `Z`, 49 end in `+00:00`, and 2,031 are spreadsheet
datetimes (`YYYY-MM-DD HH:MM:SS.ffffff`); every row has a `ts`.

### `events`

One row per linked search (`event_id` in `cache.sighting_event`). Linking is explained in [linking.md](linking.md).

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `event_id` | VARCHAR | The linked search | `u:<Flock search UUID>`, `k5:<hash>`, `k3:<hash>` or `x:<sighting_id>` |
| `event_key` | UBIGINT | `hash(event_id)` | a compact join key |
| `n_sightings` | BIGINT | Sightings linked to it | includes re-releases and SMPD blocks printed twice |
| `n_logs` | BIGINT | Distinct producers among them | a producer's network and own-search logs count once |
| `weakest_link` | VARCHAR | Weakest link tier among its sightings | `max(basis)`: tier labels sort from strongest (`1_uuid`) to weakest (`x_ambiguous`) |
| `logs` | VARCHAR[] | Those producers, sorted | |

The view groups by both `event_id` and `event_key`, so a filter on either is applied before the grouping. For one
search, use `event(eid)` ([Macros](#comparing-logs-and-drilling-into-one-search)): it filters the integer
`event_key` first, which is cheaper than comparing `event_id` strings on every cache row. An unfiltered read groups
all ~140 million cache rows and can spill tens of GB to disk. Distributions over all events are in
[stats.md](stats.md) ("Searches by number of logs").

## Macros

All are SQL macros stored in `derived.duckdb`. The scalar ones can be used anywhere; `read_field`, `event`,
`event_sightings`, `plate_key_hex` and `plate_key_pads` are table macros (`FROM macro(...)`).

| Macro | Returns | Used by |
|---|---|---|
| `flock_ts(s)` | TIMESTAMP | `sightings_*`, `tf_bound`, `event_log` |
| `tf_bound(s, i)` | TIMESTAMP | `sightings_flock` |
| `flock_int(s)` | INTEGER | `sightings_flock` |
| `iso_ts_utc(s)` | TIMESTAMP | `event_log` |
| `agency_mask(raw)` | BOOLEAN | `cell_state`, `plate_public` |
| `exemption_cite(raw)` | BOOLEAN | `cell_state`, `plate_public` |
| `cell_state(raw, in_header, withheld, partial_ok, placeholder_ok)` | VARCHAR state label | `sightings_*` |
| `clean_value(raw, st)` | VARCHAR | `sightings_*` |
| `is_withheld(rid, fld)` | BOOLEAN | `release_meta` (`release_fields` inlines the same test) |
| `basename(p)` | VARCHAR | `release_sources`, `public_rid` |
| `url_path(p)` | VARCHAR | `release_sources` |
| `public_rid(rid, pra_id, container_path, member)` | VARCHAR | `release_sources` |
| `cite_text(producer, request_label, request_url, released_on, document, sheet, locator, link)` | VARCHAR | `sighting_sources_*`, `event_log`, `event_sightings` |
| `field_of(fld, reason_v, case_v)` | either value argument | `read_field` |
| `divergence(state, value, revealed)` | VARCHAR label | `read_field` |
| `read_field(fld, who := NULL)` | table | you |
| `event(eid)` | table | you |
| `event_sightings(eid)` | table | you |
| `plate_norm(p)` | VARCHAR | plate macros |
| `plate_key_hex()` | table (`khex`) | `plate_key_pads` only; never select it |
| `plate_pad(khex, a, b)` | BLOB | `plate_key_pads` |
| `plate_key_pads()` | table (`pi`, `po`) | `sightings_public`, `plate_token` |
| `plate_hmac(p, pi, po)` | VARCHAR | `plate_token`, `plate_public`, `tokenize_in` |
| `plate_token(p)` | VARCHAR | you (needs the key file) |
| `plate_public(surface, state, pi, po)` | VARCHAR | `sightings_public` |
| `plate_candidates(txt)` | VARCHAR[] | `sightings_public` |
| `filter_plate_part(r)` | VARCHAR | `filter_plate_candidates`, `tokenize_in` |
| `filter_plate_candidates(txt)` | VARCHAR[] | `sightings_public` |
| `tokenize_in(txt, cands, word, pi, po)` | VARCHAR | `sightings_public` |
| `scrub_civilian(txt)` | VARCHAR | `sightings_public` |

### Parsing and classification

**`flock_ts(s)`** parses a timestamp as Flock exports print it (`09/14/2025, 3:04:05 PM UTC`) or as a spreadsheet
datetime was staged (`2025-09-14 15:04:05`, optionally with fractional seconds). It strips carriage returns and a
trailing `UTC`. It returns a naive `TIMESTAMP` meaning UTC, or NULL for any other format.

```sql
SELECT flock_ts('09/14/2025, 3:04:05 PM UTC'), flock_ts('2025-09-14 15:04:05.250'), flock_ts('14 Sep 2025');
```
```text
(2025-09-14 15:04:05, 2025-09-14 15:04:05.250000, NULL)
```

**`tf_bound(s, i)`** returns bound `i` of a `Time Frame` cell (1 = start, 2 = end): it splits the cell on a line
feed or on `to` with whitespace on both sides, and parses part `i` with `flock_ts`. Flock prints two lines
(`<start> UTC`, `<end> UTC`); Lodi's own-search log prints one line, `<start> UTC to <end> UTC`. NULL when the part
is missing or does not parse.

```sql
SELECT tf_bound('09/14/2025, 1:00:00 PM UTC' || chr(10) || '09/14/2025, 3:00:00 PM UTC', 2),
       tf_bound('09/14/2025, 1:00:00 PM UTC to 09/14/2025, 3:00:00 PM UTC', 1), tf_bound('09/14/2025, 1:00:00 PM UTC', 2);
```
```text
(2025-09-14 15:00:00, 2025-09-14 13:00:00, NULL)
```

**`flock_int(s)`** casts text to DOUBLE, then to INTEGER; NULL when that fails. Spreadsheet numbers staged as
`3.0` therefore parse, and a fractional value is rounded.

```sql
SELECT flock_int('5889'), flock_int('3.0'), flock_int(' 12 '), flock_int('n/a'), flock_int('2.7');
```
```text
(5889, 3, 12, NULL, 3)
```

**`iso_ts_utc(s)`** parses an ISO 8601 timestamp to a naive `TIMESTAMP` meaning UTC. When the trimmed text ends in a
time followed by `Z`, `z` or an offset (`±HH`, `±HHMM` or `±HH:MM`), it is cast to `TIMESTAMPTZ` and the
instant is returned in UTC, whatever the session `TimeZone`. Anything else goes through `TRY_CAST(trim(s) AS TIMESTAMP)`. NULL when neither
parses. A plain cast to `TIMESTAMP` would drop an offset without converting it; this macro converts.

```sql
SELECT iso_ts_utc('2025-01-01T10:00:00Z'), iso_ts_utc('2025-01-01T10:00:00.000+07:00'),
       iso_ts_utc('2025-01-01 10:00:00.250000'), iso_ts_utc('Jan 1');
```
```text
(2025-01-01 10:00:00, 2025-01-01 03:00:00, 2025-01-01 10:00:00.250000, NULL)
```

**`agency_mask(raw)`** is true when the trimmed cell is an agency's redaction mask: `REDACTED` or `[REDACTED]` (any
case), or entirely two or more `#`, asterisks separated by single spaces (`* * *`), or block glyphs (`█`, `■`).
Flock's own `***` is not an agency mask (`cell_state` gives it `redacted_flock`), and a mask followed by text is not
a whole-cell mask.

```sql
SELECT agency_mask('REDACTED'), agency_mask(' [redacted] '), agency_mask('###'), agency_mask('* * *'),
       agency_mask('██'), agency_mask('***'), agency_mask('REDACTED 459');
```
```text
(true, true, true, true, true, false, false)
```

**`exemption_cite(raw)`** is true when the whole trimmed cell is one or more exemption citations typed in place of
the value, as in Port Hueneme's `7923.600 GC` cells (counts per field in [stats.md](stats.md), "Redaction marker
strings"). A citation is a CPRA `79xx.xxx` or Civil Code `1798.90.x` section, with or without a code label, or a
pre-2023 CPRA `62xx` section with a code label. Labels: `GC`, `CGC`, `G.C.`, and `Gov`, `Gov.`, `Govt`,
`Government`, `Civ`, `Civ.` or `Civil` + `Code`, optionally after `Cal.` or `California`, before or after the number
(also `Gov't` / `Gov’t` with an apostrophe); `§`, `§§` or `sec.` and subdivisions such as
`(a)` are allowed. Several citations may be joined by `,`, `;`, `/`, `&`, `+` or `and`, with an optional final
period. Case-insensitive. The regular expression is `EXEMPTION_RE` in `build_derived.py`.

```sql
SELECT exemption_cite('7923.600 GC'), exemption_cite('Gov. Code § 7923.600(a)'), exemption_cite('GC 6254(f)'),
       exemption_cite('Civ. Code 1798.90.55'), exemption_cite('§ 7922.000, 7923.600'), exemption_cite('6254'),
       exemption_cite('459 PC'), exemption_cite('7923.600 GC stolen');
```
```text
(true, true, true, true, true, false, false, false)
```

**`cell_state(raw, in_header, withheld, partial_ok, placeholder_ok)`** classifies one cell. The first matching rule
wins:

| # | Condition | Result |
|---|---|---|
| 1 | field not in the header, and withheld | `withheld` |
| 2 | field not in the header | `not_exported` |
| 3 | cell NULL or blank | `withheld` if withheld, else `empty` |
| 4 | cell is `***` (Flock's own mask) | `redacted_flock` |
| 5 | `agency_mask(raw)` or `exemption_cite(raw)` | `redacted_agency` |
| 6 | cell starts with `REDACTED` or `[REDACTED]` (any case) and has a letter or digit after it | `partial` |
| 7 | `partial_ok`, and the cell is an optional letter (either case), a period, an optional space and 1–3 letters or apostrophes, such as `X. Ab` or `x. Ab` | `partial` |
| 8 | `placeholder_ok`, and the cell is a listed junk word (`none`, `n/a`, `na`, `-`, `--`, `xxx`, `x`, `*`, `.`, `0`, `test`, `null`) or has no letter or digit | `placeholder` |
| 9 | anything else | `value` |

```sql
SELECT cell_state(NULL, false, false, false, true), cell_state('', true, true, false, true),
       cell_state('***', true, false, false, true), cell_state('###', true, false, false, true),
       cell_state('7923.600 GC', true, false, false, true), cell_state('REDACTED / 459 SUS', true, false, false, true),
       cell_state('a. Bc', true, false, true, false), cell_state('n/a', true, false, false, true),
       cell_state('459 PC', true, false, false, true);
```
```text
('not_exported', 'withheld', 'redacted_flock', 'redacted_agency', 'redacted_agency', 'partial', 'partial', 'placeholder', 'value')
```

**`clean_value(raw, st)`** returns `trim(raw)` when `st = 'value'`, else NULL.

```sql
SELECT clean_value('  459 PC ', 'value'), clean_value('***', 'redacted_flock');
```
```text
('459 PC', NULL)
```

**`is_withheld(rid, fld)`** is true when a `truth.release_dispositions` row with `disposition = 'withheld_blanked'`
matches the release (`rid LIKE release_pattern`) and the field.

```sql
SELECT is_withheld('rwc:PRA_26_217_2024_Q1', 'Reason'), is_withheld('rwc:PRA_26_217_2024_Q1', 'License Plate');
```
```text
(true, false)
```

The second is false because Redwood City's plate disposition is `redacted`, not `withheld_blanked`.

### Citation and provenance

**`basename(p)`** returns the last component of a path (after the last `/` or `\`).

**`url_path(p)`** percent-encodes `%`, space, `#` and `?` so a repo path can sit in a URL path (`repo_url`).

```sql
SELECT basename('a/b/c.xlsx'), basename('c.csv'), url_path('assets/x y/a#1?.pdf');
```
```text
('c.xlsx', 'c.csv', 'assets/x%20y/a%231%3F.pdf')
```

**`public_rid(rid, pra_id, container_path, member)`** is the rule behind `public_release_id`. For a zip member
whose `rid` starts with `mr:<request>:<zip file>!`, it removes everything after the `!` up to the last `/` or `\`,
which are the member's folders; sheet names and container file names cannot contain either separator. Any other
`rid` is returned unchanged. Folder names inside a released zip can carry text that should not be republished, such
as a requester's name; the public form never has them.

```sql
SELECT public_rid('mr:1:r.zip!Folder A/Sub/audit.xlsx#Sheet1', 'muckrock-1', 'x/r.zip', 'Folder A/Sub/audit.xlsx'),
       public_rid('mr:1:a.csv#csv', 'muckrock-1', 'x/a.csv', NULL);
```
```text
('mr:1:r.zip!audit.xlsx#Sheet1', 'mr:1:a.csv#csv')
```

**`cite_text(producer, request_label, request_url, released_on, document, sheet, locator, link)`** assembles
`<producer>. <request_label> (<request_url>), produced <YYYY-MM-DD>. <document>, sheet "<sheet>", <locator>. <link>`.
The parts for `request_url`, `sheet`, `locator` and `link` are left out when NULL, and a NULL `released_on`
prints `(production date not recorded)`. A NULL `producer`, `request_label` or `document` makes the whole result
NULL.

```sql
SELECT cite_text('P', 'L', NULL, NULL, 'D', NULL, NULL, NULL),
       cite_text('P', 'L', 'U', DATE '2025-01-02', 'D', 'S', 'row 5', 'Link');
```
```text
('P. L, produced (production date not recorded). D.', 'P. L (U), produced 2025-01-02. D, sheet "S", row 5. Link')
```

### Comparing logs and drilling into one search

**`field_of(fld, reason_v, case_v)`** returns `reason_v` when `lower(fld) = 'reason'` and `case_v` when it is
`'case'`. Any other value, including NULL, raises an error, so `read_field` rejects an unknown field before reading
anything.

```sql
SELECT field_of('reason', 'R', 'C'), field_of('CASE', 'R', 'C');
SELECT field_of('plate', 'R', 'C');
```
```text
('R', 'C')
Invalid Input Error: read_field: field must be 'reason' or 'case', not 'plate'
```

**`divergence(state, value, revealed)`** labels how one sighting's value compares with what other logs released for
the same search. `value` is the sighting's clean value (`reason` or `case_no`), not its surface text. The first
matching rule wins:

| Condition | Result |
|---|---|
| `revealed` is NULL | `no_other_record` |
| `state` is `redacted_flock`, `redacted_agency`, `withheld` or `partial` | `masked_here_released_elsewhere` |
| `state` is `empty` or `not_exported` | `blank_here_present_elsewhere` |
| `state` is `placeholder` | `placeholder_here` |
| `value = revealed` (exact, case-sensitive) | `same` |
| otherwise | `differs_from_other_logs` |

```sql
SELECT divergence('value', 'a', NULL), divergence('redacted_flock', NULL, 'a'), divergence('empty', NULL, 'a'),
       divergence('placeholder', NULL, 'a'), divergence('value', 'a', 'a'), divergence('value', 'a', 'b');
```
```text
('no_other_record', 'masked_here_released_elsewhere', 'blank_here_present_elsewhere', 'placeholder_here', 'same', 'differs_from_other_logs')
```

**`read_field(fld, who := NULL)`** is a table macro. For each sighting it returns that log's Reason
(`fld = 'reason'`) or Case # (`fld = 'case'`), and what the other logs that recorded the same search released.
Nothing is stored.

| Column | Type | Meaning |
|---|---|---|
| `sighting_id` | UBIGINT | The sighting |
| `producer` | VARCHAR | Its log |
| `event_id` | VARCHAR | Its linked search |
| `state` | VARCHAR | Its `reason_state` or `case_state` |
| `surface` | VARCHAR | Its released text (`reason_surface` or `case_surface`; local only) |
| `revealed` | VARCHAR | Consensus value from the other sightings of the search, or NULL |
| `support` | BIGINT | Number of distinct producers behind `revealed` |
| `divergence` | VARCHAR | `divergence(state, <its clean value>, revealed)` |

How it reads:

1. The events to compare: the `event_key`s of `who`'s sightings in `cache.sighting_event` (every producer's when
   `who` is NULL), leaving out `x_ambiguous`.
2. Every cache row of those events, from every producer.
3. With `who`, only those rows are read from `truth.flock_audit_rows` and `truth.smpd_pdf_rows` (`SEMI JOIN` on
   `(release_id, row_no)`) and parsed with the `sql_templates.py` SQL. Without `who`, they are joined to the full
   `sightings` view, which parses every row in the database.
4. Output: only `who`'s sightings (all, without `who`).

With `who`, cost grows with the number of sightings in that producer's events, not with the database: a producer
with a few sightings answers in about a second ([Timings](#timings)), a producer whose logs hold millions of rows
parses those rows and every other log's copy of the same searches. Without `who` it parses all ~140 million rows;
never run that in a shared session.

How `revealed` is chosen, per event: count, for each clean value (state `value`), the distinct producers that
released it. Call the top value `v1` (with `n1` producers) and the runner-up `v2` (`n2`). Ties are broken by the value
itself (`first(value ORDER BY n DESC, value)`), so repeated runs agree.

- If this sighting's own value is `v1`, leave its producer out: `revealed` is `v1` with support `n1 − 1` when
  `n1 > 1` and `n1 − 1 ≥ n2`; otherwise `v2` with support `n2` if there is a runner-up; otherwise NULL.
- Otherwise (masked, blank, placeholder, or a different value), `revealed` is `v1` with support `n1`. Here `n1`
  can include this sighting's own producer, through another release of the same log, so
  `masked_here_released_elsewhere` can mean that the agency released the value in another production.

Sightings in `x:` (ambiguous) events and sightings not in the cache at all are not returned. How to use and cite the
comparison is in [linking.md](linking.md).

```sql
SELECT sighting_id, event_id, state, support, divergence FROM read_field('reason', who := 'Cathedral City CA PD');
```
```text
(4748357201167110158, 'u:aa84e168-9c45-48f6-b11a-0b670f8724f9', 'value', 5, 'same')
(2019124353321464460, 'u:d46e138d-517a-4f9f-9ae8-632989c6c998', 'value', 5, 'same')
(8090089599605089816, 'u:b76f83ba-3649-4820-855e-57ec6d62ea9a', 'value', 6, 'same')
```

**`event(eid)`** is a table macro returning one row with the columns of `events` for one event id, or no row for an
unknown id. It finds the event's cache rows by `event_key = hash(eid)` (an integer column), then `event_id = eid`.

```sql
SELECT n_sightings, n_logs, weakest_link, len(logs) FROM event('u:aa84e168-9c45-48f6-b11a-0b670f8724f9');
```
```text
(13, 12, '2_k5', 12)
```

**`event_sightings(eid)`** is a table macro: every log's record of one search, with its states and a citation. It
finds the event's cache rows as `event` does, selects their truth rows from `truth.flock_audit_rows` and
`truth.smpd_pdf_rows` by `(release_id, row_no)`, and parses and cites only those, with the `sql_templates.py` SQL
([above](#branch-views-and-sql_templatespy)).

| Column | Type | Meaning |
|---|---|---|
| `producer` | VARCHAR | Log |
| `basis` | VARCHAR | Link tier of this sighting (`cache.sighting_event.basis`) |
| `release_id` | VARCHAR | Release (local; public form in the citation) |
| `row_no` | BIGINT | Row position in the release |
| `src_row` | BIGINT | Row number in the original (NULL for SMPD) |
| `org` | VARCHAR | Organization that ran the search |
| `t` | TIMESTAMP | Search time, UTC |
| `nets` | INTEGER | Networks searched |
| `reason_state` | VARCHAR | State of the Reason cell |
| `reason_surface` | VARCHAR | Reason as released (local only) |
| `case_state` | VARCHAR | State of the Case # cell |
| `case_surface` | VARCHAR | Case # as released (local only) |
| `name_state` | VARCHAR | State of the Name cell |
| `plate_state` | VARCHAR | State of the plate cell |
| `citation` | VARCHAR | Full citation, as in `sighting_sources` |

Rows are ordered by `producer`, `release_id`, `row_no`.

```sql
SELECT producer, basis, src_row, org, t, nets, reason_state, case_state, plate_state
FROM event_sightings('u:aa84e168-9c45-48f6-b11a-0b670f8724f9') LIMIT 4;
```
```text
('Alameda County CA SO', '2_k5', 2, 'Miami-Dade FL SO', 2025-01-23 15:26:37, 5889, 'not_exported', 'empty', 'not_exported')
('Cathedral City CA PD', '2_k5', 2, 'Miami-Dade FL SO', 2025-01-23 15:26:37, 5889, 'value', 'not_exported', 'value')
('Contra Costa County CA SO', '2_k5', 147558, 'Miami-Dade FL SO', 2025-01-23 15:26:37, 5889, 'redacted_agency', 'empty', 'redacted_agency')
('Danville CA PD', '2_k5', 25510, 'Miami-Dade FL SO', 2025-01-23 15:26:37, 5889, 'redacted_agency', 'empty', 'redacted_agency')
```

The full result has 13 rows from 12 producers (Los Altos produced this month twice).

### Civilian PII

These implement the export policy in [pii.md](pii.md). The examples use synthetic plate-shaped strings such as
`0XXX000`, not real plates. Tokens are masked in the outputs shown here.

**`plate_norm(p)`** returns `p` with everything except letters and digits removed, upper-cased; NULL when nothing is
left.

```sql
SELECT plate_norm('0xx-x 000'), plate_norm(' - ');
```
```text
('0XXX000', NULL)
```

**`plate_key_hex()`** is a table macro returning one row, `khex`: the key read from
`~/.config/sm-alpr/plate_token_key` with every space, tab, CR and LF removed, lower-cased (the same normalization as
`plate_key.py`). A missing or empty file, or text that is not then exactly 64 hex digits, raises an error
that starts `plate token key missing, empty or not 64 hex digits`, names the file and says how to create it
(`openssl rand -hex 32`; in CI, `plate_key.py --install`). It exists for `plate_key_pads()`. Never select it: its column is the key.

**`plate_pad(khex, a, b)`** is the HMAC key-pad step. It right-pads the trimmed, lower-cased hex key with `0` to 128
hex digits (64 bytes) and XORs each byte with the nibble pair (`a`, `b`): (3, 6) gives the inner pad (`0x36`) and
(5, 12) the outer pad (`0x5c`). Shown here on a dummy all-zero key:

```sql
SELECT octet_length(plate_pad('00', 3, 6)), left(hex(plate_pad('00', 3, 6)), 8), left(hex(plate_pad('00', 5, 12)), 8);
```
```text
(64, '36363636', '5C5C5C5C')
```

**`plate_key_pads()`** is a table macro returning one row (`pi` BLOB, `po` BLOB): the inner and outer pads of the
key from `plate_key_hex()`. It raises the same error without a valid key. Use it only inside an expression, as
`sightings_public` does. Never select its columns: they are equivalent to the key.

**`plate_hmac(p, pi, po)`** returns `'p1_'` plus the first 16 hex digits of HMAC-SHA256(key,
`'plate:v1:' || plate_norm(p)`), computed from pads passed in. Lambdas cannot run subqueries, so the tokenizing macros take the pads
as arguments.

**`plate_token(p)`** is `plate_hmac(p, pi, po)` over `plate_key_pads()`, so it works in any client that has the key
file; the key is never stored in any `.duckdb` file. NULL or blank input gives NULL, and spelling variants of one
plate give one token. Without a valid key file it raises the `plate_key_hex()` error rather than returning NULL.

```sql
SELECT regexp_full_match(plate_token('0XXX000'), 'p1_[0-9a-f]{16}'), plate_token('0xxx-000') = plate_token('0XXX000'), plate_token('');
SELECT plate_hmac('0XXX000', k.pi, k.po) = plate_token('0XXX000') FROM plate_key_pads() k;
```
```text
(true, true, NULL)
(true,)
```

**`plate_public(surface, state, pi, po)`** is the rule for `sightings_public.plate`: `plate_hmac(surface)` when
`state = 'value'`; the surface as released when it is NULL, blank, `***`, an `agency_mask` or an `exemption_cite`;
NULL for anything else (such as a `partial` cell, `REDACTED` followed by text), so no raw plate text reaches a public
output.

```sql
SELECT regexp_replace(plate_public('0XXX000', 'value', k.pi, k.po), 'p1_[0-9a-f]{16}', 'p1_<16 hex>'),
       plate_public('***', 'redacted_flock', k.pi, k.po), plate_public('7923.600 GC', 'redacted_agency', k.pi, k.po),
       plate_public('', 'empty', k.pi, k.po), plate_public('REDACTED 0XXX', 'partial', k.pi, k.po)
FROM plate_key_pads() k;
```
```text
('p1_<16 hex>', '***', '7923.600 GC', '', NULL)
```

**`plate_candidates(txt)`** lists the substrings of free text to tokenize, favouring precision so that case numbers
survive. Tier A: the California standard shape `9AAA999` (digit, 3 letters, 3 digits) anywhere as a word, any case.
Tier B: the shapes `99999A9`, `9A99999`, `9AA9999`, `9A9A999` and `AA99A99` (commercial, trailer and others), only
within 15 non-alphanumeric characters after a plate word (`plate`, `lp`, `lic`, `license`, `tag`, `stolen plate`,
`alert`) or when the shape is the whole field. The shapes `AAA9999`, `AA99999`, `AAA999` and `A9999999` are never
matched, because they are case-number formats. Returns `[]` for NULL.

```sql
SELECT plate_candidates('stolen plate 0XXX000 case 24-001234'), plate_candidates('lp: 0XX0000'),
       plate_candidates('case 0XX0000'), plate_candidates('XXX0000'), plate_candidates('0xxx000');
```
```text
(['0XXX000'], ['0XX0000'], [], [], ['0xxx000'])
```

**`filter_plate_part(r)`** classifies one run of letters and digits from a `Filters` cell, where search terms are
glued onto tags. It returns the plate part of the run, or NULL:

1. the whole run is a tier A or tier B shape, any case: the run;
2. letters, then a tier A shape at the end, with a tag of any case: the last 7 characters. Flock concatenates vehicle
   attributes onto plates in `Filters` (`Chevrolet0XXX000`, `whitecalifornia0XXX000`);
3. a lower-case tag, then a tier A shape or a tier B shape that starts with a digit: the part after the tag
   (`stolen0XX0000`).

A shape is never cut out of a longer run (`0XXX0000` gives NULL), and a tier B shape after a capitalized tag is not
matched.

```sql
SELECT filter_plate_part('0xxx000'), filter_plate_part('Chevrolet0XXX000'), filter_plate_part('stolen0XX0000'),
       filter_plate_part('Stolen0XX0000'), filter_plate_part('0XXX0000'), filter_plate_part('hotlist');
```
```text
('0xxx000', '0XXX000', '0XX0000', NULL, NULL, NULL)
```

**`filter_plate_candidates(txt)`** splits `txt` into runs of letters and digits, applies `filter_plate_part` to each
and returns the distinct non-NULL results; `[]` for NULL.

```sql
SELECT filter_plate_candidates('stolen0XXX000,hotlist'), filter_plate_candidates('whitecalifornia0XXX000;0xx0000'),
       filter_plate_candidates(NULL);
```
```text
(['0XXX000'], ['0xx0000', '0XXX000'], [])
```

**`tokenize_in(txt, cands, word, pi, po)`** replaces plates in `txt` with `[<token>]`. With `word` true (free text),
each candidate in `cands` is replaced case-insensitively between word boundaries. With `word` false (`Filters`),
`cands` is not used: the text is cut into runs of letters and digits and the rest, and every run for which
`filter_plate_part` is not NULL has that part replaced, keeping the tag (`stolen[p1_…]`), so adjacent plates are all
replaced and nothing is cut out of a longer run. NULL `txt` gives NULL.

```sql
SELECT regexp_replace(tokenize_in('stolen plate 0XXX000 case 24-001234',
         plate_candidates('stolen plate 0XXX000 case 24-001234'), true, k.pi, k.po), 'p1_[0-9a-f]{16}', 'p1_<16 hex>', 'g'),
       regexp_replace(tokenize_in('stolen0XXX000,0XX0000,hotlist',
         filter_plate_candidates('stolen0XXX000,0XX0000,hotlist'), false, k.pi, k.po), 'p1_[0-9a-f]{16}', 'p1_<16 hex>', 'g')
FROM plate_key_pads() k;
```
```text
('stolen plate [p1_<16 hex>] case 24-001234', 'stolen[p1_<16 hex>],[p1_<16 hex>],hotlist')
```

**`scrub_civilian(txt)`** replaces other civilian identifiers, and only when a keyword anchors them, because bare
digit runs are often case numbers:

- upper-case `LAST, FIRST` up to 30 characters before `DOB`, `D.O.B` or `dob` → `[name]` (case-sensitive);
- a date after `dob`, `d.o.b.` or `date of birth` → `[dob]`;
- an SSN after `ssn`, `social security` or `soc sec` → `[ssn]`;
- a phone number after `phone`, `ph`, `cell`, `tel`, `call`, `contact` or `rp` (keyword included), or any
  `(999) 999-9999` → `[phone]`;
- a licence number (`A9999999`) after `dl`, `cdl`, `dln`, `cadl` or `driver's license` → `[dl]`;
- an address: a house number of 2–6 digits (optional letter) standing alone (at the start, or after a space, `,`,
  `;`, `:`, `(` or `@`), then 1–3 capitalized words or ordinals (`3rd`), then a street suffix (any case). Only the
  number becomes `[addr]`; the street name stays. Lower-case prose is left alone.

Keywords match in any case except in the name rule. Before the rules run, code sections (`459 PC`, `10851 VC`,
`11350 HS`; also `HSC`, `WI`, `WIC`, `BP`) and a number right after a case or report keyword (`case`, `report`,
`incident`, `cad`, `event`, `file`, `citation`, `booking`, `warrant`, `dr`, `no`, `ref`, `id` and similar) are
marked with the private-use character U+E000, which is removed at the end, so they are never read as house numbers.
Released text is otherwise untouched, including a genuine `§`. Only a U+E000 already present in the released text
would also be dropped.

```sql
SELECT scrub_civilian('DOB 01/02/1990 call 555-555-0100 re 459 PC at 100 Main St'),
       scrub_civilian('SSN 000-00-0000 dl: X0000000 11350 HS'), scrub_civilian('PC §459 case 12345 Main St'),
       scrub_civilian('12345 stolen vehicle from court'), scrub_civilian('DOE, JOHN dob 01/01/1980');
```
```text
('DOB [dob] [phone] re 459 PC at [addr] Main St', 'SSN [ssn] dl [dl] 11350 HS', 'PC §459 case 12345 Main St', '12345 stolen vehicle from court', '[name] dob [dob]')
```

## `audit_client.py`

Importable, read-only Python helpers for row and event lookups. They select truth rows through constant `IN`-lists
(one pruned scan per release) and parse and cite them with the `sql_templates.py` SQL, so the result is exactly what
`sightings` and `sighting_sources` return for those rows, without parsing the rest of the database.

| Function | Returns |
|---|---|
| `connect(audit_dir, threads=4, memory="4GB", temp_dir=None, max_temp="8GiB")` | A read-only connection to `derived.duckdb` with `truth` attached. Spills go to `temp_dir` (default `<system tmp>/alpr_duck_tmp`), capped at `max_temp`, so a runaway query fails instead of filling the disk |
| `sightings_for(con, pairs)` | A DuckDB relation with the `sightings` columns for `[(release_id, row_no), …]`, ordered by `(release_id, row_no)`; rows not in truth are absent. Local only (released text) |
| `citations_for(con, pairs)` | A relation with the `sighting_sources` columns for the same pairs |
| `sightings_sql(pairs)`, `citations_sql(pairs)` | The SQL those two run, to embed in your own query |
| `event(con, eid)` | A dict with the `events` columns plus `sightings`, a sorted list of `(release_id, row_no, producer, audit, basis)`; None for an unknown id. Scans the integer `event_key` and compares `event_id` in Python |
| `drill(con, eid, surfaces=False)` | One dict per sighting of the event, ordered by producer, release, row: `producer`, `audit`, `basis`, `release_id`, `row_no`, `public_release_id`, `src_row`, `org`, `t`, `nets`, the four `*_state`s, `citation`, `open_url`, `sha256`. `surfaces=True` adds `reason_surface` and `case_surface` (local only). Same rows as `event_sightings(eid)` |

From the shell, `python audit_client.py <event_id> [audit_dir]` prints the states and citations of one search (1
thread, 1 GB). Event ids other than `u:` (`k5:`, `k3:`, `x:`) are build-specific: persist `(release_id, row_no)`
instead ([linking.md](linking.md)).

```python
import sys; sys.path.insert(0, "<code>")
import audit_client as ac
con = ac.connect()
for r in ac.drill(con, "u:aa84e168-9c45-48f6-b11a-0b670f8724f9")[:3]:
    print(r["producer"], r["basis"], r["src_row"], r["reason_state"])
```
```text
Alameda County CA SO 2_k5 2 not_exported
Cathedral City CA PD 2_k5 2 value
Contra Costa County CA SO 2_k5 147558 redacted_agency
```

## Layer 3: the cache schema

The cache holds event linking only. Linking has to see every sighting at once, which takes minutes, so it is
computed at build time and stored. Everything in it can be recomputed from `truth`. The tiers, their measured
precision and how to cite a linked search are in [linking.md](linking.md); counts per tier are in
[stats.md](stats.md) ("Link tiers").

### `cache.sighting_keys`

One row per sighting that can be linked: it has a Flock search UUID, or both a search time and an org.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `sighting_id` | UBIGINT | The sighting | as in `sightings` |
| `release_id` | VARCHAR | The release | |
| `row_no` | BIGINT | Row position in the release | |
| `producer` | VARCHAR | Flock organization whose log this is | |
| `audit` | VARCHAR | `network` or `own` | |
| `flock_id` | VARCHAR | Flock search UUID, if released | |
| `has_tf` | BOOLEAN | The row has a parsed time frame | `tf_start IS NOT NULL` |
| `k3` | UBIGINT | Match key on (org, time, networks) | `hash(org, t, nets)` when `t` and `org` are both present |
| `k5` | UBIGINT | Match key that adds the time frame | `hash(org, t, nets, tf_start, tf_end)` when `tf_start` is also present |

Sightings with no UUID and no parsed time or org have no row here and never get an `event_id`; their count is the
last line of stats.md's "Link tiers" table.

### `cache.sighting_event`

One row per linkable sighting, with the search it was linked to.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `sighting_id` | UBIGINT | The sighting | |
| `release_id` | VARCHAR | The release | |
| `row_no` | BIGINT | Row position in the release | |
| `producer` | VARCHAR | Flock organization whose log this is | |
| `audit` | VARCHAR | `network` or `own` | |
| `event_id` | VARCHAR | The linked search | `u:<Flock search UUID>` (tiers 1–3b), `k5:<k5>` (tiers 4–5), `k3:<k3>` (tier 6), `x:<sighting_id>` (ambiguous: a group of its own) |
| `basis` | VARCHAR | How the sighting was linked | the tier labels under [Enumerations](#enumerations) |
| `event_key` | UBIGINT | `hash(event_id)` | what `read_field`, `event` and `event_sightings` look events up by |

Linking is deterministic. Every lookup counts the distinct candidates for a key and uses one only when there is
exactly one, taking it with `min(…)`: `min(flock_id)` in the key-to-UUID lookups (`k3u`, `k3n`, `k5u`) and `min(k5)`
in the stage-2 lookup from a `k3` key to its single `k5` group (`k3g`). No step uses `any_value`. To confirm on a new
build, compare `bit_xor(hash(sighting_id, event_id, basis))` over `cache.sighting_event` between two builds (a full
scan; run it niced).

To join the cache to parsed rows quickly, filter both sides by `release_id`:

```sql
SELECT s.row_no, s.org, s.t, se.event_id, se.basis
FROM sightings s JOIN (SELECT * FROM cache.sighting_event WHERE release_id = 'mr:196397:PRA25-746.csv#csv') se USING (sighting_id)
WHERE s.release_id = 'mr:196397:PRA25-746.csv#csv' ORDER BY s.row_no;
```
```text
(1, 'Miami-Dade FL SO', 2025-01-23 15:26:37, 'u:aa84e168-9c45-48f6-b11a-0b670f8724f9', '2_k5')
(2, 'Palos Heights IL PD', 2025-01-28 13:29:20, 'u:d46e138d-517a-4f9f-9ae8-632989c6c998', '2_k5')
(3, 'Marshall County AL SO', 2025-01-31 14:26:06, 'u:b76f83ba-3649-4820-855e-57ec6d62ea9a', '2_k5')
```

This join is also a quick check that `sighting_id` in the views and in the cache agree: it should return every
linkable row of the release.

### `cache.builds`

One row per cache table per full build. Rows accumulate until `derived.duckdb` is deleted; `check_cache.py` reads the
latest row per table.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `name` | VARCHAR | Cache table | `sighting_keys` or `sighting_event` |
| `built_at` | TIMESTAMP | When it was built | UTC, stored without a zone, like `truth.build_info.built_at_utc` |
| `truth_fingerprint` | VARCHAR | Fingerprint of the `truth` it was built from | md5; see [Staleness](#staleness) |
| `code_sha` | VARCHAR | Fingerprint of the linking code and the views and macros it reads | 12 hex digits |
| `duckdb_version` | VARCHAR | DuckDB `version()` at build time, e.g. `v1.5.5` | NULL in rows written before the column existed |

### Staleness

`cache_fingerprint.py` defines what the cache was computed from. `build_derived.py` records it in `cache.builds`;
`check_cache.py` recomputes it and compares.

- **Truth fingerprint (`truth_fingerprint`):** md5 over
  - every `truth.releases` row's `release_id`, `n_rows`, `container_sha256`, `member_sha256`, `content_sha256`,
    `producer`, `audit`, `header`, `pra_id`, `container_path`, `member` and `sheet`, in `release_id` order;
  - every row of `truth.release_layouts` (`release_pattern`, `src_row_from`, `src_row_to`, `mapping`) and of
    `truth.release_dispositions` (`release_pattern`, `field`, `disposition`), without their `source` citations;
  - the parsed SMPD rows: `release_id`, `row_no`, `id`, `count_time_line` and `user_line` of every
    `truth.smpd_pdf_rows` row. These are the loader's reading of the PDFs, which a loader or pymupdf change can move
    while the PDFs' hashes stay the same.

  NULL is kept distinct from an empty string, and a column an older `truth` lacks is skipped (so the fingerprint
  differs, which reads as stale). It changes when a release is added, dropped, re-counted, re-hashed, re-attributed
  (`producers.json`), re-headed or renamed, when `layouts.json` or `dispositions.json` changes, and when the SMPD
  loader reads a block differently.
- **Code fingerprint (`code_sha`):** the first 12 hex digits of SHA-256 over the layer-3 section of
  `build_derived.py` (the text between the `# ---------------- Layer 3` and `# ---------------- Layer 2 again`
  markers), which builds `cache.sighting_keys` and `cache.sighting_event`, plus the stored SQL of everything that
  section reads: the views `release_meta`, `flock_rows`, `sightings_flock`, `sightings_smpd` and `sightings`, and the
  macros `flock_ts`, `tf_bound`, `flock_int`, `cell_state`, `agency_mask`, `exemption_cite`, `clean_value` and
  `is_withheld`, as `duckdb_views()` and `duckdb_functions()` return them. The build reconnects before fingerprinting
  so that it sees the same normalized SQL `check_cache.py` sees. Because the views are generated from
  `sql_templates.py`, a template edit shows here as soon as a build (full or `--views-only`) has stored it. Edits to
  other views (`sightings_public`, `sighting_sources`, `event_log`, …) or to `read_field`, `event`, `event_sightings`
  and `events` do not make the cache stale, because the cache does not read them.
- **DuckDB version (`duckdb_version`):** `sighting_id`, `k3`, `k5` and `event_key` are DuckDB `hash()` values, which
  DuckDB does not promise to keep across versions. A cache built under another version is stale.

Run the check before relying on `events`, `event`, `read_field` or `event_sightings`. It reads `build_derived.py` from
its own directory, and the databases from `--audit-dir` (default: `<audit_db>`). It runs at 1 thread and 1 GB and
reads only catalogs and the small truth tables (`truth.smpd_pdf_rows` is the largest).

```sh
nice -n 19 taskpolicy -b uv run --project <code> python <code>/check_cache.py [--audit-dir DIR]
```
```text
cache.sighting_keys: built 2026-09-26 20:42 UTC — current
cache.sighting_event: built 2026-09-26 20:42 UTC — current
```

For each cache table it takes the latest `cache.builds` row and prints `current`, or any of
`truth changed since the cache was built`, `linking code or the views it reads changed` and
`built with DuckDB <version>, this is <version>`.
It exits 0 when everything is current and 1 when anything is stale (or `cache.builds` is missing); on exit 1, rebuild
derived in full.

What the check cannot see: a change in how a CSV or NDJSON file's text is split into cells that leaves the file bytes,
the header and the row count unchanged. Their `content_sha256` hashes bytes, not parsed cells (a spreadsheet's hashes
the staged values, so it does see such a change). After changing the CSV or NDJSON reader, rebuild derived in full
whatever `check_cache.py` says.

## Timings

Measured on 2026-09-26 in the standard session (`threads=4`, `memory_limit='4GB'`, read-only, DuckDB 1.5.5) on a
10-core Apple Silicon laptop with 24 GB RAM and a warm OS file cache. The machine was not idle: other sessions' niced
jobs (`nice -n 19`, `taskpolicy -b`) and desktop apps kept the load average near 7. Each call ran twice; the range
covers both runs.

| Call | Time | Why |
|---|---:|---|
| `release_sources`, all 907 rows (any release-level view) | < 0.01 s | one row per release |
| `sightings WHERE release_id = …` (3-row release, all rows) | 0.01–0.02 s | prunes to one release |
| `sightings`, `count(*)` of a 177,851-row release | 0.04 s | prunes |
| `sightings_public WHERE public_release_id = …`, `count(*)` (3-row release) | 0.02 s | prunes through the join to `release_sources`; reads the key once |
| `sighting_sources` one row by `(release_id, row_no)`, Flock or SMPD | ≤ 0.01 s | prunes |
| `sighting_sources WHERE sighting_id = …` | 0.08–0.09 s | hashes the key columns of every row but parses no cells |
| `cache.sighting_event WHERE release_id = …` | 0.02 s | |
| `event(eid)` (13 sightings) | 0.02–0.07 s | integer `event_key` scan |
| `event_sightings(eid)` (13 sightings) | 0.95–1.31 s | finds the event by `event_key`, parses only its rows |
| `read_field('reason', who := 'Cathedral City CA PD')` (3 sightings, 3 events) | 1.13–1.44 s | parses only the rows of that producer's events |
| `sql_templates.py`, 2 rows (example above) | 0.02 s | literal `release_id` and `row_no` filters |
| `audit_client.sightings_for`, 2 rows (Flock + SMPD) | 0.02 s | one pruned scan per release |
| `audit_client.event(con, eid)` | 0.02–0.04 s | integer `event_key` scan |
| `audit_client.drill(con, eid)` (13 sightings) | 0.09 s | `event_key` scan, then pruned lookups |
| `check_cache.py` (including `uv run` start-up) | 0.18–0.28 s | catalogs and small truth tables |

Not measured, because each parses or groups all ~140 million rows: `sightings WHERE sighting_id = …` (or any filter on
a computed column), `read_field` without `who`, and an unfiltered `events`. Avoid them.

## Enumerations

| Value | Where | Meaning |
|---|---|---|
| `value` | `*_state` (from `cell_state`) | A usable value; `clean_value` returns it |
| `empty` | `*_state` | Field exported, cell blank |
| `not_exported` | `*_state` | Field not in this release's header, or mapped to no cell by a layout correction |
| `withheld` | `*_state` | Field or cell withheld per an authored disposition |
| `redacted_flock` | `*_state` | Flock's own `***` mask |
| `redacted_agency` | `*_state` | An agency's mask (`REDACTED`, `###`, `* * *`, block glyphs) or an exemption citation typed in the cell (`7923.600 GC`) |
| `partial` | `*_state` | Partly masked (`REDACTED` followed by text), or initials only (names at Redwood City) |
| `placeholder` | `*_state` | Junk entry (`n/a`, `test`, punctuation only, …) |
| `no_other_record` | `divergence` | No other log released a value for this search |
| `masked_here_released_elsewhere` | `divergence` | Masked here, a value released in another log or production |
| `blank_here_present_elsewhere` | `divergence` | Blank or not exported here, present elsewhere |
| `placeholder_here` | `divergence` | Junk entry here, a value elsewhere |
| `same` | `divergence` | Same value as the other logs' consensus |
| `differs_from_other_logs` | `divergence` | A different value from the other logs' consensus |
| `1_uuid`, `2_k5`, `3_k3`, `3b_k3_to_tf_less_uuid`, `4_k5_group`, `5_k3_to_group`, `6_k3_group`, `x_ambiguous` | `cache.sighting_event.basis`, `events.weakest_link`, `event_sightings.basis` | Link tiers, strongest first ([linking.md](linking.md)) |
| `released`, `producer (own-search log without an Org Name column)`, `producer (SMPD PDF export has no org field)` | `sightings.org_basis` | How `org` was set |

What each state means for a story, and which masks come from which agency, is in [semantics.md](semantics.md).
