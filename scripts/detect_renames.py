#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Detect Flock agency lifecycle events from the sharing-list time series.

Each portal slug has a ~weekly series of snapshot JSONs
  assets/transparency.flocksafety.com/<slug>/<YYYY-MM-DD>.json
whose ``sharing_outbound`` / ``sharing_inbound`` arrays name partner agencies.
Diffing consecutive snapshots per slug and aggregating the SAME change across
many portals in a short window reveals three event classes:

  RENAME            mass ``(-"X", +"X'")`` where X and X' are the same place
                    (identical core-place tokens after stripping role/status/
                    state words). Flock renamed X -> X'.
  SYSTEMIC_REMOVAL  mass ``-X`` with no similar add -> X left the network
                    (decommission / de-federation). The offboarding signal.
  NEW_JOIN          mass ``+X`` with no similar remove -> X joined the network.

The single guard against combinatorial false positives (a portal that drops
several partners and adds several unrelated ones in the same refresh) is that a
RENAME pair must share an IDENTICAL core-place token set — this is what stops
e.g. "El Cerrito CA PD" pairing with "San Diego County CA SO" when San Diego
merely joins ~100 portals the same week.

For RENAME events the tool also resolves both spellings against the agency
registry and flags SPLITS: the two spellings minted two different agency_ids,
so one real agency is fractured into two rows and needs a registry fold
(see scripts/fold_registry_renames.py).

Read-only. Reads only the deterministic ``.json`` snapshots, never the sibling
scraped ``.html`` / ``.txt``.

Usage:
  uv run python scripts/detect_renames.py
  uv run python scripts/detect_renames.py --json outputs/renames.json
  uv run python scripts/detect_renames.py --min-portals 3 --splits-only
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import portal_jsons, resolve_agency  # noqa: E402

DATA_DIR = Path("assets/transparency.flocksafety.com")

_STATE_RE = re.compile(
    r"\b(A[LKZR]|C[AOT]|DE|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|"
    r"N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b"
)

# Generic role/status words stripped before comparing two names. 'county' and
# 'city' are DELIBERATELY kept — they distinguish a City PD from a County SO in
# the same place (e.g. Yuba City PD vs Yuba County SO), which must NOT merge.
_ROLE_WORDS = {
    "pd", "police", "department", "dept", "so", "sheriff", "sheriffs",
    "office", "dps", "university", "of", "the", "district", "dist",
    "campus", "co", "inactive", "deactivated", "dnu", "do", "not", "use",
}


