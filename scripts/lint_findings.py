#!/usr/bin/env python3
"""
Lint docs/SMPD_ALPR_Findings.md for source-numbering integrity.

Parallel PRs each adding a new source row can auto-merge into duplicate
numbered rows without a git conflict. Run this in CI to catch that — and
to catch inline [N] references that don't resolve to any source row.

Usage:
  python scripts/lint_findings.py              # check the canonical doc
  python scripts/lint_findings.py PATH         # check a specific file

Exits 0 on success, 1 on any error.
"""

import re
import sys
from pathlib import Path
from collections import defaultdict


DEFAULT_DOC = Path(__file__).resolve().parent.parent / "docs" / "SMPD_ALPR_Findings.md"

SOURCE_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|")
# [N] inline ref: 1–3 digits in brackets NOT followed by `(` (a markdown
# link). Catches [49], [35]; skips [text](url), ![alt](src), 4-digit years
# like "[2025]" used as editorial brackets in quotes.
INLINE_REF_RE = re.compile(r"\[(\d{1,3})\](?!\()")


def extract_source_section(text: str) -> tuple[list[str], int]:
    """Return (lines_in_section, start_line_number). Start is 1-indexed for
    error reporting."""
    lines = text.splitlines()
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("## Source Documents"):
            start = i + 1  # skip the heading itself
            continue
        if start is not None and line.startswith("## "):
            end = i
            break
    if start is None:
        print("ERROR: '## Source Documents' heading not found", file=sys.stderr)
        sys.exit(1)
    return lines[start:end], start + 1  # +1 so reported lines are 1-indexed


def main() -> int:
    doc_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DOC
    if not doc_path.exists():
        print(f"ERROR: {doc_path} not found", file=sys.stderr)
        return 1

    text = doc_path.read_text(encoding="utf-8")
    section_lines, section_start = extract_source_section(text)

    # Source rows: N -> list of 1-indexed line numbers where it appears.
    rows: dict[int, list[int]] = defaultdict(list)
    for offset, line in enumerate(section_lines):
        m = SOURCE_ROW_RE.match(line)
        if m:
            rows[int(m.group(1))].append(section_start + offset)

    errors: list[str] = []

    # Duplicate source numbers.
    for n, line_nums in sorted(rows.items()):
        if len(line_nums) > 1:
            locs = ", ".join(f"line {ln}" for ln in line_nums)
            errors.append(f"duplicate source #{n} ({locs})")

    # Orphan inline references: [N] in prose that doesn't resolve.
    known = set(rows.keys())
    orphans: dict[int, list[int]] = defaultdict(list)
    for i, line in enumerate(text.splitlines(), start=1):
        for m in INLINE_REF_RE.finditer(line):
            n = int(m.group(1))
            if n not in known:
                orphans[n].append(i)
    for n, line_nums in sorted(orphans.items()):
        sample = line_nums[0]
        extra = f" (+{len(line_nums) - 1} more)" if len(line_nums) > 1 else ""
        errors.append(f"inline [{n}] has no source row (first at line {sample}{extra})")

    if errors:
        print(f"lint_findings: {len(errors)} error(s) in {doc_path}:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"lint_findings: OK — {len(rows)} source rows, all inline refs resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
