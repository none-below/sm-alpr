#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Generate docs/data/audit_check_manifest.json — the file picker for the PDF
revision/edit checker (docs/audit-check.html).

Scans the W012541 audit-log production(s), runs the revision analysis from
pdf_audit_revisions.py on each PDF, and emits one entry per file:

    {"label": "Feb 2026", "path": "assets/.../<file>.pdf", "edited": true}

The chip only flags WHETHER a file was edited after export (shown as "altered");
the page re-derives what/who/when live when the chip is clicked, so no edit detail
(and no person's name) is baked into this artifact. The page falls back to an inline
copy of this list when the artifact is absent, so adding a new month is:
scrape -> merge to main -> `make build` (regenerates this).
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pdf_audit_revisions as par  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
GLOB = "assets/san-mateo-public-records/W012541*/*.pdf"
OUT = REPO / "docs/data/audit_check_manifest.json"

MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def label_for(name: str) -> tuple[str, tuple]:
    """Human label + chronological sort key from a filename like
    '1_1_2025-1_31_2025-San_Mateo_CA_PD-Audit__Part_1_.pdf'."""
    m = re.match(r"^(\d+)_(\d+)_(\d+)-", name)
    if m:
        mo, _da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        label = f"{MONTHS[mo]} {yr}" if 1 <= mo <= 12 else str(yr)
    else:
        mo, yr, label = 0, 0, name
    pm = re.search(r"Part_(\d+)", name)
    part = int(pm.group(1)) if pm else 0
    if part:
        label += f" (pt{part})"
    return label, (yr, mo, part, name)


def analyze_file(path: Path, root: Path) -> tuple[dict, tuple]:
    # The chip only flags WHETHER a file was edited after export ("altered"); the
    # page re-derives what/who/when live when the chip is clicked. So we just detect
    # any recovered content change across the incremental revisions.
    data = path.read_bytes()
    recs = [par.revision_record(data, e) for e in par.revision_ends(data)]
    edited = False
    for i in range(1, len(recs)):
        a, b = recs[i - 1], recs[i]
        if a.get("error") or b.get("error"):
            continue
        d = par.diff_revisions(a, b)
        if d["removed"] or d["added"] or d["deleted_rows"] or d["added_rows"]:
            edited = True
            break

    # a flattened render (Print-to-PDF) keeps no recoverable history — it can't be
    # checked, so flag it as opaque rather than letting it read as "clean".
    base = recs[0] if recs else None
    flattened = bool(base and not base.get("error") and par.is_flatten(base.get("generator", "")))

    label, sort_key = label_for(path.name)
    entry: dict = {"label": label, "path": path.relative_to(root).as_posix()}
    if edited:
        entry["edited"] = True
    if flattened:
        entry["flattened"] = True
    return entry, sort_key


def build_manifest(root: Path = REPO) -> list[dict]:
    # only the monthly audit-log PDFs — skip other production records in the folder
    # (e.g. the GovQA message-history export).
    files = [f for f in sorted(root.glob(GLOB)) if "audit" in f.name.lower()]
    rows = [analyze_file(f, root) for f in files]
    rows.sort(key=lambda r: r[1])
    return [entry for entry, _ in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the audit-check picker manifest.")
    ap.add_argument("--print", action="store_true",
                    help="print JSON to stdout instead of writing the artifact")
    args = ap.parse_args()
    manifest = build_manifest()
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if args.print:
        print(text)
    else:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text)
        print(f"wrote {OUT.relative_to(REPO)} ({len(manifest)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
