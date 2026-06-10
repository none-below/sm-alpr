# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Tests for the PRA conduct ledger (scripts/build_pra_ledger.py).

Covers the pure derivation pieces that carry legal weight and are easy to
get subtly wrong:

  classify_agency_message — empty updates vs production vs corpus routing;
    a message releasing records must never count as empty whatever else it
    says, and "closed into the W012462 corpus" must read as routing.

  agency_text / extract_exemptions — exemption citations are counted only
    in the agency's own words. Requests pre-rebut exemptions, so counting
    echoed requester text would credit the agency with the requester's own
    citations.

  conduct_entry timing — first substantive response days against the
    10-day / 24-day statutory marks, with the portal auto-ack excluded.

  contradiction_refs — only relations beginning "contradicts" surface as
    contradiction links; scope-followup refs must not.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import build_pra_ledger as ledger  # noqa: E402

AUTO_ACK = (
    "Thank you for your interest in public records of the City of San Mateo. "
    "Record(s) Requested: 1. Things."
)


def msg(ts, role, body, segments=None):
    m = {"ts": ts, "sender_name": "x", "sender_role": role, "body": body}
    if segments is not None:
        m["body_segments"] = segments
    return m


def make_pra(messages, *, pra_id="W012999-010126", filed="2026-01-01",
             promises=None, extension_count=0, cited_by=None,
             prior_refs=None, status="closed"):
    return {
        "id": pra_id,
        "display_status": status,
        "curated": {"title": "t", "prior_pra_refs": prior_refs or []},
        "derived": {
            "filed_date": filed,
            "messages": messages,
            "promise_history": promises or [],
            "extension_count": extension_count,
            "cited_by": cited_by or [],
        },
    }


# --- classify_agency_message ---

def test_no_records_is_empty_update():
    assert ledger.classify_agency_message(
        "At this time there are no responsive records to your request."
    ) == "empty_update"


def test_no_records_responsive_ordering_also_matches():
    assert ledger.classify_agency_message(
        "A diligent search yielded no records responsive to your request."
    ) == "empty_update"


def test_bare_promise_bump_is_empty_update():
    assert ledger.classify_agency_message(
        "We will provide a further update no later than 5/20/2026."
    ) == "empty_update"


def test_production_beats_no_records():
    # Partial production: releases records while answering no-records on
    # other items. Must count as production, not an empty update.
    assert ledger.classify_agency_message(
        "Please see the attached records. There are no responsive records "
        "for Item 3."
    ) == "production"


def test_corpus_routing_beats_closure():
    assert ledger.classify_agency_message(
        "This request is encompassed by an ongoing review of records under "
        "W012462-040226; the City now considers this record request closed."
    ) == "corpus_routed"


def test_closure_detected():
    assert ledger.classify_agency_message(
        "The City now considers this record request W012999-010126 closed."
    ) == "closure"


def test_substantive_answer_is_other():
    assert ledger.classify_agency_message(
        "The Support Services Captains during that period were as follows."
    ) == "other"


# --- agency_text / exemptions exclude echoed requester text ---

def test_echo_segments_excluded_from_exemptions():
    m = msg("2026-01-05T10:00:00", "agency", "ignored when segments exist",
            segments=[
                {"type": "echo",
                 "text": "As you are aware, § 7923.600 covers only "
                         "investigatory records and cannot apply here."},
                {"type": "after",
                 "text": "Responsive records are withheld."},
            ])
    assert ledger.extract_exemptions(ledger.agency_text(m)) == []


def test_agency_own_citation_counted():
    m = msg("2026-01-05T10:00:00", "agency", "x",
            segments=[
                {"type": "after",
                 "text": "Records are exempt under Gov. Code § 7923.600 and "
                         "attorney-client privilege."},
            ])
    keys = ledger.extract_exemptions(ledger.agency_text(m))
    assert "gov_7923_600" in keys
    assert "atty_client" in keys


def test_evidence_code_1040_matched_without_false_1040s():
    assert "evid_1040" in ledger.extract_exemptions(
        "withheld pursuant to Evidence Code section 1040")
    assert "evid_1040" not in ledger.extract_exemptions(
        "produced 1040 pages of records")


# --- streaks ---

def test_max_streak_counts_consecutive_empty_handed():
    kinds = ["other", "empty_update", "corpus_routed", "empty_update",
             "production", "empty_update"]
    assert ledger.max_streak(kinds) == 3


# --- conduct_entry timing ---

def test_first_response_skips_auto_ack_and_flags_late():
    messages = [
        msg("2026-01-01T09:00:00", "agency", AUTO_ACK),
        msg("2026-01-14T09:00:00", "agency",
            "There are no responsive records at this time."),
    ]
    e = ledger.conduct_entry(make_pra(messages), [], "2026-02-01")
    assert e["first_response"]["days"] == 13
    assert e["first_response"]["past_10day"] is True
    assert e["first_response"]["past_24day"] is False
    assert "first_response_past_10_days" in e["flags"]


