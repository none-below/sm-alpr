#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Build the PRA registry by combining curated per-PRA metadata.json files with
data parsed deterministically from the portal's Message_History.pdf exports.

Source-of-truth split:
  - Curated half: hand-written assets/san-mateo-public-records/W*/metadata.json
  - Derived half: parsed from the W*_Message_History.pdf in the same folder

Output: assets/pra_registry.json (build artifact, gitignored).

No LLM is used. Parsing is deterministic regex over native PDF text.

Usage:
  uv run python scripts/build_pra_registry.py            # build registry
  uv run python scripts/build_pra_registry.py --init     # also seed missing
                                                          # metadata.json stubs
  uv run python scripts/build_pra_registry.py --init --dry-run
"""

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

import fitz  # pymupdf

sys.path.insert(0, str(Path(__file__).parent))
from pra_tags import TAGS  # noqa: E402

ROOTS = [
    {
        "jurisdiction": "san-mateo-pd",
        "root": Path("assets/san-mateo-public-records"),
        "id_pattern": re.compile(r"^W\d{6}-\d{6}$"),
        "attachment_url_prefix": (
            "https://github.com/none-below/sm-alpr/blob/main/"
            "assets/san-mateo-public-records"
        ),
        "folder_url_prefix": (
            "https://github.com/none-below/sm-alpr/tree/main/"
            "assets/san-mateo-public-records"
        ),
    },
]
REGISTRY_PATH = Path("docs/data/pra_registry.json")

HEADER_RE = re.compile(
    r"^On (\d{1,2}/\d{1,2}/\d{4}) (\d{1,2}:\d{2}:\d{2} [AP]M), (.+?) wrote:\s*$",
    re.MULTILINE,
)
PROMISED_NUMERIC_RE = re.compile(
    r"no later than (\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE
)
PROMISED_MONTHNAME_RE = re.compile(
    r"no later than (January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:[,\s]+(\d{4}))?",
    re.IGNORECASE,
)
MONTH_NUM = {m: i + 1 for i, m in enumerate([
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
])}
PAGE_FOOTER_RE = re.compile(r"^\s*Page \d+\s*$", re.MULTILINE)
AGENCY_HINT_RE = re.compile(
    r"San Mateo Public Records Center|Maravillas|O'?Keefe|Norris|Peruzzaro|Lethin",
    re.IGNORECASE,
)
WITHDRAW_RE = re.compile(r"\bwithdraw(?:ing|n|al)?\b", re.IGNORECASE)
# SMPD's canonical closure phrasing is
#   "now considers this record request W0XXXXX-XXXXXX closed"
# Anchoring on "considers ... closed" avoids matching user-quoted "closing"
# language in request bodies that get echoed back by the agency.
CLOSED_RE = re.compile(
    r"(?:now\s+)?considers\s+(?:this|the)[\w\s\d\-]{0,80}closed",
    re.IGNORECASE,
)
PORTAL_AUTO_ACK_RE = re.compile(
    r"Thank you for your interest in public records of the City of San Mateo",
    re.IGNORECASE,
)
REQUEST_TEXT_RE = re.compile(
    r"Record\(s\) Requested:\s*(.+?)"
    r"(?=The request is being forwarded to appropriate department|"
    r"\s*Monitor request progress at the link below|"
    r"\Z)",
    re.DOTALL | re.IGNORECASE,
)


def parse_short_date(s):
    """Parse '5/4/2026' or '5/4/26' to ISO 'YYYY-MM-DD'."""
    m, d, y = s.split("/")
    y = int(y)
    if y < 100:
        y += 2000
    return f"{y:04d}-{int(m):02d}-{int(d):02d}"


def parse_timestamp(date_str, time_str):
    """Combine '5/4/2026' + '2:04:38 AM' into ISO timestamp."""
    return datetime.strptime(
        f"{date_str} {time_str}", "%m/%d/%Y %I:%M:%S %p"
    ).isoformat()


def pra_filing_iso_from_id(pra_id):
    """W012665-050426 → '2026-05-04'. Returns None if id malformed."""
    m = re.match(r"^W\d{6}-(\d{2})(\d{2})(\d{2})$", pra_id)
    if not m:
        return None
    mm, dd, yy = m.groups()
    return f"20{yy}-{mm}-{dd}"


def _pdf_text_with_paragraphs(pdf_path):
    """Read a PDF as paragraph-separated text.

    pymupdf's default ``get_text()`` preserves the PDF's visual line wraps,
    which destroys logical paragraph structure. Block-level extraction
    returns one block per visual paragraph; within each block we flatten
    column-wrap line breaks to spaces and separate blocks with a blank line.
    Page footers like ``Page N`` are dropped.

    PDFs that split a sentence across a page break produce two blocks where
    the first does not end with sentence-final punctuation. We merge those
    pairs so paragraphs read continuously across page boundaries.
    """
    doc = fitz.open(pdf_path)
    terminator_re = re.compile(r"[.!?:\"')\]]$")
    header_start_re = re.compile(r"^On \d{1,2}/\d{1,2}/\d{4}\s")
    try:
        parts = []
        for page_num in range(len(doc)):
            page_blocks = doc[page_num].get_text("blocks")
            for block_idx, block in enumerate(page_blocks):
                # block = (x0, y0, x1, y1, text, block_no, block_type)
                block_text = (block[4] or "").strip()
                if not block_text:
                    continue
                if PAGE_FOOTER_RE.match(block_text):
                    continue
                lines = [ln.strip() for ln in block_text.split("\n") if ln.strip()]
                if not lines:
                    continue
                joined = " ".join(lines)
                # If this is the first block on a non-initial page AND the
                # previous block ended without sentence-final punctuation,
                # treat it as a continuation across the page break.
                crossed_page = (
                    page_num > 0
                    and block_idx == 0
                    and parts
                    and not terminator_re.search(parts[-1])
                    and not header_start_re.match(joined)
                )
                if crossed_page:
                    parts[-1] = parts[-1] + " " + joined
                else:
                    parts.append(joined)
        return "\n\n".join(parts)
    finally:
        doc.close()


def extract_messages(pdf_path):
    """Parse a Message_History PDF into chronological list of messages."""
    text = _pdf_text_with_paragraphs(pdf_path)
    matches = list(HEADER_RE.finditer(text))
    if not matches:
        return []

    messages = []
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        sender = m.group(3).strip()
        is_agency = bool(AGENCY_HINT_RE.search(sender))
        messages.append({
            "ts": parse_timestamp(m.group(1), m.group(2)),
            "sender_name": sender,
            "sender_role": "agency" if is_agency else "requester",
            "body": body,
        })
    # PDF lists newest-first; reverse for chronological order.
    messages.reverse()
    return messages


def _promise_from_monthname(match, fallback_year):
    """Parse 'May 20th' / 'May 20, 2026' to ISO date string."""
    month = MONTH_NUM[match.group(1).lower()]
    day = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else fallback_year
    return f"{year:04d}-{month:02d}-{day:02d}"


def promise_history(messages, fallback_year):
    """Every distinct 'no later than X' date the agency has promised, in order.

    Each entry is {promise_date, set_on} where set_on is the message timestamp
    when that promise was made. The latest entry is the current promise; the
    number of entries minus one is the extension count.
    """
    history = []
    for msg in messages:
        if msg["sender_role"] != "agency":
            continue
        if PORTAL_AUTO_ACK_RE.search(msg["body"]):
            continue
        dates = []
        for m in PROMISED_NUMERIC_RE.finditer(msg["body"]):
            dates.append(parse_short_date(m.group(1)))
        for m in PROMISED_MONTHNAME_RE.finditer(msg["body"]):
            dates.append(_promise_from_monthname(m, fallback_year))
        if not dates:
            continue
        latest_in_msg = max(dates)
        # Skip if this is the same date as the last promise (no change)
        if history and history[-1]["promise_date"] == latest_in_msg:
            continue
        history.append({"promise_date": latest_in_msg, "set_on": msg["ts"][:10]})
    return history


def derive_status(messages, promised_date, today_iso):
    """Tentative status from message stream. curated.status_override wins downstream."""
    if not messages:
        return "unknown"

    last = messages[-1]
    if last["sender_role"] == "requester" and WITHDRAW_RE.search(last["body"][:300]):
        return "withdrawn"

    substantive_agency = [
        m for m in messages
        if m["sender_role"] == "agency"
        and not PORTAL_AUTO_ACK_RE.search(m["body"])
    ]

    if not substantive_agency:
        return "awaiting_initial"

    # Closure language: check last substantive agency message only.
    if CLOSED_RE.search(substantive_agency[-1]["body"]):
        return "closed"

    if promised_date:
        return "overdue" if promised_date < today_iso else "rolling_production"

    # Substantive agency reply with no future promise — likely closed but
    # the regex didn't catch it. Mark for curator review.
    return "needs_review"


CLOSING_MARKERS = [
    "If you have any questions, please contact me",
    "If you have any questions",
    "\nSincerely,",
    "\nSincerely",
    "\nThanks,\nKelly",
    "\nThank you,\nKelly",
]


def _find_closing(text):
    earliest = -1
    for marker in CLOSING_MARKERS:
        idx = text.find(marker)
        if idx >= 0 and (earliest < 0 or idx < earliest):
            earliest = idx
    return earliest


def split_agency_message_body(body, request_text):
    """Split an agency message body into typed spans.

    Segments:
      - ``before``  preamble before the first echo (Subject:, salutation, etc.)
      - ``echo``    a verbatim copy of the requester's text echoed back
      - ``after``   the agency's substantive response text
      - ``closing`` sign-off ("If you have any questions...", "Sincerely, ...")

    Agencies sometimes echo the request as one block, sometimes interleave
    item-by-item quotes between their responses. We use SequenceMatcher to
    find ALL non-overlapping matching substrings between the body and the
    original request text, then emit alternating echo/after spans as they
    appear. Matches shorter than 80 characters are ignored as noise.
    """
    if not body:
        return []

    matches = []  # [(start_in_body, end_in_body), ...]
    if request_text and len(request_text) >= 100:
        sm = SequenceMatcher(None, body, request_text, autojunk=False)
        for block in sm.get_matching_blocks():
            if block.size >= 80:
                matches.append((block.a, block.a + block.size))

    # Merge adjacent / near-adjacent matches.
    matches.sort()
    merged = []
    for a, b in matches:
        if merged and a <= merged[-1][1] + 5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))

    # Walk the body emitting unmatched/echo segments in order.
    raw = []
    cursor = 0
    for a, b in merged:
        if a > cursor:
            raw.append(("unmatched", body[cursor:a]))
        raw.append(("echo", body[a:b]))
        cursor = b
    if cursor < len(body):
        raw.append(("unmatched", body[cursor:]))

    # Classify each unmatched chunk: anything before the first echo is
    # "before" (preamble); anything after is "after" (agency response). If
    # there are no echoes at all in this body, the whole thing is "after" —
    # it's a freestanding substantive reply rather than a quoted-and-answered
    # response, so we don't want to mute it as preamble.
    has_any_echo = any(kind == "echo" for kind, _ in raw)
    segments = []
    seen_echo = False
    for kind, text in raw:
        stripped = text.strip()
        if not stripped:
            if kind == "echo":
                seen_echo = True
            continue
        if kind == "echo":
            segments.append({"type": "echo", "text": stripped})
            seen_echo = True
        else:
            if not has_any_echo:
                seg_type = "after"
            else:
                seg_type = "before" if not seen_echo else "after"
            segments.append({"type": seg_type, "text": stripped})

    # Split the trailing closing signature off the last "after" segment.
    for i in range(len(segments) - 1, -1, -1):
        seg = segments[i]
        if seg["type"] != "after":
            continue
        ci = _find_closing(seg["text"])
        if ci >= 0:
            response_text = seg["text"][:ci].rstrip()
            closing_text = seg["text"][ci:].strip()
            if response_text:
                seg["text"] = response_text
                segments.insert(i + 1, {"type": "closing", "text": closing_text})
            else:
                segments[i] = {"type": "closing", "text": closing_text}
        break

    return segments


def extract_request_text(messages):
    """Pull the original request body out of the agency's first auto-ack message.

    The portal's auto-ack echoes the requester's submitted text between
    'Record(s) Requested:' and 'The request is being forwarded to...'. For
    staff-created portal stubs ('Request was created by staff') there is no
    embedded request body; returns None in that case.
    """
    for msg in messages:
        if msg["sender_role"] != "agency":
            continue
        if not PORTAL_AUTO_ACK_RE.search(msg["body"]):
            continue
        m = REQUEST_TEXT_RE.search(msg["body"])
        if not m:
            return None
        return m.group(1).strip()
    return None


SIDECAR_RE = re.compile(r"\.[0-9a-f]{8}\.(?:txt|pdf)$")
SKIP_FILES = {"metadata.json", "audit_rows.json"}


def find_attachments(folder, url_prefix):
    """List user-relevant files in a PRA folder.

    Skips OCR text sidecars (``<name>.<8hex>.txt`` / ``<name>.<8hex>.pdf``),
    derived build artifacts (``metadata.json``, ``audit_rows.json``), and
    dotfiles. Returns ``[{name, url}]`` where ``url`` is a GitHub-rendered
    link to the file.
    """
    out = []
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        if p.name in SKIP_FILES:
            continue
        if p.name.startswith("."):
            continue
        if SIDECAR_RE.search(p.name):
            continue
        out.append({
            "name": p.name,
            "url": f"{url_prefix}/{folder.name}/{quote(p.name)}",
        })
    return out


def stub_metadata():
    return {
        "title": "TODO",
        "summary": "TODO — one or two sentences on what this PRA tests",
        "tags": [],
        "policy_promises": [],
        "prior_pra_refs": [],
        "filed_because": "TODO",
        "status_override": None,
        "notes": "",
    }


def init_stubs(roots, dry_run=False):
    created = []
    for root_cfg in roots:
        if not root_cfg["root"].is_dir():
            continue
        for folder in sorted(root_cfg["root"].iterdir()):
            if not folder.is_dir():
                continue
            if not root_cfg["id_pattern"].match(folder.name):
                continue
            mpath = folder / "metadata.json"
            if mpath.exists():
                continue
            if dry_run:
                print(f"would create: {mpath}")
            else:
                mpath.write_text(json.dumps(stub_metadata(), indent=2) + "\n")
                print(f"created:      {mpath}")
            created.append(str(mpath))
    return created


def validate_curated(curated, pra_id):
    bad = [t for t in curated.get("tags", []) if t not in TAGS]
    if bad:
        raise ValueError(
            f"{pra_id}: unknown tag(s) {bad}. "
            f"Edit scripts/pra_tags.py to add them, or fix the typo. "
            f"Valid tags: {sorted(TAGS)}"
        )


def build(roots):
    today_iso = date.today().isoformat()
    entries = []

    for root_cfg in roots:
        if not root_cfg["root"].is_dir():
            continue
        for folder in sorted(root_cfg["root"].iterdir()):
            if not folder.is_dir() or not root_cfg["id_pattern"].match(folder.name):
                continue
            pra_id = folder.name

            pdfs = list(folder.glob("*_Message_History.pdf"))
            if not pdfs:
                print(f"warn: no Message_History.pdf in {folder}", file=sys.stderr)
                continue

            messages = extract_messages(pdfs[0])
            filing_iso = pra_filing_iso_from_id(pra_id)
            filed_dt = date.fromisoformat(filing_iso) if filing_iso else None
            fallback_year = filed_dt.year if filed_dt else date.today().year
            promises = promise_history(messages, fallback_year)
            promised = promises[-1]["promise_date"] if promises else None
            status = derive_status(messages, promised, today_iso)
            request_text = extract_request_text(messages)
            for m in messages:
                if m["sender_role"] != "agency":
                    continue
                if PORTAL_AUTO_ACK_RE.search(m["body"]):
                    continue
                m["body_segments"] = split_agency_message_body(m["body"], request_text)

            days_processing = None
            if filed_dt:
                days_processing = (date.today() - filed_dt).days

            derived = {
                "filed_date": filing_iso,
                "days_processing": days_processing,
                "statutory_10day": (filed_dt + timedelta(days=10)).isoformat() if filed_dt else None,
                "statutory_24day": (filed_dt + timedelta(days=24)).isoformat() if filed_dt else None,
                "current_promised_date": promised,
                "promise_history": promises,
                "extension_count": max(0, len(promises) - 1),
                "status": status,
                "last_message_ts": messages[-1]["ts"] if messages else None,
                "request_text": request_text,
                "messages": messages,
                "attachments": find_attachments(folder, root_cfg["attachment_url_prefix"]),
                "folder_url": f"{root_cfg['folder_url_prefix']}/{pra_id}",
                "download_zip_url": (
                    "https://download-directory.github.io/?url="
                    + quote(f"{root_cfg['folder_url_prefix']}/{pra_id}", safe="")
                ),
            }

            curated_path = folder / "metadata.json"
            if curated_path.exists():
                curated = json.loads(curated_path.read_text())
            else:
                curated = stub_metadata()
            validate_curated(curated, pra_id)

            entries.append({
                "id": pra_id,
                "jurisdiction": root_cfg["jurisdiction"],
                "curated": curated,
                "derived": derived,
            })

    by_id = {e["id"]: e for e in entries}
    for e in entries:
        e["derived"]["cited_by"] = []
    for e in entries:
        for ref in e["curated"].get("prior_pra_refs", []) or []:
            if isinstance(ref, dict):
                ref_id = ref.get("ref")
                relation = ref.get("relation", "")
            else:
                ref_id = ref
                relation = ""
            if ref_id in by_id:
                by_id[ref_id]["derived"]["cited_by"].append({
                    "ref": e["id"],
                    "relation": relation,
                })

    for e in entries:
        override = e["curated"].get("status_override")
        e["display_status"] = override if override else e["derived"]["status"]

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tags": TAGS,
        "pras": sorted(entries, key=lambda x: x["id"]),
    }


def closed_ids(roots=ROOTS):
    """Set of PRA ids whose display_status is "closed".

    Computed exactly as the registry does — display_status folds in both the
    curated status_override and the parser's derived status, so it catches the
    ~40% of closed PRAs that are closed only by derived status, not override.
    Recomputed from the on-disk *_Message_History.pdf files, so it needs no
    pre-built (gitignored) registry artifact.

    Consumed by scripts/pra_download.py --active to skip already-closed
    requests: a closed PRA won't gain new messages. Like build(), this assumes
    the CWD is the repo root (ROOTS holds repo-relative paths).
    """
    registry = build(roots)
    return {e["id"] for e in registry["pras"] if e["display_status"] == "closed"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--init", action="store_true",
                    help="Create stub metadata.json files in folders missing one")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --init, show what would be created without writing")
    args = ap.parse_args()

    if args.init:
        init_stubs(ROOTS, dry_run=args.dry_run)
        if args.dry_run:
            return

    registry = build(ROOTS)
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, default=str) + "\n")
    print(f"wrote {REGISTRY_PATH}: {len(registry['pras'])} PRAs")


if __name__ == "__main__":
    main()
