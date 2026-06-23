#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
pdf_audit_revisions.py — recover the hidden revision history of a PDF, fingerprint
the tool behind each revision, and produce a timestamped, per-edit breakdown of
what changed.

Why this exists:
  An agency sometimes answers a request for a Flock search-audit export by producing
  a PDF, then opens that PDF in Adobe Acrobat and edits it before release — e.g.
  silently deleting a case number from a search-justification field. If the editor
  uses a plain "Save", the change is written as an *incremental update* appended to
  the file, and the pre-edit revision survives inside it. This tool reconstructs every
  revision (by truncating the file at each %%EOF), reports the generating/editing tool
  per revision, flags re-saves, and — for each edit — prints when it was saved and
  which rows or fields changed.

Why a token (word) diff, not a line diff:
  Editing in Acrobat re-serializes the page, so a text extractor returns the SAME
  visible content with different line breaks and ordering on the edited revision (e.g.
  a reason that was inline becomes its own line). A line- or position-based diff then
  reports unchanged rows as edited — false positives. This tool instead compares the
  *multiset of words* (order- and line-independent) to find genuinely added/removed
  text, and compares the *set of row UUIDs* to find deleted or added rows. Each changed
  word is mapped back to the row that uniquely contains it for context.

Limitation:
  A PDF re-rendered/flattened to a new file ("Print to PDF") carries no prior
  revisions — only one revision is reported and nothing can be recovered from it.

Usage:
  uv run python scripts/pdf_audit_revisions.py <file.pdf> [more.pdf ...]
  uv run python scripts/pdf_audit_revisions.py --dir <folder>
