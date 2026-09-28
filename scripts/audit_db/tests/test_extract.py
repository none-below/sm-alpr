"""Fixture tests for extract.py: every input is synthetic (fake plates and names) and built in a temp dir at test time.

Each extraction is compared with the old staging loader (muckrock_ingest.to_csv + DuckDB read_csv, as add_muckrock
loaded truth), so these tests pin the extractor to the rows truth holds, not just to themselves.

  uv run --locked --project scripts/audit_db --group dev pytest scripts/audit_db/tests
Skipped under the repo's root environment (no DuckDB there).
"""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("python_calamine")
openpyxl = pytest.importorskip("openpyxl")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_chunks  # noqa: E402
import extract  # noqa: E402
import muckrock_ingest as mi  # noqa: E402
import paths  # noqa: E402

HDR = ["ID", "Name", "Org Name", "Total Networks Searched", "Search Time", "Reason", "Case #", "License Plate"]


def unit(local_path, *, member=None, sheet="csv", kind="network_audit", name=None, request_id=900001):
    return {"request_id": request_id, "agency": "Test Agency", "request_url": "https://example.invalid/req",
            "local_path": local_path, "url": f"https://example.invalid/{local_path}", "member": member, "sheet": sheet,
            "name": name or member or local_path, "kind": kind, "file_date": "2026-01-02T00:00:00"}


def rows(n=5, start=0):
    return [[f"00000000-0000-4000-8000-{i:012d}", f"User {i % 3}", "Example CA PD", str(i % 7 + 1),
             f"01/0{i % 9 + 1}/2025, 10:00:0{i % 10} AM UTC", f"reason {i}, with comma", f"25-{i:05d}", "0AAA000"]
            for i in range(start, start + n)]


def csv_bytes(header, data, encoding="utf-8-sig", extra_lines=()):
    import csv
    import io
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(header)
    for r in data:
        w.writerow(r)
    text = buf.getvalue() + "".join(extra_lines)
    return text.encode(encoding)


def legacy(u, ev, tmp):
    """The rows the old loader put in truth for this unit: (n_rows, digest) plus to_csv's release-level result."""
    res = mi.to_csv(u, ev, tmp)
    if res is None:
        return None, None
    header = mi.canonical(res["header"])
    table, cols = extract.table_of(u)
    names = "[" + ", ".join(mi.sql_str(c) for c in ["__src_row"] + header) + "]"
    sel = ", ".join(mi.sql_ident(c) if c in header else f"NULL AS {mi.sql_ident(c)}" for c in cols)
    ext = mi.extra_json([h for h in header if h not in cols])
    con = paths.duck_connect(spill_root=tmp / "spill")
    con.execute(f"CREATE TABLE t AS SELECT row_number() OVER () AS row_no, TRY_CAST(\"__src_row\" AS BIGINT) AS src_row, "
                f"{sel}, {ext} AS extra FROM read_csv({mi.sql_str(res['path'])}, all_varchar=true, header=true, "
                f"names={names}, null_padding=true, parallel=false)")
    n, d = con.execute(f"SELECT count(*), bit_xor(hash(row_no, src_row, {', '.join(mi.sql_ident(c) for c in cols)}, "
                       "extra::VARCHAR)) FROM t").fetchone()
    return (n, str(d or 0)), res


def check_same_as_legacy(u, ev, tmp):
    man = extract.extract_unit(u, ev, tmp / "chunks", tmp / "spill")
    assert man["status"] == "ok", man.get("error")
    (n, d), res = legacy(u, ev, tmp / "stage")
    assert (man["n_rows"], man["row_digest"]) == (n, d)
    for f in ("member", "member_sha256", "sheet", "content_sha256", "src_row_basis"):
        assert man[f] == res[f], f
    assert man["header_raw"] == res["header"]
    assert man["header"] == mi.canonical(res["header"])
    return man


