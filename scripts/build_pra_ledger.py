#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Build the per-PRA conduct ledger: how the agency processed each request,
reported against the CPRA's processing framework.

Reads docs/data/pra_registry.json (run build_pra_registry.py first) plus git
history over assets/san-mateo-public-records/ (for production-file receipt
dates). Writes docs/data/pra_ledger.json (build artifact, gitignored).

Everything is derived deterministically from the parsed portal record — no
LLM. Per PRA:
  - first substantive portal response: date, days from filing, measured
    against the Gov. Code § 7922.535(a) 10-day determination window and the
    § 7922.535(b) 14-day extension ceiling (24 days total)
  - promise conduct: distinct "no later than" dates, extension count, and
    total slip between the first and final promised date
  - empty updates: substantive agency messages that neither produce records
    nor answer anything ("no responsive records at this time", bare promise
    bumps), with the longest consecutive streak
  - corpus routing: messages folding the request into the W012462
    @flocksafety.com email-corpus review
  - exemptions invoked: which CPRA / Civil Code / Evidence Code provisions
    the agency's own text cites (requester text echoed back is excluded)
  - production: file counts and first/last receipt dates from git history
  - contradiction links: curated prior_pra_refs whose relation starts with
    "contradicts" (a later production contradicting an earlier "no records"
    answer), surfaced in both directions

Method notes (also embedded in the artifact for the UI):
  - The ledger reports the portal message record. A response outside the
    portal would not appear here.
  - "First substantive response" is a fact about timing; whether a given
    message was a § 7922.535 determination is a legal characterization the
    ledger does not make.
  - Production dates are the dates files first reached this repository,
    which lag the portal's release by the scrape cadence.

Usage:
  uv run python scripts/build_pra_ledger.py             # write JSON artifact
  uv run python scripts/build_pra_ledger.py --markdown  # scorecard to stdout
"""

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_pra_productivity import (  # noqa: E402
    PORTAL_AUTO_ACK_RE,
    PRA_FOLDER_RE,
    git_file_add_dates,
    is_production_file,
)
from build_pra_registry import (  # noqa: E402
    CLOSED_RE,
    PROMISED_MONTHNAME_RE,
    PROMISED_NUMERIC_RE,
)

REGISTRY_PATH = Path("docs/data/pra_registry.json")
OUTPUT_PATH = Path("docs/data/pra_ledger.json")
ASSETS_ROOT = Path("assets/san-mateo-public-records")

# Portal exports use non-breaking spaces inside phrases; \s does not match
# \xa0 by default (same workaround as build_pra_productivity).
WS = r"[\s\xa0]"

# Both orderings appear in SMPD responses: "no responsive records" and
# "no records responsive to your request".
NO_RECORDS_RE = re.compile(
    r"no" + WS + r"+(?:responsive" + WS + r"+records|records" + WS + r"+responsive)",
    re.IGNORECASE,
)
PRODUCED_HINT_RE = re.compile(
    r"please" + WS + r"+see|attached|enclosed|releasing" + WS + r"+records|"
    r"please" + WS + r"+find|providing" + WS + r"+the" + WS + r"+following|"
    r"have" + WS + r"+been" + WS + r"+(?:uploaded|released|posted" + WS + r"+to" + WS + r"+the" + WS + r"+portal)",
    re.IGNORECASE,
)
CORPUS_RE = re.compile(
    r"W012462|211,?872|encompassed" + WS + r"+by",
    re.IGNORECASE,
)

# Exemption / withholding authorities, matched against the agency's own text
# only (echoed requester text is excluded — requests often pre-rebut
# exemptions, and counting those would credit the agency with the
# requester's citations).
EXEMPTIONS = [
    ("gov_7923_600", "Gov. Code § 7923.600 (investigatory records)",
     re.compile(r"7923\.600")),
    ("civ_1798_90_55", "Civ. Code § 1798.90.55 (ALPR sharing restriction)",
     re.compile(r"1798\.90\.55")),
    ("atty_client", "Attorney-client privilege (Gov. Code § 7927.705)",
     re.compile(r"attorney[-\s\xa0/]?client|7927\.705", re.IGNORECASE)),
    ("prelim_drafts", "Gov. Code § 7927.500 (preliminary drafts)",
     re.compile(r"7927\.500")),
    ("personnel", "Gov. Code § 7927.700 (personnel records)",
     re.compile(r"7927\.700")),
    ("public_interest", "Gov. Code § 7922.000 (public-interest balancing)",
     re.compile(r"7922\.000")),
    ("evid_1040", "Evid. Code § 1040 (official information)",
     re.compile(r"evidence" + WS + r"+code[^0-9]{0,20}1040|§" + WS + r"*1040\b",
                re.IGNORECASE)),
]

METHOD_NOTES = [
    "Derived from the portal Message_History record; agency communications "
    "outside the portal would not appear here.",
    "“First substantive response” reports timing only; the ledger does not "
    "characterize whether any message satisfied the § 7922.535 determination "
    "requirement.",
    "Production dates are the dates files first reached this repository, "
    "which lag the portal's release by the scrape cadence.",
    "Exemption counts include only the agency's own message text; requester "
    "text echoed back by the agency is excluded.",
]


def agency_text(msg):
    """The agency's own words in a message: non-echo body segments when the
    registry segmented the body, else the whole body."""
    segments = msg.get("body_segments") or []
    own = [s["text"] for s in segments if s.get("type") != "echo"]
    if own:
        return "\n".join(own)
    return msg.get("body") or ""


def has_promise(text):
    return bool(PROMISED_NUMERIC_RE.search(text) or PROMISED_MONTHNAME_RE.search(text))


def classify_agency_message(text):
    """Conduct type for one substantive agency message.

    Priority: production beats everything (a message releasing records is
    productive whatever else it says); corpus routing beats closure so that
    “closed into the W012462 corpus” reads as routing; an explicit
    no-records answer or a bare promise bump with nothing else is an empty
    update; the rest is “other” (substantive answers, Q&A).
    """
    if PRODUCED_HINT_RE.search(text):
        return "production"
    if CORPUS_RE.search(text):
        return "corpus_routed"
    if CLOSED_RE.search(text):
        return "closure"
    if NO_RECORDS_RE.search(text) or has_promise(text):
        return "empty_update"
    return "other"


def extract_exemptions(text):
    """Exemption keys cited in one message's agency text."""
    return [key for key, _label, rx in EXEMPTIONS if rx.search(text)]


