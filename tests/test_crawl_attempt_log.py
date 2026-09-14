# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below

"""Pin the attempt-log behavior that keeps the crawl queue rotating.

A 403 edge challenge or a parse_error writes nothing into the slug dir —
archive_agency stages the whole .txt/.html/.json/.pdf set and only commits on
full success. Before the attempt log, that meant a failing slug kept its old
capture date, stayed at the head of the stalest-first queue, and got re-picked
every single run. From 2026-08-17 the same six portals cycled ~48 times a day
while the other ~250 agencies were never attempted once, and the backlog
dashboard filled up with agencies the crawler had simply never reached.

The log records the attempt separately so the rotation advances, while the
capture corpus stays free of fake .txt stubs and `last_capture` keeps meaning
"last time we actually got the page".
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from flock_transparency import (
    ATTEMPT_FILE,
    is_stale,
    latest_capture_attempt_date,
    latest_capture_date,
    record_attempt,
)


def _capture(data_dir, slug, day):
    """Lay down a successful capture pair (.txt + .json) dated `day`."""
    d = data_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day.isoformat()}.txt").write_text("page text")
    (d / f"{day.isoformat()}.json").write_text("{}")


def test_attempt_advances_without_a_capture(tmp_path):
    """The whole point: a failed attempt moves last_attempt, not last_capture."""
    old = date.today() - timedelta(days=30)
    _capture(tmp_path, "acme-ca-pd", old)
    assert latest_capture_attempt_date("acme-ca-pd", tmp_path) == old

    record_attempt("acme-ca-pd", tmp_path)

    assert latest_capture_attempt_date("acme-ca-pd", tmp_path) == date.today()
    # last_capture must NOT move — no page was actually retrieved.
    assert latest_capture_date("acme-ca-pd", tmp_path) == old


def test_failed_attempt_does_not_mark_slug_fresh(tmp_path):
    """is_stale reads .txt only, so a 403 can't fake the slug into freshness.

    If a failed attempt counted as freshness the slug would drop out of the
    candidate set entirely and never be retried — the opposite failure.
    """
    old = date.today() - timedelta(days=30)
    _capture(tmp_path, "acme-ca-pd", old)
    record_attempt("acme-ca-pd", tmp_path)
    assert is_stale("acme-ca-pd", tmp_path, max_age_days=7) is True


def test_attempt_date_is_max_of_log_and_disk(tmp_path):
    """A stale log entry never drags a fresher on-disk capture backwards."""
    recent = date.today() - timedelta(days=1)
    _capture(tmp_path, "acme-ca-pd", recent)
    record_attempt("acme-ca-pd", tmp_path, when=date.today() - timedelta(days=20))
    assert latest_capture_attempt_date("acme-ca-pd", tmp_path) == recent


def test_never_captured_slug_reports_attempt_only(tmp_path):
    """A slug that has only ever failed still sorts by when it was tried."""
    record_attempt("ghost-ca-pd", tmp_path)
    assert latest_capture_attempt_date("ghost-ca-pd", tmp_path) == date.today()
    assert latest_capture_date("ghost-ca-pd", tmp_path) is None


def test_log_survives_reload_and_is_not_a_capture(tmp_path):
    """State persists across runs (it's committed) and adds no fake captures."""
    record_attempt("acme-ca-pd", tmp_path)
    assert (tmp_path / ATTEMPT_FILE).is_file()
    assert not (tmp_path / "acme-ca-pd").exists()


def test_cache_invalidates_between_writes(tmp_path):
    """The mtime-keyed cache must not pin the first value it saw."""
    first = date.today() - timedelta(days=5)
    record_attempt("acme-ca-pd", tmp_path, when=first)
    assert latest_capture_attempt_date("acme-ca-pd", tmp_path) == first
    record_attempt("acme-ca-pd", tmp_path, when=date.today())
    assert latest_capture_attempt_date("acme-ca-pd", tmp_path) == date.today()


def test_multiple_slugs_rotate_independently(tmp_path):
    """Ordering key: the just-failed slug sorts behind its untried peers."""
    old = date.today() - timedelta(days=30)
    for slug in ("a-ca-pd", "b-ca-pd", "c-ca-pd"):
        _capture(tmp_path, slug, old)
    record_attempt("a-ca-pd", tmp_path)

    order = sorted(
        ("a-ca-pd", "b-ca-pd", "c-ca-pd"),
        key=lambda s: latest_capture_attempt_date(s, tmp_path),
    )
    assert order[-1] == "a-ca-pd"
