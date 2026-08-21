# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Tests for the slug-probe -> crawler hand-off.

The probe and the main crawler run on separate schedules and commit to
separate branches. To stop them rewriting the same shared files (and
conflicting at merge time), the probe no longer touches agency_registry.json,
.content_hashes.json, or .failed_slugs.json. It writes one
.slug_probe_hits/<agency_id>.json per find; the crawler is the sole writer of
the shared files and drains the hand-off via ingest_probe_hits().

These tests pin that contract: record_hit produces a per-agency file, and
ingest applies it (promote + clear failed + carry hash + delete the file)
while leaving orphaned hits in place.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from slug_probe import HITS_DIR, record_hit
from flock_transparency import HASH_FILE, ingest_probe_hits

FAILED_FILE = ".failed_slugs.json"


def _registry(tmp_path, entries):
    p = tmp_path / "agency_registry.json"
    p.write_text(json.dumps(entries, indent=2) + "\n")
    return p


def test_record_hit_writes_per_agency_file(tmp_path):
    record_hit(tmp_path, "aid-1", "found-ca-pd", "old-ca-pd",
               content_hash="abc123", probed_at="2026-06-01T00:00:00+00:00")
    f = tmp_path / HITS_DIR / "aid-1.json"
    assert f.exists()
    rec = json.loads(f.read_text())
    assert rec == {
        "agency_id": "aid-1",
        "found_slug": "found-ca-pd",
        "old_slug": "old-ca-pd",
        "content_hash": "abc123",
        "probed_at": "2026-06-01T00:00:00+00:00",
    }
    # No stray temp file left behind by the atomic write.
    assert not list((tmp_path / HITS_DIR).glob("*.tmp"))


def test_record_hit_one_file_per_agency_is_idempotent(tmp_path):
    # A re-find for the same agency overwrites its single file rather than
    # accumulating — so two probe runs never add/add-conflict on the path.
    record_hit(tmp_path, "aid-1", "first-ca-pd", None, "h1", "t1")
    record_hit(tmp_path, "aid-1", "second-ca-pd", "first-ca-pd", "h2", "t2")
    files = list((tmp_path / HITS_DIR).glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["found_slug"] == "second-ca-pd"


def test_ingest_applies_hit(tmp_path):
    reg = _registry(tmp_path, [
        {"agency_id": "aid-1", "flock_active_slug": "old-ca-pd",
         "flock_slugs": ["old-ca-pd"]},
    ])
    # Old slug had 404'd and is in failed_slugs; the crawler clears it on ingest.
    (tmp_path / FAILED_FILE).write_text(json.dumps({"old-ca-pd": {"reason": "404"}}) + "\n")
    record_hit(tmp_path, "aid-1", "new-ca-pd", "old-ca-pd",
               content_hash="deadbeef", probed_at="2026-06-01T00:00:00+00:00")

    applied = ingest_probe_hits(tmp_path, registry_path=reg)
    assert applied == 1

    entry = json.loads(reg.read_text())[0]
    assert entry["flock_active_slug"] == "new-ca-pd"
    assert entry["flock_slugs"] == ["old-ca-pd", "new-ca-pd"]

    # Hash carried over so the next refresh skips the just-captured page.
    assert json.loads((tmp_path / HASH_FILE).read_text())["new-ca-pd"] == "deadbeef"
    # Both old and new slugs cleared from failed_slugs.
    assert json.loads((tmp_path / FAILED_FILE).read_text()) == {}
    # Hand-off consumed and the dir tidied away.
    assert not (tmp_path / HITS_DIR / "aid-1.json").exists()
    assert not (tmp_path / HITS_DIR).exists()


def test_ingest_no_hits_is_noop(tmp_path):
    reg = _registry(tmp_path, [{"agency_id": "aid-1", "flock_active_slug": None}])
    assert ingest_probe_hits(tmp_path, registry_path=reg) == 0
    # Registry untouched.
    assert json.loads(reg.read_text())[0]["flock_active_slug"] is None


def test_ingest_leaves_orphan_hit_in_place(tmp_path):
    # agency_id not in the registry: don't silently drop it, don't write
    # anything — leave it for a human to notice.
    reg = _registry(tmp_path, [{"agency_id": "aid-1", "flock_active_slug": None}])
    record_hit(tmp_path, "ghost", "ghost-ca-pd", None, "h", "t")
    applied = ingest_probe_hits(tmp_path, registry_path=reg)
    assert applied == 0
    assert (tmp_path / HITS_DIR / "ghost.json").exists()


def test_ingest_without_content_hash_skips_hash_write(tmp_path):
    # A hit whose capture failed (parse error) carries no content_hash; the
    # slug is still promoted, and the crawler will re-capture + hash it.
    reg = _registry(tmp_path, [
        {"agency_id": "aid-1", "flock_active_slug": None, "flock_slugs": []},
    ])
    record_hit(tmp_path, "aid-1", "new-ca-pd", None, content_hash=None, probed_at="t")
    assert ingest_probe_hits(tmp_path, registry_path=reg) == 1
    assert json.loads(reg.read_text())[0]["flock_active_slug"] == "new-ca-pd"
    assert "new-ca-pd" not in json.loads((tmp_path / HASH_FILE).read_text())
