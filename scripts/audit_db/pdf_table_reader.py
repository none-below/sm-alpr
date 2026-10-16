"""Flock audit spreadsheets that an agency printed to PDF, read from the text layer by position. Used by pdf_table_ingest.py to load them into truth.

Layout these PDFs share: a header row of Flock column labels on the first page; cells bottom-aligned, so a row's
single-line cells sit on its last text line and multi-line cells (Time Frame's start/end) extend upward; one Search
Time per row. Rows are anchored on the Search Time cell; every word between the previous row's anchor and this one
belongs to this row. Words are rebuilt from characters and split at column borders, and each character goes to the
column whose span holds its centre: a full cell's clipped overflow is in the text layer and would otherwise merge
into the next cell. Column spans come from the vertical border lines the page draws, else from the header labels.

Each row also lists the columns whose text lies under a solid black filled rectangle drawn on the page (_under_fill):
the text layer holds text whether or not something is drawn over it, and readers may need to know which cells those are.

layout_ok() is the gate a PDF must pass before its rows are used: a layout this reader does not handle yet (Fullerton
PD's portrait, border-less exports, where columns are clipped and overlap) comes out malformed, and is left out rather
than guessed at. read_pdf() runs it on the first pages before reading the rest.
"""
import re

import pymupdf

pymupdf.TOOLS.mupdf_display_errors(False)   # damaged PDFs print one error per page read; the results say what failed

LABELS = {  # printed label (lowercase, single-spaced) -> Flock column name
    "id": "ID", "name": "Name", "org name": "Org Name", "total networks searched": "Total Networks Searched",
    "total devices searched": "Total Devices Searched", "time frame": "Time Frame", "license plate": "License Plate",
    "reason": "Reason", "case #": "Case #", "case number": "Case #", "filters": "Filters", "search time": "Search Time",
    "search type": "Search Type", "text prompt": "Text Prompt", "moderation": "Moderation",
}
DATE = re.compile(r"\d{1,2}/\d{1,2}/\d{4},")  # anywhere in a word: clipped overflow can precede it


def _vertical_edges(page):
    xs = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "re":
                r = it[1]
            elif it[0] == "l":
                r = pymupdf.Rect(it[1], it[2]).normalize()
            else:
                continue
            if r.width <= 2 and r.height >= 15:
                xs.append(round((r.x0 + r.x1) / 2, 1))
    edges = []
    for x in sorted(xs):
        if not edges or x - edges[-1] > 3:
            edges.append(x)
    return edges


def _black_fills(page):
    out = []
    for d in page.get_drawings():
        f = d.get("fill")
        if f and max(f) < 0.15 and d["rect"].width > 5 and d["rect"].height > 5:
            out.append(d["rect"])
    return out


def _cell_words(page, spans):
    """Words rebuilt from characters, split at whitespace AND at column borders: where a full cell's text runs into
    the next cell (Filters into Search Time), the text layer's own words would merge the two cells."""
    edges = sorted({a for _, a, _ in spans} | {b for _, _, b in spans})
    col = lambda x: sum(1 for e in edges if x >= e) - 1  # index into spans (contiguous): -1 left of the table  # noqa: E731
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            cur, cur_col = [], None
            for span in line["spans"]:
                for ch in span["chars"]:
                    c, (x0, y0, x1, y1) = ch["c"], ch["bbox"]
                    k = col((x0 + x1) / 2)
                    if c.isspace() or (cur and k != cur_col):
                        if cur:
                            out.append((cur[0][0], min(b[1] for b in cur), cur[-1][2], max(b[3] for b in cur),
                                        "".join(b[4] for b in cur), cur_col))
                        cur = []
                    if not c.isspace():
                        cur.append((x0, y0, x1, y1, c))
                        cur_col = k
            if cur:
                out.append((cur[0][0], min(b[1] for b in cur), cur[-1][2], max(b[3] for b in cur),
                            "".join(b[4] for b in cur), cur_col))
    return out  # (x0, y0, x1, y1, text, span index)


