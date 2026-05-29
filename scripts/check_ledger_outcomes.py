#!/usr/bin/env python3
"""Cross-check termination_ledger × article_registry on outcome:* tags.

For each (agency, action_type, source_url) tuple in
assets/termination_ledger.json, look up the URL in
assets/article_registry/ shards. Map action_type to expected outcome:*
tag(s) and report mismatches:

  - missing: article exists in registry but doesn't carry the expected
    outcome:* tag — curator drift, candidate for re-enrichment.
  - extra: article carries an outcome:* the action_type wouldn't
    predict — usually fine (article reports more than the ledger row),
    but listed for completeness.
  - unfetched: ledger source_url not in the registry yet — pending
    crawl.
  - unmapped: action_type doesn't match any of our pattern groups —
    extend the mapping.

Multiple expected outcomes per action are honest signal of a
transitional state ("Cameras turned off, contract to be terminated"
→ both outcome:restricted AND outcome:terminated). We don't collapse
them, and a registry entry only "matches" if it carries all expected
outcomes.

Usage:
  uv run python scripts/check_ledger_outcomes.py        # full report
  uv run python scripts/check_ledger_outcomes.py --json # machine-readable

Exit code is always 0 — this is a drift-monitoring tool, not a gate.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import article_store

LEDGER_PATH = ROOT / "assets" / "termination_ledger.json"


# action_type → expected outcome:* tag(s)
# ─────────────────────────────────────────────────────────────────────
# Mapping is substring-based (case-insensitive). Each pattern group
# applies independently; an action_type may match multiple groups (e.g.
# "Cameras turned off, contract to be terminated" hits both restricted
# and terminated patterns) — preserve that as transitional signal.
#
# Pre-vs-post-adoption disambiguation: "rejected" / "declined" /
# "not awarded" mean different things depending on whether a contract
# already existed. We treat "renewal" or "extension" in the action_type
# as a post-adoption marker → terminated, not rejected.

_TERMINATED_PATTERNS = (
    r"terminat",
    r"cancel",
    r"resci(?:nd|ss)",  # rescind/rescinded + rescission
    r"deactivat",
    r"\bremoved\b",
    r"\bremoval\b",
    r"not\s+renewed",
    r"\bexpire",                    # expired / expires / expiring
    r"allowed\s+to\s+expire",
    r"\bended\b",
    r"\bend\s+contract",
    r"chief\s+ended",
    r"renewal\s+rejected",
    r"renewal\s+declined",
    r"extension\s+rejected",
    r"won.t\s+renew",
    r"funding\s+removed",
)

_REJECTED_PATTERNS = (
    r"proposal\s+rejected",
    r"proposed\s+contract\s+declined",
    r"contract\s+proposal\s+rejected",
    r"rejected\s+contract\s+proposal",
    r"contract\s+not\s+awarded",
    r"not\s+awarded",
    r"proposal\s+declined",
)

_RESTRICTED_PATTERNS = (
    r"\bpaused\b",
    r"\bpause\b",
    r"\bsuspend",
    r"turned\s+off",
    r"shut\s+down",
    r"\bshutdown\b",
    r"\bdelayed\b",
    r"not\s+activated",
    r"prohibited",
    r"police\s+access\s+paused",
    r"council\s+ordered\s+deactivation",  # also matches deactivat above
    r"sheriff\s+suspends",
)


def map_action_to_outcomes(action_type: str) -> set[str]:
    """Return set of expected outcome:* tag IDs for an action_type.

    Returns empty set for unmapped strings — caller treats that as
    'unmapped' and reports it for vocabulary extension.
    """
    s = action_type.lower()
    out: set[str] = set()
    for pat in _TERMINATED_PATTERNS:
        if re.search(pat, s):
            out.add("outcome:terminated")
            break
    # Reject only fires when no renewal/extension marker is present —
    # those are post-adoption (contract existed) and belong to the
    # terminated bucket above.
    if "renewal" not in s and "extension" not in s:
        for pat in _REJECTED_PATTERNS:
            if re.search(pat, s):
                out.add("outcome:rejected")
                break
    for pat in _RESTRICTED_PATTERNS:
        if re.search(pat, s):
            out.add("outcome:restricted")
            break
    return out


# URL normalization for the ledger × registry join
# ─────────────────────────────────────────────────────────────────────
# Some sources tack on tracking params or trailing slashes between the
# ledger and the article fetch; normalize before comparing.

def normalize_url(u: str) -> str:
    p = urlparse(u.strip())
    # Strip trailing slashes (but preserve a single root "/").
    path = p.path.rstrip("/") or "/"
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Drop query + fragment for the purposes of the join. If we ever
    # need to keep a tracking-relevant query (eg. ?p=42 on WordPress
    # archives), revisit.
    return f"{p.scheme.lower() or 'https'}://{netloc}{path.lower()}"


def build_registry_index(registry: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for e in registry:
        u = e.get("url")
        if not u:
            continue
        idx[normalize_url(u)] = e
    return idx


# ─────────────────────────────────────────────────────────────────────


def run(*, as_json: bool) -> int:
    ledger = json.loads(LEDGER_PATH.read_text())
    registry = article_store.load_registry()
    actions = ledger.get("actions", [])
    idx = build_registry_index(registry)

    by_agency: dict[str, list[dict]] = defaultdict(list)
    unmapped: dict[str, int] = defaultdict(int)
    matched_count = 0
    expected_present_count = 0
    mismatch_count = 0
    unfetched_count = 0

    for action in actions:
        agency = action.get("agency_name", "?")
        state = action.get("state") or ""
        agency_key = f"{agency} ({state})" if state else agency
        atype = action.get("action_type", "")
        expected = map_action_to_outcomes(atype)
        if not expected:
            unmapped[atype] += 1

        for url in action.get("source_urls") or []:
            entry = idx.get(normalize_url(url))
            if entry is None:
                unfetched_count += 1
                by_agency[agency_key].append({
                    "kind": "unfetched",
                    "action_type": atype,
                    "expected": sorted(expected),
                    "url": url,
                })
                continue

            matched_count += 1
            tags = set(entry.get("tags") or [])
            article_outcomes = {t for t in tags if t.startswith("outcome:")}
            missing = expected - article_outcomes
            extra = article_outcomes - expected

            if expected and not missing:
                expected_present_count += 1
            if missing or extra:
                mismatch_count += 1
                by_agency[agency_key].append({
                    "kind": "mismatch",
                    "action_type": atype,
                    "expected": sorted(expected),
                    "found": sorted(article_outcomes),
                    "missing": sorted(missing),
                    "extra": sorted(extra),
                    "article_id": entry.get("article_id"),
                    "url": url,
                })

    summary = {
        "ledger_actions": len(actions),
        "registry_entries": len(registry),
        "matched": matched_count,
        "expected_outcome_present": expected_present_count,
        "mismatch": mismatch_count,
        "unfetched": unfetched_count,
        "unmapped_action_types": dict(unmapped),
    }

    if as_json:
        print(json.dumps({
            "summary": summary,
            "by_agency": dict(by_agency),
        }, indent=2))
        return 0

    # ─── human-readable report ─────────────────────────────────────
    print("check_ledger_outcomes — ledger × registry outcome:* tags")
    print()
    print(f"  ledger actions:         {summary['ledger_actions']}")
    print(f"  registry entries:       {summary['registry_entries']}")
    print(f"  source_urls matched:    {summary['matched']}")
    print(f"  expected outcome present: {summary['expected_outcome_present']}")
    print(f"  source_urls unfetched:  {summary['unfetched']}")
    print(f"  mismatches:             {summary['mismatch']}")
    print(f"  unmapped action_types:  {len(summary['unmapped_action_types'])}")
    print()

    mismatch_agencies = {
        a: [r for r in rows if r["kind"] == "mismatch"]
        for a, rows in by_agency.items()
    }
    mismatch_agencies = {a: rs for a, rs in mismatch_agencies.items() if rs}

    if mismatch_agencies:
        print("[outcome mismatches by agency]")
        for agency in sorted(mismatch_agencies):
            print(f"  {agency}")
            for r in mismatch_agencies[agency]:
                aid = r["article_id"] or "?"
                if r["missing"] and r["extra"]:
                    detail = (f"missing={r['missing']} extra={r['extra']}")
                elif r["missing"]:
                    detail = f"missing={r['missing']}"
                else:
                    detail = f"extra={r['extra']}"
                print(f"    {aid}  action={r['action_type']!r}")
                print(f"           expected={r['expected']} "
                      f"found={r['found']}")
                print(f"           {detail}")
                print(f"           {r['url']}")
        print()
    else:
        print("[outcome mismatches by agency]")
        print("  (none)")
        print()

    unfetched = {
        a: [r for r in rows if r["kind"] == "unfetched"]
        for a, rows in by_agency.items()
    }
    unfetched = {a: rs for a, rs in unfetched.items() if rs}
    if unfetched:
        print(f"[unfetched ledger source_urls — {summary['unfetched']} total]")
        for agency in sorted(unfetched):
            for r in unfetched[agency]:
                print(f"  {agency}  expected={r['expected']}")
                print(f"           {r['url']}")
        print()

    if summary["unmapped_action_types"]:
        print("[unmapped action_types — extend mapping in this script]")
        for atype, n in sorted(summary["unmapped_action_types"].items(),
                               key=lambda x: -x[1]):
            print(f"  {n:3d}  {atype!r}")
        print()

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of report")
    args = p.parse_args()
    return run(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
