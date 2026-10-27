# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""The slug-probe -> crawler hand-off (slug_probe.record_hit and
flock_transparency.ingest_probe_hits).

The probe and the refresh crawler commit to separate bot branches. When both
rewrote agency_registry.json, .content_hashes.json and .failed_slugs.json, a
probe hit next to a slug the crawler had re-hashed conflicted on rebase and
stalled the probe. Now the probe writes one .slug_probe_hits/<agency_id>.json
per find, and the crawler applies and deletes it.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from flock_transparency import HASH_FILE, ingest_probe_hits
from lib import FAILED_FILE, PROBE_HITS_DIR
from slug_probe import record_hit


def _registry(tmp_path, entries):
    p = tmp_path / "agency_registry.json"
    p.write_text(json.dumps(entries, indent=2) + "\n")
    return p


def test_record_hit_writes_one_file_per_agency(tmp_path):
    record_hit(tmp_path, "aid-1", "found-ca-pd", "old-ca-pd",
               content_hash="abc123", probed_at="2026-10-27T00:00:00+00:00")
    assert json.loads((tmp_path / PROBE_HITS_DIR / "aid-1.json").read_text()) == {
        "agency_id": "aid-1",
        "found_slug": "found-ca-pd",
        "old_slug": "old-ca-pd",
        "content_hash": "abc123",
        "probed_at": "2026-10-27T00:00:00+00:00",
    }
    assert [p.name for p in (tmp_path / PROBE_HITS_DIR).iterdir()] == ["aid-1.json"], \
        "no temp file left behind"


def test_ingest_promotes_clears_failed_records_hash_and_deletes(tmp_path):
    reg = _registry(tmp_path, [
        {"agency_id": "aid-1", "flock_active_slug": "old-ca-pd", "flock_slugs": ["old-ca-pd"]},
        {"agency_id": "aid-2", "flock_active_slug": "other-ca-pd", "flock_slugs": ["other-ca-pd"]},
    ])
    (tmp_path / FAILED_FILE).write_text(json.dumps({
        "old-ca-pd": {"reason": "http_404"}, "unrelated-ca-pd": {"reason": "http_404"}}))
    (tmp_path / HASH_FILE).write_text(json.dumps({"other-ca-pd": "h-other"}))
    record_hit(tmp_path, "aid-1", "new-ca-pd", "old-ca-pd", "deadbeef", "t")

    assert ingest_probe_hits(tmp_path, registry_path=reg) == 1

    entries = json.loads(reg.read_text())
    assert entries[0]["flock_active_slug"] == "new-ca-pd"
    assert entries[0]["flock_slugs"] == ["old-ca-pd", "new-ca-pd"]
    assert entries[1]["flock_active_slug"] == "other-ca-pd"
    assert json.loads((tmp_path / HASH_FILE).read_text()) == {
        "new-ca-pd": "deadbeef", "other-ca-pd": "h-other"}
    assert json.loads((tmp_path / FAILED_FILE).read_text()) == {
        "unrelated-ca-pd": {"reason": "http_404"}}
    assert not (tmp_path / PROBE_HITS_DIR).exists(), "applied hand-offs are deleted"


def test_ingest_with_no_hits_writes_nothing(tmp_path):
    reg = _registry(tmp_path, [{"agency_id": "aid-1", "flock_active_slug": None}])
    before = reg.read_text()
    assert ingest_probe_hits(tmp_path, registry_path=reg) == 0
    assert reg.read_text() == before
    assert not (tmp_path / HASH_FILE).exists()
    assert not (tmp_path / FAILED_FILE).exists()


def test_ingest_leaves_unknown_and_unreadable_hits_in_place(tmp_path):
    reg = _registry(tmp_path, [{"agency_id": "aid-1", "flock_active_slug": None}])
    record_hit(tmp_path, "ghost", "ghost-ca-pd", None, "h", "t")
    (tmp_path / PROBE_HITS_DIR / "broken.json").write_text("{not json")
    record_hit(tmp_path, "aid-1", "new-ca-pd", None, "h1", "t")

    assert ingest_probe_hits(tmp_path, registry_path=reg) == 1

    assert sorted(p.name for p in (tmp_path / PROBE_HITS_DIR).iterdir()) == [
        "broken.json", "ghost.json"]
    assert json.loads(reg.read_text())[0]["flock_active_slug"] == "new-ca-pd"


def test_ingest_without_content_hash_still_promotes(tmp_path):
    """A hit whose capture failed carries no hash; the crawler captures it."""
    reg = _registry(tmp_path, [{"agency_id": "aid-1", "flock_active_slug": None, "flock_slugs": []}])
    record_hit(tmp_path, "aid-1", "new-ca-pd", None, content_hash=None, probed_at="t")

    assert ingest_probe_hits(tmp_path, registry_path=reg) == 1

    assert json.loads(reg.read_text())[0]["flock_active_slug"] == "new-ca-pd"
    assert json.loads((tmp_path / HASH_FILE).read_text()) == {}