def test_csv_quotes_blank_rows_short_rows_aliases_and_extra(tmp_path):
    header = HDR + ["Reason_1", "Unlabeled?", ""]              # alias, an unknown label (-> extra), a blank label
    data = rows(4) + [[""] * 11] + [rows(1, 4)[0][:3]]         # a blank row (skipped) and a short row (NULL-padded)
    data[1][5] = 'a "quoted" reason\nover two lines'
    (tmp_path / "a.csv").write_bytes(csv_bytes(header, data))
    man = check_same_as_legacy(unit("a.csv"), tmp_path, tmp_path)
    assert man["n_rows"] == 5 and man["reader"] == "csv, utf-8"
    assert man["header"][-3:] == ["Reason_2", "Unlabeled?", "column10"]   # Reason_1 aliases to Reason, already taken


def test_cp1252_byte_late_in_the_file_is_read_again_whole(tmp_path):
    data = rows(300)
    body = csv_bytes(HDR, data, encoding="utf-8")
    i = body.index(b"reason 250")
    (tmp_path / "b.csv").write_bytes(body[:i] + b"\x92" + body[i:])   # 0x92: a cp1252 right quote, invalid UTF-8
    man = check_same_as_legacy(unit("b.csv"), tmp_path, tmp_path)
    assert man["reader"] == "csv, cp1252" and man["src_row_basis"].endswith("decoded cp1252")


def test_zip_member_named_short_in_the_catalog(tmp_path):
    with zipfile.ZipFile(tmp_path / "c.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Folder/Some Person/audit_2025.csv", csv_bytes(HDR, rows(20)))
    man = check_same_as_legacy(unit("c.zip", member="Folder/Some Person/audit_2025", name="audit_2025"), tmp_path, tmp_path)
    assert man["member"] == "Folder/Some Person/audit_2025.csv" and man["member_sha256"] == man["content_sha256"]


def test_ambiguous_member_prefix_is_a_failed_chunk(tmp_path):
    with zipfile.ZipFile(tmp_path / "d.zip", "w") as z:
        z.writestr("audit_1.csv", csv_bytes(HDR, rows(2)))
        z.writestr("audit_1.csv.bak", csv_bytes(HDR, rows(2)))
    man = extract.extract_unit(unit("d.zip", member="audit_1."), tmp_path, tmp_path / "chunks", tmp_path / "spill")
    assert man["status"] == "failed" and "matches 2 zip members" in man["error"]
    d = extract.chunk_dir(tmp_path / "chunks", unit("d.zip", member="audit_1."))
    assert (d / "chunk.json").exists() and not (d / "rows.parquet").exists()


def test_xlsx_title_rows_numbers_dates_and_a_long_sheet_name(tmp_path):
    import datetime
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "1_1_2025-1_31_2025-Example CA PD-Network-Audit"      # 46 characters: the catalog keeps 40
    # (openpyxl warns past 31 characters, Excel's limit; releases do carry such names)
    ws.append(["Audit export"])                                     # a title row before the header
    ws.append([])
    ws.append(HDR)
    for r in rows(10):
        ws.append(r[:3] + [float(r[3])] + [datetime.datetime(2025, 1, 2, 10, 0, 5)] + r[5:])
    ws.append([None] * len(HDR))
    wb.save(tmp_path / "e.xlsx")
    man = check_same_as_legacy(unit("e.xlsx", sheet=ws.title[:40]), tmp_path, tmp_path)
    assert man["sheet"] == ws.title and man["n_rows"] == 10 and man["reader"].startswith("python-calamine")


def test_event_log_goes_to_the_event_table(tmp_path):
    hdr = ["Timestamp", "User", "Event Type", "Entity Type", "Entity Details", "Event Id"]
    data = [[f"2025-01-0{i + 1}T00:00:00Z", "admin@example.invalid", "create", "user", f"user {i}",
             f"10000000-0000-4000-8000-{i:012d}"] for i in range(3)]
    (tmp_path / "f.csv").write_bytes(csv_bytes(hdr, data))
    man = check_same_as_legacy(unit("f.csv", kind="event_log"), tmp_path, tmp_path)
    assert man["table"] == "flock_event_rows"


