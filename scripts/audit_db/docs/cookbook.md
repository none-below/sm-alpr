# Cookbook: worked queries

For engineers who write their own queries against this database and need every result to be citable. Fifteen
research questions, each answered with code that was run against this build, what it cost, where the answer can
mislead, and how to cite every row behind it. Where a recipe shows output, it is either an aggregate or non-PII value
from this build or, marked "Shape of the output", the output's columns with placeholder values. Field meanings and
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
| One raw column over every row of `truth.flock_audit_rows` (not parsed; pre-correction) | `trim("Org Name") = '<org>'`: 0.6 s |
| `ac.sightings_for(con, pairs)` / `ac.citations_for(con, pairs)` on `(release_id, row_no)` pairs from any number of releases | tens of thousands of rows from a few dozen releases parsed in under 4 s; a few thousand citations in 0.3 s |
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
| 2 | Every search by one organization in one month, in every log, one row per search, cited | one organization, one month | 3–9 s |
| 3 | Which organizations searched a producer's cameras, by month | Mountain View, November 2025 | 1.8–4.7 s |
| 4 | Classifying the organizations in a network audit by a state code in their name | one producer's network audit | varies |
| 5 | One field's cell state in two logs of the same searches | two network audits | varies |
| 6 | San Mateo PD's own log from the PDFs: months, searches, repeated ids | all 34 PDFs | 1.2 s |
| 7 | One search's Reason in an own-search log and in another producer's network audit | two producers, one month | varies |
| 8 | One Flock search (UUID) in every log, with citations | one LAPD search, 15 sightings | 1.0 s |
| 9 | Case-number formats in one release | one network-audit sheet | ≤ 0.6 s per block |
| 10 | Event logs: network-sharing changes, user creation and deletion | one producer; all event logs | ≤ 0.2 s |
| 11 | Re-releases: which releases repeat the same searches, and how they differ | one producer's own-search log | ≤ 1.6 s per block |
| 12 | Following a plate token across agencies (tokens only) | two own-search logs | varies |
| 13 | Text-prompt (`freeform`) and `visual` searches over time | one own-search log | 0.9 s |
| 14 | Searches by hour of day and day of week | one own-search log | 1.3–1.7 s |
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
- A release can be attributed to a producer other than the requesting agency by a `producers.json` file override;
  the basis is quoted in `truth.releases.producer_source`. Read it before attributing a file.
- `release_content_groups` misses same-content files in another format or row order ([semantics.md](semantics.md)
  §14; Recipe 11).

**Cite.** A release is cited by its `release_sources` row: `public_release_id`, `request_label`, `request_url`,
`document`, `sheet`, and `link` (download URL with the SHA-256 of the file and, inside a zip, of the member)
([provenance.md](provenance.md)).

---

### Recipe 2. Every search by one organization in one month, in every log, one row per search, cited

**Question.** Every search one organization (`<org>`) ran in one Pacific month (here March 2026) that any loaded log
recorded, one row per search, with how many logs have it and a citation.

Step 1: the organization's rows in every log, found without parsing. `org` is the trimmed `Org Name` cell, the producer
for an own-search log with no `Org Name` column, or San Mateo PD for its PDFs; no layout correction remaps `Org Name`.
`raw_t` is the same expression `sightings` uses, on the raw cell.

```sql
SET VARIABLE cb_org  = '<org>';                         -- as spelled in Org Name (see Caveats)
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
rows_ n | releases n                                   (shape only)
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
rows_ n | releases n                                   (shape only)
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
rows_ n | releases n | logs n                          (shape only)
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
rows_ n | unlinked_rows n | searches n | in_own_log n | in_one_log n | x_ambiguous n     (shape only)
```

```sql
SELECT weakest_link, count(*) AS searches FROM cb_searches GROUP BY 1 ORDER BY 1;
```

```text
1_uuid n | 2_k5 n | 3_k3 n | x_ambiguous n           (shape only: one entry per tier present)
```

