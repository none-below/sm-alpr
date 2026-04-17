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
from flock_transparency import parse_sections


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
