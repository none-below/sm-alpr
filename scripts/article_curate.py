#!/usr/bin/env python3
"""Curate freshly-crawled articles into the assets/article_registry/ shards.

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
    primary_subject_agency_ids. Marks curation_status="enriched".

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
import hashlib
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
import article_store

ARTICLES_DIR = ROOT / "assets" / "articles"
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


def article_id_for_url(url: str) -> str:
    """Stable, collision-free article id derived from the URL.

    Hash-based rather than a max(n)+1 counter: every branch derives the
    SAME id for a given URL and DIFFERENT ids for different URLs, so two
    PRs curating concurrently can't mint the same id the way the sequential
    counter did (each branch read the same max from its own snapshot, picked
    the same next number, and merged without a git conflict — the collision
    only surfaced in lint after the fact). Also idempotent: re-crawling a
    URL reuses its id instead of spawning a second entry. Mirrors the
    sha256(url)[:8] filename convention in scripts/article_crawl.py
    (url_filename); 12 hex chars (~48 bits) keep birthday-collision odds
    negligible at corpus scale. Legacy art_NNN ids are grandfathered by
    lint_articles.ARTICLE_ID_RE — minting changes, existing ids don't.
    """
    return "art_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


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
            itemprop = (a.get("itemprop") or "").lower()
            content = a.get("content") or ""
            if prop.startswith("og:"):
                self.og[prop] = content
            if name and content:
                self.meta[name] = content
            if prop and content and not prop.startswith("og:"):
                self.meta[prop] = content
            # Schema.org microdata (smdailyjournal and other Townnews
            # sites use <meta itemprop="datePublished" content="...">
            # instead of OpenGraph/article:published_time).
            if itemprop and content:
                self.meta[itemprop] = content
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
        "primary_subject_agency_ids": [],
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
        entry["article_id"] = article_id_for_url(entry["url"])
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
        "off_topic": {"type": "boolean"},
        "off_topic_reason": {"type": ["string", "null"]},
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
        "primary_subject_agency_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "refusal", "refusal_reason", "off_topic", "off_topic_reason",
        "summary", "key_quotes", "topic_tags", "genre",
        "primary_subject_agency_ids",
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

  IMPORTANT — outcome:* tags are orthogonal to the article's primary
  frame. When the article SUBSTANTIVELY REPORTS a specific agency
  taking a specific contract action — turning off cameras, canceling,
  pausing, rejecting (a proposal), declining renewal, letting a
  contract lapse, deactivating, removing, prohibiting use, etc. —
  apply the matching outcome:* tag EVEN IF the article's primary
  frame is audit, policy, legal, or incident. An article routinely
  carries multiple frames at once: policy:audit AND
  outcome:terminated; policy:sharing AND outcome:restricted;
  incident:ice-cooperation AND outcome:terminated;
  legal:legislation AND outcome:rejected. These are different axes —
  do not let one suppress the other.

  POSITIVE example — apply outcome:terminated: KQED's "As California
  Cities Grow Wary of Flock Safety Cameras, Mountain View Shuts Its
  Off" reports that the Mountain View Police Department turned off
  its Flock cameras after an audit revealed unauthorized federal and
  out-of-state access. Correct topic_tags include policy:audit AND
  policy:sharing AND incident:ice-cooperation AND outcome:terminated —
  the cameras-off action is the contract-action core of the story.

  NEGATIVE examples — do NOT apply outcome:* to:
    • A court ruling that ALPR data is a public record. The court is
      rejecting a legal argument, not terminating a contract. Even
      if the article names cities that have separately turned off
      cameras, the article isn't reporting THOSE actions — it's
      reporting the ruling. Tag legal:court-ruling, not outcome:*.
    • ANY article that reports actions by 2+ agencies — year-in-
      review, roundup, "growing pushback" survey, ACLU/EFF "state
      of play" piece, advocacy commentary, OR cross-cutting
      investigative reporting that covers multiple agencies'
      decisions in depth. The bright-line rule: outcome:* tags fire
      ONLY when the article reports ONE specific agency's action
      as its primary news, AND that agency is also the
      primary_subject_agency_id. Multi-agency articles get
      policy:*, scope:*, and genre tags only — never outcome:*.
      The downstream use case is "click outcome:terminated, see a
      map of terminated agencies"; tagging a multi-agency article
      with outcome:terminated maps it to one agency_id (or none),
      losing all the others and creating false-pin risk.
      Examples that should NOT carry outcome:*:
        - "Why some cities are ditching Flock" (NPR roundup)
        - "I'm hearing about more pushback against Flock"
        - "Cities are growing wary of Flock"
        - "California cities double down on ALPRs" (KQED, even
           though it reports Oakland's vote AND Richmond's
           shutdown AND Santa Cruz's limits in depth)
      Even cross-cutting investigative pieces that report each
      agency's action with multi-paragraph coverage and quotes
      stay outcome-less. The agency-specific outcome belongs in
      the per-agency article, not the cross-cut.
    • A product/feature/lawsuit article that mentions in passing that
      "some cities have shut off Flock" as background context. The
      article must substantively report the action, not just allude
      to it.

  Test: if you removed every reference to the contract action from
  the article, would the remaining article still make sense as a
  coherent piece on its stated topic? If yes, the action is
  background — skip outcome:*. If removing it would gut the article,
  apply outcome:*.
- genre: investigative | explainer | opinion | press-release | analysis.
  Pick exactly one based on what the piece is, not what it covers.
- primary_subject_agency_ids: the agency_ids of every agency this
  article is substantively about, chosen from the candidate list in
  the user message. The map viewer plots one pin per id in this list,
  so the test is "would a reader expect a map pin here?"

  Include an agency when the article reports on its specific actions,
  decisions, incidents, contracts, audits, or policies — i.e. the
  story would lose meaning without it. A piece may have one subject
  (most local news), several (an investigation naming multiple
  agencies whose cameras were exposed; a roundup of cities that turned
  Flock off), or none (national/vendor/legal pieces not anchored to
  specific agencies).

  Exclude agencies that are merely name-checked: "as in [City], where
  cameras were also installed last year" is a side mention, not a
  subject. Likewise exclude agencies that appear only in lists, asides,
  or quote attributions ("a spokesperson for [Dept] said..."). When in
  doubt, leave the agency out — false positives scatter the map.

  Return an empty list for national, vendor-focused, or legal-doctrine
  pieces with no specific agency subjects.

Two distinct escape hatches:

- refusal — you CANNOT evaluate the article: paywalled stub, truncated
  or empty body, a language you can't read. Set refusal=true with a
  refusal_reason. These go to a human review queue.

- off_topic — you CAN read it, but it simply isn't about ALPR /
  license-plate readers / police surveillance cameras at all. The
  discovery filter now queues any article naming "Flock," which
  occasionally catches an unrelated use — a literal flock of birds,
  Flock Freight (the trucking company), a church congregation, the Flock
  messaging app, etc. Set off_topic=true with a brief off_topic_reason.
  off_topic articles are DROPPED from the public viewer, so reserve it
  for genuinely unrelated content — not for on-topic articles you merely
  find thin or tangential. When unsure whether a surveillance-camera
  story counts, treat it as on-topic (off_topic=false).

When neither applies: refusal=false, refusal_reason=null, off_topic=false,
off_topic_reason=null.

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
the text. Pick primary_subject_agency_ids from this list (zero, one, or
many — see the system instructions for the inclusion test):
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
    psa_ids = data.get("primary_subject_agency_ids") or []
    bogus_psa = [p for p in psa_ids if p not in valid_agency_ids]
    if bogus_psa:
        return False, f"primary_subject_agency_ids not in candidate set: {bogus_psa}"
    if len(set(psa_ids)) != len(psa_ids):
        return False, f"primary_subject_agency_ids has duplicates: {psa_ids}"
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
               limit: int, model: str, dry_run: bool,
               reenrich: bool = False,
               include_flagged: bool = False,
               ids: set[str] | None = None) -> int:
    if not dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print("phase2: ANTHROPIC_API_KEY not set; skipping (Phase 1 still ran)")
        return 0

    # Default: only fresh "mechanical" entries.
    # --reenrich: also re-run on already-enriched entries (oldest curated_at
    # first), used after a prompt change to drag old entries up to the new
    # tagging standard. Existing summary/key_quotes/tags will be overwritten.
    accepted_statuses = {"mechanical"}
    if reenrich:
        accepted_statuses.add("enriched")

    # Default: only scanner-clean entries pass to the LLM. --include-flagged
    # opts in to processing REVIEW REQUIRED entries too — Phase 2 has no tools
    # and json_schema-constrained output, so the scanner is defense-in-depth
    # rather than a hard safety wall. Use only on known-source batches.
    accepted_prefixes = PHASE2_SCANNER_PREFIXES
    if include_flagged:
        accepted_prefixes = accepted_prefixes + ("REVIEW",)

    eligible = []
    for e in registry:
        # --ids targets a specific approved set (e.g. from the review UI);
        # those bypass the scanner gate AND the status/tier filters, since
        # the user vouched for each id. Lets you retry timed-out
        # needs_review entries by id without flipping them back manually.
        if ids is not None:
            if e.get("article_id") not in ids:
                continue
            eligible.append(e)
            continue
        if e.get("curation_status") not in accepted_statuses:
            continue
        if e.get("tier") not in PHASE2_ELIGIBLE_TIERS:
            continue
        verdict = (e.get("scanner_verdict") or "")
        # Manually-seeded URLs (article_queue_add.py default discovered_by
        # is "manual"; the seed-batch tool stamps "manual-seed-…") bypass
        # the scanner gate. The user vouched for the source when they
        # pasted the URL; Phase 2 still runs tool-less with json_schema
        # output, so this only changes which articles get enriched, not
        # what the model can do with them.
        manual = (e.get("discovered_by") or "").startswith("manual")
        if not manual and not verdict.startswith(accepted_prefixes):
            continue
        eligible.append(e)
    if reenrich:
        # Oldest curated_at first so repeated --reenrich runs make progress.
        eligible.sort(key=lambda e: (e.get("curation_status") != "mechanical",
                                     e.get("curated_at") or ""))
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
        if data.get("off_topic"):
            # Terminal, not a review queue: the curator read it and it's
            # simply not about ALPR (a literal flock of birds, Flock Freight,
            # etc. that the loosened keyword gate let through). The entry
            # stays in the registry as a tombstone so article_queue_add's URL
            # dedup never re-queues it (one crawl + one curation, ever), and
            # build_articles_data drops it from the viewer.
            entry["curation_status"] = "off_topic"
            entry["curation_error"] = f"off_topic: {data.get('off_topic_reason')}"
            entry["curated_at"] = now_iso()
            print(f"phase2: off_topic {entry['article_id']} ({data.get('off_topic_reason')})")
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
        entry["primary_subject_agency_ids"] = data["primary_subject_agency_ids"]
        # LLM judgment on PSA trumps the Phase-1 fuzzy-score cutoff: if the
        # model identifies a candidate as the article's subject, fold it into
        # agencies[] so downstream lookups (and lint_articles.py's subset
        # check) see it.
        if data["primary_subject_agency_ids"]:
            merged = list(entry.get("agencies") or [])
            for psa in data["primary_subject_agency_ids"]:
                if psa not in merged:
                    merged.append(psa)
            entry["agencies"] = merged
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
    p.add_argument("--reenrich", action="store_true",
                   help="Phase 2 only: also re-run on already-enriched "
                        "entries (oldest curated_at first), to backfill "
                        "after a prompt change. Honors --limit.")
    p.add_argument("--include-flagged", action="store_true",
                   help="Phase 2 only: opt in to processing entries with "
                        "scanner verdict REVIEW REQUIRED. Use on known-source "
                        "batches; Phase 2 has no tools so the scanner is "
                        "defense-in-depth rather than a hard wall.")
    p.add_argument("--ids", default=None,
                   help="Phase 2 only: comma- or space-separated article_ids "
                        "to enrich. Bypasses the scanner gate for those ids "
                        "(use after human review, e.g. review_mechanical.py).")
    args = p.parse_args()

    registry = article_store.load_registry()
    tags_data = read_json(TAGS_PATH, {"topics": {}, "editorial": {}})
    sources_data = read_json(SOURCES_PATH, {"sources": []})

    if args.phase in ("1", "both"):
        run_phase1(registry, tags_data=tags_data, sources_data=sources_data,
                   limit=args.limit)

    if args.phase in ("2", "both"):
        limit = args.limit if args.limit is not None else DEFAULT_PHASE2_LIMIT
        ids = None
        if args.ids:
            ids = {t for t in re.split(r"[,\s]+", args.ids) if t}
            # --ids implies "I picked these deliberately, run them all"
            if args.limit is None:
                limit = len(ids)
        run_phase2(registry, tags_data=tags_data, limit=limit,
                   model=args.model, dry_run=args.dry_run,
                   reenrich=args.reenrich,
                   include_flagged=args.include_flagged,
                   ids=ids)

    if not args.dry_run:
        article_store.save_registry(registry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