0.9 s. `weakest_link` is `max(basis)` (tier labels sort strongest first). The `3_k3` searches are those with a copy
in a network audit with neither UUIDs nor Time Frame, matched on org, second and network count; the other logs in
scope link by UUID (`1_uuid`) or, where an export has no UUID column, by `2_k5`.

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

Shape of the output (placeholder values):

```text
u:<uuid>  <t>  <logs>  <weakest_link>  <locator>
  '<citation: producer, request, document, page or row, download link, SHA-256>'
```

0.3 s, one distinct citation per search. The whole recipe took 3–9 s for the organizations tried; it grows with the
organization's rows across all logs (step 1 reads every row once).

**Caveats.**
- File names would have given the wrong month. For March 2026 the releases in scope include files named for February
  (Port Hueneme's `2_1_2026-3_2_2026` file and Los Altos's February sheet run into March), and March files hold
  April rows.
- "Searches" are events. The `x_ambiguous` ones are network-audit rows whose link key matches more than one search:
  each counts alone even if it repeats a search already counted; compare with the organization's own log where there
  is one. Say "at least N searches in the logs released to date" ([semantics.md](semantics.md) §17).
- Check spellings before step 1: exports can spell the org differently from the registry (Mountain View's rows read
  `Mountain View CA PD (Santa Clara County)`, [semantics.md](semantics.md) §5). A search recorded only under another
  spelling is missed.
- All months: skip steps 2–5 and count from the cache alone. `SET VARIABLE cb_all = (SELECT list(DISTINCT release_id)
  FROM cb_org_rows)`, then `cache.sighting_event SEMI JOIN cb_org_rows USING (release_id, row_no) WHERE
  list_contains(getvariable('cb_all'), release_id)`, and report the `x_ambiguous` events it contains, as in step 4.
  Parse month by month.

**Cite.** Step 5: `cb_cites.citation` per search. Every row behind a search: `cb_month_ev` holds every
`(release_id, row_no)` pair; pass them to `ac.citations_for`.

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

Shape of the output (placeholder values):

```text
pacific_month  orgs  rows_  unlinked_rows  searches
YYYY-MM           n      n              n         n
…                                                      (one line per Pacific month in the parsed files)
```

```sql
SELECT pacific_month, org, rows_, searches FROM cb_by_org WHERE pacific_month = '2025-11'
ORDER BY searches DESC, org LIMIT 3;
```

```text
YYYY-MM  <org>  n  n                                   (shape only)
```

1.8–4.7 s over two runs (six releases, 964,158 rows). Only November is complete in this scope: September and October need the files
before them, December's file ends on December 18.

**Caveats.**
- Where November's rows come from: most from the two November files, a few from the two December files (the
  December file starts at 22:01 on November 30 Pacific); some rows of the November files are October. A `pacific_month
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
cites = ac.citations_for(con, pairs)          # one citation per row
```

Cite one release of each identical pair and add "also produced in …" ([provenance.md](provenance.md), Re-releases).

---

### Recipe 4. Classifying the organizations in a network audit by a state code in their name

**Question.** In one producer's network audit, which organizations carry a state code in their name, and when does
each last appear in the scope you chose?

Aggregate per organization first (parsing is the cost), then classify the few thousand names:

```sql
SET VARIABLE cb_rels = (SELECT list(release_id) FROM truth.releases
                        WHERE producer = '<producer>' AND audit = 'network'
                          AND regexp_matches(release_id, '<file-name pattern for the months in scope>'));
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

Shape of the output (placeholder values):

```text
home           last_seen  orgs  rows_  latest_utc
california     YYYY-MM       n      n  YYYY-MM-DD hh:mm:ss
no_state_code  YYYY-MM       n      n  YYYY-MM-DD hh:mm:ss
other_state    YYYY-MM       n      n  YYYY-MM-DD hh:mm:ss
```

The row behind one organization's last appearance:

```python
org, last_t, rid, n = con.sql("""SELECT org, last_t, last_release_id, last_row_no FROM cb_org_home
                                 WHERE home = '<class>' ORDER BY last_t DESC LIMIT 1""").fetchone()