def test_no_header_is_a_failed_chunk(tmp_path):
    (tmp_path / "g.csv").write_bytes(b"1,2\n3,4\n")
    man = extract.extract_unit(unit("g.csv"), tmp_path, tmp_path / "chunks", tmp_path / "spill")
    assert man["status"] == "failed" and "no header row found" in man["error"]


def test_trailing_empty_cells_beyond_the_header_match_the_old_loader(tmp_path):
    body = csv_bytes(HDR, rows(3)).decode().replace("0AAA000\r\n", "0AAA000,,\r\n", 2).encode()   # 2 rows end ",,"
    (tmp_path / "k.csv").write_bytes(body)
    man = check_same_as_legacy(unit("k.csv"), tmp_path, tmp_path)
    assert man["overflow_rows"] == 0


def test_non_empty_cells_beyond_the_header_are_kept_in_extra(tmp_path):
    data = rows(3)
    data[1] = data[1] + ["", "a surprise"]
    (tmp_path / "l.csv").write_bytes(csv_bytes(HDR, data))
    man = extract.extract_unit(unit("l.csv"), tmp_path, tmp_path / "chunks", tmp_path / "spill")
    assert man["status"] == "ok" and man["overflow_rows"] == 1 and man["n_rows"] == 3
    pq = extract.chunk_dir(tmp_path / "chunks", unit("l.csv")) / "rows.parquet"
    extra = duckdb.sql(f"SELECT row_no, extra FROM read_parquet('{pq}') WHERE extra IS NOT NULL").fetchall()
    assert [(r, json.loads(e)) for r, e in extra] == [(2, {"overflow": ["", "a surprise"]})]
    (n, d), _ = legacy(unit("l.csv"), tmp_path, tmp_path / "stage")   # the old loader dropped that cell silently
    assert n == man["n_rows"] and d != man["row_digest"] and d == man["row_digest_legacy"]
    assert man["overflow_key"] == "overflow"


def test_overflow_key_never_overwrites_a_released_column(tmp_path):
    data = [r + ["kept"] for r in rows(3)]
    data[0] = data[0] + ["beyond"]
    (tmp_path / "r.csv").write_bytes(csv_bytes(HDR + ["overflow"], data))
    man = extract.extract_unit(unit("r.csv"), tmp_path, tmp_path / "chunks", tmp_path / "spill")
    assert man["status"] == "ok" and man["overflow_key"] == "overflow_" and man["overflow_rows"] == 1
    pq = extract.chunk_dir(tmp_path / "chunks", unit("r.csv")) / "rows.parquet"
    got = [json.loads(e) for (e,) in duckdb.sql(f"SELECT extra FROM read_parquet('{pq}') ORDER BY row_no").fetchall()]
    assert got == [{"overflow": "kept", "overflow_": ["beyond"]}, {"overflow": "kept"}, {"overflow": "kept"}]
    (n, d), _ = legacy(unit("r.csv"), tmp_path, tmp_path / "stage")
    assert (n, d) == (man["n_rows"], man["row_digest_legacy"])


def test_labels_that_differ_only_in_case_stay_separate_columns(tmp_path):
    assert mi.canonical(["Reason", "reason", "REASON"]) == ["Reason", "reason_2", "REASON_3"]
    assert mi.canonical(["A", "A_2", "A"]) == ["A", "A_2", "A_3"]           # a suffix never lands on a released label
    assert mi.canonical(["a", "", "column01"]) == ["a", "column01", "column01_2"]
    assert mi.canonical(["reason", "Reason"]) == ["reason_2", "Reason"]   # the Flock column's exact label keeps its name
    (tmp_path / "s.csv").write_bytes(csv_bytes(HDR + ["reason", "__ROW_NO"], [r + [f"low {i}", "x"] for i, r in enumerate(rows(2))]))
    man = check_same_as_legacy(unit("s.csv"), tmp_path, tmp_path)
    assert man["header"][-2:] == ["reason_2", "__ROW_NO"]
    pq = extract.chunk_dir(tmp_path / "chunks", unit("s.csv")) / "rows.parquet"
    got = duckdb.sql(f"SELECT row_no, \"Reason\", extra FROM read_parquet('{pq}') ORDER BY row_no").fetchall()
    assert [(r, reason, json.loads(e)) for r, reason, e in got] == [
        (1, "reason 0, with comma", {"reason_2": "low 0", "__ROW_NO": "x"}),
        (2, "reason 1, with comma", {"reason_2": "low 1", "__ROW_NO": "x"})]


