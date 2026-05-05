#!/usr/bin/env python3
"""Fuzzy-match agency references in article text against agency_registry.

For each agency in the registry, build a surface-form set (display_name +
flock_names + aliases) and search the article for case-insensitive matches.
Returns a ranked candidate list, never a single answer — the same alias
may legitimately resolve to multiple agencies (Springfield PD, Livingston
PD, etc.) and the curator picks.

Scoring is simple and replaceable:
  - base: 1 point per surface-form match
  - longer surface form: small bonus (specific > generic)
  - article mentions agency's state: bonus (geographic disambiguation)
  - source domain ends in agency's state's TLDs (rare): bonus
  - default lean: CA-resident agencies score slightly higher when context
    is silent (this project is SMPD-focused; tune as the corpus broadens)

Usage:
  scripts/agency_lookup.py --text-file article.txt
  scripts/agency_lookup.py --text-file article.txt --source-domain sfchronicle.com
  echo "Article body" | scripts/agency_lookup.py --stdin

Output (JSON):
  {"matches": [
    {"agency_id": "...", "display_name": "...", "score": 4.2,
     "matched_forms": ["San Mateo PD", "SMPD"], "state": "CA"},
    ...
  ]}
"""

import argparse
import functools
import json
import re
import sys
from pathlib import Path

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from lib import (
    agency_display_name,
    agency_state,
    agency_surface_forms,
    canonical_place_name,
    load_registry,
)

# agency_role → journalistic role suffixes the matcher should try.
# Place name comes from canonical_place_name(entry) (FIPS-derived geo.name
# with County suffix for county entries, mirroring report.js's
# shortAgencyName). The display name fallback is handled inside that helper.
ROLE_FORMS = {
    "police":         ["Police Department", "PD", "Police"],
    "sheriff":        ["Sheriff's Office", "Sheriff's Department", "Sheriff", "SO"],
    "da":             ["District Attorney's Office", "DA's Office", "District Attorney", "DA"],
    "fire":           ["Fire Department", "FD", "Fire"],
    "campus_safety":  ["Campus Safety", "Public Safety", "Department of Public Safety", "DPS"],
    "parks":          ["Parks Department", "Parks Police"],
    "highway_patrol": ["Highway Patrol", "CHP"],
    "corrections":    ["Department of Corrections", "Corrections", "DOC"],
    "intelligence":   [],
    "other":          [],
}


def _bare_place_form(entry) -> str | None:
    """Return the bare place-name form for this agency if it should be
    matched. Returns None for entries where matching by bare place would
    be too noisy:

    - test entries (display_name 'demo' etc.) — never a real subject
    - state-level entities — geo.name is a 2-letter state code that
      collides with English prepositions ('IN', 'OR', 'AT')
    - very short places (<=2 chars) — same noise class as state codes

    The longer "{place} {role suffix}" forms (e.g. "Bay Police
    Department") are still emitted by derive_journalistic_forms; only
    the standalone "Bay" / "IN" form is suppressed here.
    """
    place = canonical_place_name(entry)
    if not place:
        return None
    role = entry.get("agency_role")
    if role == "test":
        return None
    geo = entry.get("geo") or {}
    if geo.get("kind") == "state":
        return None
    if len(place) <= 2:
        return None
    return place


def derive_journalistic_forms(entry) -> list[str]:
    """Build the surface forms a journalist might write for this agency.

    Builds ``{canonical place} {role suffix}`` for every suffix in
    ROLE_FORMS[agency_role], plus the bare place name (weakest), plus
    every flock_name verbatim and any explicit aliases on the entry.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(form: str) -> None:
        f = (form or "").strip()
        if f and f.lower() not in seen:
            out.append(f)
            seen.add(f.lower())

    place = canonical_place_name(entry)
    role = entry.get("agency_role")
    if place and role and role in ROLE_FORMS:
        for suffix in ROLE_FORMS[role]:
            add(f"{place} {suffix}")
        # Bare place is weakest and also the noisiest. _bare_place_form
        # gates it on whether matching by raw place name is meaningful
        # for this agency (e.g. skip 'IN' for Indiana DOC).
        bare = _bare_place_form(entry)
        if bare:
            add(bare)

    for fn in entry.get("flock_names", []) or []:
        add(fn)
    for alias in entry.get("aliases", []) or []:
        add(alias)

    return out

# US state code → full name, for state-mention detection.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def detect_states(text: str) -> set[str]:
    """Return the 2-letter codes of US states explicitly mentioned in text."""
    found: set[str] = set()
    lower = text.lower()
    for code, name in US_STATES.items():
        if name.lower() in lower:
            found.add(code)
        if re.search(rf"\b{code}\b", text):
            found.add(code)
    return found


def find_form_spans(text: str, form: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of case-insensitive whole-word matches."""
    if not form or not form.strip():
        return []
    pattern = r"\b" + re.escape(form) + r"\b"
    return [(m.start(), m.end()) for m in re.finditer(pattern, text, flags=re.IGNORECASE)]


