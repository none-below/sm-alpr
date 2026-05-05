"""Pin the per-level batch-allocation behavior in flock_transparency.cmd_crawl.

Crawl runs at depth 1 with a hard rate-limit cap (batch=3 at the
hourly cadence). Without per-level allocation, level 0 (the seed's
direct outbound) consumes the whole batch every run and slugs that
only surface at level 1+ starve — alameda-ca-pd hadn't been re-
crawled for 16 days because it's not in san-mateo-ca-pd's outbound,
only reachable via 70 peers at level 1.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
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
