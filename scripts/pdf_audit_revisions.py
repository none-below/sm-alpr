#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
pdf_audit_revisions.py — recover the hidden revision history of a PDF, fingerprint
the tool behind each revision, and diff the text that changed between them.

Why this exists:
  An agency sometimes answers a request for a Flock search-audit export by producing
  a PDF, then opens that PDF in Adobe Acrobat and edits it before release — e.g.
  silently deleting a case number from a search-justification field. If the editor
  uses a plain "Save", the change is written as an *incremental update* appended to
  the file, and the pre-edit revision survives inside it. This tool reconstructs every
  revision (by truncating the file at each %%EOF), reports the producing/editing tool
  for each revision (from the Producer/Creator strings and the XMP toolkit), flags
  re-saves (DocumentID != InstanceID, or ModifyDate after CreateDate), and diffs the
  extracted text so removed or altered content is recovered.

  A row whose id is present in an earlier revision and absent later was deleted; a
  row present in both with different reason text was edited.

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


def norm(line: str) -> str:
    """Collapse whitespace so pure layout/reflow shifts are not flagged as edits."""
    return " ".join(line.split())


def extract_lines(doc: "fitz.Document") -> list[str]:
    out: list[str] = []
    for page in doc:
        out.extend(page.get_text("text").splitlines())
    return out


def revision_record(data: bytes, end: int) -> dict:
    rec: dict = {"bytes": end}
    try:
        doc = fitz.open(stream=data[:end], filetype="pdf")
    except Exception as e:  # truncated/partial revision that won't parse
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
    edited = bool(x["doc_id"] and x["inst_id"] and x["doc_id"] != x["inst_id"])
    if rec["create"] and rec["modify"] and rec["create"] != rec["modify"]:
        edited = True
    rec["edited_marker"] = edited
    rec["lines"] = extract_lines(doc)
    doc.close()
    return rec


def multiset_diff(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    co = Counter(norm(x) for x in old if norm(x))
    cn = Counter(norm(x) for x in new if norm(x))
    return list((co - cn).elements()), list((cn - co).elements())


def pair_by_leading_token(removed: list[str], added: list[str]):
    """Pair removed/added lines that share a leading token (e.g. a row's UUID)."""
    def key(s: str) -> str:
        parts = s.split()
        return parts[0] if parts else ""

    rem, add = defaultdict(list), defaultdict(list)
    for r in removed:
        rem[key(r)].append(r)
    for a in added:
        add[key(a)].append(a)
    pairs, only_removed, only_added = [], [], []
    for k in set(rem) | set(add):
        rs, as_ = rem[k], add[k]
        n = min(len(rs), len(as_))
        pairs.extend(zip(rs[:n], as_[:n]))
        only_removed.extend(rs[n:])
        only_added.extend(as_[n:])
    return pairs, only_removed, only_added


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
        gen = r["generator"] or "unidentified"
        print(f"  rev {i}: {r['pages']}pp   generated by: {gen}{flag}")
        if r["xmp_tool"] and r["xmp_tool"] != r["generator"]:
            print(f"         XMP last written by: {r['xmp_tool']}  (i.e. opened/edited in this app after generation)")
        print(f"         Producer={r['producer']!r}  Creator={r['creator']!r}  xmptk={r.get('xmptk', '')!r}")
        print(f"         create={r['create']}   modify={r['modify']}")
        if r.get("doc_id") or r.get("inst_id"):
            same = "==" if r.get("doc_id") == r.get("inst_id") else "!=  (content re-saved)"
            print(f"         DocumentID {same} InstanceID")

    # plain-English headline
    good = [r for r in recs if not r.get("error")]
    if good:
        first, last = good[0], good[-1]
        made = first["generator"] or first["xmp_tool"] or "an unidentified tool"
        line = f"  SUMMARY: generated by {made}"
        if first.get("create"):
            line += f" ({first['create']})"
        editor = last["xmp_tool"] or last["generator"]
        if len(good) > 1 or last["edited_marker"]:
            if editor and editor != made:
                line += f"; last edited in {editor}"
            else:
                line += "; re-saved after generation"
            if last.get("modify"):
                line += f" ({last['modify']})"
        print(line)

    for i in range(1, len(recs)):
        a, b = recs[i - 1], recs[i]
        if a.get("error") or b.get("error"):
            continue
        removed, added = multiset_diff(a["lines"], b["lines"])
        if not removed and not added:
            print(f"  rev {i}->{i + 1}: no text change (re-save only — linearization or metadata)")
            continue
        print(f"  rev {i}->{i + 1}: TEXT CHANGED  (-{len(removed)} / +{len(added)} lines, whitespace-normalized)")
        pairs, only_removed, only_added = pair_by_leading_token(removed, added)
        for old, new in pairs:
            print(f"      CHANGED   original: {old}")
            print(f"                produced: {new}")
        for r in only_removed:
            print(f"      REMOVED   {r}")
        for a_ in only_added:
            print(f"      ADDED     {a_}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Recover a PDF's incremental-update revisions, per-revision tool, and changed text."
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
