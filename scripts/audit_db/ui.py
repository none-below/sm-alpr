"""Open the audit DB read-only in the DuckDB UI (browser notebook at http://localhost:4213).
Queries run locally; the UI's page assets are fetched from ui.duckdb.org. Ctrl-C to stop.
  uv run --with duckdb python ui.py
"""
import time
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
con = duckdb.connect(str(HERE / "derived.duckdb"), read_only=True)
con.execute((HERE / "init.sql").read_text())
con.execute("INSTALL ui; LOAD ui; CALL start_ui_server()")
print("DuckDB UI: http://localhost:4213  (try: SELECT * FROM sightings_public LIMIT 20)")
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    con.execute("CALL stop_ui_server()")