ac.citations_for(con, [(rid, n)]).select("src_row, document, citation").fetchone()
```

**Caveats.**
- The state comes from the name, a heuristic. `SD` in `Los Angeles County CA SD` is a department (caught by `CA`);
  `CO` can be Colorado or "county". `no_state_code` holds names with no code, such as agencies spelled out in full
  (`Cal Fire`) and regional centers (`NCRIC`). Read the `cb_org_home` rows by hand before quoting a count.
- "Last seen in this log" is not "stopped searching": a network audit shows only searches that covered this
  producer's cameras. Say which network audits you checked, and repeat per producer before generalizing.
- A last-seen month at the end of the scope says nothing about later months: widen the scope.

**Cite.** `last_release_id` / `last_row_no` per org, through `ac.citations_for` as above. For "no rows of a class
after date D", cite the release(s) covering the later period and say how many rows they hold.

---

### Recipe 5. One field's cell state in two logs of the same searches

**Question.** For the searches two network audits share, how does one field's cell state in the first log compare
with the other log's (a mask in one and a value in the other, both masked, both values)?

`read_field('reason', who := …)` compares a producer against every other log; a pairwise comparison scoped to two
producers is cheaper and keeps both row keys:

```sql
SET VARIABLE cb_here  = (SELECT list(release_id) FROM truth.releases WHERE producer = '<producer A>' AND audit = 'network');
SET VARIABLE cb_there = (SELECT list(release_id) FROM truth.releases WHERE producer = '<producer B>' AND audit = 'network');
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

Shape of the output (placeholder values):

```text
state_here  state_there  searches  row_pairs
<state>     <state>             n          n
…                                               (one line per pair of states)
```

Report the tiers behind the pairs too (`SELECT weakest_link, count(DISTINCT event_key) FROM cb_pairs GROUP BY 1`):
logs without UUIDs link only on `2_k5` or weaker.

**Caveats.**
- Two logs share a search only if both cover its day and it touched both producers' cameras. The pairs are that
  overlap, not all of either producer's searches: say which periods the two logs cover.
- `value` is not proof the text is a genuine reason ([semantics.md](semantics.md) §10). Look at the distinct values
  locally before counting, and check each quoted value in both originals.
- `row_pairs` exceeds `searches` where one side holds the same search twice. A `4_k5_group` match joins rows that
  share org, second, networks and time frame but no UUID: name the tier ([linking.md](linking.md)).
- `divergence()` and `read_field` compare exact, case-sensitive strings; see Recipe 7.

**Cite.** Both rows, always:

```python
p = con.sql("""SELECT here_release, here_row, there_release, there_row FROM cb_pairs
               WHERE state_here <> state_there
               ORDER BY here_release, here_row LIMIT 1""").fetchone()
ac.citations_for(con, [(p[0], p[1]), (p[2], p[3])]).select("src_row, document, citation").fetchall()
```

(`citations_for` returns rows ordered by `release_id`, `row_no`.)

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

Shape of the output (placeholder values):

```text
pacific_month  rows_  searches  events  unlinked  pdfs
YYYY-MM            n         n       n         n     n
…                                                        (one line per Pacific month with a loaded PDF)
```

1.2 s. Each PDF's rows fall inside its own Pacific month. A month with no line has no loaded PDF: none was produced
to date, or its PDF has no text layer. Releases loaded with no rows, and why:

```sql
SELECT regexp_extract(release_id, '[^:]+$') AS pdf, n_rows, header_basis FROM truth.releases
WHERE producer = 'San Mateo CA PD' AND n_rows = 0;
```

Search ids printed more than once, and rows read by printed position:

