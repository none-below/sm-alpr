# One search, many logs: sightings and events

For engineers who count searches, follow one search across agencies' logs, or compare what different logs released
for it, and who have to say how confident a cross-agency claim is. It covers how `build_derived.py` links rows into
searches (keys, tiers, ambiguity), `events` and `event()`, how to measure and state the reliability of a link,
cross-log comparison with `read_field`, the drill-down that proves "log A says X, log B says Y" with a citation for
each side (`event_sightings`, `audit_client.drill`), query costs, and the traps. Column dictionaries are in
[derived.md](derived.md), what the fields mean is in [semantics.md](semantics.md), citing rows is in
[provenance.md](provenance.md), and civilian data rules are in [pii.md](pii.md). Corpus-wide numbers (tier counts,
linking precision, searches by number of logs) are in [stats.md](stats.md), which is generated on each build, and the
loaded logs are listed in [coverage.md](coverage.md).

## Before you start

- Use the standard session from [README.md](README.md). SQL blocks run as written via `con.sql("""…""")` or in the
  DuckDB CLI. Python blocks also need `audit_client`, which is in `<code>`:

  ```python
  import sys; sys.path.insert(0, "<code>")   # scripts/audit_db, as in the standard session
  import audit_client as ac                  # ac.connect() opens the same session and caps the spill directory
  ```