def max_streak(kinds, counted=("empty_update", "corpus_routed")):
    """Longest consecutive run of empty-handed messages."""
    best = run = 0
    for k in kinds:
        if k in counted:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def substantive_messages(messages):
    return [
        m for m in messages
        if m["sender_role"] == "agency"
        and not PORTAL_AUTO_ACK_RE.search(m["body"])
    ]


def days_between(a_iso, b_iso):
    return (date.fromisoformat(b_iso) - date.fromisoformat(a_iso)).days


def production_by_pra(file_dates):
    """pra_id -> sorted list of ISO dates of production files received."""
    out = {}
    for path, iso in file_dates.items():
        parts = Path(path).parts
        if len(parts) < 3 or parts[1] != "san-mateo-public-records":
            continue
        pra_id = parts[2]
        if not PRA_FOLDER_RE.match(pra_id):
            continue
        if not is_production_file(Path(path).name):
            continue
        out.setdefault(pra_id, []).append(iso)
    for dates in out.values():
        dates.sort()
    return out


def contradiction_refs(refs):
    """prior_pra_refs / cited_by entries whose relation marks a contradiction."""
    out = []
    for ref in refs or []:
        if isinstance(ref, dict) and str(ref.get("relation", "")).startswith("contradicts"):
            out.append({"ref": ref.get("ref"), "note": ref.get("note", "")})
    return out


