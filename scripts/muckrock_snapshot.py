#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Snapshot our own MuckRock requests into the repo.

Fetches each public request page, parses it deterministically (no LLM), and
writes assets/public-records/<agency>/muckrock-<id>/:

  MuckRock_<id>_Thread.pdf   the request header and every communication, as text
  metadata.json              request fields, communication index, capture provenance
  <attachment files>         documents the agency attached (pdf/doc/xls/csv/zip)

Then it runs scripts/ocr_sidecar.py on the new PDFs. A request whose thread
text is unchanged since the last snapshot is left alone, so re-running
produces no churn.

Redactions, applied to everything written:
  - The requester's surname. The sender of the first communication is the
    requester; their full name becomes their first name. No name is hardcoded.
  - MuckRock upload links (agency-facing, carry an access token) and
    *@requests.muckrock.com routing addresses.
Cloudflare-obfuscated addresses ("[email protected]") are decoded first, so
agency addresses read as sent.

Fetching: www.muckrock.com challenges browser user agents, but plain curl
with its default user agent gets the page, so this shells out to curl.

Usage:
  uv run python scripts/muckrock_snapshot.py --agency san-jose <request-url> [<request-url> ...]
  uv run python scripts/muckrock_snapshot.py --agency smcso --save-html <dir> <request-url>