def test_authored_audit_labels_never_cross_the_event_line():
    net, ev = unit("t.csv"), unit("u.csv", kind="event_log")
    assert mi.audit_of(net, {"audit": "own"}, {}) == "own"
    assert mi.audit_of(ev, {"audit": "network"}, {}) == "event"     # a request-level label leaves event logs alone
    assert mi.audit_of(net, {"audit": "own"}, {"audit": "network"}) == "network"
    for u, ov, fov in ((ev, {}, {"audit": "network"}), (net, {}, {"audit": "event"}), (net, {"audit": "event"}, {}),
                       (ev, {"audit": "event"}, {})):                 # a request label is network or own, whatever the file
        with pytest.raises(ValueError, match="event"):
            mi.audit_of(u, ov, fov)


def test_headers_that_collide_with_internal_column_names(tmp_path):
    (tmp_path / "m.csv").write_bytes(csv_bytes(HDR + ["row_no"], [r + ["7"] for r in rows(2)]))
    assert check_same_as_legacy(unit("m.csv"), tmp_path, tmp_path)["header"][-1] == "row_no"
    (tmp_path / "n.csv").write_bytes(csv_bytes(HDR + ["__src_row"], [r + ["7"] for r in rows(2)]))
    man = extract.extract_unit(unit("n.csv"), tmp_path, tmp_path / "chunks", tmp_path / "spill")   # the old loader failed this file
    assert man["status"] == "ok" and man["n_rows"] == 2 and man["header"][-1] == "__src_row"


