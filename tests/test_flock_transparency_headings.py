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


def test_parse_number_data_unavailable_returns_none():
    """Flock shows 'Data Unavailable' in place of a value when a stat
    isn't ready (seen on kensington-ca-pd hotlist_hits_30d). Must
    return None rather than tripping the multi-paragraph error path."""
    body = "Total hotlist hits over the last 30 days.\n\nData Unavailable"
    assert _parse_number(body, field="hotlist_hits_30d", slug="kensington-ca-pd") is None
    # Case variations
    assert _parse_number("description.\n\ndata unavailable") is None
    assert _parse_number("description.\n\nDATA UNAVAILABLE") is None


def test_match_heading_new_2026_05_variants():
    """Heading variants observed in the 5/28/2026 rolling refresh batch.

    - oceanside-ca-pd added "Distinct Vehicles (No Duplicates) ..."
    - alameda-county-ca-so uses "ACSO Policy: GO 5.42 - ... (ALPR) System"
      (suffix "System" not "Policy") and posts case stories under
      "Solved Stories with Flock ALPR Technology - <case>".
    """
    assert _match_heading("Distinct Vehicles (No Duplicates) detected in the last 30 days") == "vehicles_detected_30d"
    assert _match_heading("ACSO Policy: GO 5.42 - Automated License Plate Recognition (ALPR) System") == "alpr_policy"
    assert _match_heading("Solved Stories with Flock ALPR Technology - Armed Robbery") == "success_stories"
    assert _match_heading("Solved Story with Flock ALPR Technology - X") == "success_stories"


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


def test_parse_org_names_drops_policy_disclaimer():
    """Some portals (LVMPD 2026-05) render a policy disclaimer in the
    orgs section instead of an agency list. The disclaimer doesn't
    match the "Organizations granted access" boilerplate, but it is
    obviously a sentence (long, ends with a period) rather than an
    agency name. Drop it so we don't ingest 137-char prose as a
    recipient agency (issue #209)."""
    assert _parse_org_names(
        "Access to or disclosure of ALPR data will only be provided "
        "to individuals within the department or other authorized "
        "governmental agencies."
    ) == []
    # Real agency names — even unusually long ones — survive: they
    # don't end with a period, so the heuristic doesn't touch them.
    assert _parse_org_names(
        "Ohio Department of Rehabilitation and Correction - Office of "
        "the Chief Inspector"
    ) == [
        "Ohio Department of Rehabilitation and Correction - Office of "
        "the Chief Inspector"
    ]


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


def test_spelled_out_alpr_policy_heading_classifies_as_alpr_policy():
    """Raleigh (2026-05-12) bolds the policy title with ALPR spelled
    out instead of as an acronym: "Raleigh Police Department Automated
    License Plate Recognition and Internet Protocol Camera System
    Policy". The spelled-out dynamic pattern parallels the existing
    ALPR/LPR-acronym pattern and routes these to alpr_policy."""
    from flock_transparency import _match_heading_kind
    field, _ = _match_heading_kind(
        "Raleigh Police Department Automated License Plate Recognition "
        "and Internet Protocol Camera System Policy"
    )
    assert field == "alpr_policy"
    # "Reader" / "Readers" singular and plural also accepted.
    field, _ = _match_heading_kind(
        "Foo PD Automated License Plate Reader Policy"
    )
    assert field == "alpr_policy"
    field, _ = _match_heading_kind(
        "Foo PD Automated License Plate Readers Usage Policy"
    )
    assert field == "alpr_policy"


def test_camera_count_variants_route_to_camera_count():
    """Two new camera-count heading variants seen in the wild:
    "Number of Flock LPR Cameras" (las-vegas-metro-nv-pd 2026-05-12)
    and "Number of LPRs" (prescott-valley-az-pd 2026-05-12). Both
    are aliases for the existing camera_count field."""
    assert _match_heading("Number of Flock LPR Cameras") == "camera_count"
    assert _match_heading("Number of LPRs") == "camera_count"
    # Case-insensitive (parser lowercases for lookup).
    assert _match_heading("number of flock lpr cameras") == "camera_count"
    assert _match_heading("NUMBER OF LPRS") == "camera_count"


