"""Tests for the registry-prune helper in build_agency_registry.

Speculative slugs (`name_to_slug` guesses that nobody verified) shouldn't
appear in `flock_active_slug` or `flock_slugs` — those fields claim the
slug is real. The prune helper removes claims that aren't backed by a
captured `.json` on disk or a slug_probe `found` record.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from build_agency_registry import confirmed_slugs, prune_speculative_slugs


def test_prune_nullifies_active_when_unconfirmed():
    """The most common case: an entry whose flock_active_slug came from
    name_to_slug() never actually got captured. Active goes to None,
    flock_slugs empties out."""
    registry = [{
        "agency_id": "x",
        "slug": "abilene-ks-pd",
        "flock_active_slug": "abilene-ks-pd",
        "flock_slugs": ["abilene-ks-pd"],
        "flock_names": ["Abilene KS PD"],
    }]
    stats = prune_speculative_slugs(registry, confirmed=set())
    assert registry[0]["flock_active_slug"] is None
    assert registry[0]["flock_slugs"] == []
    assert stats["pruned_active"] == 1


def test_prune_preserves_confirmed_active():
    """Slugs with on-disk captures (or probe-found state) survive
    untouched. This is the load-bearing assertion — we mustn't lose
    the 193 confirmed real slugs."""
    registry = [{
        "agency_id": "x",
        "slug": "alameda-ca-pd",
        "flock_active_slug": "alameda-ca-pd",
        "flock_slugs": ["alameda-ca-pd"],
        "flock_names": ["Alameda CA PD"],
    }]
    stats = prune_speculative_slugs(registry, confirmed={"alameda-ca-pd"})
    assert registry[0]["flock_active_slug"] == "alameda-ca-pd"
    assert registry[0]["flock_slugs"] == ["alameda-ca-pd"]
    assert stats["pruned_active"] == 0
    assert stats["kept"] == 1


def test_prune_promotes_other_confirmed_slug_when_active_is_speculative():
    """If active is speculative but flock_slugs contains a confirmed
    historical slug, promote it. Rare but real: Flock renamed an
    agency's slug, we crawled under the new name, but registry still
    pointed at the old."""
    registry = [{
        "agency_id": "x",
        "slug": "alameda-ca-pd",
        "flock_active_slug": "old-speculative-guess",
        "flock_slugs": ["old-speculative-guess", "alameda-ca-pd"],
        "flock_names": ["Alameda CA PD"],
    }]
    prune_speculative_slugs(registry, confirmed={"alameda-ca-pd"})
    assert registry[0]["flock_active_slug"] == "alameda-ca-pd"
    assert registry[0]["flock_slugs"] == ["alameda-ca-pd"]


def test_prune_drops_speculative_history_keeping_confirmed_active():
    """flock_slugs may carry a speculative tail from prior name_to_slug
    guesses; those drop out even when active is confirmed."""
    registry = [{
        "agency_id": "x",
        "slug": "alameda-ca-pd",
        "flock_active_slug": "alameda-ca-pd",
        "flock_slugs": ["alameda-ca-pd", "alameda-pd-ca", "alameda-ca"],
        "flock_names": ["Alameda CA PD"],
    }]
    prune_speculative_slugs(registry, confirmed={"alameda-ca-pd"})
    assert registry[0]["flock_active_slug"] == "alameda-ca-pd"
    assert registry[0]["flock_slugs"] == ["alameda-ca-pd"]


def test_prune_leaves_already_null_active_alone():
    """Idempotent: a second prune run on the result of the first must
    be a no-op."""
    registry = [{
        "agency_id": "x",
        "slug": "x-pd",
        "flock_active_slug": None,
        "flock_slugs": [],
        "flock_names": ["X PD"],
    }]
    stats = prune_speculative_slugs(registry, confirmed=set())
    assert registry[0]["flock_active_slug"] is None
    assert registry[0]["flock_slugs"] == []
    assert stats["pruned_active"] == 0


def test_prune_does_not_drop_entries():
    """Pruning never removes a registry entry — peer-outbound discovery
    is still useful signal even when we can't (yet) crawl the agency.
    Entries become probe-tier-A targets for later confirmation."""
    registry = [
        {"agency_id": "a", "slug": "a", "flock_active_slug": "a-spec",
         "flock_slugs": ["a-spec"], "flock_names": ["A"]},
        {"agency_id": "b", "slug": "b", "flock_active_slug": "b-real",
         "flock_slugs": ["b-real"], "flock_names": ["B"]},
    ]
    prune_speculative_slugs(registry, confirmed={"b-real"})
    assert len(registry) == 2
    assert {e["agency_id"] for e in registry} == {"a", "b"}


# ── confirmed_slugs ─────────────────────────────────────────────


def test_confirmed_slugs_walks_data_dir(tmp_path):
    """A slug is confirmed when there's a parsed .json under its
    directory. Missing dir or empty dir = not confirmed."""
    base = tmp_path / "transparency"
    base.mkdir()
    (base / "alameda-ca-pd").mkdir()
    (base / "alameda-ca-pd" / "2026-04-19.json").write_text("{}")
    (base / "no-data").mkdir()  # exists but empty
    (base / ".hidden_dotfile").mkdir()  # hidden state dirs ignored
    confirmed = confirmed_slugs(base)
    assert confirmed == {"alameda-ca-pd"}


def test_confirmed_slugs_includes_probe_found(tmp_path):
    """Probe can confirm a slug before the captured artifact is on
    disk — e.g. probe just ran but archive_agency hasn't completed
    yet, or the artifacts weren't committed. Probe state is the
    secondary source of truth."""
    base = tmp_path / "transparency"
    base.mkdir()
    state = {"agencies": {
        "aid-1": {"found": "probe-only-confirmed-pd"},
        "aid-2": {"found": None},  # not found yet, ignored
        "aid-3": {},                # never probed, ignored
    }}
    confirmed = confirmed_slugs(base, probe_state=state)
    assert confirmed == {"probe-only-confirmed-pd"}


def test_confirmed_slugs_handles_missing_data_dir(tmp_path):
    """Fresh checkouts without crawled data shouldn't crash the prune."""
    missing = tmp_path / "does-not-exist"
    assert confirmed_slugs(missing) == set()


def test_confirmed_slugs_includes_txt_only_directories(tmp_path):
    """A `.txt` capture without a `.json` means the parser failed,
    but the slug itself is real — Flock served a 200 with portal
    content. The parser bug is a separate problem; the slug shouldn't
    be pruned just because we can't parse it yet."""
    base = tmp_path / "transparency"
    base.mkdir()
    (base / "txt-only").mkdir()
    (base / "txt-only" / "2026-04-19.txt").write_text("page text")
    assert confirmed_slugs(base) == {"txt-only"}
