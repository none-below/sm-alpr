#!/usr/bin/env python3
"""Match registry agencies to FBI ORIs and (optionally) write the `ori` field.

The agency registry keys identity off Flock portal slugs and display names —
fragile and Flock-specific. The FBI's ORI (Originating Agency Identifier) is a
stable 9-char federal code that joins to any FBI/BJS dataset (sworn-officer
counts, UCR crime, LEMAS). This script attaches it.

Inputs:
  assets/agency_registry.json     — the registry (identity + geo)
  data/fbi/agencies.tsv           — FBI ORI directory (from refresh_fbi_agencies.py)

The match is one-time and reviewable. For each registry entry that plausibly
corresponds to a real law-enforcement agency, we find the best FBI candidate in
the SAME STATE and score it on three independent signals:

  place   normalized place-name agreement (e.g. "san mateo" == "san mateo")
  role    role consistency (registry sheriff ↔ FBI "...Sheriff's Office", etc.)
  geo     haversine distance between the two coordinate pairs (when both exist)

Confidence tiers:
  high    name + role agree, and geo (if available) confirms — auto-applied
  medium  partial name OR geo-only agreement — needs human/LLM verification
  low     weak signal — listed for review, not applied
  none    no plausible candidate

Outputs:
  data/fbi/ori_matches.json   — full provenance for every attempted entry
                                (tier, score, distance, candidate name) so the
                                verification step and future re-runs are auditable

  With --apply: writes `ori` into assets/agency_registry.json for `high`
  matches only, and NEVER overwrites an existing `ori` (hand-corrections and
  verified-medium promotions win). Re-running is idempotent.

Usage:
  uv run python scripts/match_ori.py                 # report only
  uv run python scripts/match_ori.py --apply         # + write high-confidence ori
  uv run python scripts/match_ori.py --slug san-mateo-ca-pd   # debug one agency
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import (  # noqa: E402
    load_registry, agency_coords, agency_state,
    canonical_place_name, agency_surface_forms, agency_display_name,
)

REGISTRY_PATH = Path("assets/agency_registry.json")
FBI_TSV = Path("data/fbi/agencies.tsv")
MATCHES_OUT = Path("data/fbi/ori_matches.json")

# Registry agency_role values worth trying to match. Others (hoa, business,
# school, test, decommissioned, other) are not FBI-tracked law enforcement and
# would only generate noise.
MATCHABLE_ROLES = {
    "police", "sheriff", "da", "campus_safety", "highway_patrol",
    "state_parks", "fish_wildlife", "corrections", "tribal", "parks",
}

# Statewide agencies the FBI splits across many reporting units with NO single
# statewide ORI (CHP = ~148 area offices, State Parks = ~10 districts). For
# these we attach the ENTIRE in-state family as the agency's ori list, and the
# report sums across it — because sharing to the agency grants its whole force
# access. Keyed by registry agency_role → predicate over an FBI agency name.
UMBRELLA_ROLES = {
    "highway_patrol": lambda n, t: "highway patrol" in n or t == "State Police",
    "state_parks":    lambda n, t: "state parks" in n,
    "fish_wildlife":  lambda n, t: "fish" in n and "wildlife" in n,
    "corrections":    lambda n, t: "correction" in n,
}

# Phrases stripped from both registry and FBI names to expose the core place
# token. Order matters — longest first so "police department" goes before
# "police". State words handled separately.
_STRIP_PHRASES = [
    "department of public safety", "public safety department",
    "office of the sheriff", "sheriff's department", "sheriffs department",
    "sheriff's office", "sheriffs office", "police department",
    "district attorney's office", "district attorneys office",
    "district attorney", "public safety", "police services",
    "marshal's office", "marshals office", "department of police",
    "police", "sheriff", "constable", "marshal", "department",
    "city of", "town of", "village of", "county of", "port of", "the ",
]

# Standalone abbreviation / filler tokens removed after phrase-stripping, so
# "Corona CA PD" and "Corona Police Department" both reduce to "corona", and
# "East Bay Regional Parks" lines up with "East Bay Parks".
_FILLER_TOKENS = {"pd", "so", "sd", "dps", "dept", "regional"}
_FILLER_RE = re.compile(r"\b(" + "|".join(_FILLER_TOKENS) + r")\b")

# Two-letter state codes embedded in Flock names ("Flint Township MI PD").
# "ca"/"in"/"or"/"me" etc. — stripped so the place token matches the FBI name.
_STATE_CODES = (
    "al ak az ar co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms "
    "mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa "
    "wv wi wy dc"
).split()
_STATE_CODE_RE = re.compile(r"\b(" + "|".join(_STATE_CODES) + r")\b")


def _norm_place(s):
    """Reduce an agency or place name to a comparison key: lowercase, drop
    role/agency words and 'county', strip punctuation and whitespace."""
    if not s:
        return ""
    s = s.lower()
    s = s.replace("&", " and ")
    # University naming: "University of California: Berkeley" → "berkeley";
    # registry campus entries vary, so keep the distinctive tail.
    s = re.sub(r"university of california:?\s*", "uc ", s)
    s = re.sub(r"california state university:?\s*", "csu ", s)
    for p in _STRIP_PHRASES:
        s = s.replace(p, " ")
    s = re.sub(r"\bcounty\b", " ", s)
    s = _STATE_CODE_RE.sub(" ", s)  # drop trailing "<place> MI pd" state codes
    s = re.sub(r"\bcalifornia\b", " ", s)
    s = _FILLER_RE.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)  # drop spaces+punct entirely
    return s


def _tokens(s):
    if not s:
        return set()
    s = s.lower()
    for p in _STRIP_PHRASES:
        s = s.replace(p, " ")
    toks = set(re.findall(r"[a-z0-9]+", s))
    return toks - {"county", "ca", "california"} - _FILLER_TOKENS


_RAW_STOP = {"of", "the", "and", "a", "ca", "california", "pd", "police",
             "department", "sheriff", "office", "city", "town"}


def _raw_tokens(s):
    """Tokens with NO phrase-stripping — used only for qualifier detection,
    where 'township'/'airport'/'isd' etc. must survive to distinguish a
    sub-agency from the parent city it was geocoded onto."""
    if not s:
        return set()
    return set(re.findall(r"[a-z0-9]+", s.lower())) - _RAW_STOP


# A sub-agency qualifier present on ONE side but not the other means the two
# names are almost certainly different agencies that happen to share a place
# name (e.g. "Flint Township PD" vs "Flint PD", "UC Riverside" vs the city of
# Riverside, "Humble ISD PD" vs "Humble PD"). The registry geocoder collapses
# these onto the parent place, so name-matching alone can't tell them apart;
# this guard catches it. "city"/"town" are NOT qualifiers — they're the default
# municipal form. "park" is excluded because it's usually a place name
# (Rohnert Park, Menlo Park), and it's only a signal when one side has it.
_QUALIFIER_GROUPS = [
    {"township", "twp"},
    {"village"},
    {"school", "schools", "isd", "cisd", "usd", "ausd"},
    {"airport"},
    {"university", "college", "univ", "uc", "csu", "campus"},
    {"hospital", "health", "medical", "healthcare"},
    {"port", "harbor", "levee", "wharf"},
    {"transit", "metro", "bart", "marta", "rail", "railway", "railroad"},
    {"tribal", "band", "rancheria", "pueblo", "reservation"},
    {"parks"},
    {"district", "drug", "task"},  # task forces, special districts
]


def _qualifier_mismatch(reg_raw, fbi_raw):
    """Return the first qualifier group present on exactly one side, or None."""
    for g in _QUALIFIER_GROUPS:
        in_reg = bool(g & reg_raw)
        in_fbi = bool(g & fbi_raw)
        if in_reg != in_fbi:
            return next(iter(g & (reg_raw | fbi_raw)))
    return None


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _fbi_role(name, type_name):
    n = name.lower()
    if "sheriff" in n:
        return "sheriff"
    if "district attorney" in n:
        return "da"
    if "highway patrol" in n or "state police" in n or type_name == "State Police":
        return "highway_patrol"
    if "university" in n or "college" in n or type_name == "University or College":
        return "campus_safety"
    if "tribal" in n or type_name == "Tribal":
        return "tribal"
    if "park" in n:
        return "parks"
    if "fish" in n and "wildlife" in n:
        return "fish_wildlife"
    if "correction" in n or "department of corrections" in n:
        return "corrections"
    if "police" in n or type_name == "City":
        return "police"
    return "other"


# Which FBI roles are acceptable for a given registry role. 1.0 = strong match,
# 0.5 = plausible (e.g. DA offices land in FBI "Other"), 0.0 = conflict.
def _role_compat(reg_role, fbi_role):
    if reg_role == fbi_role:
        return 1.0
    # DA offices are not in the FBI agency endpoint (confirmed 0 nationally in
    # CA). A DA must only match an FBI "District Attorney" row (fbi_role=="da")
    # — never the same-named city PD. So any da↔non-da pair is a hard conflict;
    # those entries fall through to `none` and are reported as un-matchable.
    if "da" in (reg_role, fbi_role):
        return 0.0
    ok_pairs = {
        ("parks", "other"), ("parks", "police"),
        ("state_parks", "parks"), ("state_parks", "other"),
        ("fish_wildlife", "other"),
        ("corrections", "other"),
        ("campus_safety", "police"),  # some campus PDs typed as City
        ("highway_patrol", "police"),
        ("tribal", "police"),
    }
    if (reg_role, fbi_role) in ok_pairs:
        return 0.5
    # Hard conflicts: police vs sheriff is the classic false-match trap.
    if {reg_role, fbi_role} == {"police", "sheriff"}:
        return 0.0
    return 0.2


def load_fbi(tsv_path):
    """Return {state: [agency_dict, ...]} indexed for fast same-state lookup."""
    by_state = {}
    with tsv_path.open() as f:
        header = f.readline().rstrip("\n").split("\t")
        idx = {c: i for i, c in enumerate(header)}
        for line in f:
            row = line.rstrip("\n").split("\t")
            if len(row) < len(header):
                continue
            lat = row[idx["lat"]]
            lng = row[idx["lng"]]
            ag = {
                "ori": row[idx["ori"]],
                "agency_name": row[idx["agency_name"]],
                "agency_type_name": row[idx["agency_type_name"]],
                "state": row[idx["state"]],
                "county": row[idx["county"]],
                "lat": float(lat) if lat else None,
                "lng": float(lng) if lng else None,
            }
            ag["norm"] = _norm_place(ag["agency_name"])
            ag["tokens"] = _tokens(ag["agency_name"])
            ag["raw"] = _raw_tokens(ag["agency_name"])
            ag["fbi_role"] = _fbi_role(ag["agency_name"], ag["agency_type_name"])
            by_state.setdefault(ag["state"], []).append(ag)
    return by_state


def umbrella_family(role, candidates):
    """Sorted list of all in-state ORIs belonging to a split statewide agency
    (CHP, State Parks…), or [] if `role` isn't an umbrella or none are found."""
    pred = UMBRELLA_ROLES.get(role)
    if not pred:
        return []
    fam = [c["ori"] for c in candidates
           if pred(c["agency_name"].lower(), c["agency_type_name"])]
    return sorted(fam)


