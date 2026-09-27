# Cookbook: worked queries

For engineers who write their own queries against this database and need every result to be citable. Fifteen
research questions, each answered with code that was run against this build, the output it gave (aggregates or
non-PII values), what it cost, where the answer can mislead, and how to cite every row behind it. Field meanings and
traps are in [semantics.md](semantics.md), citing a row in [provenance.md](provenance.md), cross-log matching in
[linking.md](linking.md), civilian data in [pii.md](pii.md), column dictionaries in [truth.md](truth.md) and
[derived.md](derived.md). Corpus-wide numbers (link tiers, cell states, per-producer spans, rows without a time) are
generated into [stats.md](stats.md) and [coverage.md](coverage.md); this document does not repeat them.

## Before you start

```python
import sys
sys.path.insert(0, "<code>")            # scripts/audit_db in a checkout of the repo (see README.md)
import audit_client as ac
con = ac.connect()                      # <audit_db>/derived.duckdb read-only, truth attached READ_ONLY, 4 threads / 4 GB, spill capped
```

- This is the [README.md](README.md) session plus `audit_client`. SQL blocks run as written with `con.sql("""…""")`
  (a block with several statements runs them all and returns the last result) or in the DuckDB CLI; Python blocks
  use `con` and `ac`.
- Run a recipe's blocks in order in one session: later blocks read the temp tables and variables earlier ones create.
  `CREATE TEMP TABLE` and `SET VARIABLE` work on a read-only connection. Every temp table and variable here starts
  with `cb_`, so none collides with [linking.md](linking.md)'s.
- Run `check_cache.py` first: every recipe that counts searches reads the linking cache ([derived.md](derived.md),
  Staleness).
- Numbers are from the build in `truth.build_info` (`built_at_utc` 2026-09-26T20:38:17Z, repo inputs `e138455bb`,
  DuckDB 1.5.5) and the cache built 2026-09-26 20:42 UTC. They change on rebuild.
- Timings: one or two runs each on 2026-09-26, `threads=4`, `memory_limit='4GB'`, under `nice -n 19 taskpolicy -b`, with other
  sessions querying the same files (load average 5–7). Expect variation of a few times either way.
- Outputs show aggregates, agency names, Flock search UUIDs, file names, row numbers, page numbers and citations.
  Reason and Case # text appears only as shapes (digit → `9`, letter → `A`) or comparison labels. Plate tokens are
  never printed.

## Patterns

**Cost follows the rows in scope, not the size of the answer.** `sightings`, `sightings_public`, `flock_rows` and
`sighting_sources` are views: every row in scope is parsed on each read.

| Fast | Measured |
|---|---|
| A view filtered by `release_id = '<literal>'` | one 770,583-row release, `count(DISTINCT org)` 0.3 s; with `t` parsed 0.5 s |
| A set of releases: `SET VARIABLE cb_rels = (SELECT list(release_id) FROM truth.releases WHERE …)`, then `list_contains(getvariable('cb_rels'), release_id)` on **every** view and on `cache.sighting_event` | 4 releases, 595,114 rows parsed and joined to the cache: 1.2 s; both scans return exactly the in-scope rows |
| `producer = '…'` (+ `audit`) on a view or on the cache | all 3,847,369 Mountain View network rows with `t`: 1.8 s; San Francisco's 491,343 own-log cache rows: 0.1 s |
| One raw column over every row of `truth.flock_audit_rows` (not parsed; pre-correction) | `trim("Org Name") = 'Cotati CA PD'`: 0.6 s |
| `ac.sightings_for(con, pairs)` / `ac.citations_for(con, pairs)` on `(release_id, row_no)` pairs from any number of releases | 48,834 rows from 25 releases parsed in 3.7 s; 3,391 citations in 0.3 s |
| One search: `cache.sighting_event WHERE event_key = hash('u:…')`; `ac.event(con, eid)`; `ac.drill(con, eid)` | 0.3 s; 0.4 s; 1.0 s (15 sightings, parsed and cited) |

| Slow or wrong | Instead |
|---|---|
| `release_id IN (SELECT …)` in a query that joins a view: planned as a semi join **after** the parse (profile: the truth scan returned 3,509,498 rows for 595,114 in scope) | `list_contains(getvariable(…), release_id)`; `IN (SELECT …)` is fine in a single-view or cache-only query |
| A condition on a computed column (`t`, `org`, `*_state`, `reason`, `plate`) as the only filter | it is evaluated on every row in scope and never narrows the scan: add a release filter |
| Joining a list of rows to the full `sightings` / `sighting_sources` view | `ac.sightings_for` / `ac.citations_for` (literal per-release IN-lists) |
| `cache.sighting_event WHERE event_id = '…'` (a string compare on every cache row, 4.0 s) | `event_key = hash('…')`, or the `event(eid)` macro |
| `event_sightings('u:…')`: 11.6 s for 15 sightings; `read_field(fld, who := …)`: 7.7 s for a 3-sighting producer | `ac.drill` (Recipe 8); a pairwise comparison (Recipe 5) |
| `events` without an `event_id`/`event_key` filter; any `GROUP BY`, `DISTINCT` or `ORDER BY` over all of `cache.sighting_event` | spills tens of GB. Scope the cache by `release_id` or `producer` first; corpus-wide counts are in [stats.md](stats.md) |

**The default shape of a recipe:**

1. Narrow cheaply: a raw truth column (Recipe 2), a producer or release list (Recipes 3–5), or the cache
   (`event_key`, Recipe 8). Carry `(release_id, row_no)`; that is the database key.
2. Parse only what matched: a view with a release filter when the scope is a few releases, `ac.sightings_for` when
   the rows are scattered over many.
3. Count searches through the cache, scoped by the same release list (below).
4. Cite at the end: `ac.citations_for(con, pairs)`, `sighting_sources` with the same release filter, `ac.drill` or
   `event_sightings` for one search, `event_log.citation` for event logs. `sighting_id` (a DuckDB hash) is a join key
   within one session: never publish or store it. Export rules: [pii.md](pii.md), "Exporting with citations".

**Months are decided by `t`, never by file names.** `t` is UTC stored without a zone ([semantics.md](semantics.md),
§6). A Pacific month in UTC bounds: `timezone('UTC', timezone('America/Los_Angeles', TIMESTAMP '2026-03-01'))` gives
`2026-03-01 08:00` (PST); April 1 gives `07:00` (PDT). Files cut on other boundaries spill across months by a day or
so: Mountain View's December 2025 file starts at 22:01 on November 30 Pacific (Recipe 3); Port Hueneme's are named
`2_1_2026-3_2_2026`, `3_2_2026-4_1_2026` (Recipe 2). Use file names only to choose which releases to parse, include the files
named for the months either side, then filter on `t`. To bucket many rows, `date_trunc('hour', t)` before converting:
the Pacific offset is a whole number of hours.

**Counting searches.** One search appears in many logs and re-releases ([semantics.md](semantics.md), §3). Count
`count(DISTINCT event_key)` from `cache.sighting_event`, scoped by the same release list; say "rows" for `count(*)`.
Two traps:

- `count(DISTINCT event_key)` silently skips rows with no cache row (no UUID, and no parsed time or org; the corpus
  count is in [stats.md](stats.md), Link tiers). `LEFT JOIN` the cache and report `count(*) FILTER (WHERE event_key IS NULL)`
  as unlinked rows.
- `x_ambiguous` sightings are their own events (`x:` + `sighting_id`): re-released copies count twice and never
  match another log. Report how many a count contains.

## Recipes

| # | Question | Example scope | Measured |
|---|---|---|---:|
| 1 | What is loaded for a producer, and which releases to use | Santa Rosa | ≤ 0.6 s |
| 2 | Every search by one organization in one month, in every log, one row per search, cited | San Mateo PD, March 2026; Cotati, June 2026 | 8.6 s; 3.1 s |
| 3 | Which organizations searched a producer's cameras, by month | Mountain View, November 2025 | 1.8–4.7 s |
| 4 | When out-of-state organizations stopped appearing in a network audit | San Bruno, Jan–Jun 2025 | 3.4–3.7 s |
| 5 | Reasons masked in one log but readable in another | Contra Costa vs Vacaville | 2.0–3.0 s |
| 6 | San Mateo PD's own log from the PDFs: months, searches, duplicates | all 34 PDFs | 1.2 s |
| 7 | A producer's own-log Reason vs another log's record of the same search | San Mateo PD vs San Bruno, March 2026 | 1.2 s |
| 8 | One Flock search (UUID) in every log, with citations | one LAPD search, 15 sightings | 1.0 s |
| 9 | Case-number patterns, framed carefully | one San Jose network sheet | ≤ 0.6 s per block |
| 10 | Event logs: network-sharing changes, user creation and deletion | Santa Rosa; all event logs | ≤ 0.2 s |
| 11 | Re-releases: which releases repeat the same searches, and how they differ | San Jose own-search log | ≤ 1.6 s per block |
| 12 | Following a plate token across agencies (tokens only) | Ventura PD and Port Hueneme own-search logs | 0.7 s |
| 13 | Text-prompt (`freeform`) and `visual` searches over time | San Francisco own-search log | 0.9 s |
| 14 | Searches by hour of day and day of week | Santa Rosa own-search log | 1.3–1.7 s |
| 15 | Rows the parser could not place in time | Santa Rosa, Feb 2026 network audit | 1.7 s |

---

### Recipe 1. What is loaded for a producer, and which releases to use

**Question.** Which requests, productions and exports exist for one producer, and which columns does each have?
Per-producer spans for the whole corpus are in [coverage.md](coverage.md); this is the per-release view.