def conduct_entry(pra, prod_dates, today_iso):
    derived = pra["derived"]
    filed = derived.get("filed_date")
    messages = derived.get("messages") or []
    subs = substantive_messages(messages)

    first = None
    if subs and filed:
        first_date = subs[0]["ts"][:10]
        days = days_between(filed, first_date)
        first = {
            "date": first_date,
            "days": days,
            "past_10day": days > 10,
            "past_24day": days > 24,
        }

    kinds = []
    exemption_hits = {}  # key -> {count, first_cited}
    corpus = {"count": 0, "first_date": None}
    closed_date = None
    for m in subs:
        text = agency_text(m)
        kind = classify_agency_message(text)
        kinds.append(kind)
        msg_date = m["ts"][:10]
        if kind == "corpus_routed":
            corpus["count"] += 1
            corpus["first_date"] = corpus["first_date"] or msg_date
        if CLOSED_RE.search(text):
            closed_date = msg_date
        for key in extract_exemptions(text):
            hit = exemption_hits.setdefault(key, {"count": 0, "first_cited": msg_date})
            hit["count"] += 1

    promises = derived.get("promise_history") or []
    slip = 0
    if len(promises) > 1:
        slip = days_between(promises[0]["promise_date"], promises[-1]["promise_date"])

    status = pra.get("display_status", derived.get("status"))
    # A request can be closed without the canonical closure phrase ever
    # appearing (curated status_override, or a withdrawal by the requester).
    # Its record then ends at its last message — counting to "today" would
    # overstate days_open for every such request.
    end_date = None
    if status in ("closed", "withdrawn"):
        last_message_date = messages[-1]["ts"][:10] if messages else None
        end_date = closed_date or last_message_date
    days_open = days_between(filed, end_date or today_iso) if filed else None

    labels = dict((k, lbl) for k, lbl, _rx in EXEMPTIONS)
    exemptions = [
        {"key": k, "label": labels[k], **v}
        for k, v in sorted(exemption_hits.items(),
                           key=lambda kv: kv[1]["first_cited"])
    ]

    contradicted_by = contradiction_refs(derived.get("cited_by"))
    contradicts = contradiction_refs(pra.get("curated", {}).get("prior_pra_refs"))

    empty_count = sum(1 for k in kinds if k == "empty_update")

    flags = []
    if first and first["past_24day"]:
        flags.append("first_response_past_24_days")
    elif first and first["past_10day"]:
        flags.append("first_response_past_10_days")
    if not first and filed and days_between(filed, today_iso) > 10 \
            and status not in ("closed", "withdrawn"):
        flags.append("no_substantive_response_past_10_days")
    if derived.get("extension_count", 0) >= 2:
        flags.append("repeated_promise_extensions")
    if corpus["count"]:
        flags.append("corpus_routed")
    if contradicted_by:
        flags.append("denial_contradicted_by_later_production")
    if days_open is not None and days_open > 60 and status not in ("closed", "withdrawn"):
        flags.append("open_over_60_days")

    return {
        "id": pra["id"],
        "title": pra.get("curated", {}).get("title", ""),
        "status": status,
        "filed_date": filed,
        "first_response": first,
        "promises": {
            "count": len(promises),
            "extensions": derived.get("extension_count", 0),
            "first_date": promises[0]["promise_date"] if promises else None,
            "final_date": promises[-1]["promise_date"] if promises else None,
            "slip_days": slip,
            "dates": [p["promise_date"] for p in promises],
        },
        "messages_substantive": len(subs),
        "empty_updates": {"count": empty_count, "max_streak": max_streak(kinds)},
        "corpus_routing": corpus if corpus["count"] else None,
        "exemptions": exemptions,
        "production": {
            "files": len(prod_dates),
            "first_date": prod_dates[0] if prod_dates else None,
            "last_date": prod_dates[-1] if prod_dates else None,
            "dates": sorted(set(prod_dates)),
        },
        "closed_date": closed_date,
        "end_date": end_date,
        "days_open": days_open,
        "contradictions": {
            "contradicted_by": contradicted_by,
            "contradicts": contradicts,
        },
        "flags": flags,
    }


def aggregate(entries):
    labels = dict((k, lbl) for k, lbl, _rx in EXEMPTIONS)
    responded = [e for e in entries if e["first_response"]]
    tally = {}
    for e in entries:
        for ex in e["exemptions"]:
            t = tally.setdefault(
                ex["key"],
                {"pra_count": 0, "first_cited": ex["first_cited"], "pras": []})
            t["pra_count"] += 1
            t["first_cited"] = min(t["first_cited"], ex["first_cited"])
            t["pras"].append(e["id"])
    return {
        "pra_count": len(entries),
        "first_response": {
            "within_10day": sum(1 for e in responded if not e["first_response"]["past_10day"]),
            "past_10day": sum(1 for e in responded if e["first_response"]["past_10day"]),
            "past_24day": sum(1 for e in responded if e["first_response"]["past_24day"]),
            "none_yet": len(entries) - len(responded),
        },
        "extensions_total": sum(e["promises"]["extensions"] for e in entries),
        "slip_days_total": sum(e["promises"]["slip_days"] for e in entries),
        "empty_updates_total": sum(e["empty_updates"]["count"] for e in entries),
        "corpus_routed": [e["id"] for e in entries if e["corpus_routing"]],
        "contradicted_denials": [
            {"id": e["id"],
             "contradicted_by": [c["ref"] for c in e["contradictions"]["contradicted_by"]]}
            for e in entries if e["contradictions"]["contradicted_by"]
        ],
        "exemption_tally": [
            {"key": k, "label": labels[k], **v}
            for k, v in sorted(tally.items(), key=lambda kv: -kv[1]["pra_count"])
        ],
        "open_aging": sorted(
            ({"id": e["id"], "days_open": e["days_open"]}
             for e in entries
             if e["status"] not in ("closed", "withdrawn") and e["days_open"] is not None),
            key=lambda x: -x["days_open"],
        ),
    }


