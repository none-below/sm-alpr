#!/usr/bin/env python3
"""
Validate data/contracts/ source files: id uniqueness, cross-references
against both articles.json and assets/agency_registry.json, event types,
vendor enum, reason enum, date parseability, and US-bbox coords.

Run with: uv run pytest tests/test_contract_data.py
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from build_contract_map import (  # noqa: E402
    KNOWN_VENDORS,
    REASONS,
    US_BBOX,
    VALID_EVENT_TYPES,
    parse_event_date,
    validate,
)
from lib import agency_coords, load_registry, registry_by_id  # noqa: E402

SRC = ROOT / "data" / "contracts"


@pytest.fixture(scope="module")
def data():
    return {
        "events": json.loads((SRC / "events.json").read_text()),
        "articles": json.loads((SRC / "articles.json").read_text()),
    }


@pytest.fixture(scope="module")
def reg_by_id():
    load_registry.__globals__["_registry_cache"] = None  # force reload
    load_registry()
    return registry_by_id()


def test_sources_exist():
    assert (SRC / "events.json").is_file()
    assert (SRC / "articles.json").is_file()


def test_sources_parse_as_lists(data):
    assert isinstance(data["events"], list)
    assert isinstance(data["articles"], list)


def test_no_validation_errors(data, reg_by_id):
    errors, _ = validate(data["events"], data["articles"], reg_by_id)
    assert errors == [], "validation errors: " + "\n".join(errors)


def test_article_ids_unique(data):
    ids = [a["id"] for a in data["articles"]]
    assert len(ids) == len(set(ids))


def test_event_types_valid(data):
    for ev in data["events"]:
        assert ev.get("type") in VALID_EVENT_TYPES, ev


def test_event_agency_refs_resolve_to_registry(data, reg_by_id):
    for ev in data["events"]:
        assert ev["agency_id"] in reg_by_id, (
            f"event references agency_id {ev['agency_id']!r} not in registry"
        )


def test_event_article_refs_valid(data):
    article_ids = {a["id"] for a in data["articles"]}
    for ev in data["events"]:
        for aid in ev.get("article_ids") or []:
            assert aid in article_ids, (ev, aid)


def test_referenced_agencies_have_coords(data, reg_by_id):
    for ev in data["events"]:
        reg = reg_by_id[ev["agency_id"]]
        lat, lng = agency_coords(reg)
        assert lat is not None and lng is not None, (
            f"registry entry for {ev['agency_id']} lacks coords"
        )
        assert US_BBOX["lat_min"] <= lat <= US_BBOX["lat_max"]
        assert US_BBOX["lng_min"] <= lng <= US_BBOX["lng_max"]


def test_dates_parseable(data):
    for ev in data["events"]:
        parse_event_date(ev.get("date"))


def test_event_vendors(data):
    for ev in data["events"]:
        v = ev.get("vendor")
        if v is None:
            continue
        assert isinstance(v, str)


def test_event_reasons_valid(data):
    for ev in data["events"]:
        for r in ev.get("reasons") or []:
            assert r in REASONS, f"unknown reason {r!r} in event {ev}"


def test_parse_date_formats():
    assert parse_event_date("2025-06-15")[2] is True
    assert parse_event_date("2025-06")[2] is False
    assert parse_event_date("2025")[2] is False
    assert parse_event_date(None)[1] == "undated"
    with pytest.raises(ValueError):
        parse_event_date("not-a-date")
    with pytest.raises(ValueError):
        parse_event_date("2025-13-01")


def test_known_vendors_includes_flock():
    assert "flock" in KNOWN_VENDORS


def test_reasons_metadata_shape():
    for code, meta in REASONS.items():
        assert "label" in meta
        assert "icon" in meta
        assert "color" in meta
