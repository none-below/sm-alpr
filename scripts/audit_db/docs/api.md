# Audit DB: API guide

How to query, build and check the audit database. Table, view, macro and column definitions are in
[schema.md](schema.md).

- **Code:** `scripts/audit_db/` in any checkout of the repo (`<code>` below).
- **Databases:** `truth.duckdb` and `derived.duckdb` in the **audit dir**, the primary checkout's `.claude/audit_db/`
  (`<audit_db>` below). They are gitignored build outputs: they hold released rows verbatim, including civilian data.
- **Environment:** pinned in `<code>/pyproject.toml` and `<code>/uv.lock` (Python, DuckDB, the spreadsheet and PDF
  readers; every version changes what a build produces). Run every script in it:

```sh
uv run --locked --project scripts/audit_db python scripts/audit_db/<script>.py   # from the repo root
```

`--locked` fails, instead of silently re-resolving, when `pyproject.toml` and `uv.lock` disagree.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `AUDIT_DB_DIR` | every tool | Audit dir to use instead of the one found through git (`paths.py`) |
| `AUDIT_DB_THREADS`, `AUDIT_DB_MEMORY` | builds, `gen_stats.py` | DuckDB threads and memory limit (defaults 4 and 6GB) |
| `AUDIT_DB_TEMP`, `AUDIT_DB_MAX_TEMP` | `gen_stats.py` | Parent of the process's spill directory (default `<system tmp>/alpr_duck_tmp`) and the spill cap (default 12GiB) |
| `PLATE_TOKEN_KEY` | `plate_key.py --install` | Plate-token key to install as the key file (CI) |

Heavy jobs are meant to run niced at background QoS: prefix them with `nice -n 19 taskpolicy -b` (macOS).

Every tool and every `init.sql` session spills into its own directory (`paths.duck_temp`), which DuckDB removes on
close. DuckDB names its temp files by block size only, so two processes sharing one spill directory corrupt each
other's queries. If you open the databases with `duckdb.connect` yourself, set `temp_directory` the same way.

## Querying from Python: `audit_client.py`

Read-only helpers. Import the module from `<code>`:

```python
import sys; sys.path.insert(0, "<code>")
import audit_client as ac

con = ac.connect(threads=1, memory="1GB")
pairs = [("<release_id>", 1), ("<release_id>", 3)]
ac.sightings_for(con, pairs).fetchall()          # parsed rows, the `sightings` columns
ac.citations_for(con, pairs).df()                # where each row is in the original, with a citation
ac.event(con, "u:<Flock search UUID>")           # one search, with every log's copy
for r in ac.drill(con, "u:<Flock search UUID>"):
    print(r["producer"], r["basis"], r["src_row"], r["reason_state"], r["citation"])
```