```sql
SELECT rs.audit, rs.request_label, rs.released_on, count(*) AS releases, sum(r.n_rows) AS rows_,
       count(*) FILTER (WHERE list_contains(r.header, 'Reason')) AS with_reason_column,
       count(*) FILTER (WHERE list_contains(r.header, 'ID')) AS with_search_uuid,
       count(*) FILTER (WHERE r.content_sha256 IN (SELECT content_sha256 FROM release_content_groups)) AS in_identical_group
FROM release_sources rs JOIN truth.releases r USING (release_id)
WHERE rs.producer = 'Santa Rosa CA PD'
GROUP BY ALL ORDER BY 1, 3;
```

```text
audit    request_label             released_on releases rows_     with_reason with_uuid in_identical_group
event    MuckRock request 214823   2026-08-20   3          434     0           0         3
event    MuckRock request 214823   2026-09-01   3          434     0           0         3
network  MuckRock request 214823   2026-08-20   8    4,099,760     8           8         8
network  MuckRock request 214823   2026-09-01  11    5,345,949    11          11         8
own      MuckRock request 214823   2026-08-20  13       67,744    13          13        13
own      MuckRock request 214823   2026-09-01  19      104,794    19          19        13
```

0.01 s. Every sheet of the 2026-08-20 production reappears with identical content in the 2026-09-01 one, which adds 3
network and 6 own-search sheets. Adding `rows_` across the two productions double-counts those sheets.

What each release actually covers (file names can be wrong, [semantics.md](semantics.md) §6.2); this parses the
producer's rows:

```sql
SELECT release_id, count(*) AS rows_,
       strftime(timezone('America/Los_Angeles', timezone('UTC', min(t))), '%Y-%m-%d %H:%M') AS first_pacific,
       strftime(timezone('America/Los_Angeles', timezone('UTC', max(t))), '%Y-%m-%d %H:%M') AS last_pacific
FROM sightings
WHERE producer = 'Santa Rosa CA PD' AND audit = 'own'
GROUP BY 1 ORDER BY min(t), release_id LIMIT 2;
```

```text
mr:214823:26-996_2026-09-01_00_44_52_-0700.zip!REDACTED_1_1_2025-1_31_2025-Santa Rosa CA PD-Audit.csv#csv  6,272  2025-01-01 02:09  2025-01-31 23:17
mr:214823:26-996_2026-09-01_00_44_52_-0700.zip!REDACTED_2_1_2025-2_28_2025-Santa Rosa CA PD-Audit.csv#csv  5,227  2025-02-01 00:59  2025-02-28 22:56
```

0.55 s (172,538 rows). Producers with both an own-search log and a network audit:

```sql
SELECT producer, list(DISTINCT audit ORDER BY audit) AS logs, count(*) AS releases
FROM truth.releases
GROUP BY 1 HAVING list_contains(logs, 'network') AND list_contains(logs, 'own')
ORDER BY 1;
```

```text
18 rows: Berkeley CA PD [network, own] 24 | Cotati CA PD 2 | El Cerrito CA PD 2 | Los Altos CA PD 74 | …
         Riverside County CA SO [event, network, own] 3 | … | Ventura County CA SO 77
```

0.01 s. San Mateo PD is own-log only: 34 releases, one per PDF (Recipe 6).

**Caveats.**
- MuckRock 188086's `SAMPLES/Denver_ALPR_Network_Searches_1.xlsx` (1,000,000 rows, none by Pasadena) is attributed
  to `Denver Police Department` by a `producers.json` file override; the basis is circumstantial and quoted in
  `truth.releases.producer_source`. Pasadena has only its own-search log.
- `release_content_groups` misses same-content files in another format or row order ([semantics.md](semantics.md)
  §14; Recipe 11).

**Cite.** A release is cited by its `release_sources` row: `public_release_id`, `request_label`, `request_url`,
`document`, `sheet`, and `link` (download URL with the SHA-256 of the file and, inside a zip, of the member)
([provenance.md](provenance.md)).

---

### Recipe 2. Every search by one organization in one month, in every log, one row per search, cited

**Question.** Every search San Mateo PD ran in March 2026 (Pacific) that any loaded log recorded, one row per search,
with how many logs have it and a citation.

Step 1: the organization's rows in every log, found without parsing. `org` is the trimmed `Org Name` cell, the producer
for an own-search log with no `Org Name` column, or San Mateo PD for its PDFs; no layout correction remaps `Org Name`.
`raw_t` is the same expression `sightings` uses, on the raw cell.

```sql
SET VARIABLE cb_org  = 'San Mateo CA PD';
SET VARIABLE cb_from = timezone('UTC', timezone('America/Los_Angeles', TIMESTAMP '2026-03-01'));
SET VARIABLE cb_to   = timezone('UTC', timezone('America/Los_Angeles', TIMESTAMP '2026-04-01'));
CREATE OR REPLACE TEMP TABLE cb_org_rows AS
SELECT release_id, row_no,                              -- rows whose released Org Name is the org
       coalesce(flock_ts("Search Time"),
                flock_ts(json_extract_string(extra, '$."Search Date"') || ' ' || trim("Search Time"))) AS raw_t
FROM truth.flock_audit_rows
WHERE trim("Org Name") = getvariable('cb_org')
UNION ALL
SELECT release_id, row_no,                              -- the org's own-search logs without an Org Name column
       coalesce(flock_ts("Search Time"),
                flock_ts(json_extract_string(extra, '$."Search Date"') || ' ' || trim("Search Time")))
FROM truth.flock_audit_rows
WHERE release_id IN (SELECT release_id FROM truth.releases
                     WHERE producer = getvariable('cb_org') AND audit = 'own' AND NOT list_contains(header, 'Org Name'))
UNION ALL
SELECT release_id, row_no, t                            -- San Mateo PD's PDFs: no org field; 110,705 rows, parsed whole
FROM sightings_smpd
WHERE getvariable('cb_org') = 'San Mateo CA PD';
SELECT count(*) AS rows_, count(DISTINCT release_id) AS releases FROM cb_org_rows;
```

```text
rows_ 1,240,930 | releases 452
```

3.6 s (one pass over every row of `truth.flock_audit_rows`).

Step 2: keep the rows that can be in the month. For rows outside layout-corrected releases `raw_t` equals the parsed
`t`; rows with no raw time and every row of a release with a layout correction go to the parser regardless.

```sql
CREATE OR REPLACE TEMP TABLE cb_candidates AS
SELECT release_id, row_no FROM cb_org_rows
WHERE raw_t >= getvariable('cb_from') AND raw_t < getvariable('cb_to')
   OR raw_t IS NULL
   OR release_id IN (SELECT release_id FROM release_meta WHERE layouts IS NOT NULL);
SELECT count(*) AS rows_, count(DISTINCT release_id) AS releases FROM cb_candidates;
```

```text
rows_ 48,834 | releases 25
```

0.05 s.

Step 3: parse exactly those rows (`audit_client`, the same SQL as `sightings`), then decide the month by `t`.

```python
pairs = con.sql("SELECT release_id, row_no FROM cb_candidates").fetchall()
con.execute("CREATE OR REPLACE TEMP TABLE cb_parsed AS " + ac.sightings_sql(pairs))
```

```sql
CREATE OR REPLACE TEMP TABLE cb_month AS
SELECT sighting_id, release_id, row_no, producer, audit, t FROM cb_parsed
WHERE t >= getvariable('cb_from') AND t < getvariable('cb_to');
SET VARIABLE cb_rels = (SELECT list(DISTINCT release_id) FROM cb_month);
SELECT count(*) AS rows_, count(DISTINCT release_id) AS releases, count(DISTINCT producer) AS logs FROM cb_month;
```

```text
rows_ 38,102 | releases 21 | logs 12
```

3.7 s.

Step 4: one row per search. The cache is scoped by `cb_rels` and `LEFT JOIN`ed, so unlinked rows stay visible. The
citation row prefers the organization's own log.

```sql
CREATE OR REPLACE TEMP TABLE cb_month_ev AS
SELECT m.*, se.event_key, se.event_id, se.basis
FROM cb_month m
LEFT JOIN (SELECT sighting_id, event_key, event_id, basis FROM cache.sighting_event
           WHERE list_contains(getvariable('cb_rels'), release_id)) se USING (sighting_id);
CREATE OR REPLACE TEMP TABLE cb_searches AS
SELECT event_id, min(t) AS t, count(*) AS sightings, count(DISTINCT producer) AS logs, max(basis) AS weakest_link,
       bool_or(audit = 'own') AS in_own_log,
       arg_min({'release_id': release_id, 'row_no': row_no}, (audit <> 'own', release_id, row_no)) AS cite
FROM cb_month_ev WHERE event_key IS NOT NULL
GROUP BY event_key, event_id;
SELECT (SELECT count(*) FROM cb_month_ev) AS rows_,
       (SELECT count(*) FROM cb_month_ev WHERE event_key IS NULL) AS unlinked_rows,
       count(*) AS searches, count(*) FILTER (WHERE in_own_log) AS in_own_log,
       count(*) FILTER (WHERE logs = 1) AS in_one_log, count(*) FILTER (WHERE event_id LIKE 'x:%') AS x_ambiguous
FROM cb_searches;
```

```text
rows_ 38,102 | unlinked_rows 0 | searches 3,391 | in_own_log 3,352 | in_one_log 786 | x_ambiguous 39
```

```sql
SELECT weakest_link, count(*) AS searches FROM cb_searches GROUP BY 1 ORDER BY 1;
```

