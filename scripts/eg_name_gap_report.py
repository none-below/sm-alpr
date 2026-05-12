#!/usr/bin/env python3
"""Report which names in the Elk Grove inbound/outbound lists don't resolve
to an existing registry entry. Read-only — surfaces gaps for review.

Usage:
  uv run python scripts/eg_name_gap_report.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import resolve_agency

PORTAL_DIR = Path("assets/transparency.flocksafety.com/elk-grove-ca-pd")


def load(snapshot):
    p = PORTAL_DIR / f"{snapshot}.json"
    return json.loads(p.read_text())


def check(label, names):
    matched, missed = [], []
    for n in names:
        if resolve_agency(name=n):
            matched.append(n)
        else:
            missed.append(n)
    print(f"\n{label}: {len(matched)} matched, {len(missed)} missed")
    for n in missed:
        print(f"  MISS  {n}")
    return missed


def main():
    dec = load("2025-12-31")
    may = load("2026-05-12")

    all_missed = set()
    all_missed.update(check("Dec 2025 outbound", dec["sharing_outbound"]))
    all_missed.update(check("Dec 2025 inbound", dec["sharing_inbound"]))
    all_missed.update(check("May 2026 outbound", may["sharing_outbound"]))

    print(f"\n=== Unique gap entries: {len(all_missed)} ===")
    for n in sorted(all_missed):
        print(f"  {n}")


if __name__ == "__main__":
    main()
