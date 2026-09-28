"""What the derived cache was computed from — shared by build_derived.py (records it) and check_cache.py (compares it).

truth fingerprint: md5 over, per release, its id, row count, hashes (container, zip member, content), producer, audit,
header and the inputs of its public_release_id (pra_id, container_path, member, sheet), plus every row of
truth.release_layouts and truth.release_dispositions except their `source` citation text, plus the parsed SMPD rows
(id, count/time line and user line per row of truth.smpd_pdf_rows: those rows are the loader's reading of the PDFs, so
a loader or pymupdf change can move them while the PDFs' hashes stay the same). Changes when truth is rebuilt
with different inputs or different authored facts (producers.json, layouts.json, dispositions.json), all of which change
the org / t / nets / time frame the link keys are made of. Columns an older truth lacks are skipped (the fingerprint
then differs, i.e. stale, which is right).
code fingerprint: sha256 over the layer-3 code in build_derived.py plus the stored definitions of the views and macros
the cache reads (changes when anything that feeds event linking changes; other view edits do not).
DuckDB version: sighting_id, k3, k5 and event_key are DuckDB hash() values, which DuckDB does not promise to keep across
versions, so a cache built under another version is stale.
"""
import hashlib
from pathlib import Path

RELEASE_COLS = ["release_id", "n_rows", "container_sha256", "member_sha256", "content_sha256", "producer", "audit", "header",
                "pra_id", "container_path", "member", "sheet"]
AUTHORED = {"release_layouts": ["release_pattern", "src_row_from", "src_row_to", "mapping"],
            "release_dispositions": ["release_pattern", "field", "disposition"]}
CACHE_INPUTS_SQL = """SELECT
  (SELECT string_agg(view_name || ':' || sql, chr(10) ORDER BY view_name) FROM duckdb_views()
    WHERE database_name = current_database() AND view_name IN ('release_meta', 'flock_rows', 'sightings_flock', 'sightings_smpd', 'sightings'))
  || (SELECT string_agg(function_name || ':' || macro_definition, chr(10) ORDER BY function_name, macro_definition) FROM duckdb_functions()
    WHERE NOT internal AND function_name IN ('flock_ts', 'tf_bound', 'flock_int', 'cell_state', 'agency_mask', 'exemption_cite',
                                             'clean_value', 'is_withheld'))"""


def layer3_code(build_derived_path):
    src = Path(build_derived_path).read_text()
    return src[src.index("# ---------------- Layer 3"):src.index("# ---------------- Layer 2 again")]


def code_sha(con, build_derived_path):
    return hashlib.sha256((layer3_code(build_derived_path) + con.execute(CACHE_INPUTS_SQL).fetchone()[0]).encode()).hexdigest()[:12]


def _rows_sql(con, table, want, order):
    have = {c for (c,) in con.execute("SELECT column_name FROM duckdb_columns() WHERE database_name = 'truth' AND table_name = ?",
                                      [table]).fetchall()}
    cells = [f'coalesce(CAST("{c}" AS VARCHAR), chr(0))' for c in want if c in have]   # chr(0) keeps NULL <> ''
    keep = ", release_id" if order == "release_id" else ""
    return (f"coalesce((SELECT string_agg(x, chr(10) ORDER BY {order}) FROM (SELECT {' || chr(31) || '.join(cells)} AS x{keep} "
            f"FROM truth.{table})), '')")


def truth_fp(con):
    parts = [_rows_sql(con, "releases", RELEASE_COLS, "release_id")] + [_rows_sql(con, t, c, "x") for t, c in AUTHORED.items()]
    if con.execute("SELECT count(*) FROM duckdb_tables() WHERE database_name = 'truth' AND table_name = 'smpd_pdf_rows'").fetchone()[0]:
        parts.append("coalesce((SELECT md5(string_agg(concat_ws(chr(31), release_id, row_no, id, coalesce(count_time_line, chr(0)), "
                     "coalesce(user_line, chr(0))), chr(10) ORDER BY release_id, row_no)) FROM truth.smpd_pdf_rows), '')")
    return con.execute("SELECT md5(" + " || chr(30) || ".join(parts) + ")").fetchone()[0]


def duckdb_version(con):
    return con.execute("SELECT version()").fetchone()[0]