```text
1_uuid 754 | 2_k5 8 | 3_k3 2,590 | x_ambiguous 39
```

0.9 s. `weakest_link` is `max(basis)` (tier labels sort strongest first). The 2,590 `3_k3` searches are those with a
copy in Sonoma County's network audit, which has neither UUIDs nor Time Frame and is matched on org, second and
network count; the other logs in scope link by UUID (`1_uuid`) or, for Los Altos and Ventura County, by `2_k5`.

Step 5: a citation per search.

```python
cite_pairs = con.sql("SELECT cite.release_id, cite.row_no FROM cb_searches").fetchall()
con.execute("CREATE OR REPLACE TEMP TABLE cb_cites AS " + ac.citations_sql(cite_pairs))
```

```sql
SELECT s.event_id, s.t, s.logs, s.weakest_link, c.locator, c.citation
FROM cb_searches s JOIN cb_cites c ON c.release_id = s.cite.release_id AND c.row_no = s.cite.row_no
ORDER BY s.t, s.event_id LIMIT 1;
```

```text
u:cc4e6114-2d77-411e-906b-237c9a3d42c7  2026-03-01 08:13:00  11  3_k3  page 31 (search id cc4e6114-2d77-411e-906b-237c9a3d42c7)
  'San Mateo CA PD. San Mateo public records request W012541-041426, produced (production date not recorded).
   3_1_2026-3_31_2026-San_Mateo_CA_PD-Audit__1_.pdf, page 31 (search id cc4e6114-…). Document:
   https://github.com/none-below/sm-alpr/blob/e138455bb…/assets/san-mateo-public-records/W012541-041426/3_1_2026-3_31_2026-San_Mateo_CA_PD-Audit__1_.pdf
   (SHA-256 bc578dbd…e4e6)'
```

0.3 s; 3,391 searches, 3,391 distinct citations. Total 8.6 s. The same code for `cb_org = 'Cotati CA PD'`, June 2026:
2,901 rows in 100 releases → 244 candidates → 102 rows in 8 releases → 20 searches, all `1_uuid`, in 3.1 s.

**Caveats.**
- File names would have given the wrong month. The 21 releases include files named for February (Port Hueneme's
  `2_1_2026-3_2_2026` file holds 59 March rows per copy; Los Altos's February sheet, 23), and March files hold April
  rows (56 and 17).
- "Searches" are events. The 39 `x_ambiguous` ones are network-audit rows whose link key matches more than one search:
  each counts alone even if it repeats a search already counted; San Mateo PD's own PDF has 3,352 distinct search ids for the
  month. Say "at least N searches in the logs released to date" ([semantics.md](semantics.md) §17).
- Check spellings before step 1: exports can spell the org differently from the registry (Mountain View's rows read
  `Mountain View CA PD (Santa Clara County)`, [semantics.md](semantics.md) §5). A search recorded only under another
  spelling is missed.
- All months: skip steps 2–5 and count from the cache alone. `SET VARIABLE cb_all = (SELECT list(DISTINCT release_id)
  FROM cb_org_rows)`, then `cache.sighting_event SEMI JOIN cb_org_rows USING (release_id, row_no) WHERE
  list_contains(getvariable('cb_all'), release_id)`: 1,240,930 rows, 142,507 searches (3,222 `x_ambiguous`), 4.7 s.
  Parse month by month.

**Cite.** Step 5: `cb_cites.citation` per search. Every row behind a search: `cb_month_ev` holds all 38,102
`(release_id, row_no)` pairs; pass them to `ac.citations_for`.

---

### Recipe 3. Which organizations searched a producer's cameras, by month

**Question.** Per Pacific month, which organizations appear in Mountain View's network audit, counting searches rather
than rows. Example: November 2025.

File names choose which releases to parse (the month and its neighbours); `t` decides the month.

```sql
SET VARIABLE cb_rels = (SELECT list(release_id) FROM truth.releases
                        WHERE producer = 'Mountain View Police Department' AND audit = 'network'
                          AND regexp_matches(release_id, '_(Oct|Nov|Dec)_2025\.csv#'));   -- November and both neighbours
CREATE OR REPLACE TEMP TABLE cb_by_org AS
WITH se AS (SELECT sighting_id, event_key FROM cache.sighting_event
            WHERE list_contains(getvariable('cb_rels'), release_id))
SELECT strftime(timezone('America/Los_Angeles', timezone('UTC', date_trunc('hour', s.t))), '%Y-%m') AS pacific_month,
       s.org, count(*) AS rows_, count(se.event_key) AS linked_rows, count(DISTINCT se.event_key) AS searches
FROM sightings s LEFT JOIN se USING (sighting_id)
WHERE list_contains(getvariable('cb_rels'), s.release_id)
GROUP BY ALL;
SELECT pacific_month, count(*) AS orgs, sum(rows_) AS rows_, sum(rows_ - linked_rows) AS unlinked_rows,
       sum(searches) AS searches
FROM cb_by_org GROUP BY 1 ORDER BY 1;
```

```text
pacific_month  orgs    rows_  unlinked_rows  searches
2025-09          26      314              0       157
2025-10         281  369,100              0   184,550
2025-11         274  362,310              0   181,155
2025-12         258  232,434              0   116,217
```

```sql
SELECT pacific_month, org, rows_, searches FROM cb_by_org WHERE pacific_month = '2025-11'
ORDER BY searches DESC, org LIMIT 3;
```

```text
2025-11  San Francisco CA PD     66,978  33,489
2025-11  Riverside County CA SO  61,896  30,948
2025-11  Fremont CA PD           15,538   7,769
```

1.8–4.7 s over two runs (six releases, 964,158 rows). Only November is complete in this scope: September and October need the files
before them, December's file ends on December 18.

**Caveats.**
- Where November's rows come from: 361,230 from the two November files and 1,080 from the two December files (the
  December file starts at 22:01 on November 30 Pacific); 370 rows of the November files are October. A `pacific_month
  = NULL` line would hold rows with no `t`; there are none here.
- Every month here was produced twice, byte-identical, in requests 197131 and 197815, so rows are exactly twice the
  searches. Searches are counted within this producer's rows only.
- `sum(searches)` over orgs is a total only because each event has one org within one producer's log.
- A network audit lists every search that covered the producer's cameras, including the producer's own
  ([semantics.md](semantics.md) §4), spelled here `Mountain View CA PD (Santa Clara County)`.

**Cite.** One cell (Oakland, November):

```python
pairs = con.sql("""SELECT release_id, row_no FROM sightings
                   WHERE list_contains(getvariable('cb_rels'), release_id) AND org = 'Oakland CA PD'
                     AND t >= TIMESTAMP '2025-11-01 07:00' AND t < TIMESTAMP '2025-12-01 08:00'""").fetchall()
cites = ac.citations_for(con, pairs)          # 9,798 rows, 9,798 distinct citations, 1.2 s
```

Cite one release of each identical pair and add "also produced in …" ([provenance.md](provenance.md), Re-releases).

---

### Recipe 4. When out-of-state organizations stopped appearing in a network audit

**Question.** In San Bruno's network audit, when was the last search by an organization outside California?

Aggregate per organization first (parsing is the cost), then classify the few thousand names:

```sql
SET VARIABLE cb_rels = (SELECT list(release_id) FROM truth.releases
                        WHERE producer = 'San Bruno CA PD' AND audit = 'network'
                          AND regexp_matches(release_id, '/[1-6]_1_2025-'));   -- example scope: files named Jan-Jun 2025
CREATE OR REPLACE TEMP TABLE cb_per_org AS
SELECT org, count(*) AS rows_, max(t) AS last_t,
       arg_max(release_id, t) AS last_release_id, arg_max(row_no, t) AS last_row_no
FROM sightings
WHERE list_contains(getvariable('cb_rels'), release_id) AND org IS NOT NULL AND t IS NOT NULL
GROUP BY org;
CREATE OR REPLACE TEMP TABLE cb_org_home AS
WITH c AS (
  SELECT *, list_filter(regexp_extract_all(org, '\b[A-Z]{2}\b'), x -> list_contains(
      ['AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD','MA','MI',
       'MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT',
       'VT','VA','WA','WV','WI','WY','DC'], x)) AS codes
  FROM cb_per_org)
SELECT *, CASE WHEN list_contains(codes, 'CA') OR org ILIKE 'california%' THEN 'california'
               WHEN len(codes) > 0 THEN 'other_state'
               ELSE 'no_state_code' END AS home
FROM c;
SELECT home, strftime(timezone('America/Los_Angeles', timezone('UTC', last_t)), '%Y-%m') AS last_seen_pacific_month,
       count(*) AS orgs, sum(rows_) AS rows_, max(last_t) AS latest_utc
FROM cb_org_home GROUP BY ALL ORDER BY 1, 2;
```

```text
home           last_seen  orgs  rows_      latest_utc
california     2025-01       3         14  2025-01-22 20:23:25
california     2025-04       6     54,919  2025-04-30 15:46:32
california     2025-05      14      1,549  2025-05-30 20:52:12
california     2025-06     296  2,546,337  2025-07-01 06:59:56
no_state_code  2025-01      18        127  2025-01-31 17:08:14
no_state_code  2025-02      49     19,498  2025-02-11 15:54:30
no_state_code  2025-04       1         17  2025-04-14 04:30:04
no_state_code  2025-05       1          1  2025-05-16 14:24:50
no_state_code  2025-06       5     85,514  2025-07-01 06:53:48
other_state    2025-01     680      5,203  2025-02-01 07:36:40
other_state    2025-02   2,489    399,627  2025-02-11 15:56:55
```

