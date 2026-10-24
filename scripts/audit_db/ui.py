"""Open the audit DB read-only in the DuckDB UI (browser notebook at http://localhost:4213).
Queries run locally; the UI's page assets are fetched from ui.duckdb.org. Ctrl-C to stop.
  uv run --locked --project scripts/audit_db python scripts/audit_db/ui.py
"""
import time

from paths import CODE, audit_dir, duck_connect, sql_str, use_spill_dir

A = audit_dir()
con = duck_connect(A / "derived.duckdb", read_only=True, spill_root="")   # no spill directory until init.sql has run
con.execute(f"ATTACH {sql_str(A / 'truth.duckdb')} AS truth (READ_ONLY)")   # init.sql's relative ATTACH then leaves it alone
con.execute((CODE / "init.sql").read_text())
use_spill_dir(con, A / "spill")   # init.sql turns spilling off for the CLI; the UI process spills into its own directory
con.execute("INSTALL ui; LOAD ui; CALL start_ui_server()")
print("DuckDB UI: http://localhost:4213  (try: SELECT * FROM sightings_public LIMIT 20)")
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    con.execute("CALL stop_ui_server()")
