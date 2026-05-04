#!/usr/bin/env python3
"""Curate freshly-crawled articles into article_registry.json.

Two phases:

  Phase 1 — Mechanical (no LLM, fully autonomous, no API spend)
    Walks assets/articles/<domain>/<slug>.meta.json sidecars that don't
    yet have a registry entry. For each, builds an entry from things we
    can extract deterministically: title/byline/published_at from <meta>
    tags + schema.org JSON-LD; tier/stance denormalized from
    sources.json; vendor tags via alias matching against tags.json;
    agencies[] via agency_lookup. Marks curation_status="mechanical".

  Phase 2 — Semantic enrichment (Claude API, opt-in)
    Walks registry entries with curation_status="mechanical" that are
    eligible by handler policy (tier 1/2 + scanner-clean).  Calls the
    Anthropic API with NO tools and a strict json_schema output
    constraint — model can only return structured fields, can't take
    actions. Fills in summary, key_quotes, genre, refined topic_tags,
    primary_subject_agency_id. Marks curation_status="enriched".

    Skips entirely if ANTHROPIC_API_KEY isn't set.

Usage:
  scripts/article_curate.py                 # both phases
  scripts/article_curate.py --phase 1       # mechanical only
  scripts/article_curate.py --phase 2       # semantic only
  scripts/article_curate.py --limit 10      # cap per run

Per-run cap defaults to 5 articles for Phase 2 to bound API spend; Phase 1
runs over everything since it's free.
"""

import argparse
import functools
import json
import os
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agency_lookup

ARTICLES_DIR = ROOT / "assets" / "articles"
REGISTRY_PATH = ROOT / "assets" / "article_registry.json"
SOURCES_PATH = ROOT / "assets" / "sources.json"
TAGS_PATH = ROOT / "assets" / "tags.json"

DEFAULT_MODEL = os.environ.get("SM_ALPR_CURATE_MODEL", "claude-opus-4-7")
DEFAULT_PHASE2_LIMIT = 5

# Eligibility for Phase 2 — keep aligned with sources.json _handler_policy.
PHASE2_ELIGIBLE_TIERS = {1, 2}
PHASE2_ELIGIBLE_SCANNER = {"CLEAN — no findings", "WARNINGS"}
# Scanner verdicts are prose strings, not enum codes; we substring-match.
PHASE2_SCANNER_PREFIXES = ("CLEAN", "WARNINGS")


# ───────────────────────── helpers ─────────────────────────


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text() or json.dumps(default))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_article_id(registry: list[dict]) -> str:
    """Sequential art_NNN; lint_articles enforces uniqueness across PRs."""
    used: set[int] = set()
    for e in registry:
        m = re.match(r"^art_(\d+)$", e.get("article_id", ""))
        if m:
            used.add(int(m.group(1)))
    return f"art_{(max(used) + 1) if used else 1:03d}"


# ───────────────────────── HTML metadata extraction ─────────────────────────