3.4–3.7 s (six releases, 3,112,806 rows). All of San Bruno's network audit is 11.6M rows.

The row behind the last out-of-state search:

```python
org, last_t, rid, n = con.sql("""SELECT org, last_t, last_release_id, last_row_no FROM cb_org_home
                                 WHERE home = 'other_state' ORDER BY last_t DESC LIMIT 1""").fetchone()
ac.citations_for(con, [(rid, n)]).select("src_row, document, citation").fetchone()
```

```text
Tulsa OK PD  2025-02-11 15:56:55  414743  OneDrive_2026-04-28.zip > 2_1_2025-2_28_2025-San Bruno CA PD-Network-Audit.csv
  '… row 414743. Download: https://cdn.muckrock.com/foia_files/2026/04/28/OneDrive_2026-04-28.zip (SHA-256 04b8f490…0aea69);
   2_1_2025-2_28_2025-San Bruno CA PD-Network-Audit.csv inside it: SHA-256 7d47eaf7…efe2e'
```

0.04 s.

**Caveats.**
- The state comes from the name, a heuristic. `SD` in `Los Angeles County CA SD` is a department (caught by `CA`);
  `CO` can be Colorado or "county" (`Blaine CO OK SO`). `no_state_code` holds names with no code: statewide
  agencies spelled out (`Texas Department of Public Safety`, `Cal Fire`), `NCRIC`, federal entries. Read the
  `cb_org_home` rows by hand before quoting a count.
- "Last seen in this log" is not "stopped searching": a network audit shows only searches that covered this
  producer's cameras. Say which network audits you checked, and repeat per producer before generalizing.
- A last-seen month at the end of the scope says nothing about later months: widen the scope.

**Cite.** `last_release_id` / `last_row_no` per org, through `ac.citations_for` as above. For "no out-of-state rows
after date D", cite the release(s) covering the later period and say how many rows they hold.

---

### Recipe 5. Reasons masked in one log but readable in another

**Question.** Contra Costa's network audit masks Reason as `[REDACTED]`. For the same searches, what did Vacaville's
network audit release?

`read_field('reason', who := …)` compares a producer against every other log; a pairwise comparison scoped to two
producers is cheaper and keeps both row keys:

```sql
SET VARIABLE cb_here  = (SELECT list(release_id) FROM truth.releases WHERE producer = 'Contra Costa County CA SO' AND audit = 'network');
SET VARIABLE cb_there = (SELECT list(release_id) FROM truth.releases WHERE producer = 'Vacaville CA PD' AND audit = 'network');
CREATE OR REPLACE TEMP TABLE cb_pairs AS
WITH kh AS (SELECT sighting_id, event_key, basis FROM cache.sighting_event WHERE list_contains(getvariable('cb_here'), release_id)),
     kt AS (SELECT sighting_id, event_key, basis FROM cache.sighting_event WHERE list_contains(getvariable('cb_there'), release_id)),
     sh AS (SELECT sighting_id, release_id, row_no, reason_state FROM sightings WHERE list_contains(getvariable('cb_here'), release_id)),
     st AS (SELECT sighting_id, release_id, row_no, reason_state FROM sightings WHERE list_contains(getvariable('cb_there'), release_id))
SELECT kh.event_key, greatest(kh.basis, kt.basis) AS weakest_link,
       sh.release_id AS here_release, sh.row_no AS here_row, sh.reason_state AS state_here,
       st.release_id AS there_release, st.row_no AS there_row, st.reason_state AS state_there
FROM sh JOIN kh USING (sighting_id) JOIN kt USING (event_key) JOIN st ON st.sighting_id = kt.sighting_id;
SELECT state_here, state_there, count(DISTINCT event_key) AS searches, count(*) AS row_pairs
FROM cb_pairs GROUP BY ALL ORDER BY searches DESC;
```

```text
redacted_agency  value        78,903  79,407
redacted_agency  placeholder     290     292
```

2.0–3.0 s (371,962 + 88,132 rows). By tier: `2_k5` 78,920 searches, `4_k5_group` 273 (neither log has UUIDs here).

**Caveats.**
- Vacaville's three files each cover one Pacific day (January 23, 28 and 31, 2025), so this is the overlap of those
  days with Contra Costa's January 17 – February 1 period, not all of Contra Costa's searches.
- `value` is not proof the text is a genuine reason ([semantics.md](semantics.md) §10). Look at the distinct values
  locally before counting "reasons revealed", and check each quoted value in both originals.
- `row_pairs` exceeds `searches` where one side holds the same search twice. A `4_k5_group` match joins rows that
  share org, second, networks and time frame but no UUID: name the tier ([linking.md](linking.md)).
- `divergence()` and `read_field` compare exact, case-sensitive strings; see Recipe 7.

**Cite.** Both rows, always:

```python
p = con.sql("""SELECT here_release, here_row, there_release, there_row FROM cb_pairs
               WHERE state_here IN ('redacted_flock', 'redacted_agency', 'withheld', 'partial') AND state_there = 'value'
               ORDER BY here_release, here_row LIMIT 1""").fetchone()
ac.citations_for(con, [(p[0], p[1]), (p[2], p[3])]).select("src_row, document, citation").fetchall()
```

```text
20941  1_23_2025-1_23_2025-Vacaville_CA_PD-Network-Audit.csv            'Vacaville CA PD. MuckRock request 196333 (…), produced 2025-11-06. … row 20941. …'
242    CCCSO_Network_Audit_1_17_2025_2_1_2025_REDACTED_File_1_of_2.xlsb  'Contra Costa County CA SO. MuckRock request 196392 (…), produced 2025-11-13. …'
```

(`citations_for` returns rows ordered by `release_id`, `row_no`.)

0.09 s.

---

### Recipe 6. San Mateo PD's own log from the PDFs: months, searches, duplicates

**Question.** Which months of San Mateo PD's own search log are loaded, how many searches each holds, and which rows
need care?

San Mateo PD's log is read from the produced PDFs' text layer: one release per PDF (`smpd:<request>:<pdf name>`),
one truth row (`truth.smpd_pdf_rows`) per printed search-id block. It is small enough to parse whole.

```sql
SET VARIABLE cb_smpd = (SELECT list(release_id) FROM truth.releases WHERE producer = 'San Mateo CA PD');
CREATE OR REPLACE TEMP TABLE cb_smpd_rows AS
SELECT s.release_id, s.row_no, s.t, s.flock_id, se.event_key
FROM sightings s
LEFT JOIN (SELECT sighting_id, event_key FROM cache.sighting_event
           WHERE list_contains(getvariable('cb_smpd'), release_id)) se USING (sighting_id)
WHERE list_contains(getvariable('cb_smpd'), s.release_id);
SELECT strftime(timezone('America/Los_Angeles', timezone('UTC', date_trunc('hour', t))), '%Y-%m') AS pacific_month,
       count(*) AS rows_, count(DISTINCT flock_id) AS searches, count(DISTINCT event_key) AS events,
       count(*) FILTER (WHERE event_key IS NULL) AS unlinked, count(DISTINCT release_id) AS pdfs
FROM cb_smpd_rows GROUP BY 1 ORDER BY 1;
```

```text
pacific_month  rows_  searches  events  unlinked  pdfs
2023-01        2,829     2,751   2,751         0     1
2023-02        3,041     3,041   3,041         0     1
…
2023-06        1,864     1,864   1,864         0     1
2023-10        2,002     2,002   2,002         0     1
…
2024-05        3,283     3,283   3,283         0     1
2024-10        3,272     3,272   3,272         0     1
…
2024-12        4,026     4,025   4,025         0     1
2025-01        5,293     5,293   5,293         0     2
2025-02        6,235     6,135   6,135         0     1
…
2025-05        5,856     5,856   5,856         0     1
2025-10        3,782     3,782   3,782         0     1
…
2026-01        4,650     4,650   4,650         0     2
…
2026-05        2,741     2,741   2,741         0     1
(30 months; totals: 110,705 rows, 110,526 searches, 110,526 events, no row without t or search id)
```

1.2 s. Each PDF's rows fall inside its own Pacific month. June 2023 and May 2025 are present; June 2024 is not: its two
PDFs are image-only (no text layer; OCR not loaded):

```sql
SELECT regexp_extract(release_id, '[^:]+$') AS pdf, n_rows, header_basis FROM truth.releases
WHERE producer = 'San Mateo CA PD' AND n_rows = 0;
```

```text
6_1_2024-6_30_2024-San_Mateo_CA_PD-Audit_-_PART_1.pdf  0  image-only PDF: no text layer; OCR rows not loaded yet
6_1_2024-6_30_2024-San_Mateo_CA_PD-Audit_-_PART_2.pdf  0  image-only PDF: no text layer; OCR rows not loaded yet
```

No PDF is loaded for July–September 2023, July–September 2024 or June–September 2025. Search ids printed more than
once, and rows read by printed position:

```sql
SELECT count(*) AS ids_printed_more_than_once, sum(n) - count(*) AS extra_rows,
       count(*) FILTER (WHERE pdfs > 1) AS in_two_pdfs
FROM (SELECT flock_id, count(*) AS n, count(DISTINCT release_id) AS pdfs FROM cb_smpd_rows GROUP BY 1 HAVING count(*) > 1);
```

```text
ids_printed_more_than_once 159 | extra_rows 179 | in_two_pdfs 0
```

