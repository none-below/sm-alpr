# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below

"""Pin when a run that captured nothing fails the refresh job.

After #986 about half of all refresh runs meet Cloudflare's human check on
their first slug and capture nothing, while the next run's IP gets through.
Failing the job on each of those made the run history mostly red for a crawler
that was working. A run that captured nothing now fails only once no run has
captured a portal for BLOCKED_AFTER_HOURS (see flock_transparency.py).
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from flock_transparency import (
    BLOCKED_AFTER_HOURS,
    crawler_blocked,
    latest_crawled_at,
)

NOW = datetime(2026, 11, 3, 21, 15, tzinfo=timezone.utc)


def _capture(data_dir, slug, crawled_at, name=None):
    d = data_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    name = name or crawled_at.date().isoformat()
    (d / f"{name}.json").write_text(json.dumps({"crawled_at": crawled_at.isoformat()}))


def test_recent_capture_means_not_blocked(tmp_path):
    _capture(tmp_path, "benicia-ca-pd", NOW - timedelta(hours=3))
    blocked, last = crawler_blocked(tmp_path, NOW)
    assert not blocked
    assert last == NOW - timedelta(hours=3)


def test_no_capture_for_the_window_is_blocked(tmp_path):
    _capture(tmp_path, "benicia-ca-pd", NOW - timedelta(hours=BLOCKED_AFTER_HOURS, minutes=1))
    assert crawler_blocked(tmp_path, NOW)[0]


def test_no_recent_captures_at_all_is_blocked(tmp_path):
    _capture(tmp_path, "benicia-ca-pd", NOW - timedelta(days=6))
    (tmp_path / ".attempts.json").write_text("{}")
    assert crawler_blocked(tmp_path, NOW) == (True, None)


def test_newest_wins_across_slugs_and_midnight(tmp_path):
    """Yesterday's file counts, so a window that crosses midnight works."""
    _capture(tmp_path, "a-ca-pd", NOW - timedelta(hours=22))
    _capture(tmp_path, "b-ca-pd", NOW.replace(hour=1))
    assert latest_crawled_at(tmp_path, NOW) == NOW.replace(hour=1)


def test_local_date_ahead_of_utc_is_read(tmp_path):
    """A machine east of UTC names today's capture for tomorrow."""
    _capture(tmp_path, "a-ca-pd", NOW - timedelta(hours=1),
             name=(NOW + timedelta(days=1)).date().isoformat())
    assert latest_crawled_at(tmp_path, NOW) == NOW - timedelta(hours=1)


def test_unreadable_captures_are_skipped(tmp_path):
    d = tmp_path / "a-ca-pd"
    d.mkdir()
    (d / f"{NOW.date().isoformat()}.json").write_text("{not json")
    _capture(tmp_path, "b-ca-pd", NOW - timedelta(hours=2), name=None)
    (tmp_path / "c-ca-pd").mkdir()
    (tmp_path / "c-ca-pd" / f"{NOW.date().isoformat()}.json").write_text("{}")
    assert latest_crawled_at(tmp_path, NOW) == NOW - timedelta(hours=2)
