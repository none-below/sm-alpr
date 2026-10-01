#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Pattern-driven detection of federal-law-enforcement references in ALPR
audit-log reason fields.

California Civil Code §1798.90.55(b), as read by the Attorney General's
Bulletin 2023-DLE-06 (Oct 27, 2023), limits ALPR-data sharing to
California public agencies as defined in §1798.90.5(f) — a definition the
AG says "does not include out-of-state or federal law enforcement
agencies." A California agency that runs an ALPR search whose reason text
names a federal LE agency (U.S. Marshals, FBI, DEA, ATF, DHS/ICE/CBP/HSI,
Secret Service) is, on the face of the reason, querying shared networks
for the benefit of an entity outside that definition.

Why patterns instead of a hardcoded agency list
------------------------------------------------
The flag is keyed on the REASON TEXT, not on a fixed roster of agencies.
That makes it self-extending: when a new agency starts entering
"USMS case" / "FBI assist" reasons in its published audit log, it is
picked up automatically on the next build — no edit to this file needed.
The hardcoded part is the *vocabulary* of federal-LE references we have
observed, not the list of offenders.

Calibration
-----------
Patterns are deliberately narrow (word-boundaried) so they don't false-
positive on look-alikes:
  - "Marshall" (a surname / place name) is NOT matched by the USMS
    pattern, which requires "USMS", "U.S. Marshal", or "Marshals Service".
  - "idea" / "dead" / "deal" are NOT matched by the DEA pattern (\bdea\b).
  - "service" / "police" / "notice" do NOT trip the "ice" fragment in the
    DHS group, which is bounded as \bice\b.
This set reproduces the hand-curated meeting-prep dataset
(meeting-prep/chief/share-federal-le/) with zero spurious agencies across
every published California audit log.

Trust note
----------
The reason field is officer-entered free text scraped from third-party
transparency portals. It is treated strictly as regex input here and is
never executed or interpreted. The audit logs this module reads
(docs/data/audit/<slug>.json, built by build_audit_log.py) are themselves
deterministic JSON, and are already published verbatim on the project's
Pages site — surfacing a sample reason adds no new exposure.

Out-of-state assists
--------------------
The same statute bars out-of-state sharing too, so a reason naming another
state's LE agency (e.g. "Assist Talent PD (Oregon)") is flagged as well.
A bare state-name scan is noisy — state names collide with street and
place names ("Washington Blvd") and with California places named after
states ("Nevada City") — so detection requires a non-CA state name beside
a law-enforcement token (PD / police / sheriff / SO / trooper / DPS). This
ignores "offender from out of state" phrasings (a suspect's origin, not a
data transfer). See OUT_OF_STATE_CATEGORY.

Note: this is distinct from an out-of-state *recipient* (an agency
physically in another state), which is detected from registry geotags
elsewhere — here the recipient is itself a California agency, but its
search's reason names an out-of-state agency.

CLI:
  uv run python scripts/federal_le_queries.py            # summary table
  uv run python scripts/federal_le_queries.py --json     # full index as JSON