```sql
SELECT regexp_extract(release_id, '[^:]+$') AS pdf, count(*) AS rows_with_parse_note
FROM truth.smpd_pdf_rows WHERE parse_note IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1;
```

```text
2_1_2026-2_28_2026-San_Mateo_CA_PD-Audit__2_.pdf 33 | 3_1_2026-3_31_2026-…__1_.pdf 15 | 1_1_2026-1_31_2026-…__1___Part_2_.pdf 8
2_1_2024-2_29_2024-…pdf 7 | 1_1_2025-1_31_2025-…__Part_1_.pdf 2 | 4_1_2025-4_30_2025-…pdf 2 | 5_1_2024-5_31_2024-…pdf 2
```

0.1 s.

**Caveats.**
- Count searches with `count(DISTINCT flock_id)` or `count(DISTINCT event_key)`, never rows: 179 rows repeat a search
  id already printed (78 in January 2023, 100 in February 2025, 1 in December 2024). A search printed in two PDFs
  would likewise be two sightings of one event; this build has none.
- The 69 `parse_note` rows sit on Acrobat-edited pages where the text order disagreed with the printed row; they were
  read by printed position. Check the page before quoting one. The repo's merged JSON differs from the PDFs on those
  pages ([provenance.md](provenance.md), [semantics.md](semantics.md)); quote the PDF.
- The PDFs have no Org Name, Case #, plate or Time Frame column: `case_state` and `plate_state` are `not_exported`.
  They do print the search UUID, so every PDF row is linked `1_uuid`.
- Missing months are missing from what was produced to date, not evidence of no searches ([semantics.md](semantics.md)
  §17).

**Cite.** A PDF row is cited by file and page; there is no `src_row`:

```python
pairs = con.sql("""SELECT release_id, row_no FROM sightings WHERE list_contains(getvariable('cb_smpd'), release_id)
                   QUALIFY count(*) OVER (PARTITION BY flock_id) > 1 ORDER BY release_id, row_no LIMIT 2""").fetchall()
ac.citations_for(con, pairs).select("row_no, locator, link").fetchall()
```

```text
2146  page 66 (search id 644fc0c3-8544-4f68-b08d-1bf5ed7ad1d1)  Document: https://github.com/none-below/sm-alpr/blob/e138455bb…/12_1_2024-12_31_2024-San_Mateo_CA_PD-Audit.pdf (SHA-256 c5017a26…5d36)
2147  page 66 (search id 644fc0c3-8544-4f68-b08d-1bf5ed7ad1d1)  (same)
```

0.4 s. Two copies on one page get the same locator; `truth.smpd_pdf_rows.src_line` tells them apart.

---

### Recipe 7. A producer's own-log Reason vs another log's record of the same search

**Question.** For San Mateo PD's searches in March 2026, does its own log (the PDF) give the same Reason as San
Bruno's network audit? Both carry Flock UUIDs, so they join on `flock_id`.

```sql
SET VARIABLE cb_own   = (SELECT list(release_id) FROM truth.releases WHERE producer = 'San Mateo CA PD');
SET VARIABLE cb_other = (SELECT list(release_id) FROM truth.releases
                         WHERE producer = 'San Bruno CA PD' AND audit = 'network'
                           AND regexp_matches(release_id, '/[23]_1_2026-'));    -- files named Feb and Mar 2026 (the last)
CREATE OR REPLACE TEMP TABLE cb_cmp AS
WITH own AS (SELECT release_id, row_no, flock_id, reason_state, reason FROM sightings
             WHERE list_contains(getvariable('cb_own'), release_id)
               AND t >= TIMESTAMP '2026-03-01 08:00' AND t < TIMESTAMP '2026-04-01 07:00'),   -- March 2026, Pacific
other AS (SELECT release_id, row_no, flock_id, reason_state, reason FROM sightings
          WHERE list_contains(getvariable('cb_other'), release_id) AND org = 'San Mateo CA PD')
SELECT flock_id, own.release_id AS own_release, own.row_no AS own_row, other.release_id AS other_release,
       other.row_no AS other_row, own.reason_state AS own_state, other.reason_state AS other_state,
       CASE WHEN own.reason = other.reason THEN 'identical'
            WHEN lower(regexp_replace(own.reason, '\s*-\s*$', '')) = lower(regexp_replace(other.reason, '\s*-\s*$', ''))
                 THEN 'same after dropping a trailing " -" and case'
            WHEN starts_with(lower(own.reason), lower(other.reason) || ' - ') THEN 'own = other + " - " + more text'
            ELSE 'different' END AS comparison
FROM other JOIN own USING (flock_id);
SELECT own_state, other_state, comparison, count(DISTINCT flock_id) AS searches, count(*) AS row_pairs
FROM cb_cmp GROUP BY ALL ORDER BY searches DESC;
```

```text
value  value  same after dropping a trailing " -" and case  2,289  2,289
value  value  own = other + " - " + more text                  227    227
value  value  different                                         66     66
value  value  identical                                         22     22
```

1.2 s (all 110,705 PDF rows and 1,018,304 San Bruno rows parsed). March's own-log rows are 3,352 searches; 2,604 of
them are in San Bruno's audit, all in its March file.

**Caveats.**
- An exact comparison misleads here: 2,582 of 2,604 pairs differ as strings, but 2,289 of those differ only by a
  trailing `" -"` and letter case (San Mateo PD's log reads `Welfare Check -` where San Bruno's reads `Welfare Check`).
  `divergence()` and `read_field` compare exactly, so they would label all 2,582 `differs_from_other_logs`.
  Normalize on purpose, and say how.
- In 227 pairs San Mateo PD's text is San Bruno's followed by `" - "` and more text. The data does not say why two
  exports of one search differ; quote both, with both citations.
- Rows with a `parse_note` (Recipe 6) were read by printed position; check the page before quoting one.

**Cite.** Both sides:

```python
p = con.sql("""SELECT own_release, own_row, other_release, other_row FROM cb_cmp
               WHERE comparison = 'different' ORDER BY own_release, own_row LIMIT 1""").fetchone()
ac.citations_for(con, [(p[0], p[1]), (p[2], p[3])]).select("locator, document, citation").fetchall()
```

```text
row 376868                                               OneDrive_2026-04-28.zip > 3_1_2026-3_31_2026-San Bruno CA PD-Network-Audit.csv  '…'
page 6 (search id f0198ee6-e8eb-4125-8c93-42c265f9a5c3)  3_1_2026-3_31_2026-San_Mateo_CA_PD-Audit__1_.pdf  '…'
```

0.04 s.

---

### Recipe 8. One Flock search (UUID) in every log, with citations

**Question.** Where does Flock search `6023db79-9334-4b07-8b35-7f6e18366971` (a Los Angeles PD search, used in
[semantics.md](semantics.md) §1) appear, and where is each copy in its original?

```python
ac.event(con, "u:6023db79-9334-4b07-8b35-7f6e18366971")
# {'event_id': 'u:6023db79-…', 'n_sightings': 15, 'n_logs': 8, 'weakest_link': '3_k3', 'logs': ['Los Altos CA PD', …], …}
for r in ac.drill(con, "u:6023db79-9334-4b07-8b35-7f6e18366971"):
    print(r["producer"], r["basis"], r["src_row"], r["reason_state"], r["sha256"][:12], sep=" | ")
```

```text
Los Altos CA PD      2_k5     59184  value            5035864ed43c
Los Altos CA PD      2_k5     59184  value            322b642989dc
Port Hueneme CA PD   1_uuid   87889  redacted_agency  6daa20ce97a2
Port Hueneme CA PD   1_uuid  392317  redacted_agency  6daa20ce97a2
Port Hueneme CA PD   1_uuid   87889  value            b062d0498449
Port Hueneme CA PD   1_uuid  392317  value            b062d0498449
Redwood City CA PD   2_k5      6471  withheld         b6e204136a05
San Bruno CA PD      1_uuid  194588  redacted_flock   04b8f4904f4b
San Jose CA PD       2_k5      1001  redacted_agency  0b541dfbc3b2
San Jose CA PD       2_k5      1001  redacted_agency  7e6ac25972d2
Sonoma County CA SO  3_k3    162031  value            6d18c0ae42bf
Sonoma County CA SO  3_k3    162031  value            0bb5d342a2e8
Sonoma County CA SO  3_k3    162031  value            8214ee959165
Ukiah CA PD          1_uuid  219701  value            a00f38740c7e
Ukiah Fire CA FD     1_uuid   71343  value            b80842804268
```

`event` 0.4 s, `drill` 1.0 s. Each dict also has `org`, `t`, `nets`, the four `*_state` columns, `public_release_id`,
`citation` and `open_url`; `drill(…, surfaces=True)` adds the released Reason and Case # (local only). From the shell:
`python audit_client.py u:6023db79-9334-4b07-8b35-7f6e18366971`. In SQL, the same rows:

```sql
CREATE OR REPLACE TEMP TABLE cb_one AS
SELECT sighting_id, release_id, row_no, producer, basis FROM cache.sighting_event
WHERE event_key = hash('u:6023db79-9334-4b07-8b35-7f6e18366971');
SET VARIABLE cb_rels = (SELECT list(DISTINCT release_id) FROM cb_one);
SELECT o.producer, o.basis, ss.released_on, ss.src_row, ss.citation
FROM cb_one o JOIN sighting_sources ss USING (sighting_id)
WHERE list_contains(getvariable('cb_rels'), ss.release_id)
ORDER BY o.producer, ss.released_on, ss.src_row;
```

0.7–4.1 s over three runs. `SELECT * FROM event_sightings('u:…')` returns the drill columns plus released text in one macro call, but took
11.6 s.

