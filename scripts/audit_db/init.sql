-- Session setup for any DuckDB client opening derived.duckdb. From the audit dir (<primary checkout>/.claude/audit_db):
--   duckdb -readonly -init <repo>/scripts/audit_db/init.sql derived.duckdb
-- The relative path below resolves against that working directory; ui.py attaches truth by absolute path first, which
-- this IF NOT EXISTS then leaves alone. The check after it fails the session unless the attached truth sits beside the
-- opened derived, so a different truth.duckdb in the working directory is never picked up silently.
-- ATTACH is instance-wide, so the DuckDB UI and every cursor see it. No key here: plate_token() reads ~/.config itself.
ATTACH IF NOT EXISTS 'truth.duckdb' AS truth (READ_ONLY);
SELECT error('init.sql: attached ' || t.path || ', which is not beside ' || d.path || ': run the CLI from the audit dir')
FROM duckdb_databases() t, duckdb_databases() d
WHERE t.database_name = 'truth' AND d.database_name = current_database()
  AND regexp_replace(t.path, '[^/]*$', '') <> regexp_replace(d.path, '[^/]*$', '');
SET threads = 4;
SET memory_limit = '4GB';   -- modest, so the machine stays usable; raise for big scans
SET temp_directory = '/tmp/alpr_duck_tmp';
SET max_temp_directory_size = '8GiB';   -- a runaway query errors instead of filling the disk
SET preserve_insertion_order = false;