"""
import argparse
import re
from collections import Counter
from pathlib import Path

import fitz  # pymupdf

EOF_RE = re.compile(rb"%%EOF")
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Repeated page furniture, stripped so pagination shifts can't move it between rows.
BOILERPLATE = [
    re.compile(r"Powered by GovQA", re.I),
    re.compile(r"\bGovQA\b"),
    re.compile(r"\bPage \d+\b", re.I),
    re.compile(r"ID\s+userID\s+networkCount\s+Search Time\s+Reason", re.I),
]


def revision_ends(data: bytes) -> list[int]:
    """Byte offset just past each %%EOF marker; each is the end of one revision."""
    return [m.end() for m in EOF_RE.finditer(data)]


def parse_xmp(xmp: str) -> dict:
    def grab(pat: str, flags: int = 0) -> str:
        m = re.search(pat, xmp or "", flags)
        return m.group(1).strip() if m else ""

    return {
        "xmptk": grab(r'x:xmptk="([^"]*)"'),
        "doc_id": grab(r"<xmpMM:DocumentID>([^<]*)</xmpMM:DocumentID>"),
        "inst_id": grab(r"<xmpMM:InstanceID>([^<]*)</xmpMM:InstanceID>"),
        "xmp_create": grab(r"<xmp:CreateDate>([^<]*)</xmp:CreateDate>"),
        "xmp_modify": grab(r"<xmp:ModifyDate>([^<]*)</xmp:ModifyDate>"),
        # dc:creator is the authoring user; Acrobat writes it on save, so it names
        # whoever last edited the file.
        "dc_creator": grab(r"<dc:creator>.*?<rdf:li[^>]*>([^<]*)</rdf:li>", re.S),
    }


def classify(s: str) -> str:
    """Map a Producer/Creator/XMP-toolkit string to a recognizable application."""
    t = (s or "").lower()
    if not t:
        return ""
    if any(k in t for k in ("microsoft", "word", "excel", "powerpoint")):
        return "Microsoft (Office / Print to PDF)"
    if "acrobat" in t or "adobe" in t:
        return "Adobe Acrobat / Adobe PDF Library"
    if t.startswith("3.1-7") or "xmp toolkit 3.1" in t:
        return "legacy XMP toolkit 3.1 (older library; consistent with a native platform export)"
    if "skia" in t:
        return "Chromium / Chrome (Skia/PDF print-to-PDF)"
    if "wkhtmltopdf" in t:
        return "wkhtmltopdf (HTML-to-PDF)"
    if "reportlab" in t:
        return "ReportLab"
    if "itext" in t:
        return "iText"
    if "quartz" in t or "mac os x" in t:
        return "Apple Quartz / macOS"
    return ""


def fmt_date(d: str) -> str:
    """Render a PDF (D:YYYYMMDD...) or XMP-ISO date as 'YYYY-MM-DD HH:MM:SS TZ'."""
    if not d:
        return ""
    s = d[2:] if d.startswith("D:") else d
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(.*)$", s)
    if m:
        y, mo, da, h, mi, se, tz = m.groups()
        tz = tz.replace("'", ":").strip(":")
        return f"{y}-{mo}-{da} {h}:{mi}:{se}" + (f" {tz}" if tz else "")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(.*)$", s)
    if m:
        return f"{m.group(1)} {m.group(2)}" + (f" {m.group(3)}" if m.group(3) else "")
    return d


def norm(s: str) -> str:
    return " ".join(s.split())


def clean_text(doc: "fitz.Document") -> str:
    """Reading-order text (position-sorted so rows stay together), boilerplate removed."""
    t = " ".join(page.get_text("text", sort=True) for page in doc)
    for rx in BOILERPLATE:
        t = rx.sub(" ", t)
    return norm(t)


def rows_of(text: str) -> list[tuple[str, str]]:
    """Split text into (uuid, row_text) at each UUID, in order."""
    ms = list(UUID_RE.finditer(text))
    rows = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        rows.append((m.group(0).lower(), norm(text[m.start():end])))
    return rows


def is_trivial(tok: str) -> bool:
    """Drop pure punctuation and the UUID tokens themselves from the word diff."""
    return not re.search(r"[A-Za-z0-9]", tok) or bool(UUID_RE.fullmatch(tok))


def find_unique_row(rows: list[tuple[str, str]], tok: str):
    """Return the single row whose words contain tok, or None if 0 or many match."""
    hits = [r for r in rows if tok in r[1].split()]
    return hits[0] if len(hits) == 1 else None


def revision_record(data: bytes, end: int) -> dict:
    rec: dict = {"bytes": end}
    try:
        doc = fitz.open(stream=data[:end], filetype="pdf")
    except Exception as e:
        return {"bytes": end, "error": f"unparseable ({e})", "text": "", "rows": []}
    md = doc.metadata or {}
    try:
        xmp = doc.get_xml_metadata()
    except Exception:
        xmp = ""
    x = parse_xmp(xmp)
    rec.update(x)
    rec["producer"] = md.get("producer", "")
    rec["creator"] = md.get("creator", "")
    rec["author"] = md.get("author", "")
    rec["dc_creator"] = x["dc_creator"]
    rec["create"] = md.get("creationDate", "") or x["xmp_create"]
    rec["modify"] = md.get("modDate", "") or x["xmp_modify"]
    rec["pages"] = doc.page_count
    rec["generator"] = classify(rec["producer"]) or classify(rec["creator"])
    rec["xmp_tool"] = classify(x["xmptk"])
    rec["edited_marker"] = bool(x["doc_id"] and x["inst_id"] and x["doc_id"] != x["inst_id"]) or (
        bool(rec["create"]) and bool(rec["modify"]) and rec["create"] != rec["modify"]
    )
    rec["text"] = clean_text(doc)
    rec["rows"] = rows_of(rec["text"])
    doc.close()
    return rec


def diff_revisions(a: dict, b: dict) -> dict:
    """Order-independent word diff + UUID-set row diff between two revisions."""
    ra, rb = a.get("rows", []), b.get("rows", [])
    ida, idb = {r[0] for r in ra}, {r[0] for r in rb}
    deleted_rows = [r for r in ra if r[0] not in idb]
    added_rows = [r for r in rb if r[0] not in ida]

    wa = [w for w in a.get("text", "").split() if not is_trivial(w)]
    wb = [w for w in b.get("text", "").split() if not is_trivial(w)]
    ca, cb = Counter(wa), Counter(wb)
    removed = [(t, find_unique_row(ra, t)) for t in (ca - cb).elements()]
    added = [(t, find_unique_row(rb, t)) for t in (cb - ca).elements()]
    return {"deleted_rows": deleted_rows, "added_rows": added_rows, "removed": removed, "added": added}


def analyze(path: Path) -> None:
    data = path.read_bytes()
    ends = revision_ends(data)
    print(f"\n### {path.name}  ({len(data):,} bytes, {len(ends)} revision(s))")
    if not ends:
        print("  no %%EOF marker found — not a parseable PDF.")
        return
    recs = [revision_record(data, e) for e in ends]

    for i, r in enumerate(recs, 1):
        if r.get("error"):
            print(f"  rev {i}: {r['error']}")
            continue
        flag = "   [EDITED / RE-SAVED]" if r["edited_marker"] else ""
        print(f"  rev {i}: {r['pages']}pp   generated by: {r['generator'] or 'unidentified'}{flag}")
        if r["xmp_tool"] and r["xmp_tool"] != r["generator"]:
            print(f"         XMP last written by: {r['xmp_tool']}  (opened/edited in this app after generation)")
        who = r.get("dc_creator") or r.get("author")
        if who:
            print(f"         Author / dc:creator: {who}")
        print(f"         Producer={r['producer']!r}  xmptk={r.get('xmptk', '')!r}")
        print(f"         created={fmt_date(r['create'])}   modified={fmt_date(r['modify'])}")
        if r.get("doc_id") or r.get("inst_id"):
            same = "==" if r.get("doc_id") == r.get("inst_id") else "!=  (content re-saved)"
            print(f"         DocumentID {same} InstanceID")

    good = [r for r in recs if not r.get("error")]
    if good:
        first, last = good[0], good[-1]
        made = first["generator"] or first["xmp_tool"] or "an unidentified tool"
        line = f"  SUMMARY: generated by {made}"
        if first.get("create"):
            line += f" ({fmt_date(first['create'])})"
        editor = last["xmp_tool"] or last["generator"]
        if len(good) > 1 or last["edited_marker"]:
            line += f"; last edited in {editor}" if editor and editor != made else "; re-saved after generation"
            if last.get("modify"):
                line += f" ({fmt_date(last['modify'])})"
        print(line)

    if len(recs) < 2:
        return
    print("  --- edit timeline ---")
    edits = 0
    for i in range(1, len(recs)):
        a, b = recs[i - 1], recs[i]
        if a.get("error") or b.get("error"):
            continue
        ts = fmt_date(b["modify"] or b["create"])
        tool = b["xmp_tool"] or b["generator"] or "unidentified tool"
        editor = b.get("dc_creator") or b.get("author") or ""
        by = f" by {editor}" if editor else ""
        d = diff_revisions(a, b)
        rem, add = d["removed"], d["added"]
        dele, addr = d["deleted_rows"], d["added_rows"]
        total = len(rem) + len(add) + len(dele) + len(addr)
        if total == 0:
            print(f"  rev {i + 1} saved {ts}{by} [{tool}]: re-saved, no text change (linearization/metadata)")
            continue
        edits += 1
        print(f"  EDIT {edits} — saved {ts}{by}  [{tool}]  ({total} change(s)):")
        # lone word swap in a single row reads best as one change (e.g. a typo fix)
        if len(rem) == 1 and len(add) == 1 and not dele and not addr:
            (tr, row_r), (ta, row_a) = rem[0], add[0]
            anchor = row_r or row_a
            where = f"  row {anchor[0]}" if anchor else ""
            print(f"      CHANGED   '{tr}' -> '{ta}'{where}")
            if anchor:
                print(f"                {anchor[1]}")
        else:
            for tok, row in rem:
                ctx = f"  row {row[0]}: {row[1]}" if row else "  (appears in multiple rows / no unique row)"
                print(f"      REMOVED   '{tok}'{ctx}")
            for tok, row in add:
                ctx = f"  row {row[0]}: {row[1]}" if row else "  (appears in multiple rows / no unique row)"
                print(f"      ADDED     '{tok}'{ctx}")
        for uuid, text in dele:
            print(f"      ROW DELETED  {uuid}: {text}   (present in earlier revision, absent here)")
        for uuid, text in addr:
            print(f"      ROW ADDED    {uuid}: {text}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Recover a PDF's incremental-update revisions, per-revision tool, and a timestamped per-edit diff."
    )
    ap.add_argument("paths", nargs="*", help="PDF file(s) to analyze")
    ap.add_argument("--dir", help="analyze every *.pdf in this folder")
    args = ap.parse_args()

    files: list[Path] = []
    if args.dir:
        files += sorted(Path(args.dir).glob("*.pdf"))
    files += [Path(p) for p in args.paths]
    if not files:
        ap.error("provide one or more PDF files, or --dir <folder>")

    for f in files:
        if not f.exists():
            print(f"\n### {f}: not found")
            continue
        analyze(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
