"""Pin the heading-kind gating fix in flock_transparency.parse_sections.

Body text that starts with a known heading prefix (e.g. "California SVS,
NCMEC Amber Alert" starts with the "California SVS" prefix) should NOT
be promoted to a section heading — prefix matches require independent
bold-heading evidence from the HTML. Exact and dynamic matches stay
trusted because structural dividers like "Policies"/"Usage" aren't
necessarily styled as bold headings.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from flock_transparency import (
    extract_bold_headings,
    parse_sections,
    parse_portal_text,
    _match_heading,
    _parse_number,
    _parse_org_names,
)


def test_prefix_match_in_body_not_promoted_without_bold_evidence():
    """The regression this fix addressed: "California SVS, NCMEC Amber
    Alert" reads as body text in agencies that don't actually have a
    California SVS section. With bold_headings={"Policies"} only, the
    parser must NOT treat this line as a heading.
    """
    text = "\n".join([
        "Policies",
        "",
        "California SVS, NCMEC Amber Alert",
        "",
        "additional acknowledgements here",
        "",
    ])
    sections, _unknown = parse_sections(text, bold_headings={"Policies"})
    headings = [s[0] for s in sections]
    assert "Policies" in headings
    assert "California SVS, NCMEC Amber Alert" not in headings, (
        f"Body text was incorrectly promoted to a heading: {headings}"
    )


def test_prefix_match_stays_when_bold_evidence_present():
    """Symmetric: if the portal HTML DID bold the "California SVS"
    header, the parser must still treat it as a heading so the body
    under it is captured.
    """
    text = "\n".join([
        "California SVS",
        "",
        "full content of the California SVS statement goes here",
        "",
    ])
    sections, _unknown = parse_sections(
        text, bold_headings={"California SVS"}
    )
    headings = [s[0] for s in sections]
    assert "California SVS" in headings, (
        f"Prefix-matched heading was dropped: {headings}"
    )


def test_exact_match_headings_accepted_without_bold_evidence():
    """Structural dividers like "Policies" match exactly in
    _HEADING_MAP; they must remain headings even when not present in
    bold_headings (they're not styled bold in the source HTML but
    still demarcate structure)."""
    text = "\n".join([
        "Policies",
        "",
        "some body",
        "",
    ])
    sections, _unknown = parse_sections(text, bold_headings=set())
    headings = [s[0] for s in sections]
    assert "Policies" in headings


# ─── 2026 layout regression tests ─────────────────────────────────
# Flock redesigned the transparency portal in 2026: section dividers
# are now CSS-uppercased (Overview → OVERVIEW), field headings use
# font-weight:600 (was 700), and every numeric stat gained a
# description sentence above the value. The original parser silently
# extracted "30" from "Number of …last 30 days." prose for every stat.

def test_uppercase_section_dividers_recognized():
    """The 2026 layout renders Overview / Policies / Usage / Additional
    Info as ALL CAPS via text-transform:uppercase, so the .txt sees
    them upper-cased. They must be matched as structural headings."""
    text = "\n".join([
        "OVERVIEW",
        "",
        "agency overview prose",
        "",
        "POLICIES",
        "",
        "What's Detected",
        "",
        "License Plates",
        "",
        "USAGE",
        "",
        "ADDITIONAL INFO",
        "",
    ])
    sections, _ = parse_sections(text, bold_headings=set())
    headings = [s[0] for s in sections]
    for h in ("OVERVIEW", "POLICIES", "USAGE", "ADDITIONAL INFO"):
        assert h in headings, f"missing structural divider {h}: {headings}"


def test_parse_number_skips_description_prose():
    """The 2026 layout puts a description above the numeric value:

        Number of unique plate reads over the last 30 days.

        341,081

    A naive "first integer" search returns 30 instead of 341,081.
    The fix: prefer a line that is *only* a number.
    """
    body = (
        "Number of unique plate reads over the last 30 days.\n"
        "\n"
        "341,081"
    )
    assert _parse_number(body) == 341081


def test_parse_number_legacy_value_only_body():
    """Legacy layout had just the value as the body — must still work."""
    assert _parse_number("341,599") == 341599
    assert _parse_number("30 days") == 30


def test_parse_org_names_merges_company_suffix():
    """Flock's data has un-escaped commas in company names like
    "CA - Topgolf USA El Segundo, LLC". On the new grid layout the
    suffix lands in its own cell ("LLC" alone) — re-attach it.
    """
    body_new = (
        "CA - Topgolf USA El Segundo\n"
        "LLC\n"
        "CA - Wasco PD\n"
        "Extended Stay America (ESA) Management\n"
        "LLC\n"
        "Fairfield CA PD\n"
    )
    names = _parse_org_names(body_new)
    assert "CA - Topgolf USA El Segundo, LLC" in names
    assert "Extended Stay America (ESA) Management, LLC" in names
    assert "LLC" not in names, "standalone LLC should never survive merge"
    # Adjacent real entries stay intact
    assert "CA - Wasco PD" in names
    assert "Fairfield CA PD" in names


def test_parse_org_names_handles_one_per_line_layout():
    """The 2026 layout renders the partner list with one agency per
    line (was: comma-separated paragraph). Both must work, including
    when a description sentence precedes the list."""
    body_new = (
        "Organizations granted access to Foo PD data.\n"
        "\n"
        "Albany CA PD\n"
        "Antioch CA PD\n"
        "Arcadia CA PD\n"
        "Avenal CA PD"
    )
    assert _parse_org_names(body_new) == [
        "Albany CA PD", "Antioch CA PD", "Arcadia CA PD", "Avenal CA PD",
    ]
    body_legacy = "Albany CA PD, Antioch CA PD, Arcadia CA PD, Avenal CA PD"
    assert _parse_org_names(body_legacy) == [
        "Albany CA PD", "Antioch CA PD", "Arcadia CA PD", "Avenal CA PD",
    ]


def test_agency_prefixed_policy_heading_maps_to_policy_info():
    """Flock now bolds agency-prefixed policy headings like
    "Marin County Sheriff's Office Policy" as standalone <h*>. They
    used to live as body text under Additional Info. The dynamic
    pattern catches these variants and routes them to policy_info,
    while the existing ALPR-specific patterns still win for things
    like "X Police Department ALPR Policy".
    """
    assert _match_heading(
        "Marin County Sheriff's Office Policy"
    ) == "policy_info"
    assert _match_heading("X Police Department Policy") == "policy_info"
    assert _match_heading("Y Police Bureau Policy") == "policy_info"
    # ALPR-specific dynamic patterns still take precedence
    assert _match_heading("X Police Department ALPR Policy") == "alpr_policy"
    # Exact-mapped headings unaffected
    assert _match_heading("Hotlist Policy") == "hotlist_policy"
    assert _match_heading("Sharing Policy") == "sharing_with_partners"


def test_parse_org_names_description_only_empty_table():
    """When the sharing table is empty, the 2026 layout still renders
    the description sentence on its own. It must not be mistaken for a
    one-agency list (regression: PR #240 picked up
    "Organizations granted access to Carmel CA PD data." as an org)."""
    assert _parse_org_names(
        "Organizations granted access to Carmel CA PD data."
    ) == []
    assert _parse_org_names(
        "Organizations sharing with Culver City CA PD data."
    ) == []


def test_extract_bold_headings_matches_2026_layout():
    """The 2026 layout uses font-weight:600 for field headings (was
    700) and h3 + text-transform:uppercase for section dividers."""
    html = (
        '<p style="font-weight:600;font-size:16px">Hotlist Policy</p>'
        '<p style="font-weight:400">body content</p>'
        '<h3 style="text-transform:uppercase;font-weight:500">Overview</h3>'
    )
    headings = extract_bold_headings(html)
    assert "Hotlist Policy" in headings
    assert "Overview" in headings
    assert "OVERVIEW" in headings, (
        "h3 text-transform:uppercase elements must surface in their "
        "rendered form too, since .txt comes from inner_text()"
    )
    assert "body content" not in headings


def test_extract_bold_headings_matches_legacy_layout():
    """Legacy font-weight:700 must still be recognized."""
    html = '<p style="font-weight:700">Acceptable Use Policy</p>'
    assert "Acceptable Use Policy" in extract_bold_headings(html)


def test_extract_bold_headings_handles_arbitrary_heavy_weights():
    """Match any font-weight in the heading range (500–900). Body text
    is uniformly 400 across Flock's pages, so anything heavier should
    be considered heading-eligible. This guards against another silent
    parse degradation if Flock picks 550 or 800 in a future redesign.
    """
    html = (
        '<p style="font-weight:550">Future Heading</p>'
        '<p style="font-weight:800">Bolder Heading</p>'
        '<p style="font-weight:400">body content</p>'
        '<p style="font-weight:300">light label</p>'
    )
    headings = extract_bold_headings(html)
    assert "Future Heading" in headings
    assert "Bolder Heading" in headings
    assert "body content" not in headings
    assert "light label" not in headings


def test_parse_number_raises_on_unrecognized_multiparagraph_body():
    """Fail-loud guard: if Flock changes the layout in a way that buries
    the value in prose with no pure-number line and no "N days" line,
    raise instead of silently picking the first digit (the 2026-04-29
    bug). Single-paragraph bodies stay permissive for legacy compat.
    """
    import pytest
    body = (
        "Description sentence with the number 30 in it.\n"
        "\n"
        "Another paragraph with no digits"
    )
    with pytest.raises(ValueError, match="Flock layout may have changed"):
        _parse_number(body, field="vehicles_detected_30d", slug="test-agency")


def test_parse_number_handles_n_days_pattern():
    """data_retention bodies look like "30 days" (legacy) or
    "The number of days...\\n\\n30 days" (2026)."""
    assert _parse_number("30 days") == 30
    assert _parse_number(
        "The number of days data is retained.\n\n30 days"
    ) == 30


def test_portal_content_without_styled_headings_raises():
    """If the page has clearly-portal content ("What's Detected" etc.)
    but extract_bold_headings returned nothing, Flock changed the
    heading CSS and prefix-match gating is globally suppressed. Raise
    instead of saving mostly-empty JSON.
    """
    import pytest
    text = "\n".join([
        "What's Detected",
        "",
        "License Plates, Vehicles",
        "",
        "Acceptable Use Policy",
        "",
        "Some prose here.",
        "",
    ])
    with pytest.raises(ValueError, match="no styled headings"):
        parse_portal_text(text, "test-agency", "2026-05-03",
                          bold_headings=set())


def test_stub_page_without_portal_markers_does_not_raise():
    """A disabled/stub page that lacks portal markers should NOT raise
    on the empty-bold-headings check — that defense is content-gated."""
    text = "Page Not Found\n\nThis portal is no longer available.\n"
    # Should not raise — stub page, no portal markers present
    parse_portal_text(text, "test-agency", "2026-05-03",
                      bold_headings=set())


def test_overview_mentioning_flock_without_marker_raises():
    """If the overview mentions Flock Safety but the agency-name marker
    ('X uses Flock Safety [LPR] [Tt]echnology') doesn't match, that
    points at Flock rephrasing their boilerplate — surface it instead
    of silently dropping crawled_name."""
    import pytest
    text = "\n".join([
        "Overview",
        "",
        "Some agency partners with Flock Safety to deploy ALPR cameras.",
        "",
    ])
    with pytest.raises(ValueError, match="Flock may have rephrased"):
        parse_portal_text(text, "test-agency", "2026-05-03",
                          bold_headings={"Overview"})


def test_empty_overview_does_not_raise():
    """If overview body is empty (some agencies omit it), don't raise —
    crawled_name stays None, that's a known/acceptable state."""
    text = "Overview\n\n\n"
    result = parse_portal_text(text, "test-agency", "2026-05-03",
                               bold_headings={"Overview"})
    assert result["crawled_name"] is None