def sharing_names(path):
    """Set of partner-agency name strings in a snapshot's sharing lists."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    out = set()
    for key in ("sharing_outbound", "sharing_inbound"):
        val = data.get(key)
        if isinstance(val, list):
            out.update(n.strip() for n in val if isinstance(n, str) and n.strip())
    return out


def core_tokens(name):
    """Normalized set of place tokens: drop status tags, punctuation, role words."""
    n = re.sub(r"\[.*?\]|\(.*?\)", " ", name.lower())   # drop [Inactive], (deactivated)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return frozenset(t for t in n.split() if t not in _ROLE_WORDS)


def _states(name):
    return set(_STATE_RE.findall(name))


def same_place(a, b):
    """True iff a and b name the same place, differing only in role / format /
    status / state-position — the anti-false-positive gate for renames."""
    if a == b:
        return False
    ta, tb = core_tokens(a), core_tokens(b)
    if not ta or ta != tb:
        return False
    sa, sb = _states(a), _states(b)
    return not (sa and sb and sa != sb)


def _window(dates):
    ds = sorted(dates)
    return f"{ds[0]}..{ds[-1]}" if ds else "?"


def _confidence(n):
    return "high" if n >= 3 else ("med" if n == 2 else "low")


def detect(data_dir, min_portals):
    """Walk every slug's snapshot series and aggregate the three event classes."""
    rename_hits = defaultdict(lambda: {"slugs": set(), "dates": set()})   # (old, new)
    removed_hits = defaultdict(lambda: {"slugs": set(), "dates": set()})  # name
    added_hits = defaultdict(lambda: {"slugs": set(), "dates": set()})    # name

    slugs = 0
    for slug_dir in sorted(data_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue
        snaps = portal_jsons(slug_dir)
        if len(snaps) < 2:
            continue
        slugs += 1
        slug = slug_dir.name
        prev = None
        for path in snaps:
            date = path.stem
            cur = sharing_names(path)
            if prev is not None:
                removed = prev - cur
                added = cur - prev
                matched_removed, matched_added = set(), set()
                if removed and added:
                    for old in removed:
                        for new in added:
                            if same_place(old, new):
                                hit = rename_hits[(old, new)]
                                hit["slugs"].add(slug)
                                hit["dates"].add(date)
                                matched_removed.add(old)
                                matched_added.add(new)
                for nm in removed - matched_removed:
                    removed_hits[nm]["slugs"].add(slug)
                    removed_hits[nm]["dates"].add(date)
                for nm in added - matched_added:
                    added_hits[nm]["slugs"].add(slug)
                    added_hits[nm]["dates"].add(date)
            prev = cur

    rename_names = set()
    for old, new in rename_hits:
        rename_names.add(old)
        rename_names.add(new)

    def rid(name):
        e = resolve_agency(name=name)
        return e["agency_id"] if e else None

    renames = []
    for (old, new), info in rename_hits.items():
        old_id, new_id = rid(old), rid(new)
        renames.append({
            "old": old, "new": new,
            "portals": len(info["slugs"]),
            "confidence": _confidence(len(info["slugs"])),
            "window": _window(info["dates"]),
            "old_id": old_id, "new_id": new_id,
            "registry_split": bool(old_id and new_id and old_id != new_id),
        })
    renames.sort(key=lambda r: -r["portals"])

    removals = []
    for nm, info in removed_hits.items():
        n = len(info["slugs"])
        if n >= min_portals and nm not in rename_names:
            removals.append({
                "name": nm, "portals": n, "window": _window(info["dates"]),
                "agency_id": rid(nm),
            })
    removals.sort(key=lambda r: -r["portals"])

    joins = []
    for nm, info in added_hits.items():
        n = len(info["slugs"])
        if n >= min_portals and nm not in rename_names:
            joins.append({
                "name": nm, "portals": n, "window": _window(info["dates"]),
                "agency_id": rid(nm),
            })
    joins.sort(key=lambda r: -r["portals"])

    return {"slugs_scanned": slugs, "renames": renames,
            "systemic_removals": removals, "new_joins": joins}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--min-portals", type=int, default=3,
                    help="distinct-portal threshold for a systemic removal/join (default 3)")
    ap.add_argument("--json", type=Path, default=None, help="also write full results as JSON")
    ap.add_argument("--splits-only", action="store_true",
                    help="print only RENAME events that fractured the registry")
    args = ap.parse_args()

    res = detect(args.data_dir, args.min_portals)
    splits = [r for r in res["renames"] if r["registry_split"]]

    print(f"scanned {res['slugs_scanned']} slugs\n")

    print("== RENAMES (registry SPLIT = needs fold) ==")
    for r in res["renames"]:
        if args.splits_only and not r["registry_split"]:
            continue
        flag = "  SPLIT->FOLD" if r["registry_split"] else ""
        print(f"  {r['portals']:>3} [{r['confidence']:>4}] {r['old']!r} -> {r['new']!r} "
              f"| {r['window']}{flag}")

    if not args.splits_only:
        print(f"\n== SYSTEMIC REMOVALS (>= {args.min_portals} portals; decommission/de-federation signal) ==")
        for r in res["systemic_removals"]:
            print(f"  {r['portals']:>3} {r['name']!r} | {r['window']}")

        print(f"\n== NEW JOINS (>= {args.min_portals} portals) ==")
        for r in res["new_joins"]:
            print(f"  {r['portals']:>3} {r['name']!r} | {r['window']}")

    print(f"\nrenames={len(res['renames'])} splits={len(splits)} "
          f"removals={len(res['systemic_removals'])} joins={len(res['new_joins'])}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(res, indent=2) + "\n")
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
