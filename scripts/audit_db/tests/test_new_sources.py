"""Loaders added for the portal harvest and audit spreadsheets printed to PDF: table profiling, the PDF position reader
and its layout gate, and portal producer attribution / exclusions."""
import collections
import io
import sys
import zipfile
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pdf_table_reader as ptr  # noqa: E402
import portal_ingest as pi  # noqa: E402
from table_profile import profile_bytes  # noqa: E402

HDR = "ID,Name,Org Name,Total Networks Searched,Time Frame,Reason,Case #,Search Time\n"
ROW = "{id},A. Bcd,{org},300,\"03/01/2026, 10:00:00 AM UTC\n03/02/2026, 10:00:00 AM UTC\",theft,,\"03/02/2026, 10:00:05 AM UTC\"\n"
U1, U2 = "11111111-2222-3333-4444-555555555555", "66666666-7777-8888-9999-000000000000"


def csv_bytes(*orgs):
    return (HDR + "".join(ROW.format(id=U1 if i % 2 else U2, org=o) for i, o in enumerate(orgs))).encode()


def test_profile_network_own_event_and_zip_member():
    assert profile_bytes(csv_bytes("Hayward CA PD", "Pacifica CA PD"), "x.csv")[0]["kind"] == "network_audit"
    own = profile_bytes(csv_bytes("Pacifica CA PD", "Pacifica CA PD"), "Organization_Audit.csv")[0]
    assert own["kind"] == "search_audit_own" and own["top_org"] == "Pacifica CA PD"
    # a single org in a file named as a network audit is still a network audit
    assert profile_bytes(csv_bytes("Pacifica CA PD"), "Fairfield-Network-Audit.csv")[0]["kind"] == "network_audit"
    ev = b"Timestamp,User,Event Type,Entity Type,Entity Details,Event Id\n2026-01-01,a,create,networkShare,x,1\n"
    assert profile_bytes(ev, "events.csv")[0]["kind"] == "event_log"
    assert profile_bytes(b"Name,Address,Phone\nA,B,C\n", "contacts.csv") == []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("June/network.csv", csv_bytes("Hayward CA PD", "Pacifica CA PD"))
        z.writestr("letter.pdf", b"%PDF-1.4")
    (p,) = profile_bytes(buf.getvalue(), "release.zip")
    assert p["member"] == "June/network.csv" and p["kind"] == "network_audit"


def make_pdf(path, fill_reason=True, overflow=False):
    """One page laid out like an Excel export: bordered columns, bottom-aligned cells, a 2-line Time Frame."""
    doc = pymupdf.open()
    page = doc.new_page(width=792, height=612)
    cols = [("Org Name", 50, 150), ("Time Frame", 150, 300), ("Reason", 300, 380), ("Filters", 380, 430),
            ("Search Time", 430, 560)]
    for _, a, b in cols:
        page.draw_line((a, 60), (a, 200))
    page.draw_line((560, 60), (560, 200))
    for name, a, _ in cols:
        page.insert_text((a + 3, 70), name, fontsize=7)
    rows = [("Hayward CA PD", "theft", U1), ("Pacifica CA PD", "25-012345 burglary", U2)]
    y = 90
    for org, reason, _ in rows:
        page.insert_text((153, y), "03/01/2026, 10:00:00 AM UTC", fontsize=6)
        page.insert_text((153, y + 8), "03/02/2026, 10:00:00 AM UTC", fontsize=6)
        page.insert_text((53, y + 8), org, fontsize=7)
        page.insert_text((303, y + 8), reason, fontsize=7)
        if overflow:   # Filters text written one character past its border, as Excel exports clip it (same font)
            filt = "make:Chevrolet_model:Malibu"
            while 383 + pymupdf.get_text_length(filt, fontsize=7) > 433:
                filt = filt[:-1]
            page.insert_text((383, y + 8), filt, fontsize=7)
        page.insert_text((433, y + 8), "03/02/2026, 10:00:05 AM UTC", fontsize=7 if overflow else 6)
        if fill_reason:
            page.draw_rect(pymupdf.Rect(300, y - 2, 380, y + 10), color=(0, 0, 0), fill=(0, 0, 0))
        y += 20
    doc.save(path)


def test_pdf_reader_rows_fill_and_gate(tmp_path):
    p = tmp_path / "audit.pdf"
    make_pdf(p)
    res = ptr.read_pdf(p)
    assert res["header"] == ["Org Name", "Time Frame", "Reason", "Filters", "Search Time"]
    assert [r["Org Name"] for r in res["rows"]] == ["Hayward CA PD", "Pacifica CA PD"]
    r0 = res["rows"][0]
    assert r0["Time Frame"] == "03/01/2026, 10:00:00 AM UTC\n03/02/2026, 10:00:00 AM UTC"
    assert r0["Search Time"] == "03/02/2026, 10:00:05 AM UTC"
    assert "Reason" in r0["_under_fill"] and (r0["_page"], r0["_row"]) == (1, 1)
    assert res["ok"], res["why"]


