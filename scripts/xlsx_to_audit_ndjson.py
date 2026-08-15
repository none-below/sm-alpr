#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Convert Flock "Network Audit" .xlsx exports to per-release gzipped NDJSON.

Faithful, lossless dump: one JSON object per line, one line per search, keys are
the workbook's column headers exactly as produced. Empty cells are omitted;
`***` redaction markers are kept verbatim. Used to commit a machine-readable
copy of a large ALPR audit production (e.g. Redwood City PRA 26-217 / 26-741,
Los Altos PRA 26-366) without committing hundreds of MB of raw spreadsheets.

Handles three quirks seen in agency-produced workbooks:

* **Header-less exports** (first row is data, not headers) fall back to the
  standard Flock network-audit column order.
* **Multi-sheet workbooks** (Los Altos ships one sheet per month) emit one
  NDJSON per sheet, suffixed `__<SHEET>`. Single-sheet workbooks keep their
  bare release slug, so earlier single-sheet productions regenerate unchanged.
* **Hand-edited headers.** Agencies delete columns in Excel before producing,
  and the header row does not always survive that edit intact. Two failure
  modes are detected by type-checking sampled data against each label's
  expected kind (int / date / text), never by trusting position alone:
  a *phantom* header (a label with no corresponding data column, which would
  silently shift every later field by one) and an *unlabeled* column (data
  present, header blank, which would silently drop the column). Phantom labels
  are dropped and unlabeled columns are kept under a `column_<N>` key; every
  such repair is recorded per sheet in `_manifest.json`. Ambiguous alignments
  are a hard error rather than a guess.

Usage:
  uv run --with openpyxl python scripts/xlsx_to_audit_ndjson.py \
      --out assets/redwood-city-pras/json  "PRA 26-217 2024 Q1.xlsx" ...