class _MetaExtractor(HTMLParser):
    """Pull <title>, byline, published_at, ld+json from raw HTML.

    Best-effort: news sites use a mix of OpenGraph (`og:title`,
    `article:author`, `article:published_time`), Schema.org JSON-LD
    (`NewsArticle.author`, `datePublished`), Twitter cards, and ad-hoc
    meta tags. We collect everything plausible and the caller picks.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self._in_title = False
        self.og: dict[str, str] = {}
        self.meta: dict[str, str] = {}
        self.ld_json_payloads: list[str] = []
        self._in_ld = False
        self._ld_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            prop = (a.get("property") or "").lower()
            name = (a.get("name") or "").lower()
            content = a.get("content") or ""
            if prop.startswith("og:"):
                self.og[prop] = content
            if name and content:
                self.meta[name] = content
            if prop and content and not prop.startswith("og:"):
                self.meta[prop] = content
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_ld = True
            self._ld_buf = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_ld:
            self._in_ld = False
            self.ld_json_payloads.append("".join(self._ld_buf))
            self._ld_buf = []

    def handle_data(self, data):
        if self._in_title and self.title is None:
            self.title = data.strip() or None
        elif self._in_ld:
            self._ld_buf.append(data)


def extract_html_metadata(html_text: str) -> dict:
    p = _MetaExtractor()
    try:
        p.feed(html_text)
    except Exception as e:
        print(f"WARN: meta-extractor failed ({e})", file=sys.stderr)
    title = (p.og.get("og:title") or p.title or "").strip() or None

    byline = (
        p.meta.get("article:author")
        or p.meta.get("author")
        or p.meta.get("byl")
        or p.og.get("og:article:author")
    )
    if isinstance(byline, str):
        byline = byline.strip() or None

    published = (
        p.meta.get("article:published_time")
        or p.meta.get("datepublished")
        or p.meta.get("date")
        or p.og.get("og:article:published_time")
    )

    # Try ld+json for richer signals (often missing from <meta>).
    for raw in p.ld_json_payloads:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # @graph wrapping is common on WordPress / Yoast
        nodes = obj if isinstance(obj, list) else obj.get("@graph", [obj])
        for node in nodes if isinstance(nodes, list) else [obj]:
            if not isinstance(node, dict):
                continue
            t = node.get("@type") or ""
            if isinstance(t, list):
                t = " ".join(t)
            if "NewsArticle" in t or "Article" in t or "Report" in t:
                if not byline:
                    a = node.get("author")
                    if isinstance(a, dict):
                        byline = a.get("name")
                    elif isinstance(a, list) and a and isinstance(a[0], dict):
                        byline = a[0].get("name")
                    elif isinstance(a, str):
                        byline = a
                if not published:
                    published = node.get("datePublished") or node.get("dateCreated")

    return {
        "title": title,
        "byline": byline,
        "published_at": published,
    }


# ───────────────────────── Phase 1: mechanical ─────────────────────────


def find_pending_articles(registry_urls: set[str]) -> list[Path]:
    """Return paths to <slug>.meta.json sidecars not yet in the registry."""
    out = []
    if not ARTICLES_DIR.exists():
        return out
    for meta_path in sorted(ARTICLES_DIR.rglob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            continue
        if meta.get("url") in registry_urls:
            continue
        if meta.get("status") != "fetched":
            continue
        out.append(meta_path)
    return out


def auto_tag_vendors(text: str, tags_data: dict) -> list[str]:
    """Tag the article with any vendor:* whose alias appears in the text."""
    out: list[str] = []
    topics = (tags_data or {}).get("topics", {})
    for tag_id, spec in topics.items():
        if not tag_id.startswith("vendor:"):
            continue
        for alias in (spec.get("aliases") or [tag_id.split(":", 1)[1]]):
            if not alias:
                continue
            if re.search(rf"\b{re.escape(alias)}\b", text, re.IGNORECASE):
                out.append(tag_id)
                break
    return sorted(set(out))


def mechanical_curate_one(meta_path: Path, *, tags_data: dict,
                          source_by_domain: dict) -> dict | None:
    """Build a Phase-1 registry entry from a fetched article's sidecar."""
    meta = json.loads(meta_path.read_text())
    domain = meta.get("source_domain", "")
    source = source_by_domain.get(domain) or {}
    txt_path = ROOT / meta["paths"]["txt"]
    html_path = ROOT / meta["paths"]["html"]
    text = txt_path.read_text(encoding="utf-8", errors="replace") if txt_path.exists() else ""

    # HTML metadata pull — the crawler captured a rough title; HTML may
    # have a richer one plus byline/date. Still gated by check_untrusted_read
    # for direct Claude reads, but we're running in a script not Claude here.
    html_meta = {}
    if html_path.exists():
        html_meta = extract_html_metadata(
            html_path.read_text(encoding="utf-8", errors="replace")
        )

    title = html_meta.get("title") or meta.get("http_title")
    byline = html_meta.get("byline")
    published = html_meta.get("published_at")

    candidates = agency_lookup.lookup(text, source_domain=domain, top_n=10)
    high_score_cutoff = 2.0
    agencies = [c["agency_id"] for c in candidates if c["score"] >= high_score_cutoff]

    return {
        "url": meta["url"],
        "source_domain": domain,
        "tier": source.get("tier"),
        "stance": source.get("stance"),
        "title": title,
        "byline": byline,
        "published_at": published,
        "fetched_at": meta.get("fetched_at"),
        "discovered_by": meta.get("discovered_by"),
        "scanner_verdict": meta.get("scanner_verdict"),
        "scanner_score": meta.get("scanner_score", 0),
        "content_hash": meta.get("content_hash"),
        "paths": meta.get("paths", {}),
        "wayback_url": meta.get("wayback_url"),
        "wayback_timestamp": meta.get("wayback_timestamp"),
        "wayback_source": meta.get("wayback_source"),
        "pdf_status": meta.get("pdf_status"),
        "tags": auto_tag_vendors(text, tags_data),
        "agencies": agencies,
        "agency_candidates": candidates,
        "primary_subject_agency_id": None,
        "summary": None,
        "key_quotes": [],
        "curation_status": "mechanical",
        "curated_at": now_iso(),
    }