def test_custom_non_boilerplate_overview_does_not_raise():
    """Some agencies write their own overview that doesn't follow the
    Flock Safety boilerplate at all — NCRIC (Northern California
    Regional Intelligence Center) starts with '***draft version***'
    and never mentions Flock Safety. The marker check is gated on
    'Flock Safety' appearing in the overview, so non-boilerplate
    customizations don't trip it; crawled_name stays None silently."""
    text = "\n".join([
        "Overview",
        "",
        "***draft version*** The Northern California Regional Intelligence "
        "Center (NCRIC) is a multi-jurisdiction government program that "
        "serves fifteen counties in Northern California.",
        "",
    ])
    result = parse_portal_text(text, "ncric", "2026-05-05",
                               bold_headings={"Overview"})
    assert result["crawled_name"] is None


def test_heading_match_is_case_insensitive():
    """Flock has multiple case variants of the same heading across
    agencies ("Number of LPR cameras" / "Number of LPR Cameras" /
    "LPR Cameras"). Matching must be case-insensitive so we don't
    bloat _HEADING_MAP with every casing and silently miss new ones.
    """
    from flock_transparency import _match_heading
    assert _match_heading("Hotlist Policy") == "hotlist_policy"
    assert _match_heading("HOTLIST POLICY") == "hotlist_policy"
    assert _match_heading("hotlist policy") == "hotlist_policy"
    assert _match_heading("Number of Hotlist Hits") == "hotlist_hits_30d"
    assert _match_heading("NUMBER OF HOTLIST HITS") == "hotlist_hits_30d"