| Function | Returns |
|---|---|
| `connect(audit_dir=None, threads=4, memory="4GB", temp_dir=None, max_temp="8GiB")` | A read-only connection to `derived.duckdb` with `truth` attached read-only. Spills go to the connection's own subdirectory of `temp_dir` (default `<system tmp>/alpr_duck_tmp`) and are capped at `max_temp`, so a runaway query fails instead of filling the disk. `audit_dir` defaults to the audit dir. |
| `sightings_for(con, pairs)` | A DuckDB relation with the `sightings` columns for a list of `(release_id, row_no)` pairs, ordered by `(release_id, row_no)`. Pairs not in truth are absent. Holds released text: local only. |
| `citations_for(con, pairs)` | A relation with the `sighting_sources` columns for the same pairs: where each row is in the original, with a ready-to-paste citation. No cell values. |
| `sightings_sql(pairs)`, `citations_sql(pairs)` | The SQL those two functions run, for use inside your own query. |
| `event(con, eid)` | One search (event) as the `events` view shows it, as a dict with its sightings; `None` if the id is unknown. |
| `drill(con, eid, surfaces=False)` | One dict per sighting of the search (every log's copy), ordered by producer, release and row: the same rows as the `event_sightings()` macro, with states and citations. `surfaces=True` adds the released Reason and Case # text (local only). |

The helpers select rows by literal `release_id` / `row_no` first, then parse and cite them with the same SQL the full
views use (`sql_templates.py`), so they are fast for any set of rows and give exactly what the views give. Event ids
other than `u:` are build-specific: persist `(release_id, row_no)` instead.

From a shell, `audit_client.py <event_id> [audit_dir]` prints the states and citations of one search.

## SQL sessions: `init.sql`, `ui.py`

- **DuckDB CLI** (version 1.5.5, as pinned), from the audit dir:
  `duckdb -bail -readonly -init <code>/init.sql derived.duckdb`. `init.sql` attaches `truth` read-only, fails the session
  unless that `truth.duckdb` sits beside the opened `derived.duckdb`, and sets limits (4 threads, 4 GB, spill capped).
- **DuckDB UI:** `ui.py` opens the same session in the browser notebook at `http://localhost:4213` (page assets come
  from ui.duckdb.org; queries run locally).

## Building

Run builds from a fresh worktree off `origin/main` with no local edits under `scripts/audit_db/`: the build records
the repo commit, which identifies the build code as well as the committed inputs.

| Script | What it does | Arguments |
|---|---|---|
| `muckrock_ingest.py` | Stages every MuckRock release (file, zip member, sheet) as a CSV for the truth build. Also the MuckRock loader `build_truth.py` imports. | `stage <evidence dir> <tmp dir>` |
| `build_truth.py` | Builds `truth.duckdb`: released rows verbatim from the repo checkout (NDJSON conversions, San Mateo PD's audit PDFs) and the staged MuckRock releases, plus the authored facts. Refuses to run from a different checkout than `--repo`. | `OUT.duckdb TMPDIR [--repo CHECKOUT]` |
| `smpd_pdf_loader.py` | San Mateo PD's PDF reader, used by `build_truth.py`. Run on its own, it prints a per-PDF summary. | `PDF...` |
| `build_derived.py` | Builds `derived.duckdb`: the views and macros, `public_macros.sql`, and the event-linking cache. Run from the audit dir. | `truth.duckdb derived.duckdb [--views-only]` (refresh views and macros, keep the cache) |
| `sql_templates.py` | Module: the parse and citation SQL shared by the full views, the row-lookup macros and `audit_client.py`. | — |
| `cache_fingerprint.py` | Module: what the cache was built from (truth fingerprint, linking-code hash, DuckDB version). | — |
| `paths.py` | Module: where the audit dir is. | — |
| `plate_key.py` | The plate-token key: `--check` validates the key file (status only, never the key); `--install` writes `PLATE_TOKEN_KEY` to the key file (CI). Needed only for `sightings_public` and `plate_token()`. | `--check` or `--install` |

Authored facts, loaded into truth with their citations:

| File | Holds |
|---|---|
| `producers.json` | Which organization produced each MuckRock release, when the requesting agency is not it, with the basis |
| `dispositions.json` | Fields a producer's cover letter says were withheld or redacted, per release pattern |
| `layouts.json` | Row ranges released under the wrong column labels, and the labels to read them by |

Edit them through a PR, with the citation. Editing them changes truth on the next build (and the cache's truth
fingerprint, except for the `source` text).

### Rebuild sequence

```sh
setopt interactivecomments 2>/dev/null || true   # zsh: lets the trailing # comments paste into an interactive shell
R=<fresh worktree>; C=$R/scripts/audit_db; A=<primary checkout>/.claude/audit_db; T=<scratch dir with ~25 GB free>
py()   { uv run --locked --project "$C" python "$@"; }                         # quick steps
bgpy() { nice -n 19 taskpolicy -b uv run --locked --project "$C" python "$@"; }  # heavy steps, at background QoS
cd "$R"
bgpy "$C/muckrock_ingest.py" stage <evidence dir> "$T"                                             # ~6 min
bgpy "$C/build_truth.py" "$A/truth_new.duckdb" "$T" && mv "$A/truth_new.duckdb" "$A/truth.duckdb"   # ~10 min
rm -f "$A/derived.duckdb" && (cd "$A" && bgpy "$C/build_derived.py" truth.duckdb derived.duckdb)  # ~12 min
py "$C/check_cache.py"                                   # seconds; exit 1 = stale cache
bgpy "$C/gen_coverage.py" && bgpy "$C/gen_stats.py"      # local pages in <audit_db>/docs/; stats needs the plate key
py "$C/check_docs.py"                                    # seconds
bgpy "$C/verify_provenance.py"                           # needs poppler's pdftotext
```

## Checking

| Script | Checks | Arguments |
|---|---|---|
| `check_cache.py` | Whether the linking cache was built from the current truth, linking code and DuckDB version. Exit 1 = stale: rebuild derived. | `[--audit-dir DIR]` |
| `check_docs.py` | That every table, view, macro, column and enumeration value is in [schema.md](schema.md), every function and script is in this guide, and no docs link is dead. | `[--audit-dir DIR]` |
| `verify_provenance.py` | Re-reads sampled rows from the original released files with an independent reader and compares them with truth; re-hashes the sources. | `[--audit-dir DIR] [--truth DB] [--per-release N] [--per-layout N] [--deep N] [--deep-releases K] [--smpd N] [--only PATTERN] [--seed N]` |
| `gen_coverage.py` | Writes `<audit_db>/docs/coverage.md` (local): what each producer's logs cover. | `[--audit-dir DIR]` |
| `gen_stats.py` | Writes `<audit_db>/docs/stats.md` (local): corpus-wide counts and the pre-export plate check. Heavy. | `[--audit-dir DIR]` |

`verify_provenance.py` options: `--per-release` rows sampled within each release's first 3,000 (default 2);
`--deep` rows beyond row 3,000 in each of `--deep-releases` large releases (defaults 2 and 16; `-1` = all, slow; `0`
for a quick run); `--per-layout` rows inside each layout-corrected range (default 3); `--smpd` San Mateo PD rows
checked on their PDF page (default 40, needs `pdftotext`); `--only` a `release_id` LIKE pattern.
