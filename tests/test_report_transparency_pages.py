# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""build_report_data links each report to the agency's public transparency
pages across vendors: its Flock portal (when crawled), a published Axon ALPR
page, and public Skydio drone-flight dashboards.

Focus: only records linked to a registry agency_id count (an unconfirmed
`candidate` doesn't); an Axon tenant that publishes nothing and a non-public
Skydio dashboard are left out; a missing vendor directory is not an error.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import build_report_data


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _tenant(axon, key, **fields):
    _write(axon / key / "tenant.json", {"host": f"{key}.evidence.com", **fields})


def _org(skydio, org_id, agency_id, dashboards):
    _write(skydio / org_id / "org.json",
           {"org_id": org_id, "agency_id": agency_id, "dashboards": dashboards})


def test_vendor_pages(tmp_path):
    axon, skydio = tmp_path / "axon", tmp_path / "skydio"
    _tenant(axon, "pacificapdca", agency_id="pacifica", published=True)
    _tenant(axon, "sanmateopdca", agency_id="san-mateo", published=False)
    _tenant(axon, "clearlakepd", agency_id=None, published=True,
            candidate={"agency_id": "clearlake"})
    _org(skydio, "org-sm", "san-mateo", [
        {"access": "public", "url": "https://www.arcgis.com/apps/dashboards/aaa"},
        {"access": "org", "url": "https://www.arcgis.com/apps/dashboards/bbb"},
    ])
    _org(skydio, "org-unlinked", None, [
        {"access": "public", "url": "https://www.arcgis.com/apps/dashboards/ccc"},
    ])

    pages = build_report_data._load_vendor_pages(axon, skydio)

    assert dict(pages) == {
        "pacifica": [{"vendor": "Axon", "kind": "alpr", "label": "Axon ALPR",
                      "url": "https://pacificapdca.evidence.com/alpr/public/policy"}],
        "san-mateo": [{"vendor": "Skydio", "kind": "drone", "label": "Skydio drone flights",
                       "url": "https://www.arcgis.com/apps/dashboards/aaa"}],
    }


def test_missing_vendor_dirs(tmp_path):
    assert build_report_data._load_vendor_pages(tmp_path / "a", tmp_path / "b") == {}


def test_flock_first_then_vendors():
    vendor = {"pacifica": [{"vendor": "Axon", "kind": "alpr", "label": "Axon ALPR",
                            "url": "https://pacificapdca.evidence.com/alpr/public/policy"}]}
    reg = {"agency_id": "pacifica", "slug": "pacifica-ca-pd",
           "flock_active_slug": "pacifica-ca-pd", "flock_slugs": ["pacifica-ca-pd"]}

    crawled = build_report_data._transparency_pages(reg, True, vendor)
    assert [p["vendor"] for p in crawled] == ["Flock", "Axon"]
    assert crawled[0]["url"] == "https://transparency.flocksafety.com/pacifica-ca-pd"

    uncrawled = build_report_data._transparency_pages(reg, False, vendor)
    assert [p["vendor"] for p in uncrawled] == ["Axon"]
