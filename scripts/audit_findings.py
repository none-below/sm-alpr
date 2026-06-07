#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
audit_findings.py — adversarial quality audit for docs/SMPD_ALPR_Findings.md.

THE FINDINGS-AUDIT STANDARD (6 criteria). This is the coherent, reusable spec for
checking the findings document before it goes to a hostile reader (city attorney,
grand jury, press):

  1. Singular & sourced — each body bullet states ONE verifiable fact and carries a
     source ref [N]. (Key Findings and the Executive Summary cite sections "(see §N)"
     by convention, NOT inline [N] — they are exempt from the [N] requirement.)
  2. Quote / source / content fidelity — each [N] is the correct source for its claim,
     AND the source actually supports the bullet's assertion (read the source and
     fact-check the claim, not just the pointer).                         [LLM review]
  3. Reachable — every source URL resolves (no 404 / moved host).   [auto: --check-urls]
  4. URL <-> description — each source URL is actually about what its row describes
     (a "Redwood City" row's link is about that Redwood City matter).     [LLM review]
  5. Required -> Say -> Do — fact clusters follow "what the law/policy requires ->
     what SMPD says it does -> what SMPD does" where practical.           [LLM review]
  6. Tables clear & concise — every table row has the header's column count.     [auto]

This script automates the deterministic criteria (1 presence, 3 reachability, 6 table
shape) and lists the rows the LLM-review criteria (2, 4, 5) must cover. Pair it with the
companion adversarial workflow for 2/4/5 (fan out one verifier per source row + per
section). Criterion 3 distinguishes in-repo GitHub blob links (checked against the local
working tree, since blob/main 404s for files still on a PR branch) from external URLs
(HTTP-checked).

Usage:
  python3 scripts/audit_findings.py                 # offline: criteria 1 + 6
  python3 scripts/audit_findings.py --check-urls    # + criterion 3 (network)
  python3 scripts/audit_findings.py --json          # machine-readable report

Exits 0 if no hard issues (sourceless body bullets, ragged tables, dead URLs), else 1.
"""

import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001 — fall back to system trust store
    _SSL_CTX = ssl.create_default_context()

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOC = REPO_ROOT / "docs" / "SMPD_ALPR_Findings.md"

# Sections that cite by (see §N), not inline [N] — exempt from criterion 1's [N] rule.
SECTION_REF_EXEMPT = {
    "Executive Summary", "Key Findings", "Source Documents", "Key Contacts",
    "Items Requiring Verification",
}
# Heading prefixes that are reference matter, not fact-bullet body.
APPENDIX_PREFIXES = ("Appendix",)

MD_LINK_RE = re.compile(r"\[[^\]]+\]\((https?://[^)\s]+)\)")
# A bullet is "sourced" if it has [N], [N, §..], (see §N), or an inline [text](url) link.
HAS_SOURCE_RE = re.compile(r"\[\d{1,3}(?:,|\])|\(see §|\]\(https?://")
GITHUB_BLOB_RE = re.compile(
    r"https://github\.com/none-below/sm-alpr/blob/[^/]+/(.+)$"
)
SENTENCE_END_RE = re.compile(r"[.;] +[A-Z(]")  # rough sentence boundary


def split_sections(text):
    """Yield (heading, [body_lines], start_line) for each '## ' section."""
    lines = text.splitlines()
    cur_head, cur_body, cur_start = None, [], 0
    for i, line in enumerate(lines, start=1):
        if line.startswith("## "):
            if cur_head is not None:
                yield cur_head, cur_body, cur_start
            cur_head, cur_body, cur_start = line[3:].strip(), [], i
        elif cur_head is not None:
            cur_body.append((i, line))
    if cur_head is not None:
        yield cur_head, cur_body, cur_start


def is_body_section(heading):
    if heading in SECTION_REF_EXEMPT:
        return False
    if any(heading.startswith(p) for p in APPENDIX_PREFIXES):
        return False
    return True  # numbered "N. Title" sections etc.


def check_bullets(sections):
    """Criterion 1: body bullets are singular and sourced."""
    sourceless, compound = [], []
    for heading, body, _ in sections:
        if not is_body_section(heading):
            continue
        for ln, line in body:
            s = line.strip()
            if not (s.startswith("- ") or s.startswith("* ")):
                continue
            # skip table rows / sub-structural
            text = s[2:].strip()
            if not text or text.startswith("|"):
                continue
            # indented sub-bullets inherit their parent's source — don't flag them
            indent = len(line) - len(line.lstrip())
            if not HAS_SOURCE_RE.search(text) and indent == 0:
                sourceless.append((ln, heading, text[:90]))
            # singular heuristic: many sentences AND many distinct refs -> maybe compound
            n_sent = len(SENTENCE_END_RE.findall(text)) + 1
            n_ref = len(set(re.findall(r"\[(\d{1,3})\]", text)))
            if n_sent >= 4 and n_ref >= 4:
                compound.append((ln, heading, n_sent, n_ref, text[:90]))
    return sourceless, compound


def check_tables(text):
    """Criterion 6: every table row matches the header's column count."""
    issues = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and "|" in lines[i][1:]:
            block = []
            start = i
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append((i + 1, lines[i]))
                i += 1
            if len(block) >= 2:
                def ncols(row):
                    return row.strip().strip("|").count("|") + 1
                header_cols = ncols(block[0][1])
                for ln, row in block[1:]:
                    # separator row (---|---) is fine
                    if set(row.strip().strip("|").replace("|", "").replace(":", "")
                           .strip()) <= {"-", " "}:
                        continue
                    if ncols(row) != header_cols:
                        issues.append((ln, header_cols, ncols(row), row.strip()[:80]))
        else:
            i += 1
    return issues


# 401/403/405/429 and TLS-trust failures are environmental (bot blocks, throttling,
# client cert store) — flag for manual review, not as a hard "dead link".
BLOCKED_CODES = {401, 403, 405, 429}


def check_urls(text, timeout=12):
    """Criterion 3: every source URL resolves. GitHub blob/main links are checked
    against the local working tree (URL-decoded; they 404 until a PR merges); external
    URLs are HTTP-checked. Returns (urls, dead, blocked, local_missing)."""
    urls = sorted(set(MD_LINK_RE.findall(text)))
    dead, blocked, local_missing = [], [], []
    ua = "Mozilla/5.0 (findings-audit; +https://github.com/none-below/sm-alpr)"
    for url in urls:
        m = GITHUB_BLOB_RE.match(url)
        if m:
            rel = unquote(m.group(1))  # %20 -> space; files on disk use literal spaces
            if not (REPO_ROOT / rel).exists():
                local_missing.append((url, rel))
            continue
        category, detail = _http_status(url, ua, timeout)
        if category == "dead":
            dead.append((url, detail))
        elif category == "blocked":
            blocked.append((url, detail))
        time.sleep(0.3)  # be polite
    return urls, dead, blocked, local_missing


def _classify_code(code):
    # 401/403/405/429 = bot-block/throttle; 5xx = server-side transient. Both -> manual.
    if code in BLOCKED_CODES or code >= 500:
        return "blocked", f"HTTP {code}"
    return "dead", f"HTTP {code}"  # 404/410/other 4xx = genuinely dead


def _http_status(url, ua, timeout):
    """Return ('ok'|'dead'|'blocked', detail). HEAD only short-circuits on success;
    a full GET is authoritative for any negative (many servers 404/405 a HEAD but
    200 a GET). 'dead' = real 404/gone/no-host; 'blocked' = throttle/block/TLS/5xx."""
    last = ("dead", "unreachable")
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                code = resp.getcode() or 0
                if code < 400:
                    return "ok", str(code)
                last = _classify_code(code)
        except urllib.error.HTTPError as e:
            last = _classify_code(e.code)
        except ssl.SSLError as e:
            last = ("blocked", f"TLS: {getattr(e, 'reason', e)}")
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLError) or "CERTIFICATE" in str(e.reason):
                last = ("blocked", f"TLS: {e.reason}")
            else:
                last = ("dead", f"{type(e.reason).__name__}: {e.reason}")
        except Exception as e:  # noqa: BLE001
            last = ("dead", f"{type(e).__name__}: {e}")
        if method == "GET":
            return last  # GET result is authoritative
    return last


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("doc", nargs="?", default=str(DEFAULT_DOC))
    ap.add_argument("--check-urls", action="store_true", help="criterion 3 (network)")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    doc = Path(args.doc)
    if not doc.exists():
        print(f"ERROR: {doc} not found", file=sys.stderr)
        return 1
    text = doc.read_text(encoding="utf-8")
    sections = list(split_sections(text))

    sourceless, compound = check_bullets(sections)
    table_issues = check_tables(text)
    urls, dead, blocked, local_missing = ([], [], [], [])
    if args.check_urls:
        urls, dead, blocked, local_missing = check_urls(text)

    report = {
        "criterion_1_sourceless_bullets": [
            {"line": ln, "section": h, "text": t} for ln, h, t in sourceless],
        "criterion_1_possibly_compound": [
            {"line": ln, "section": h, "sentences": ns, "refs": nr, "text": t}
            for ln, h, ns, nr, t in compound],
        "criterion_6_ragged_tables": [
            {"line": ln, "header_cols": hc, "row_cols": rc, "row": r}
            for ln, hc, rc, r in table_issues],
        "criterion_3_dead_urls": [{"url": u, "detail": d} for u, d in dead],
        "criterion_3_blocked_urls": [{"url": u, "detail": d} for u, d in blocked],
        "criterion_3_missing_local_blob": [
            {"url": u, "path": p} for u, p in local_missing],
        "criterion_3_urls_checked": len(urls),
    }

    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report, args.check_urls)

    # Hard failures are STRUCTURAL: ragged tables, dead links, missing in-repo files.
    # Sourceless / compound bullets are advisories — lead-ins and synthesis nudges
    # legitimately lack their own [N], so they warrant human review, not a build break.
    hard = table_issues or dead or local_missing
    advisories = len(sourceless) + len(compound)
    if not args.as_json and advisories:
        print(f"\n{advisories} advisory item(s) for review (not build failures).")
    return 1 if hard else 0