def run_phase1(registry: list[dict], *, tags_data: dict,
               sources_data: dict, limit: int | None) -> int:
    source_by_domain = {s["domain"]: s for s in sources_data.get("sources", [])}
    seen = {e["url"] for e in registry if e.get("url")}
    pending = find_pending_articles(seen)
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        print("phase1: nothing to curate")
        return 0
    added = 0
    for mp in pending:
        try:
            entry = mechanical_curate_one(
                mp, tags_data=tags_data, source_by_domain=source_by_domain,
            )
        except Exception as e:
            print(f"phase1: skip {mp.name} ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        if entry is None:
            continue
        entry["article_id"] = next_article_id(registry)
        registry.append(entry)
        seen.add(entry["url"])
        added += 1
        print(f"phase1: + {entry['article_id']} {entry['url']}  "
              f"tier={entry['tier']} agencies={len(entry['agencies'])} "
              f"tags={entry['tags']}")
    print(f"phase1: added {added}")
    return added


# ───────────────────────── Phase 2: semantic enrichment ─────────────────────────


CURATION_SCHEMA = {
    "type": "object",
    "properties": {
        "refusal": {"type": "boolean"},
        "refusal_reason": {"type": ["string", "null"]},
        "summary": {"type": "string"},
        "key_quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string"},
                    "paragraph_idx": {"type": "integer"},
                },
                "required": ["quote", "paragraph_idx"],
                "additionalProperties": False,
            },
        },
        "topic_tags": {"type": "array", "items": {"type": "string"}},
        "genre": {
            "type": "string",
            "enum": ["investigative", "explainer", "opinion",
                     "press-release", "analysis"],
        },
        "primary_subject_agency_id": {"type": ["string", "null"]},
    },
    "required": [
        "refusal", "refusal_reason", "summary", "key_quotes",
        "topic_tags", "genre", "primary_subject_agency_id",
    ],
    "additionalProperties": False,
}