def _join(parts):
    """Wrapped cell lines -> text: Excel wraps at a space (dropped) or after a hyphen (kept, no space)."""
    out = ""
    for p in parts:
        out = p if not out else (out + p if out.endswith("-") else out + " " + p)
    return out


def _lines(words):
    lines = {}
    for w in words:
        lines.setdefault(round(w[3], 0), []).append(w)   # by baseline-ish bottom y
    return lines


def _header(page):
    """(header bottom y, [(label, x0, x1)]) from the first line holding >= 3 known labels, else None."""
    for y, ws in sorted(_lines(page.get_text("words")).items()):
        ws = sorted(ws, key=lambda w: w[0])
        text = " ".join(w[4] for w in ws).lower()
        if sum(1 for k in LABELS if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", text)) < 3:
            continue
        # group words into labels: greedy longest match left to right
        out, i = [], 0
        while i < len(ws):
            for n in (n for n in (3, 2, 1) if i + n <= len(ws)):
                cand = " ".join(w[4] for w in ws[i:i + n]).lower()
                if cand in LABELS:
                    out.append((LABELS[cand], ws[i][0], ws[i + n - 1][2]))
                    i += n
                    break
            else:
                i += 1
        if len(out) >= 3:
            return y, out
    return None


def _spans(edges, labels):
    """Column (name, left, right) from border edges and header labels: each label goes to the span holding its center."""
    if len(edges) >= len(labels) + 1:
        spans = []
        for name, x0, x1 in labels:
            c = (x0 + x1) / 2
            for a, b in zip(edges, edges[1:]):
                if a <= c < b:
                    spans.append((name, a, b))
                    break
        if len(spans) == len(labels):
            return spans
    # no usable borders: boundaries halfway between neighbouring labels
    cs = [((x0 + x1) / 2, name, x0) for name, x0, x1 in labels]
    out = []
    for i, (c, name, x0) in enumerate(cs):
        left = (cs[i - 1][0] + c) / 2 if i else 0
        right = (c + cs[i + 1][0]) / 2 if i + 1 < len(cs) else 10_000
        out.append((name, left, right))
    return out


def rows(path, orgs=None):
    """Yield dict rows {column: text, '_page': n, '_row': k, '_under_fill': [columns]} for rows whose Org Name is in
    orgs (all rows when orgs is None). Pages without any target org string are skipped unread."""
    with pymupdf.open(path) as doc:
        if not doc.page_count:
            return
        hdr = _header(doc[0])
        if not hdr:
            return
        hdr_y, labels = hdr
        names = [n for n, _, _ in labels]
        if "Search Time" not in names:
            return
        spans = None
        for pno in range(doc.page_count):
            page = doc[pno]
            if orgs is not None:
                text = page.get_text()
                if not any(o in text for o in orgs):
                    continue
            edges = _vertical_edges(page)
            page_spans = _spans(edges, labels) if edges else None
            spans = page_spans or spans or _spans([], labels)
            st_i = [n for n, _, _ in spans].index("Search Time")
            words = [w for w in _cell_words(page, spans) if not (pno == 0 and w[3] <= hdr_y + 1)]
            lines = _lines(words)
            anchors = sorted(y for y, ws in lines.items() if any(w[5] == st_i and DATE.search(w[4]) for w in ws))
            fills = _black_fills(page)
            prev = (hdr_y + 1) if pno == 0 else -1
            for k, ay in enumerate(anchors, 1):
                band = sorted((w for y, ws in lines.items() if prev < y <= ay for w in ws), key=lambda w: (w[3], w[0]))
                prev = ay
                rec = {"_page": pno + 1, "_row": k, "_under_fill": []}
                for i, (name, a, b) in enumerate(spans):
                    cell = [w for w in band if w[5] == i]
                    if not cell:
                        rec[name] = None
                        continue
                    by_line = {}
                    for w in cell:
                        by_line.setdefault(round(w[3], 0), []).append(w[4])
                    parts = [" ".join(v) for _, v in sorted(by_line.items())]
                    rec[name] = "\n".join(parts) if name == "Time Frame" else _join(parts)
                    box = pymupdf.Rect(min(w[0] for w in cell), min(w[1] for w in cell),
                                       max(w[2] for w in cell), max(w[3] for w in cell))
                    if any(f.contains(box) or (f & box).get_area() > 0.8 * box.get_area() for f in fills):
                        rec["_under_fill"].append(name)
                # clipped overflow from the cell to the left can sit in front of the date: give it back
                t = rec.get("Search Time") or ""
                m = DATE.search(t)
                if m and m.start():
                    spill = t[:m.start()].strip()
                    rec["Search Time"] = t[m.start():]
                    left = spans[[n for n, _, _ in spans].index("Search Time") - 1][0]
                    rec[left] = (rec.get(left) or "") + spill
                if orgs is None or (rec.get("Org Name") or "").strip() in orgs:
                    yield rec


FLOCK_TIME = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}, \d{1,2}:\d{2}:\d{2} [AP]M UTC$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
SRC_ROW_BASIS = ("page * 1000 + row on that page: rows numbered from the top of each page by their Search Time cell "
                 "(page 1's header row excluded); cells read by position from the PDF's text layer")


