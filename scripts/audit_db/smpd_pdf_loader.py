"""SMPD's own search log, read from the PDFs it produced (PRA W012541, W012818): Flock search-audit exports printed to PDF.

One row per search-id block in a PDF's text layer: pymupdf page.get_text(), the text scripts/parse_pra_audit.py reads.
A block is the id line plus the lines up to the next id, normally
    userID  |  '<networkCount> MM/DD/YYYY, HH:MM:SS AM|PM UTC'  |  Reason (no line when the Reason cell is blank)
Lines are stored verbatim (reason lines keep their trailing spaces). No id block is ever dropped: parse_note says when a
block is not that shape and how its cells were read.

Why position matters: on some pages the text layer lists cells out of printed order (reason lines pile up after the
last row of the page). A text-order reader then gives a row no reason and another row several, or swallows the next
row. Each line's position on the page still matches the printed row, so every block is checked against the lines
printed in its id's row band; where text order and the printed row disagree, the printed row wins and parse_note
names the page line of each cell.

  uv run --locked --project scripts/audit_db python scripts/audit_db/smpd_pdf_loader.py PDF...   # per-PDF summary, by hand
"""
import re
import sys
from pathlib import Path

UUID_LINE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
COUNT_TIME = re.compile(r"\d+\s+\d{2}/\d{2}/\d{4},\s+\d{2}:\d{2}:\d{2}\s+(?:AM|PM)\s+UTC")
# the five columns of Flock's search-audit export, named as the other loaders name them (Flock superset); the printed
# labels (ID, userID, networkCount, Search Time, Reason) are kept verbatim in releases.header_raw
HEADER = ["ID", "Name", "Total Networks Searched", "Search Time", "Reason"]
PRINTED = ["ID", "userID", "networkCount", "Search Time", "Reason"]
SRC_ROW_BASIS = "PDF page src_page, line src_line of that page; the search id is printed there"
IMAGE_ONLY_BASIS = "image-only PDF: no text layer; OCR rows not loaded yet"


def audit_pdfs(repo):
    """Every search-audit PDF of the two SMPD requests (message-history PDFs excluded), in a stable order."""
    base = Path(repo) / "assets/san-mateo-public-records"
    return sorted(p for w in ("W012541-*", "W012818-*") for p in base.glob(f"{w}/*.pdf") if "audit" in p.name.lower())


def _page_lines(page):
    """Non-empty lines of page.get_text(), verbatim, each with its bbox from the same extraction in dict form.

    The two forms list the same lines in the same order (checked on every committed PDF); if a page ever differs, its
    lines come back without positions and its blocks are read in text order only."""
    import pymupdf
    tp = page.get_textpage(flags=pymupdf.TEXTFLAGS_TEXT)   # one extraction for both forms; no image data
    text = [s for s in (page.get_text(textpage=tp) or "").splitlines() if s.strip()]
    boxed = [("".join(sp["text"] for sp in ln["spans"]), tuple(ln["bbox"]))
             for bl in page.get_text("dict", textpage=tp)["blocks"] for ln in bl.get("lines", [])]
    boxed = [(t, b) for t, b in boxed if t.strip()]
    return boxed if [t for t, _ in boxed] == text else [(t, None) for t in text]


def _shape_ok(lines):
    """userID, count/time, and at most one reason line."""
    return len(lines) in (2, 3) and bool(COUNT_TIME.fullmatch(lines[1].strip()))


