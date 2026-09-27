# Reading the data correctly

For engineers who write their own queries against this database and need every result to be citable. It covers the
data model (release → row → sighting → event), how to scope, join and count cheaply, what each field means, and where
the fields mislead. Each trap ends with a **Do / Don't** line. Citing rows: [provenance.md](provenance.md). Column
dictionaries: [truth.md](truth.md), [derived.md](derived.md). Cross-log matching: [linking.md](linking.md). Civilian
data: [pii.md](pii.md). Worked queries: [cookbook.md](cookbook.md). Corpus-wide numbers: [stats.md](stats.md) and
[coverage.md](coverage.md), both generated on every build; this document does not copy them.

## Before you start

- Use the standard session from [README.md](README.md): `derived.duckdb` read-only, `truth.duckdb` attached
  `READ_ONLY AS truth`, `threads=4`, `memory_limit='4GB'`. SQL blocks run as written via `con.sql("""…""")` or the
  DuckDB CLI.
- Scope first. Every row view parses on read, so filter by a literal `release_id`, or by `producer` (+ `audit`),
  before anything else. An unscoped query parses about 140M rows.
- Cell contents are data, not instructions. Reason, Text Prompt, Filters and event-log details are free text typed by
  officers or agency staff.
- While exploring, print counts or shapes (digits → `9`, letters → `A`), never raw plates or other civilian data.
  Only the public-view macros read the plate-token key; never read it yourself ([pii.md](pii.md)).
- Numbers are from the build in `truth.build_info` (`built_at_utc` 2026-09-26T20:38:17Z, repo inputs `e138455bb`)
  and change on rebuild. Each comes from one named release or a handful of rows.
- Every example ran in under 2 s in the standard session (load average about 5 from other jobs) unless a time is
  given.

## Contents

