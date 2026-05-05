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


# ── load_registry_at (git diff against base) ─────────────────────


def test_load_registry_at_returns_empty_for_unknown_ref(monkeypatch, tmp_path):
    """If the git ref doesn't exist or has no registry, return []."""
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    out = pb.load_registry_at("nonexistent-ref")
    assert out == []


def test_load_registry_at_reads_from_git(monkeypatch, tmp_path):
    """Spin up a tiny git repo, commit a registry, verify load_registry_at
    can read it back."""
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "assets").mkdir()
    reg = [_entry("art_001")]
    (tmp_path / "assets" / "article_registry.json").write_text(json.dumps(reg))
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    out = pb.load_registry_at("HEAD")
    assert len(out) == 1
    assert out[0]["article_id"] == "art_001"