**Caveats.**
- 15 sightings, 8 producers, 0 own-search logs: none of these is the searcher's record. Port Hueneme's April and May
  2025 files both hold it (the April file runs to the end of May 1 Pacific), in two productions each; the July 17
  production masks the Reason (`redacted_agency`) and the September 21 one releases it (`value`).
- Tiers differ per copy: `1_uuid` where the export has the UUID, `2_k5` where it was matched on org, second, networks
  and time frame, `3_k3` on org, second and networks. Name the tier when you rely on a match ([linking.md](linking.md)).
- Only `u:` event ids are stable across builds; `k5:`, `k3:` and `x:` ids are build-specific. Persist
  `(release_id, row_no)` instead ([linking.md](linking.md)).
- A UUID found in no log returns `None` / no rows, which says only that no loaded log recorded it
  ([semantics.md](semantics.md) §17).

**Cite.** `r["citation"]` per copy from `ac.drill`, or `ss.citation` above; `sha256` is the hash to check each
download against ([provenance.md](provenance.md)).

---

### Recipe 9. Case-number patterns, framed carefully

**Question.** What formats do the Case # values in a network audit take, and which rows have a shape like an FBI file
number (a classification number and letter, a two-letter office code, a serial: `999A-AA-9999999`)?

Many network audits mask other agencies' Case #; San Jose's released them. Shape census for one sheet:

```sql
SET VARIABLE cb_rid = 'mr:202333:Attachment_2-_Network_Audit_Aug_2025-Feb_2026_Redacted.xlsx#9_1_2025-9_30_2025-San Jose CA ';
SELECT regexp_replace(regexp_replace(case_no, '[0-9]', '9', 'g'), '[A-Za-z]', 'A', 'g') AS shape,
       count(*) AS rows_, count(DISTINCT org) AS orgs
FROM sightings WHERE release_id = getvariable('cb_rid') AND case_state = 'value'
GROUP BY 1 ORDER BY rows_ DESC LIMIT 5;
```

```text
99-99999      15,585  107
99-999999     15,089   54
99999999      12,672   80
999999999     12,254   71
999999AA9999   8,363    4
```

0.55 s (432,202 rows). Note the trailing space in the `release_id`: sheet names are kept as stored (Excel's
31-character cap cut this one after `CA `). Copy ids from `truth.releases`, never retype them.

```sql
CREATE OR REPLACE TEMP TABLE cb_hits AS
SELECT release_id, row_no, org,
       upper(regexp_extract(case_no, '(?i)\b[0-9]{1,3}[A-Z]{1,2}-([A-Z]{2})-[0-9]{4,8}\b', 1)) AS office_code
FROM sightings
WHERE release_id = getvariable('cb_rid') AND case_state = 'value'
  AND regexp_matches(case_no, '(?i)\b[0-9]{1,3}[A-Z]{1,2}-[A-Z]{2}-[0-9]{4,8}\b');
SELECT office_code, count(*) AS rows_, count(DISTINCT org) AS orgs FROM cb_hits GROUP BY 1 ORDER BY 2 DESC, 1;
```

```text
LA 140 4 | SC 48 3 | SL 37 1 | SD 27 2 | CG 3 1 | KC 3 1 | SF 1 1
```

```sql
SELECT org, count(*) AS rows_ FROM cb_hits GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 3;
```

```text
Los Angeles CA PD 140 | Turlock CA PD 42 | California Highway Patrol 27
```

0.45 s and under 0.01 s.

**Caveats.**
- A shape is not an attribution. That a value is laid out like an FBI file number does not show the FBI opened the
  case, took part in the search, or received results; an agency or task force can use the same layout. Write "a case
  number in the format of an FBI file number" and confirm with the agency or other records before saying more.
- Case # is free text typed by the searcher. Look at the flagged rows locally (in `sightings`, not in anything you
  publish) before counting; some shapes are plates ([pii.md](pii.md): `9AAA999` "case numbers" are tokenized in
  `sightings_public`).
- Another sheet or producer masks or formats Case # differently; the census is per release.

**Cite.** One release, so `sighting_sources` with the literal filter is enough:

```sql
SELECT h.office_code, ss.src_row, ss.citation
FROM cb_hits h JOIN sighting_sources ss USING (release_id, row_no)
WHERE ss.release_id = getvariable('cb_rid')
ORDER BY ss.src_row LIMIT 1;
```

```text
SC  2770  'San Jose CA PD. MuckRock request 202333 (https://www.muckrock.com/foi/san-jose-336/flock-safety-search-audits-san-jose-police-department-202333/),
           produced 2026-03-23. Attachment_2-_Network_Audit_Aug_2025-Feb_2026_Redacted.xlsx, sheet "9_1_2025-9_30_2025-San Jose CA ",
           row 2770. Download: https://cdn.muckrock.com/foia_files/2026/03/23/Attachment_2-_Network_Audit_Aug_2025-Feb_2026_Redacted.xlsx
           (SHA-256 1791b011…2e44)'
```

0.44 s; all 259 rows have distinct citations.

---

### Recipe 10. Event logs: network-sharing changes, user creation and deletion

**Question.** When did Santa Rosa add and remove network shares, and with whom? Which producers' event logs record
user creation and deletion?

`event_log` has 11,608 rows, so these read it whole. `Entity Details` of a `networkShare` row is three lines,
`Network Name: …`, `Permissions: …`, `Receiving Organization: …`: the network whose cameras are shared, what the
receiver may do, and who receives access.

```sql
CREATE OR REPLACE TEMP TABLE cb_shares AS
WITH keep AS (   -- one release per identical-content group
  SELECT release_id FROM truth.releases
  WHERE producer = 'Santa Rosa CA PD' AND audit = 'event'
    AND release_id NOT IN (SELECT unnest(releases[2:]) FROM release_content_groups))
SELECT release_id, row_no, ts, event_type,
       trim(regexp_extract(entity_details, 'Network Name: ([^\n]*)', 1)) AS network,
       trim(regexp_extract(entity_details, 'Receiving Organization: ([^\n]*)', 1)) AS receiver,
       trim(regexp_extract(entity_details, 'Permissions: ([^\n]*)', 1)) AS permissions,
       citation
FROM event_log
WHERE release_id IN (SELECT release_id FROM keep) AND entity_type = 'networkShare';
SELECT strftime(timezone('America/Los_Angeles', timezone('UTC', ts)), '%Y') AS pacific_year,
       network = 'Santa Rosa CA PD' AS own_network, event_type,
       count(*) AS shares, count(DISTINCT receiver) AS receivers, count(DISTINCT ts) AS timestamps
FROM cb_shares GROUP BY ALL ORDER BY 1, 2, 3;
```

```text
2023  false  delete    1    1    1
2023  true   create   88   15   61
2024  true   create  245  184  236
2025  true   create   74   71   73
2026  true   create   19   19   19
2026  true   delete    4    4    4
```

```sql
SELECT ts, event_type, receiver, permissions, split_part(citation, ' Download:', 1) AS citation
FROM cb_shares WHERE event_type = 'delete' AND network = 'Santa Rosa CA PD'
ORDER BY ts DESC LIMIT 2;
```

```text
2026-06-09 15:10:28  delete  Shafter PD CA             Search          'Santa Rosa CA PD. MuckRock request 214823 (…), produced 2026-08-20.
                                                                        26-996_2026-08-20_232659_-0700.zip > Event Logs - Network Share Log.csv, row 2.'
2026-05-28 16:32:26  delete  Cal State Fullerton (CA)  Search, Alerts  '… Event Logs - Network Share Log.csv, row 3.'
```

User creation and deletion across every event log (the `user` column, account names and e-mail addresses, is left
out):

```sql
WITH keep AS (
  SELECT release_id FROM truth.releases
  WHERE audit = 'event' AND release_id NOT IN (SELECT unnest(releases[2:]) FROM release_content_groups))
SELECT producer, event_type, count(*) AS rows_,
       count(DISTINCT (ts, "user", entity_details)) AS distinct_rows,
       min(ts)::DATE AS first_utc, max(ts)::DATE AS last_utc
FROM event_log
WHERE release_id IN (SELECT release_id FROM keep) AND entity_type = 'user' AND event_type IN ('create', 'delete')
GROUP BY ALL ORDER BY 1, 2;
```

```text
Amador County CA Sheriff's Office  create   9   3  2025-07-17  2025-07-17
Amador County CA Sheriff's Office  delete   7   4  2025-07-17  2026-04-29
Arcadia CA PD                      create  25  25  2023-05-24  2025-07-02
Arcadia CA PD                      delete  19  19  2025-02-04  2025-12-15
Greenfield CA PD                   create   2   1  2025-01-22  2025-01-22
San Joaquin County CA SO           create  17  17  2025-03-25  2025-09-09
San Joaquin County CA SO           delete   3   3  2025-03-25  2025-07-28
```

Each block under 0.1 s. `event_log_id` (the released `Event Id`) is not a key for a row or for one action:

