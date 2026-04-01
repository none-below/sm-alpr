#!/usr/bin/env python3
"""
Classify all sharing recipients from Flock transparency portal data.

For each unique recipient across all archived portals, determines:
  1. Public vs private entity
  2. California vs out-of-state
  3. Scope (city, county, state, federal, university, tribal, other)
  4. Role (police, sheriff, da, fire, parks, corrections, campus_safety, other)

Usage:
  uv run python scripts/classify_agencies.py
  uv run python scripts/classify_agencies.py --json --out outputs/agency_classifications.json
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

DEFAULT_DATA_DIR = Path("assets/transparency.flocksafety.com")

# ── State indicators ──

# Two-letter state codes used in Flock names (e.g. "Anaheim CA PD", "Round Rock TX PD")
ALL_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}

CA_PATTERNS = re.compile(
    r"\bCA\b|California|Cal Fire|Cal State|NCRIC|Cal Poly",
    re.IGNORECASE,
)

# Match "XX PD", "XX SO", etc. where XX is a non-CA state code
NON_CA_STATE_RE = re.compile(
    r"\b(" + "|".join(s for s in ALL_STATES if s != "CA") + r")\b"
)

# ── Known private institutions ──

KNOWN_PRIVATE_UNIVERSITIES = [
    "University of the Pacific",
    "Stanford University",
    "University of San Francisco",
    "Santa Clara University",
    "Chapman University",
    "Azusa Pacific",
    "Loyola Marymount",
    "Pepperdine",
    "USC",
    "University of Southern California",
    "Biola University",
    "Point Loma Nazarene",
    "Whittier College",
    "Claremont McKenna",
    "Pomona College",
    "Harvey Mudd",
    "Scripps College",
    "Occidental College",
    "University of Redlands",
    "University of La Verne",
    "Woodbury University",
]

KNOWN_PRIVATE_RE = re.compile(
    "|".join(re.escape(n) for n in KNOWN_PRIVATE_UNIVERSITIES),
    re.IGNORECASE,
)

# ── Public university systems ──

PUBLIC_UNI_PATTERNS = re.compile(
    r"\b(UC |University of California|Cal State|CSU|Cal Poly"
    r"|San Jose State|San Diego State|San Francisco State"
    r"|Sacramento State|Fresno State|Long Beach State"
    r"|Fullerton|Northridge|Pomona|Bakersfield|Chico|Stanislaus"
    r"|Humboldt|Sonoma State|Maritime Academy"
    r"|Community College|Foothill.DeAnza|Rio Hondo|Cerritos"
    r"|Sequoias|San Joaquin Delta|West Valley Mission"
    r"|Napa Valley College|Chabot College"
    r"|San Jose.Evergreen|San Bernardino Community College"
    r"|Riverside Community College)\b",
    re.IGNORECASE,
)

# ── Role detection ──

ROLE_PATTERNS = [
    ("police", re.compile(r"\bP\.?D\.?\b|Police\b|Public Safety\b", re.IGNORECASE)),
    ("sheriff", re.compile(r"\bS\.?O\.?\b|S\.?D\.?\b|Sheriff", re.IGNORECASE)),
    ("da", re.compile(r"\bD\.?A\.?\b|District Attorney|Attorney.s Office", re.IGNORECASE)),
    ("highway_patrol", re.compile(r"Highway Patrol|CHP\b", re.IGNORECASE)),
    ("state_parks", re.compile(r"State Parks\b", re.IGNORECASE)),
    ("fire", re.compile(r"\bFire\b|FD\b", re.IGNORECASE)),
    ("parks", re.compile(r"Parks Department|Parks Police|Park Ranger|Port Police", re.IGNORECASE)),
    ("corrections", re.compile(r"Correction|Probation|Parole", re.IGNORECASE)),
    ("campus_safety", re.compile(r"Campus|College.*PD|University.*PD|University.*Police", re.IGNORECASE)),
    ("marshal", re.compile(r"Marshal\b", re.IGNORECASE)),
    ("fish_wildlife", re.compile(r"Fish.*Wildlife|Game Warden", re.IGNORECASE)),
    ("tribal", re.compile(r"Tribal|Rancheria|Reservation", re.IGNORECASE)),
    ("intelligence", re.compile(r"Intelligence|NCRIC|Fusion Center", re.IGNORECASE)),
]

# ── Scope detection ──

SCOPE_PATTERNS = [
    ("federal", re.compile(r"\bFBI\b|Federal\b|US Marshal|DEA\b|ATF\b|ICE\b|CBP\b|Secret Service", re.IGNORECASE)),
    ("state", re.compile(r"California Highway Patrol|California State|Cal Fire|State Parks|State Police|Department of Corrections|Fish.*Wildlife|NCRIC|Attorney General", re.IGNORECASE)),
    ("county", re.compile(r"County\b|Sheriff|SO\b|SD\b|\bDA\b|District Attorney", re.IGNORECASE)),
    ("university", re.compile(r"University|College|Campus|Cal State|UC ", re.IGNORECASE)),
    ("tribal", re.compile(r"Tribal|Rancheria", re.IGNORECASE)),
    ("city", re.compile(r"PD\b|Police\b|City of|Town of|Public Safety", re.IGNORECASE)),
]


def classify(name):
    """Classify a single agency/organization name."""
    result = {
        "name": name,
        "is_california": False,
        "is_out_of_state": False,
        "is_public": True,  # default assumption
        "is_private": False,
        "scope": "unknown",
        "role": "unknown",
        "flags": [],
    }

    # ── California vs out-of-state ──
    if CA_PATTERNS.search(name):
        result["is_california"] = True

    # Check for non-CA state codes
    # Skip codes that are commonly used as role abbreviations in CA names:
    #   SD = Sheriff's Department, CO = County, SO = Sheriff's Office,
    #   DA = District Attorney, AL = (part of names like "Palo Alto")
    # Only flag as out-of-state if the name does NOT also contain "CA" or "California"
    has_ca = bool(CA_PATTERNS.search(name))
    non_ca_match = NON_CA_STATE_RE.search(name)
    if non_ca_match and not has_ca:
        code = non_ca_match.group(1)
        before = name[:non_ca_match.start()]
        after = name[non_ca_match.end():]
        if (before == "" or before[-1] in " -,(") and (after == "" or after[0] in " -,)"):
            result["is_out_of_state"] = True
            result["is_california"] = False
            result["flags"].append(f"state_code:{code}")

    # If no state indicator found at all, mark as unknown location
    if not result["is_california"] and not result["is_out_of_state"]:
        # Check for California-specific names without explicit CA
        if any(x in name for x in ["NCRIC", "Cal Fire", "California"]):
            result["is_california"] = True
        else:
            result["flags"].append("location_unclear")

    # ── Public vs private ──
    if KNOWN_PRIVATE_RE.search(name):
        result["is_private"] = True
        result["is_public"] = False
        result["flags"].append("PRIVATE_INSTITUTION")

    # ── Scope ──
    for scope, pattern in SCOPE_PATTERNS:
        if pattern.search(name):
            result["scope"] = scope
            break

    # ── Role ──
    for role, pattern in ROLE_PATTERNS:
        if pattern.search(name):
            result["role"] = role
            break

    # ── Special cases ──
    # University scope + check public vs private
    if result["scope"] == "university" and not result["is_private"]:
        if PUBLIC_UNI_PATTERNS.search(name):
            result["is_public"] = True
        else:
            # Unknown university — flag for review
            result["flags"].append("UNIVERSITY_PUBLIC_STATUS_UNKNOWN")

    # Fire authority is not law enforcement
    if result["role"] == "fire":
        result["flags"].append("NON_LAW_ENFORCEMENT")

    return result


def main():
    parser = argparse.ArgumentParser(description="Classify Flock sharing recipients")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    # Collect all unique org names and which agencies share with them
    org_sources = {}  # name -> set of agencies that share with this org
    for slug_dir in sorted(args.data_dir.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("."):
            continue
        jsons = sorted(slug_dir.glob("*.json"))
        if not jsons:
            continue
        data = json.loads(jsons[-1].read_text())
        source_slug = slug_dir.name
        for org_name in data.get("shared_org_names", []):
            org_sources.setdefault(org_name, set()).add(source_slug)

    # Classify
    classifications = []
    for name in sorted(org_sources.keys()):
        c = classify(name)
        c["shared_by_count"] = len(org_sources[name])
        c["shared_by"] = sorted(org_sources[name])
        classifications.append(c)

    # Stats
    total = len(classifications)
    ca_count = sum(1 for c in classifications if c["is_california"])
    oos_count = sum(1 for c in classifications if c["is_out_of_state"])
    unclear_count = sum(1 for c in classifications if "location_unclear" in c["flags"])
    private_count = sum(1 for c in classifications if c["is_private"])
    flagged = [c for c in classifications if c["flags"]]

    scope_counts = Counter(c["scope"] for c in classifications)
    role_counts = Counter(c["role"] for c in classifications)

    results = {
        "summary": {
            "total_unique_recipients": total,
            "california": ca_count,
            "out_of_state": oos_count,
            "location_unclear": unclear_count,
            "private_entities": private_count,
            "scopes": dict(scope_counts.most_common()),
            "roles": dict(role_counts.most_common()),
        },
        "classifications": classifications,
        "flagged": flagged,
    }

    if args.json_output:
        # Convert sets to lists for JSON
        print(json.dumps(results, indent=2, default=list))
    else:
        _print_report(results)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2, default=list) + "\n")
        print(f"\nJSON written to {args.out}")


def _print_report(r):
    s = r["summary"]
    print(f"{'=' * 70}")
    print(f"AGENCY CLASSIFICATION REPORT")
    print(f"{'=' * 70}")
    print(f"\n  Total unique recipients: {s['total_unique_recipients']}")
    print(f"  California:              {s['california']}")
    print(f"  Out-of-state:            {s['out_of_state']}")
    print(f"  Location unclear:        {s['location_unclear']}")
    print(f"  Private entities:        {s['private_entities']}")

    print(f"\n  Scope breakdown:")
    for scope, count in sorted(s["scopes"].items(), key=lambda x: -x[1]):
        print(f"    {scope:<20} {count:>4}")

    print(f"\n  Role breakdown:")
    for role, count in sorted(s["roles"].items(), key=lambda x: -x[1]):
        print(f"    {role:<20} {count:>4}")

    # ── Private entities ──
    private = [c for c in r["classifications"] if c["is_private"]]
    if private:
        print(f"\n{'─' * 70}")
        print(f"PRIVATE ENTITIES ({len(private)})")
        print(f"  CA Civil Code §1798.90.55(b): sharing restricted to public agencies\n")
        for c in sorted(private, key=lambda x: -x["shared_by_count"]):
            print(f"  {c['name']}")
            print(f"    scope: {c['scope']}, role: {c['role']}")
            print(f"    shared by {c['shared_by_count']} agencies")
            print()

    # ── Out-of-state ──
    oos = [c for c in r["classifications"] if c["is_out_of_state"]]
    if oos:
        print(f"{'─' * 70}")
        print(f"OUT-OF-STATE ENTITIES ({len(oos)})\n")
        for c in sorted(oos, key=lambda x: -x["shared_by_count"]):
            state = [f for f in c["flags"] if f.startswith("state_code:")][0].split(":")[1] if any(f.startswith("state_code:") for f in c["flags"]) else "?"
            print(f"  {c['name']}  [{state}]")
            print(f"    scope: {c['scope']}, role: {c['role']}")
            print(f"    shared by {c['shared_by_count']} agencies")
            print()

    # ── Unknown public status universities ──
    unknown_uni = [c for c in r["classifications"] if "UNIVERSITY_PUBLIC_STATUS_UNKNOWN" in c["flags"]]
    if unknown_uni:
        print(f"{'─' * 70}")
        print(f"UNIVERSITIES — PUBLIC STATUS UNKNOWN ({len(unknown_uni)})")
        print(f"  Not matched as known public or private — needs manual review\n")
        for c in sorted(unknown_uni, key=lambda x: -x["shared_by_count"]):
            print(f"  {c['name']}")
            print(f"    shared by {c['shared_by_count']} agencies")
            print()

    # ── Non-law enforcement ──
    non_le = [c for c in r["classifications"] if "NON_LAW_ENFORCEMENT" in c["flags"]]
    if non_le:
        print(f"{'─' * 70}")
        print(f"NON-LAW-ENFORCEMENT ENTITIES ({len(non_le)})\n")
        for c in sorted(non_le, key=lambda x: -x["shared_by_count"]):
            print(f"  {c['name']}")
            print(f"    scope: {c['scope']}, role: {c['role']}")
            print(f"    shared by {c['shared_by_count']} agencies")
            print()

    # ── Location unclear ──
    unclear = [c for c in r["classifications"] if "location_unclear" in c["flags"]
               and not c["is_private"] and "UNIVERSITY_PUBLIC_STATUS_UNKNOWN" not in c["flags"]]
    if unclear:
        print(f"{'─' * 70}")
        print(f"LOCATION UNCLEAR ({len(unclear)})")
        print(f"  No CA or state code detected — needs manual review\n")
        for c in sorted(unclear, key=lambda x: -x["shared_by_count"])[:30]:
            print(f"  {c['name']}")
            print(f"    scope: {c['scope']}, role: {c['role']}")
            print()
        if len(unclear) > 30:
            print(f"  ... and {len(unclear) - 30} more")

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    main()
