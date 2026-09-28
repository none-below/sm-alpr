"""Small importable helpers for engineers querying the audit DB from Python. Read-only; nothing is written.

    import sys; sys.path.insert(0, "<repo>/scripts/audit_db")
    import audit_client as ac

    con = ac.connect()                             # derived + truth read-only, 4 threads / 4 GB, spill capped
    pairs = [("<release_id>", 1), ("<release_id>", 3)]
    ac.sightings_for(con, pairs).fetchall()        # parsed rows, same columns as the `sightings` view
    ac.citations_for(con, pairs).df()              # where each row is in the original + citation (`sighting_sources`)
    ac.event(con, "u:<Flock search UUID>")
    # {'event_id': 'u:…', 'event_key': …, 'n_sightings': …, 'n_logs': …, 'weakest_link': '<tier>', 'logs': [...],
    #  'sightings': [(release_id, row_no, producer, audit, basis), ...]}
    for r in ac.drill(con, "u:<Flock search UUID>"):
        print(r["producer"], r["basis"], r["reason_state"], r["citation"])

Why these exist: a row lookup must reach truth through literal release_id / row_no filters so DuckDB skips every other
row group; a join from a list of rows to the full `sightings` view parses all ~140M rows instead. These helpers select
the rows first (constant IN-lists), then parse and cite them with the SAME SQL templates the full views use
(sql_templates.py), so the result is exactly what `sightings` / `sighting_sources` return for those rows.

Local only: `sightings_for` returns released text (`*_surface`, reason, case_no, filters, text_prompt), which can hold
civilian plates. Export `sightings_public` columns, states, citations (docs/schema.md).
Event ids other than `u:` (k5:, k3:, x:) are build-specific; persist (release_id, row_no) instead (docs/schema.md).
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from paths import audit_dir as default_audit_dir, duck_connect  # noqa: E402
from sql_templates import (flock_rows_sql, sightings_flock_sql, sightings_smpd_sql, sources_flock_sql,  # noqa: E402
                           sources_smpd_sql)


def _s(v):
    return "'" + str(v).replace("'", "''") + "'"


def connect(audit_dir=None, threads=4, memory="4GB", temp_dir=None, max_temp="8GiB"):
    """Read-only connection to <audit_dir>/derived.duckdb with truth attached (READ_ONLY).

    audit_dir defaults to paths.audit_dir(): the primary checkout's .claude/audit_db/, or AUDIT_DB_DIR.
    Spills go to this process's own directory under temp_dir (default <audit_dir>/spill; paths.use_spill_dir) and are
    capped at max_temp, so a runaway query fails instead of filling the disk; temp_dir="" disables spilling.
    Settings belong to the DuckDB instance, and every connection to one database file in one Python process shares it:
    a second connect() changes threads and memory for both, and keeps the instance's spill directory. On a shared machine use threads=1, memory='1GB'.

    >>> con = connect(threads=1, memory="1GB")
    >>> con.sql("SELECT key, value FROM truth.build_info").fetchall()
    """
    A = Path(audit_dir or default_audit_dir())
    con = duck_connect(A / "derived.duckdb", read_only=True, spill_parent=A / "spill" if temp_dir is None else temp_dir,
                       threads=int(threads), memory_limit=memory, max_temp_directory_size=max_temp)
    con.execute(f"ATTACH IF NOT EXISTS {_s(A / 'truth.duckdb')} AS truth (READ_ONLY)")
    return con


def _groups(pairs):
    """[(release_id, row_no), ...] -> ({flock release_id: [row_no]}, {smpd release_id: [row_no]}), de-duplicated."""
    flock, smpd = defaultdict(set), defaultdict(set)
    for rid, n in pairs:
        (smpd if str(rid).startswith("smpd:") else flock)[str(rid)].add(int(n))
    return ({k: sorted(v) for k, v in flock.items()}, {k: sorted(v) for k, v in smpd.items()})


def _rows(table, groups):
    """One pruned scan per release (literal release_id + row_no IN-list), unioned. None when there are no rows."""
    if not groups:
        return None
    return "\n  UNION ALL ".join(f"SELECT * FROM {table} WHERE release_id = {_s(rid)} AND row_no IN ({', '.join(map(str, ns))})"
                                 for rid, ns in groups.items())


def _branches(pairs, flock_sql, smpd_sql, empty_view):
    flock, smpd = _groups(pairs)
    parts, ctes = [], []
    if flock:
        ctes.append(f"raw AS ({_rows('truth.flock_audit_rows', flock)})")
        parts.append(flock_sql)
    if smpd:
        ctes.append(f"rsm AS ({_rows('truth.smpd_pdf_rows', smpd)})")
        parts.append(smpd_sql)
    if not parts:
        return f"SELECT * FROM {empty_view} LIMIT 0"
    return ("WITH " + ",\n".join(ctes) + "\nSELECT * FROM (" + ") UNION ALL BY NAME SELECT * FROM (".join(parts) + ")"
            " ORDER BY release_id, row_no")


def sightings_sql(pairs):
    """The SQL sightings_for() runs (for use inside your own query)."""
    return _branches(pairs, sightings_flock_sql(f"({flock_rows_sql('raw')})"), sightings_smpd_sql("rsm"), "sightings")


def citations_sql(pairs):
    """The SQL citations_for() runs."""
    return _branches(pairs, sources_flock_sql("raw"), sources_smpd_sql("rsm"), "sighting_sources")


def sightings_for(con, pairs):
    """Parsed sightings for [(release_id, row_no), ...]: a DuckDB relation with the `sightings` columns, ordered by
    (release_id, row_no). Rows not in truth are simply absent. Local only (released text).

    >>> rel = sightings_for(con, [("<release_id>", 1)])
    >>> rel.fetchall()      # or rel.df(), rel.show(), rel.filter("reason_state = 'value'")
    """
    return con.sql(sightings_sql(pairs))


def citations_for(con, pairs):
    """Where each (release_id, row_no) is in the original, with a ready-to-paste citation: a relation with the
    `sighting_sources` columns (public_release_id, document, locator, open_url, sha256, member_sha256, link, citation, ...).
    Safe to export by content (no cell values).

    >>> [c for (c,) in citations_for(con, pairs).select("citation").fetchall()]
    """
    return con.sql(citations_sql(pairs))


def _event_rows(con, eid):
    # The stored key finds the event (a scan of one integer column); the id comparison runs in Python, so the
    # VARCHAR column is never compared on every cache row (that is what makes `events WHERE event_id = …` slow).
    h = con.execute("SELECT hash(?)", [eid]).fetchone()[0]
    rows = con.execute(f"""SELECT event_id, release_id, row_no, producer, audit, basis FROM cache.sighting_event
                           WHERE event_key = {int(h)}""").fetchall()
    return h, [r[1:] for r in rows if r[0] == eid]


def event(con, eid):
    """One search (event) by its event_id, as the `events` view would show it, plus its sightings; None if unknown.

    >>> event(con, "u:<Flock search UUID>")["n_logs"]   # how many producers' logs recorded that search
    """
    h, rows = _event_rows(con, eid)
    if not rows:
        return None
    return {"event_id": eid, "event_key": h, "n_sightings": len(rows), "n_logs": len({r[2] for r in rows}),
            "weakest_link": max(r[4] for r in rows), "logs": sorted({r[2] for r in rows}),
            "sightings": sorted(rows)}   # (release_id, row_no, producer, audit, basis)


def drill(con, eid, surfaces=False):
    """Every log's copy of one search with where to find it: one dict per sighting, ordered by producer, release, row.
    Same rows as the event_sightings(eid) macro, found by event_key and parsed by (release_id, row_no) lookups.
    surfaces=True adds reason_surface / case_surface (released text: local only).

    >>> for r in drill(con, "u:<Flock search UUID>"):
    ...     print(r["producer"], r["basis"], r["src_row"], r["reason_state"], r["citation"])
    """
    _, rows = _event_rows(con, eid)
    if not rows:
        return []
    pairs = [(r[0], r[1]) for r in rows]
    keep = ["src_row", "org", "t", "nets", "reason_state", "case_state", "name_state", "plate_state"] + \
           (["reason_surface", "case_surface"] if surfaces else [])
    rel = sightings_for(con, pairs)
    cols = rel.columns
    s = {(d["release_id"], d["row_no"]): d for d in (dict(zip(cols, t)) for t in rel.fetchall())}
    crel = citations_for(con, pairs)
    ccols = crel.columns
    c = {(d["release_id"], d["row_no"]): d for d in (dict(zip(ccols, t)) for t in crel.fetchall())}
    out = []
    for rid, n, producer, audit, basis in rows:
        d, cd = s.get((rid, n), {}), c.get((rid, n), {})
        out.append({"producer": producer, "audit": audit, "basis": basis, "release_id": rid, "row_no": n,
                    "public_release_id": cd.get("public_release_id"), **{k: d.get(k) for k in keep},
                    "citation": cd.get("citation"), "open_url": cd.get("open_url"), "sha256": cd.get("sha256")})
    return sorted(out, key=lambda r: (r["producer"], r["release_id"], r["row_no"]))


if __name__ == "__main__":
    # uv run --locked --project scripts/audit_db python scripts/audit_db/audit_client.py <event_id> [audit_dir]: states + citations of one search (no released text)
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    c = connect(sys.argv[2] if len(sys.argv) > 2 else None, threads=1, memory="1GB")
    for r in drill(c, sys.argv[1]):
        print(r["producer"], r["basis"], r["reason_state"], r["case_state"], r["citation"], sep=" | ")
