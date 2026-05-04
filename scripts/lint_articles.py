#!/usr/bin/env python3
"""Lint assets/article_registry.json for structural integrity.

Parallel PRs adding article entries can collide on art_NNN IDs without
touching the same line. CI runs this on every PR. The same shape as
scripts/lint_findings.py — fail closed on any error, list every error,
exit 1.

Checks:
  - article_id format (art_NNN) and uniqueness
  - URL uniqueness
  - source_domain present and listed in assets/sources.json
  - agencies[] entries resolve to assets/agency_registry.json agency_ids
  - primary_subject_agency_id (if set) appears in this entry's agencies[]
  - tags appear in assets/tags.json (topics + editorial vocabulary)
  - curation_status in known set
  - paths.{html,txt,meta} relative paths exist on disk

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

        psa = entry.get("primary_subject_agency_id")
        if psa is not None:
            if valid_agency_ids and psa not in valid_agency_ids:
                errors.append(
                    f"{prefix}: primary_subject_agency_id {psa!r} not in agency registry"
                )
            elif psa not in (entry.get("agencies") or []):
                errors.append(
                    f"{prefix}: primary_subject_agency_id {psa!r} not in this entry's agencies[]"
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

    if errors:
        print(f"lint_articles: {len(errors)} error(s) in {path}:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"lint_articles: OK — {len(registry)} entries, {len(seen_ids)} unique IDs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
