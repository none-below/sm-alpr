"""Tests for scripts/lint_articles.py.

The lint enforces: article_id format + uniqueness, URL uniqueness,
source_domain in allowlist, agencies[] entries resolve to
agency_registry, primary_subject_agency_id appears in this entry's
agencies[], tags appear in tags.json, curation_status in known set,
paths.{html,txt,meta} exist.

We test by running the script as a subprocess against a temp repo
laid out in tmp_path.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "lint_articles.py"


def _make_repo(tmp_path: Path, *, registry, sources=None, tags=None,
               agencies=None, paths_exist=True):
    """Build a minimal repo structure under tmp_path/repo/. Returns
    the path to the article_registry.json so it can be passed to the
    lint script."""
    repo = tmp_path / "repo"
    (repo / "assets" / "articles").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    SCRIPT.read_bytes()  # ensure source exists; copy is below
    (repo / "scripts" / "lint_articles.py").write_bytes(SCRIPT.read_bytes())

    sources = sources or [{"domain": "eff.org", "tier": 2, "name": "EFF"}]
    (repo / "assets" / "sources.json").write_text(
        json.dumps({"version": 1, "sources": sources})
    )
    tags = tags or {"topics": {"vendor:flock": {"description": ""}},
                    "editorial": {"stance:critical": {"description": ""}}}
    (repo / "assets" / "tags.json").write_text(json.dumps(tags))
    agencies = agencies or [{"agency_id": "agency-1", "geo": {"name": "X"}}]
    (repo / "assets" / "agency_registry.json").write_text(json.dumps(agencies))

    # Optionally drop placeholder files so paths.{html,txt,meta} resolve
    if paths_exist:
        for entry in registry:
            for kind in ("html", "txt", "meta"):
                rel = (entry.get("paths") or {}).get(kind)
                if rel:
                    p = repo / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.touch()

    reg = repo / "assets" / "article_registry.json"
    reg.write_text(json.dumps(registry))
    return repo, reg


def _run(repo: Path):
    """Run lint_articles.py with cwd=repo so it picks up the repo's
    sources/tags/agency files. Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "scripts/lint_articles.py"],
        cwd=repo, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _good_entry(article_id="art_001", **overrides):
    e = {
        "article_id": article_id,
        "url": f"https://eff.org/{article_id}",
        "source_domain": "eff.org",
        "tags": ["vendor:flock"],
        "agencies": [],
        "curation_status": "mechanical",
    }
    e.update(overrides)
    return e


# ── Empty / clean cases ──────────────────────────────────────────


def test_lint_passes_on_empty_registry(tmp_path):
    repo, _ = _make_repo(tmp_path, registry=[])
    rc, out, err = _run(repo)
    assert rc == 0, err


def test_lint_passes_on_valid_entries(tmp_path):
    repo, _ = _make_repo(tmp_path,
                         registry=[_good_entry("art_001"), _good_entry("art_002")])
    rc, out, err = _run(repo)
    assert rc == 0, err


# ── Failure cases ────────────────────────────────────────────────


def test_lint_rejects_duplicate_article_id(tmp_path):
    repo, _ = _make_repo(tmp_path, registry=[
        _good_entry("art_001"),
        _good_entry("art_001", url="https://eff.org/different"),
    ])
    rc, out, err = _run(repo)
    assert rc == 1
    assert "duplicate article_id" in err


def test_lint_rejects_duplicate_url(tmp_path):
    repo, _ = _make_repo(tmp_path, registry=[
        _good_entry("art_001", url="https://eff.org/same"),
        _good_entry("art_002", url="https://eff.org/same"),
    ])
    rc, out, err = _run(repo)
    assert rc == 1
    assert "duplicate url" in err


def test_lint_rejects_bad_article_id_format(tmp_path):
    repo, _ = _make_repo(tmp_path,
                         registry=[_good_entry("article-1")])  # not art_NNN
    rc, out, err = _run(repo)
    assert rc == 1
    assert "doesn't match" in err


def test_lint_rejects_unknown_source_domain(tmp_path):
    repo, _ = _make_repo(tmp_path,
                         registry=[_good_entry(source_domain="example.com")])
    rc, out, err = _run(repo)
    assert rc == 1
    assert "not in sources.json" in err


def test_lint_rejects_unknown_agency_id(tmp_path):
    repo, _ = _make_repo(tmp_path,
                         registry=[_good_entry(agencies=["nonexistent-id"])])
    rc, out, err = _run(repo)
    assert rc == 1
    assert "unknown agency_id" in err


def test_lint_rejects_unknown_tag(tmp_path):
    repo, _ = _make_repo(tmp_path,
                         registry=[_good_entry(tags=["vendor:flock", "made-up-tag"])])
    rc, out, err = _run(repo)
    assert rc == 1
    assert "unknown tag" in err


def test_lint_rejects_primary_subject_not_in_agencies(tmp_path):
    """primary_subject_agency_id must appear in this entry's agencies[]."""
    repo, _ = _make_repo(tmp_path, registry=[_good_entry(
        agencies=["agency-1"],
        primary_subject_agency_id="agency-2",  # not in agencies[]
    )])
    rc, out, err = _run(repo)
    assert rc == 1
    assert "primary_subject_agency_id" in err


def test_lint_rejects_unknown_curation_status(tmp_path):
    repo, _ = _make_repo(tmp_path, registry=[_good_entry(curation_status="completed")])
    rc, out, err = _run(repo)
    assert rc == 1
    assert "unknown curation_status" in err


def test_lint_rejects_missing_paths(tmp_path):
    repo, _ = _make_repo(tmp_path, registry=[_good_entry(
        paths={"html": "assets/articles/eff.org/missing.html"},
    )], paths_exist=False)
    rc, out, err = _run(repo)
    assert rc == 1
    assert "missing on disk" in err