def test_alpr_acronym_in_parens_classifies_as_alpr_policy():
    """Belmont/Pacifica (2026-05-06) introduced a heading shape with
    the (ALPR) acronym in parentheses rather than space-delimited:
    "Belmont Police Department - Automated License Plate Readers (ALPR) Policy".
    The dynamic regex must accept either ' ALPR ' or '(ALPR)' as a
    boundary so this routes to alpr_policy."""
    from flock_transparency import _match_heading_kind
    field, _ = _match_heading_kind(
        "Belmont Police Department - Automated License Plate Readers (ALPR) Policy"
    )
    assert field == "alpr_policy"
    field, _ = _match_heading_kind(
        "Pacifica Police Department Automated License Plate Reader (ALPR) Policy"
    )
    assert field == "alpr_policy"
    # The space-delimited variant must still match.
    field, _ = _match_heading_kind("Foo ALPR Policy")
    assert field == "alpr_policy"
    field, _ = _match_heading_kind("Foo LPR Policy")
    assert field == "alpr_policy"


def test_titled_success_story_subheading_classifies_as_success_stories():
    """Blue Springs (2026-05-10) posts titled success-story excerpts as
    bold subheadings like "Credit Card Skimming Ring - Success story".
    The dynamic pattern parallels the existing "X - Facebook Post - …"
    rule and routes them to success_stories so they don't trip the
    unrecognized-bold-heading check."""
    from flock_transparency import _match_heading_kind
    for h in (
        "Credit Card Skimming Ring - Success story",
        "Domestic Assault - Success Story",
        "Ulta Beauty Burglary - Success story",
        "Some Long Multi-Word Title - Success Stories",
    ):
        field, kind = _match_heading_kind(h)
        assert (field, kind) == ("success_stories", "dynamic"), (
            f"expected success_stories/dynamic for {h!r}, got {(field, kind)}"
        )


def test_additional_flock_safety_information_maps_to_additional_info():
    """Blue Springs (2026-05-10) uses "Additional Flock Safety
    Information" as a section heading — same role as the existing
    "Additional Info" / "Additional Information" aliases."""
    assert _match_heading("Additional Flock Safety Information") == "additional_info"


def test_lpr_and_other_cameras_heading_maps_to_additional_info():
    """Durango (2026-05-11) bolds "LPR and other Cameras" as a
    section heading (distinct from the existing "Number of LPR and
    other cameras" count). Conservative routing: additional_info,
    so the body is captured as text without an integer-parse attempt."""
    assert _match_heading("LPR and other Cameras") == "additional_info"
    # The count heading must still route to camera_count.
    assert _match_heading("Number of LPR and other cameras") == "camera_count"


def test_glossary_bold_heading_does_not_raise():
    """Durango (2026-05-11) bolds "Glossary" as documentation chrome —
    not a field heading. The dynamic noise pattern should swallow it."""
    from flock_transparency import _match_heading_kind
    field, kind = _match_heading_kind("Glossary")
    assert (field, kind) == (None, "dynamic")
    text = "What's Detected\n\nLicense Plates\n\n"
    parse_portal_text(
        text, "test-agency", "2026-05-11",
        bold_headings={"What's Detected", "Glossary"},
    )


def test_overview_marker_accepts_operating_system_phrasing():
    """Flock rephrased the overview boilerplate from
    '<Agency> uses Flock Safety [LPR] technology' to
    "<Agency> uses Flock Safety's Operating System" (seen on
    pacifica-ca-pd 2026-05-06). Both must extract crawled_name."""
    # Old phrasing: " uses Flock Safety LPR technology"
    text_old = (
        "Overview\n\n"
        "The Foo PD uses Flock Safety LPR technology to capture evidence.\n\n"
    )
    result = parse_portal_text(
        text_old, "foo-pd", "2026-05-06",
        bold_headings={"Overview"},
    )
    assert result["crawled_name"] == "The Foo PD"
    # New phrasing: " uses Flock Safety's Operating System"
    text_new = (
        "Overview\n\n"
        "The Pacifica Police Department uses Flock Safety's Operating "
        "System to capture objective evidence.\n\n"
    )
    result = parse_portal_text(
        text_new, "pacifica-ca-pd", "2026-05-06",
        bold_headings={"Overview"},
    )
    assert result["crawled_name"] == "The Pacifica Police Department"
    # Plain "technology" variant (no LPR) must still work.
    text_plain = (
        "Overview\n\n"
        "Bar PD uses Flock Safety technology for ALPR.\n\n"
    )
    result = parse_portal_text(
        text_plain, "bar-pd", "2026-05-06",
        bold_headings={"Overview"},
    )
    assert result["crawled_name"] == "Bar PD"


