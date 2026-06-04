"""Tests for scripts/lint_articles.py.

The lint enforces: article_id format + uniqueness, URL uniqueness,
source_domain in allowlist, agencies[] entries resolve to
agency_registry, primary_subject_agency_ids entries each appear in
this entry's agencies[] (and are unique), tags appear in tags.json,
curation_status in known set, paths.{html,txt,meta} exist.

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
ARTICLE_STORE = ROOT / "scripts" / "article_store.py"


def _make_repo(tmp_path: Path, *, registry, sources=None, tags=None,
               agencies=None, paths_exist=True):
    """Build a minimal repo structure under tmp_path/repo/. Returns
    the path to the article_registry/ shard directory."""
    repo = tmp_path / "repo"
    (repo / "assets" / "articles").mkdir(parents=True)
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "lint_articles.py").write_bytes(SCRIPT.read_bytes())
    # lint imports article_store, so the temp repo needs it too.
    (repo / "scripts" / "article_store.py").write_bytes(ARTICLE_STORE.read_bytes())

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

    reg_dir = repo / "assets" / "article_registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    for entry in registry:
        aid = entry.get("article_id") or "noid"
        (reg_dir / f"{aid}.json").write_text(json.dumps(entry))
    return repo, reg_dir


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


def test_lint_accepts_hashed_article_id(tmp_path):
    # Current minting is art_<12 hex> (article_curate.article_id_for_url);
    # legacy art_NNN must keep validating alongside the new hashed form.
    repo, _ = _make_repo(tmp_path, registry=[
        _good_entry("art_a1b2c3d4e5f6"),
        _good_entry("art_001", url="https://eff.org/legacy"),
    ])
    rc, out, err = _run(repo)
    assert rc == 0, err


# ── Failure cases ────────────────────────────────────────────────


def test_lint_rejects_duplicate_article_id(tmp_path):
    # Two shards with the same internal article_id — only reachable via a
    # hand-created/misnamed file. save_registry keys the filename on the id,
    # so it can't produce this, and a colliding add surfaces as a git
    # add/add conflict; the lint is the backstop for hand-edits.
    repo, reg_dir = _make_repo(tmp_path, registry=[_good_entry("art_001")])
    (reg_dir / "art_001_dup.json").write_text(
        json.dumps(_good_entry("art_001", url="https://eff.org/different")))
    rc, out, err = _run(repo)
    assert rc == 1
    assert "duplicate article_id" in err


def test_lint_rejects_filename_id_mismatch(tmp_path):
    repo, reg_dir = _make_repo(tmp_path, registry=[_good_entry("art_001")])
    # filename stem (art_999) != internal article_id (art_002)
    (reg_dir / "art_999.json").write_text(
        json.dumps(_good_entry("art_002", url="https://eff.org/x")))
    rc, out, err = _run(repo)
    assert rc == 1
    assert "doesn't match filename" in err


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
    """Every primary_subject_agency_ids entry must appear in agencies[]."""
    repo, _ = _make_repo(tmp_path, registry=[_good_entry(
        agencies=["agency-1"],
        primary_subject_agency_ids=["agency-2"],  # not in agencies[]
    )])
    rc, out, err = _run(repo)
    assert rc == 1
    assert "primary_subject_agency_ids" in err


def test_lint_rejects_primary_subject_duplicate(tmp_path):
    """primary_subject_agency_ids entries must be unique."""
    repo, _ = _make_repo(tmp_path, registry=[_good_entry(
        agencies=["agency-1"],
        primary_subject_agency_ids=["agency-1", "agency-1"],
    )])
    rc, out, err = _run(repo)
    assert rc == 1
    assert "duplicate" in err


def test_lint_accepts_multiple_primary_subjects(tmp_path):
    """Multiple distinct primary subjects, all in agencies[], passes."""
    repo, _ = _make_repo(
        tmp_path,
        registry=[_good_entry(
            agencies=["agency-1", "agency-2"],
            primary_subject_agency_ids=["agency-1", "agency-2"],
        )],
        agencies=[
            {"agency_id": "agency-1", "geo": {"name": "X"}},
            {"agency_id": "agency-2", "geo": {"name": "Y"}},
        ],
    )
    rc, out, err = _run(repo)
    assert rc == 0, err


def test_lint_rejects_unknown_curation_status(tmp_path):
    repo, _ = _make_repo(tmp_path, registry=[_good_entry(curation_status="completed")])
    rc, out, err = _run(repo)
    assert rc == 1
    assert "unknown curation_status" in err


def test_lint_accepts_off_topic_status(tmp_path):
    """off_topic is a terminal verdict (curator: not about ALPR); the entry
    stays in the registry as a dedup tombstone, so lint must accept it."""
    repo, _ = _make_repo(tmp_path, registry=[_good_entry(curation_status="off_topic")])
    rc, out, err = _run(repo)
    assert rc == 0, err


def test_lint_rejects_missing_paths(tmp_path):
    repo, _ = _make_repo(tmp_path, registry=[_good_entry(
        paths={"html": "assets/articles/eff.org/missing.html"},
    )], paths_exist=False)
    rc, out, err = _run(repo)
    assert rc == 1
    assert "missing on disk" in err
