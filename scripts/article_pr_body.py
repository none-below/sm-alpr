#!/usr/bin/env python3
"""Generate a markdown body for the rolling article-registry-bot PR.

Diffs the bot branch's article_registry/ shard filenames against main to
find what's new in the open PR, then summarizes per-article (title, source, tier,
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
sys.path.insert(0, str(ROOT / "scripts"))
import article_store

REGISTRY_REL = "assets/article_registry"
QUEUE_DIR = ROOT / "assets" / "articles" / "queue"
PRIORITY_DIR = QUEUE_DIR / "priority"


def base_article_ids(ref: str) -> set[str]:
    """article_ids present at a git ref. With one shard file per article,
    that's just the shard filenames under the registry dir — a single git
    call, no content reads (we only diff ids to find what's new)."""
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", ref, "--", REGISTRY_REL],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return set()
    if result.returncode != 0:
        return set()
    ids: set[str] = set()
    for line in result.stdout.splitlines():
        name = line.rsplit("/", 1)[-1]
        if name.endswith(".json"):
            ids.add(name[: -len(".json")])
    return ids


def load_registry_local() -> list[dict]:
    return article_store.load_registry(article_store.REGISTRY_DIR)


def count_queue_files(d: Path) -> int:
    """Count *.json files directly under d (non-recursive). The nested
    priority/ subdir is counted separately by its own caller."""
    if not d.is_dir():
        return 0
    return sum(1 for p in d.iterdir()
               if p.is_file() and p.suffix == ".json")


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
    psa_ids = a.get("primary_subject_agency_ids") or []
    if psa_ids:
        label = "Primary subject agency_id" + ("s" if len(psa_ids) != 1 else "")
        lines.append(f"{label}: " + ", ".join(f"`{p}`" for p in psa_ids))
    return lines


def render_stats(all_articles: list[dict],
                 queue_count: int, priority_count: int) -> list[str]:
    """Aggregate stats block. Queue depth always renders (independent of
    registry contents) since pending work is meaningful even when no
    articles have been curated yet."""
    lines = ["## Stats", ""]
    lines.append(f"- **Total articles in registry:** {len(all_articles)}")
    queue_line = (f"- Queue remaining: {priority_count} priority + "
                  f"{queue_count} auto = {priority_count + queue_count}")
    if not all_articles:
        lines.append(queue_line)
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

    lines.append(queue_line)
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

    base_ids = base_article_ids(args.base)
    head_articles = load_registry_local()
    new_articles = [a for a in head_articles
                    if a.get("article_id") not in base_ids]

    queue_count = count_queue_files(QUEUE_DIR)
    priority_count = count_queue_files(PRIORITY_DIR)

    sys.stdout.write(render_body(
        new_articles, head_articles, queue_count, priority_count,
    ))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
