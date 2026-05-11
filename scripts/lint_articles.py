#!/usr/bin/env python3
"""Lint assets/article_registry.json for structural integrity.

Parallel PRs adding article entries can collide on art_NNN IDs without
touching the same line. CI runs this on every PR. The same shape as
scripts/lint_findings.py — fail closed on any error, list every error,
exit 1.

Checks (errors — exit 1):
  - article_id format (art_NNN) and uniqueness
  - URL uniqueness
  - source_domain present and listed in assets/sources.json
  - agencies[] entries resolve to assets/agency_registry.json agency_ids
  - primary_subject_agency_ids entries each appear in this entry's agencies[]
    (and are unique within the list)
  - tags appear in assets/tags.json (topics + editorial vocabulary)
  - curation_status in known set
  - paths.{html,txt,meta} relative paths exist on disk

Warnings (don't fail — just printed for visibility):
  - title or summary mentions a contract-action keyword (terminate,
    shut off, paused, rejected, rescinded, deactivated, not renewed,
    not awarded, etc.) but no outcome:* tag is applied. Surfaces
    curator drift after a prompt change.

Usage:
  python scripts/lint_articles.py            # check the canonical registry
  python scripts/lint_articles.py PATH       # check an alternate path
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "assets" / "article_registry.json"
SOURCES = ROOT / "assets" / "sources.json"
TAGS = ROOT / "assets" / "tags.json"
AGENCY_REGISTRY = ROOT / "assets" / "agency_registry.json"

ARTICLE_ID_RE = re.compile(r"^art_\d{3,}$")
KNOWN_STATUSES = {"mechanical", "enriched", "needs_review"}

# Word-boundary regex hits suggesting the article reports a contract
# action — terminating, turning off, pausing, declining renewal, etc.
# An enriched entry whose title or summary matches any of these but has
# no outcome:* tag gets a warning.
#
# We keep ambiguous verbs (cancel, reject, decline, drop) anchored to
# contract/program/pilot/camera context so we don't flag "court rejected
# the argument" or "drop in stock". Stronger verbs (terminat/rescind/
# deactivat/turn-off/shut-down) fire on their own — those rarely appear
# outside the contract-action sense.
_OBJ = r"(?:contract|program|pilot|agreement|cameras?|alpr|flock|deal|ALPRs?)"
CONTRACT_ACTION_RES = tuple(re.compile(p, re.I) for p in (
    r"\bterminat\w*\b",
    r"\brescind\w*\b",
    r"\bdeactivat\w*\b",
    r"\b(?:turn|turns|turned|turning)\s+(?:its\s+\w+\s+)?off\b",
    r"\b(?:shut|shuts|shutting)\s+(?:its\s+\w+\s+)?(?:off|down)\b",
    rf"\b{_OBJ}[\w\s]{{0,20}}?(?:was|were|got|is|are|been|to\s+be)?\s*"
    r"(?:paus|suspend)\w*\b",
    rf"\b(?:paus|suspend)\w*\s+(?:its\s+\w+\s+|the\s+)?{_OBJ}\b",
    r"\bnot\s+renew(?:ed|ing)?\b",
    r"\bnot\s+awarded\b",
    r"\bnot\s+activated\b",
    rf"\b{_OBJ}\s+(?:has\s+|have\s+)?(?:ended|ends|expir\w*|laps\w*)\b",
    r"\b(?:won[’']t|will\s+not|wouldn[’']t)\s+renew\b",
    rf"\blet\s+(?:its\s+|the\s+)?{_OBJ}\s+laps\w*\b",
    rf"\b{_OBJ}\s+\w*\s*(?:reject\w*|declin\w*|cancel\w*)\b",
    rf"\b(?:reject\w*|declin\w*|cancel\w*)\s+(?:its\s+|the\s+)?{_OBJ}\b",
    r"\b(?:cameras?|alpr)\s+(?:were\s+|are\s+|was\s+|will\s+be\s+"
    r"|to\s+be\s+)?removed\b",
    rf"\bdrop(?:ped|ping|s)?\s+(?:its\s+|the\s+)?{_OBJ}\b",
    r"\bprohibit\w*\s+(?:from\s+)?us(?:e|ing)\s+(?:flock|alpr)",
))


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else REGISTRY
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 1

    try:
        registry = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: {path} not valid JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(registry, list):
        print(f"ERROR: {path} top-level must be a list", file=sys.stderr)
        return 1

    if SOURCES.exists():
        sources = json.loads(SOURCES.read_text()).get("sources", [])
        valid_domains = {s["domain"].lower() for s in sources}
    else:
        valid_domains = set()

    if TAGS.exists():
        tags_doc = json.loads(TAGS.read_text())
        valid_tags = set((tags_doc.get("topics") or {}).keys())
        valid_tags.update((tags_doc.get("editorial") or {}).keys())
        # Allow `genre:investigative` etc. that get auto-applied by Phase 2.
        for genre in ("investigative", "explainer", "opinion",
                      "press-release", "analysis"):
            valid_tags.add(f"genre:{genre}")
    else:
        valid_tags = set()

    if AGENCY_REGISTRY.exists():
        valid_agency_ids = {e["agency_id"]
                            for e in json.loads(AGENCY_REGISTRY.read_text())
                            if "agency_id" in e}
    else:
        valid_agency_ids = set()

    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: dict[str, int] = {}
    seen_urls: dict[str, int] = {}

    for i, entry in enumerate(registry):
        prefix = f"entry[{i}] (art_id={entry.get('article_id', '?')})"

        aid = entry.get("article_id")
        if not aid:
            errors.append(f"{prefix}: missing article_id")
        elif not ARTICLE_ID_RE.match(aid):
            errors.append(f"{prefix}: article_id {aid!r} doesn't match art_NNN")
        elif aid in seen_ids:
            errors.append(
                f"{prefix}: duplicate article_id (also at entry[{seen_ids[aid]}])"
            )
        else:
            seen_ids[aid] = i

        url = entry.get("url")
        if not url:
            errors.append(f"{prefix}: missing url")
        elif url in seen_urls:
            errors.append(
                f"{prefix}: duplicate url (also at entry[{seen_urls[url]}])"
            )
        else:
            seen_urls[url] = i

        domain = (entry.get("source_domain") or "").lower()
        if not domain:
            errors.append(f"{prefix}: missing source_domain")
        elif valid_domains and domain not in valid_domains:
            errors.append(f"{prefix}: source_domain {domain!r} not in sources.json")

        for tag in entry.get("tags", []) or []:
            if valid_tags and tag not in valid_tags:
                errors.append(f"{prefix}: unknown tag {tag!r}")

        for aid_ref in entry.get("agencies", []) or []:
            if valid_agency_ids and aid_ref not in valid_agency_ids:
                errors.append(
                    f"{prefix}: agencies[] references unknown agency_id {aid_ref!r}"
                )

        psa_ids = entry.get("primary_subject_agency_ids") or []
        agencies_set = set(entry.get("agencies") or [])
        seen_psa: set[str] = set()
        for psa in psa_ids:
            if psa in seen_psa:
                errors.append(
                    f"{prefix}: primary_subject_agency_ids has duplicate {psa!r}"
                )
                continue
            seen_psa.add(psa)
            if valid_agency_ids and psa not in valid_agency_ids:
                errors.append(
                    f"{prefix}: primary_subject_agency_ids entry {psa!r} not in agency registry"
                )
            elif psa not in agencies_set:
                errors.append(
                    f"{prefix}: primary_subject_agency_ids entry {psa!r} not in this entry's agencies[]"
                )

        status = entry.get("curation_status")
        if status and status not in KNOWN_STATUSES:
            errors.append(
                f"{prefix}: unknown curation_status {status!r}; "
                f"expected one of {sorted(KNOWN_STATUSES)}"
            )

        paths = entry.get("paths") or {}
        for kind in ("html", "txt", "meta"):
            rel = paths.get(kind)
            if not rel:
                continue
            if (ROOT / rel).exists():
                continue
            errors.append(f"{prefix}: paths.{kind} missing on disk: {rel}")

        # Curator drift check: contract-action language with no outcome:* tag.
        # Skip mechanical entries — they don't have a summary yet and Phase 2
        # is what adds outcome:* tags. Once an entry is enriched, an action
        # mention without outcome:* is a real miss.
        if entry.get("curation_status") == "enriched":
            tags = entry.get("tags") or []
            has_outcome = any(t.startswith("outcome:") for t in tags)
            if not has_outcome:
                blob = " ".join(filter(None, [
                    (entry.get("title") or ""),
                    (entry.get("summary") or ""),
                ]))
                m = next((cre.search(blob) for cre in CONTRACT_ACTION_RES
                          if cre.search(blob)), None)
                if m:
                    warnings.append(
                        f"{prefix}: contract-action language "
                        f"({m.group(0)!r}) but no outcome:* tag — "
                        f"title={entry.get('title')!r}"
                    )

    if warnings:
        print(f"lint_articles: {len(warnings)} warning(s) in {path}:")
        for w in warnings:
            print(f"  ⚠ {w}")

    if errors:
        print(f"lint_articles: {len(errors)} error(s) in {path}:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"lint_articles: OK — {len(registry)} entries, "
          f"{len(seen_ids)} unique IDs, {len(warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