```sql
SELECT producer, count(*) AS rows_, count(DISTINCT event_log_id) AS distinct_event_ids,
       max(rows_per_id) AS max_rows_per_id, max(times_per_id) AS max_timestamps_per_id
FROM (SELECT *, count(*) OVER w AS rows_per_id, count(DISTINCT ts) OVER w AS times_per_id
      FROM event_log WINDOW w AS (PARTITION BY release_id, event_log_id))
WHERE event_log_id IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

```text
Amador County CA Sheriff's Office   327    86  191   47
Arcadia CA PD                      7292  1538  394  376
Atherton CA PD                       24    24    1    1
Fountain Valley CA PD                32    32    1    1
Greenfield CA PD                    285   252    1    1
San Joaquin County CA SO            749   749    1    1
Santa Rosa CA PD                    868    28  406  369
```

0.18 s. Riverside County's hotlist log has no `Event Id` column.

**Caveats.**
- Count rows, not ids. De-duplicate re-releases by release, and exact repeats by the row's content, never by
  `event_log_id`.
- `keep` drops the later copies in each `release_content_groups` group. Releases that overlap without being identical
  (by their file names, Greenfield's three files share January 21–23, 2025) still repeat rows, and rows can repeat
  within one file (Amador's 9 user-creation rows are 3 distinct ones); `distinct_rows` shows how many.
- `Network Name` is spelled as Flock spells the network, which can differ from `producer` (Amador's and Greenfield's
  networks never equal their producer spelling). Check before filtering on `network = producer`.
- Event times: [semantics.md](semantics.md) §6.1; Riverside County's are read as UTC, unverified. Hotlist entries
  (`Custom Hotlist Entry`) can hold plates in `entity_details`: local only ([pii.md](pii.md)).

**Cite.** Every `event_log` row carries `citation` (`row <src_row>` of the event-log file) and `public_release_id`.

---

### Recipe 11. Re-releases: which releases repeat the same searches, and how they differ

**Question.** Which of a producer's releases hold the same searches, which of those pairs have identical content
(`release_content_groups`), and what differs in the others?

```sql
CREATE OR REPLACE TEMP TABLE cb_release_pairs AS
WITH se AS (SELECT release_id, event_key FROM cache.sighting_event
            WHERE producer = 'San Jose CA PD' AND audit = 'own'),
g AS (SELECT unnest(releases) AS release_id, content_sha256 FROM release_content_groups)
SELECT a.release_id AS release_a, b.release_id AS release_b, count(DISTINCT a.event_key) AS shared_searches,
       coalesce(ga.content_sha256 = gb.content_sha256, false) AS identical_content
FROM se a JOIN se b ON a.event_key = b.event_key AND a.release_id < b.release_id
LEFT JOIN g ga ON ga.release_id = a.release_id
LEFT JOIN g gb ON gb.release_id = b.release_id
GROUP BY ALL;
SELECT p.release_a, p.release_b, p.shared_searches, p.identical_content, ra.n_rows AS rows_a, rb.n_rows AS rows_b
FROM cb_release_pairs p
JOIN truth.releases ra ON ra.release_id = p.release_a
JOIN truth.releases rb ON rb.release_id = p.release_b
ORDER BY p.shared_searches DESC, p.release_a, p.release_b LIMIT 3;
```

```text
mr:187612:2025-09-15__Attachment_2-_Org_Audit_Jun_2024_-_Jun_2025_Redacted.xlsx#Sheet1
  mr:187612:2025-09-16__Attachment_2-_Org_Audit_Jun_2024_-_Jun_2025_Redacted.xlsx#Sheet1      261,384  false  268,047  268,047
mr:187612:2025-09-15__…#Sheet1
  mr:202333:Attachment_3-_Org_Audit_March_2025-Jan_2026_Redacted.xlsx#March- June 2025         64,934  false  268,047   65,135
mr:187612:2025-09-16__…#Sheet1
  mr:202333:Attachment_3-_Org_Audit_March_2025-Jan_2026_Redacted.xlsx#March- June 2025         64,934  false  268,047   65,135
```

0.4 s (only cache columns are read). The same query for Santa Rosa's own-search log finds 13 pairs, all identical.
The first pair above, same size but not identical, compared cell state by cell state:

```sql
SET VARIABLE cb_pair = ['mr:187612:2025-09-15__Attachment_2-_Org_Audit_Jun_2024_-_Jun_2025_Redacted.xlsx#Sheet1',
                        'mr:187612:2025-09-16__Attachment_2-_Org_Audit_Jun_2024_-_Jun_2025_Redacted.xlsx#Sheet1'];
SELECT field, state,
       count(*) FILTER (WHERE release_id = getvariable('cb_pair')[1]) AS first_copy,
       count(*) FILTER (WHERE release_id = getvariable('cb_pair')[2]) AS second_copy
FROM (SELECT release_id, unnest(['reason', 'case', 'name', 'plate']) AS field,
             unnest([reason_state, case_state, name_state, plate_state]) AS state
      FROM sightings WHERE list_contains(getvariable('cb_pair'), release_id))
GROUP BY ALL HAVING first_copy <> second_copy ORDER BY 1, 2;
```

```text
name  redacted_agency  34,525  34,704
name  value           233,522 233,343
```

0.8 s. Which rows changed, row by row (the two copies have the same row count; the event check confirms each row
number holds the same search):

```sql
CREATE OR REPLACE TEMP TABLE cb_changed AS
WITH se AS (SELECT sighting_id, event_key FROM cache.sighting_event
            WHERE list_contains(getvariable('cb_pair'), release_id)),
s AS (SELECT release_id, row_no, name_state, event_key FROM sightings JOIN se USING (sighting_id)
      WHERE list_contains(getvariable('cb_pair'), release_id))
SELECT a.row_no, a.event_key = b.event_key AS same_search, a.name_state AS a_state, b.name_state AS b_state,
       a.release_id AS a_release, b.release_id AS b_release
FROM s a JOIN s b USING (row_no)
WHERE a.release_id = getvariable('cb_pair')[1] AND b.release_id = getvariable('cb_pair')[2];
SELECT same_search, a_state, b_state, count(*) AS rows_ FROM cb_changed
GROUP BY ALL HAVING NOT same_search OR a_state <> b_state ORDER BY rows_ DESC;
```

```text
false  value            value              391
true   value            redacted_agency    179
false  redacted_agency  redacted_agency     65
```

1.6 s. The copy produced a day later redacts 179 more searcher names, at the same rows. The 456 rows with
`same_search = false` are the 456 `x_ambiguous` sightings in each copy: each is its own event, so the two copies of
one row never share an event.

**Caveats.**
- `shared_searches` counts events, so it can be below the row count: repeated rows count once, and `x_ambiguous`
  sightings never match across releases.
- Equal state counts do not prove equal values; compare row by row as above (or by `flock_id` where both copies have
  it).
- Which copy to cite: [provenance.md](provenance.md), "Re-releases: which copy to cite".

**Cite.** Each changed row in both copies:

```python
ch = con.sql("SELECT a_release, row_no, b_release FROM cb_changed WHERE a_state <> b_state ORDER BY row_no").fetchall()
cites = ac.citations_for(con, [(a, n) for a, n, b in ch] + [(b, n) for a, n, b in ch])   # 358 rows, 358 citations, 0.2 s
```

---

### Recipe 12. Following a plate token across agencies (tokens only)

**Question.** Did Ventura PD and Port Hueneme PD search any of the same plates? Work only with tokens from
`sightings_public` ([pii.md](pii.md)); never with `plate_surface`.

```sql
CREATE OR REPLACE TEMP TABLE cb_tok AS
SELECT producer, public_release_id, row_no, t, plate AS token
FROM sightings_public
WHERE producer IN ('Ventura CA PD', 'Port Hueneme CA PD') AND audit = 'own' AND plate_state = 'value';
CREATE OR REPLACE TEMP TABLE cb_shared AS
SELECT token, count(DISTINCT producer) AS producers, count(*) AS rows_, min(t) AS first_t, max(t) AS last_t
FROM cb_tok GROUP BY token HAVING count(DISTINCT producer) > 1;
SELECT (SELECT count(DISTINCT token) FROM cb_tok) AS tokens,
       (SELECT count(*) FROM cb_tok WHERE NOT regexp_full_match(token, 'p1_[0-9a-f]{16}')) AS not_a_token,
       count(*) AS shared_tokens, sum(rows_) AS rows_behind_them
FROM cb_shared;
```

```text
tokens 8,816 | not_a_token 0 | shared_tokens 2 | rows_behind_them 23
```

0.7 s (50 releases, 97,326 rows). `sightings_public` carries `public_release_id` (no zip folder components), not
`release_id`.

**Caveats.**
- `sightings_public` fails loudly without a valid key file; `not_a_token = 0` is the check that every plate came out
  as a token ([pii.md](pii.md), Pre-export check).
- Few logs release plates as values; network audits usually mask other agencies' plates ([semantics.md](semantics.md)
  §11; per-field cell states in [stats.md](stats.md)). "No shared token" means little outside logs that release
  plates. A plate can also sit only in Reason or other free text, as a bracketed token ([pii.md](pii.md), "What a
  token does and does not tell you").
- Tokens are for following, not for identifying. Do not print one next to the released original, which shows the raw
  plate.

**Cite.** Map `public_release_id` back through `release_sources`, then cite:

```python
pairs = con.sql("""SELECT rs.release_id, t.row_no FROM cb_tok t JOIN cb_shared USING (token)
                   JOIN release_sources rs USING (public_release_id)""").fetchall()
ac.citations_for(con, pairs).aggregate(
    "count(*) AS rows_cited, count(DISTINCT release_id) AS releases, count(DISTINCT citation) AS citations").fetchall()