def best_match(entry, candidates):
    """Score every same-state candidate; return (best_candidate, detail) or
    (None, detail). detail carries tier/score for the report."""
    reg_role = entry.get("agency_role")
    reg_lat, reg_lng = agency_coords(entry)
    reg_place = canonical_place_name(entry) or ""
    reg_norm = _norm_place(reg_place)
    # Surface forms give us extra spellings for the token-overlap fallback,
    # and raw tokens (un-stripped) for the sub-agency qualifier guard.
    reg_tokens = set()
    reg_raw = set()
    for form in agency_surface_forms(entry):
        reg_tokens |= _tokens(form)
        reg_raw |= _raw_tokens(form)
    reg_tokens |= _tokens(reg_place)

    scored = []
    for f in candidates:
        # place signal
        if reg_norm and f["norm"]:
            if reg_norm == f["norm"]:
                place = 1.0
            elif reg_norm in f["norm"] or f["norm"] in reg_norm:
                place = 0.85
            else:
                place = _jaccard(reg_tokens, f["tokens"])
        else:
            place = _jaccard(reg_tokens, f["tokens"])

        role = _role_compat(reg_role, f["fbi_role"])

        if reg_lat is not None and f["lat"] is not None:
            dist = _haversine_km(reg_lat, reg_lng, f["lat"], f["lng"])
            geo = max(0.0, 1.0 - dist / 25.0)  # 0 km→1.0, 25 km→0
        else:
            dist = None
            geo = None

        scored.append((place, role, geo, dist, f))

    if not scored:
        return None, {"tier": "none", "reason": "no same-state candidates"}

    # Rank LEXICOGRAPHICALLY: name first, then role, then geo as a tiebreaker.
    # FBI's per-agency coordinates are unreliable (e.g. Corona PD's longitude is
    # ~145 km off), so geo must never override an exact name+role match — it
    # only breaks ties among equally-named candidates. `geo or 0.0` keeps
    # coordinate-less candidates competitive at a tie.
    scored.sort(key=lambda t: (round(t[0], 3), t[1], t[2] if t[2] is not None else 0.0),
                reverse=True)
    place, role, geo, dist, f = scored[0]

    # Sub-agency guard: if a distinguishing qualifier (township/airport/ISD/
    # university/hospital/port…) is on one name but not the other, they're
    # likely different agencies sharing a place name — don't auto-trust it.
    qualifier = _qualifier_mismatch(reg_raw, f["raw"])

    tier = _tier(place, role, dist, qualifier)
    detail = {
        "tier": tier,
        "place": round(place, 3),
        "role": role,
        "distance_km": round(dist, 2) if dist is not None else None,
        "ori": f["ori"],
        "fbi_name": f["agency_name"],
        "fbi_role": f["fbi_role"],
    }
    if qualifier:
        detail["qualifier_mismatch"] = qualifier
    return f, detail


