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
    con = paths.duck_connect(spill_parent=tmp / "spill")
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
    assert n == man["n_rows"] and d != man["row_digest"]


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
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    req = tmp_path / "900001"
    dead, live = req / f".tmp-{'a' * 16}-{p.pid}-0123abcd", req / f".old-{'b' * 16}-{os.getpid()}-89abcdef"
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