def test_pdf_reader_gives_overflow_back(tmp_path):
    p = tmp_path / "overflow.pdf"
    make_pdf(p, fill_reason=False, overflow=True)
    rows = ptr.read_pdf(p)["rows"]
    assert all(r["Search Time"] == "03/02/2026, 10:00:05 AM UTC" for r in rows)
    assert all(r["Filters"].startswith("make:Chevrolet") for r in rows)


def test_layout_gate_rejects_malformed():
    rows = [{"Search Time": "1/04/2024, 11:58:13", "Time Frame": "x", "ID": "625f78c6-2d35-"}] * 10
    ok, why = ptr.layout_ok(["ID", "Time Frame", "Search Time"], rows)
    assert not ok and "Search Time 10/10" in why


def test_portal_producer_precedence_and_exclusions():
    prod = {"hosts": {"cityofmanteca.nextrequest.com": {"org": "Manteca CA PD", "source": "s"}},
            "requests": {"x.nextrequest.com:26-1": {"org": "Napa CA PD", "source": "r"}}, "files": {}}
    u = {"host": "cityofmanteca.nextrequest.com", "request": "26-286", "local_path": "a/b/26-286/files/1__f.csv",
         "name": "67972158__Manteca_CA_PD_network_Apr_2024.csv"}
    assert pi.producer_for(u, prod, {}) == ("Manteca CA PD", "portal_producers.json (host)", "s")
    u2 = {**u, "name": "1__4_1_2026-4_30_2026-Santa Clara CA PD-Network-Audit.csv"}
    assert pi.producer_for(u2, prod, {})[:2] == ("Santa Clara CA PD", "filename")
    u3 = {**u, "host": "x.nextrequest.com", "request": "26-1"}
    assert pi.producer_for(u3, prod, {})[0] == "Napa CA PD"
    rules = [("pp:nextrequest:example.nextrequest.com:25-1:*__Example Network Audit.xlsx*", "r", "src")]
    assert pi.excluded("pp:nextrequest:example.nextrequest.com:25-1:53__Example Network Audit.xlsx#S", rules)
    assert not pi.excluded("pp:nextrequest:example.nextrequest.com:25-1:54__Example Network Audit_v2.xlsx#S", rules)
    assert pi.shown_name("a5cd4419-c12__Example County CA SO _Network_Audit_April 2025.pdf").startswith("Example County")
    assert pi.producer_for({**u, "name": "9__1_1_2026-1_31_2026-San_Diego_CA_PD-ORG-Audit.csv"}, prod, {})[0] == "San Diego CA PD"
    assert pi.producer_for({**u, "name": "9__1_1_2026-Roseville CA PD org-Audit.csv"}, prod, {})[0] == "Roseville CA PD"
    # an own-search Org Name the registry does not know gives way to the host's agency
    own = {"cityofmanteca.nextrequest.com:26-286": collections.Counter({"MPD": 3})}
    u4 = {**u, "name": "export.csv"}
    assert pi.producer_for(u4, prod, own, resolve={"Manteca CA PD": "id"}.get)[:2] == ("Manteca CA PD", "portal_producers.json (host)")
    assert pi.producer_for(u4, prod, own)[:2] == ("MPD", "own-search dominant Org Name")


def test_gate_on_first_pages_stops_early(tmp_path, monkeypatch):
    """A layout that fails on the first pages is not read further."""
    doc = pymupdf.open()
    for pno in range(3):
        page = doc.new_page(width=792, height=612)
        for name, x in (("Org Name", 50), ("Reason", 200), ("Search Time", 400)):
            page.insert_text((x, 70), name, fontsize=7)
        for k in range(3):
            page.insert_text((50, 90 + 20 * k), "Hayward CA PD", fontsize=7)
            page.insert_text((400, 90 + 20 * k), "03/02/2026, 10:00:05", fontsize=7)   # clipped: no AM/PM, no UTC
    p = tmp_path / "clipped.pdf"
    doc.save(p)
    monkeypatch.setattr(ptr, "SAMPLE_PAGES", 1)
    res = ptr.read_pdf(p)
    assert not res["ok"] and res["sampled"] and res["why"].startswith("first 1 pages")
    assert {r["_page"] for r in res["rows"]} == {1}
