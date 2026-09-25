"""Tests for scripts/pra_portal_discover.py and scripts/pra_portal_harvest.py.

Network-free: covers candidate-name generation, the CA/non-CA registry
filter, ALPR relevance matching, and the GovQA archive parsing that the
live harvest depends on (DevExpress callback encoding, grid row parsing).
"""

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import pra_portal_discover as disc  # noqa: E402
import pra_portal_harvest as harv  # noqa: E402


def test_candidates_cover_known_tenants():
    nr = disc.candidates("nextrequest")
    # Real tenants found in the wild, one per naming pattern.
    for sub in ["losaltosca", "cityofalamedaca", "oaklandca", "adelanto", "cityofanaheimcapd",
                "city-of-salinas-ca", "suttercountyca", "fresnosheriff", "samtrans"]:
        assert sub in nr, sub
    assert "place:Los Altos" in nr["losaltosca"]
    gq = disc.candidates("govqa")
    for sub in ["fullerton", "sanmateoca", "chulavistaca", "eldoradocountyca"]:
        assert sub in gq, sub
    assert "palmspringsca" in disc.candidates("justfoia")


def test_candidates_are_valid_subdomains():
    for platform in ("nextrequest", "govqa", "justfoia"):
        for sub in disc.candidates(platform):
            assert sub == sub.lower() and " " not in sub and "'" not in sub, sub


def test_norm_strips_accents_and_punctuation():
    assert disc.norm("La Cañada Flintridge") == "la canada flintridge"
    assert disc.norm("St. Helena") == "st helena"


def test_build_drops_non_ca_and_flags_searchable(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "justfoia_found.json").write_text(json.dumps({
        "palmspringsca.justfoia.com": {"status": 200, "organization": "Palm Springs, CA",
                                       "search_enabled": True, "sources": ["place:Palm Springs"]},
        "auburn.justfoia.com": {"status": 200, "organization": "Auburn University",
                                "search_enabled": True, "sources": ["place:Auburn"]},
        "cityofbrea.justfoia.com": {"status": 200, "organization": "Brea, CA",
                                    "search_enabled": False, "sources": ["place:Brea"]},
    }))
    (cache / "nextrequest_found.json").write_text(json.dumps({
        "losaltosca.nextrequest.com": {"status": 200, "total_count": 5000, "sources": ["place:Los Altos"]},
        "arvin.nextrequest.com": {"status": 200, "total_count": 0, "sources": ["place:Arvin"]},
    }))
    (cache / "govqa_found.json").write_text(json.dumps({
        "uci.govqa.us": {"status": 200, "title": "University of California Irvine | Public Records Portal",
                         "archive": True, "sources": ["extra"]},
    }))
    registry = tmp_path / "pra_portals.json"
    monkeypatch.setattr(disc, "REGISTRY", registry)
    monkeypatch.setattr(disc, "REPO", tmp_path)

    class Args:
        pass

    args = Args()
    args.cache, args.keep_non_ca = str(cache), False
    disc.cmd_build(args)
    portals = {p["host"]: p for p in json.loads(registry.read_text())["portals"]}
    assert "auburn.justfoia.com" not in portals
    assert portals["uci.govqa.us"]["ca_confirmed"] is True  # "California" beats "University"
    assert portals["palmspringsca.justfoia.com"]["searchable"] is True
    assert portals["palmspringsca.justfoia.com"]["ca_confirmed"] is True
    assert portals["cityofbrea.justfoia.com"]["searchable"] is False
    assert portals["losaltosca.nextrequest.com"]["ca_confirmed"] is True  # 'ca' suffix
    assert portals["losaltosca.nextrequest.com"]["agency"] == "Los Altos"
    assert portals["arvin.nextrequest.com"]["searchable"] is False
    assert portals["arvin.nextrequest.com"]["ca_confirmed"] is None  # bare name only