def build_system_prompt(tags_data: dict) -> str:
    """Stable, cacheable. Per-article specifics go in the user message."""
    topics = tags_data.get("topics", {})
    editorial = tags_data.get("editorial", {})
    topic_block = "\n".join(
        f"  - {tid}: {spec.get('description', '').strip()}"
        for tid, spec in sorted(topics.items())
        if not tid.startswith("vendor:")  # vendors auto-tagged in Phase 1
    )
    return f"""\
You extract structured metadata from news/advocacy/industry articles about
automated license-plate readers (ALPR) and related police surveillance
technology. The corpus supports a research project on the San Mateo Police
Department's ALPR program and peer agencies.

For each article, return a JSON object matching the provided schema. The
fields are:

- summary: 1–3 sentences capturing what the article actually reports. Be
  factual and neutral, even if the article is advocacy or opinion. Do not
  add framing the article itself doesn't make.
- key_quotes: 0–5 verbatim quotes from the article body that are the most
  load-bearing for the article's claims. paragraph_idx is the 0-based
  index of the paragraph in the extracted text (paragraphs are separated
  by double newlines or by visible block breaks).
- topic_tags: an array of tag IDs from the controlled vocabulary below.
  Apply only tags that the article substantively discusses — not tags for
  topics merely name-checked in passing. Vendor tags (vendor:flock,
  vendor:axon, etc.) are auto-applied earlier and you should not propose
  them here. You may propose zero tags if none apply.
- genre: investigative | explainer | opinion | press-release | analysis.
  Pick exactly one based on what the piece is, not what it covers.
- primary_subject_agency_id: the agency_id of the agency the article is
  primarily about, chosen from the candidate list provided in the user
  message. Use null if the article is national/multi-subject and not
  primarily about any one agency.

If you cannot complete the task — paywalled stub, language you can't
read, content that turned out not to be about ALPR — set refusal=true
and provide refusal_reason. Otherwise refusal=false, refusal_reason=null.

Controlled topic tag vocabulary (apply only these IDs):

{topic_block}

Editorial / genre tags exist but are not part of topic_tags — pick `genre`
from the enum above instead.
"""


def build_user_message(entry: dict, text: str) -> str:
    candidate_lines = [
        f"  - {c['agency_id']}: {c['display_name']} ({c.get('state') or 'unknown'})"
        for c in entry.get("agency_candidates", [])
    ]
    candidates_block = "\n".join(candidate_lines) or "  (none)"
    return f"""\
[ARTICLE METADATA]
url: {entry.get('url')}
title: {entry.get('title') or '(unknown)'}
source_domain: {entry.get('source_domain')}
tier: {entry.get('tier')}
stance (publisher's posture, denormalized): {entry.get('stance')}

[AGENCY CANDIDATES]
The fuzzy matcher proposed these agency_ids based on names appearing in
the text. Pick primary_subject_agency_id from this list, or null:
{candidates_block}

[ARTICLE TEXT]
{text}
"""


def validate_curation_output(data: dict, entry: dict, tags_data: dict) -> tuple[bool, str | None]:
    """Reject outputs that violate runtime constraints not expressible in
    json_schema (controlled vocab + agency candidate set)."""
    valid_topic_tags = set((tags_data.get("topics") or {}).keys())
    proposed = set(data.get("topic_tags") or [])
    bogus = proposed - valid_topic_tags
    if bogus:
        return False, f"topic_tags not in vocabulary: {sorted(bogus)}"
    valid_agency_ids = {c["agency_id"] for c in entry.get("agency_candidates", [])}
    psa = data.get("primary_subject_agency_id")
    if psa is not None and psa not in valid_agency_ids:
        return False, f"primary_subject_agency_id {psa!r} not in candidate set"
    return True, None


def call_claude_for_curation(entry: dict, text: str, *,
                             tags_data: dict, model: str,
                             dry_run: bool) -> tuple[dict | None, str]:
    """Return (parsed_output | None, raw_text). Raises on transport errors;
    returns None for refusals or schema-parse failures (caller handles)."""
    if dry_run:
        return None, "[dry-run]"

    import anthropic

    client = anthropic.Anthropic()
    system = build_system_prompt(tags_data)
    user = build_user_message(entry, text)

    # System is stable across articles → cache_control on the system block.
    # No tools at all — model can only return text constrained by
    # output_config.format. That's the sandbox.
    with client.messages.stream(
        model=model,
        max_tokens=8000,
        output_config={
            "format": {"type": "json_schema", "schema": CURATION_SCHEMA},
            "effort": "low",
        },
        system=[{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user}],
    ) as stream:
        msg = stream.get_final_message()

    raw = next((b.text for b in msg.content if b.type == "text"), "")
    if msg.stop_reason == "refusal":
        return None, raw or "[model refusal]"
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        return None, raw