def layout_ok(header, rows):
    """(True, None) when the position read is clean: >= 95% of rows have a complete Flock Search Time, a two-line Time
    Frame and a whole UUID (each where the header has that column). Otherwise (False, why)."""
    n = len(rows)
    if not n:
        return False, "no rows"
    checks = [("Search Time", lambda v: bool(v and FLOCK_TIME.match(v.strip())))]
    if "Time Frame" in header:
        checks.append(("Time Frame", lambda v: bool(v) and len(v.split("\n")) == 2
                       and all(FLOCK_TIME.match(x.strip()) for x in v.split("\n"))))
    if "ID" in header:
        checks.append(("ID", lambda v: bool(v and UUID_RE.match(v.strip()))))
    bad = [f"{c} {sum(not f(r.get(c)) for r in rows)}/{n}" for c, f in checks if sum(f(r.get(c)) for r in rows) < 0.95 * n]
    return (True, None) if not bad else (False, "layout not read cleanly: " + ", ".join(bad) + " rows malformed")


SAMPLE_PAGES = 5   # the gate runs on these first pages before the rest is read


def read_pdf(path):
    """Everything the loader needs from one PDF: {header, header_raw, n_pages, rows, ok, why, sampled}. header is None
    when page 1 holds no Flock header row (not an audit table, or image-only). The layout gate runs on the first
    SAMPLE_PAGES pages first: a layout that fails there is not read further (Fullerton's exports run to 33,000 pages),
    and rows then holds only those pages' rows (sampled = True). A PDF that passes is read in full and gated again."""
    with pymupdf.open(path) as doc:
        n_pages = doc.page_count
        hdr = _header(doc[0]) if n_pages else None
        header_raw = None
        if hdr:
            words = sorted((w for w in doc[0].get_text("words") if round(w[3], 0) == hdr[0]), key=lambda w: w[0])
            header_raw = [" ".join(w[4] for w in words)]
    if not hdr:
        return {"header": None, "header_raw": None, "n_pages": n_pages, "rows": [], "ok": False,
                "why": "no Flock header row on page 1"}
    header = [n for n, _, _ in hdr[1]]
    rs, gated = [], n_pages <= SAMPLE_PAGES
    for r in rows(path):
        if not gated and r["_page"] > SAMPLE_PAGES:
            gated = True
            ok, why = layout_ok(header, rs)
            if not ok:
                return {"header": header, "header_raw": header_raw, "n_pages": n_pages, "rows": rs, "ok": False,
                        "why": f"first {SAMPLE_PAGES} pages: {why}", "sampled": True}
        rs.append(r)
    ok, why = layout_ok(header, rs)
    return {"header": header, "header_raw": header_raw, "n_pages": n_pages, "rows": rs, "ok": ok, "why": why,
            "sampled": False}
