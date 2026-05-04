#!/usr/bin/env python3
"""Generate a markdown body for the rolling article-registry-bot PR.

Diffs the bot branch's article_registry.json against main to find what's
new in the open PR, then summarizes per-article (title, source, tier,
status, summary, wayback) plus aggregate stats (total in registry,
curation_status counts, scanner verdict distribution, Wayback coverage,
queue depth).

The article-registry.yml and discover-articles.yml workflows write the
script's stdout to a file and pass it to ``gh pr create/edit
--body-file`` so the PR description always reflects the current state
of the branch.

Usage:
  uv run python scripts/article_pr_body.py > /tmp/pr-body.md
  uv run python scripts/article_pr_body.py --base origin/main
"""

import argparse
import functools
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "assets" / "article_registry.json"
QUEUE_PATH = ROOT / "assets" / "articles" / "queue.jsonl"
PRIORITY_PATH = ROOT / "assets" / "articles" / "queue_priority.jsonl"


def load_registry_at(ref: str) -> list[dict]:
    """Load the article_registry.json from a git ref. Returns [] if absent."""
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:assets/article_registry.json"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout) or []
    except json.JSONDecodeError:
        return []


def load_registry_local() -> list[dict]:
    if not REGISTRY_PATH.exists():
        return []
    try:
        return json.loads(REGISTRY_PATH.read_text()) or []
    except json.JSONDecodeError:
        return []


def count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def fmt_summary_line(article: dict, max_len: int = 220) -> str | None:
    s = (article.get("summary") or "").strip()
    if not s:
        return None
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


def render_article(a: dict) -> list[str]:
    """One article block for the PR body."""
    title = a.get("title") or "(no title)"
    src = a.get("source_domain") or "?"
    tier = a.get("tier")
    stance = a.get("stance") or "?"
    status = a.get("curation_status") or "?"
    aid = a.get("article_id") or "art_???"
    url = a.get("url") or ""

    lines = [f"### {aid} · {src} (tier {tier}, {stance}) · `{status}`"]
    lines.append(f"**{title}**")
    lines.append("")
    summary = fmt_summary_line(a)
    if summary:
        lines.append(summary)
        lines.append("")
    meta = []
    if url:
        meta.append(f"[Article]({url})")
    if a.get("wayback_url"):
        meta.append(f"[Wayback]({a['wayback_url']})")
    if a.get("paths", {}).get("pdf"):
        meta.append(f"PDF: `{a['paths']['pdf']}`")
    if meta:
        lines.append(" · ".join(meta))
    tags = a.get("tags") or []
    if tags:
        lines.append("Tags: " + ", ".join(f"`{t}`" for t in tags))
    psa = a.get("primary_subject_agency_id")
    if psa:
        lines.append(f"Primary subject agency_id: `{psa}`")
    return lines


def render_stats(all_articles: list[dict],
                 queue_count: int, priority_count: int) -> list[str]:
    """Aggregate stats block."""
    lines = ["## Stats", ""]
    lines.append(f"- **Total articles in registry:** {len(all_articles)}")
    if not all_articles:
        return lines

    status_counts = Counter(a.get("curation_status") for a in all_articles)
    parts = []
    for k in ("enriched", "mechanical", "needs_review"):
        parts.append(f"{k}: {status_counts.get(k, 0)}")
    lines.append(f"- Curation: {' · '.join(parts)}")

    # Scanner verdicts: bucket by prefix word ("CLEAN", "WARNINGS",
    # "REVIEW REQUIRED", or other/scanner errors).
    def scanner_bucket(v: str | None) -> str:
        if not v:
            return "(unset)"
        for prefix in ("CLEAN", "WARNINGS", "REVIEW REQUIRED"):
            if v.startswith(prefix):
                return prefix
        return "ERROR"
    scanner_counts = Counter(scanner_bucket(a.get("scanner_verdict"))
                             for a in all_articles)
    parts = [f"{k}: {v}" for k, v in
             sorted(scanner_counts.items(), key=lambda kv: -kv[1])]
    lines.append(f"- Scanner: {' · '.join(parts)}")

    wayback_counts = Counter(a.get("wayback_source") or "(unset)"
                             for a in all_articles)
    parts = [f"{k}: {v}" for k, v in
             sorted(wayback_counts.items(), key=lambda kv: -kv[1])]
    lines.append(f"- Wayback: {' · '.join(parts)}")

    pdf_counts = Counter(a.get("pdf_status") or "(unset)"
                         for a in all_articles)
    parts = [f"{k}: {v}" for k, v in
             sorted(pdf_counts.items(), key=lambda kv: -kv[1])]
    lines.append(f"- PDF: {' · '.join(parts)}")

    by_source = Counter(a.get("source_domain") or "?" for a in all_articles)
    top = ", ".join(f"{d} ({n})" for d, n in by_source.most_common(8))
    lines.append(f"- Top sources: {top}")

    lines.append(f"- Queue remaining: {priority_count} priority + "
                 f"{queue_count} auto = {priority_count + queue_count}")
    return lines


def render_body(new: list[dict], all_articles: list[dict],
                queue_count: int, priority_count: int) -> str:
    lines = [
        "Automated rolling article crawl + curation + RSS discovery.",
        "",
        "Each cron tick fetches a few queued URLs, runs the injection ",
        "scanner, renders a Playwright PDF, submits a Wayback save, and ",
        "builds a registry entry. With `ANTHROPIC_API_KEY` set, eligible ",
        "articles also get Phase-2 semantic enrichment.",
        "",
    ]
    new = sorted(new, key=lambda a: a.get("article_id") or "")
    lines.append(f"## In this PR — {len(new)} new article(s)")
    lines.append("")
    if not new:
        lines.append("_(no new articles since last merge — this PR is "
                     "currently a state-only update)_")
        lines.append("")
    else:
        for a in new:
            lines.extend(render_article(a))
            lines.append("")
    lines.extend(render_stats(all_articles, queue_count, priority_count))
    lines.append("")
    lines.append(
        "---\n"
        "_Body auto-generated by `scripts/article_pr_body.py` on each "
        "cron tick. Edits will be overwritten._"
    )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default="origin/main",
                   help="git ref to diff against (default: origin/main)")
    args = p.parse_args()

    base_articles = load_registry_at(args.base)
    head_articles = load_registry_local()
    base_ids = {a.get("article_id") for a in base_articles}
    new_articles = [a for a in head_articles
                    if a.get("article_id") not in base_ids]

    queue_count = count_jsonl(QUEUE_PATH)
    priority_count = count_jsonl(PRIORITY_PATH)

    sys.stdout.write(render_body(
        new_articles, head_articles, queue_count, priority_count,
    ))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
