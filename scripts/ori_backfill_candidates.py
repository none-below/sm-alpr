# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Emit per-agency FBI-ORI candidate shortlists for the verified backfill.

`match_ori.py` auto-applies only HIGH-confidence, name+role-exact matches. Every
other missing-ORI agency (medium/low/collision) needs a human/LLM judgment call:
is the best FBI candidate the SAME real-world agency, or a parent city the
geocoder collapsed onto (Port of Stockton PD ≠ Stockton PD), or nothing at all?

This script prepares that judgment. For each registry entry that (a) lacks an
`ori`, (b) has an FBI-matchable role, and (c) has a state, it emits the top-K
same-state FBI candidates ranked exactly as `match_ori.best_match` ranks them
(role bucket, then normalized-name agreement, then geo). The LLM verifier
(scripts workflow) then picks the correct ORI from the shortlist, or none.

Output: data/fbi/ori_backfill_candidates.json — one record per entry:
  { agency_id, name, surface_forms, role, state, place, coords, website,
    matcher_tier, candidates: [ {ori, fbi_name, fbi_type, fbi_county,
    place_score, role_compat, distance_km}, ... ] }

Usage:
  uv run python scripts/ori_backfill_candidates.py            # all eligible
  uv run python scripts/ori_backfill_candidates.py --limit 20 # debug sample
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import (  # noqa: E402
    agency_coords, agency_state, canonical_place_name,
    agency_surface_forms, agency_display_name,
)
import match_ori as mo  # noqa: E402

REGISTRY_PATH = Path("assets/agency_registry.json")
FBI_TSV = Path("data/fbi/agencies.tsv")
MATCHES = Path("data/fbi/ori_matches.json")
OUT = Path("data/fbi/ori_backfill_candidates.json")

TOP_K = 8


def score_candidates(entry, candidates):
    """Return every same-state FBI candidate scored (place, role, geo, dist),
    ranked with match_ori's role-bucket-first ordering."""
    reg_role = entry.get("agency_role")
    reg_lat, reg_lng = agency_coords(entry)
    reg_place = canonical_place_name(entry) or ""
    reg_norm = mo._norm_place(reg_place)
    reg_tokens = set()
    reg_raw = set()
    for form in agency_surface_forms(entry):
        reg_tokens |= mo._tokens(form)
        reg_raw |= mo._raw_tokens(form)
    reg_tokens |= mo._tokens(reg_place)

    scored = []
    for f in candidates:
        if reg_norm and f["norm"]:
            if reg_norm == f["norm"]:
                place = 1.0
            elif reg_norm in f["norm"] or f["norm"] in reg_norm:
                place = 0.85
            else:
                place = mo._jaccard(reg_tokens, f["tokens"])
        else:
            place = mo._jaccard(reg_tokens, f["tokens"])

        role = mo._role_compat(reg_role, f["fbi_role"])

        if reg_lat is not None and f["lat"] is not None:
            dist = mo._haversine_km(reg_lat, reg_lng, f["lat"], f["lng"])
        else:
            dist = None

        qualifier = mo._qualifier_mismatch(reg_raw, f["raw"])
        scored.append((place, role, dist, qualifier, f))

    def bucket(r):
        return 3 if r >= 1.0 else 2 if r >= 0.5 else 1 if r > 0.0 else 0

    # Drop pure-noise rows (no name signal at all) before ranking. Rank the
    # SHORTLIST by NAME first (recall-oriented): the correct ORI is almost always
    # the exact/substring name match, and it must survive the top-K cut even when
    # a role quirk (e.g. a parks district the registry typed as `police`) would
    # otherwise sink it. The LLM verifier then weighs role/geo/knowledge itself.
    scored = [s for s in scored if s[0] > 0.0]
    scored.sort(key=lambda t: (round(t[0], 3), bucket(t[1]),
                               -(t[2] if t[2] is not None else 9e9)),
                reverse=True)
    return scored


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="Only emit the first N records (debug)")
    args = ap.parse_args()

    if not FBI_TSV.exists():
        print(f"ERROR: {FBI_TSV} missing — run refresh_fbi_agencies.py first",
              file=sys.stderr)
        sys.exit(1)

    registry = json.loads(REGISTRY_PATH.read_text())
    by_state = mo.load_fbi(FBI_TSV)
    tier_by_id = {}
    if MATCHES.exists():
        tier_by_id = {r["agency_id"]: r.get("tier")
                      for r in json.loads(MATCHES.read_text())}

    out = []
    for entry in registry:
        if entry.get("ori"):
            continue
        role = entry.get("agency_role")
        state = agency_state(entry)
        if role not in mo.MATCHABLE_ROLES or not state:
            continue
        scored = score_candidates(entry, by_state.get(state, []))
        if not scored:
            continue
        cands = []
        for place, rc, dist, qual, f in scored[:TOP_K]:
            cands.append({
                "ori": f["ori"],
                "fbi_name": f["agency_name"],
                "fbi_type": f["agency_type_name"],
                "fbi_county": f["county"],
                "place_score": round(place, 3),
                "role_compat": rc,
                "distance_km": round(dist, 1) if dist is not None else None,
                "qualifier_mismatch": qual,
            })
        lat, lng = agency_coords(entry)
        out.append({
            "agency_id": entry["agency_id"],
            "name": agency_display_name(entry, entry.get("slug")),
            "surface_forms": agency_surface_forms(entry),
            "role": role,
            "state": state,
            "place": canonical_place_name(entry),
            "coords": [lat, lng] if lat is not None else None,
            "website": entry.get("website"),
            "matcher_tier": tier_by_id.get(entry["agency_id"]),
            "candidates": cands,
        })
        if args.limit and len(out) >= args.limit:
            break

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    from collections import Counter
    tc = Counter(r["matcher_tier"] for r in out)
    print(f"Wrote {len(out)} candidate shortlists -> {OUT}")
    print("  by matcher tier:", dict(tc.most_common()))


if __name__ == "__main__":
    main()