def test_first_response_within_window_unflagged():
    messages = [
        msg("2026-01-01T09:00:00", "agency", AUTO_ACK),
        msg("2026-01-08T09:00:00", "agency", "Please see the attached records."),
    ]
    e = ledger.conduct_entry(make_pra(messages), ["2026-01-09"], "2026-02-01")
    assert e["first_response"]["past_10day"] is False
    assert e["flags"] == []
    assert e["production"]["files"] == 1


def test_open_request_with_no_substantive_response_flagged():
    messages = [msg("2026-01-01T09:00:00", "agency", AUTO_ACK)]
    e = ledger.conduct_entry(
        make_pra(messages, status="awaiting_initial"), [], "2026-01-20")
    assert e["first_response"] is None
    assert "no_substantive_response_past_10_days" in e["flags"]


def test_promise_slip_days():
    promises = [
        {"promise_date": "2026-03-17", "set_on": "2026-03-13"},
        {"promise_date": "2026-05-22", "set_on": "2026-05-06"},
    ]
    messages = [
        msg("2026-03-11T09:00:00", "agency", AUTO_ACK),
        msg("2026-03-13T09:00:00", "agency",
            "We will respond no later than 3/17/2026."),
    ]
    e = ledger.conduct_entry(
        make_pra(messages, promises=promises, extension_count=1), [],
        "2026-06-01")
    assert e["promises"]["slip_days"] == 66
    assert e["promises"]["extensions"] == 1


def test_override_closed_without_closure_phrase_ends_at_last_message():
    # W012807 pattern: status_override "closed" but the agency's final
    # message lacks the canonical "considers ... closed" phrasing. days_open
    # must end at the last message, not run to the build date.
    messages = [
        msg("2026-05-28T09:00:00", "agency", AUTO_ACK),
        msg("2026-06-04T09:00:00", "agency",
            "Records are exempt under Gov. Code § 7923.600."),
    ]
    e = ledger.conduct_entry(make_pra(messages, filed="2026-05-28"), [],
                             "2026-06-09")
    assert e["closed_date"] is None
    assert e["end_date"] == "2026-06-04"
    assert e["days_open"] == 7


def test_explicit_closure_date_still_wins():
    messages = [
        msg("2026-01-01T09:00:00", "agency", AUTO_ACK),
        msg("2026-01-05T09:00:00", "agency",
            "The City now considers this record request closed."),
    ]
    e = ledger.conduct_entry(make_pra(messages), [], "2026-02-01")
    assert e["closed_date"] == "2026-01-05"
    assert e["end_date"] == "2026-01-05"
    assert e["days_open"] == 4


# --- contradiction links ---

def test_contradiction_refs_filter():
    refs = [
        {"ref": "W012328-031226", "relation": "scope-followup", "note": "n1"},
        {"ref": "W012328-031226", "relation": "contradicts-denial", "note": "n2"},
    ]
    out = ledger.contradiction_refs(refs)
    assert len(out) == 1
    assert out[0]["ref"] == "W012328-031226"
    assert out[0]["note"] == "n2"


def test_contradicted_denial_flag_from_cited_by():
    messages = [
        msg("2026-01-01T09:00:00", "agency", AUTO_ACK),
        msg("2026-01-05T09:00:00", "agency",
            "No responsive records. The City considers this request closed."),
    ]
    e = ledger.conduct_entry(
        make_pra(messages, cited_by=[
            {"ref": "W013000-020126", "relation": "contradicts-denial",
             "note": "later production"},
        ]), [], "2026-02-01")
    assert "denial_contradicted_by_later_production" in e["flags"]
    assert e["contradictions"]["contradicted_by"][0]["ref"] == "W013000-020126"


# --- aggregate ---

def test_aggregate_counts():
    e1_msgs = [
        msg("2026-01-01T09:00:00", "agency", AUTO_ACK),
        msg("2026-01-14T09:00:00", "agency",
            "No responsive records at this time."),
    ]
    e2_msgs = [
        msg("2026-02-01T09:00:00", "agency", AUTO_ACK),
        msg("2026-02-03T09:00:00", "agency",
            "This request is encompassed by W012462-040226."),
    ]
    entries = [
        ledger.conduct_entry(make_pra(e1_msgs), [], "2026-03-01"),
        ledger.conduct_entry(
            make_pra(e2_msgs, pra_id="W013001-020126", filed="2026-02-01"),
            [], "2026-03-01"),
    ]
    agg = ledger.aggregate(entries)
    assert agg["pra_count"] == 2
    assert agg["first_response"]["past_10day"] == 1
    assert agg["first_response"]["within_10day"] == 1
    assert agg["corpus_routed"] == ["W013001-020126"]
