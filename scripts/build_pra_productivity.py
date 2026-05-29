#!/usr/bin/env python3
"""
Build a per-week timeline of San Mateo PD's actual PRA throughput.

For each ISO week (Monday–Sunday), aggregate:
  - Files first committed to assets/san-mateo-public-records/<pra>/ that week,
    grouped by PRA. These approximate the documents SMPD produced and we
    received from the portal.
  - Substantive agency messages on each PRA's portal thread that week,
    excluding the boilerplate "thank you for your interest" auto-ack.

Reads:
  docs/data/pra_registry.json  (run scripts/build_pra_registry.py first)
  git log over assets/san-mateo-public-records/

Writes:
  docs/data/pra_productivity.json (build artifact, gitignored)

Used by docs/pras.html to render a productivity-vs-claimed-hours panel.
"""

import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

REGISTRY_PATH = Path("docs/data/pra_registry.json")
OUTPUT_PATH = Path("docs/data/pra_productivity.json")
ASSETS_ROOT = Path("assets/san-mateo-public-records")

SIDECAR_RE = re.compile(r"\.[0-9a-f]{8}\.(?:txt|pdf)$")
MESSAGE_HISTORY_RE = re.compile(r"_Message_History\.pdf$")
SKIP_NAMES = {"metadata.json", "audit_rows.json"}
PORTAL_AUTO_ACK_RE = re.compile(
    r"Thank you for your interest in public records of the City of San Mateo",
    re.IGNORECASE,
)
PRA_FOLDER_RE = re.compile(r"^W\d{6}-\d{6}$")

# SMPD's portal exports use non-breaking spaces (U+00A00xa0) inside disposition
# phrases ("No\xa0responsive\xa0records"). \s in Python's re does not match \xa0
# by default, so we use [\s\xa0] explicitly wherever a phrase straddles a space.
WS = r"[\s\xa0]"
ITEM_HEAD_RE = re.compile(r"(?m)^(\d+)\." + WS + r"+\S")
NO_RECORDS_RE = re.compile(r"no" + WS + r"responsive" + WS + r"records", re.IGNORECASE)
WITHHELD_RE = re.compile(
    r"§" + WS + r"*\d{4}\.\d+|attorney[- ]client|withhold|exempt(?:\b|ion|ed)|"
    r"privilege",
    re.IGNORECASE,
)
REDIRECTED_RE = re.compile(
    r"previously" + WS + r"(?:provided|produced|submitted)|"
    r"posted" + WS + r"publicly|transparency" + WS + r"portal|"
    r"on" + WS + r"the" + WS + r"City['’]s" + WS + r"website",
    re.IGNORECASE,
)
PRODUCED_HINT_RE = re.compile(
    r"please" + WS + r"see|attached|enclosed|releasing" + WS + r"records|"
    r"please" + WS + r"find|providing" + WS + r"the" + WS + r"following",
    re.IGNORECASE,
)


def is_production_file(name):
    """A 'produced document' from SMPD: not a sidecar, not metadata, not the
    portal-message-history PDF (which gets re-exported with every refresh and
    is the conversation log itself, not produced content)."""
    if name in SKIP_NAMES:
        return False
    if name.startswith("."):
        return False
    if SIDECAR_RE.search(name):
        return False
    if MESSAGE_HISTORY_RE.search(name):
        return False
    return True


def git_file_add_dates(root):
    """Map relative file path -> ISO date of its first commit under root.

    Single git invocation; we walk the output to find each path's earliest
    add. Files may be re-added (e.g., re-scraped Message_History.pdf rounds);
    we keep the oldest."""
    out = subprocess.run(
        ["git", "log", "--diff-filter=A", "--name-only",
         "--pretty=format:##%ad", "--date=short", "--", str(root)],
        check=True, capture_output=True, text=True,
    ).stdout

    earliest = {}
    cur_date = None
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("##"):
            cur_date = line[2:]
            continue
        if cur_date is None:
            continue
        prev = earliest.get(line)
        if prev is None or cur_date < prev:
            earliest[line] = cur_date
    return earliest


def iso_week_bounds(d):
    """Monday and Sunday ISO dates for the week containing date `d`."""
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def count_request_items(request_text):
    """Highest numbered item in a request body. Item headings live at line
    starts (``\\d+\\. <title>``); we take the max so an out-of-order quote
    doesn't truncate the count."""
    if not request_text:
        return 0
    nums = [int(m.group(1)) for m in ITEM_HEAD_RE.finditer(request_text)]
    return max(nums) if nums else 0


def classify_chunk(chunk):
    """Disposition type for one item-chunk of an agency reply.

    Priority is no-records > redirected > withheld > produced so that
    explicit "no responsive records" beats an echoed exemption discussion
    in the request body. The chunk includes the echoed item text plus the
    agency's response."""
    if NO_RECORDS_RE.search(chunk):
        return "no_records"
    if REDIRECTED_RE.search(chunk):
        return "redirected"
    if WITHHELD_RE.search(chunk):
        return "withheld"
    if PRODUCED_HINT_RE.search(chunk):
        return "produced"
    return "other"


def parse_dispositions(body, n_items):
    """Walk the agency body looking for item headings 1..N in order; classify
    the text between consecutive headings as that item's disposition.

    Returns ``[{item, type}]``. Requires the agency to have echoed numbered
    item headings; status-update or extension messages return ``[]``."""
    if not body or n_items <= 0:
        return []
    positions = []
    expected = 1
    for m in ITEM_HEAD_RE.finditer(body):
        if int(m.group(1)) == expected:
            positions.append((expected, m.start()))
            expected += 1
            if expected > n_items:
                break
    out = []
    for i, (n, start) in enumerate(positions):
        end = positions[i + 1][1] if i + 1 < len(positions) else len(body)
        out.append({"item": n, "type": classify_chunk(body[start:end])})
    return out