def parse_pdf(path):
    """-> {n_pages, header_raw (lines printed before the first id, None if none), rows: [dict], stats}.

    rows: row_no (block order in the text layer, 1-based), src_page (1-based), src_line (1-based among the page's
    non-empty lines), id, user_line, count_time_line, reason_line, parse_note."""
    import pymupdf
    with pymupdf.open(path) as doc:
        pages = [_page_lines(p) for p in doc]
    flat = [(pno, k, t, bb) for pno, lines in enumerate(pages, 1) for k, (t, bb) in enumerate(lines, 1)]
    ids = [i for i, (_, _, t, _) in enumerate(flat) if UUID_LINE.fullmatch(t.strip())]
    idset = set(ids)
    # positional reading, per page: a non-id line belongs to the id whose printed row (vertical band) holds its centre
    row_of, orphans = {}, set()
    by_page = {}
    for i, (pno, _, _, _) in enumerate(flat):
        by_page.setdefault(pno, []).append(i)
    no_pos = {pno for pno, lines in enumerate(pages, 1) if any(bb is None for _, bb in lines)}
    for pno, idx in by_page.items():
        pids = [i for i in idx if i in idset]
        if not pids or pno in no_pos:
            continue
        for j in idx:
            if j in idset:
                continue
            y0, y1 = flat[j][3][1], flat[j][3][3]
            cy = (y0 + y1) / 2
            cand = [i for i in pids if flat[i][3][1] <= cy <= flat[i][3][3]]
            if cand:
                best = min(cand, key=lambda i: abs(cy - (flat[i][3][1] + flat[i][3][3]) / 2))
                row_of.setdefault(best, []).append(j)
            elif j > pids[0]:   # lines above the first id of a page are the printed header
                orphans.add(j)
    orphan_pages = {flat[j][0] for j in orphans}
    txt = lambda j: flat[j][2]
    rows, n_pos, n_note = [], 0, 0
    for n, (a, b) in enumerate(zip(ids, ids[1:] + [len(flat)]), 1):
        pno, k = flat[a][0], flat[a][1]
        seq = list(range(a + 1, b))
        pos = sorted(row_of.get(a, []), key=lambda j: flat[j][3][0])   # left to right across the printed row
        seq_ok, pos_ok = _shape_ok([txt(j) for j in seq]), _shape_ok([txt(j) for j in pos])
        use, note = seq, None
        if pno in no_pos or pno in orphan_pages:   # no trustworthy positions: text order only, and say so
            why = ("its text and layout disagree" if pno in no_pos else "it has text outside every printed row")
            note = (f"read in text order only (position not used: page {pno} {why})"
                    + ("" if seq_ok else f"; unexpected block: {len(seq)} line(s) between this id and the next"))
        elif pos_ok and pos != seq:
            use = pos
            cells = ", ".join(f"{lab} line {flat[j][1]}" for lab, j in zip(("userID", "count/time", "reason"), pos))
            note = (f"text order differs from the printed row: cells read by position on page {pno} ({cells}"
                    + ("; Reason cell blank" if len(pos) == 2 else "") + ")")
            n_pos += 1
        elif not pos_ok:   # the printed row itself is not the expected shape: keep text order
            note = (f"printed row holds {len(pos)} line(s) besides the id; read in text order"
                    + ("" if seq_ok else f" ({len(seq)} line(s) between this id and the next)"))
        if any(flat[j][0] != pno for j in use):
            note = (note + "; " if note else "") + f"block continues on page {flat[use[-1]][0]} in text order"
        n_note += note is not None
        rows.append({"row_no": n, "src_page": pno, "src_line": k, "id": txt(a),
                     "user_line": txt(use[0]) if len(use) > 0 else None,
                     "count_time_line": txt(use[1]) if len(use) > 1 else None,
                     "reason_line": "\n".join(txt(j) for j in use[2:]) if len(use) > 2 else None,
                     "parse_note": note})
    header_raw = [t for _, _, t, _ in flat[:ids[0]]] if ids else None
    return {"n_pages": len(pages), "header_raw": header_raw or None, "rows": rows,
            "stats": {"blocks": len(rows), "distinct_ids": len({r["id"].strip() for r in rows}), "read_by_position": n_pos,
                      "with_note": n_note, "orphan_lines": len(orphans), "pages_without_positions": len(no_pos)}}


if __name__ == "__main__":
    for p in sys.argv[1:]:
        r = parse_pdf(p)
        print(Path(p).name, r["n_pages"], "pages", r["stats"], "header:", r["header_raw"])
        for row in r["rows"]:
            if row["parse_note"]:
                print("   ", row["src_page"], row["src_line"], row["id"], "|", row["parse_note"])