def test_unrecognized_bold_heading_raises():
    """A bold heading that isn't in _HEADING_MAP and doesn't match a
    dynamic noise pattern (month-year, *Transparency Portal) should
    raise — silently dropping it would lose that field's body."""
    import pytest
    text = "\n".join([
        "What's Detected",
        "",
        "License Plates",
        "",
        "Some Brand New Field",
        "",
        "value goes here",
        "",
    ])
    with pytest.raises(ValueError, match="not in _HEADING_MAP"):
        parse_portal_text(
            text, "test-agency", "2026-05-03",
            bold_headings={"What's Detected", "Some Brand New Field"},
        )


def test_prefix_matching_prefers_longest_match():
    """When two prefixes both match (e.g. "Sharing" and "Sharing
    Network Data With"), the longer one wins. Otherwise 12 agencies'
    org lists get silently routed to sharing_info instead of
    orgs_granted_access — a bug we hit on PR 170 reparse.
    """
    from flock_transparency import _match_heading
    assert _match_heading("Sharing Network Data With") == "orgs_granted_access"
    assert _match_heading("Sharing Policy") == "sharing_with_partners"
    assert _match_heading("Sharing Restrictions") == "sharing_restrictions"


def test_following_link_blurb_classifies_as_policy_info():
    """Some agencies (auburn-ca-pd) use a sentence-style heading like
    "Auburn PD's Policies and Procedures can be found at the following
    link:" instead of an exact heading like "Policy Documents". The
    dynamic pattern should route it to policy_info just like the
    "Link to..." / "To view..." prefixes do."""
    from flock_transparency import _match_heading_kind
    field, kind = _match_heading_kind(
        "Auburn PD's Policies and Procedures can be found at the following link:"
    )
    assert (field, kind) == ("policy_info", "dynamic")
    # Without the trailing colon, same routing.
    field, kind = _match_heading_kind(
        "Auburn PD's Policies and Procedures can be found at the following link"
    )
    assert (field, kind) == ("policy_info", "dynamic")
    # The "the following link" pattern is a fallthrough — if a heading
    # also matches a more specific earlier rule (exact/prefix from
    # _HEADING_MAP, or one of the alpr_policy dynamic patterns), the
    # earlier rule must win and route to its specific field.
    field, _kind = _match_heading_kind(
        "Full ALPR Policy can be found at the following link:"
    )
    assert field == "alpr_policy"
    field, _kind = _match_heading_kind(
        "Anytown Police Department Policy Manual can be found at the following link:"
    )
    assert field == "alpr_policy"


def test_none_prefixed_empty_state_does_not_raise():
    """Flock renders an empty-state for non-sharing agencies as a bold
    heading like "None: Alameda does not share with outside agencies".
    The dynamic noise pattern should swallow it instead of failing the
    unrecognized-bold-heading check (alameda-ca-pd, 2026-05-05)."""
    from flock_transparency import _match_heading_kind
    field, kind = _match_heading_kind(
        "None: Alameda does not share with outside agencies"
    )
    assert (field, kind) == (None, "dynamic")
    text = "What's Detected\n\nLicense Plates\n\n"
    parse_portal_text(
        text, "test-agency", "2026-05-05",
        bold_headings={
            "What's Detected",
            "None: Alameda does not share with outside agencies",
        },
    )


def test_month_year_bold_heading_does_not_raise():
    """Success-stories sections often have month-year subheadings
    ("April 2026"). Those are styled bold but aren't field headings —
    the dynamic noise pattern should swallow them."""
    text = "What's Detected\n\nLicense Plates\n\n"
    parse_portal_text(
        text, "test-agency", "2026-05-03",
        bold_headings={"What's Detected", "April 2026", "January 2026"},
    )