def _tier(place, role, dist, qualifier=None):
    """Confidence for the best candidate. Geo confirms or rescues, but never
    vetoes a strong name+role match (FBI coordinates are too noisy)."""
    near = dist is not None and dist <= 5
    # A role conflict (0.0) on the *best* candidate means no role-compatible
    # agency shares this name in-state — treat as no match, not a weak one.
    if role <= 0.0:
        return "none"
    # Qualifier mismatch caps confidence at `low` — the place/role may look
    # perfect, but it's the parent city, not this sub-agency. Verification (or
    # the sub-agency's own ORI) resolves it; never auto-applied.
    if qualifier:
        return "low"
    # HIGH: exact normalized name + compatible role. Within a single state this
    # is effectively unique, so distance is irrelevant.
    if place >= 1.0 and role >= 1.0:
        return "high"
    # HIGH: strong (substring) name + role, with geo confirming tightly.
    if place >= 0.85 and role >= 1.0 and near:
        return "high"
    # HIGH: geo lock (same spot) + role aligns + a real name signal.
    if dist is not None and dist <= 1.5 and role >= 1.0 and place >= 0.5:
        return "high"
    # MEDIUM: exact name but role only plausible (e.g. campus typed as City);
    # or strong/partial name without a tight geo confirmation.
    if place >= 1.0 and role >= 0.5:
        return "medium"
    if place >= 0.85 and role >= 1.0:
        return "medium"
    if place >= 0.55 and role >= 0.5:
        return "medium"
    if dist is not None and dist <= 3.0 and role >= 1.0 and place >= 0.35:
        return "medium"
    # LOW: a real but weak signal worth a human glance.
    if place >= 0.4 or (dist is not None and dist <= 10 and place >= 0.3):
        return "low"
    return "none"