def main():
    if not REGISTRY_PATH.exists():
        print(f"missing {REGISTRY_PATH} — run build_pra_registry.py first",
              file=sys.stderr)
        sys.exit(1)

    registry = json.loads(REGISTRY_PATH.read_text())
    pra_by_id = {p["id"]: p for p in registry["pras"]}

    file_dates = git_file_add_dates(ASSETS_ROOT)

    # Bucket events into ISO weeks: (week_start, pra_id) -> {files, messages}
    weeks = defaultdict(lambda: defaultdict(lambda: {"files": [], "messages": []}))

    for path, iso_date in file_dates.items():
        rel = Path(path)
        parts = rel.parts
        if len(parts) < 3:
            continue
        pra_id = parts[2] if parts[1] == "san-mateo-public-records" else None
        if not pra_id or not PRA_FOLDER_RE.match(pra_id):
            continue
        name = rel.name
        if not is_production_file(name):
            continue
        if pra_id not in pra_by_id:
            continue
        wk_start, _ = iso_week_bounds(date.fromisoformat(iso_date))
        attachment = next(
            (a for a in pra_by_id[pra_id]["derived"]["attachments"] if a["name"] == name),
            None,
        )
        weeks[wk_start][pra_id]["files"].append({
            "name": name,
            "date": iso_date,
            "url": attachment["url"] if attachment else None,
        })

    for pra in registry["pras"]:
        n_items = count_request_items(pra["derived"].get("request_text"))
        for msg in pra["derived"]["messages"]:
            if msg["sender_role"] != "agency":
                continue
            if PORTAL_AUTO_ACK_RE.search(msg["body"]):
                continue
            ts = msg["ts"]
            msg_date = date.fromisoformat(ts[:10])
            wk_start, _ = iso_week_bounds(msg_date)
            weeks[wk_start][pra["id"]]["messages"].append({
                "ts": ts,
                "sender_name": msg["sender_name"],
                "body": msg["body"],
                "body_segments": msg.get("body_segments") or [],
                "dispositions": parse_dispositions(msg["body"], n_items),
            })

    out_weeks = []
    for wk_start in sorted(weeks.keys(), reverse=True):
        _, wk_end = iso_week_bounds(date.fromisoformat(wk_start))
        by_pra_rows = []
        files_total = 0
        msgs_total = 0
        response_total = 0
        no_records_total = 0
        continuation_total = 0
        for pra_id in sorted(weeks[wk_start].keys()):
            ev = weeks[wk_start][pra_id]
            ev["files"].sort(key=lambda f: f["name"])
            ev["messages"].sort(key=lambda m: m["ts"])
            files_total += len(ev["files"])
            msgs_total += len(ev["messages"])

            pra = pra_by_id[pra_id]
            n_items = count_request_items(pra["derived"].get("request_text"))
            # Per-item outcome for this PRA-week: a disposed item takes its
            # disposition type; an item that was not disposed but whose PRA
            # received any message that week is a continuation; items in a
            # PRA-week with no messages don't count as "worked on."
            disposed = {}
            for m in ev["messages"]:
                for d in m["dispositions"]:
                    disposed[d["item"]] = d["type"]
            pra_no_records = 0
            pra_response = 0
            pra_continuation = 0
            if n_items > 0 and ev["messages"]:
                for i in range(1, n_items + 1):
                    if i in disposed:
                        if disposed[i] == "no_records":
                            pra_no_records += 1
                        else:
                            pra_response += 1
                    else:
                        pra_continuation += 1
            elif ev["messages"]:
                # Staff-created stub with no parseable item list. Treat each
                # agency message as one unit of work, attributed as continuation
                # since we have no item-level signal.
                pra_continuation = len(ev["messages"])
            no_records_total += pra_no_records
            response_total += pra_response
            continuation_total += pra_continuation

            by_pra_rows.append({
                "pra_id": pra_id,
                "title": pra["curated"].get("title", ""),
                "display_status": pra["display_status"],
                "n_items": n_items,
                "outcomes": {
                    "response": pra_response,
                    "no_records": pra_no_records,
                    "continuation": pra_continuation,
                },
                "files": ev["files"],
                "messages": ev["messages"],
            })
        worked_total = response_total + no_records_total + continuation_total
        out_weeks.append({
            "week_start": wk_start,
            "week_end": wk_end,
            "totals": {
                "files": files_total,
                "messages": msgs_total,
                "worked_on": worked_total,
                "response": response_total,
                "no_records": no_records_total,
                "continuation": continuation_total,
                "pras_touched": len(by_pra_rows),
            },
            "by_pra": by_pra_rows,
        })

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "weeks": out_weeks,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {OUTPUT_PATH}: {len(out_weeks)} weeks, "
          f"{sum(w['totals']['files'] for w in out_weeks)} files, "
          f"{sum(w['totals']['messages'] for w in out_weeks)} agency messages, "
          f"{sum(w['totals']['worked_on'] for w in out_weeks)} items worked on "
          f"({sum(w['totals']['response'] for w in out_weeks)} response, "
          f"{sum(w['totals']['no_records'] for w in out_weeks)} no-records, "
          f"{sum(w['totals']['continuation'] for w in out_weeks)} continuation)")


if __name__ == "__main__":
    main()