```sql
SELECT count(*) AS ids_printed_more_than_once, sum(n) - count(*) AS extra_rows,
       count(*) FILTER (WHERE pdfs > 1) AS in_two_pdfs
FROM (SELECT flock_id, count(*) AS n, count(DISTINCT release_id) AS pdfs FROM cb_smpd_rows GROUP BY 1 HAVING count(*) > 1);
```

```sql
SELECT regexp_extract(release_id, '[^:]+$') AS pdf, count(*) AS rows_with_parse_note
FROM truth.smpd_pdf_rows WHERE parse_note IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1;
```

**Caveats.**
- Count searches with `count(DISTINCT flock_id)` or `count(DISTINCT event_key)`, never rows: a search id can be
  printed more than once. A search printed in two PDFs would likewise be two sightings of one event.
- `parse_note` rows were read by printed position, because the page's text order disagreed with the printed row
  ([provenance.md](provenance.md), [semantics.md](semantics.md)). Check the page before quoting one, and quote the PDF.
- The PDFs have no Org Name, Case #, plate or Time Frame column: `case_state` and `plate_state` are `not_exported`.
  They do print the search UUID, so every PDF row is linked `1_uuid`.
- Missing months are missing from what was produced to date, not evidence of no searches ([semantics.md](semantics.md)
  §17).

**Cite.** A PDF row is cited by file and page; there is no `src_row`:

```python
pairs = con.sql("""SELECT release_id, row_no FROM sightings WHERE list_contains(getvariable('cb_smpd'), release_id)
                   ORDER BY release_id, row_no LIMIT 2""").fetchall()
ac.citations_for(con, pairs).select("row_no, locator, link").fetchall()
```

Shape of the output (placeholder values):

```text
<row_no>  page <n> (search id <uuid>)  Document: https://github.com/none-below/sm-alpr/blob/<commit>/…/<pdf name> (SHA-256 <hash>)
```

Two copies of one search id on one page get the same locator; `truth.smpd_pdf_rows.src_line` tells them apart.

---

### Recipe 7. One search's Reason in an own-search log and in another producer's network audit

**Question.** For one producer's searches in one month, does its own log give the same Reason as another producer's
network audit? Where both carry Flock UUIDs, they join on `flock_id`. Set `cb_from` / `cb_to` to the month's UTC
bounds as in Recipe 2.

```sql
SET VARIABLE cb_own   = (SELECT list(release_id) FROM truth.releases WHERE producer = '<producer A>' AND audit = 'own');
SET VARIABLE cb_other = (SELECT list(release_id) FROM truth.releases
                         WHERE producer = '<producer B>' AND audit = 'network'
                           AND regexp_matches(release_id, '<file-name pattern for the month and its neighbours>'));
CREATE OR REPLACE TEMP TABLE cb_cmp AS
WITH own AS (SELECT release_id, row_no, flock_id, reason_state, reason FROM sightings
             WHERE list_contains(getvariable('cb_own'), release_id)
               AND t >= getvariable('cb_from') AND t < getvariable('cb_to')),
other AS (SELECT release_id, row_no, flock_id, reason_state, reason FROM sightings
          WHERE list_contains(getvariable('cb_other'), release_id) AND org = '<producer A>')
SELECT flock_id, own.release_id AS own_release, own.row_no AS own_row, other.release_id AS other_release,
       other.row_no AS other_row, own.reason_state AS own_state, other.reason_state AS other_state,
       CASE WHEN own.reason = other.reason THEN 'identical'
            WHEN lower(trim(own.reason)) = lower(trim(other.reason)) THEN 'same after trimming and case'
            ELSE 'different' END AS comparison
FROM other JOIN own USING (flock_id);
SELECT own_state, other_state, comparison, count(DISTINCT flock_id) AS searches, count(*) AS row_pairs
FROM cb_cmp GROUP BY ALL ORDER BY searches DESC;
```

Shape of the output (placeholder values):

```text
own_state  other_state  comparison                    searches  row_pairs
value      value        identical                            n          n
value      value        same after trimming and case         n          n
value      value        different                            n          n
```