"""
import argparse
import gzip
import itertools
import json
import re
import sys
from pathlib import Path

import openpyxl

MAIN_HINTS = {"Search Time", "Org Name", "Search Type"}
# Standard Flock network-audit column order, for header-less exports.
SCHEMA_A = ["Name", "Org Name", "Total Networks Searched", "Total Devices Searched",
            "Time Frame", "License Plate", "Reason", "Case #", "Filters",
            "Search Time", "Search Type"]

# Expected value kind per known column, used to verify header/data alignment.
EXPECTED_KIND = {
    "Name": "text", "Org Name": "text", "Total Networks Searched": "int",
    "Total Devices Searched": "int", "Time Frame": "date", "License Plate": "text",
    "Reason": "text", "Case #": "text", "Filters": "text", "Search Time": "date",
    "Search Type": "text", "Text Prompt": "text", "Moderation": "text",
}
DATEISH = re.compile(r"\d{2}/\d{2}/\d{4},")
INTISH = re.compile(r"^\d+$")
ALIGN_SAMPLE = 500
# Agencies mark withheld cells either with Flock's native `***` or by typing
# over the column in Excel; both are kept verbatim and counted, not stripped.
REDACTION_MARKERS = {"***", "REDACTED", "Redacted", "redacted"}


def release_slug(name):
    s = re.sub(r"\s*\(\d+\)$", "", Path(name).stem)      # drop trailing " (1)"
    s = re.sub(r"\s*6th Release", "", s)
    s = re.sub(r"\s*\(10\.1\.23.*$", "-Dec2023", s)      # 4th-release long name
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def cell_kind(v):
    """Coarse type of one cell, for header/data alignment checks."""
    if v is None:
        return "none"
    s = str(v).strip()
    if s == "":
        return "none"
    if DATEISH.search(s):
        return "date"
    if INTISH.match(s):
        return "int"
    return "text"


def column_kinds(sample, ncol):
    """Dominant non-empty kind of each data column across sampled rows."""
    out = []
    for c in range(ncol):
        counts = {}
        for r in sample:
            k = cell_kind(r[c]) if c < len(r) else "none"
            if k != "none":
                counts[k] = counts.get(k, 0) + 1
        out.append(max(counts, key=counts.get) if counts else "none")
    return out


def pair_score(h, kind):
    """Agreement between one header label and its candidate data column."""
    want = EXPECTED_KIND.get(h) if h else None
    if want is None or kind == "none":
        return 0.0          # blank/unknown label, or an all-empty column
    return 1.0 if want == kind else -1.0


def align(hdr, kinds):
    """Map header positions to data positions, tolerating hand-edited headers.

    Returns (mapping, phantom) where mapping[data_index] = header label (or
    None for an unlabeled column) and phantom is the list of labels that
    matched no data column. Order is preserved on both sides; the assignment
    maximising total type agreement is chosen by dynamic programming, so a
    label the agency left behind after deleting its column is dropped rather
    than shifting every later field by one. Both kinds of gap are penalised,
    so a well-formed sheet always aligns straight through.
    """
    GAP = -0.25
    # Two labels of the same expected kind (e.g. the two "Total ... Searched"
    # counts) score identically against one surviving column, so ties are broken
    # toward binding the earlier label: a deleted column's leftover label is the
    # trailing one. EPS is far below any real agreement score, so it only ever
    # decides exact ties, never a genuine mismatch.
    EPS = 1e-6
    n, m = len(hdr), len(kinds)
    NEG = float("-inf")
    dp = [[NEG] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if dp[i][j] == NEG:
                continue
            if i < n and j < m:                      # label takes this column
                s = dp[i][j] + pair_score(hdr[i], kinds[j]) + EPS * (n - i)
                if s > dp[i + 1][j + 1]:
                    dp[i + 1][j + 1], back[i + 1][j + 1] = s, ("take", i, j)
            if i < n:                                # label with no column
                s = dp[i][j] + GAP
                if s > dp[i + 1][j]:
                    dp[i + 1][j], back[i + 1][j] = s, ("phantom", i, j)
            if j < m:                                # column with no label
                s = dp[i][j] + GAP
                if s > dp[i][j + 1]:
                    dp[i][j + 1], back[i][j + 1] = s, ("unlabeled", i, j)
    mapping, phantom = {}, []
    i, j = n, m
    while back[i][j] is not None:
        kind, pi, pj = back[i][j]
        if kind == "take":
            mapping[pj] = hdr[pi] or None
        elif kind == "phantom":
            if hdr[pi]:
                phantom.append(hdr[pi])
        else:
            mapping[pj] = None
        i, j = pi, pj
    phantom.reverse()
    return mapping, phantom


def verify(mapping, kinds):
    """Labels that still contradict their data column after alignment."""
    return [f"{h}@{d}:want={EXPECTED_KIND[h]},got={kinds[d]}"
            for d, h in mapping.items()
            if h in EXPECTED_KIND and kinds[d] != "none"
            and EXPECTED_KIND[h] != kinds[d]]


def sheet_header(ws):
    """First row of a sheet, normalised, plus whether it is a Flock audit sheet."""
    it = ws.iter_rows(values_only=True)
    first = next(it, None) or ()
    hdr = [str(c).strip() if c is not None else "" for c in first]
    return hdr, bool(MAIN_HINTS & set(hdr)), first, it


def convert_sheet(ws, out_path, strict=True, headerless=False):
    hdr, _, first, it = sheet_header(ws)
    if headerless:
        hdr = SCHEMA_A[:]
        rows_iter = itertools.chain([first], it)  # first row was data
    else:
        rows_iter = it

    # Buffer enough rows to type-check the header before committing to a mapping.
    sample = []
    for r in rows_iter:
        sample.append(r)
        if len(sample) >= ALIGN_SAMPLE:
            break
    ncol = max([len(hdr)] + [len(r) for r in sample]) if sample else len(hdr)
    kinds = column_kinds(sample, ncol)
    mapping, phantom = align(hdr, kinds)
    bad = verify(mapping, kinds)
    if bad and strict:
        raise SystemExit(f"header/data mismatch in sheet {ws.title!r}: {bad}\n"
                         f"  header: {hdr}\n  kinds : {kinds}")

    unlabeled = sorted(d for d, h in mapping.items()
                       if h is None and kinds[d] != "none")
    # Sorted so emitted JSON keys follow the workbook's column order.
    keys = {d: (mapping[d] or f"column_{d + 1}") for d in sorted(mapping)}

    n = 0
    present, redacted = {}, {}
    with gzip.open(out_path, "wt", encoding="utf-8") as g:
        for r in itertools.chain(sample, rows_iter):
            obj = {}
            for d, key in keys.items():
                if d >= len(r):
                    continue
                v = r[d]
                if v is None:
                    continue
                if isinstance(v, str):
                    v = v.strip()
                    if v == "":
                        continue
                    if v in REDACTION_MARKERS:
                        redacted[key] = redacted.get(key, 0) + 1
                obj[key] = v
                present[key] = present.get(key, 0) + 1
            if obj:
                g.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
                n += 1
    return {"rows": n, "headerless": headerless, "header": hdr,
            "phantom_headers": phantom, "unlabeled_columns": unlabeled,
            "mismatches": bad, "populated": present, "redacted": redacted}


def convert(fp, out_dir, strict=True):
    wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
    slug = release_slug(fp.name)

    # Agencies ship analysis tabs next to the export (Redwood City's releases
    # carry "SearchReason"/"Searcher" pivot tables). Convert only sheets that
    # actually look like a Flock audit; a lone sheet with no recognisable header
    # is the known header-less export, so keep the positional fallback for it.
    audit = [ws for ws in wb.worksheets if sheet_header(ws)[1]]
    headerless = False
    if not audit:
        audit, headerless = wb.worksheets[:1], True
    skipped = [ws.title for ws in wb.worksheets if ws not in audit]

    results = []
    for ws in audit:
        name = slug if len(audit) == 1 else \
            f"{slug}__{re.sub(r'[^A-Za-z0-9]+', '_', ws.title).strip('_')}"
        out_path = out_dir / (name + ".ndjson.gz")
        info = convert_sheet(ws, out_path, strict=strict, headerless=headerless)
        info.update(source=fp.name, sheet=ws.title if len(audit) > 1 else None,
                    output=out_path.name, gz_bytes=out_path.stat().st_size,
                    skipped_sheets=skipped)
        results.append(info)
    wb.close()
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", help="xlsx workbooks to convert")
    ap.add_argument("--out", required=True, help="output directory for *.ndjson.gz")
    ap.add_argument("--allow-mismatch", action="store_true",
                    help="record unresolved header/data mismatches instead of failing")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [Path(f) for f in args.files]
    missing = [f for f in files if not f.exists()]
    if missing:
        print(f"missing: {missing}", file=sys.stderr)
        return 1

    total = 0
    manifest = []
    for fp in sorted(files, key=lambda p: p.name):
        for info in convert(fp, out_dir, strict=not args.allow_mismatch):
            total += info["rows"]
            manifest.append(info)
            notes = []
            if info["phantom_headers"]:
                notes.append(f"phantom={info['phantom_headers']}")
            if info["unlabeled_columns"]:
                notes.append(f"unlabeled={info['unlabeled_columns']}")
            if info["headerless"]:
                notes.append("headerless")
            print(f"  {info['output']:<52} rows={info['rows']:>9,} "
                  f"{info['gz_bytes']/1e6:6.1f}MB {' '.join(notes)}", file=sys.stderr)
    (out_dir / "_manifest.json").write_text(
        json.dumps({"files": manifest, "total_rows": total}, indent=2) + "\n")
    print(f"TOTAL: {total:,} rows, {len(manifest)} outputs from {len(files)} "
          f"workbooks -> {out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
