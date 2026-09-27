-- Session setup for any DuckDB client opening derived.duckdb. From the audit dir (<primary checkout>/.claude/audit_db):
--   duckdb -readonly -init <repo>/scripts/audit_db/init.sql derived.duckdb
-- The relative path below resolves against that working directory; ui.py attaches truth by absolute path first, which
-- this IF NOT EXISTS then leaves alone.
-- ATTACH is instance-wide, so the DuckDB UI and every cursor see it. No key here: plate_token() reads ~/.config itself.
ATTACH IF NOT EXISTS 'truth.duckdb' AS truth (READ_ONLY);
SET threads = 4;
SET memory_limit = '4GB';   -- modest, so the machine stays usable; raise for big scans
SET temp_directory = '/tmp/alpr_duck_tmp';
SET max_temp_directory_size = '8GiB';   -- a runaway query errors instead of filling the disk
SET preserve_insertion_order = false;
