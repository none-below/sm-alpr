"""Pin the per-level batch-allocation behavior in flock_transparency.cmd_crawl.

Crawl runs at depth 1 with a hard rate-limit cap (batch=3 at the
hourly cadence). Without per-level allocation, level 0 (the seed's
direct outbound) consumes the whole batch every run and slugs that
only surface at level 1+ starve — alameda-ca-pd hadn't been re-
crawled for 16 days because it's not in san-mateo-ca-pd's outbound,
only reachable via 70 peers at level 1.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import flock_transparency as ft
from flock_transparency import split_batch_across_levels


def test_even_split_across_three_levels():
    """3 levels, batch=3 → 1 slot per level. The user's stated cadence."""
    assert split_batch_across_levels(3, 3) == [1, 1, 1]


def test_remainder_goes_to_lower_levels():
    """Lower levels (closer to seed) carry higher-signal candidates,
    so when the split is uneven they get the remainder. batch=3, 2
    levels → [2, 1] (not [1, 2])."""
    assert split_batch_across_levels(3, 2) == [2, 1]
    assert split_batch_across_levels(7, 3) == [3, 2, 2]


def test_single_level_gets_full_batch():
    """Without depth iteration, behavior collapses to the full batch."""
    assert split_batch_across_levels(3, 1) == [3]


def test_starvation_when_batch_smaller_than_levels():
    """If batch < num_levels, deeper levels get 0 — still better than the
    pre-fix behavior where deeper levels always got 0."""
    assert split_batch_across_levels(2, 3) == [1, 1, 0]
    assert split_batch_across_levels(1, 3) == [1, 0, 0]


def test_unbatched_runs_get_unlimited_per_level():
    """Manual `crawl` invocations without --batch shouldn't be capped."""
    assert split_batch_across_levels(None, 2) == [float("inf"), float("inf")]
    assert split_batch_across_levels(0, 3) == [float("inf")] * 3


# ── Priority: overdue against a group target, not raw age ──

TODAY = date(2026, 11, 4)


def test_california_ori_names_the_county():
    """CA041xxxx is county 41 alphabetically (San Mateo), FIPS 06081. The
    geo's nearest-centroid guess put Oakland in Contra Costa; the ORI can't."""
    assert ft.california_county_fips({"ori": ["CA0411600"]}) == "06081"
    assert ft.california_county_fips({"ori": ["CA0010900"]}) == "06001"
    assert ft.california_county_fips({"ori": ["OH0480300"]}) is None


def test_county_without_an_ori_comes_from_the_geo():
    county = {"kind": "county", "fips": "06085", "state": "CA"}
    assert ft.california_county_fips({"geo": county}) == "06085"
    # A "manual" geo is only a point: NCRIC's, in San Francisco.
    point = {"kind": "manual", "state": "CA", "lat": 37.7786, "lng": -122.3893}
    assert ft.california_county_fips({"geo": point}) == "06075"
    assert ft.california_county_fips({"geo": {"state": "OR", "lat": 45.5, "lng": -122.7}}) is None


def _registry(monkeypatch, entries):
    monkeypatch.setattr(ft, "resolve_agency",
                        lambda slug=None, name=None: entries.get(slug or name))


def test_queue_groups(monkeypatch):
    _registry(monkeypatch, {
        "oakland-ca-pd": {"ori": ["CA0010900"], "geo": {"state": "CA"}},
        "fresno-ca-pd": {"ori": ["CA0100500"], "geo": {"state": "CA"}},
        "chico-ca-pd": {"ori": ["CA0040200"], "geo": {"state": "CA"}},
        "niles-oh-pd": {"ori": ["OH0780500"], "geo": {"state": "OH"}},
    })
    seeds, partners = {"san-mateo-ca-pd"}, {"fresno-ca-pd", "niles-oh-pd"}
    group = {s: ft.QUEUE_GROUPS[ft.queue_group(s, seeds, partners)][0]
             for s in ("san-mateo-ca-pd", "oakland-ca-pd", "fresno-ca-pd",
                       "chico-ca-pd", "niles-oh-pd", "unknown-xx-pd")}
    assert group == {
        "san-mateo-ca-pd": "Bay Area",
        "oakland-ca-pd": "Bay Area",
        "fresno-ca-pd": "seed partner",
        "chico-ca-pd": "California",
        "niles-oh-pd": "seed partner",
        "unknown-xx-pd": "out of state",
    }


def _tried(data_dir, days_ago):
    (data_dir / ft.ATTEMPT_FILE).write_text(json.dumps(
        {slug: (TODAY - timedelta(days=n)).isoformat() for slug, n in days_ago.items()}))


def test_overdue_beats_old(tmp_path):
    """A Bay Area portal two weeks old (2x its 7-day target) goes before an
    out-of-state one 100 days old (1.1x its 90)."""
    _tried(tmp_path, {"bay-ca-pd": 14, "far-oh-pd": 100})
    target = dict(ft.QUEUE_GROUPS)

    def key(slug, group):
        return ft.crawl_order_key(slug, tmp_path, target[group], set(), TODAY)

    assert key("bay-ca-pd", "Bay Area") < key("far-oh-pd", "out of state")
    # Far enough past its own target, the out-of-state portal gets its turn.
    _tried(tmp_path, {"bay-ca-pd": 14, "far-oh-pd": 200})
    assert key("far-oh-pd", "out of state") < key("bay-ca-pd", "Bay Area")


def test_never_tried_first_and_known_failures_last(tmp_path):
    _tried(tmp_path, {"old-ca-pd": 300})
    order = sorted(["failed-ca-pd", "old-ca-pd", "new-ca-pd"],
                   key=lambda s: ft.crawl_order_key(s, tmp_path, 7, {"failed-ca-pd"}, TODAY))
    assert order == ["new-ca-pd", "old-ca-pd", "failed-ca-pd"]


def _capture(data_dir, slug, outbound):
    d = data_dir / slug
    d.mkdir()
    (d / "2026-11-01.json").write_text(json.dumps({"sharing_outbound": outbound}))


def test_seed_partners_count_sharing_in_either_direction(tmp_path, monkeypatch):
    _registry(monkeypatch, {
        "san-mateo-ca-pd": {"flock_names": ["San Mateo CA PD"]},
        "Fresno CA PD": {"flock_active_slug": "fresno-ca-pd"},
        "Gone CA PD": {},  # no portal
    })
    _capture(tmp_path, "san-mateo-ca-pd", ["Fresno CA PD", "Gone CA PD"])
    _capture(tmp_path, "chico-ca-pd", ["San Mateo CA PD"])
    _capture(tmp_path, "redding-ca-pd", ["Chico CA PD"])
    assert ft.seed_partner_slugs({"san-mateo-ca-pd"}, tmp_path) == {"fresno-ca-pd", "chico-ca-pd"}
