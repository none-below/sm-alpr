# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""The parser-free second row count: the helper that compares the two counts, the NDJSON line counter, the per-page
UUID count of an invented PDF, and the PDF-table comparison with a page that disagrees."""
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_truth  # noqa: E402
import pdf_table_ingest as pti  # noqa: E402
import smpd_pdf_loader as smpd  # noqa: E402
from muckrock_ingest import second_count, staged_second  # noqa: E402

UUIDS = ["0f3b2a1c-1111-4aaa-8bbb-000000000001", "0f3b2a1c-1111-4aaa-8bbb-000000000002", "0f3b2a1c-1111-4aaa-8bbb-000000000003",
         "0f3b2a1c-1111-4aaa-8bbb-000000000004", "0f3b2a1c-1111-4aaa-8bbb-000000000005"]


def test_second_count_helper():
    assert second_count(10, 10, "b") == (10, "b", None)
    assert second_count(10, None, "none") == (None, "none", None)
    assert second_count(10, 12, "b") == (12, "b", "totals differ: loaded 10, counted 12")
    assert second_count(10, 12, "b", where="page 3") == (12, "b", "page 3")
    assert staged_second(4, {"n_data_rows": 4})[2] is None
    assert staged_second(4, {})[0] is None and "staged before" in staged_second(4, {})[1]


def test_ndjson_lines(tmp_path):
    p = tmp_path / "x.ndjson.gz"
    with gzip.open(p, "wt") as fh:
        fh.write('{"a": 1}\n\n{"a": 2}\n   \n{"a": 3}\n')
    assert build_truth.ndjson_lines(p) == 3


def _pdf(path, pages):
    import pymupdf
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for ln in lines:
            page.insert_text((72, y), ln, fontsize=9)
            y += 14
    doc.save(path)
    doc.close()


def test_smpd_uuid_per_page(tmp_path):
    p = tmp_path / "x.pdf"
    _pdf(p, [["userID networks search time reason", UUIDS[0], "u1", "3 01/02/2026, 01:00:00 AM UTC", "reason one",
              UUIDS[1], "u2", "3 01/02/2026, 01:00:01 AM UTC", "reason two"],
             [UUIDS[2], "u3", "3 01/03/2026, 01:00:00 AM UTC", "reason three"]])
    res = smpd.parse_pdf(p)
    assert res["uuid_per_page"] == {1: 2, 2: 1}
    s = build_truth.smpd_second(res)
    assert s["n_rows_second"] == 3 and s["count_mismatch_at"] is None
    res["uuid_per_page"] = {1: 3, 2: 1}       # the text layer holding an id the block parser did not make a row for
    s = build_truth.smpd_second(res)
    assert s["n_rows_second"] == 4 and s["count_mismatch_at"].startswith("page 1: 3 ids in the text, 2 rows parsed")


def test_pdf_table_second_and_uuid_per_page(tmp_path):
    p = tmp_path / "t.pdf"
    _pdf(p, [[UUIDS[0], UUIDS[1]], [UUIDS[2]]])
    assert pti.uuid_per_page(p) == {1: 2, 2: 1}
    rows = [{"_page": 1, "_row": 1}, {"_page": 1, "_row": 2}, {"_page": 2, "_row": 1}]
    assert pti.pdf_second(3, {"uuid_per_page": {"1": 2, "2": 1}}, rows)[2] is None
    n2, basis, where = pti.pdf_second(3, {"uuid_per_page": {"1": 2, "2": 2}}, rows)
    assert n2 == 4 and where == "page 2: 2 ids in the text, 1 rows read"
    assert pti.pdf_second(3, {"uuid_per_page": None}, rows)[0] is None
    assert "predates" in pti.pdf_second(3, {}, rows)[1]