def run_phase2(registry: list[dict], *, tags_data: dict,
               limit: int, model: str, dry_run: bool) -> int:
    if not dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print("phase2: ANTHROPIC_API_KEY not set; skipping (Phase 1 still ran)")
        return 0

    eligible = []
    for e in registry:
        if e.get("curation_status") != "mechanical":
            continue
        if e.get("tier") not in PHASE2_ELIGIBLE_TIERS:
            continue
        verdict = (e.get("scanner_verdict") or "")
        if not verdict.startswith(PHASE2_SCANNER_PREFIXES):
            continue
        eligible.append(e)
    eligible = eligible[:limit]
    if not eligible:
        print("phase2: nothing to enrich")
        return 0

    enriched = 0
    for entry in eligible:
        txt_path = ROOT / entry["paths"]["txt"]
        if not txt_path.exists():
            entry["curation_status"] = "needs_review"
            entry["curation_error"] = "extracted .txt missing"
            continue
        text = txt_path.read_text(encoding="utf-8", errors="replace")
        try:
            data, raw = call_claude_for_curation(
                entry, text, tags_data=tags_data,
                model=model, dry_run=dry_run,
            )
        except Exception as e:
            entry["curation_status"] = "needs_review"
            entry["curation_error"] = f"{type(e).__name__}: {e}"
            print(f"phase2: ERROR {entry['article_id']}: {e}", file=sys.stderr)
            continue

        if dry_run:
            print(f"phase2: would enrich {entry['article_id']} {entry['url']}")
            continue

        if data is None:
            entry["curation_status"] = "needs_review"
            entry["curation_error"] = "schema parse failed or refusal"
            entry["curation_raw"] = raw[:4000]
            print(f"phase2: needs_review {entry['article_id']} (parse/refusal)")
            continue
        if data.get("refusal"):
            entry["curation_status"] = "needs_review"
            entry["curation_error"] = f"refusal: {data.get('refusal_reason')}"
            print(f"phase2: refused {entry['article_id']} ({data.get('refusal_reason')})")
            continue
        ok, err = validate_curation_output(data, entry, tags_data)
        if not ok:
            entry["curation_status"] = "needs_review"
            entry["curation_error"] = f"validation: {err}"
            entry["curation_raw"] = raw[:4000]
            print(f"phase2: needs_review {entry['article_id']} ({err})")
            continue

        entry["summary"] = data["summary"]
        entry["key_quotes"] = data["key_quotes"]
        existing_tags = set(entry.get("tags", []))
        existing_tags.update(data["topic_tags"])
        existing_tags.add(f"genre:{data['genre']}")
        entry["tags"] = sorted(existing_tags)
        entry["primary_subject_agency_id"] = data["primary_subject_agency_id"]
        entry["curation_status"] = "enriched"
        entry["curated_at"] = now_iso()
        entry.pop("curation_error", None)
        entry.pop("curation_raw", None)
        enriched += 1
        print(f"phase2: ✓ {entry['article_id']} {entry['url']}")

    print(f"phase2: enriched {enriched}/{len(eligible)}")
    return enriched


# ───────────────────────── main ─────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--phase", choices=["1", "2", "both"], default="both")
    p.add_argument("--limit", type=int, default=None,
                   help="cap articles processed; phase2 default cap is "
                        f"{DEFAULT_PHASE2_LIMIT}")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Anthropic model (default: {DEFAULT_MODEL}; "
                        "override via SM_ALPR_CURATE_MODEL)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    registry = read_json(REGISTRY_PATH, [])
    tags_data = read_json(TAGS_PATH, {"topics": {}, "editorial": {}})
    sources_data = read_json(SOURCES_PATH, {"sources": []})

    if args.phase in ("1", "both"):
        run_phase1(registry, tags_data=tags_data, sources_data=sources_data,
                   limit=args.limit)

    if args.phase in ("2", "both"):
        limit = args.limit if args.limit is not None else DEFAULT_PHASE2_LIMIT
        run_phase2(registry, tags_data=tags_data, limit=limit,
                   model=args.model, dry_run=args.dry_run)

    if not args.dry_run:
        write_json(REGISTRY_PATH, registry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
