"""Tests for crawled_slugs_on_disk — the refresh path's candidate set.

The refresh rotation must include every previously crawled agency, not
just those still reachable via the seed's outbound graph. Otherwise a
slug the seed drops from sharing (e.g. SMPD removed NCRIC on 2026-05-11)
falls off the rotation despite years of history on disk.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from flock_transparency import crawled_slugs_on_disk


def _touch_capture(slug_dir, datestamp):
    """Create a minimal date-named JSON capture so the slug counts as crawled."""
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / f"{datestamp}.json").write_text("{}")


def test_returns_empty_for_missing_dir(tmp_path):
    assert crawled_slugs_on_disk(tmp_path / "does-not-exist") == []


def test_returns_slug_dirs_with_portal_json(tmp_path):
    _touch_capture(tmp_path / "san-mateo-ca-pd", "2026-05-27")
    _touch_capture(tmp_path / "ncric", "2026-05-12")
    _touch_capture(tmp_path / "alameda-county-ca-so", "2026-05-22")
    result = set(crawled_slugs_on_disk(tmp_path))
    assert result == {"san-mateo-ca-pd", "ncric", "alameda-county-ca-so"}


def test_excludes_dot_dirs(tmp_path):
    """Meta files like .failed_slugs.json sit beside slug dirs; the loop
    must not treat hidden entries as slugs even if they contain JSON."""
    (tmp_path / ".meta").mkdir()
    (tmp_path / ".meta" / "2026-05-27.json").write_text("{}")
    _touch_capture(tmp_path / "real-slug", "2026-05-27")
    assert crawled_slugs_on_disk(tmp_path) == ["real-slug"]


def test_excludes_dirs_without_portal_json(tmp_path):
    """A slug dir with only sidecar artifacts (no date-named .json) hasn't
    been successfully captured and shouldn't enter the refresh rotation."""
    pra_only = tmp_path / "smpd-pra-only"
    pra_only.mkdir()
    (pra_only / "pra-W012541-041426.json").write_text("{}")
    _touch_capture(tmp_path / "real-slug", "2026-05-27")
    result = crawled_slugs_on_disk(tmp_path)
    assert result == ["real-slug"]


def test_excludes_dirs_with_only_html_or_txt(tmp_path):
    """An interrupted capture might leave .html/.txt without a .json
    (the artifact-set integrity rule prevents this on the happy path,
    but legacy data exists). Without a date-named .json, the slug is
    not 'successfully crawled' and shouldn't be a refresh candidate."""
    stub = tmp_path / "stub-slug"
    stub.mkdir()
    (stub / "2026-05-27.html").write_text("<html></html>")
    (stub / "2026-05-27.txt").write_text("text")
    _touch_capture(tmp_path / "real-slug", "2026-05-27")
    assert crawled_slugs_on_disk(tmp_path) == ["real-slug"]
