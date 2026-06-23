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
  per revision (Producer/Creator string + XMP toolkit), flags re-saves, and — for each
  edit — prints when it was saved and exactly which text changed, was removed, or added.

Diffing is order-independent:
  Editing in Acrobat re-serializes the page, so a text extractor may return content in
  a different ORDER in the edited revision even where nothing visibly changed. The diff
  therefore compares the *multiset of normalized lines* between revisions (order does
  not matter) and pairs a removed line with an added line that shares a leading token,
  so a changed reason shows as original -> produced. This avoids the false positives a
  position/order-sensitive diff would produce.

Limitation:
  A PDF re-rendered/flattened to a new file ("Print to PDF") carries no prior
  revisions — only one revision is reported and nothing can be recovered from it.

Usage:
  uv run python scripts/pdf_audit_revisions.py <file.pdf> [more.pdf ...]
  uv run python scripts/pdf_audit_revisions.py --dir <folder>
"""
import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

import fitz  # pymupdf

EOF_RE = re.compile(rb"%%EOF")


def revision_ends(data: bytes) -> list[int]:
    """Byte offset just past each %%EOF marker; each is the end of one revision."""
    return [m.end() for m in EOF_RE.finditer(data)]


def parse_xmp(xmp: str) -> dict:
    def grab(pat: str) -> str:
        m = re.search(pat, xmp or "")
        return m.group(1).strip() if m else ""

    return {
        "xmptk": grab(r'x:xmptk="([^"]*)"'),
        "doc_id": grab(r"<xmpMM:DocumentID>([^<]*)</xmpMM:DocumentID>"),
        "inst_id": grab(r"<xmpMM:InstanceID>([^<]*)</xmpMM:InstanceID>"),
        "xmp_create": grab(r"<xmp:CreateDate>([^<]*)</xmp:CreateDate>"),
        "xmp_modify": grab(r"<xmp:ModifyDate>([^<]*)</xmp:ModifyDate>"),
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


def norm(line: str) -> str:
    """Collapse whitespace so pure layout/reflow shifts are not flagged as edits."""
    return " ".join(line.split())


def extract_lines(doc: "fitz.Document") -> list[str]:
    out: list[str] = []
    for page in doc:
        for line in page.get_text("text").splitlines():
            nl = norm(line)
            if nl:
                out.append(nl)
    return out


def revision_record(data: bytes, end: int) -> dict:
    rec: dict = {"bytes": end}
    try:
        doc = fitz.open(stream=data[:end], filetype="pdf")
    except Exception as e:
        return {"bytes": end, "error": f"unparseable ({e})", "lines": []}
    md = doc.metadata or {}
    try:
        xmp = doc.get_xml_metadata()
    except Exception:
        xmp = ""
    x = parse_xmp(xmp)
    rec.update(x)
    rec["producer"] = md.get("producer", "")
    rec["creator"] = md.get("creator", "")
    rec["create"] = md.get("creationDate", "") or x["xmp_create"]
    rec["modify"] = md.get("modDate", "") or x["xmp_modify"]
    rec["pages"] = doc.page_count
    rec["generator"] = classify(rec["producer"]) or classify(rec["creator"])
    rec["xmp_tool"] = classify(x["xmptk"])
    rec["edited_marker"] = bool(x["doc_id"] and x["inst_id"] and x["doc_id"] != x["inst_id"]) or (
        bool(rec["create"]) and bool(rec["modify"]) and rec["create"] != rec["modify"]
    )
    rec["lines"] = extract_lines(doc)
    doc.close()
    return rec


def diff_revisions(a: dict, b: dict):
    """Order-independent multiset line diff; pair removed/added by leading token.

    Returns (changed, removed, added):
      changed = [(old_line, new_line)]  paired by shared leading token
      removed = [line]                  present before, gone after (no pair)
      added   = [line]                  new after (no pair)
    """
    co, cn = Counter(a.get("lines", [])), Counter(b.get("lines", []))
    removed = list((co - cn).elements())
    added = list((cn - co).elements())

    def key(s: str) -> str:
        parts = s.split()
        return parts[0] if parts else ""

    rem, add = defaultdict(list), defaultdict(list)
    for r in removed:
        rem[key(r)].append(r)
    for a_ in added:
        add[key(a_)].append(a_)
    changed, only_removed, only_added = [], [], []
    for k in set(rem) | set(add):
        rs, as_ = rem[k], add[k]
        m = min(len(rs), len(as_))
        changed.extend(zip(rs[:m], as_[:m]))
        only_removed.extend(rs[m:])
        only_added.extend(as_[m:])
    # lone unpaired removed+added in one edit is almost certainly the same field
    # reworded (e.g. a typo fix) — pair it so it reads as a change, not delete+add.
    if len(only_removed) == 1 and len(only_added) == 1:
        changed.append((only_removed[0], only_added[0]))
        only_removed, only_added = [], []
    return changed, only_removed, only_added


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
        changed, removed, added = diff_revisions(a, b)
        total = len(changed) + len(removed) + len(added)
        if total == 0:
            print(f"  rev {i + 1} saved {ts} [{tool}]: re-saved, no text change (linearization/metadata)")
            continue
        edits += 1
        print(f"  EDIT {edits} — saved {ts}  [{tool}]  ({total} change(s)):")
        for o, n in changed:
            print(f"      CHANGED   original: {o}")
            print(f"                produced: {n}")
        for r in removed:
            print(f"      REMOVED   {r}   (present in earlier revision, absent here)")
        for a_ in added:
            print(f"      ADDED     {a_}")


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