- Outputs are as of the truth build in `truth.build_info` (`built_at_utc` 2026-09-26T20:38:17Z, repo inputs
  `e138455bb`, DuckDB 1.5.5) and the cache build in `cache.builds` (2026-09-26 20:42 UTC). Any rebuild can change event
  ids other than `u:` ids, and can change tiers and counts ([Pitfalls](#10-pitfalls)).
- Timings are from the standard session (4 threads, 4 GB) under `nice -n 19 taskpolicy -b`, with other sessions
  querying the same files (load average 5–7). The first query in a fresh process is slower. On a shared machine use
  `ac.connect(threads=1, memory="1GB")` and expect several times longer.
- The linking cache is valid only for the truth, linking code and DuckDB version it was built from. Run
  `check_cache.py` first (under a second; see [derived.md](derived.md), "Staleness").

## Contents

1. [Rows, sightings, events](#1-rows-sightings-events)
2. [Which rows are linked, and the keys](#2-which-rows-are-linked-and-the-keys)
3. [The linking algorithm](#3-the-linking-algorithm)
4. [Every tier in one producer's logs](#4-every-tier-in-one-producers-logs)
5. [`events` and `event()`: one row per linked search](#5-events-and-event-one-row-per-linked-search)
6. [How reliable a link is](#6-how-reliable-a-link-is)
7. [Comparing logs: `read_field`](#7-comparing-logs-read_field)
8. [Proving "log A says X, log B says Y"](#8-proving-log-a-says-x-log-b-says-y)
9. [Query patterns and costs](#9-query-patterns-and-costs)
10. [Pitfalls](#10-pitfalls)

## 1. Rows, sightings, events

| Level | What it is | Where | Key |
|---|---|---|---|
| release | One released file, zip member, sheet, or (San Mateo PD) PDF | `truth.releases`, `release_sources` | `release_id` |
| row / sighting | One released row. Parsed, it is one log's record of one search | `truth.flock_audit_rows`, `truth.smpd_pdf_rows` → `sightings` | (`release_id`, `row_no`); `sighting_id` = `hash(release_id, row_no)` |
| event | One search, linked across logs, productions and repeated rows | `cache.sighting_event` (sighting → `event_id`, `basis`); `events`, `event(eid)` | `event_id`; `event_key` = `hash(event_id)` |

A Flock search is exported into the searcher's own-search log and into the network audit of every producer whose
cameras it covered. It is exported again in every re-release of those logs ([semantics.md](semantics.md) §3–§4,
§14–§15). Linking gives all those rows one `event_id`. Nothing is merged or deleted: each sighting keeps its own row
and its own citation, and the event is only a label on it.

**San Mateo PD's log has one release per PDF** (`smpd:<request folder>:<pdf name>`), with one row per search-id block
as printed. A search printed twice, in two PDFs or twice in one PDF, is two sightings of one event. Count distinct
`event_key` for searches and `n_logs` for agencies, never rows or `n_sightings`. In this build every repeat is inside
one PDF:

```sql
SELECT n_sightings, n_releases, count(*) AS searches FROM (
  SELECT event_key, count(*) AS n_sightings, count(DISTINCT release_id) AS n_releases
  FROM cache.sighting_event WHERE producer = 'San Mateo CA PD' GROUP BY 1)
WHERE n_sightings > 1 GROUP BY ALL ORDER BY ALL;
-- 2 1 141 | 3 1 16 | 4 1 2                                                             (0.2 s)
```

`event_id` has four forms:

| Form | Assigned by tiers | Stable across rebuilds? |
|---|---|---|
| `u:<Flock search UUID>` | `1_uuid`, `2_k5`, `3_k3`, `3b_k3_to_tf_less_uuid` | Yes, as long as the same link is made. The UUID is a released value: Flock's `ID` column, or San Mateo PD's printed search id |
| `k5:<k5>` | `4_k5_group`, `5_k3_to_group` | No. The value is a DuckDB `hash()` (it changes with the DuckDB version), and the group can gain a UUID when a new log is loaded |
| `k3:<k3>` | `6_k3_group` | No, for the same reasons |
| `x:<sighting_id>` | `x_ambiguous` | No. The value is a DuckDB `hash()` of the row key |

San Mateo PD's printed search id is the Flock search UUID. Take the 200 searches in the first 200 rows of its April 2026
PDF (`smpd:W012818-053026:4_1_2026-4_30_2026-San_Mateo_CA_PD-Audit__1_.pdf`). For 157 of them, the same value appears
in the `ID` column of the network audits of Riverside County, Rohnert Park, San Francisco, Santa Rosa, Ukiah and Ukiah
Fire, all as `1_uuid` sightings in the same events.

## 2. Which rows are linked, and the keys

`cache.sighting_keys` is built once from `sightings` (both branches), with this filter and these keys:

```sql
-- build_derived.py, layer 3
SELECT sighting_id, release_id, row_no, producer, audit, flock_id, tf_start IS NOT NULL AS has_tf,
       CASE WHEN t IS NOT NULL AND org IS NOT NULL THEN hash(org, t, nets) END AS k3,
       CASE WHEN t IS NOT NULL AND org IS NOT NULL AND tf_start IS NOT NULL THEN hash(org, t, nets, tf_start, tf_end) END AS k5
FROM sightings WHERE (t IS NOT NULL AND org IS NOT NULL) OR flock_id IS NOT NULL
```

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `sighting_id` | UBIGINT | The sighting | `hash(release_id, row_no)` |
| `release_id` | VARCHAR | Its release | |
| `row_no` | BIGINT | Its row position in the release | |
| `producer` | VARCHAR | Whose log | |
| `audit` | VARCHAR | `network` or `own` | |
| `flock_id` | VARCHAR | Flock search UUID, if released | `trim(ID)` unless blank or `***`; for San Mateo PD, `trim(id)` of the printed search id. Not lower-cased |
| `has_tf` | BOOLEAN | A Time Frame start was parsed | |
| `k3` | UBIGINT | `hash(org, t, nets)` | NULL without `t` or `org` |
| `k5` | UBIGINT | `hash(org, t, nets, tf_start, tf_end)` | NULL without `t`, `org` or `tf_start` |

What the keys compare, and what follows from that:

- **`org`** is the searching organization as exported, trimmed and case-sensitive. For an own-search log without an
  `Org Name` column, and for San Mateo PD's PDFs, it is the producer ([semantics.md](semantics.md) §5). A search
  exported under two spellings of the same organization gets two keys.
- **`t`** is the search time to the second, in UTC, stored without a zone ([semantics.md](semantics.md) §6.1).
- **`nets`** is Total Networks Searched as an integer (for San Mateo PD, the count printed before the search time). A
  NULL is hashed like a value (`hash(org, t, NULL)` is not NULL). Rows with no network count still get keys, and they
  match only other rows with no network count.
- **`tf_start`, `tf_end`** are the Time Frame bounds from `tf_bound`, which reads two lines or Lodi's one-line
  `<start> UTC to <end> UTC` form ([semantics.md](semantics.md) §6.3). A row without a parsed Time Frame gets no `k5`
  and is linked by `k3` only (tiers `3_k3`, `5_k3_to_group`, `6_k3_group`, or the UUID side of `3b`). Such rows include
  San Mateo PD's PDFs, El Cerrito's own-search log, Sonoma County's logs and the other releases without a `Time Frame`
  column.
- **Reason, Case #, Name, License Plate, Search Type and devices are never used for linking.** When two rows agree in
  those fields, that is independent evidence that they are one search. When they disagree, it is a finding about the
  productions, not an input to linking (§7).

Some rows are not linked at all, so they are absent from `cache.sighting_event`, `events` and `read_field`. These are
the sightings with no UUID and either no `t` or no `org` (count: [stats.md](stats.md), "Link tiers", last row; for
example Los Altos rows that are `REDACTED` placeholders, [semantics.md](semantics.md) §16), and every event-log row
(`event_log` is not a search log). A row with a UUID but no parsed time is linked by its UUID only and contributes no
key.

## 3. The linking algorithm

Linking runs once, in layer 3 of `build_derived.py`, over every sighting of every producer, in two stages. Every
lookup counts distinct candidates with `count(DISTINCT …)`, keeps one with `min(…)`, and uses it only when the count
is 1. That applies to the key-to-UUID lookups and to stage 2's `k3`-to-group lookup alike; nothing uses `any_value`.
The result therefore depends only on the truth data, the linking code and the DuckDB version. The version matters
because its `hash()` produces `k3`, `k5`, `sighting_id`, `event_key` and the `k5:`/`k3:`/`x:` ids. `cache.builds`
records all three, and `check_cache.py` compares them.

**Stage 1: attach a Flock UUID where one can be found.** Three lookup tables are built from the sightings that carry
a UUID:

| Lookup | Built from UUID-bearing sightings | Per key |
|---|---|---|
| `k5u` | with a `k5` | `n` = `count(DISTINCT flock_id)`, `fid` = `min(flock_id)` |
| `k3u` | with a `k3`, with or without a Time Frame | same |
| `k3n` | with a `k3` and **no** Time Frame (San Mateo PD's PDFs, own-search logs without the column) | same |

Each sighting takes the first rule that applies. This is the code:

```sql
-- build_derived.py, layer 3; k = cache.sighting_keys, LEFT JOIN k5u USING (k5), k3u USING (k3), k3n ON k3n.k3 = k.k3
CASE WHEN k.flock_id IS NOT NULL THEN '1_uuid'
     WHEN k5u.n = 1 THEN '2_k5'
     WHEN NOT k.has_tf AND k3u.n = 1 THEN '3_k3'
     WHEN k.has_tf AND k5u.n IS NULL AND k3n.n = 1 THEN '3b_k3_to_tf_less_uuid'
     WHEN k5u.n > 1 OR (NOT k.has_tf AND k3u.n > 1) OR (k.has_tf AND k5u.n IS NULL AND k3n.n > 1) THEN 'x_ambiguous' END
```

| `basis` | Rule | `event_id` |
|---|---|---|
| `1_uuid` | The row carries a UUID | `u:` + its UUID |
| `2_k5` | Its `k5` belongs to exactly one UUID | `u:` + `k5u.fid` |
| `3_k3` | It has no Time Frame, and its `k3` belongs to exactly one UUID among all UUID rows | `u:` + `k3u.fid` |
| `3b_k3_to_tf_less_uuid` | It has a Time Frame, its `k5` matches no UUID, and its `k3` belongs to exactly one UUID among UUID rows **without** a Time Frame | `u:` + `k3n.fid` |
| `x_ambiguous` | Its key has two or more UUIDs under the rule it reached: `k5u.n > 1`; or no Time Frame and `k3u.n > 1`; or a Time Frame, no `k5` match and `k3n.n > 1` | `x:` + its `sighting_id` |
| (unresolved) | None of the above: no UUID anywhere for its key | stage 2 |

What the order implies:

- **Through `k5`, a row with a Time Frame links only to a UUID whose rows carry the same Time Frame.** Suppose its `k3`
  matches only UUID rows that have a *different* Time Frame, and no UUID row without a Time Frame. Then it stays
  unresolved: the same organization, second and network count with a different window is treated as a different
  search.
- **Tier `3` compares no Time Frame.** The row has none, so it matches against every UUID row, with or without one.
- **Tier `3b` does not look at the UUID's other sightings.** It matches `k3` only among UUID rows without a Time Frame
  (`k3n`), so it never sees whether the same UUID also appears in another log with a Time Frame that differs from this
  row's. If it does, the `3b` link is made anyway. Check this before relying on a `3b` link. In this build it never
  happens:

```sql
-- every sighting of every search with a 3b link, then: does any of them carry both a UUID and a Time Frame?
CREATE OR REPLACE TEMP TABLE lk_3b AS
SELECT sighting_id, event_key FROM cache.sighting_event
WHERE event_key IN (SELECT event_key FROM cache.sighting_event WHERE basis = '3b_k3_to_tf_less_uuid');
SELECT count(DISTINCT e.event_key) AS searches_with_3b,
       count(DISTINCT e.event_key) FILTER (WHERE k.flock_id IS NOT NULL AND k.has_tf) AS also_a_uuid_row_with_tf
FROM lk_3b e JOIN cache.sighting_keys k USING (sighting_id);
-- 153 | 0                                                                              (3.4–4.4 s)
```

**Stage 2: group the rest (no UUID for this search in any loaded log).** Only the unresolved sightings take part. One
more lookup, `k3g`, is built over the unresolved rows *with* a Time Frame: per `k3`, `n` = `count(DISTINCT k5)` and
`k5` = `min(k5)`.

| `basis` | Rule | `event_id` |
|---|---|---|
| `4_k5_group` | It has a Time Frame | `k5:` + its `k5` (every unresolved row with that `k5` shares the event) |
| `5_k3_to_group` | No Time Frame, and `k3g.n = 1` for its `k3` | `k5:` + `k3g.k5` |
| `x_ambiguous` | No Time Frame, and `k3g.n > 1` | `x:` + its `sighting_id` |
| `6_k3_group` | No Time Frame, and no `k3g` entry | `k3:` + its `k3` |

An `x_ambiguous` sighting is an event of its own. It is not "unmatched": its key fits more than one search, and the
exported columns cannot say which (examples in §4).

Because linking is global, loading a release can change the links of other sightings. A new UUID-bearing log can turn
a `4_k5_group` event into a `u:` event (tier `2_k5`), or give a key a second UUID and make previously linked rows
`x_ambiguous`. Tier counts for the current build are in [stats.md](stats.md), "Link tiers". The build also prints them
at the end of its log (`ingest.log`).

## 4. Every tier in one producer's logs

El Cerrito released a network audit (no `ID` column, with a Time Frame) and an own-search log (no `ID`, no Time Frame).
Between them, its rows use every fallback tier:

```sql
SELECT audit, basis, count(*) AS sightings
FROM cache.sighting_event WHERE producer = 'El Cerrito CA PD'
GROUP BY ALL ORDER BY ALL;
-- network 2_k5 434,274 | network 4_k5_group 872 | network x_ambiguous 3,384
-- own 3_k3 312 | own 5_k3_to_group 424 | own 6_k3_group 9 | own x_ambiguous 2               (0.1 s)
```

One example per tier from the own-search log, and who else recorded each search:

```sql
SELECT basis, min(row_no) AS first_row, arg_min(event_id, row_no) AS event_id
FROM cache.sighting_event
WHERE release_id = 'mr:199390:12_15_2025-1_15_2026-El_Cerrito_CA_PD-Audit.csv#csv'
GROUP BY 1 ORDER BY 1;
-- 3_k3          1   u:c998f053-0f3a-426c-b913-e696080c37a1
-- 5_k3_to_group 2   k5:11889108306047384431
-- 6_k3_group    12  k3:14715902566269317403
-- x_ambiguous   690 x:15460324039829249206                                                 (0.2 s)

SELECT producer, audit, basis, count(*) AS sightings, count(DISTINCT release_id) AS releases
FROM cache.sighting_event WHERE event_key = hash('u:c998f053-0f3a-426c-b913-e696080c37a1')
GROUP BY ALL ORDER BY ALL;                                                                  -- (0.3–0.8 s)
```

| Search | Sightings | What linked it |
|---|---|---|
| `u:c998f053-…` (El Cerrito, 2025-12-16 23:11:44 UTC, 451 networks) | 17 in 11 producers' logs | UUID rows (`1_uuid`) in 9 network audits; El Cerrito's network row by `k5` (`2_k5`); its own-log row and 3 Sonoma County copies by `k3` (`3_k3`, no Time Frame in either) |
| `k5:11889108306047384431` (El Cerrito, 1 network) | 2: El Cerrito network row 236012 (`4_k5_group`) + own-log row 2 (`5_k3_to_group`) | No UUID-bearing row has its keys (a search of one network appears only in that network's audit). The own-log row has no Time Frame and joins the one network-audit group with its `k3` |
| `k3:14715902566269317403` (El Cerrito, 1 network) | 1: own-log row 12 (`6_k3_group`) | Nothing else matched its `k3` |

**Ambiguous, and why.** Own-log row 690 is one of two El Cerrito searches in the same second with the same network
count. The network audits carry Time Frames one second apart, which tell the two searches apart. The own-search log
and Sonoma County's network audit have no Time Frame, so their rows fit both searches:

```sql
-- every sighting that shares row 690's k3
WITH k AS (SELECT k3 FROM cache.sighting_keys
           WHERE release_id = 'mr:199390:12_15_2025-1_15_2026-El_Cerrito_CA_PD-Audit.csv#csv' AND row_no = 690)
SELECT flock_id IS NOT NULL AS has_uuid, has_tf, count(*) AS sightings, count(DISTINCT flock_id) AS uuids,
       count(DISTINCT k5) AS time_frames, count(DISTINCT producer) AS producers
FROM cache.sighting_keys WHERE k3 = (SELECT k3 FROM k) GROUP BY ALL ORDER BY ALL;
-- false false  8 0 0 2     El Cerrito own log (rows 690, 691) + Sonoma County: no UUID, no Time Frame -> x_ambiguous
-- false true   2 0 2 1     El Cerrito network audit (rows 203419, 203420) -> 2_k5, one to each UUID
-- true  true  20 2 2 8     the two searches' UUID rows in 8 network audits                (0.3–1 s)

SELECT row_no, org, t, nets, tf_start, tf_end FROM sightings
WHERE release_id = 'mr:199390:12_16_2025-1_15_2026-El_Cerrito_CA_PD-Network-Audit.csv#csv' AND row_no IN (203419, 203420);
-- 203419 El Cerrito CA PD 2025-12-28 12:45:49 404 2025-12-28 06:45:47 2025-12-28 12:45:47   -> u:9c21b15b-…
-- 203420 El Cerrito CA PD 2025-12-28 12:45:49 404 2025-12-28 06:45:48 2025-12-28 12:45:48   -> u:8c85afad-…  (0.1 s)
```

The other kind of ambiguity: El Cerrito network rows 342404 and 342405 record a `Cypress CA PD` lookup at 2026-01-09
07:08:51 UTC over 642 networks. Their shared `k5` belongs to two different UUIDs, each released in 11 sightings across
9 producers' network audits. The two rows are those two searches, but which is which cannot be decided, so both are
`x_ambiguous`:

```sql
WITH k AS (SELECT k5 FROM cache.sighting_keys
           WHERE release_id = 'mr:199390:12_16_2025-1_15_2026-El_Cerrito_CA_PD-Network-Audit.csv#csv' AND row_no = 342404)
SELECT flock_id, count(*) AS sightings, count(DISTINCT producer) AS producers
FROM cache.sighting_keys WHERE k5 = (SELECT k5 FROM k) GROUP BY 1 ORDER BY 1 NULLS LAST;
-- 65451a8a-8c3d-4c30-a8b2-bf0b88307b83 11 9 | 9e7c7f3d-3f20-42c6-a660-b7f5023367da 11 9 | NULL 2 1      (0.6 s)
```

**Tier `3b`.** Los Altos's February 2025 network audit has five rows (147747–147751) that are identical in every parsed
column, and it has them in each of its two productions. They record a San Mateo PD search at 2025-02-12 19:37:16 UTC
over 1 network. They carry a Time Frame but no UUID, and no UUID row has their `k5`. San Mateo PD's February 2025 PDF
(UUIDs, no Time Frame) has exactly one search with their `k3`, at row 4542. So all ten link to
`u:5d6ee1d3-1824-4620-a424-a7c363699d82` as `3b_k3_to_tf_less_uuid` (§5 shows the event).

## 5. `events` and `event()`: one row per linked search

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `event_id` | VARCHAR | The search | Forms in §1 |
| `event_key` | UBIGINT | `hash(event_id)` | Grouped on together with `event_id` |
| `n_sightings` | BIGINT | Sightings linked to it | Counts every release and every repeated row |
| `n_logs` | BIGINT | `count(DISTINCT producer)` | A producer's own-search log and network audit count once; re-releases do not count |
| `weakest_link` | VARCHAR | `max(basis)` over its sightings | Tier labels sort from strongest to weakest, so this is the least certain sighting, not a confidence for the whole event |
| `logs` | VARCHAR[] | Its producers, sorted | |

`events` groups `cache.sighting_event` on read, by (`event_id`, `event_key`), so a filter on either column is applied
before grouping. Filter on `event_key`, the stored integer: a filter on `event_id` compares the string on every cache
row. Any other filter (`n_logs`, `weakest_link`, …) groups all ~140 million cache rows, so don't. The `event(eid)`
macro returns the same row for one id, found by `event_key = hash(eid)` and then `event_id = eid`.
`ac.event(con, eid)` returns it as a dict, with the sighting list added.

```sql
SELECT n_sightings, n_logs, weakest_link, logs FROM event('u:c998f053-0f3a-426c-b913-e696080c37a1');
-- 17 | 11 | 3_k3 | [El Cerrito CA PD, Mountain View Police Department, …, Ukiah Fire CA FD]         (0.1–1.1 s)
SELECT n_sightings, n_logs, weakest_link FROM events WHERE event_key = hash('u:c998f053-0f3a-426c-b913-e696080c37a1');
-- same row (0.2–0.8 s); `events WHERE event_id = '…'` gives it too, in 4–8 s
```

```python
e = ac.event(con, "u:5d6ee1d3-1824-4620-a424-a7c363699d82")   # None if the id is not in the cache
print(e["n_sightings"], e["n_logs"], e["weakest_link"], e["logs"], len({s[0] for s in e["sightings"]}))
# 11 2 3b_k3_to_tf_less_uuid ['Los Altos CA PD', 'San Mateo CA PD'] 3      (0.2–0.4 s; sightings: (release_id, row_no, producer, audit, basis))
```

That event is one San Mateo PD search. It has 11 sightings because Los Altos exported it as five identical rows and
produced that month twice. Its `n_logs` is 2. To say "how many agencies' records show this search", report `n_logs`
(or name the logs), never `n_sightings`.

**Counting searches.** Count `DISTINCT event_key` (or `event_id`) over `cache.sighting_event` rows filtered by
`producer`, `audit` or `release_id`, and say which tiers the count includes:

```sql
SELECT count(*) AS sightings, count(DISTINCT event_key) AS searches,
       count(*) FILTER (WHERE basis = 'x_ambiguous') AS ambiguous_sightings
FROM cache.sighting_event WHERE producer = 'Los Altos CA PD' AND audit = 'own';
-- 13,870 | 11,343 | 61                                                                     (0.03–0.7 s)
```

Los Altos's own-search log has 13,886 rows over 37 releases, and 16 of them cannot be linked (§2). That leaves 2,527
sightings beyond one per search. Of those, 2,507 are the same search in both PRAs (25-312 and 26-366 overlap for
January–August 2025), and 20 are repeats inside one release. Each `x_ambiguous` sighting is its own event, so
re-released copies of it count as separate searches: exclude them or report them. More counting rules are in
[semantics.md](semantics.md) §3.

## 6. How reliable a link is

**By tier.**

| `basis` | Evidence | State it as |
|---|---|---|
| `1_uuid` | The row itself carries Flock's search UUID | "The same Flock search ID appears in log A (row …) and log B (row …)." Identity, not inference |
| `2_k5` | Same organization, second, network count and Time Frame as the rows of exactly one search UUID in the database | "Log A's row matches Flock search ID … (released in log B) on organization, search time to the second, networks searched and time frame." |
| `3_k3` | Same organization, second and network count as exactly one UUID; this row has no Time Frame | As above, without "time frame"; say that log A has no time frame |
| `3b_k3_to_tf_less_uuid` | Same organization, second and network count as exactly one UUID among the UUID rows without a Time Frame. This row's Time Frame is compared with nothing (§3) | As `3_k3`; say that the UUID's log has no time frame. Run the §3 check if the claim rests on it |
| `4_k5_group` | Identical organization, second, networks and Time Frame, and no UUID in any loaded log | "Rows in logs A and B with identical organization, time, networks and time frame." Two genuinely different searches that look identical in these columns would be merged, and nothing here can detect it |
| `5_k3_to_group`, `6_k3_group` | As `4_k5_group`, without the Time Frame on one side or on all sides | Weakest: look for corroboration from Reason or Case # (§7) before relying on it |
| `x_ambiguous` | The key fits two or more searches | Not linked. Do not attribute it to either search |

**Measured precision of the keys.** The fallback keys can be tested where the answer is known: on the sightings that
carry a Flock UUID. [stats.md](stats.md), "Linking precision", reports two measures for `k3` and `k5` (SQL: `precision()`
in `gen_stats.py`, one pass over the UUID rows of `cache.sighting_keys`):

- *Key precision*: the share of distinct key values (computed on UUID-bearing sightings) that belong to exactly one
  UUID. One minus it is how often the key alone would merge two different searches.
- *UUID coherence*: the share of UUIDs whose sightings all produce the same key. One minus it is how often the key
  alone would split one search.

What these numbers do and do not say:

- They are per distinct key and per UUID, not per sighting.
- Where a key has two UUIDs, linking already refuses the match (`x_ambiguous`). So on UUID rows, imprecision mostly
  shows up as ambiguous rows in tiers 2–3b, not as wrong links. In tiers 4–6 there is no UUID to detect a collision,
  and the measured precision is the best available estimate of how often a group merges two searches.
- They are measured on logs that export UUIDs, which are the newer exports. The fallback tiers mostly serve older
  exports and repo NDJSON. If those format a field differently (an organization spelling, say), links are lost there,
  and the UUID measurement cannot show it. A lost link shows up as a search with fewer logs than expected, or as a
  `4_k5_group` / `6_k3_group` event that should have been `2_k5` / `3_k3`.
- [stats.md](stats.md), "Own-search log vs network audit", is a completeness check on linking. For each producer that
  released both logs, it gives the share of own-log searches (inside a network-audit period) that are also found in its
  network audit. A share below 100% combines own searches that did not touch the producer's own cameras with links
  that were lost.

**For your slice**, check the tier mix first: run the `basis` count of §4 on your releases, and report each tier's
share with the result.

**In an article or filing**, name the tiers behind a cross-log number ("of the 1,000 searches, 940 are linked by Flock
search ID, 60 by matching organization, time to the second and network count"). Give the fallback keys' measured
precision from stats.md, and cite one row per log for any individual search you describe (§8).

## 7. Comparing logs: `read_field`

Each search is one Flock record, with one Reason and one Case #, and every log's row for it is an export of that
record (the same search UUID recurs across logs). The logs should therefore agree, except where a production masked,
blanked, truncated or replaced the text. A difference is a finding about a production, not about the search.
`read_field` puts that comparison on every sighting.

`read_field(fld, who := NULL)` is a table macro. `fld` is `'reason'` or `'case'`, in any letter case. Any other value
raises `read_field: field must be 'reason' or 'case', not '<fld>'` (`field_of`). With `who`, only that producer's
sightings are returned, and only the rows of the events they belong to are parsed. Without `who`, every linked
sighting is returned, and the whole `sightings` view is parsed (minutes, with a large spill; do not run it in a
shared session).

| Column | Type | Meaning |
|---|---|---|
| `sighting_id` | UBIGINT | The sighting |
| `producer` | VARCHAR | Its log |
| `event_id` | VARCHAR | Its search |
| `state` | VARCHAR | Its `reason_state` / `case_state` ([semantics.md](semantics.md) §10) |
| `surface` | VARCHAR | Its released text. Local only ([pii.md](pii.md)) |
| `revealed` | VARCHAR | What the **other** sightings of the search released, by consensus. Local only |
| `support` | BIGINT | Distinct producers behind `revealed` |
| `divergence` | VARCHAR | Label below |

**How `revealed` is chosen**, per event. Sightings in `x:` events are not returned.

1. Take each sighting's clean value: the trimmed text of a `value` cell. Count, for each clean value, the distinct
   producers that released it. Masked, withheld, blank, placeholder and `partial` cells never vote.
2. Take the top value `v1` (`n1` producers) and the runner-up `v2` (`n2`). Ties are broken on the value itself
   (`first(value ORDER BY n DESC, value)`: among equally supported values, the one that sorts first wins), so
   repeated runs return the same `revealed`.
3. If this sighting's own value is `v1`, leave its producer out: `revealed` = `v1` with `support` = `n1 − 1` when
   `n1 > 1` and `n1 − 1 ≥ n2`; otherwise `v2` with `n2`; otherwise NULL. If its own value is not `v1`, `revealed` =
   `v1` with `support` = `n1`.
4. `divergence(state, value, revealed)` labels the sighting; the first match wins:

| `divergence` | Condition | What you can claim | What you cannot |
|---|---|---|---|
| `no_other_record` | `revealed` is NULL | No other producer's log released a value for this search | That no value exists: other logs may be unreleased or not loaded |
| `masked_here_released_elsewhere` | state `redacted_flock`, `redacted_agency`, `withheld` or `partial` | This production masked a value that `support` producers released | Who masked it ([semantics.md](semantics.md) §11). Also, `support` can include this producer's own other production |
| `blank_here_present_elsewhere` | state `empty` or `not_exported` | The cell is blank here, or the column absent; the value was released elsewhere | That the agency removed it: `not_exported` means the release's `header` lacks the column. For Redwood City, `header` lists only NDJSON keys with a non-empty cell, so the column may have existed and been blank throughout ([semantics.md](semantics.md) §12) |
| `placeholder_here` | state `placeholder` | A junk entry here (`n/a`, `test`, …); a value elsewhere | |
| `same` | own clean value = `revealed` (exact, case-sensitive) | At least one **other** producer released the identical text | That the text is genuine ([semantics.md](semantics.md) §10) |
| `differs_from_other_logs` | otherwise | This production's text differs from what `support` producers released | That the text was altered. The comparison is exact, so letter case, inner spacing, a trailing `" -"` and truncation all count as different. Look at both surfaces first |

Properties that follow from the rules:

- **Re-releases never corroborate.** The votes are distinct producers, so a producer's second production of the same
  value cannot make `same`. In the "otherwise" branch, though, `support` counts every producer, including this one. A
  label other than `same` can therefore rest on the same agency's other production (Port Hueneme example below).
- **Ties after leaving one out favour agreement.** If the own value has 2 producers and another value has 1, the result
  is `same` with `support` 1.
- **Deterministic.** The label and `support` do not depend on how ties are broken, and the tie-break on the value makes
  `revealed` fixed as well.
- **Exact strings.** `same` needs byte-identical trimmed text. Normalize on purpose, and say how (as in the third
  example below and cookbook.md Recipe 7).

**Cost.** `read_field(fld, who := …)` scales with the number of sightings in that producer's events. For Cathedral City
(3 sightings, 38 in their events) it took 6–11 s. For Cotati (177,870 sightings, 946,068 in their events) it took
22–28 s. For one release, or a slice of a large producer's rows, use `lk_compare` below. `audit_client` has no
equivalent. It applies the same rules to the sightings chosen by any pruning filter on `cache.sighting_event`, and it
parses only their events' rows, through `ac.sightings_sql` (the shared templates). The consensus SQL is the
`read_field` macro's (`build_derived.py`, `CACHE_READS`), with its input swapped for temp tables. If the macro
changes, change this too. For Cathedral City and for Cotati's own-search log, it returned the same `state`, `support`,
`divergence` and `revealed` as `read_field(…, who := …)`. Its temp tables are prefixed `lk_` so that they do not
collide with cookbook.md's, which are all prefixed `cb_`.

```python
LK_FV = """
CREATE OR REPLACE TEMP TABLE lk_fv AS
SELECT se.sighting_id, se.release_id, se.row_no, se.producer, se.event_id, se.event_key,
       field_of(?, p.reason_state, p.case_state) AS state,
       field_of(?, p.reason_surface, p.case_surface) AS surface,
       field_of(?, p.reason, p.case_no) AS value
FROM lk_se se JOIN lk_parsed p USING (release_id, row_no)"""
LK_CMP = """
CREATE OR REPLACE TEMP TABLE lk_cmp AS
WITH cnt AS (SELECT event_key, value, count(DISTINCT producer) AS n FROM lk_fv WHERE state = 'value' GROUP BY ALL),
t1 AS (SELECT event_key, first(value ORDER BY n DESC, value) AS v1, max(n) AS n1 FROM cnt GROUP BY event_key),
t2 AS (SELECT c.event_key, first(c.value ORDER BY c.n DESC, c.value) AS v2, max(c.n) AS n2 FROM cnt c JOIN t1 USING (event_key)
       WHERE c.value <> t1.v1 GROUP BY c.event_key),
r AS (SELECT fv.*,
        CASE WHEN fv.state = 'value' AND fv.value = t1.v1 THEN
                  CASE WHEN t1.n1 - 1 >= coalesce(t2.n2, 0) AND t1.n1 > 1 THEN t1.v1 WHEN t2.n2 > 0 THEN t2.v2 END
             ELSE t1.v1 END AS revealed,
        CASE WHEN fv.state = 'value' AND fv.value = t1.v1 THEN
                  CASE WHEN t1.n1 - 1 >= coalesce(t2.n2, 0) AND t1.n1 > 1 THEN t1.n1 - 1 WHEN t2.n2 > 0 THEN t2.n2 END
             ELSE t1.n1 END AS support
      FROM lk_fv fv LEFT JOIN t1 USING (event_key) LEFT JOIN t2 USING (event_key))
SELECT sighting_id, release_id, row_no, producer, event_id, event_key, state, surface, value, revealed, support,
       divergence(state, value, revealed) AS divergence
FROM r SEMI JOIN lk_mine USING (sighting_id)"""


def lk_compare(con, fld, where, params=()):
    """read_field(fld) for the sightings matching `where`, a pruning filter on cache.sighting_event such as
    "release_id = ?" or "release_id = ? AND row_no <= 100". Result in temp table lk_cmp (read_field's columns plus
    release_id, row_no, event_key and the clean `value`); lk_fv holds every sighting of those searches. Local only."""
    con.execute(f"""CREATE OR REPLACE TEMP TABLE lk_mine AS SELECT sighting_id, event_key FROM cache.sighting_event
                    WHERE ({where}) AND basis <> 'x_ambiguous'""", list(params))
    con.execute("""CREATE OR REPLACE TEMP TABLE lk_se AS
                   SELECT sighting_id, release_id, row_no, producer, event_id, event_key FROM cache.sighting_event
                   WHERE event_key IN (SELECT event_key FROM lk_mine)""")
    pairs = con.execute("SELECT release_id, row_no FROM lk_se").fetchall()
    con.execute(f"""CREATE OR REPLACE TEMP TABLE lk_parsed AS
                    SELECT release_id, row_no, reason_state, reason_surface, reason, case_state, case_surface, case_no
                    FROM ({ac.sightings_sql(pairs)})""")
    con.execute(LK_FV, [fld, fld, fld])
    con.execute(LK_CMP)
    return con.table("lk_cmp")


def lk_backers(con):
    """After lk_compare: each compared row paired with every OTHER sighting of its search that released exactly its
    `revealed` value (temp table lk_back), with a citation for both rows. No cell values are returned."""
    con.execute("""CREATE OR REPLACE TEMP TABLE lk_back AS
                   SELECT c.release_id, c.row_no, c.state, c.divergence, c.support, o.producer AS backer,
                          o.release_id AS backer_release, o.row_no AS backer_row
                   FROM lk_cmp c JOIN lk_fv o ON o.event_key = c.event_key AND o.sighting_id <> c.sighting_id
                   WHERE o.state = 'value' AND o.value = c.revealed""")
    pairs = con.execute("SELECT release_id, row_no FROM lk_back UNION SELECT backer_release, backer_row FROM lk_back").fetchall()
    cite = {(r, n): c for r, n, c in ac.citations_for(con, pairs).select("release_id, row_no, citation").fetchall()}
    cols = ["release_id", "row_no", "state", "divergence", "support", "backer", "backer_release", "backer_row"]
    return [dict(zip(cols, x), citation=cite.get((x[0], x[1])), backer_citation=cite.get((x[6], x[7])))
            for x in con.execute("SELECT * FROM lk_back ORDER BY ALL").fetchall()]
```

**Example: masked here, released elsewhere, with both citations.** Cotati's own-search log masks Reason with `***` on
every row. Which other logs released it?

```python
COTATI_OWN = "mr:214820:ALPR_PRA_Response.07-28-2026T01-47-15_PDT.zip!ALPR PRA Response/Organization Audit Log 6_6_2026-7_6_2026.csv#csv"
lk_compare(con, "reason", "release_id = ?", [COTATI_OWN])
print(con.sql("""SELECT state, divergence, count(*) AS sightings, min(support), max(support)
                 FROM lk_cmp GROUP BY ALL ORDER BY sightings DESC""").fetchall())
b = lk_backers(con)
print(con.sql("""SELECT backer, count(DISTINCT (release_id, row_no)) AS rows_here, count(*) AS backer_rows
                 FROM lk_back GROUP BY 1 ORDER BY 1""").fetchall())
print(b[0]["citation"], "|", b[0]["backer_citation"])   # one masked row and one row that released it
# [('redacted_flock', 'masked_here_released_elsewhere', 12, 2, 2), ('redacted_flock', 'no_other_record', 7, None, None)]
# [('San Francisco CA PD', 12, 12), ('Santa Rosa CA PD', 12, 24)]                          (6.5–7 s in all)
```

For 12 of Cotati's 19 searches, San Francisco's network audit and Santa Rosa's (in both of its productions of that
month) released the Reason that Cotati's own log masks. In the same events, Redwood City's Reason is `withheld` and
Rohnert Park's export has no Reason column. The other 7 searches appear only in Cotati's own logs, masked in both.

**Example: the "other log" is the same agency.** Port Hueneme's 2026-07-17 production replaced every Reason with the
exemption citation `7923.600 GC`. That cell is `redacted_agency` ([semantics.md](semantics.md) §10). The first 10
rows of September 2025:

```python
PH_JULY = ("mr:207641:PRR_26-54_-_Flock_Audit_-_National_Lookups_Response_7.16.26-20260717T090710Z-1-001.zip!"
           "PRR 26-54 - Flock Audit - National Lookups Response 7.16.26/9-1-2026 to 10-1-2026-Port Hueneme CA PD-"
           "Network-Audit - Red#9_1_2025-10_1_2025-Port Hueneme")
lk_compare(con, "reason", "release_id = ? AND row_no <= 10", [PH_JULY])
print(con.sql("""SELECT state, trim(surface) = '7923.600 GC' AS exemption_citation, divergence, count(*), min(support), max(support)
                 FROM lk_cmp GROUP BY ALL""").fetchall())
lk_backers(con)
print(con.sql("""SELECT b.backer, list(DISTINCT r.released_on ORDER BY r.released_on) AS productions,
                        count(DISTINCT (b.release_id, b.row_no)) AS rows_here
                 FROM lk_back b JOIN truth.releases r ON r.release_id = b.backer_release
                 GROUP BY 1 ORDER BY 3 DESC, 1""").fetchall())
# [('redacted_agency', True, 'masked_here_released_elsewhere', 10, 6, 7)]                  (6.2–6.3 s)
# Port Hueneme [2026-09-21] 10 | San Bruno [2026-04-28] 10 | Sonoma County [2026-07-29, 2026-08-05, 2026-08-31] 10 |
# Ukiah [2026-06-23] 10 | Ukiah Fire [2026-06-24] 10 | Santa Rosa [2026-09-01] 9 | Los Altos [2026-07-09] 2
```

All 10 are `masked_here_released_elsewhere` with `support` 6 or 7, and one of those producers is Port Hueneme itself:
its 2026-09-21 production of the same month released the free-text Reason. The claim this supports is "Port Hueneme's
July production shows an exemption citation where its September production and five to six other agencies' logs
show the searcher's reason". It does not support "other agencies contradict Port Hueneme".

**Example: `differs_from_other_logs` is an exact comparison.** The first 100 rows of San Mateo PD's March 2026 PDF:

```python
SMPD_MAR26 = "smpd:W012541-041426:3_1_2026-3_31_2026-San_Mateo_CA_PD-Audit__1_.pdf"
lk_compare(con, "reason", "release_id = ? AND row_no <= 100", [SMPD_MAR26])
print(con.sql("SELECT divergence, count(*), min(support), max(support) FROM lk_cmp GROUP BY ALL ORDER BY 2 DESC").fetchall())
print(con.sql(r"""SELECT CASE WHEN value = revealed || ' -' THEN 'here = revealed + " -"'
                              WHEN starts_with(revealed, value) THEN 'here is a prefix of revealed'
                              ELSE 'other' END AS how,
                         count(*), min(length(revealed) - length(value)), max(length(revealed) - length(value))
                  FROM lk_cmp WHERE divergence = 'differs_from_other_logs' GROUP BY 1 ORDER BY 2""").fetchall())
lk_backers(con)
print(con.sql("""SELECT backer, count(DISTINCT (release_id, row_no)) FROM lk_back
                 WHERE divergence = 'differs_from_other_logs' GROUP BY 1 ORDER BY 2, 1""").fetchall())
# [('same', 51, 4, 4), ('no_other_record', 25, None, None), ('differs_from_other_logs', 24, 1, 5)]   (5.4–6.8 s)
# [('here = revealed + " -"', 3, -2, -2), ('here is a prefix of revealed', 21, 43, 43)]
# [('San Bruno CA PD', 3), ('Los Altos CA PD', 21), ('Port Hueneme CA PD', 21), ('Sonoma County CA SO', 21),
#  ('Ukiah CA PD', 21), ('Ukiah Fire CA FD', 21)]
```

None of the 24 is a linking error, and none is a different reason. In 3 rows, San Mateo PD's text is San Bruno's text
with `" -"` added. In 21 rows it is the first part of the text that five other logs released, 43 characters shorter;
the rest of that text is not in the PDF's text layer on the cited page. The label only says the strings differ. The
data does not say why two exports of one search differ, so quote both originals, with both citations.

**Consensus of masked values.** Only `value` cells vote. A `***` in ten logs is not a consensus, the residue of a
`partial` cell (`REDACTED / 459 SUS`) is ignored, and exemption citations such as `7923.600 GC` are `redacted_agency`.
But a mask string the classifier does not know, or a junk word not on the `placeholder` list, reads as `value` and
does vote ([semantics.md](semantics.md) §10). If several producers released the same such string, it can become
`revealed`. Look at the distinct values behind a count before reporting it.

## 8. Proving "log A says X, log B says Y"

`event_sightings(eid)` is a table macro that returns every sighting of one search, with its states, surfaces and a
ready-to-paste citation. It finds the event by `event_key = hash(eid)` and then `event_id = eid` (under a second). It
then selects that event's truth rows and parses and cites them with the same SQL templates as the full views
([derived.md](derived.md)).

| Column | Type | Meaning |
|---|---|---|
| `producer` | VARCHAR | Log |
| `basis` | VARCHAR | This sighting's link tier |
| `release_id` | VARCHAR | Release |
| `row_no` | BIGINT | Row position in the release |
| `src_row` | BIGINT | Row a reader sees in the original (NULL for San Mateo PD, which is cited by PDF page and search id) |
| `org` | VARCHAR | Searching organization |
| `t` | TIMESTAMP | Search time, UTC |
| `nets` | INTEGER | Networks searched |
| `reason_state`, `case_state`, `name_state`, `plate_state` | VARCHAR | Cell states |
| `reason_surface`, `case_surface` | VARCHAR | Released text. Local only; can hold plates and civilian details |
| `citation` | VARCHAR | Same text as `sighting_sources.citation` |

The Port Hueneme search in row 1 of the July production:

```sql
SELECT producer, basis, src_row, reason_state, trim(reason_surface) = '7923.600 GC' AS exemption_citation, citation
FROM event_sightings('u:801cd2c0-4c42-41c0-9111-8d9e030fdf21');
-- 17 rows, 12 producers (11–11.5 s). Condensed:
-- Port Hueneme CA PD  1_uuid 2      redacted_agency true   … produced 2026-07-17. PRR_26-54_-_Flock_Audit_-_National_Lookups_Response_7.16.26-20260717T090710Z-1-001.zip > 9-1-2026 to 10-1-2026-Port Hueneme CA PD-Network-Audit - Redatced Batch 3.xlsx, sheet "9_1_2025-10_1_2025-Port Hueneme", row 2. …
-- Port Hueneme CA PD  1_uuid 2      value           false  … produced 2026-09-21. PRR_26-54_-_Response_9.18.26-20260921T072017Z-1-001.zip > 9-1-2025 to 10-1-2025-Port Hueneme CA PD-Network-Audit.xlsx, sheet "9_1_2025-10_1_2025-Port Hueneme", row 2. …
-- San Bruno CA PD 1_uuid 281143 value | Santa Rosa CA PD 1_uuid 102215 value | Ukiah CA PD 1_uuid 23659 value | Ukiah Fire CA FD 1_uuid 26656 value
-- Sonoma County CA SO 3_k3 101138 value (three productions) | Los Altos CA PD 2_k5 80370 value
-- San Francisco CA PD 1_uuid 128176 redacted_agency | San Jose CA PD 2_k5 52994, 184045 redacted_agency (two productions)
-- Redwood City CA PD 1_uuid 261050 withheld
-- Mountain View Police Department 1_uuid 105124 not_exported (two requests) | Ventura County CA SO 2_k5 47999 not_exported
```

**Faster, for scripts: `ac.drill(con, eid)`.** It finds the event by `event_key`, then parses and cites its rows through
literal (`release_id`, `row_no`) lookups. For this event it returned the same 17 rows, tiers, states and citations as
the macro in 1.1–2.2 s, against about 11 s. The macro's cost depends on how many releases the event spans: 0.2–0.4 s
for a 2-sighting event in two El Cerrito releases. `drill` returns one dict per sighting, ordered by producer, release
and row. The keys are `producer`, `audit`, `basis`, `release_id`, `row_no`, `public_release_id`, `src_row`, `org`,
`t`, `nets`, `reason_state`, `case_state`, `name_state`, `plate_state`, `citation`, `open_url` and `sha256`.
`surfaces=True` adds `reason_surface` and `case_surface`, which are local only. From a shell,
`python audit_client.py <event_id> [audit_dir]` prints states and citations without surfaces.

```python
rows = ac.drill(con, "u:801cd2c0-4c42-41c0-9111-8d9e030fdf21")
print(len(rows), len({r["producer"] for r in rows}))                         # 17 12
for r in rows:
    if r["producer"] == "Port Hueneme CA PD":
        print(r["basis"], r["src_row"], r["reason_state"], r["public_release_id"])
```

To write it up, take the two rows you contrast. Quote each surface from the original that you open with the
`citation`, not from the database, and give both citations. State the tier of each row: both are `1_uuid` here, so the
same Flock search ID is in both files. If a surface holds a plate or a civilian detail, describe it instead of quoting
it ([pii.md](pii.md)). For many rows at once, `lk_backers` (§7) gives both citations for each pair.

## 9. Query patterns and costs

Standard session, niced, load average 5–7 ([Before you start](#before-you-start)).

| You have | Do | Time |
|---|---|---:|
| A producer or release | `cache.sighting_event WHERE producer = …` / `release_id = …` (tier mix, event ids) | 0.03–0.2 s |
| A Flock UUID or any `event_id` | `event('<event_id>')`, `events WHERE event_key = hash('<event_id>')`, `ac.event(con, eid)`, or `cache.sighting_event WHERE event_key = hash(…)` | 0.1–1.1 s |
| | `events WHERE event_id = …` | 4–8 s |
| A batch of searches | `WHERE event_key IN (SELECT event_key FROM <temp table>)`, or a literal `IN (…)` list | about 1 s for 19 or 2,000 keys |
| A row's keys | `cache.sighting_keys WHERE release_id = … AND row_no = …`, then `WHERE k3 = …` / `k5 = …` | 0.02–0.05 s, then 0.3–1 s |
| One search's rows with citations | `ac.drill(con, eid)` (§8) | 0.4–2.2 s |
| | `event_sightings(eid)` | 0.2 s (2 sightings, 2 releases) – 11 s (17 sightings, 17 releases) |
| Cross-log comparison for a release or slice | `lk_compare` (§7) | 4–7 s for 38–1,041 sightings in the events involved |
| Cross-log comparison for a producer | `read_field(fld, who := …)` | 6–11 s (38 event sightings) to 22–28 s (946,068) |
| | `read_field(fld)` without `who` | Parses every sighting. Don't |
| Arbitrary rows by (`release_id`, `row_no`) | `ac.sightings_for(con, pairs)`, `ac.citations_for(con, pairs)` | 1.2 s and 0.4 s for 97 rows in 19 releases |

Two rules make these fast:

- **Filter the cache on a stored column** (`producer`, `release_id`, `event_key`, `basis`). Never filter it on
  `event_id`, and never with `LIKE` over all rows. Adding `event_id NOT LIKE 'x:%'` to a 97-row semi join took it from
  0.9 s to 4.6–5.0 s; `basis <> 'x_ambiguous'` gives the same rows in about 1 s.
- **Reach parsed rows through literal `release_id` and `row_no` values** (`ac.sightings_for` / `ac.citations_for`,
  `drill`, `lk_compare`). Never go through a join to the full `sightings` or `sighting_sources` view, which parses
  every row. How to join the cache to the parsed rows of one release is in [derived.md](derived.md),
  "`cache.sighting_event`".

## 10. Pitfalls

- **Re-releases and repeats inflate `n_sightings`, not `n_logs`.** One San Mateo PD search has 11 sightings in 2 logs
  (§5). San Mateo PD's PDFs repeat some searches inside one PDF, and a search printed in two PDFs would be two sightings
  (§1). **Do:** report `n_logs`, or name the logs, and count searches as distinct `event_key`. **Don't:** call
  `n_sightings` or rows "agencies", "records" or "searches".
- **`n_logs` counts producers.** A search in a producer's own-search log and in its network audit counts once. **Do:**
  group by (`producer`, `audit`) if the type of log matters.
- **`x_ambiguous` is neither matched nor absent.** Each such row belongs to one of two or more candidate searches
  (UUIDs, or Time Frame groups). Each is its own event, so it inflates search counts, and it is missing from the
  `n_logs` and the `read_field` of the search it belongs to. **Do:** report how many sightings in a count are
  `x_ambiguous`, or exclude them and say so.
- **Tier `3b` ignores the UUID's other Time Frames** (§3). **Do:** run the §3 check before a claim rests on a `3b`
  link.
- **Weaker tiers can merge.** Tiers `4_k5_group`–`6_k3_group` put together rows that are identical in organization,
  second and network count (and Time Frame), with no UUID to check against. Two real searches that look alike become
  one, and identical repeated rows are de-duplicated ([semantics.md](semantics.md) §15). **Do:** say "rows identical
  in …" for these tiers, and corroborate with Reason or Case # when a claim rests on one link.
- **Links can be lost.** Different spellings of an organization, a Time Frame that did not parse, or a missing network
  count keep the rows of one search apart. An absent link is not evidence that a search is missing from a log
  ([semantics.md](semantics.md) §17).
- **Event ids are build-specific.** `k5:`, `k3:` and `x:` ids, `event_key` and `sighting_id` are DuckDB hashes that
  change with the DuckDB version, and any id can change when a release is added (§3). **Do:** persist (`release_id`,
  `row_no`) and re-derive events after a rebuild, and publish `u:` UUIDs or citations. **Don't:** store or publish
  `k5:`/`k3:`/`x:` ids, `event_key` or `sighting_id` as identifiers.
- **Consensus is a vote of producers, not a check of truth.** `same` means another agency released the identical
  string. `support` can include the sighting's own producer, ties after leaving one out go to `same`, and masks that
  read as `value` can win. **Do:** read `support`, see who backs `revealed` (`lk_backers`), and open both originals
  before writing "differs". **Don't:** write "altered" from `differs_from_other_logs` alone.
- **The comparison is exact.** A trailing `" -"`, letter case, spacing or truncation gives `differs_from_other_logs`
  (§7). **Do:** normalize on purpose, and say how.
- **Stale cache.** A cache built from other truth, other linking code or another DuckDB version gives wrong events
  without any error. **Do:** run `check_cache.py`. After editing `layouts.json` or `producers.json`, rebuild derived in
  full, whatever it says ([derived.md](derived.md)).
- **Temp-table names.** The helpers here write `lk_*` temp tables, and cookbook.md's recipes write
  `cb_*` ones. Running a second call of the same helper replaces its tables.
- **Surfaces are local.** `read_field`, `event_sightings`, `ac.drill(…, surfaces=True)`, `ac.sightings_for`, and the
  `lk_parsed`, `lk_fv` and `lk_cmp` tables hold released text. Export states, labels, support and citations, or
  `sightings_public` columns ([pii.md](pii.md)).