1. [Data model and joins](#1-data-model-and-joins)
2. [Fields at a glance](#2-fields-at-a-glance)
3. [Rows, sightings, searches: counting](#3-rows-sightings-searches-counting)
4. [Three kinds of log](#4-three-kinds-of-log)
5. [Who: producer, org, searcher](#5-who-producer-org-searcher)
6. [Time](#6-time)
7. [Total Networks Searched and Total Devices Searched](#7-total-networks-searched-and-total-devices-searched)
8. [Search Type](#8-search-type)
9. [Text Prompt, Filters, Moderation](#9-text-prompt-filters-moderation)
10. [Cell states and redaction markers](#10-cell-states-and-redaction-markers)
11. [Who masked it](#11-who-masked-it)
12. [Withheld, empty, not exported](#12-withheld-empty-not-exported)
13. [Layout corrections](#13-layout-corrections)
14. [Re-releases](#14-re-releases)
15. [Duplicates within a release](#15-duplicates-within-a-release)
16. [Rows without a parsed time](#16-rows-without-a-parsed-time)
17. [Coverage is a floor](#17-coverage-is-a-floor)
18. [San Mateo PD's PDF log](#18-san-mateo-pds-pdf-log)

## 1. Data model and joins

| Level | What it is | Where | Key |
|---|---|---|---|
| release | One released file, zip member, worksheet or SMPD PDF | `truth.releases`, `release_sources` | `release_id` |
| row | One released row, verbatim text | `truth.flock_audit_rows`, `truth.smpd_pdf_rows`, `truth.flock_event_rows` | (`release_id`, `row_no`) |
| sighting | A search row, parsed: one log's record of one search | `sightings` (= `sightings_flock` ∪ `sightings_smpd`); `sightings_public` for export | (`release_id`, `row_no`); `sighting_id` = `hash(release_id, row_no)` |
| event | One search, linked across logs and re-releases | `cache.sighting_event` (sighting → `event_id`, `basis`); `events`; `event(eid)` | `event_id`; `event_key` = `hash(event_id)` |
| citation | Where the row sits in the released original | `sighting_sources`; `event_log.citation` | (`release_id`, `row_no`) |

Three kinds of log, in the `audit` column (§4):

| `audit` | One row is | Watch for |
|---|---|---|
| `network` | A search, by any organization, that included the producer's cameras | Contains the producer's own searches too (`org = producer`) |
| `own` | A search by one of the producer's users, whatever it covered (the "own-search log") | SMPD's PDFs are this kind (§18) |
| `event` | An administrative action (users, sharing, hotlists) | Not a search: rows go to `event_log`, never to `sightings`. `user` is the acting account; `entity_details` names the account or object acted on |

`release_id` forms: `mr:<request>:<file>[!<zip member path>]#<sheet, or csv>`, `rwc:<release>`,
`la:<request>:<workbook>__<sheet>`, `smpd:<request folder>:<pdf name>`. Query with it. Publish `public_release_id`
instead (`release_sources`, `sighting_sources`, `event_log`, `sightings_public`): the same id without zip folder
names, unique per build.

`row_no` is load order within the release (1-based). `src_row` is the row a reader sees in the original
(spreadsheet row, or CSV record with the header as row 1); `release_sources.src_row_basis` states the rule per
release. SMPD rows have no `src_row` and are cited by PDF page (§18).

One search, many rows. A `Los Angeles CA PD` search at 2025-05-01 09:11:16 UTC over 541 networks is 15 sightings in
15 releases from 8 producers' network audits. LAPD's own logs are not in the database, so none of the 15 is the
searcher's record:

```sql
SELECT producer, audit, basis, count(*) AS sightings, count(DISTINCT release_id) AS releases
FROM cache.sighting_event
WHERE event_key = hash('u:6023db79-9334-4b07-8b35-7f6e18366971')
GROUP BY ALL ORDER BY producer;
-- Los Altos CA PD     network 2_k5   2 2  | Port Hueneme CA PD  network 1_uuid 4 4
-- Redwood City CA PD  network 2_k5   1 1  | San Bruno CA PD     network 1_uuid 1 1
-- San Jose CA PD      network 2_k5   2 2  | Sonoma County CA SO network 3_k3   3 3
-- Ukiah CA PD         network 1_uuid 1 1  | Ukiah Fire CA FD    network 1_uuid 1 1
```

Parse one of them and cite it in the same query:

```sql
SELECT s.producer, s.org, s.t, s.nets, s.reason_state, s.name_state, ss.citation
FROM sightings s JOIN sighting_sources ss USING (release_id, row_no)
WHERE s.release_id = 'mr:212287:5_1_2025-5_31_2025-Ukiah_CA_PD-Network-Audit.csv#csv' AND s.row_no = 219700;
-- Ukiah CA PD | Los Angeles CA PD | 2025-05-01 09:11:16 | 541 | value | redacted_flock |
-- 'Ukiah CA PD. MuckRock request 212287 (…), produced 2026-06-23. 5_1_2025-5_31_2025-Ukiah_CA_PD-Network-Audit.csv,
--  row 219701. Download: https://cdn.muckrock.com/… (SHA-256 a00f3874…)'
```

**Joins.**

| From | To | Join on | Notes |
|---|---|---|---|
| any row view | `truth.releases`, `release_sources` | `release_id` | producer, `audit`, `released_on`, `header`, URLs, hashes |
| `sightings` | `sighting_sources` | `release_id`, `row_no` | filter by `release_id` on at least one side |
| `sightings` | `cache.sighting_event` | `release_id`, `row_no` | filter the cache by `producer` (+ `audit`) or a literal `release_id` |
| `cache.sighting_event` | the same search in other logs | `event_key` | `event_key = hash('<event_id>')`, or `event_key IN (SELECT event_key …)` |
| `sightings` | `flock_rows` | `release_id`, `row_no` | for `Moderation` and the raw `Search Time`; neither is in `sightings` |
| `sightings_smpd` | `truth.smpd_pdf_rows` | `release_id`, `row_no` | the printed lines and `parse_note` (§18) |
| `truth.releases` | `release_fields` | `release_id` | header / withheld flags for 7 fields only (`ID`, `Name`, `Org Name`, `License Plate`, `Reason`, `Case #`, `Time Frame`); for any other column use `list_contains(header, 'Search Type')` |

**What is fast and what is not** (standard session):

| Pattern | Time | Why |
|---|---:|---|
| Any row view with `release_id = '…'` (El Cerrito's 438,530-row network audit, aggregated) | 0.1–0.4 s | prunes to one release |
| `cache.sighting_event` with `producer = '…' AND audit = 'own'` | < 0.1 s | |
| A row view with `release_id IN (SELECT release_id FROM truth.releases WHERE producer = …)` | 0.2 s | |
| `event('u:…')` | < 0.1 s | the stored `event_key`, then the id |
| `cache.sighting_event WHERE event_key = hash('u:…')` | 0.6 s | integer compare over one column |
| `cache.sighting_event WHERE event_id = 'u:…'` | 1.6 s | string compare over every row |
| `event_key IN (SELECT event_key FROM cache.sighting_event WHERE release_id = '…' AND row_no IN (…))` | 0.2 s | one pass for a batch of searches |
| `event_sightings('u:…')` (15 sightings, parsed and cited) | 3 s | finds the rows by `event_key`, parses only those |

Filtering a row view by `sighting_id` alone hashes every row. `events` groups the whole cache on read: filter it by
`event_id`, or use `event(eid)`.

`sighting_id`, `event_key` and the linking keys `k3`/`k5` are DuckDB `hash()` values: stable within one build and
DuckDB version (`cache.builds.duckdb_version`), not identifiers to publish.

**Do:** carry (`release_id`, `row_no`) through every step so each output row can be cited; scope by `release_id` or
`producer` first; publish `public_release_id`. **Don't:** publish or persist `sighting_id` or `event_key`, filter by
`sighting_id` alone, or query `events` unfiltered.

## 2. Fields at a glance

The parsed search row is `sightings`. How each column is computed is in [derived.md](derived.md); this table is about
what each one means.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `sighting_id` | UBIGINT | One released row | Not a search (§3) |
| `release_id` | VARCHAR | The released file, sheet or PDF | See `truth.releases` |
| `row_no` | BIGINT | Load order within the release | Cite `src_row`, not `row_no` |
| `src_row` | BIGINT | Row number a reader sees in the original | NULL for SMPD (cite the page, §18) |
| `producer` | VARCHAR | Whose log this is: the Flock organization that exported it | §5 |
| `audit` | VARCHAR | `network` or `own` | Event logs are in `event_log` (§4) |
| `org` | VARCHAR | Organization that ran the search | §5 |
| `org_basis` | VARCHAR | Where `org` came from | §5 |
| `t` | TIMESTAMP | When the search was run, UTC, stored without a zone | §6 |
| `tf_start`, `tf_end` | TIMESTAMP | The window of camera reads the user asked to search, UTC | §6.3 |
| `nets` | INTEGER | Total Networks Searched (SMPD: `networkCount`) | §7 |
| `devices` | INTEGER | Total Devices Searched | §7; NULL for SMPD |
| `flock_id` | VARCHAR | Flock's search UUID (`ID`; SMPD: the printed id) | Same search, same UUID in every log that exports it; NULL when blank or `***` |
| `search_type` | VARCHAR | Flock's Search Type label | §8 |
| `reason_surface`, `reason_state`, `reason` | VARCHAR | Reason as released; its cell state; the trimmed value only when the state is `value` | §10 |
| `case_surface`, `case_state`, `case_no` | VARCHAR | Case # as released; state; value | §10 |
| `name_surface`, `name_state` | VARCHAR | Searcher's account name as released (SMPD: the `userID` line); state | §5, §10 |
| `plate_surface`, `plate_state` | VARCHAR | License Plate searched, as released; state | Civilian PII ([pii.md](pii.md)) |
| `text_prompt` | VARCHAR | Free-text prompt of a `freeform` search | §9 |
| `filters` | VARCHAR | Filters and search terms of the search | §9 |
| `layout_corrected` | BOOLEAN | Fields read from other header labels (`truth.release_layouts`) | §13 |

`*_surface`, `text_prompt` and `filters` are the released text, untrimmed: Marin County's logs have Reason cells
`' REDACTED'` with a leading space. Blank cells can arrive as `''` or NULL depending on the source format. Compare with
`nullif(trim(x), '')`, or use the `*_state` columns.

## 3. Rows, sightings, searches: counting

A released row is a **sighting**: one log's record of a search. One search appears in the searcher's own-search log,
in the network audit of every producer whose cameras it touched, again in every re-release, and sometimes twice in
one release (§15, §18).

| You want | Count | Caveat |
|---|---|---|
| Rows in a production | `count(*)` over its releases; call them rows | |
| Searches in one log | `count(DISTINCT event_id)` over that producer's `cache.sighting_event` rows for the log | See `x_ambiguous` below |
| Searches across logs | `count(DISTINCT event_id)`, tiers stated ([linking.md](linking.md)) | |
| Searches by an organization | rows with that `org` (spellings checked, §5), de-duplicated by `event_id` | Only in the logs we hold (§17) |

Santa Rosa's own-search log, per Pacific month (two productions, 2026-08-20 and 2026-09-01):

```sql
WITH s AS (SELECT release_id, row_no, t FROM sightings
           WHERE release_id IN (SELECT release_id FROM truth.releases WHERE producer = 'Santa Rosa CA PD' AND audit = 'own')),
se AS (SELECT release_id, row_no, event_id FROM cache.sighting_event WHERE producer = 'Santa Rosa CA PD' AND audit = 'own')
SELECT strftime(timezone('America/Los_Angeles', timezone('UTC', s.t)), '%Y-%m') AS pacific_month,
       count(*) AS sightings, count(DISTINCT s.release_id) AS releases, count(DISTINCT se.event_id) AS searches
FROM s LEFT JOIN se USING (release_id, row_no)
GROUP BY 1 ORDER BY 1;
-- 2025-01  6,272 1 6,272 | 2025-02  5,227 1 5,146 | … | 2025-07 10,518 2 5,259 | … | 2026-07 9,444 2 4,722
```

From July 2025 each month was produced twice, so rows are double the searches. February 2025 has 81 repeated rows in
a single release.

Three properties of the event id to know before counting with it:

- An `x_ambiguous` sighting gets its own event (`x:` + its `sighting_id`), so re-released copies of it count as
  separate searches. Report how many `x_ambiguous` sightings a count includes, or exclude them.
- Sightings without a UUID whose `org`, `t`, `nets` and Time Frame are identical share one event (tiers `4_k5_group`,
  `5_k3_to_group`, `6_k3_group`). This de-duplicates repeated rows, and would also merge two genuine searches that
  look identical in the exported columns.
- `events.n_logs` is `count(DISTINCT producer)`, not a count of logs: a search in a producer's own-search log and its
  network audit counts once. `events.n_sightings` counts re-releases and repeats.

**Do:** say "rows" or "sightings" for `count(*)`, and "searches" only after de-duplicating by `event_id` (with the
tiers named). **Don't:** add rows across producers, logs, productions or adjacent monthly files and call the total
searches.

## 4. Three kinds of log

The `audit` column of `truth.releases` (and of `flock_rows`, `sightings` and `cache.sighting_event`) says which kind a
release is. Counts per kind: `SELECT audit, count(*) FROM truth.releases GROUP BY 1`.

| `audit` | Flock's name | One row is | Who searched | Rows live in |
|---|---|---|---|---|
| `network` | Network Audit | A search that included the producer's cameras | Any organization, **including the producer** (`Org Name`) | `truth.flock_audit_rows` → `flock_rows` → `sightings_flock` |
| `own` | Organization Audit ("own-search log") | A search by one of the producer's users, whatever cameras it covered | The producer | Same; SMPD's: `truth.smpd_pdf_rows` → `sightings_smpd` (§18) |
| `event` | Event Logs | An administrative action | Not a search | `truth.flock_event_rows` → `event_log` |

### Event logs

| Column | Meaning |
|---|---|
| `user` | The account that **performed** the action: an account label, occasionally an e-mail address |
| `entity_type` | What was acted on: `Custom Hotlist Entry`, `customHotlist`, `networkShare`, `networkShareSettings`, `role`, `user`, `stream`, `location`, `organization`, `export`, `integration`, `accountContact` |
| `event_type` | `create`, `update` or `delete` |
| `entity_details` | The thing acted on, as `Label: value` lines. For `entity_type = 'user'` it names the account **affected** |
| `ts` | Event time, UTC (§6.1) |

Label sets by entity type, checked by shape in Arcadia's log (values not printed):

```sql
SELECT entity_type, regexp_extract_all(entity_details, '(?m)^([A-Za-z ]+):') AS labels, count(*) AS n
FROM event_log WHERE producer = 'Arcadia CA PD'
GROUP BY ALL ORDER BY n DESC;
-- Custom Hotlist Entry [Hotlist Name:, License:, State:, Reason:, Case:, Expiry:, Consolidated:] 2,028
-- role                 [Name:]                                                                  1,390
-- Custom Hotlist Entry [Hotlist Name:, Deleted Plates:]                                         1,376
-- …
-- networkShare         [Network Name:, Permissions:, Receiving Organization:]                     800
-- user                 [Name:]                                                                    122
-- user                 [Message:, Reset User:, User Email:]                                        14   …
```

So "who was deleted" is in `entity_details`, "who deleted" is in `user`. Flock does not document these formats, and
they can differ by producer: check the label shapes per producer before parsing. `user` and `user`-entity details hold
police-employee names; hotlist details hold civilian plates ([pii.md](pii.md)).

**Do:** read the actor from `user` and the target from `entity_details`. **Don't:** read `user` on a `user`-entity row
as the account created or deleted, or count event rows as searches.

### A network audit contains the producer's own searches

An own search usually includes the producer's own cameras, so it appears in the producer's network audit too. Two
measurements, restricted to the time span both logs cover:

| Producer | Own-search rows in span | Also in its network audit | How matched |
|---|---:|---:|---|
| El Cerrito CA PD | 743 | 734 | linked events (`org` + second + networks; neither export has UUIDs) |
| Cotati CA PD | 18 | 18 | Flock UUID, same second |

```sql
-- El Cerrito
WITH span AS (SELECT min(t) AS t0, max(t) AS t1 FROM sightings
              WHERE release_id = 'mr:199390:12_16_2025-1_15_2026-El_Cerrito_CA_PD-Network-Audit.csv#csv'),
own AS (SELECT s.release_id, s.row_no FROM sightings s, span
        WHERE s.release_id = 'mr:199390:12_15_2025-1_15_2026-El_Cerrito_CA_PD-Audit.csv#csv' AND s.t BETWEEN span.t0 AND span.t1),
se AS (SELECT release_id, row_no, audit, event_key FROM cache.sighting_event WHERE producer = 'El Cerrito CA PD'),
net AS (SELECT DISTINCT event_key FROM se WHERE audit = 'network')
SELECT count(*) AS own_in_span, count(net.event_key) AS also_in_network_audit
FROM own JOIN se USING (release_id, row_no) LEFT JOIN net USING (event_key);
-- 743 | 734
```

Per-producer shares for every producer with both logs, restricted to each network release's time span, are in
stats.md ("Own-search log vs network audit"). Consequences:

- One search by the producer can be counted in its own-search log, in its network audit, and in other producers'
  network audits.
- Other organizations' searches of the producer's cameras appear only in network audits.
- An own search missing from the network audit either did not cover the producer's cameras or was not linked
  ([linking.md](linking.md)). The data alone does not say which.

**Do:** name the log a count comes from, and count across logs by event. **Don't:** add a producer's own-search rows
to its network-audit rows, or read a network audit as "the producer's searches".

## 5. Who: producer, org, searcher

**`producer`** is the organization whose log the file is, spelled as in the agency registry's `flock_names` (for
example `San Mateo CA PD`). `producer_agency_id` is that registry entry's UUID. Neither is a slug.
`truth.releases.producer_basis` records how the producer was decided (counts:
`SELECT producer_basis, count(*) FROM truth.releases GROUP BY 1`):

| `producer_basis` | Meaning |
|---|---|
| `filename` | Parsed from a file name like `<dates>-<Org Name>-Network-Audit` |
| `own-search dominant Org Name` | Commonest `Org Name` in the same request's own-search files |
| `producers.json` | Authored per MuckRock request, or per file; the evidence is in `producer_source` |
| `fixed (dedicated loader)` | Redwood City and Los Altos repo NDJSON |
| `fixed (SMPD PRA)` | San Mateo PD's PDFs |

**The Denver sample in Pasadena's production.** MuckRock 188086 (Pasadena PD) includes
`SAMPLES/Denver_ALPR_Network_Searches_1.xlsx`: exactly 1,000,000 rows, 3,458 searching orgs nationwide, no
`Pasadena CA PD` row (the 956 Pasadena rows are `Pasadena TX PD`). A per-file entry in `producers.json` attributes it to
`Denver Police Department`, so coverage.md lists it under Denver. The evidence is circumstantial (`producer_source`):
the file name, Colorado orgs over-represented, `Denver CO PD` the 4th-largest searcher; its search times also run from
06:02 UTC on 2024-06-01 to 05:59 UTC on 2024-10-01, midnight to midnight in Mountain daylight time, where California
productions start at 07:00 UTC (§6.2). It is a processed extract, not a native export: 6 columns, no `ID` (so it links
only on `org` + `t` + `nets`: tiers `3_k3`, `5_k3_to_group`, `6_k3_group` or `x_ambiguous`), no Time Frame, and Reason
empty in 999,435 rows.

**Do:** describe it as "a file in Pasadena PD's production, attributed to Denver PD's network from its contents".
**Don't:** count it as Pasadena's log, or as a verified Denver PD export.

**`org`** is the organization that ran the search. `org_basis` says where it came from:

| `org_basis` | Meaning |
|---|---|
| `released` | The row's own `Org Name` cell (trimmed) |
| `producer (own-search log without an Org Name column)` | An `own` release with no `Org Name` column (for example Lodi's): every row is the producer's by definition |
| `producer (SMPD PDF export has no org field)` | San Mateo PD's PDFs (§18) |
| NULL | `Org Name` blank in a release that has the column, for example Los Altos's non-search rows (§16) |

`org` is spelled as the export spelled it, which can differ from `producer`:

```sql
SELECT producer, org, org = producer AS same_spelling, count(*) AS n
FROM sightings
WHERE release_id = 'mr:197815:12_1_2024-12_31_2024-Mountain_View_CA_PD_Santa_Clara_County-Network-Audit_4.csv#csv'
  AND org ILIKE '%mountain view%'
GROUP BY ALL;
-- Mountain View Police Department | Mountain View CA PD (Santa Clara County) | false | 956
```

A repeated header row inside `rwc:PRA_26_217_4th_Release_Dec2023` (`src_row` 16485) has `org` = `Org Name`, `t`
NULL and `search_type` = `Search Type`.

**The searcher** is `name_surface` (`searcher_name` in `sightings_public`): the Flock account label as exported, or
SMPD's `userID` line. It is an account, not a verified person: one person can hold several accounts, and a label can
recur across agencies. Police employee names stay verbatim ([pii.md](pii.md)). Most Flock rows mask it (share by
state in stats.md, "Cell states by field").

**Do:** name the producer when citing a count ("in Redwood City's network audit"), and check `org` spellings with
`ILIKE` before comparing orgs with producers. **Don't:** treat `org = producer` as the complete set of a producer's
own searches, or an account name as a person without other evidence.

## 6. Time

### 6.1 Search time: `t`

`t` is when the search was run: a naive `TIMESTAMP` that means **UTC**.

| Source | Released cell (digits as 9) | Parsed by |
|---|---|---|
| Flock export text | `99/99/9999, 99:99:99 PM UTC` (month and day can be unpadded, as in Lodi's and the Denver sample's `9/9/9999, …`) | `flock_ts`: strips `UTC`, `%m/%d/%Y, %I:%M:%S %p` |
| San Jose's split sheets (10 releases whose header has `Search Date`) | `Search Date` `9999-99-99` (kept in `extra`) + `Search Time` `99:99:99` | `flock_ts` on the two joined (`%Y-%m-%d %H:%M:%S`) |
| SMPD PDFs (`truth.smpd_pdf_rows.count_time_line`) | `999 99/99/9999, 99:99:99 AM UTC`: network count and search time printed on one line | `flock_ts` on the time part |
| Event logs `Timestamp` (`event_log.ts`) | ISO 8601 with `Z`; San Joaquin County 49 rows with `+00:00`; Riverside County's 2,031 rows with no zone | `flock_ts`, else `iso_ts_utc` (offsets converted); Riverside's read as UTC, unverified |

Evidence that `t` is UTC:

- Flock's text carries the literal `UTC`: every `Search Time` and `Time Frame` cell of El Cerrito's network audit
  (438,530 rows) ends in `UTC`, as does every SMPD time line.
- The same search has the same second in every format. The LAPD search in §1 has `t` = 2025-05-01 09:11:16 in San
  Jose's split sheet (no zone label) and in San Bruno's, Ukiah's, Los Altos's and Port Hueneme's `… UTC` text.
- All 2,604 `San Mateo CA PD` rows in San Bruno's March 2026 network audit match an SMPD PDF row by UUID, with the same
  second and the same network count (query below).
- Productions begin at Pacific midnight expressed in UTC: SMPD's April 2026 PDF starts 2026-04-01 07:00:11 UTC, and
  Port Hueneme's April 2025 network audit 2025-04-01 07:00:05 UTC (§6.2).

```sql
-- UTC -> Pacific, DST-aware
SELECT min(t) AS first_utc, timezone('America/Los_Angeles', timezone('UTC', min(t))) AS first_pacific
FROM sightings WHERE release_id = 'smpd:W012818-053026:4_1_2026-4_30_2026-San_Mateo_CA_PD-Audit__1_.pdf';
-- 2026-04-01 07:00:11 | 2026-04-01 00:00:11
```

```sql
-- SMPD's PDFs vs San Bruno's network audit, March 2026 Pacific
WITH sm AS (SELECT flock_id, t, nets FROM sightings_smpd
            WHERE t >= TIMESTAMP '2026-03-01 08:00' AND t < TIMESTAMP '2026-04-01 07:00'),
     sb AS (SELECT flock_id, t, nets FROM sightings
            WHERE release_id = 'mr:205259:OneDrive_2026-04-28.zip!Network Audit/3_1_2026-3_31_2026-San Bruno CA PD-Network-Audit.csv#csv'
              AND org = 'San Mateo CA PD')
SELECT (SELECT count(*) FROM sb) AS in_san_bruno, count(*) AS matched_by_uuid,
       count(*) FILTER (WHERE sb.t = sm.t AND sb.nets = sm.nets) AS same_second_and_nets
FROM sb JOIN sm USING (flock_id);
-- 2,604 | 2,604 | 2,604
```

**Do:** convert to Pacific before stating a local date or hour, and bucket by Pacific date when matching an agency's
monthly files. **Don't:** read `t` as local time, or bucket by UTC month and call it the agency's month (up to 8 hours
of each month land in the next).

### 6.2 File periods and file names

Monthly files are Pacific-day ranges, and a file named "M-1 to M+1-1" can include the first day of the next month:

```sql
-- Port Hueneme, 2026-09-21 production: April and May 2025 network audits
WITH apr AS (SELECT flock_id, t FROM sightings WHERE release_id = 'mr:207641:PRR_26-54_-_Response_9.18.26-20260921T072017Z-1-001.zip!PRR 26-54 - Response 9.18.26/4-1-2025 to 5-1-2025-Port Hueneme CA PD-Network-Audit .xlsx#4_1_2025-5_1_2025-Port Hueneme '),
     may AS (SELECT flock_id, t FROM sightings WHERE release_id = 'mr:207641:PRR_26-54_-_Response_9.18.26-20260921T072017Z-1-001.zip!PRR 26-54 - Response 9.18.26/5-1-2025 to 6-1-2025-Port Hueneme CA PD-Network-Audit.xlsx#5_1_2025-6_1_2025-Port Hueneme ')
SELECT (SELECT min(t) FROM apr) AS apr_first, (SELECT max(t) FROM apr) AS apr_last,
       count(DISTINCT flock_id) AS in_both, min(t) AS first_shared, max(t) AS last_shared
FROM apr SEMI JOIN may USING (flock_id);
-- 2025-04-01 07:00:05 | 2025-05-02 06:59:48 | 18,744 | 2025-05-01 07:00:22 | 2025-05-02 06:59:48
```

The April file runs to the end of May 1 Pacific, so 18,744 searches are in both files. Santa Rosa's own-search
files, by contrast, do not overlap (§3). File names can also be wrong: the 2026-07-17 Port Hueneme member named
`9-1-2026 to 10-1-2026-…` holds September 2025 rows (its sheet is `9_1_2025-10_1_2025-…`), and Ukiah Fire's
`5_13_2022-6_13_2022-…` file starts on 2022-06-03. A non-California file follows its own zone's days (the Denver
sample, §5).

**Do:** derive periods from `t` (in the producer's local time), and de-duplicate across adjacent files by event.
**Don't:** trust a file name's dates, or sum adjacent monthly files.

### 6.3 Time Frame: `tf_start`, `tf_end`

`Time Frame` holds a start and an end: two lines (`<start> UTC` newline `<end> UTC`), or Lodi's one line
(`<start> UTC to <end> UTC`). `tf_bound` splits on either. It is the window of camera reads the user asked to search:
not the time of the search, and not what came back.

- **Missing.** Some exports have no Time Frame column: El Cerrito's own-search log, the Denver sample (§5), SMPD's
  PDFs. `tf_start` and `tf_end` are NULL there.
- **The window can end after the search.** In El Cerrito's network audit, 205,474 of 438,530 windows end after `t`:
  196,820 within an hour, 176 more than a day later. 123 start after `t`, and 6 end before they start.
- **Lookups ask for longer windows than searches:**

```sql
SELECT search_type, count(*) AS n,
       median(date_diff('hour', tf_start, tf_end)) AS median_window_h,
       count(*) FILTER (WHERE tf_end > t) AS window_ends_after_search
FROM sightings
WHERE release_id = 'mr:199390:12_16_2025-1_15_2026-El_Cerrito_CA_PD-Network-Audit.csv#csv'
GROUP BY 1 ORDER BY n DESC;
-- lookup 378,973 168 h 188,564 | search 57,945 6 h 16,856 | convoy 1,513 54 h 7 | freeform 86 48 h 46 | multiGeo 13 5 h 1
```

**Do:** describe `tf_start`–`tf_end` as "the period the user asked to search". **Don't:** use it as the search
time, as evidence of what was retrieved, or as proof that data past a retention limit was accessed without checking
`t` and the quirks above.

## 7. Total Networks Searched and Total Devices Searched

- **`nets`** (`flock_int` of `Total Networks Searched`; SMPD's printed `networkCount`) is how many Flock
  organizations' camera networks the search covered. A network-audit row exists because the producer's network was one
  of them. It says nothing about how many of the producer's cameras were read, how many plates came back, or whether
  anything matched. Typical values are in the hundreds: medians in El Cerrito's network audit are 631 (`lookup`) and
  623 (`search`), maximum 3,494; in San Francisco's January 2026 network audit 632 (`lookup`) against 5 (`visual`,
  `freeform`). SMPD's `networkCount` equals San Bruno's `Total Networks Searched` on all 2,604 matched rows (§6.1).
- **`nets` = 0 does not mean zero.** Every row of Ukiah Fire's network audits with `t` from June to November 2022 has
  `nets` = 0 (all by other organizations), although each of those searches covered at least Ukiah Fire's network.
  The zeros thin out from December 2022 and are rare after April 2023.
- **`devices`** (`Total Devices Searched`) reads as a camera count across the networks searched where it is filled
  (El Cerrito's `lookup` median 15,737). Some exports fill it with 0: Truckee's January 15–31, 2025 network audit has
  `devices` = 0 in 240,329 of 240,337 rows. SMPD's PDFs have no such column (`devices` NULL).

**Do:** use `nets` as a measure of how widely a search reached. **Don't:** call `nets` a number of cameras, hits or
results, read 0 literally, or compare `devices` across producers without checking that the export fills it.

## 8. Search Type

`search_type` is Flock's label, trimmed; NULL or `''` where the export has no `Search Type` column (SMPD's PDFs) or the
cell is blank. Labels in San Francisco's January 2026 logs: `lookup`, `lookup - Mobile`,
`search`, `search - Mobile`, `convoy`, `visual`, `freeform`, `freeform - Mobile`, `multiGeo`,
`searchSummary - Mobile`, and in the own-search log `apiLookup`. The ` - Mobile` suffix appears to mark searches from
Flock's mobile app; the export does not define it. Non-label values also occur, for example the text `Search Type` in
Redwood City's repeated header row (§5); `sightings_public` exports `search_type` only when it looks like a label. The
full list with counts is in stats.md ("Search types"); counts there are rows, not searches.

"National lookups": Port Hueneme's 2026-07-17 production is titled "Flock Audit - National Lookups Response", yet its
network sheets have the same row counts as the full 2026-09-21 production and contain every search type (September
2025: `lookup` 318,939, `search` 46,580, `convoy` 1,447, `freeform` 5, `multiGeo` 1). The title does not describe a
filter. `lookup` rows reach hundreds of networks (§7).

**Do:** report search types as Flock's labels, quoted, and fold ` - Mobile` only on purpose. **Don't:** infer what a
search type did from its name alone, or compare search-type mixes across producers whose exports lack the column.

## 9. Text Prompt, Filters, Moderation

- **`text_prompt`**: the natural-language prompt of a `freeform` search. In San Francisco's January 2026 network
  audit, all 316 `freeform` and `freeform - Mobile` rows have one and no other row does. It is typed by an officer and
  can hold civilian details; `sightings_public` tokenizes plates and scrubs other identifiers in it ([pii.md](pii.md)).
- **`Moderation`** (`flock_rows."Moderation"`; not in `sightings`): Flock's verdict on a freeform prompt. San
  Francisco, January 2026: `allow` 312, `block` 3, `warn` 1; El Cerrito: `allow` on all 86 `freeform` rows.
- **`filters`**: the vehicle attributes the searcher filtered on (state, colour, body type, make and similar) and any
  plate search terms. In El Cerrito's network audit 109,905 rows hold `***`, 342 hold text and the rest are blank. The
  text has no fixed grammar:
  - comma-separated in some exports (El Cerrito: shapes like `Aaaaaaaa, aaaaa`);
  - run together with no separator in others (California Highway Patrol's own-search logs: `suvgmcblack`);
  - a plate glued onto the preceding attribute, because Flock concatenates vehicle attributes onto the plate:
    `california` or `Chevrolet` + a `9AAA999` plate (shape `aaaaaaaaaa9aaa999`), seen in San Jose's and CHP's
    own-search logs. `sightings_public` tokenizes a `9AAA999` glued onto any letters (`filter_plate_part`);
  - `*` inside a longer value is a wildcard in a partial plate (`*AAA999`, `9***999`, `****999` in El Cerrito's
    network audit), not a mask. The public view does not tokenize these fragments: `AAA999` is not a tokenized shape.
- None of the three gets a cell state (§10). A mask is stored raw: Santa Rosa's own-search log has `###` in both
  `Text Prompt` and `Filters` on every row of its March 2025 release.

**Do:** treat `filters` and `text_prompt` as what the searcher asked for; exclude whole-cell `***` / `###` before
counting "searches with filters"; look for plates inside and at the end of letter runs. **Don't:** split Filters on
commas or word boundaries alone, read `*` inside a value as a mask, read a blank `filters` as "no filter applied" where
the release masks or omits the column, or quote a prompt without checking it for civilian PII.

## 10. Cell states and redaction markers

Four fields get a state: Reason, Case #, Name and License Plate (`reason_state`, `case_state`, `name_state`,
`plate_state`). The macro `cell_state(raw, in_header, withheld, partial_ok, placeholder_ok)` takes the first rule that
matches, on the trimmed cell (so `' REDACTED'` and `'REDACTED '` classify like `REDACTED`); `clean_value` returns the
trimmed text only for `value`, which is what `reason` and `case_no` hold.

| State | Rule, in order | What it supports in a claim |
|---|---|---|
| `withheld` | Field not in the header (or no cell, §13), or cell blank, **and** an authored `withheld_blanked` disposition covers the field (`truth.release_dispositions`) | The agency says it withheld this field (§12) |
| `not_exported` | Field not in the release's `header`, or a layout correction maps it to no cell (§13) | The release has no such column in these rows. For `rwc:` releases, also a column the workbook left blank in every row (§12) |
| `empty` | Cell blank | Blank as released. Not proof it was blank in Flock |
| `redacted_flock` | Cell is exactly `***` | Masked with the `***` marker. Who applied it varies (§11) |
| `redacted_agency` | `REDACTED` or `[REDACTED]` (any case); the whole cell `##`+, `* *`+ or block glyphs; or the whole cell one or more exemption citations (`exemption_cite`: `7923.600 GC`, `GC 7923.600`, `Gov. Code § 7923.600(a)`, `Civ. Code 1798.90.55`, `GC 6254(f)`) | Masked by the agency |
| `partial` | `REDACTED` / `[REDACTED]` followed by text; for Redwood City names only, an initial of either case and a short fragment (`[A-Za-z]?\. ?` + 1–3 letters or apostrophes) | Part masked, part released |
| `placeholder` | Reason and Case # only: `none`, `n/a`, `na`, `-`, `--`, `xxx`, `x`, `*`, `.`, `0`, `test`, `null` (any case), or no letter or digit at all | Not a real reason or case number |
| `value` | Anything else | Released text. **Not** a check that it is genuine (below) |

Name and License Plate have no `placeholder` state, so a lone `#` or `-` in those fields reads `value`.

Markers seen, each checked in the named release (corpus counts by field: stats.md, "Cell states by field" and
"Redaction marker strings"):

| Marker (released) | State | Where checked |
|---|---|---|
| `***` | `redacted_flock` | El Cerrito network audit (Case #, plate); Port Hueneme network audits (Name, plate); Cotati own-search log (all four fields); San Bruno own-search June 2024 (Reason, plate); SMPD `userID` (107,691 of 110,705 rows) |
| `REDACTED` | `redacted_agency` | San Jose network audits (Reason, plate, Name); Marin own-search logs (Reason, plate); NCRIC network audit (Name); Los Altos (Name, plate) |
| `[REDACTED]` | `redacted_agency` | Contra Costa network audit (Reason, plate) |
| `###` | `redacted_agency` | Santa Rosa own-search log (Case #, Name, plate) |
| `* * *` | `redacted_agency` | San Bruno own-search January 2024 and January 2025 (Reason, Case #, plate); its other months use `***` |
| `7923.600 GC` | `redacted_agency` | Port Hueneme: every Reason in the 13 network audits of the 2026-07-17 production; License Plate cells in its own-search logs; 3 Case # cells |
| `REDACTED` + text: `REDACTED, …`, `REDACTED …`, `REDACTED / …`, `REDACTED; …`, `REDACTED/…` and more | `partial` | Marin County Reason; San Jose Name (31 rows in the May–June 2025 sheet) |
| Initial + fragment, shapes `A. Aaa`, `. Aaa`, `A. Aa`, `A. A'A`, and lower-case initials (`a. Aaa`, 169 rows in `rwc:PRA_26_217_2025_1`) | `partial` | Redwood City Name |

Census for one release:

```sql
SELECT f AS field, st AS state, CASE WHEN st IN ('redacted_flock', 'redacted_agency') THEN v END AS marker, count(*) AS n
FROM (SELECT unnest(['reason', 'case', 'name', 'plate']) AS f,
             unnest([reason_surface, case_surface, name_surface, plate_surface]) AS v,
             unnest([reason_state, case_state, name_state, plate_state]) AS st
      FROM sightings WHERE release_id = 'mr:196392:CCCSO_Network_Audit_1_17_2025_2_1_2025_REDACTED_File_1_of_2.xlsb#Network Audit 1.17.25-2.1.25')
GROUP BY ALL ORDER BY 1, n DESC;
-- case empty 185,935 | case value 45 | name value 185,980 | plate redacted_agency [REDACTED] 185,980 | reason redacted_agency [REDACTED] 185,980
```

An exemption citation typed in place of the value is the agency withholding it. Port Hueneme produced September 2025
twice; the first production cites the exemption in every Reason cell, the second releases the reasons:

```sql
SELECT r.released_on, s.reason_state, s.reason_surface = '7923.600 GC' AS exemption_citation, count(*) AS n
FROM sightings s JOIN truth.releases r USING (release_id)
WHERE r.producer = 'Port Hueneme CA PD' AND r.audit = 'network' AND r.sheet LIKE '9_1_2025-10_1_2025%'
GROUP BY ALL ORDER BY 1, n DESC;
-- 2026-07-17 redacted_agency true 366,972 | 2026-09-21 value false 362,224 | empty NULL 4,144 | placeholder false 604
```

**`value` does not mean genuine.** Every Reason cell in both Riverside County releases of MuckRock 210264 (own-search
log, 55,000 rows; network audit, 554,620 rows including other organizations' searches) is `Investigation`, state
`value`. The same Riverside searches carry other reasons in other producers' logs (linked by event):

```sql
-- Riverside's own searches as recorded in Ukiah PD's network audits
WITH rv AS (SELECT DISTINCT event_key FROM cache.sighting_event
            WHERE producer = 'Riverside County CA SO' AND audit = 'own'),
uk AS (SELECT c.release_id, c.row_no FROM cache.sighting_event c SEMI JOIN rv USING (event_key)
       WHERE c.producer = 'Ukiah CA PD' AND c.audit = 'network')
SELECT count(*) AS sightings, count(*) FILTER (WHERE s.reason_state = 'value') AS reason_value,
       count(*) FILTER (WHERE s.reason = 'Investigation') AS investigation, count(DISTINCT s.reason) AS distinct_reasons
FROM sightings s SEMI JOIN uk USING (release_id, row_no)
WHERE s.release_id IN (SELECT release_id FROM truth.releases WHERE producer = 'Ukiah CA PD' AND audit = 'network');
-- 37,979 | 37,979 | 0 | 2,983
```

`value` can also be civilian data in the wrong field: 662 Case # values in CHP's October 2025 own-search release have
the California plate shape `9AAA999`.

**Do:** filter on the state (`reason_state = 'value'`), read `reason` / `case_no`, and look at the distinct values
(and at the same searches in other logs) before counting "searches with a reason". **Don't:** count non-blank surfaces
as disclosed values, treat `empty`, `not_exported` and the masked states as the same thing, or quote a column that
holds one value in every row as what searchers entered.

## 11. Who masked it

`redacted_flock` names the marker (`***`), not the actor. Compare the producer's own rows (`org = producer`) with
other organizations' rows in the same network audit:

| Release | Producer's own rows | Other organizations' rows |
|---|---|---|
| El Cerrito network audit (2026-01-15) | 743 rows: plate and Case # `value` or blank | 437,787 rows: plate and Case # `***` or blank, never `value` |
| Port Hueneme September 2025 network audit (both productions) | 72 rows in each production: Name `value`, plate `value` or blank | Name `***` on every row; plate `***` or blank |
| Ukiah February 2025 network audit | 470 rows: Name `***` | Name `***` |
| Cotati own-search log | all 19 rows `***` in Reason, Case #, Name, plate | — |

So `***` appears both where only other organizations' fields are masked (consistent with Flock's export masking
other agencies' details) and on the producer's own rows, which only the agency or an export setting can explain.
Reason is usually left readable in network audits (El Cerrito: `value` in 438,179 of 438,530 rows).

```sql
-- El Cerrito: producer's own searches vs everyone else's
SELECT org = producer AS producers_own_search, plate_state, case_state, count(*) AS n
FROM sightings
WHERE release_id = 'mr:199390:12_16_2025-1_15_2026-El_Cerrito_CA_PD-Network-Audit.csv#csv'
GROUP BY ALL ORDER BY 1, n DESC;
```

**Do:** write "masked (`***`)" or "redacted by the agency (`REDACTED`)", and name the release. **Don't:** write
"Flock redacted" from `redacted_flock` alone, or compare masking rates across producers or productions without
checking each release (§14).

## 12. Withheld, empty, not exported

- **`withheld`**: an authored fact, from a cover letter, that the agency withheld the field
  (`truth.release_dispositions`, loaded from `dispositions.json` with its `source`; applied through `is_withheld`,
  `release_meta` and `release_fields`). Only `withheld_blanked` dispositions feed `cell_state`. Today there is one:
  Redwood City's PRA 26-217 / 26-741 cover letter withholds Reason, covering all 28 Redwood City releases.
- **`empty`**: the column exists and the cell is blank, with no disposition. Nothing entered, or a redaction that left
  no marker.
- **`not_exported`**: the release has no such column, or a layout correction says no cell holds the field in those
  rows (§13).
- **Redwood City exception.** `rwc:` releases come from committed NDJSON conversions that omit empty cells, and no
  header row was recorded, so `header` is the set of keys that appear in at least one row (`header_basis` says so).
  A column the workbook had but left blank in every row is therefore absent and reads `not_exported`, not `empty`: for
  example `License Plate`, `Reason` and `Case #` in `rwc:PRA_26_217_2024_Q3`. Los Altos (`la:`) headers come from the
  recorded header row and are not affected.

```sql
SELECT field, in_header, withheld, count(*) AS releases
FROM release_fields
WHERE producer = 'Redwood City CA PD' AND field IN ('Reason', 'License Plate')
GROUP BY ALL ORDER BY 1, 2;
-- License Plate false false 15 | License Plate true false 13 | Reason false true 23 | Reason true true 5
```

A disposition does not erase what survived: in `rwc:PRA_26_217_2025_1`, which has a Reason column, 192,111 Reason
cells are `value`, 1,244 blank cells are `withheld` and 237 are `placeholder`. Redwood City's second disposition,
License Plate `redacted`, is not a `withheld_blanked` disposition, so its plate columns that are absent or blank
throughout read `not_exported`, and blank plate cells `empty`, although the cover letter says plates were removed.

**Do:** cite the disposition's `source` when you say an agency withheld a field; say "blank as released" for
`empty`; check the workbook's header row before saying Redwood City did not export a column. **Don't:** describe
`empty` or `not_exported` as withholding, or as the searcher leaving a field blank, unless a document says so.

## 13. Layout corrections

Some releases put values under the wrong header labels. `truth.release_layouts` (from `layouts.json`, each entry with
its evidence in `source`) maps, for a range of `src_row` or a list of single rows, each field to the label whose cell
holds it. `flock_rows` reads each field from that label and sets `layout_corrected`; truth is unchanged, so the
original cell is under the label named in the mapping. A field mapped to null has no cell in those rows: its header
flag is cleared, so its state is `not_exported`.

| Release | Rows | What is shifted | `layout_corrected` rows |
|---|---|---|---:|
| Santa Rosa May 2026 network audit, both productions | `src_row` ≥ 92527 | Two exports stacked with no second header | 455,584 in each |
| Santa Rosa February 2026 network audit, both productions | 19 listed rows | The Moderation verdict is under `Search Time`; no cell holds the time, so `t` stays NULL | 19 in each |
| San Bruno April 2024 own-search log | `src_row` ≥ 2 | From Filters on, every cell one to the right | 1,840 (all now have `t`) |
| Cathedral City `PRA25-746.csv` | `src_row` ≥ 2 | Data rows lack the Case # cell | 3 |

```sql
SELECT release_id, layout_corrected, case_state, count(*) AS n, min(src_row) AS first_row, max(src_row) AS last_row
FROM sightings WHERE release_id = 'mr:196397:PRA25-746.csv#csv' GROUP BY ALL;
-- mr:196397:PRA25-746.csv#csv | true | not_exported | 3 | 2 | 7
```

Cathedral City's header lists `Case #`, but no data cell holds it, so `case_state` is `not_exported`.
`sighting_sources.citation` flags corrected rows ("cells released under other column labels").

**Do:** say "as corrected for a column shift (see `truth.release_layouts`)" when quoting a corrected row, and cite
the original cell under the label the mapping names. **Don't:** quote a corrected release's cells by their printed
labels.

## 14. Re-releases

Every release is stored in full; nothing is collapsed. Agencies re-produce the same period, sometimes byte for byte
and sometimes with different masking or row counts.

- **`release_content_groups`** groups releases with the same `content_sha256`. That is the SHA-256 of the file bytes
  for a CSV, a hash of the non-empty data rows' values, in order, for a spreadsheet, the SHA-256 of the decompressed
  committed NDJSON for Redwood City and Los Altos, and the PDF's SHA-256 for SMPD. It is order-sensitive: Ukiah
  Fire's two `7_13_2023-8_12_2023` CSVs (one named `(1)`) hold the same 11,385 rows in a different order and do not
  group. The same data as CSV and as XLSX never groups. Los Altos's January–July 2025 network audits group across PRAs
  25-312 and 26-366, and so do its own-search logs for the same months except May.
- **Same period, different content.** San Jose's May–June 2025 network sheet, produced twice:

```sql
SELECT r.released_on, count(*) AS n,
       count(*) FILTER (WHERE s.name_state = 'value') AS searcher_name_shown,
       count(*) FILTER (WHERE s.name_state = 'redacted_agency') AS searcher_name_redacted
FROM sightings s JOIN truth.releases r USING (release_id)
WHERE s.release_id IN ('mr:187612:Attachment_3-_Network_Audit_June_2024-June_2025_Redacted.xlsx#May 2025- Jun 2025',
                       'mr:202333:Attachment_-_Network_Audit_March_2025-Aug_2025.xlsx#May 2025- Jun 2025')
GROUP BY 1 ORDER BY 1;
-- 2025-09-15 700,601 695,748 4,822 | 2026-02-27 700,601 30,174 670,427
```

- Port Hueneme's 2026-07-17 and 2026-09-21 productions have equal row counts sheet for sheet, yet none of those
  pairs groups, because the July copy replaces every Reason with an exemption citation (§10). Its only group is the
  July and August redacted copies of February 2026. March 2026 differs by a row between August and September (456,850
  against 456,851).
- Los Altos's August 2025 network audit is partial in 25-312 (35,070 rows) and full in 26-366 (362,205).

Which copy to cite: [provenance.md](provenance.md), "Re-releases: which copy to cite".

**Do:** count searches by event, or pick one release per producer, log and period and say which; when a later
production unmasks what an earlier one masked, say so. **Don't:** sum `n_rows` or `count(*)` across releases, or
assume `release_content_groups` finds every duplicate production.

## 15. Duplicates within a release

One release can contain the same row several times, identical in every column:

```sql
SELECT count(*) AS n_rows, count(DISTINCT flock_id) AS distinct_search_ids
FROM sightings
WHERE release_id = 'mr:214821:Records_Request_Download_PS-359-2026_2026-07-09--22-17-20.zip!2_1_2025-2_28_2025-Rohnert Park Department of Public Safety (CA)-Network-Audit.csv#csv';
-- 213,160 | 201,153
```

There, 3,023 UUIDs repeat (up to 5 copies), and the release has exactly 201,153 distinct full rows, so every repeat
is an exact copy. Santa Rosa's February 2025 own-search release has 5,227 rows for 5,146 UUIDs; three SMPD PDFs print
some searches more than once (§18). Where a release has no `ID` column, a repeat cannot be told from two searches that look
the same in the exported columns: Redwood City's `rwc:PRA_26_217_2025_7` has 465,690 rows but 393,891 distinct full
rows, and at least one repeated pair (rows 331960 and 442682) links to a single UUID in other logs. Linking puts
identical UUID-less rows in one event (§3).

**Do:** count `DISTINCT flock_id` or events, and state the rule. **Don't:** report `count(*)` of a release as a
number of searches.

## 16. Rows without a parsed time

A few rows have `t` NULL; [coverage.md](coverage.md) lists them by producer. What they are:

- Los Altos: rows with `REDACTED` in Name and License Plate and every other cell blank (10 in the May 2025 network
  release in 25-312), so `org`, `t` and `nets` are NULL. They are not searches, have no `cache.sighting_event` row,
  and never count as events.
- Santa Rosa's February 2026 network audit: 19 `freeform` rows in each production whose `Search Time` cell holds the
  Moderation verdict (`allow` 16, `block` 3) and no cell holds the time (§13). coverage.md reports these as "blank
  cell" because it reads the corrected field; the released cell is not blank.
- Redwood City's repeated header row (§5).

**Do:** exclude rows with `t` NULL from time-based counts and say how many you dropped. **Don't:** count them as
searches.

## 17. Coverage is a floor

The database holds only logs that have been released and loaded ([coverage.md](coverage.md)). Absence here is not
evidence that a search did not happen.

- **Only producers we hold.** A network audit shows only searches that touched that producer's cameras. A search of
  an agency whose logs we don't hold is invisible unless it also touched one we do.
- **Not yet produced or loaded is not omitted.** SMPD's log has rows for every Pacific month from January 2023 to May
  2026 except July–September 2023, June–September 2024 and June–September 2025. June 2024 *was* produced, as two
  image-only PDFs: releases with `n_rows` 0 whose `header_basis` says so (OCR not loaded). The other months have no
  PDF in the committed productions (W012541, W012818). June 2023 and May 2025 were produced and are loaded (the repo's
  merged JSON lacks them, §18). None of this says whether searches happened.

```sql
SELECT strftime(timezone('America/Los_Angeles', timezone('UTC', t)), '%Y-%m') AS pacific_month,
       count(*) AS n_rows, count(DISTINCT flock_id) AS searches
FROM sightings_smpd GROUP BY 1 ORDER BY 1;
SELECT release_id, header_basis FROM truth.releases WHERE release_id LIKE 'smpd:%' AND n_rows = 0;
```

- **Partial files.** Los Altos's August 2025 file in PRA 25-312 is partial (§14). Some file names do not match their
  contents (§6.2).
- **Round row counts** can signal an export cap, though none is verified: the Denver sample (1,000,000 rows),
  Riverside County's own-search log (55,000), `rwc:PRA_26_217_4th_Release_Dec2023` (20,000).

**Do:** write "at least N searches in the logs released to date" and name the logs and periods. **Don't:** write "no
searches", "never" or "only N" from an absence here, or call a period "missing" or "omitted" when it has not been
produced or loaded.

## 18. San Mateo PD's PDF log

SMPD produced its own-search log (PRAs W012541 and W012818) as Flock search-audit exports printed to PDF: five
printed columns `ID`, `userID`, `networkCount`, `Search Time`, `Reason` (`releases.header_raw`, as printed;
`header` uses Flock's names `ID`, `Name`, `Total Networks Searched`, `Search Time`, `Reason`). The PDF is the
original, and this database reads it directly (`smpd_pdf_loader.py`, pymupdf text layer).

- **One release per PDF**: `smpd:<request folder>:<pdf name>`. January 2025 and January 2026 are each split across
  two PDFs (Part 1, Part 2); no search id appears in two PDFs. The two June 2024 PDFs are image-only (§17).
- **One row per search-id block as printed** (`truth.smpd_pdf_rows`): `id`, `user_line`, `count_time_line` (network
  count and time on one line), `reason_line` (NULL when the Reason cell is blank), all verbatim; `src_page` and
  `src_line` (1-based) locate the id on the page. No block is dropped. Cite by page:
  `sighting_sources.locator` = `page N (search id …)`; `src_row` is NULL.
- **Parsed fields** (`sightings_smpd`): `org` = producer; `t` and `nets` from `count_time_line`; `name_surface` =
  `user_line`; `reason_surface` = `reason_line`. Case # and License Plate are `not_exported`; Time Frame, devices,
  Search Type, Text Prompt and Filters are NULL. Every row links by UUID (`1_uuid`).
- **A search printed twice is two sightings.** Three PDFs repeat some blocks (up to 4 copies), identical and on
  adjacent rows. Count distinct `flock_id` or `event_id`:

```sql
SELECT release_id, count(*) AS n_rows, count(DISTINCT flock_id) AS searches
FROM sightings_smpd GROUP BY 1 HAVING count(*) <> count(DISTINCT flock_id) ORDER BY 1;
-- …12_1_2024-12_31_2024-San_Mateo_CA_PD-Audit.pdf 4,026 4,025 | …1_1_2023-1_31_2023-San_Mateo_CA_PD-Audit2.pdf 2,829 2,751
-- …2_1_2025-2_28_2025-San_Mateo_CA_PD-Audit.pdf 6,235 6,135
```

- **`parse_note` rows.** Some pages had Reason cells edited in Acrobat before production (see the
  `smpd_pdf_loader.py` docstring), and the re-save moved those pages' text out of printed order. Where text order and the printed row disagree, the loader reads each cell by its
  position in the id's printed row, and `parse_note` names the page line it took each cell from (69 rows on 7 pages
  of 7 PDFs in this build). Check these against the page before quoting them:

```sql
SELECT regexp_extract(release_id, '[^:]+$') AS pdf, src_page, count(*) AS rows_read_by_position
FROM truth.smpd_pdf_rows WHERE parse_note IS NOT NULL GROUP BY ALL ORDER BY 1;
```

- **Not the repo's merged JSON.** `assets/transparency.flocksafety.com/san-mateo-ca-pd/pra-W012541-041426.json`,
  the repo's earlier parse of the same request, was built from 28 of W012541's 32 PDFs (not June 2023, May 2025 or
  the image-only June 2024 pair). On the Acrobat-edited pages it holds 35 reasons that differ from the printed row
  (33 are the next row's search id, 2 another row's reason on the same page), and it lacks 33 printed rows. Counts
  from this database and from that JSON differ for these reasons.

**Do:** count SMPD searches as `count(DISTINCT flock_id)`, cite the PDF page, and check `parse_note` rows on the page.
**Don't:** count SMPD rows as searches, or reconcile against the merged JSON without saying which source a number
comes from.