def all_agency_forms(entry) -> list[str]:
    """Every plausible surface form for an agency, longest first.

    Combines the explicit set (display_name + flock_names + aliases) with
    journalistic expansions ({canonical place} + role suffix variants).
    """
    forms = set(agency_surface_forms(entry))
    forms.update(derive_journalistic_forms(entry))
    forms = {f for f in forms if f and f.strip()}
    return sorted(forms, key=lambda s: (-len(s), s.lower()))


def lookup(text: str, *, source_domain: str | None = None,
           top_n: int = 10) -> list[dict]:
    """Return top-N candidate agencies for an article.

    Span collision resolution: every (form, agency) match contributes a
    span. We sort by length desc and sweep, keeping a span only if it
    doesn't fall inside an already-kept longer span. This is what stops
    'Palo Alto' (Palo Alto PD) from scoring inside 'East Palo Alto'
    (East Palo Alto PD) text — the longer form wins the position, the
    shorter one is discarded.
    """
    mentioned_states = detect_states(text)
    source_state_hint = None
    if source_domain:
        d = source_domain.lower()
        if d.endswith("sfchronicle.com") or d.endswith("mercurynews.com") \
                or d.endswith("sacbee.com") or d.endswith("kqed.org") \
                or d.endswith("smdailyjournal.com") or d.endswith("latimes.com"):
            source_state_hint = "CA"

    registry = load_registry()
    # Collect (start, end, agency_idx, form) for every form match in text.
    # Bare-place forms (e.g. "Oakland" for Oakland TN PD) only count when
    # the agency's state is corroborated in the article — otherwise an
    # article about Oakland CA scores Oakland TN PD just as highly.
    candidates_raw: list[tuple[int, int, int, str]] = []
    for ai, entry in enumerate(registry):
        bare = _bare_place_form(entry)
        state = agency_state(entry)
        state_corroborated = bool(
            state and (state in mentioned_states or state == source_state_hint)
        )
        for form in all_agency_forms(entry):
            if bare and form == bare and not state_corroborated:
                continue
            for s, e in find_form_spans(text, form):
                candidates_raw.append((s, e, ai, form))
    if not candidates_raw:
        return []

    # Resolve overlaps: longest form wins each text position. Sort by
    # length descending; for each candidate, keep it only if its span
    # isn't fully covered by an already-kept span. (Equal-length spans
    # at the same position will both be kept — they belong to different
    # agencies that legitimately share an alias; the curator picks.)
    candidates_raw.sort(key=lambda t: (-(t[1] - t[0]), t[0]))
    kept: list[tuple[int, int, int, str]] = []
    for c in candidates_raw:
        s, e, ai, form = c
        contained = False
        for ks, ke, kai, _ in kept:
            if ks <= s and e <= ke and (ks, ke) != (s, e) and kai != ai:
                contained = True
                break
        if not contained:
            kept.append(c)

    # Aggregate per-agency.
    by_agency: dict[int, dict] = {}
    for s, e, ai, form in kept:
        slot = by_agency.setdefault(ai, {"score": 0.0, "matched_forms": set()})
        length_bonus = min(0.5, (e - s) / 60.0)
        slot["score"] += 1.0 + length_bonus
        slot["matched_forms"].add(form)

    out: list[dict] = []
    for ai, slot in by_agency.items():
        entry = registry[ai]
        state = agency_state(entry)
        score = slot["score"]
        if state and state in mentioned_states:
            score += 1.5
        if state and source_state_hint and state == source_state_hint:
            score += 0.5
        if state == "CA" and not mentioned_states:
            score += 0.25
        out.append({
            "agency_id": entry["agency_id"],
            "display_name": agency_display_name(entry),
            "state": state,
            "score": round(score, 3),
            "matched_forms": sorted(slot["matched_forms"]),
        })
    out.sort(key=lambda c: (-c["score"], c["display_name"] or ""))
    return out[:top_n]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text-file", help="path to extracted article .txt")
    src.add_argument("--stdin", action="store_true", help="read text from stdin")
    p.add_argument("--source-domain", default=None,
                   help="article's publisher domain (helps geo scoring)")
    p.add_argument("--top", type=int, default=10,
                   help="max candidates to return (default 10)")
    args = p.parse_args()

    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8", errors="replace")
    else:
        text = sys.stdin.read()

    matches = lookup(text, source_domain=args.source_domain, top_n=args.top)
    print(json.dumps({"matches": matches}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