```

```text
rows_cited 23 | releases 9 | citations 23
```

0.1 s. In anything you publish, refer to plates only by token, with the citations ([pii.md](pii.md)).

---

### Recipe 13. Text-prompt (`freeform`) and `visual` searches over time

**Question.** How many of San Francisco PD's own searches were `freeform` (a typed text prompt) or `visual`, by Pacific
month?

```sql
SET VARIABLE cb_rels = (SELECT list(release_id) FROM truth.releases WHERE producer = 'San Francisco CA PD' AND audit = 'own');
SELECT strftime(timezone('America/Los_Angeles', timezone('UTC', date_trunc('hour', t))), '%Y-%m') AS pacific_month,
       count(*) AS rows_,
       count(*) FILTER (WHERE search_type LIKE 'freeform%') AS freeform,
       count(*) FILTER (WHERE search_type LIKE 'visual%') AS visual,
       count(*) FILTER (WHERE nullif(trim(text_prompt), '') IS NOT NULL) AS with_prompt
FROM sightings
WHERE list_contains(getvariable('cb_rels'), release_id)
GROUP BY 1 ORDER BY 1;
```

```text
2025-08 40,172 155   176 155 | 2025-09 33,981 380   309 380 | 2025-10 44,529 267   875 267 | 2025-11 47,424 362   455 362
2025-12 36,810 306   354 306 | 2026-01 42,959 347   324 347 | 2026-02 41,687 347   512 347 | 2026-03 39,093 315   440 315
2026-04 50,707 368   649 368 | 2026-05 36,624 292   799 292 | 2026-06 33,175 180 1,186 180 | 2026-07 44,182 387 1,021 387
```

0.9 s (491,343 rows; no row lacks `t`, so there is no `NULL` month). Flock's moderation verdict is in
`flock_rows."Moderation"`, not in `sightings`:

```sql
SELECT trim("Search Type") AS search_type, "Moderation" AS moderation, count(*) AS rows_
FROM flock_rows
WHERE list_contains(getvariable('cb_rels'), release_id) AND regexp_matches("Search Type", '^\s*(freeform|visual)')
GROUP BY ALL ORDER BY rows_ DESC;
```

```text
visual NULL 7,096 | freeform allow 3,659 | freeform block 44 | visual - Mobile NULL 4 | freeform warn 3
```

0.5 s.

**Caveats.**
- Here the `freeform` rows are exactly the rows with a prompt (3,706; no `freeform` row lacks one and no `visual`
  row has one). What `visual` does is not defined by the export; quote the label ([semantics.md](semantics.md)
  §8–9).
- Rows are searches in this log: its 491,343 cache rows have 491,343 distinct events and no `x_ambiguous` ones
  (`SELECT count(*), count(DISTINCT event_key) FROM cache.sighting_event WHERE producer = 'San Francisco CA PD' AND
  audit = 'own'`, 0.1 s). Check the same way before counting rows as searches elsewhere.
- Prompts are officer-typed free text and can hold civilian details: read them locally, publish only from
  `sightings_public.text_prompt` ([pii.md](pii.md)).
- `flock_rows` reads `"Search Type"` through layout corrections; for other producers check `layout_corrected`
  ([semantics.md](semantics.md) §13).

**Cite.**

```python
pairs = con.sql("""SELECT release_id, row_no FROM sightings
                   WHERE list_contains(getvariable('cb_rels'), release_id) AND search_type LIKE 'freeform%'""").fetchall()
cites = ac.citations_for(con, pairs)          # 3,706 rows, one citation each
```

---

### Recipe 14. Searches by hour of day and day of week

**Question.** When in the Pacific day do Santa Rosa's own searches happen? Count events, because the months produced
twice would otherwise count twice.

```sql
SET VARIABLE cb_rels = (SELECT list(release_id) FROM truth.releases WHERE producer = 'Santa Rosa CA PD' AND audit = 'own');
CREATE OR REPLACE TEMP TABLE cb_hour_ev AS
WITH se AS (SELECT sighting_id, event_key FROM cache.sighting_event
            WHERE list_contains(getvariable('cb_rels'), release_id)),
s AS (SELECT sighting_id, t FROM sightings WHERE list_contains(getvariable('cb_rels'), release_id))
SELECT se.event_key, min(timezone('America/Los_Angeles', timezone('UTC', s.t))) AS t_pacific, count(*) AS rows_
FROM s LEFT JOIN se USING (sighting_id) GROUP BY 1;
SELECT coalesce(sum(rows_) FILTER (WHERE event_key IS NULL), 0) AS unlinked_rows,
       count(*) FILTER (WHERE t_pacific IS NULL) AS no_t,
       count(*) FILTER (WHERE event_key IS NOT NULL) AS searches, sum(rows_) AS rows_
FROM cb_hour_ev;
```

```text
unlinked_rows 0 | no_t 0 | searches 104,713 | rows_ 172,538
```

```sql
SELECT hour(t_pacific) AS pacific_hour, count(*) AS searches FROM cb_hour_ev WHERE event_key IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

```text
00 3,361 | 01 2,602 | 02 1,953 | 03 1,306 | 04 1,131 | 05 1,083 | 06 1,108 | 07 1,981
08 3,776 | 09 5,376 | 10 5,894 | 11 6,308 | 12 5,489 | 13 6,790 | 14 7,319 | 15 7,019
16 6,862 | 17 5,628 | 18 4,944 | 19 4,882 | 20 5,390 | 21 5,350 | 22 4,974 | 23 4,187
```

1.3–1.7 s (172,538 rows).

```sql
SELECT dayname(t_pacific) AS day, count(*) AS searches, count(DISTINCT t_pacific::DATE) AS days_with_searches
FROM cb_hour_ev WHERE event_key IS NOT NULL GROUP BY 1 ORDER BY min(isodow(t_pacific));
```

```text
Monday 15,706 82 | Tuesday 15,445 82 | Wednesday 18,271 83 | Thursday 14,036 83 | Friday 15,362 83
Saturday 15,183 82 | Sunday 10,710 82
```

Under 0.01 s.

**Caveats.**
- Convert before bucketing: read in UTC, the same peak shows up 7–8 hours later on the clock.
- `days_with_searches` counts days with at least one search; a per-day rate needs the calendar days covered, which
  file names do not reliably give (Recipe 1).
- Unlinked rows (no cache row) all land in the one `event_key IS NULL` group; count them from `rows_`, as above. In
  this log there are none.

**Cite.** One hour (04:00–04:59 Pacific):

```python
pairs = con.sql("""SELECT release_id, row_no FROM sightings WHERE list_contains(getvariable('cb_rels'), release_id)
                     AND hour(timezone('America/Los_Angeles', timezone('UTC', t))) = 4""").fetchall()
cites = ac.citations_for(con, pairs)          # 1,763 rows (both copies of twice-produced months), 1.4 s
```

Both copies of a twice-produced month exist; cite one and name the other.

---

### Recipe 15. Rows the parser could not place in time

**Question.** Which rows have no `t`, and what do their released `Search Time` cells hold? The per-producer list is in
[coverage.md](coverage.md) ("Rows the parser could not place in time"); this is how to inspect one release.

```sql
SET VARIABLE cb_rels = (SELECT list(release_id) FROM truth.releases
                        WHERE producer = 'Santa Rosa CA PD' AND audit = 'network' AND release_id LIKE '%2_1_2026-2_28_2026%');
CREATE OR REPLACE TEMP TABLE cb_no_t AS
SELECT release_id, row_no, src_row, search_type, flock_id, layout_corrected FROM sightings
WHERE list_contains(getvariable('cb_rels'), release_id) AND t IS NULL;
SELECT n.release_id, n.search_type, n.layout_corrected,
       regexp_replace(regexp_replace(coalesce(a."Search Time", '<NULL>'), '[0-9]', '9', 'g'), '[A-Za-z]', 'A', 'g') AS released_cell_shape,
       count(*) AS rows_, count(n.flock_id) AS with_uuid, min(n.src_row) AS first_src_row
FROM cb_no_t n JOIN truth.flock_audit_rows a USING (release_id, row_no)
WHERE list_contains(getvariable('cb_rels'), a.release_id)
GROUP BY ALL ORDER BY 1, rows_ DESC;
```

```text
mr:214823:26-996_2026-08-20_232659_-0700.zip!REDACTED_2_1_2026-2_28_2026-Santa Rosa CA PD-Network-Audit.xlsx#2_1_2026-2_28_2026-Santa Rosa C  freeform  true  AAAAA  19  19  5222
mr:214823:26-996_2026-09-01_00_44_52_-0700.zip!REDACTED_2_1_2026-2_28_2026-Santa Rosa CA PD-Network-Audit.xlsx#2_1_2026-2_28_2026-Santa Rosa C  freeform  true  AAAAA  19  19  5222
```

1.7 s (two releases). The five-letter cells are Moderation verdicts (`allow` 16, `block` 3 per copy) sitting under
`Search Time`. A `truth.release_layouts` entry lists these 19 rows: `flock_rows` reads their `Moderation` from that
cell and their `Search Time` as NULL, because no cell holds the time ([semantics.md](semantics.md) §13, §16).

**Caveats.**
- The raw truth cell is what was released; in layout-corrected rows the parsed fields come from the cells
  `truth.release_layouts` names. Read `flock_rows` for the corrected view and check `layout_corrected`.
- These rows carry UUIDs, so they are linked (`1_uuid`), and another log's record of the same search may have the
  time: `ac.drill(con, 'u:' + flock_id)` for the first one finds its copy in Rohnert Park's network audit, with `t`.
- Drop `t IS NULL` rows from time-based counts and say how many you dropped.

**Cite.** `ac.citations_for(con, con.sql("SELECT release_id, row_no FROM cb_no_t").fetchall())`: 38 rows, each locator
`row <src_row>; cells released under other column labels (see release_layouts)` (0.2 s).
