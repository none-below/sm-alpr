# ALPR audit database — build code

A DuckDB database of Flock ALPR search-audit logs released under public-records requests, built so that every row
traces back to the released file, sheet or page and row it came from.

- [docs/schema.md](docs/schema.md): every table, view, macro and column, and the state and tier values.
- [docs/api.md](docs/api.md): querying from Python and SQL, building, checking, and the pinned environment.

The code, SQL and authored facts live here, in git. The databases are build outputs and never go in git: they hold
released rows verbatim, including civilian data. They live in the **audit dir**, the primary checkout's
`.claude/audit_db/` (gitignored), which every worktree finds through git's common dir (`paths.py`; `AUDIT_DB_DIR`
overrides).

The build follows one rule: **store only authoritative data, compute everything else on read, and cache only what is
too slow — firewalled from ground truth.** `truth.duckdb` holds the released rows verbatim plus authored facts with
citations; `derived.duckdb` holds views and macros computed on read over `truth` (attached read-only) and a `cache`
schema for event linking.

Run any script in the pinned environment, from the repo root:

```sh
uv run --locked --project scripts/audit_db python scripts/audit_db/<script>.py
```