def test_sheet_whose_used_range_starts_below_row_1(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    for j, h in enumerate(HDR):
        ws.cell(row=5, column=3 + j, value=h)                      # header at C5
    for i, r in enumerate(rows(3)):
        for j, v in enumerate(r):
            ws.cell(row=6 + i, column=3 + j, value=v)
    wb.save(tmp_path / "o.xlsx")
    man = check_same_as_legacy(unit("o.xlsx", sheet=ws.title), tmp_path, tmp_path)
    pq = extract.chunk_dir(tmp_path / "chunks", unit("o.xlsx", sheet=ws.title)) / "rows.parquet"
    assert [r for (r,) in duckdb.sql(f"SELECT src_row FROM read_parquet('{pq}') ORDER BY row_no").fetchall()] == [6, 7, 8]


def test_missing_container_is_a_failed_chunk_and_leaves_no_temp_dir(tmp_path):
    man = extract.extract_unit(unit("absent.csv"), tmp_path, tmp_path / "chunks", tmp_path / "spill")
    assert man["status"] == "failed" and "No such file" in man["error"]
    assert [p.name for p in extract.chunk_dir(tmp_path / "chunks", unit("absent.csv")).parent.iterdir()] == [extract.unit_id(unit("absent.csv"))]


def test_sweep_tmp_removes_dirs_of_dead_workers_only(tmp_path):
    code = "import sys; sys.path.insert(0, sys.argv[1]); import paths; print(paths.owner(sys.argv[2]))"
    dead_owner = subprocess.run([sys.executable, "-c", code, str(Path(paths.__file__).parent), str(tmp_path)],
                                capture_output=True, text=True, check=True).stdout.strip()   # took a lock, then exited
    req = tmp_path / "900001"
    dead = req / f".tmp-{'a' * 16}-{dead_owner}-0123abcd"
    live = req / f".old-{'b' * 16}-{paths.owner(tmp_path)}-89abcdef"
    for d in (dead, live):
        d.mkdir(parents=True)
    assert extract.sweep_tmp(tmp_path) == [dead] and live.exists()


def test_failed_chunks_are_never_reused(tmp_path):
    (tmp_path / "q.csv").write_bytes(b"no,header\n")
    u, code = unit("q.csv"), extract.code_identity()
    man = extract.extract_unit(u, tmp_path, tmp_path / "chunks", tmp_path / "spill", code=code)
    assert man["status"] == "failed"
    assert build_chunks.reusable(u, tmp_path / "chunks", False, man["container_sha256"], code) is None
    (tmp_path / "q.csv").write_bytes(csv_bytes(HDR, rows(2)))
    ok = extract.extract_unit(u, tmp_path, tmp_path / "chunks", tmp_path / "spill", code=code)
    assert build_chunks.reusable(u, tmp_path / "chunks", False, ok["container_sha256"], code)["status"] == "ok"


def test_own_search_org_counts_for_the_producer_inference(tmp_path):
    data = rows(3) + [r[:2] + ["Other CA SO"] + r[3:] for r in rows(1, 3)]
    (tmp_path / "i.csv").write_bytes(csv_bytes(HDR, data))
    man = extract.extract_unit(unit("i.csv", kind="search_audit_own"), tmp_path, tmp_path / "chunks", tmp_path / "spill")
    assert man["own_org_counts"] == [["Example CA PD", 3], ["Other CA SO", 1]] or \
        man["own_org_counts"] == [("Example CA PD", 3), ("Other CA SO", 1)]


def test_deterministic_and_replaced_atomically(tmp_path):
    (tmp_path / "j.csv").write_bytes(csv_bytes(HDR, rows(50)))
    u = unit("j.csv")
    a = extract.extract_unit(u, tmp_path, tmp_path / "chunks", tmp_path / "spill")
    b = extract.extract_unit(u, tmp_path, tmp_path / "chunks", tmp_path / "spill")
    assert (a["row_digest"], a["parquet_sha256"]) == (b["row_digest"], b["parquet_sha256"])
    siblings = [p.name for p in extract.chunk_dir(tmp_path / "chunks", u).parent.iterdir()]
    assert siblings == [extract.unit_id(u)], siblings                 # no .tmp- or .old- directories left behind
    man = json.loads((extract.chunk_dir(tmp_path / "chunks", u) / "chunk.json").read_text())
    assert man["unit_sha256"] == extract.unit_sha256(u) and man["versions"]["duckdb"]


class FakePanic(BaseException):
    """What pyo3 raises for a Rust panic in a reader: a BaseException, not an Exception."""


def test_a_reader_panic_is_a_failed_chunk_and_an_interrupt_is_not(tmp_path, monkeypatch):
    (tmp_path / "v.csv").write_bytes(csv_bytes(HDR, rows(2)))
    u = unit("v.csv")
    monkeypatch.setattr(extract, "_extract", lambda *a: (_ for _ in ()).throw(FakePanic("index out of bounds")))
    man = extract.extract_unit(u, tmp_path, tmp_path / "chunks", tmp_path / "spill")
    assert man["status"] == "failed" and man["error"] == "FakePanic: index out of bounds" and man["container_sha256"]
    monkeypatch.setattr(extract, "_extract", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        extract.extract_unit(u, tmp_path, tmp_path / "chunks", tmp_path / "spill")
    assert [p.name for p in extract.chunk_dir(tmp_path / "chunks", u).parent.iterdir()] == [extract.unit_id(u)]
    assert json.loads((extract.chunk_dir(tmp_path / "chunks", u) / "chunk.json").read_text())["status"] == "failed"


def evidence(ev, names):
    """A minimal evidence dir: a catalog naming one CSV network audit per name (the files themselves are the caller's)."""
    ev.mkdir(exist_ok=True)
    cat = [{"http": 200, "local_path": n, "request_id": 900001, "url": f"https://example.invalid/{n}", "file_name": n,
            "profile": [{"kind": "network_audit", "member": None, "sheet": "csv", "headers": HDR}]} for n in names]
    (ev / "catalog.json").write_text(json.dumps(cat))
    (ev / "catalog2.json").write_text("[]")
    (ev / "catalog_requests.json").write_text(json.dumps([{"request_id": 900001, "agency": "Test Agency",
                                                           "url": "https://example.invalid/req"}]))
    return ev


BUILD = [sys.executable, str(Path(build_chunks.__file__))]


def test_build_chunks_goes_on_past_a_missing_container_and_an_empty_selection_fails(tmp_path):
    ev = evidence(tmp_path / "ev", ["ok.csv", "gone.csv"])
    (ev / "ok.csv").write_bytes(csv_bytes(HDR, rows(3)))
    out, audit = tmp_path / "chunks", tmp_path / "audit"
    r = subprocess.run(BUILD + [str(out), "--evidence", str(ev), "--audit-dir", str(audit), "--workers", "1"],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 1, r.stdout + r.stderr
    status = {m["unit"]["local_path"]: m["status"] for m in (json.loads(p.read_text()) for p in out.glob("*/*/chunk.json"))}
    assert status == {"ok.csv": "ok", "gone.csv": "failed"}
    r = subprocess.run(BUILD + [str(out), "--evidence", str(ev), "--audit-dir", str(audit), "--only", "mr:1:%"],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 2 and "no units selected" in r.stderr


@pytest.mark.parametrize("workers", [1, 2])
def test_a_worker_that_dies_takes_only_its_own_unit_down(tmp_path, workers):
    r, mans = forked_run(tmp_path, "os._exit(9)", ["a.csv", "b.csv", "bad.csv", "c.csv", "d.csv", "e.csv"], workers)
    assert r.returncode == 1, r.stdout + r.stderr
    assert {k: m["status"] for k, m in mans.items()} == {**{f"{c}.csv": "ok" for c in "abcde"}, "bad.csv": "failed"}
    assert "worker process died" in mans["bad.csv"]["error"]


def mini_truth(audit, ev):
    """A truth.duckdb holding ev's units loaded the old way (stage_all + add_muckrock), with build_truth's tables."""
    import build_truth
    audit.mkdir()
    con = paths.duck_connect(audit / "truth.duckdb", spill_root=audit / "spill")
    con.execute(build_truth.RELEASES_DDL)
    con.execute("CREATE TABLE flock_audit_rows (release_id VARCHAR, row_no BIGINT, src_row BIGINT, "
                f"{', '.join(f'{mi.sql_ident(c)} VARCHAR' for c in mi.FLOCK_COLS)}, extra JSON)")
    mi.stage_all(ev, audit / "tmp", workers=1)
    mi.add_muckrock(con, ev, audit / "tmp", log=lambda m: None)
    con.execute("CREATE TABLE build_info (key VARCHAR, value VARCHAR)")
    con.execute("INSERT INTO build_info VALUES ('evidence_dir', ?)", [str(ev)])
    con.close()


def test_verify_passes_overflow_cells_and_names_a_digest_only_difference(tmp_path):
    ev = evidence(tmp_path / "ev", ["w.csv", "x.csv", "v.csv"])
    (ev / "v.csv").write_bytes(b"no,header\n")               # a failed chunk (and not in truth either)
    data = rows(3)
    data[1] = data[1] + ["beyond the header"]               # truth's loader dropped this cell
    (ev / "w.csv").write_bytes(csv_bytes(HDR, data))
    (ev / "x.csv").write_bytes(csv_bytes(HDR, rows(2)))
    audit = tmp_path / "audit"
    mini_truth(audit, ev)
    con = duckdb.connect(str(audit / "truth.duckdb"))       # one cell of x.csv differs; its row count does not
    assert con.execute("UPDATE flock_audit_rows SET \"Reason\" = 'changed' "
                       "WHERE release_id = 'mr:900001:x.csv#csv' AND row_no = 1").fetchone() == (1,)
    con.close()
    r = subprocess.run(BUILD + [str(tmp_path / "chunks"), "--audit-dir", str(audit), "--verify", "--workers", "1"],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 1, r.stdout + r.stderr
    report = {v["release_id"]: v for v in map(json.loads, next((tmp_path / "chunks").glob("verify-*.jsonl")).open())}
    assert report["mr:900001:w.csv#csv"]["match"] and report["mr:900001:w.csv#csv"]["digest"] == "row_digest_legacy"
    assert report["mr:900001:x.csv#csv"]["diffs"] == ["row digest (same 2 rows; some row's values differ)"]
    assert "extracted 3, failed 1, match 1, mismatch 1;" in r.stdout      # a failed unit counts once, as failed


def test_a_case_variant_before_the_flock_label_does_not_take_its_column(tmp_path):
    hdr = HDR[:5] + ["reason"] + HDR[5:]                  # 'reason' (a note column, say) ahead of the real 'Reason'
    data = [r[:5] + [f"note {i}"] + r[5:] for i, r in enumerate(rows(2))]
    (tmp_path / "t.csv").write_bytes(csv_bytes(hdr, data))
    man = check_same_as_legacy(unit("t.csv"), tmp_path, tmp_path)
    pq = extract.chunk_dir(tmp_path / "chunks", unit("t.csv")) / "rows.parquet"
    got = duckdb.sql(f"SELECT \"Reason\", extra FROM read_parquet('{pq}') ORDER BY row_no").fetchall()
    assert [(r, json.loads(e)) for r, e in got] == [("reason 0, with comma", {"reason_2": "note 0"}),
                                                    ("reason 1, with comma", {"reason_2": "note 1"})]
    assert man["header"][5] == "reason_2"


def test_a_truncated_rows_parquet_is_not_reused(tmp_path):
    (tmp_path / "y.csv").write_bytes(csv_bytes(HDR, rows(50)))
    u, code = unit("y.csv"), extract.code_identity()
    man = extract.extract_unit(u, tmp_path, tmp_path / "chunks", tmp_path / "spill", code=code)
    assert build_chunks.reusable(u, tmp_path / "chunks", False, man["container_sha256"], code)
    pq = extract.chunk_dir(tmp_path / "chunks", u) / "rows.parquet"
    pq.write_bytes(pq.read_bytes()[:-100])
    assert build_chunks.reusable(u, tmp_path / "chunks", False, man["container_sha256"], code) is None


def test_a_failure_never_replaces_a_good_chunk(tmp_path):
    (tmp_path / "z.csv").write_bytes(csv_bytes(HDR, rows(3)))
    u = unit("z.csv")
    good = extract.extract_unit(u, tmp_path, tmp_path / "chunks", tmp_path / "spill")
    (tmp_path / "z.csv").unlink()                       # a partial sync, say
    man = extract.extract_unit(u, tmp_path, tmp_path / "chunks", tmp_path / "spill")
    assert man["status"] == "failed" and man["kept"]["extracted_at"] == good["extracted_at"]
    d = extract.chunk_dir(tmp_path / "chunks", u)
    assert json.loads((d / "chunk.json").read_text()) == good and (d / "rows.parquet").exists()
    assert [p.name for p in d.parent.iterdir()] == [d.name]   # no temp dir left


FORKED = ("import multiprocessing as mp, os, sys; mp.set_start_method('fork'); sys.path.insert(0, sys.argv[1])\n"
          "import build_chunks, extract\n"
          "real = extract.extract_unit\n"
          "def patched(u, *a, **k):\n"
          "    if u['local_path'] == 'bad.csv':\n"
          "        {action}\n"
          "    return real(u, *a, **k)\n"
          "extract.extract_unit = patched\n"
          "sys.argv = ['build_chunks.py'] + sys.argv[2:]\n"
          "build_chunks.main()")


def forked_run(tmp_path, action, names, workers=2):
    """build_chunks over names (CSV audits), in fork mode so the workers inherit an extract_unit that does `action` on
    bad.csv. Returns (completed process, {local_path: chunk.json})."""
    ev = evidence(tmp_path / "ev", names)
    for n in names:
        (ev / n).write_bytes(csv_bytes(HDR, rows(3)))
    out = tmp_path / "chunks"
    r = subprocess.run([sys.executable, "-c", FORKED.format(action=action), str(Path(extract.__file__).parent), str(out),
                        "--evidence", str(ev), "--audit-dir", str(tmp_path / "audit"), "--workers", str(workers)],
                       capture_output=True, text=True, timeout=300)
    return r, {m["unit"]["local_path"]: m for m in (json.loads(p.read_text()) for p in out.glob("*/*/chunk.json"))}


def test_a_chunk_that_cannot_be_written_fails_only_its_unit(tmp_path):
    r, mans = forked_run(tmp_path, "raise OSError(28, 'No space left on device')", ["a.csv", "bad.csv", "b.csv", "c.csv"])
    assert r.returncode == 1, r.stdout + r.stderr
    assert {k: m["status"] for k, m in mans.items()} == {"a.csv": "ok", "b.csv": "ok", "c.csv": "ok"}
    assert "chunk not written: OSError: [Errno 28] No space left on device" in r.stdout
    assert "extracted 4, failed 1" in r.stdout                  # the summary still comes


def test_one_run_at_a_time_per_out_dir(tmp_path):
    import fcntl
    ev = evidence(tmp_path / "ev", ["a.csv"])
    (ev / "a.csv").write_bytes(csv_bytes(HDR, rows(1)))
    out = tmp_path / "chunks"
    out.mkdir()
    fd = os.open(out / ".build_chunks.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = subprocess.run(BUILD + [str(out), "--evidence", str(ev), "--audit-dir", str(tmp_path / "audit")],
                           capture_output=True, text=True, timeout=300)
    finally:
        os.close(fd)
    assert r.returncode == 2 and "another build_chunks run" in r.stderr and not list(out.glob("*/*/chunk.json"))


def test_evidence_edited_in_place_is_reported_and_its_chunk_kept(tmp_path):
    ev = evidence(tmp_path / "ev", ["a.csv", "b.csv"])
    for n in ("a.csv", "b.csv"):
        (ev / n).write_bytes(csv_bytes(HDR, rows(3)))
    out = tmp_path / "chunks"

    def run(*extra):
        return subprocess.run(BUILD + [str(out), "--evidence", str(ev), "--audit-dir", str(tmp_path / "audit"),
                                       "--workers", "1", *extra], capture_output=True, text=True, timeout=300)
    assert run().returncode == 0
    d = extract.chunk_dir(out, unit("a.csv"))
    before = (d / "chunk.json").read_text()
    (ev / "a.csv").write_bytes(csv_bytes(HDR, rows(3, start=10)))   # the same path, new bytes
    for extra in ((), ("--force",)):
        r = run(*extra)
        assert r.returncode == 1 and "evidence changed in place: a.csv" in r.stdout, r.stdout + r.stderr
        assert (d / "chunk.json").read_text() == before and (d / "rows.parquet").exists()   # the old version stays
    assert json.loads((extract.chunk_dir(out, unit("b.csv")) / "chunk.json").read_text())["status"] == "ok"


def test_the_stamped_manifest_binds_each_path(tmp_path):
    ev = evidence(tmp_path / "ev", ["a.csv", "b.csv", "c.csv"])
    for n in ("a.csv", "b.csv", "c.csv"):
        (ev / n).write_bytes(csv_bytes(HDR, rows(2)))
    (ev / "MANIFEST_v2.txt").write_text("# sha256  local_path  source_url\n"
                                        f"{'0' * 64}  a.csv  https://example.invalid/a.csv\n"
                                        f"{mi.sha256_file(ev / 'b.csv')}  b.csv  https://example.invalid/b.csv\n")
    out = tmp_path / "chunks"
    r = subprocess.run(BUILD + [str(out), "--evidence", str(ev), "--audit-dir", str(tmp_path / "audit")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 1 and "evidence changed in place: a.csv" in r.stdout and "MANIFEST_v2.txt binds" in r.stdout
    assert "1 containers are not in MANIFEST_v2.txt" in r.stdout      # c.csv: extracted, noted as not stamped yet
    status = {m["unit"]["local_path"]: m["status"] for m in (json.loads(p.read_text()) for p in out.glob("*/*/chunk.json"))}
    assert status == {"b.csv": "ok", "c.csv": "ok"}                    # nothing extracted for a.csv
