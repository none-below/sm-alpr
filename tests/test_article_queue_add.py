"""Tests for scripts/article_queue_add.py.

Covers the pure functions (URL validation, domain matching, filename
hashing) plus integration through the CLI entrypoint with a tmp_path
queue dir.
"""

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import article_queue_add as qa  # noqa: E402


# ── URL validation ─────────────────────────────────────────────────


@pytest.mark.parametrize("url,error_substr", [
    ("",                          "empty"),
    ("   ",                       "empty"),
    ("//example.com/foo",         "scheme-relative"),
    ("ftp://example.com",         "scheme"),
    ("javascript:alert(1)",       "scheme"),
    ("https://",                  "host"),
])
def test_validate_url_rejects(url, error_substr):
    canonical, err = qa.validate_url(url)
    assert err is not None
    assert error_substr in err


def test_validate_url_strips_fragment_keeps_query():
    canonical, err = qa.validate_url(
        "https://eff.org/deeplinks/foo?utm_source=rss#section-2"
    )
    assert err is None
    assert canonical == "https://eff.org/deeplinks/foo?utm_source=rss"


def test_validate_url_accepts_normal():
    canonical, err = qa.validate_url("https://www.eff.org/deeplinks/abc")
    assert err is None
    assert canonical == "https://www.eff.org/deeplinks/abc"


# ── domain matching ───────────────────────────────────────────────


def test_normalize_domain_strips_www():
    assert qa.normalize_domain("www.eff.org") == "eff.org"
    assert qa.normalize_domain("WWW.EFF.ORG") == "eff.org"
    assert qa.normalize_domain("blog.eff.org") == "blog.eff.org"


def test_domain_matches_exact():
    allowed = {"eff.org", "aclu.org"}
    assert qa.domain_matches("eff.org", allowed) == "eff.org"
    assert qa.domain_matches("www.eff.org", allowed) == "eff.org"


def test_domain_matches_subdomain_strip():
    """blog.eff.org → eff.org (one-level strip)."""
    allowed = {"eff.org"}
    assert qa.domain_matches("blog.eff.org", allowed) == "eff.org"
    # ww2.kqed.org → strips ww2 → kqed.org
    allowed = {"kqed.org"}
    assert qa.domain_matches("ww2.kqed.org", allowed) == "kqed.org"


def test_domain_matches_unknown():
    assert qa.domain_matches("unknown.example.com", {"eff.org"}) is None


# ── filename hashing ──────────────────────────────────────────────


def test_url_filename_deterministic():
    f1 = qa.url_filename("https://eff.org/foo")
    f2 = qa.url_filename("https://eff.org/foo")
    assert f1 == f2
    # 8 hex chars + .json = 13 chars
    assert len(f1) == 13
    assert f1.endswith(".json")


def test_url_filename_distinct_for_different_urls():
    a = qa.url_filename("https://eff.org/foo")
    b = qa.url_filename("https://eff.org/bar")
    assert a != b


# ── CLI integration ──────────────────────────────────────────────


def _setup_repo(tmp_path: Path, monkeypatch):
    """Build a minimal in-tmp repo so qa can read sources/registry and
    write into a queue dir without touching the real project."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "sources.json").write_text(json.dumps({
        "version": 1,
        "sources": [
            {"domain": "eff.org",      "tier": 2, "name": "EFF"},
            {"domain": "themarkup.org","tier": 2, "name": "The Markup"},
        ],
    }))
    (tmp_path / "assets" / "article_registry").mkdir()
    (tmp_path / "assets" / "articles").mkdir()
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    monkeypatch.setattr(qa, "SOURCES_PATH", tmp_path / "assets/sources.json")
    monkeypatch.setattr(qa, "REGISTRY_DIR", tmp_path / "assets/article_registry")
    monkeypatch.setattr(qa, "QUEUE_DIR", tmp_path / "assets/articles/queue")
    monkeypatch.setattr(qa, "PRIORITY_DIR", tmp_path / "assets/articles/queue/priority")


def _run(monkeypatch, capsys, *argv):
    """Drive qa.main() with a fake argv. Returns (rc, out, err)."""
    monkeypatch.setattr(sys, "argv", ["article_queue_add.py", *argv])
    rc = qa.main()
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_add_one_url_creates_file(tmp_path, monkeypatch, capsys):
    _setup_repo(tmp_path, monkeypatch)
    rc, out, err = _run(monkeypatch, capsys, "https://eff.org/deeplinks/x")
    assert rc == 0
    files = list((tmp_path / "assets/articles/queue").glob("*.json"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text())
    assert entry["url"] == "https://eff.org/deeplinks/x"
    assert entry["source_domain"] == "eff.org"
    assert "appended=1" in out


def test_add_dedupes_existing_url(tmp_path, monkeypatch, capsys):
    _setup_repo(tmp_path, monkeypatch)
    _run(monkeypatch, capsys, "https://eff.org/deeplinks/x")
    rc, out, _ = _run(monkeypatch, capsys, "https://eff.org/deeplinks/x")
    assert rc == 0
    assert "appended=0" in out
    assert "duplicate=1" in out
    files = list((tmp_path / "assets/articles/queue").glob("*.json"))
    assert len(files) == 1


def test_add_rejects_unknown_domain(tmp_path, monkeypatch, capsys):
    _setup_repo(tmp_path, monkeypatch)
    rc, out, err = _run(monkeypatch, capsys, "https://example.com/foo")
    assert rc == 1
    assert "rejected=1" in out
    assert "not in" in err  # "not in sources.json"
    assert not list((tmp_path / "assets/articles/queue").glob("*.json"))


def test_add_priority_uses_priority_dir(tmp_path, monkeypatch, capsys):
    _setup_repo(tmp_path, monkeypatch)
    rc, out, _ = _run(monkeypatch, capsys, "--priority",
                      "https://eff.org/deeplinks/x")
    assert rc == 0
    auto = list((tmp_path / "assets/articles/queue").glob("*.json"))
    prio = list((tmp_path / "assets/articles/queue/priority").glob("*.json"))
    assert len(auto) == 0
    assert len(prio) == 1
    assert "queue/priority/" in out


def test_add_dedup_across_priority_and_auto(tmp_path, monkeypatch, capsys):
    """A URL queued in priority should not be re-added to auto."""
    _setup_repo(tmp_path, monkeypatch)
    _run(monkeypatch, capsys, "--priority", "https://eff.org/deeplinks/x")
    rc, out, _ = _run(monkeypatch, capsys, "https://eff.org/deeplinks/x")
    assert rc == 0
    assert "duplicate=1" in out


def test_add_dedup_against_registry(tmp_path, monkeypatch, capsys):
    """A URL already in the article registry should dedup."""
    _setup_repo(tmp_path, monkeypatch)
    (tmp_path / "assets/article_registry/art_001.json").write_text(json.dumps(
        {"article_id": "art_001",
         "url": "https://eff.org/already-curated",
         "curation_status": "enriched"}))
    rc, out, _ = _run(monkeypatch, capsys, "https://eff.org/already-curated")
    assert rc == 0
    assert "duplicate=1" in out


def test_add_subdomain_url_matches_apex_allowlist(tmp_path, monkeypatch, capsys):
    """blog.eff.org should be accepted under eff.org allowlist entry."""
    _setup_repo(tmp_path, monkeypatch)
    rc, out, _ = _run(monkeypatch, capsys, "https://blog.eff.org/post-1")
    assert rc == 0
    assert "appended=1" in out
