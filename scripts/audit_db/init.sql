-- Session setup for any DuckDB client opening derived.duckdb (CLI: duckdb -readonly -init init.sql derived.duckdb).
-- ATTACH is instance-wide, so the DuckDB UI and every cursor see it. No key here: plate_token() reads ~/.config itself.
ATTACH IF NOT EXISTS '/Users/bc/src/github.com/none-below/sm-alpr/.claude/audit_db/truth.duckdb' AS truth (READ_ONLY);
SET threads = 4;
SET memory_limit = '4GB';   -- modest, so the machine stays usable; raise for big scans
SET temp_directory = '/tmp/alpr_duck_tmp';
SET max_temp_directory_size = '8GiB';   -- a runaway query errors instead of filling the disk
SET preserve_insertion_order = false;
