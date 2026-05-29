"""Tests for scripts/article_pr_body.py.

The body generator's pure functions are aggregation + rendering. The
diff-against-base function uses git, so we test it by spinning up a
temp git repo with two commits.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import article_pr_body as pb  # noqa: E402


def _entry(article_id, **overrides):
    e = {
        "article_id": article_id,
        "url": f"https://eff.org/{article_id}",
        "source_domain": "eff.org",
        "tier": 2,
        "stance": "critical",
        "title": f"Title {article_id}",
        "summary": f"Summary text for {article_id}.",
        "tags": ["vendor:flock"],
        "agencies": [],
        "curation_status": "enriched",
        "scanner_verdict": "WARNINGS — 1 pts, low/medium only",
        "wayback_source": "saved",
        "wayback_url": f"https://web.archive.org/web/20260101000000/https://eff.org/{article_id}",
        "pdf_status": "rendered",
        "paths": {
            "html": f"assets/articles/eff.org/{article_id}.html",
            "pdf": f"assets/articles/eff.org/{article_id}.pdf",
        },
    }
    e.update(overrides)
    return e


# ── render_body content ──────────────────────────────────────────


def test_render_body_empty_registry():
    body = pb.render_body(new=[], all_articles=[],
                          queue_count=0, priority_count=0)
    assert "no new articles" in body
    assert "Total articles in registry:** 0" in body


def test_render_body_includes_per_article_block():
    e = _entry("art_001")
    body = pb.render_body(new=[e], all_articles=[e],
                          queue_count=10, priority_count=2)
    assert "art_001" in body
    assert "Title art_001" in body
    assert "Summary text for art_001" in body
    assert e["url"] in body
    assert "vendor:flock" in body
    # Wayback link rendered
    assert "Wayback" in body


def test_render_body_aggregates_curation_status():
    entries = [
        _entry("art_001", curation_status="enriched"),
        _entry("art_002", curation_status="mechanical"),
        _entry("art_003", curation_status="needs_review"),
    ]
    body = pb.render_body(new=entries, all_articles=entries,
                          queue_count=0, priority_count=0)
    assert "enriched: 1" in body
    assert "mechanical: 1" in body
    assert "needs_review: 1" in body


def test_render_body_aggregates_scanner_verdict():
    entries = [
        _entry("art_001", scanner_verdict="CLEAN — no findings"),
        _entry("art_002", scanner_verdict="WARNINGS — 1 pts, low/medium only"),
        _entry("art_003", scanner_verdict="WARNINGS — 3 pts, low/medium only"),
    ]
    body = pb.render_body(new=entries, all_articles=entries,
                          queue_count=0, priority_count=0)
    assert "CLEAN: 1" in body
    assert "WARNINGS: 2" in body


def test_render_body_top_sources_count():
    entries = [
        _entry(f"art_00{i}", source_domain="eff.org") for i in range(1, 4)
    ] + [
        _entry("art_004", source_domain="aclu.org"),
    ]
    body = pb.render_body(new=[], all_articles=entries,
                          queue_count=0, priority_count=0)
    assert "eff.org (3)" in body
    assert "aclu.org (1)" in body


def test_render_body_queue_remaining():
    body = pb.render_body(new=[], all_articles=[],
                          queue_count=42, priority_count=8)
    assert "8 priority" in body
    assert "42 auto" in body
    assert "= 50" in body


def test_fmt_summary_line_truncates_long_summaries():
    e = _entry("art_x", summary="x" * 500)
    out = pb.fmt_summary_line(e, max_len=50)
    assert len(out) <= 50
    assert out.endswith("…")


def test_fmt_summary_line_returns_none_for_empty():
    e = _entry("art_x", summary="")
    assert pb.fmt_summary_line(e) is None


def test_fmt_summary_line_returns_none_for_missing():
    e = {"article_id": "art_x"}
    assert pb.fmt_summary_line(e) is None


# ── count_queue_files ────────────────────────────────────────────


def test_count_queue_files_zero_for_missing_dir(tmp_path):
    assert pb.count_queue_files(tmp_path / "nonexistent") == 0


def test_count_queue_files_counts_only_json(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.json").write_text("{}")
    (tmp_path / "c.txt").write_text("not json")
    (tmp_path / "subdir").mkdir()  # subdirs are not files
    assert pb.count_queue_files(tmp_path) == 2


# ── base_article_ids (git diff against base) ─────────────────────


def test_base_article_ids_returns_empty_for_unknown_ref(monkeypatch, tmp_path):
    """If the git ref doesn't exist, return an empty set."""
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    assert pb.base_article_ids("nonexistent-ref") == set()


def test_base_article_ids_reads_from_git(monkeypatch, tmp_path):
    """Spin up a tiny git repo, commit a shard, verify base_article_ids
    derives the id from the shard filename at that ref."""
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "assets" / "article_registry").mkdir(parents=True)
    (tmp_path / "assets" / "article_registry" / "art_001.json").write_text(
        json.dumps(_entry("art_001")))
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    assert pb.base_article_ids("HEAD") == {"art_001"}
