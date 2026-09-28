# ALPR audit database — build code

The documentation is in [docs/README.md](docs/README.md), and [docs/provenance.md](docs/provenance.md) is the place
to start.

The code, SQL, authored facts and docs live here, in git. The databases are build outputs and never go in git: they
hold raw plates and verbatim released text ([docs/pii.md](docs/pii.md)). They live in the **audit dir**, the primary
checkout's `.claude/audit_db/` (gitignored), which every worktree finds through git's common dir (`paths.py`;
`AUDIT_DB_DIR` overrides). The rule the build follows is: **store only authoritative data, compute everything else on
read, and cache only what is too slow — firewalled from ground truth.**

| File | Role |
|---|---|
| audit dir: `truth.duckdb` | Layer 1: released rows, verbatim, plus authored facts with citations |
| audit dir: `derived.duckdb` | Layers 2–3: views and macros computed on read, plus the `cache` schema (event linking) |
| `pyproject.toml`, `uv.lock` | The pinned environment (Python, DuckDB, the spreadsheet and PDF readers): every version here changes what a build produces. Run every script with `uv run --locked --project scripts/audit_db python scripts/audit_db/<script>.py` (`--locked` fails instead of silently re-resolving if `pyproject.toml` and `uv.lock` disagree) |
| `paths.py` | Where the audit dir is |
| `build_truth.py` | Builds truth from the repo checkout (Redwood City / Los Altos NDJSON, SMPD audit PDFs) and the MuckRock evidence directory |
| `smpd_pdf_loader.py` | SMPD audit-PDF reader for `build_truth.py` (pymupdf text layer, one row per search-id block, positional check on Acrobat-edited pages); run on its own with `PDF...` arguments it prints a per-PDF summary |
| `muckrock_ingest.py` | MuckRock loader for `build_truth.py`; its `stage` step converts every released file to CSV first (cached in `<T>/muckrock_units/staged.json`) |
| `build_derived.py` | Builds derived (`--views-only` refreshes the views without recomputing the cache) |
| `sql_templates.py` | The parse and citation SQL shared by the full views, the row-lookup macros and `audit_client.py` (importing it runs nothing) |
| `public_macros.sql` | Plate tokens, the civilian-PII scrub and `sightings_public` (run by `build_derived.py`) |
| `cache_fingerprint.py`, `check_cache.py` | What the linking cache was built from (truth, code, DuckDB version); `check_cache.py` exits 1 when it is stale |
| `producers.json`, `dispositions.json`, `layouts.json` | Authored facts: who produced each MuckRock release, cover-letter withholdings, rows released under the wrong labels |
| `gen_coverage.py`, `gen_stats.py` | Regenerate `docs/coverage.md` and `docs/stats.md` (stats: several full scans, heavy) |
| `check_docs.py` | Fails if a table, column, view, macro or enumeration value is undocumented, or a docs link is dead |
| `verify_provenance.py` | Re-reads sampled rows from the original files with independent readers and re-hashes the sources |
| `plate_key.py` | Installs, checks (`--check`) and loads the plate-token key (never prints it) |
| `audit_client.py` | Python helpers for engineers: `connect()`, `sightings_for()` / `citations_for()` by (release_id, row_no), `event()`, `drill()` |
| `init.sql`, `ui.py` | Session setup for the DuckDB CLI (from the audit dir: `duckdb -readonly -init <repo>/scripts/audit_db/init.sql derived.duckdb`) and the DuckDB UI (4 threads, 4 GB, set by `init.sql`) |
| audit dir: `ingest.log` | Output of the last derived build (step timings, tier counts, cache check) |

## Rebuild

Run from a fresh worktree off `origin/main`, with no local edits under `scripts/audit_db/`. The build records the repo
commit it read from (`build_info.repo_commit`), which identifies the build code as well as the committed inputs:
`build_truth.py` refuses to run from a different checkout than `--repo`, records `repo_inputs_dirty = True` if the code
or the inputs have uncommitted edits, and warns if the checkout is stale. Re-run the `stage` step whenever `muckrock_ingest.py` changes or evidence is added: `build_truth.py` loads from
the cached `staged.json`.

Builds run niced at background QoS (`nice -n 19 taskpolicy -b`) with modest DuckDB limits (4 threads, 6 GB), so the
laptop stays usable. For a faster build when nothing else is running, set `AUDIT_DB_THREADS=8 AUDIT_DB_MEMORY=12GB`.
`gen_stats.py` spills to `AUDIT_DB_TEMP` (default `<system tmp>/alpr_duck_tmp`), capped at `AUDIT_DB_MAX_TEMP` (12GiB).

```sh
R=<fresh worktree>; C=$R/scripts/audit_db; A=<primary checkout>/.claude/audit_db; T=<scratch dir with ~25 GB free>
py()   { uv run --locked --project "$C" python "$@"; }                         # quick steps
bgpy() { nice -n 19 taskpolicy -b uv run --locked --project "$C" python "$@"; }  # heavy steps, at background QoS
cd "$R"
bgpy "$C/muckrock_ingest.py" stage <evidence dir> "$T"                        # ~6 min
bgpy "$C/build_truth.py" "$A/truth_new.duckdb" "$T" && mv "$A/truth_new.duckdb" "$A/truth.duckdb"  # ~10 min at the defaults (SMPD PDFs: 2-3 min of it)
rm -f "$A/derived.duckdb" && (cd "$A" && bgpy "$C/build_derived.py" truth.duckdb derived.duckdb)  # ~12 min at the defaults (689.6 s on 2026-09-25)
py "$C/check_cache.py"                                   # seconds; exit 1 = stale cache
bgpy "$C/gen_coverage.py" && bgpy "$C/gen_stats.py"      # stats: heavy, needs the plate key; both rewrite docs/ here, so the git diff shows what changed
py "$C/check_docs.py"                                    # seconds; after gen_* (it checks links to their pages)
bgpy "$C/verify_provenance.py"                           # needs poppler's pdftotext
```

`gen_stats.py` took 141 s on a quiet machine and 592 s niced under memory pressure (2026-09-27); it scans `sightings`, `sightings_public`, `flock_rows` and
the linking cache once each, and exits 1 if a section failed or the pre-export plate check could not be reported
(it refuses to report it unless `sightings_public` has as many rows as `sightings`). `verify_provenance.py` reads each
original only up to its deepest sampled row, so its time is set by the deep samples: the 548,109-row Santa Rosa sheet
took about 2 minutes under load (`--deep-releases 0` for a quick run, `-1` to deep-sample every large release;
`--only 'mr:214823:%'` to check one request).

The plate-token key (`~/.config/sm-alpr/plate_token_key`, 64 hex chars) is needed only to build or query
`sightings_public` and `plate_token()`; `py "$C/plate_key.py" --check` validates it without printing it.