**Caveats.**
- An exact comparison can mislead: two exports of one search can differ only in letter case, spacing or trailing
  punctuation (a made-up example: `Stolen vehicle` in one log, `stolen vehicle ` in the other). `divergence()` and
  `read_field` compare exactly, so they would label such a pair `differs_from_other_logs`. Normalize on purpose, and
  say how.
- The data does not say why two exports of one search differ; quote both, with both citations.
- Rows with a `parse_note` (San Mateo PD's PDFs, Recipe 6) were read by printed position; check the page before
  quoting one.

**Cite.** Both sides:

```python
p = con.sql("""SELECT own_release, own_row, other_release, other_row FROM cb_cmp
               WHERE comparison = 'different' ORDER BY own_release, own_row LIMIT 1""").fetchone()
ac.citations_for(con, [(p[0], p[1]), (p[2], p[3])]).select("locator, document, citation").fetchall()
```

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

Shape of the output (placeholder values):

```text
<producer>  <basis>  <src_row>  <reason_state>  <sha256, 12 hex>
…                                                  (one line per sighting: 15 for this search)
```

`event` 0.4 s, `drill` 1.0 s. Each dict also has `org`, `t`, `nets`, the four `*_state` columns, `public_release_id`,
`citation` and `open_url`; `drill(…, surfaces=True)` adds the released Reason and Case # (local only). From the shell:
`uv run --locked --project <code> python <code>/audit_client.py u:6023db79-9334-4b07-8b35-7f6e18366971`. In SQL, the same rows:

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
- 15 sightings, 8 producers, 0 own-search logs: none of these is the searcher's record. One file can hold a search
  that falls just past its named month, and a re-released file adds another copy. Cell states can differ between
  copies (`value` in one, `redacted_agency` in another), so read `reason_state` per copy and cite the copy you quote.
- Tiers differ per copy: `1_uuid` where the export has the UUID, `2_k5` where it was matched on org, second, networks
  and time frame, `3_k3` on org, second and networks. Name the tier when you rely on a match ([linking.md](linking.md)).
- Only `u:` event ids are stable across builds; `k5:`, `k3:` and `x:` ids are build-specific. Persist
  `(release_id, row_no)` instead ([linking.md](linking.md)).
- A UUID found in no log returns `None` / no rows, which says only that no loaded log recorded it
  ([semantics.md](semantics.md) §17).

**Cite.** `r["citation"]` per copy from `ac.drill`, or `ss.citation` above; `sha256` is the hash to check each
download against ([provenance.md](provenance.md)).

---

### Recipe 9. Case-number formats in one release

**Question.** What formats do the Case # values in one release take, and which rows have a given format?

Many network audits mask other agencies' Case #; some release them. Shape census for one sheet:

```sql
SET VARIABLE cb_rid = '<release_id, copied from truth.releases>';
SELECT regexp_replace(regexp_replace(case_no, '[0-9]', '9', 'g'), '[A-Za-z]', 'A', 'g') AS shape,
       count(*) AS rows_, count(DISTINCT org) AS orgs
FROM sightings WHERE release_id = getvariable('cb_rid') AND case_state = 'value'
GROUP BY 1 ORDER BY rows_ DESC LIMIT 5;
```

Shape of the output (placeholder values):

```text
shape        rows_  orgs
99-99999         n     n
99999999         n     n
…
```

Sheet names are kept as stored, so a `release_id` can end in a space (Excel's 31-character cap cuts sheet names
mid-word). Copy ids from `truth.releases`, never retype them.

The rows of one format, kept by `(release_id, row_no)`:

```sql
CREATE OR REPLACE TEMP TABLE cb_hits AS
SELECT release_id, row_no, org FROM sightings
WHERE release_id = getvariable('cb_rid') AND case_state = 'value'
  AND regexp_matches(case_no, '<regular expression for the format>');
SELECT org, count(*) AS rows_ FROM cb_hits GROUP BY 1 ORDER BY 2 DESC, 1;
```

**Caveats.**
- A shape is not an attribution. That a value is laid out like one agency's numbering does not show that agency
  opened the case, took part in the search, or received results; other agencies can use the same layout. Describe
  the format, and confirm with the agency or other records before saying more.
- Case # is free text typed by the searcher. Look at the flagged rows locally (in `sightings`, not in anything you
  publish) before counting; some shapes are plates ([pii.md](pii.md): `9AAA999` "case numbers" are tokenized in
  `sightings_public`).