def main():
    ap = argparse.ArgumentParser(description="Match registry agencies to FBI ORIs")
    ap.add_argument("--apply", action="store_true",
                    help="Write `ori` into the registry for high-confidence matches")
    ap.add_argument("--slug", help="Debug: only process the entry with this slug")
    args = ap.parse_args()

    if not FBI_TSV.exists():
        print(f"ERROR: {FBI_TSV} missing — run refresh_fbi_agencies.py first",
              file=sys.stderr)
        sys.exit(1)

    registry = json.loads(REGISTRY_PATH.read_text())
    by_state = load_fbi(FBI_TSV)

    results = []
    tier_counts = {"high": 0, "medium": 0, "low": 0, "none": 0, "skip": 0,
                   "preset": 0}

    for entry in registry:
        aid = entry["agency_id"]
        if args.slug and args.slug not in (
            [entry.get("slug")] + entry.get("flock_slugs", [])
        ):
            continue

        rec = {
            "agency_id": aid,
            "name": agency_display_name(entry),
            "state": agency_state(entry),
            "role": entry.get("agency_role"),
            "existing_ori": entry.get("ori"),
        }

        if entry.get("ori"):
            rec["tier"] = "preset"
            tier_counts["preset"] += 1
            results.append(rec)
            continue

        role = entry.get("agency_role")
        state = agency_state(entry)
        if role not in MATCHABLE_ROLES or not state:
            rec["tier"] = "skip"
            rec["reason"] = (f"role={role} not matchable"
                             if role not in MATCHABLE_ROLES else "no state")
            tier_counts["skip"] += 1
            results.append(rec)
            continue

        # Statewide umbrella agency: attach the whole in-state family of ORIs.
        family = umbrella_family(role, by_state.get(state, []))
        if family:
            rec["tier"] = "umbrella"
            rec["ori_list"] = family
            rec["fbi_count"] = len(family)
            tier_counts["umbrella"] = tier_counts.get("umbrella", 0) + 1
            results.append(rec)
            continue

        cand, detail = best_match(entry, by_state.get(state, []))
        rec.update(detail)
        if detail.get("ori"):
            rec["ori_list"] = [detail["ori"]]
        tier_counts[detail["tier"]] = tier_counts.get(detail["tier"], 0) + 1
        results.append(rec)

    # Second pass: flag collisions. An ORI claimed by >1 high match means at
    # most one is correct — usually a distinct sub-agency (airport/harbor/port
    # police) collapsed onto its parent city, or a true duplicate registry row.
    # We never auto-apply ANY of them; all go to human/LLM review. Doing this
    # after the loop (not first-come) keeps it order-independent.
    from collections import Counter
    high_ori = Counter(r["ori"] for r in results if r.get("tier") == "high")
    dup_oris = {o for o, c in high_ori.items() if c > 1}
    for r in results:
        if r.get("tier") == "high":
            r["ori_collision"] = r["ori"] in dup_oris

    MATCHES_OUT.parent.mkdir(parents=True, exist_ok=True)
    MATCHES_OUT.write_text(json.dumps(results, indent=2) + "\n")

    # Report
    print(f"Matched {len(results)} entries -> {MATCHES_OUT}")
    for t in ("high", "umbrella", "medium", "low", "none", "preset", "skip"):
        print(f"  {t:8s} {tier_counts.get(t, 0)}")
    n_collide = sum(1 for r in results if r.get("ori_collision"))
    if n_collide:
        print(f"\n  {n_collide} high matches share an ORI with another entry "
              f"({len(dup_oris)} ORIs) — excluded from auto-apply, flagged for review")

    if args.apply:
        # `ori` is always a LIST: one element for ordinary agencies, the full
        # family for umbrella agencies. Never overwrite an existing value
        # (hand-corrections and verified medium/low promotions win).
        applied = umbrella = 0
        by_id = {e["agency_id"]: e for e in registry}
        for rec in results:
            e = by_id[rec["agency_id"]]
            if e.get("ori"):
                continue
            t = rec.get("tier")
            if t == "umbrella":
                e["ori"] = rec["ori_list"]
                umbrella += 1
            elif t == "high" and not rec.get("ori_collision"):
                e["ori"] = rec["ori_list"]
                applied += 1
        REGISTRY_PATH.write_text(json.dumps(registry, indent=2) + "\n")
        print(f"\nApplied `ori` to {applied} high-confidence + {umbrella} "
              f"umbrella entries (lists; non-colliding, previously empty).")


if __name__ == "__main__":
    main()