def test_overview_advance_template_extracts_name():
    """A second 2026 boilerplate embeds the agency name mid-sentence
    instead of as a subject prefix:
    "The use of Flock Safety technology helps advance the <Agency>'s
    public safety mission ..." (wilton-ct-pd 2026-05). The prefix
    strategy can't apply; _FLOCK_ADVANCE_RE must capture the embedded
    name and the parser must NOT raise."""
    text = (
        "Overview\n\n"
        "The use of Flock Safety technology helps advance the Wilton, CT "
        "Police Department's public safety mission by turning vehicle and "
        "license plate information into timely, objective leads that "
        "support crime prevention.\n\n"
    )
    result = parse_portal_text(text, "wilton-ct-pd", "2026-05-29",
                               bold_headings={"Overview"})
    assert result["crawled_name"] == "Wilton, CT Police Department"
    # No "the", curly apostrophe — name still extracted, possessive dropped.
    text2 = (
        "Overview\n\n"
        "The use of Flock Safety technology helps advance Stanford "
        "University’s public safety mission by deterring crime.\n\n"
    )
    result = parse_portal_text(text2, "x", "2026-05-29",
                               bold_headings={"Overview"})
    assert result["crawled_name"] == "Stanford University"
    # "Operating System" product term (which _FLOCK_MARKER_RE accepts) too.
    result = parse_portal_text(
        "Overview\n\nThe use of Flock Safety Operating System helps advance "
        "the City of Burlingame's public safety mission by acting on data.\n\n",
        "x", "2026-05-29", bold_headings={"Overview"})
    assert result["crawled_name"] == "City of Burlingame"


def test_overview_advance_template_rejects_nameless_and_garbage():
    """Hardening (adversarial review of _FLOCK_ADVANCE_RE): the embedded-
    name capture must not store a bare article/adjective or absorb a
    repeated boilerplate clause. A name-less rephrasing must fail loud."""
    import pytest
    # Name-less rephrasing: "advance the public safety mission of the
    # community" → captures the article "the"; the proper-noun guard
    # rejects it so the fail-loud raise fires instead of storing "the".
    with pytest.raises(ValueError, match="Flock may have rephrased"):
        parse_portal_text(
            "Overview\n\nThe use of Flock Safety technology helps advance the "
            "public safety mission of the community we serve.\n\n",
            "x", "2026-05-29", bold_headings={"Overview"})
    # Abstract/regional adjective fragment → also rejected.
    with pytest.raises(ValueError, match="Flock may have rephrased"):
        parse_portal_text(
            "Overview\n\nThe use of Flock Safety technology helps advance the "
            "shared regional public safety mission across agencies.\n\n",
            "x", "2026-05-29", bold_headings={"Overview"})
    # Two "helps advance" clauses on one line: the capture must not span
    # the first clause's gap; re.search re-anchors on the named clause.
    result = parse_portal_text(
        "Overview\n\nFlock Safety technology helps advance the goals of our "
        "city. Flock Safety technology helps advance the Beta County "
        "Sheriff's public safety mission here.\n\n",
        "x", "2026-05-29", bold_headings={"Overview"})
    assert result["crawled_name"] == "Beta County Sheriff"
    # A name legitimately ending in "s" (no apostrophe) must not be
    # over-stripped, and "Department of Public Safety" (name contains the
    # anchor words) must capture in full.
    result = parse_portal_text(
        "Overview\n\nThe use of Flock Safety technology helps advance the "
        "Three Rivers public safety mission today.\n\n",
        "x", "2026-05-29", bold_headings={"Overview"})
    assert result["crawled_name"] == "Three Rivers"
    # A genuinely-new phrasing that mentions Flock Safety but matches no
    # marker shape must still fail loud.
    with pytest.raises(ValueError, match="Flock may have rephrased"):
        parse_portal_text(
            "Overview\n\nWe partnered with Flock Safety to deploy cameras.\n\n",
            "y", "2026-05-29", bold_headings={"Overview"})


# ─── issue #223: 2026-05 rolling-refresh heading variants ──────────

