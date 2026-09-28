"""Is the derived cache (event linking) current? Compares cache.builds with the truth, code and DuckDB version it would be
built from now.

  uv run --locked --project scripts/audit_db python scripts/audit_db/check_cache.py [--audit-dir DIR]

Exit 0: every cache table was built from the current truth.duckdb, the current linking code and this DuckDB version.
Exit 1: stale — rebuild derived (build_derived.py; see README.md) before relying on events /
read_field / event_sightings. A different DuckDB version counts as stale because the cached ids are DuckDB hash() values.
"""
import argparse
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
import cache_fingerprint  # noqa: E402
from paths import audit_dir  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--audit-dir", help="directory holding the databases (default: paths.audit_dir())")
A = Path(ap.parse_args().audit_dir or audit_dir())
con = duckdb.connect(str(A / "derived.duckdb"), read_only=True)
con.execute("SET threads=1; SET memory_limit='1GB'")   # reads catalogs and the small truth tables only
con.execute(f"ATTACH IF NOT EXISTS '{A / 'truth.duckdb'}' AS truth (READ_ONLY)")
cols = {c for (c,) in con.execute("""SELECT column_name FROM duckdb_columns()
                                     WHERE database_name = current_database() AND schema_name = 'cache' AND table_name = 'builds'""").fetchall()}
if not cols:
    print("cache.builds missing: the cache was never built — run a full build_derived.py")
    sys.exit(1)
now_fp, now_code = cache_fingerprint.truth_fp(con), cache_fingerprint.code_sha(con, Path(__file__).parent / "build_derived.py")
now_ver = cache_fingerprint.duckdb_version(con)
ver_col = "duckdb_version" if "duckdb_version" in cols else "NULL"
stale = False
for name, built_at, fp, code, ver in con.execute(f"""SELECT name, built_at, truth_fingerprint, code_sha, {ver_col} FROM cache.builds
                                                    QUALIFY row_number() OVER (PARTITION BY name ORDER BY built_at DESC) = 1""").fetchall():
    problems = ([] if fp == now_fp else ["truth changed since the cache was built"]) + \
               ([] if code == now_code else ["linking code or the views it reads changed"]) + \
               ([] if ver == now_ver else [f"built with DuckDB {ver or '(version not recorded)'}, this is {now_ver}"])
    stale |= bool(problems)
    print(f"cache.{name}: built {built_at:%Y-%m-%d %H:%M} UTC — " + ("; ".join(problems) if problems else "current"))
sys.exit(1 if stale else 0)
