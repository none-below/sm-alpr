# ALPR audit-log database

A DuckDB database of Flock Safety ALPR (automated license plate reader) audit logs released by California law
enforcement agencies under the California Public Records Act. It records each search as it appears in each agency's
log, and links the copies of the same search across logs. Every row traces back to the page, sheet and row of the
document the agency released. These docs are for engineers and analysts who want to mine it: write their own queries,
join across agencies, and hand results to reporters or lawyers with a citation for every row.

**New to the data?** Read these in order:
1. [semantics.md](semantics.md) §1–§4: the data model (release → row → sighting → event), the three kinds of log, and
   what is fast.
2. [cookbook.md](cookbook.md) "Patterns", then the recipe closest to your question.
3. [provenance.md](provenance.md): how to cite any result.

Check [linking.md](linking.md) before any cross-agency claim, and [pii.md](pii.md) before publishing any derived data.

## Documents

| Document | Read it for |
|---|---|
| [semantics.md](semantics.md) | What the fields mean and the traps: network vs own-search logs, time zones, redaction markers, re-releases, coverage limits |
| [cookbook.md](cookbook.md) | Worked queries for common research questions, each with how to cite the rows behind the answer |
| [linking.md](linking.md) | How one search is matched across agencies' logs, how reliable each match tier is, and cross-log comparison (`read_field`) |
| [provenance.md](provenance.md) | From any row to the original document: file, sheet or page, row, link, SHA-256, a ready-made citation, and how to verify it |
| [pii.md](pii.md) | Civilian data handling: plate tokens, the public view, what may be exported |
| [truth.md](truth.md) | Data dictionary, layer 1: the verbatim released rows and the facts about each release |
| [derived.md](derived.md) | Data dictionary, layers 2–3: views and macros computed on read, and the linking cache |
| [coverage.md](coverage.md) | Generated: which agencies, logs, date ranges and requests are loaded |
| [stats.md](stats.md) | Generated: link tiers, linking precision, redaction census, pre-export plate check |

## Standard session

Every example in these docs assumes the setup below. `<audit_db>` is the directory that holds the two database files.

```python
import duckdb

A = "<audit_db>"
con = duckdb.connect(f"{A}/derived.duckdb", read_only=True)
con.execute(f"ATTACH IF NOT EXISTS '{A}/truth.duckdb' AS truth (READ_ONLY)")
con.execute("SET threads=4; SET memory_limit='4GB'")   # modest, so the machine stays usable; raise for big scans
con.execute("SET temp_directory='<scratch dir>'; SET max_temp_directory_size='8GiB'")   # a runaway query errors, not a full disk
print(con.sql("SELECT * FROM truth.build_info"))
```

From Python, `audit_client.py` in `<audit_db>` wraps this. It provides `connect()`, fast parsing of any set of rows
by `(release_id, row_no)` (`sightings_for`, `citations_for`), and one search across every log (`event`, `drill`); see
[cookbook.md](cookbook.md).

In the DuckDB CLI, run `duckdb -readonly -init <audit_db>/init.sql <audit_db>/derived.duckdb`. `init.sql` attaches
truth and sets limits. On a shared machine, prefix long jobs with `nice -n 19 taskpolicy -b` (macOS).

**What is fast:** anything filtered by `release_id`, by producer, or by one event.
**What is slow:** any view scanned in full. `sightings` parses about 140M rows on read.

## How it is organized

- **Layer 1: `truth.duckdb`, what was released, verbatim.** Every value is text, exactly as the agency produced it.
  - There is one row per released row. San Mateo PD's rows are one per search printed in its PDFs.
  - Releases are never merged or de-duplicated, even when an agency produced the same month twice.
  - There is no parsing or cleaning.
  - Facts that come from documents rather than rows sit in their own tables, each with a citation: withheld fields
    from cover letters, and corrections for rows released under the wrong column labels.
- **Layer 2: views and macros in `derived.duckdb`, computed on read.** They:
  - parse timestamps;
  - classify every cell (value, redacted, withheld, …);
  - link sightings of the same search and compare logs;
  - build citations and produce the public view.

  `truth` is attached read-only, so nothing here can alter the released data.
- **Layer 3: the `cache` schema in `derived.duckdb`.** It holds only the event linking, which is too slow to compute on
  every read.
  - Each cache build is logged in `cache.builds` with fingerprints of the truth data, the linking code and the DuckDB
    version.
  - `check_cache.py` says whether the cache is current.
  - Deleting `derived.duckdb` loses nothing: it is rebuilt from truth.

## Rebuilding and checking

The build code sits beside the databases. The rebuild commands are in `<audit_db>/README.md`. After a rebuild:

- `check_cache.py` exits 1 if the linking cache was built from different truth data, older linking code or another
  DuckDB version.
- `gen_coverage.py` regenerates [coverage.md](coverage.md).
- `gen_stats.py` regenerates [stats.md](stats.md). It makes several full scans and runs the pre-export plate check,
  which fails if any plate shape survives in `sightings_public`.
- `check_docs.py` fails if any table, column, view, macro or enumeration value is undocumented, or a link is dead.
- `verify_provenance.py` re-opens sampled rows in the original files with an independent reader, checks that each is
  where the database says, and re-hashes every source file against the timestamped evidence manifest. It also checks
  that every San Mateo PD search id is printed on its stated PDF page. Results are in
  [provenance.md](provenance.md).

## Status

This is a local research database and has not been published yet.
- **Civilian data:** the underlying records are public, but `truth` holds civilian licence plates exactly as some
  agencies released them. Only `sightings_public`, with plates tokenized, is meant for export (see [pii.md](pii.md)).
- **Plan:** move the build code into the repository with a make target, and have CI publish a public-safe Parquet
  export.
- **Also planned:** an evidence-pack tool that bundles a query's results with the original files behind them, for a
  reporter to spot-check.