def test_issue223_heading_variants_classify():
    """Every distinct heading variant that tripped the parser across the
    5/5–5/29 refresh batch (issue #223) must now resolve to a field or to
    structural noise (None) rather than _UNKNOWN."""
    from flock_transparency import _match_heading_kind, _UNKNOWN
    expected = {
        # spelled-out / no-prefix / Documentation policy titles
        "License Plate Reader Policy": "alpr_policy",                       # alexandria, charlottesville
        "Automated License Plate Reader Policy 470": "alpr_policy",         # mendocino
        "Automated License Plate Reader (ALPR) Acceptable Use Policy": "alpr_policy",  # muskegon
        "SDPD Automated License Plate Recognition (ALPR) Documentation": "alpr_policy",  # san-diego
        "APD Policy Manual: Automated License Plate Readers (Section 708)": "alpr_policy",  # arlington
        # exact aliases
        "System Access Policy": "access_policy",                           # arlington
        "Search Specifics": "additional_info",                             # anoka
        "Flock Safety Contract": "additional_info",                        # charlottesville
        "Access to external organizations": "orgs_granted_access",         # san-diego
        "Law enforcement data sharing": "sharing_with_partners",           # tulsa
        "Program Success": "success_stories",                              # murfreesboro
        # dynamic stat / FAQ / success-story variants
        "MVPD Flock Safety ALPR and Camera Use Information and Public FAQ": "additional_info",  # mill-valley
        "City of Saratoga Success Stories": "success_stories",             # santa-clara-county
        "Number of License Plate Reader Cameras": "camera_count",          # tulsa
        "Individual vehicles detected in the last 30 days": "vehicles_detected_30d",  # sparks
        "Total Searches by Sparks Police Department in the last 30 days": "searches_30d",  # sparks
        # structural noise (None)
        "*Hotlist hits last 30 days info": None,                           # johnson-city
        "Monterey PD: Camera Map Locations": None,                         # monterey
        "In Texas, license plates are not subject to Open Records Requests.": None,  # pflugerville
        "Live Feed Data retention (in days)": None,                        # mill-valley
    }
    for heading, want in expected.items():
        field, kind = _match_heading_kind(heading)
        assert field is not _UNKNOWN, f"{heading!r} stayed _UNKNOWN"
        assert field == want, f"{heading!r}: got {field!r}, want {want!r}"


def test_policy_pdf_url_not_promoted_to_heading():
    """Regression: the spelled-out ALPR-policy lookahead must NOT match a
    policy-PDF URL whose path contains hyphenated 'policy'/'alpr' fragments
    ('…/policies/apd-policy-424-alpr.pdf', albany-ca-pd). A bare
    \\bALPR\\b/\\bPolicy\\b match would fire on the URL and — because dynamic
    matches are trusted as headings — promote the URL line to a heading,
    dropping the real policy link from alpr_policy."""
    text = (
        "ALPR Policy\n\n"
        "https://www.albanyca.gov/files/assets/city/v/1/police/documents/"
        "policies/apd-policy-424-alpr.pdf\n\n"
    )
    bold = {"ALPR Policy"}
    result = parse_portal_text(text, "albany-ca-pd", "2026-05-05",
                               bold_headings=bold)
    assert result["alpr_policy"] == (
        "https://www.albanyca.gov/files/assets/city/v/1/police/documents/"
        "policies/apd-policy-424-alpr.pdf"
    )


def test_long_prose_line_not_promoted_to_heading():
    """Regression: a dynamic pattern can fire on a long prose line that
    happens to contain a title-like substring. Real headings are short, so
    an over-length (> _MAX_HEADING_LEN) line must stay body. Covers the
    napa-ca-pd overview ('… uses Automatic License Plate Reader technology …
    policies …', matches the spelled-out alpr_policy lookahead) and the
    ncric overview ('***draft version*** …', matches the '^\\*' footnote
    noise pattern)."""
    napa = (
        "Overview\n\n"
        "Napa Police Department uses Automatic License Plate Reader "
        "technology to capture objective evidence. In an effort to ensure "
        "proper usage and guardrails are in place, they have made the below "
        "policies and usage statistics available to the public for review "
        "and ongoing transparency about the program.\n\n"
    )
    result = parse_portal_text(napa, "napa-ca-pd", "2026-05-25",
                               bold_headings={"Overview"})
    assert result["overview"].startswith("Napa Police Department uses")
    assert result["alpr_policy"] == ""

    ncric = (
        "Overview\n\n"
        "***draft version*** The Northern California Regional Intelligence "
        "Center is a multi-jurisdiction government program that serves "
        "fifteen counties in Northern California and operates under a "
        "governance structure described elsewhere on this portal.\n\n"
    )
    result = parse_portal_text(ncric, "ncric", "2026-05-28",
                               bold_headings={"Overview"})
    assert result["overview"].startswith("***draft version***")


def test_parse_number_negative_infinity_returns_none():
    """Flock occasionally renders a JS sentinel ('-Infinity days') for an
    unset retention value (monte-sereno-ca-pd data_retention). Treat it
    like 'Data Unavailable' — return None, don't raise."""
    assert _parse_number(
        "The number of days data is retained.\n\n-Infinity days",
        field="data_retention", slug="monte-sereno-ca-pd",
    ) is None
    assert _parse_number("Infinity") is None


def test_parse_org_names_drops_none_empty_state():
    """When an agency shares with no one, some portals render the org-list
    body as just 'None' (san-diego-ca-pd 'Access to external
    organizations'). 'None' is an empty-state marker, not an agency."""
    assert _parse_org_names("None") == []
    assert _parse_org_names("N/A") == []
    # A real agency name that merely starts with "No..." is unaffected.
    assert _parse_org_names("Novato CA PD") == ["Novato CA PD"]
