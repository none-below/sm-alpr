#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Convert Flock "Network Audit" .xlsx exports to per-release gzipped NDJSON.

Faithful, lossless dump: one JSON object per line, one line per search, keys are
the workbook's column headers exactly as produced. Empty cells are omitted;
`***` redaction markers are kept verbatim. Used to commit a machine-readable
copy of a large ALPR audit production (e.g. Redwood City PRA 26-217 / 26-741)
without committing hundreds of MB of raw spreadsheets.

Handles the occasional header-less export (first row is data, not headers) by
falling back to the standard Flock network-audit column order.

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


def release_slug(name):
    s = re.sub(r"\s*\(\d+\)$", "", Path(name).stem)      # drop trailing " (1)"
    s = re.sub(r"\s*6th Release", "", s)
    s = re.sub(r"\s*\(10\.1\.23.*$", "-Dec2023", s)      # 4th-release long name
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def convert(fp, out_dir):
    wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    first = next(it, None) or ()
    hdr = [str(c).strip() if c is not None else "" for c in first]
    headerless = not (MAIN_HINTS & set(hdr))
    if headerless:
        hdr = SCHEMA_A[:]
        rows_iter = itertools.chain([first], it)  # first row was data
    else:
        rows_iter = it

    out_path = out_dir / (release_slug(fp.name) + ".ndjson.gz")
    n = 0
    with gzip.open(out_path, "wt", encoding="utf-8") as g:
        for r in rows_iter:
            obj = {}
            for i, key in enumerate(hdr):
                if i >= len(r) or not key:
                    continue
                v = r[i]
                if v is None:
                    continue
                if isinstance(v, str):
                    v = v.strip()
                    if v == "":
                        continue
                obj[key] = v
            if obj:
                g.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
                n += 1
    wb.close()
    return out_path, n, headerless


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", help="xlsx workbooks to convert")
    ap.add_argument("--out", required=True, help="output directory for *.ndjson.gz")
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
        out_path, n, headerless = convert(fp, out_dir)
        sz = out_path.stat().st_size
        total += n
        manifest.append({"source": fp.name, "output": out_path.name,
                         "rows": n, "gz_bytes": sz, "headerless": headerless})
        print(f"  {out_path.name:<42} rows={n:>9,}  {sz/1e6:6.1f}MB", file=sys.stderr)
    (out_dir / "_manifest.json").write_text(
        json.dumps({"files": manifest, "total_rows": total}, indent=2) + "\n")
    print(f"TOTAL: {total:,} rows, {len(files)} files -> {out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
