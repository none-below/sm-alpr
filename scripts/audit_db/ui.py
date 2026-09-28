"""Open the audit DB read-only in the DuckDB UI (browser notebook at http://localhost:4213).
Queries run locally; the UI's page assets are fetched from ui.duckdb.org. Ctrl-C to stop.
  uv run --locked --project scripts/audit_db python scripts/audit_db/ui.py
"""
import time

import duckdb

from paths import CODE, audit_dir

A = audit_dir()
con = duckdb.connect(str(A / "derived.duckdb"), read_only=True)
truth = str(A / "truth.duckdb").replace("'", "''")
con.execute(f"ATTACH '{truth}' AS truth (READ_ONLY)")   # init.sql's relative ATTACH then leaves it alone
con.execute((CODE / "init.sql").read_text())
con.execute("INSTALL ui; LOAD ui; CALL start_ui_server()")
print("DuckDB UI: http://localhost:4213  (try: SELECT * FROM sightings_public LIMIT 20)")
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    con.execute("CALL stop_ui_server()")