def _print_report(r, checked_urls):
    def section(title, items):
        print(f"\n{title}: {len(items)}")
        for it in items[:50]:
            print("  - " + json.dumps(it, ensure_ascii=False))

    print("audit_findings — automated criteria (1 presence, 3 reachability, 6 tables)")
    section("[1] sourceless body bullets (no [N] / (see §N))",
            r["criterion_1_sourceless_bullets"])
    section("[1] possibly-compound bullets (review for singularity)",
            r["criterion_1_possibly_compound"])
    section("[6] ragged tables (column-count mismatch)", r["criterion_6_ragged_tables"])
    if checked_urls:
        section("[3] DEAD external URLs (404/gone — fix these)", r["criterion_3_dead_urls"])
        section("[3] in-repo blob links missing locally", r["criterion_3_missing_local_blob"])
        section("[3] blocked/throttled URLs (403/429/TLS — manual check, not failures)",
                r["criterion_3_blocked_urls"])
        print(f"\n[3] external+blob URLs checked: {r['criterion_3_urls_checked']}")
    else:
        print("\n[3] URL reachability skipped (pass --check-urls)")
    print("\n[2] quote/source/CONTENT fidelity, [4] URL<->description, [5] Required->Say->Do:")
    print("    LLM-review criteria — run the companion adversarial workflow. [2] reads each")
    print("    source and fact-checks the bullet's claim (not just the pointer); use curated")
    print("    article shards / JSON / PDF only, never raw scraped .html/.txt.")


if __name__ == "__main__":
    sys.exit(main())