- Another sheet or producer masks or formats Case # differently; the census is per release.

**Cite.** One release, so `sighting_sources` with the literal filter is enough:

```sql
SELECT ss.src_row, ss.citation
FROM cb_hits h JOIN sighting_sources ss USING (release_id, row_no)
WHERE ss.release_id = getvariable('cb_rid')
ORDER BY ss.src_row LIMIT 1;
```

---

### Recipe 10. Event logs: network-sharing changes, user creation and deletion

**Question.** When did `<producer>` add and remove network shares, and with whom? Which producers' event logs record
user creation and deletion?

`event_log` has 11,608 rows, so these read it whole. `Entity Details` of a `networkShare` row is three lines,
`Network Name: …`, `Permissions: …`, `Receiving Organization: …`: the network whose cameras are shared, what the
receiver may do, and who receives access.

```sql
CREATE OR REPLACE TEMP TABLE cb_shares AS
WITH keep AS (   -- one release per identical-content group
  SELECT release_id FROM truth.releases
  WHERE producer = '<producer>' AND audit = 'event'
    AND release_id NOT IN (SELECT unnest(releases[2:]) FROM release_content_groups))
SELECT release_id, row_no, ts, event_type,
       trim(regexp_extract(entity_details, 'Network Name: ([^\n]*)', 1)) AS network,
       trim(regexp_extract(entity_details, 'Receiving Organization: ([^\n]*)', 1)) AS receiver,
       trim(regexp_extract(entity_details, 'Permissions: ([^\n]*)', 1)) AS permissions,
       citation
FROM event_log
WHERE release_id IN (SELECT release_id FROM keep) AND entity_type = 'networkShare';
SELECT strftime(timezone('America/Los_Angeles', timezone('UTC', ts)), '%Y') AS pacific_year,
       network = '<network name>' AS own_network, event_type,   -- the producer's own network, as Flock spells it
       count(*) AS shares, count(DISTINCT receiver) AS receivers, count(DISTINCT ts) AS timestamps
FROM cb_shares GROUP BY ALL ORDER BY 1, 2, 3;
```

Shape of the output (placeholder values):

```text
pacific_year  own_network  event_type  shares  receivers  timestamps
YYYY          true|false   create      n       n          n
YYYY          true|false   delete      n       n          n
```

```sql
SELECT ts, event_type, receiver, permissions, split_part(citation, ' Download:', 1) AS citation
FROM cb_shares WHERE event_type = 'delete' AND network = '<network name>'
ORDER BY ts DESC LIMIT 2;
```

Shape of the output (placeholder values):

```text
<ts>  delete  <receiving organization>  <permissions>  '<producer>. MuckRock request <n> (…), produced <date>. <file>, row <n>.'
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

Shape of the output (placeholder values):

```text
producer    event_type  rows_  distinct_rows  first_utc   last_utc
<producer>  create      n      n              YYYY-MM-DD  YYYY-MM-DD
<producer>  delete      n      n              YYYY-MM-DD  YYYY-MM-DD
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
  within one file (one user creation listed more than once); `distinct_rows` shows how many.
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
            WHERE producer = '<producer>' AND audit = 'own'),
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

Shape of the output (placeholder values):

```text
<release_a>
  <release_b>    shared_searches n  identical_content true|false  rows_a n  rows_b n
…
```