def build():
    if not REGISTRY_PATH.exists():
        print(f"missing {REGISTRY_PATH} — run build_pra_registry.py first",
              file=sys.stderr)
        sys.exit(1)
    registry = json.loads(REGISTRY_PATH.read_text())
    today_iso = date.today().isoformat()
    prod = production_by_pra(git_file_add_dates(ASSETS_ROOT))

    entries = [
        conduct_entry(pra, prod.get(pra["id"], []), today_iso)
        for pra in registry["pras"]
    ]
    entries.sort(key=lambda e: (e["filed_date"] or "9999", e["id"]))

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method_notes": METHOD_NOTES,
        "pras": entries,
        "aggregate": aggregate(entries),
    }


def fmt_first_response(e):
    fr = e["first_response"]
    if not fr:
        return "—"
    flag = " ⚠24+" if fr["past_24day"] else (" ⚠10+" if fr["past_10day"] else "")
    return f"{fr['days']}d{flag}"


def to_markdown(ledger):
    lines = [
        "| PRA | Filed | First resp. | Ext. | Slip (d) | Empty upd. | Exemptions | Files | Status | Days open |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for e in ledger["pras"]:
        ex = ", ".join(x["key"] for x in e["exemptions"]) or "—"
        corpus = " ➜corpus" if e["corpus_routing"] else ""
        lines.append(
            f"| {e['id']} | {e['filed_date'] or '—'} | {fmt_first_response(e)} "
            f"| {e['promises']['extensions']} | {e['promises']['slip_days']} "
            f"| {e['empty_updates']['count']}{corpus} | {ex} "
            f"| {e['production']['files']} | {e['status']} | {e['days_open'] if e['days_open'] is not None else '—'} |"
        )
    agg = ledger["aggregate"]
    fr = agg["first_response"]
    lines += [
        "",
        f"- First substantive response within 10 days: {fr['within_10day']} of "
        f"{fr['within_10day'] + fr['past_10day']} responded "
        f"({fr['past_10day']} past 10 days, {fr['past_24day']} past 24; "
        f"{fr['none_yet']} with no substantive response yet)",
        f"- Promise extensions: {agg['extensions_total']} totaling {agg['slip_days_total']} slipped days",
        f"- Empty updates: {agg['empty_updates_total']}",
        f"- Routed into the W012462 corpus: {', '.join(agg['corpus_routed']) or 'none'}",
        f"- Denials contradicted by later production: "
        + ("; ".join(f"{c['id']} ⇐ {', '.join(c['contradicted_by'])}"
                     for c in agg["contradicted_denials"]) or "none"),
    ]
    for ex in agg["exemption_tally"]:
        lines.append(
            f"- {ex['label']}: {ex['pra_count']} PRA"
            f"{'s' if ex['pra_count'] != 1 else ''} "
            f"(first {ex['first_cited']}) — {', '.join(ex['pras'])}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--markdown", action="store_true",
                    help="Print the scorecard as a markdown table instead of "
                         "writing the JSON artifact")
    args = ap.parse_args()

    ledger = build()
    if args.markdown:
        print(to_markdown(ledger))
        return
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(ledger, indent=2) + "\n")
    agg = ledger["aggregate"]
    print(f"wrote {OUTPUT_PATH}: {agg['pra_count']} PRAs, "
          f"{agg['first_response']['past_10day']} past the 10-day window, "
          f"{agg['extensions_total']} extensions, "
          f"{agg['empty_updates_total']} empty updates, "
          f"{len(agg['corpus_routed'])} corpus-routed")


if __name__ == "__main__":
    main()
