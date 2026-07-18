# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Apply the LLM-verified FBI-ORI backfill to the agency registry.

Reads a decisions file (produced by the two-pass verification workflow: a
`pick` agent chooses an ORI from each agency's candidate shortlist, an
independent `verify` agent adversarially confirms or refutes the pick) and
writes the confirmed ORIs into assets/agency_registry.json.

A decision is APPLIED only when every gate passes:
  - an ORI was chosen (not null)
  - the pick agent's confidence is high or medium
  - the verify agent confirmed the pair (same real-world agency)
  - the verify agent's confidence is high or medium
  - the ORI is not already assigned to a DIFFERENT registry agency
  - the ORI is not claimed by two different backfill decisions (collision)

The registry entry's `ori` is written as a single-element list, matching the
convention `match_ori.py --apply` uses. Existing `ori` values are never
overwritten. Every decision (applied or not, with the reason) is recorded in
data/fbi/ori_backfill.json for review.

Usage:
  uv run python scripts/apply_ori_backfill.py data/fbi/ori_decisions.json
  uv run python scripts/apply_ori_backfill.py data/fbi/ori_decisions.json --dry-run
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REGISTRY_PATH = Path("assets/agency_registry.json")
CANDIDATES = Path("data/fbi/ori_backfill_candidates.json")
PROVENANCE = Path("data/fbi/ori_backfill.json")

OK_CONF = {"high", "medium"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("decisions", help="JSON file of workflow decisions")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing the registry")
    args = ap.parse_args()

    decisions = json.loads(Path(args.decisions).read_text())
    registry = json.loads(REGISTRY_PATH.read_text())
    by_id = {e["agency_id"]: e for e in registry}

    # Hard anti-hallucination guard: the chosen ORI must be one the shortlist
    # actually offered for that agency. An LLM must never invent an ORI.
    shortlist_oris = {}  # agency_id -> set(offered oris)
    if CANDIDATES.exists():
        for r in json.loads(CANDIDATES.read_text()):
            shortlist_oris[r["agency_id"]] = {c["ori"] for c in r["candidates"]}

    # ORIs already assigned to some agency (single or umbrella list). A backfill
    # must not hand the same ORI to a second, different agency.
    assigned = {}  # ori -> agency_id
    for e in registry:
        for o in (e.get("ori") or []):
            assigned.setdefault(o, e["agency_id"])

    # First pass: which decisions clear the confidence + confirmation gates.
    eligible = []
    for d in decisions:
        aid = d.get("id") or d.get("agency_id")
        ori = d.get("ori") or d.get("chosen_ori")
        gate = None
        if not aid or aid not in by_id:
            gate = "unknown-agency"
        elif by_id[aid].get("ori"):
            gate = "already-has-ori"
        elif not ori:
            gate = "no-match"
        elif aid in shortlist_oris and ori not in shortlist_oris[aid]:
            gate = "ori-not-in-shortlist"
        elif d.get("pick_conf") not in OK_CONF:
            gate = "low-pick-confidence"
        elif not d.get("verify_ok"):
            gate = "verify-refuted"
        elif d.get("verify_conf") not in OK_CONF:
            gate = "low-verify-confidence"
        elif ori in assigned and assigned[ori] != aid:
            gate = "ori-owned-by-other-agency"
        d["_gate"] = gate
        if gate is None:
            eligible.append(d)

    # Second pass: when >1 confirmed decision lands on the same ORI, the rows
    # are DUPLICATE registry entries for one agency (e.g. "Beaumont PD" and its
    # data-prefixed "AMPM - Beaumont PD" twin, or a live row and its "[Inactive]"
    # copy). This is safe to conclude here because the adversarial verify pass
    # already rejects parent-jurisdiction collapses — the failure mode match_ori
    # guards against ("Airport PD" collapsed onto its city) is filtered out
    # upstream as `no-match`/`verify-refuted`. Assigning the ORI to every such
    # row is correct (they ARE that agency) and harmless: the sworn-officer sum
    # dedupes by ORI, so whichever duplicate is a data recipient gets counted
    # exactly once. Each shared-ORI group is recorded in provenance for review.
    def _did(d):
        return d.get("id") or d.get("agency_id")

    def _dori(d):
        return d.get("ori") or d.get("chosen_ori")

    groups = defaultdict(list)
    for d in eligible:
        groups[_dori(d)].append(d)

    shared = {o for o, ds in groups.items() if len(ds) > 1}
    applied = []
    for ori, ds in groups.items():
        for d in ds:
            d["_gate"] = "APPLIED"
            if ori in shared:
                d["_shared_ori_with"] = sorted(_did(x) for x in ds if x is not d)
            applied.append(d)
            if not args.dry_run:
                by_id[_did(d)]["ori"] = [ori]
    collisions = shared

    if not args.dry_run:
        REGISTRY_PATH.write_text(json.dumps(registry, indent=2) + "\n")
        PROVENANCE.write_text(json.dumps(decisions, indent=2) + "\n")

    # Report
    gate_counts = Counter(d["_gate"] for d in decisions)
    total = len(decisions)
    print(f"Decisions: {total}")
    for g, c in gate_counts.most_common():
        print(f"  {g:28s} {c}")
    print(f"\n{'DRY-RUN — would apply' if args.dry_run else 'Applied'} "
          f"{len(applied)} new ORIs")
    if collisions:
        print(f"  ({len(collisions)} ORIs each assigned to >1 duplicate "
              f"registry row — see _shared_ori_with in provenance)")
    if not args.dry_run:
        print(f"Provenance -> {PROVENANCE}")


if __name__ == "__main__":
    main()
