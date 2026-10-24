"""The parallel catch-up workflow's plan/collect/merge helpers (scripts/flock_catchup.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import flock_catchup as fc
from lib import load_json, save_json


def test_plan_chunks_in_order_and_drops_duplicates():
    assert fc.plan(["a", "b", "a", "c", "d", "e"], 2) == ["a b", "c d", "e"]


def _seed(data_dir):
    save_json(data_dir / ".content_hashes.json", {"one": "h1-old", "two": "h2", "other": "x"})
    save_json(data_dir / ".attempts.json", {"one": "2026-09-20"})
    save_json(data_dir / ".rate_limit_burns.json", {"one": 2})
    save_json(data_dir / ".failed_slugs.json", {})


def _crawl(data_dir):
    """What a crawl of `one` (captured) and `two` (rate-limited) leaves behind."""
    (data_dir / "one").mkdir()
    for ext in ("txt", "json"):
        (data_dir / "one" / f"2026-10-19.{ext}").write_text(ext)
    hashes = load_json(data_dir / ".content_hashes.json")
    hashes["one"] = "h1-new"
    save_json(data_dir / ".content_hashes.json", hashes)
    save_json(data_dir / ".attempts.json", {"one": "2026-10-19"})
    save_json(data_dir / ".rate_limit_burns.json", {"two": 1})  # one's streak cleared


def test_collect_carries_only_changed_entries_for_its_own_slugs(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _seed(data)
    before = fc.snapshot(data, ["one", "two"])
    _crawl(data)

    captured = fc.collect(data, ["one", "two"], before, tmp_path / "pkg")

    assert captured == ["one"]
    meta = load_json(tmp_path / "pkg" / "meta.json")
    assert meta == {
        ".content_hashes.json": {"one": "h1-new"},
        ".attempts.json": {"one": "2026-10-19"},
        ".rate_limit_burns.json": {"one": None, "two": 1},
    }, meta
    assert sorted(p.name for p in (tmp_path / "pkg" / "captures" / "one").iterdir()) == [
        "2026-10-19.json", "2026-10-19.txt"]


def test_merge_applies_packages_without_touching_other_slugs(tmp_path):
    crawl = tmp_path / "crawl"
    crawl.mkdir()
    _seed(crawl)
    before = fc.snapshot(crawl, ["one", "two"])
    _crawl(crawl)
    fc.collect(crawl, ["one", "two"], before, tmp_path / "pkg")

    # the branch the commit job lands on: newer state for an unrelated slug
    dest = tmp_path / "dest"
    dest.mkdir()
    _seed(dest)
    hashes = load_json(dest / ".content_hashes.json")
    hashes["other"] = "x-newer-on-branch"
    save_json(dest / ".content_hashes.json", hashes)

    stage, skipped = fc.merge(dest, [tmp_path / "pkg"])

    assert skipped == []
    assert load_json(dest / ".content_hashes.json") == {
        "one": "h1-new", "two": "h2", "other": "x-newer-on-branch"}
    assert load_json(dest / ".rate_limit_burns.json") == {"two": 1}
    assert load_json(dest / ".attempts.json") == {"one": "2026-10-19"}
    assert (dest / "one" / "2026-10-19.txt").read_text() == "txt"
    assert str(dest / "one" / "2026-10-19.txt") in stage
    assert str(dest / ".content_hashes.json") in stage
    assert str(dest / ".failed_slugs.json") not in stage, "unchanged meta isn't restaged"


def test_merge_never_overwrites_a_capture_already_on_the_branch(tmp_path):
    pkg = tmp_path / "pkg" / "captures" / "one"
    pkg.mkdir(parents=True)
    (pkg / "2026-10-19.txt").write_text("from catch-up")
    dest = tmp_path / "dest"
    (dest / "one").mkdir(parents=True)
    (dest / "one" / "2026-10-19.txt").write_text("from refresh")

    stage, skipped = fc.merge(dest, [tmp_path / "pkg"])

    assert (dest / "one" / "2026-10-19.txt").read_text() == "from refresh"
    assert stage == [] and skipped == [str(dest / "one" / "2026-10-19.txt")]