def test_is_alpr():
    yes = ["Flock Safety network audit", "ALPR policy", "automated license plate reader data",
           "LPR hits for 2025", "Organization Audit export", "Vigilant hot list", "hotlist entries",
           "Los Altos CA PD_NETWORK AUDIT 2024.xlsx", "license plate recognition cameras"]
    no = ["collision report with license plate 7ABC123", "IT network security assessment",
          "flocking permit for holiday trees", "audit of the city budget"]
    for t in yes:
        assert harv.is_alpr(t), t
    for t in no:
        assert not harv.is_alpr(t), t


def test_grid_callback_param():
    # Captured from a live GovQA archive: page 2 of the DevExpress grid.
    assert harv.GovQA.grid_callback_param("PAGERONCLICK", "PN1") == "c0:GB|20;12|PAGERONCLICK3|PN1;"
    assert harv.GovQA.grid_callback_param("PAGERONCLICK", "PN10") == "c0:GB|21;12|PAGERONCLICK4|PN10;"


def test_govqa_rows_parse():
    row = (
        '<tr id="gridView_DXDataRow0" class="dxgvDataRow_MaterialCompact">'
        '<td class="dxgv dx-al" aria-label="Request Number: R001014-081126">R001014-081126</td>'
        '<td aria-label="Create Date: 8/11/2026 8:13:00 AM">8/11/2026</td>'
        '<td aria-label="Summary: All Flock audit logs &amp; network audits">...</td>'
        '<td aria-label="Request Status: Full Release">Full Release</td>'
        '<td><a href="javascript:void(0);" onclick="redirectInfo(&#39;18617&#39;)">View</a></td></tr>'
    )
    rows = harv.GovQA._rows("<table>" + row + "</table>")
    assert rows == [{"number": "R001014-081126", "rid": "18617", "date": "8/11/2026 8:13:00 AM",
                     "status": "Full Release", "summary": "All Flock audit logs & network audits"}]


def test_wanted_filters_media_and_size():
    assert harv.wanted({"title": "Network Audit 2025.xlsx", "ext": "xlsx"}, False, None)
    assert harv.wanted({"title": "export.csv", "ext": "csv"}, False, None)
    assert not harv.wanted({"title": "Invoice 123.pdf", "ext": "pdf"}, False, None)
    assert harv.wanted({"title": "Invoice 123.pdf", "ext": "pdf"}, True, None)
    assert not harv.wanted({"title": "bodycam.mp4", "ext": "mp4"}, True, None)
    assert not harv.wanted({"title": "audit.zip", "ext": "zip", "size": 3e9}, False, 500)


def test_doc_prefix_is_stable_per_platform():
    # GovQA postback targets repeat across requests (ctl00 on every request),
    # so the slot is only a per-request prefix; the manifest key adds request_id.
    assert harv.doc_prefix({"id": "rptAttachments$ctl03$lnkStreamCloud"}) == "ctl03"
    assert harv.doc_prefix({"id": 66770337}) == "66770337"
    assert harv.doc_prefix({"id": "4d0fa018-6d7a-4bfe-9de1-3377153e904c"}) == "4d0fa018-6d7"


def test_alpr_context_centers_on_match():
    text = "x" * 500 + " records re: Flock Safety camera audit " + "y" * 500
    ctx = harv.alpr_context(text, width=120)
    assert "Flock" in ctx and len(ctx) == 120
    assert harv.alpr_context("no match here", width=5) == "no ma"


def test_throttle_widens_on_429():
    t = harv.Throttle(1.0)
    t.wait(pause=0.001)
    assert abs(t.interval - 1.25) < 1e-9


def test_trusted_search_terms_are_single_words():
    # GovQA ORs the words of a multi-word search, so trusting e.g. "license
    # plate recognition" pulled in every business-license request (Encinitas: 52).
    assert all(" " not in t for t in harv.SPECIFIC_TERMS)
    assert harv.SPECIFIC_TERMS <= set(harv.SEARCH_TERMS)


def test_underscore_file_names_match():
    assert harv.is_alpr("Flock_Safety_INV-90909_2026-04-01.pdf")
    assert harv.wanted({"title": "event_log_for_last_30_days_Redacted.pdf", "ext": "pdf"}, False, None)
    assert harv.wanted({"title": "1_1_2025-1_31_2025-Fullerton_CA_PD-Network-Audit.pdf", "ext": "pdf"}, False, None)