Only cache columns are read, so this is fast. The same query for Santa Rosa's own-search log finds 13 pairs, all
identical. A pair of the same size that is not identical, compared cell state by cell state:

```sql
SET VARIABLE cb_pair = ['<release_a>', '<release_b>'];
SELECT field, state,
       count(*) FILTER (WHERE release_id = getvariable('cb_pair')[1]) AS first_copy,
       count(*) FILTER (WHERE release_id = getvariable('cb_pair')[2]) AS second_copy
FROM (SELECT release_id, unnest(['reason', 'case', 'name', 'plate']) AS field,
             unnest([reason_state, case_state, name_state, plate_state]) AS state
      FROM sightings WHERE list_contains(getvariable('cb_pair'), release_id))
GROUP BY ALL HAVING first_copy <> second_copy ORDER BY 1, 2;
```

Shape of the output (placeholder values):

```text
<field>  <state>  first_copy n  second_copy n        (one line per field and state whose counts differ)
```

Which rows changed, row by row, for one field the previous query listed (here `name_state`). This assumes the two
copies have the same row count; the event check confirms each row number holds the same search:

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

Rows with `same_search = false` are `x_ambiguous` sightings: each is its own event, so the two copies of one row
never share an event. Rows with `same_search` true and two different states are the cells that changed between the
copies.

**Caveats.**
- `shared_searches` counts events, so it can be below the row count: repeated rows count once, and `x_ambiguous`
  sightings never match across releases.
- Equal state counts do not prove equal values; compare row by row as above (or by `flock_id` where both copies have
  it).
- Which copy to cite: [provenance.md](provenance.md), "Re-releases: which copy to cite".

**Cite.** Each changed row in both copies:

```python
ch = con.sql("SELECT a_release, row_no, b_release FROM cb_changed WHERE a_state <> b_state ORDER BY row_no").fetchall()
cites = ac.citations_for(con, [(a, n) for a, n, b in ch] + [(b, n) for a, n, b in ch])
```

---

### Recipe 12. Following a plate token across agencies (tokens only)

**Question.** Did two producers search any of the same plates? Work only with tokens from `sightings_public`
([pii.md](pii.md)); never with `plate_surface`.

```sql
CREATE OR REPLACE TEMP TABLE cb_tok AS
SELECT producer, public_release_id, row_no, t, plate AS token
FROM sightings_public
WHERE producer IN ('<producer A>', '<producer B>') AND audit = 'own' AND plate_state = 'value';
CREATE OR REPLACE TEMP TABLE cb_shared AS
SELECT token, count(DISTINCT producer) AS producers, count(*) AS rows_, min(t) AS first_t, max(t) AS last_t
FROM cb_tok GROUP BY token HAVING count(DISTINCT producer) > 1;
SELECT (SELECT count(DISTINCT token) FROM cb_tok) AS tokens,
       (SELECT count(*) FROM cb_tok WHERE NOT regexp_full_match(token, 'p1_[0-9a-f]{16}')) AS not_a_token,
       count(*) AS shared_tokens, sum(rows_) AS rows_behind_them
FROM cb_shared;
```

Shape of the output (placeholder values; `not_a_token` must be 0):

```text
tokens n | not_a_token 0 | shared_tokens n | rows_behind_them n
```

`sightings_public` carries `public_release_id` (no zip folder components), not `release_id`.

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

Shape of the output (placeholder values): `rows_cited n | releases n | citations n`, with `citations` equal to
`rows_cited`.

In anything you publish, refer to plates only by token, with the citations ([pii.md](pii.md)).

---

### Recipe 13. Text-prompt (`freeform`) and `visual` searches over time

**Question.** How many of `<producer>`'s own searches were `freeform` (a typed text prompt) or `visual`, by Pacific
month?

```sql
SET VARIABLE cb_rels = (SELECT list(release_id) FROM truth.releases WHERE producer = '<producer>' AND audit = 'own');
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
pacific_month  rows_  freeform  visual  with_prompt
YYYY-MM            n         n       n            n        (shape only: one line per Pacific month)
```