"""

import argparse
import hashlib
import html
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import fitz  # pymupdf

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "assets" / "public-records"
ATTACHMENT_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "csv", "zip"}

REQUEST_ID_RE = re.compile(r"-(\d+)/?(?:#.*)?$")
COMM_START_RE = re.compile(
    r'<section class="[^"]*\bcommunication textbox[^"]*" id="comm-(\d+)">'
)
FILES_PANEL = '<section role="tabpanel" class="tab-panel files"'
CFEMAIL_RE = re.compile(
    r'<(a|span)\b[^>]*data-cfemail="([0-9a-fA-F]+)"[^>]*>.*?</\1>', re.S
)
UPLOAD_LINK_RE = re.compile(r"(Upload documents directly:)\s*\S+")
SENDGRID_RE = re.compile(r"https?://\S*sendgrid\.net\S*")
ROUTING_ADDR_RE = re.compile(r"[\w.+-]+@requests\.muckrock\.com", re.I)


def fetch(url: str) -> str:
    # No -A: the default curl UA passes Cloudflare where browser UAs are challenged.
    out = subprocess.run(
        ["curl", "-sS", "-f", "--max-time", "60", url],
        check=True, capture_output=True,
    )
    return out.stdout.decode("utf-8")


def decode_cfemail(hexstr: str) -> str:
    data = bytes.fromhex(hexstr)
    key = data[0]
    return bytes(b ^ key for b in data[1:]).decode("utf-8", "replace")


def html_to_text(fragment: str) -> str:
    fragment = CFEMAIL_RE.sub(lambda m: html.escape(decode_cfemail(m.group(2))), fragment)
    fragment = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", fragment, flags=re.S)
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"</(p|div|li|tr|h\d)>", "\n\n", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _first(pattern: str, s: str, flags=re.S) -> str | None:
    m = re.search(pattern, s, flags)
    return html_to_text(m.group(1)) if m else None


def parse_request(page: str, url: str) -> dict:
    m = REQUEST_ID_RE.search(url)
    if not m:
        raise ValueError(f"no request id in URL: {url}")
    dates, overdue = {}, []
    tbl = re.search(r'<table class="dates">(.*?)</table>', page, re.S)
    if tbl:
        # MuckRock marks a missed date by adding "failure" to the cell's class.
        for key, label, cls, val in re.findall(
            r'<tr class="([^"]*)">\s*<td class="label">([^<]*)</td>\s*<td class="date([^"]*)">(.*?)</td>',
            tbl.group(1), re.S,
        ):
            name = key.strip() or html_to_text(label)
            dates[name] = html_to_text(val)
            if "failure" in cls.split():
                overdue.append(name)
    status = _first(r'<section class="status manager"[^>]*>(.*?)</section>', page)
    if status:
        status = re.sub(r"^Status\s*", "", status).strip()
    return {
        "muckrock_id": int(m.group(1)),
        "url": re.sub(r"#.*$", "", url),
        "title": _first(r"<h1[^>]*>(.*?)</h1>", page),
        "agency": _first(r'<a href="/agency/[^/"]+/[^/"]+/">(.*?)</a>', page),
        "agency_tracking_number": _first(
            r'<tr class="tracking-number">\s*<td>.*?</td>\s*<td>(.*?)</td>', page
        ),
        "status": status,
        "dates": dates,
        "overdue": overdue,
        "communications": parse_communications(page),
    }


def parse_communications(page: str) -> list[dict]:
    end = page.find(FILES_PANEL)
    region = page if end < 0 else page[:end]
    starts = list(COMM_START_RE.finditer(region))
    comms = []
    for i, m in enumerate(starts):
        block = region[m.start(): starts[i + 1].start() if i + 1 < len(starts) else len(region)]
        sender = _first(r'<p class="from">\s*From:(.*?)</p>', block) or ""
        dt = re.search(r'<time datetime="([^"]+)"', block)
        body_m = re.search(
            r'<section class="textbox__section communication-body">(.*?)</section>\s*(?:<ul class="files">|</section>|$)',
            block, re.S,
        )
        attachments = []
        files = re.search(r'<ul class="files">(.*?)</ul>\s*</section>', block, re.S)
        if files:
            for title, href in re.findall(
                r'data-title="([^"]*)".*?<a href="(https://cdn\.muckrock\.com/[^"]+)"',
                files.group(1), re.S,
            ):
                attachments.append({"title": html.unescape(title), "url": href})
        comms.append({
            "id": f"comm-{m.group(1)}",
            "from": sender.strip(),
            "datetime": dt.group(1) if dt else None,
            "subject": re.sub(r"^Subject:\s*", "", _first(r'<p class="small subject">(.*?)</p>', block) or ""),
            "channel": _first(r'<span class="small badge">(.*?)</span>', block),
            "body": html_to_text(body_m.group(1)) if body_m else "",
            "attachments": attachments,
        })
    return comms


def redact(req: dict) -> dict:
    """Return a redacted deep copy. The requester is the first sender."""
    req = json.loads(json.dumps(req))
    comms = req["communications"]
    full = comms[0]["from"].strip() if comms else ""
    parts = full.split()
    subs = []
    if len(parts) >= 2:
        first, surname = parts[0], parts[-1]
        subs = [
            (re.compile(re.escape(full), re.I), first),
            (re.compile(rf"\b{re.escape(surname)}\b", re.I), "[surname redacted]"),
        ]

    def clean(s):
        if not isinstance(s, str):
            return s
        s = UPLOAD_LINK_RE.sub(r"\1 [MuckRock upload link removed]", s)
        s = SENDGRID_RE.sub("[link removed]", s)
        s = ROUTING_ADDR_RE.sub("[MuckRock request address]", s)
        for rx, rep in subs:
            s = rx.sub(rep, s)
        return s

    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return clean(o)

    out = walk(req)
    out["redactions"] = [
        "requester surname (first sender's full name reduced to first name)",
        "MuckRock upload links",
        "MuckRock request routing addresses",
    ]
    return out


def thread_text(req: dict) -> str:
    """Canonical text of the thread; its hash decides whether anything changed."""
    keep = {k: req[k] for k in ("title", "agency", "agency_tracking_number", "status", "dates", "overdue", "communications")}
    return json.dumps(keep, ensure_ascii=False, sort_keys=True, indent=1)


def render_pdf(req: dict, captured_at: str, html_sha256: str, out: Path) -> None:
    e = html.escape
    meta_bits = [f"MuckRock request #{req['muckrock_id']}", req.get("agency") or ""]
    if req.get("agency_tracking_number"):
        meta_bits.append(f"Agency tracking # {req['agency_tracking_number']}")
    date_bits = [f"Status: {req.get('status') or 'unknown'}"] + [
        f"{k.replace('-', ' ').title()}: {v}" + (" (overdue per MuckRock)" if k in req["overdue"] else "")
        for k, v in req["dates"].items()
    ]
    parts = [
        f"<h1>{e(req.get('title') or '')}</h1>",
        f"<p class='meta'>{e(' · '.join(b for b in meta_bits if b))}</p>",
        f"<p class='meta'>{e(' · '.join(date_bits))}</p>",
        f"<p class='meta'>Source: {e(req['url'])}</p>",
        "<p class='note'>"
        + e(
            f"Captured {captured_at} from the public request page (raw HTML sha256 {html_sha256}). "
            "Communication text is extracted verbatim. Redacted: "
            + "; ".join(req["redactions"]) + "."
        )
        + "</p>",
    ]
    for c in req["communications"]:
        head = f"From: {c['from']}"
        if c.get("datetime"):
            head += f" — {c['datetime']}"
        if c.get("channel"):
            head += f" ({c['channel']})"
        parts.append(f"<h2>{e(head)}</h2>")
        if c.get("subject"):
            parts.append(f"<p class='subj'>Subject: {e(c['subject'])}</p>")
        for para in c["body"].split("\n\n"):
            if para.strip():
                parts.append("<p>" + "<br/>".join(e(ln) for ln in para.split("\n")) + "</p>")
        if c["attachments"]:
            names = ", ".join(a["title"] for a in c["attachments"])
            parts.append(f"<p class='att'>Attachments: {e(names)}</p>")
    # Monospace on purpose: MuPDF shapes proportional fonts with ligatures it
    # won't turn off ("Sheriff" extracts as "Sheri\ufb00"), which breaks text
    # search in the PDF and its sidecar. The monospace face has none.
    css = (
        "body {font-family: monospace; font-size: 9pt;} "
        "h1 {font-size: 11pt;} h2 {font-size: 9.5pt; margin-top: 12pt;} "
        ".meta, .note, .subj, .att {font-size: 8.5pt;} .note {font-style: italic;}"
    )
    story = fitz.Story(html="".join(parts), user_css=css)
    writer = fitz.DocumentWriter(str(out))
    page = fitz.paper_rect("letter")
    where = page + (54, 54, -54, -54)
    more = True
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


def download_attachments(req: dict, folder: Path, requester_first: str) -> list[Path]:
    saved = []
    for c in req["communications"]:
        if c["from"] == requester_first:
            continue  # our own attachments are already in the repo
        for a in c["attachments"]:
            name = Path(a["url"].split("?")[0]).name
            if name.rsplit(".", 1)[-1].lower() not in ATTACHMENT_EXTENSIONS:
                continue
            dest = folder / name
            if not dest.exists():
                subprocess.run(["curl", "-sS", "-f", "-o", str(dest), a["url"]], check=True)
            saved.append(dest)
    return saved


def snapshot(url: str, agency: str, root: Path, save_html: Path | None, force: bool) -> Path | None:
    page = fetch(url)
    html_sha = hashlib.sha256(page.encode("utf-8")).hexdigest()
    req = redact(parse_request(page, url))
    rid = req["muckrock_id"]
    folder = root / agency / f"muckrock-{rid}"
    folder.mkdir(parents=True, exist_ok=True)
    meta_path = folder / "metadata.json"
    thread_sha = hashlib.sha256(thread_text(req).encode("utf-8")).hexdigest()
    if meta_path.exists() and not force:
        old = json.loads(meta_path.read_text())
        if old.get("thread_sha256") == thread_sha:
            print(f"{rid}: unchanged since {old.get('captured_at_utc')}")
            return None
    captured_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if save_html:
        save_html.mkdir(parents=True, exist_ok=True)
        (save_html / f"muckrock_{rid}_{captured_at[:10]}.html").write_text(page)
    pdf = folder / f"MuckRock_{rid}_Thread.pdf"
    render_pdf(req, captured_at, html_sha, pdf)
    first = req["communications"][0]["from"] if req["communications"] else ""
    attachments = download_attachments(req, folder, first)
    meta = {
        "platform": "muckrock",
        **{k: req[k] for k in ("muckrock_id", "url", "title", "agency", "agency_tracking_number", "status", "dates", "overdue")},
        "captured_at_utc": captured_at,
        "html_sha256": html_sha,
        "thread_sha256": thread_sha,
        "redactions": req["redactions"],
        "communications": [
            {k: c[k] for k in ("id", "from", "datetime", "subject", "channel", "attachments")}
            for c in req["communications"]
        ],
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ocr_sidecar.py"), "--files", str(pdf), *map(str, attachments)],
        check=True,
    )
    print(f"{rid}: wrote {folder.relative_to(REPO_ROOT)} ({len(req['communications'])} communications)")
    return folder


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+", help="MuckRock request URLs")
    ap.add_argument("--agency", required=True, help="folder under assets/public-records/ (e.g. san-jose, smcso)")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--save-html", type=Path, help="also keep the raw page here (outside the repo: it carries tokens)")
    ap.add_argument("--force", action="store_true", help="rewrite even if the thread is unchanged")
    args = ap.parse_args()
    for url in args.urls:
        snapshot(url, args.agency, args.root, args.save_html, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