"""

import argparse
import json
import re
import sys
from pathlib import Path

AUDIT_DIR = Path("docs/data/audit")

# Ordered list of federal-LE categories. `key` is the stable identifier,
# `label` is the human-facing name used in the report callout, `re` is the
# detection pattern (case-insensitive). Order is the first-match priority
# for classify_reason(); in practice the categories are disjoint in the
# observed data, so a row's total is never double-counted.
FEDERAL_LE_CATEGORIES = [
    {
        "key": "usms",
        "label": "U.S. Marshals",
        "re": re.compile(r"\busms\b|\bu\.?\s*s\.?\s*marshal|\bmarshals?\s+service\b", re.I),
    },
    {
        "key": "fbi",
        "label": "FBI",
        "re": re.compile(r"\bfbi\b", re.I),
    },
    {
        "key": "dea",
        "label": "DEA",
        "re": re.compile(r"\bdea\b", re.I),
    },
    {
        "key": "atf",
        "label": "ATF",
        "re": re.compile(r"\batf\b", re.I),
    },
    {
        "key": "dhs",
        "label": "DHS / ICE / CBP / HSI",
        "re": re.compile(
            r"\bdhs\b|\bhsi\b|\bcbp\b|\bice\b|homeland|border\s+patrol|\bcustoms\b", re.I
        ),
    },
    {
        "key": "usss",
        "label": "U.S. Secret Service",
        "re": re.compile(r"\busss\b|secret\s+service|\bpotus\b", re.I),
    },
]

# ── Out-of-state assist ──
# §1798.90.55(b) / AG Bulletin 2023-DLE-06 bar out-of-state sharing on the
# same footing as federal. Detecting it from the reason text is trickier
# than the federal acronyms: a bare state-name scan collides with street
# and place names ("Washington Blvd", "Virginia St") and with California
# places named after states ("Nevada City"). So we require a non-California
# state name to sit next to a law-enforcement token (PD / police / sheriff
# / SO / trooper / DPS), which is how an assist for another state's agency
# reads — e.g. "Assist Talent PD (Oregon)". "Nevada" is guarded against the
# CA "Nevada City" / "Nevada County". This deliberately ignores phrasings
# like "offender from out of state" (a suspect's origin, not a transfer of
# data to another state's agency). Validated to yield exactly the one
# observed assist (Manteca → Oregon) with zero false positives across all
# published CA audit logs; it remains self-extending for future rows.
_NON_CA_STATES = (
    r"Alabama|Alaska|Arizona|Arkansas|Colorado|Connecticut|Delaware|Florida|"
    r"Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|"
    r"Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|"
    r"Montana|Nebraska|Nevada(?!\s+(?:City|County))|New\s+Hampshire|"
    r"New\s+Jersey|New\s+Mexico|New\s+York|North\s+Carolina|North\s+Dakota|"
    r"Ohio|Oklahoma|Oregon|Pennsylvania|Rhode\s+Island|South\s+Carolina|"
    r"South\s+Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|"
    r"West\s+Virginia|Wisconsin|Wyoming"
)
_LE_TOKEN = r"PD|police|sheriff|deputies|trooper|troopers|DPS|state\s+police|SO"
_OOS_RE = re.compile(
    rf"(?:\b(?:{_LE_TOKEN})\b.{{0,25}}\b(?:{_NON_CA_STATES})\b)"
    rf"|(?:\b(?:{_NON_CA_STATES})\b.{{0,25}}\b(?:{_LE_TOKEN})\b)",
    re.I,
)
OUT_OF_STATE_CATEGORY = {
    "key": "oos",
    "label": "Out-of-state assist",
    "re": _OOS_RE,
}

# Federal categories take priority over out-of-state (a "FBI / Oregon PD"
# reason classifies as fbi). Out-of-state is checked last.
DETECT_CATEGORIES = FEDERAL_LE_CATEGORIES + [OUT_OF_STATE_CATEGORY]

_LABEL_BY_KEY = {c["key"]: c["label"] for c in DETECT_CATEGORIES}


def classify_reason(reason):
    """Return the category key of the first detection pattern that matches
    `reason`, or None. First-match priority follows DETECT_CATEGORIES order
    (federal acronyms first, out-of-state last)."""
    if not reason:
        return None
    text = str(reason)
    for cat in DETECT_CATEGORIES:
        if cat["re"].search(text):
            return cat["key"]
    return None


def scan_rows(rows, max_samples=2):
    """Scan a list of audit rows for federal-LE or out-of-state reason refs.

    Returns a summary dict, or None when nothing matches:

        {
          "total": 9,                       # rows matching any pattern
          "categories": [                   # sorted by count desc, then key
              {"key": "fbi",  "label": "FBI",            "count": 5},
              {"key": "usms", "label": "U.S. Marshals",  "count": 4},
          ],
          "samples": ["FBI ASSIST: BANK THEFT", "Usms case"],
          "date_min": "2026-03-04",
          "date_max": "2026-05-31",
        }

    `samples` holds up to `max_samples` distinct verbatim reason strings
    (truncated), for a concrete, verifiable callout.
    """
    counts = {}
    distinct_reasons = {}  # lower-cased -> original, dedup while preserving first form
    dates = []
    for r in rows or []:
        key = classify_reason(r.get("reason"))
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        sd = (r.get("searchDate") or "")[:10]
        if sd:
            dates.append(sd)
        reason = str(r.get("reason") or "").strip()
        if reason:
            distinct_reasons.setdefault(reason.lower(), reason)

    if not counts:
        return None

    # Prefer the shortest distinct reasons as samples. The shortest tend to
    # be generic ("Usms case", "dea inquiry") rather than longer free-text
    # that may embed a subject's name — surfaced text stays minimal even
    # though the underlying audit log is already public.
    samples = [
        s if len(s) <= 80 else s[:77] + "…"
        for s in sorted(distinct_reasons.values(), key=lambda s: (len(s), s.lower()))[:max_samples]
    ]

    categories = sorted(
        ({"key": k, "label": _LABEL_BY_KEY[k], "count": n} for k, n in counts.items()),
        key=lambda c: (-c["count"], c["key"]),
    )
    return {
        "total": sum(counts.values()),
        "categories": categories,
        "samples": samples,
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
    }


def build_index(audit_dir=AUDIT_DIR, key_by=None, max_samples=2):
    """Scan every audit log under `audit_dir` and return a per-agency index
    of federal-LE query activity.

    `key_by`: optional {slug: agency_id} map. When provided, the returned
    dict is keyed by agency_id (slugs absent from the map fall back to the
    slug); otherwise it is keyed by slug. Each value is the scan_rows()
    summary with the source `slug` attached. Agencies with no matches are
    omitted.
    """
    audit_dir = Path(audit_dir)
    index = {}
    if not audit_dir.is_dir():
        return index
    for path in sorted(audit_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        slug = data.get("portal") or path.stem
        summary = scan_rows(data.get("rows") or [], max_samples=max_samples)
        if not summary:
            continue
        summary["slug"] = slug
        key = (key_by or {}).get(slug, slug)
        index[key] = summary
    return index


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audit-dir", default=str(AUDIT_DIR),
                    help="directory of <slug>.json audit logs (default: %(default)s)")
    ap.add_argument("--json", action="store_true",
                    help="emit the full index as JSON instead of a table")
    args = ap.parse_args()

    index = build_index(args.audit_dir)
    if args.json:
        print(json.dumps(index, indent=2, sort_keys=True))
        return 0

    if not index:
        print("no federal-LE references found", file=sys.stderr)
        return 0

    rows = sorted(index.values(), key=lambda s: (-s["total"], s["slug"]))
    total_rows = sum(s["total"] for s in rows)
    print(f"{len(rows)} agencies with federal-LE query references "
          f"({total_rows} rows total)\n")
    for s in rows:
        cats = ", ".join(f"{c['count']} {c['label']}" for c in s["categories"])
        window = f"{s['date_min']}..{s['date_max']}" if s["date_min"] else "—"
        print(f"  {s['slug']:<28} {s['total']:>4}  [{cats}]  {window}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