0.9 s. A `NULL` month line would hold rows with no `t`. Flock's moderation verdict is in
`flock_rows."Moderation"`, not in `sightings`:

```sql
SELECT trim("Search Type") AS search_type, "Moderation" AS moderation, count(*) AS rows_
FROM flock_rows
WHERE list_contains(getvariable('cb_rels'), release_id) AND regexp_matches("Search Type", '^\s*(freeform|visual)')
GROUP BY ALL ORDER BY rows_ DESC;
```

```text
<search type>  <moderation>  <rows>        (shape only: one line per search type and verdict)
```

0.5 s.

**Caveats.**
- Compare `freeform` with `with_prompt` before treating either as the other: a `freeform` row can lack a prompt, and
  a row of another type can carry one. What `visual` does is not defined by the export; quote the label ([semantics.md](semantics.md)
  §8–9).
- Rows are searches only if the log's cache rows have as many distinct events and no `x_ambiguous` ones
  (`SELECT count(*), count(DISTINCT event_key) FROM cache.sighting_event WHERE producer = '<producer>' AND
  audit = 'own'`, 0.1 s). Check before counting rows as searches.
- Prompts are officer-typed free text and can hold civilian details: read them locally, publish only from
  `sightings_public.text_prompt` ([pii.md](pii.md)).
- `flock_rows` reads `"Search Type"` through layout corrections; check `layout_corrected`
  ([semantics.md](semantics.md) §13).

**Cite.**

```python
pairs = con.sql("""SELECT release_id, row_no FROM sightings
                   WHERE list_contains(getvariable('cb_rels'), release_id) AND search_type LIKE 'freeform%'""").fetchall()
cites = ac.citations_for(con, pairs)          # one citation per row
```

---

### Recipe 14. Searches by hour of day and day of week

**Question.** When in the Pacific day do `<producer>`'s own searches happen? Count events, because a month produced
twice would otherwise count twice.

```sql
SET VARIABLE cb_rels = (SELECT list(release_id) FROM truth.releases WHERE producer = '<producer>' AND audit = 'own');
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
unlinked_rows n | no_t n | searches n | rows_ n           (shape only)
```

```sql
SELECT hour(t_pacific) AS pacific_hour, count(*) AS searches FROM cb_hour_ev WHERE event_key IS NOT NULL
GROUP BY 1 ORDER BY 1;
```

```text
00 n | 01 n | … | 23 n                                  (shape only: one entry per Pacific hour)
```

1.3–1.7 s.

```sql
SELECT dayname(t_pacific) AS day, count(*) AS searches, count(DISTINCT t_pacific::DATE) AS days_with_searches
FROM cb_hour_ev WHERE event_key IS NOT NULL GROUP BY 1 ORDER BY min(isodow(t_pacific));
```

```text
Monday n n | Tuesday n n | … | Sunday n n               (shape only: searches, days_with_searches)
```

Under 0.01 s.

**Caveats.**
- Convert before bucketing: read in UTC, the same peak shows up 7–8 hours later on the clock.
- `days_with_searches` counts days with at least one search; a per-day rate needs the calendar days covered, which
  file names do not reliably give (Recipe 1).
- Unlinked rows (no cache row) all land in the one `event_key IS NULL` group; count them from `rows_`, as above.

**Cite.** One hour (14:00–14:59 Pacific):

```python
pairs = con.sql("""SELECT release_id, row_no FROM sightings WHERE list_contains(getvariable('cb_rels'), release_id)
                     AND hour(timezone('America/Los_Angeles', timezone('UTC', t))) = 14""").fetchall()
cites = ac.citations_for(con, pairs)          # both copies of twice-produced months
```

Where a month was produced twice, both copies exist; cite one and name the other.

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

1.7 s (two releases). The five-letter cells are Moderation verdicts (such as `allow` / `block`) sitting under
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
