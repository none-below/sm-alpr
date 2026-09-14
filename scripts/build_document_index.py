#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Build a searchable index of every document this investigation has produced
or cited: PRA-produced attachments, curated policy/reference PDFs, and the
hand-picked citations in docs/SMPD_ALPR_Findings.md's Source Documents table.

Two layers, merged on local asset path:
  - Catalog: a filesystem walk of the asset roots below (WALK_ROOTS), giving
    full coverage of everything on disk, with a best-effort blurb (a PRA's
    curated metadata.json title, or a humanized filename).
  - Cited: parsed from the Source Documents table. Richer, hand-written
    descriptions. A cited row that links to a local assets/ file merges
    into that file's catalog entry (marking it `featured` with its source
    number); a row linking elsewhere (Wayback, city website, PrimeGov)
    becomes its own external-only entry.

Deliberately NOT indexed: assets/transparency.flocksafety.com/ (scraped,
untrusted, and already surfaced via the sharing map/scoreboard/audit-check),
assets/articles/ (already searchable on articles.html), and the raw audit
corpora under redwood-city-pras/ and los-altos-pras/ (network-audit exports,
not human-readable documents).

Output: docs/data/document_index.json

Usage:
  uv run python scripts/build_document_index.py
"""

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "docs" / "data" / "document_index.json"
FINDINGS_MD = REPO_ROOT / "docs" / "SMPD_ALPR_Findings.md"

GITHUB_BLOB_PREFIX = "https://github.com/none-below/sm-alpr/blob/main/"

DOC_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "csv", "png", "jpg", "jpeg", "zip"}

DATE_IN_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
PRA_ID_RE = re.compile(r"^W\d{6}-\d{6}$")

# City-keyed reference/case-file roots under assets/public-records/<city>/.
CITY_AGENCY = {
    "san-mateo": ("san-mateo-pd", "San Mateo PD"),
    "east-palo-alto": ("east-palo-alto-pd", "East Palo Alto PD"),
    "stockton": ("stockton-pd", "Stockton PD"),
}
SMPD = ("san-mateo-pd", "San Mateo PD")


def humanize(name: str) -> str:
    stem = re.sub(r"\.[0-9a-f]{6,10}\.txt$", "", name)  # defensive; shouldn't hit
    stem = Path(stem).stem
    stem = re.sub(r"^\d+[_-]", "", stem)  # drop a leading NN_ source-number prefix
    stem = re.sub(r"[_-]?\d{4}-\d{2}-\d{2}$", "", stem)  # drop a trailing date; shown separately
    stem = re.sub(r"[_-]+", " ", stem).strip()
    return stem or name


def extract_date(name: str) -> str | None:
    matches = DATE_IN_NAME_RE.findall(name)
    return matches[-1] if matches else None


def github_url(relpath: str) -> str:
    return GITHUB_BLOB_PREFIX + quote(relpath, safe="/")


def detect_agency_from_filename(name: str):
    low = name.lower()
    if "stockton" in low:
        return ("stockton-pd", "Stockton PD")
    return SMPD


def load_pra_metadata() -> dict:
    """id -> {title, summary, tags} from each W-folder's curated metadata.json.

    Deliberately reads only the curated half (hand-written JSON) rather than
    depending on docs/data/pra_registry.json, which also requires parsing
    every Message_History.pdf and isn't part of `make build`'s default set.
    """
    out = {}
    root = REPO_ROOT / "assets" / "san-mateo-public-records"
    if not root.is_dir():
        return out
    for folder in root.iterdir():
        if not folder.is_dir() or not PRA_ID_RE.match(folder.name):
            continue
        meta_path = folder / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out[folder.name] = {
            "title": meta.get("title"),
            "summary": meta.get("summary"),
            "tags": meta.get("tags", []),
        }
    return out


def classify_smpd_file(relpath: Path, pra_meta: dict):
    """One file under assets/san-mateo-public-records/. Returns a catalog
    entry dict, or None if it should be skipped."""
    parts = relpath.parts  # ("assets", "san-mateo-public-records", <sub>, ..., filename)
    sub = parts[2]
    filename = parts[-1]

    if PRA_ID_RE.match(sub):
        meta = pra_meta.get(sub, {})
        title = meta.get("title") or humanize(filename)
        return {
            "source_type": "pra_attachment",
            "agency_key": SMPD[0],
            "agency_label": SMPD[1],
            "blurb": title,
            "tags": meta.get("tags", []),
            "context_title": meta.get("title"),
            "context_url": f"pras.html#{sub}/{quote(filename)}",
        }
    if sub == "emails":
        agency = detect_agency_from_filename(filename)
        return {
            "source_type": "correspondence",
            "agency_key": agency[0],
            "agency_label": agency[1],
            "blurb": humanize(filename),
            "tags": [],
            "context_title": "Investigation correspondence",
            "context_url": None,
        }
    if sub.startswith("smcso-pra-"):
        return {
            "source_type": "pra_attachment",
            "agency_key": "smcso",
            "agency_label": "San Mateo County SO",
            "blurb": humanize(filename),
            "tags": [],
            "context_title": f"SMCSO public records response ({sub})",
            "context_url": None,
        }
    if sub.startswith("stockton-pra-"):
        return {
            "source_type": "pra_attachment",
            "agency_key": "stockton-pd",
            "agency_label": "Stockton PD",
            "blurb": humanize(filename),
            "tags": [],
            "context_title": f"Stockton PD public records response ({sub})",
            "context_url": None,
        }
    if sub.startswith("portal-archives-"):
        agency = detect_agency_from_filename(filename)
        return {
            "source_type": "portal_snapshot",
            "agency_key": agency[0],
            "agency_label": agency[1],
            "blurb": humanize(filename),
            "tags": [],
            "context_title": f"Transparency-portal snapshot archive ({sub})",
            "context_url": None,
        }
    return None  # unrecognized subfolder — skip rather than guess


def classify_public_records_file(relpath: Path):
    parts = relpath.parts  # ("assets", "public-records", city, ...rest)
    city = parts[2]
    filename = parts[-1]
    agency = CITY_AGENCY.get(city, (city, humanize(city) + " PD"))
    is_reference = "reference" in parts[3:-1]
    if is_reference:
        return {
            "source_type": "reference",
            "agency_key": agency[0],
            "agency_label": agency[1],
            "blurb": humanize(filename),
            "tags": [],
            "context_title": f"{agency[1]} — reference document",
            "context_url": None,
        }
    case_id = parts[3] if len(parts) > 4 else None
    return {
        "source_type": "pra_attachment",
        "agency_key": agency[0],
        "agency_label": agency[1],
        "blurb": humanize(filename),
        "tags": [],
        "context_title": f"{agency[1]} public records response"
        + (f" ({case_id})" if case_id else ""),
        "context_url": None,
    }


def classify_grand_jury_file(filename: str):
    return {
        "source_type": "report",
        "agency_key": "san-mateo-county",
        "agency_label": "San Mateo County",
        "blurb": humanize(filename),
        "tags": [],
        "context_title": "San Mateo County Civil Grand Jury — ALPR report",
        "context_url": None,
    }


# (root under assets/, classifier). Classifiers receive the path relative to
# REPO_ROOT and return a partial entry dict, or None to skip the file.
WALK_ROOTS = [
    ("assets/san-mateo-public-records", classify_smpd_file),
    ("assets/public-records", lambda relpath, _m: classify_public_records_file(relpath)),
    ("assets/sanmateo.courts.ca.gov/grand-jury", lambda relpath, _m: classify_grand_jury_file(relpath.name)),
]


def build_catalog(pra_meta: dict) -> dict:
    """relpath (posix str, repo-relative) -> entry dict."""
    catalog = {}
    for root_rel, classify in WALK_ROOTS:
        root = REPO_ROOT / root_rel
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            ext = path.suffix.lower().lstrip(".")
            if ext not in DOC_EXTENSIONS:
                continue
            relpath = path.relative_to(REPO_ROOT)
            entry = classify(relpath, pra_meta)
            if entry is None:
                continue
            relpath_str = relpath.as_posix()
            entry.update({
                "id": relpath_str,
                "path": relpath_str,
                "filename": path.name,
                "ext": ext,
                "size_bytes": path.stat().st_size,
                "url": github_url(relpath_str),
                "date": extract_date(path.name),
                "featured": False,
                "source_number": None,
                "cite_label": None,
            })
            catalog[relpath_str] = entry
    return catalog


SOURCE_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def parse_source_table(text: str):
    """Yield (number, document_name, link_cell_text) for each Source
    Documents table row. Mirrors scripts/lint_findings.py's section
    extraction so the two stay in agreement about what counts as a row."""
    lines = text.splitlines()
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("## Source Documents"):
            start = i + 1
            continue
        if start is not None and line.startswith("## "):
            end = i
            break
    if start is None:
        return
    for line in lines[start:end]:
        if not SOURCE_ROW_RE.match(line):
            continue
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if len(parts) < 3:
            continue
        yield int(parts[0]), parts[1], parts[2]


def merge_cited_rows(catalog: dict) -> list:
    """Parse the Source Documents table and either merge each linked
    document into its matching catalog entry (by local path) or, for
    external links, return it as a standalone entry."""
    external = []
    if not FINDINGS_MD.exists():
        return external
    text = FINDINGS_MD.read_text(encoding="utf-8")
    for num, doc_name, link_cell in parse_source_table(text):
        links = MD_LINK_RE.findall(link_cell)
        if not links:
            links = [(None, None)]
        for label, url in links:
            if url and url.startswith(GITHUB_BLOB_PREFIX):
                relpath = unquote(url[len(GITHUB_BLOB_PREFIX):])
                if relpath in catalog:
                    entry = catalog[relpath]
                    entry["blurb"] = doc_name
                    entry["featured"] = True
                    entry["source_number"] = num
                    entry["cite_label"] = label
                    continue
                # Cited but outside our walk roots — index it anyway so the
                # link isn't silently missing from search.
                catalog[relpath] = {
                    "id": relpath,
                    "path": relpath,
                    "filename": Path(relpath).name,
                    "ext": Path(relpath).suffix.lower().lstrip("."),
                    "size_bytes": None,
                    "url": url,
                    "date": extract_date(relpath),
                    "source_type": "pra_attachment",
                    "agency_key": None,
                    "agency_label": None,
                    "blurb": doc_name,
                    "tags": [],
                    "context_title": None,
                    "context_url": None,
                    "featured": True,
                    "source_number": num,
                    "cite_label": label,
                }
                continue
            external.append({
                "id": f"source-{num}-{re.sub(r'[^a-z0-9]+', '-', (label or str(num)).lower()).strip('-')}",
                "path": None,
                "filename": None,
                "ext": None,
                "size_bytes": None,
                "url": url,
                "date": None,
                "source_type": "cited",
                "agency_key": None,
                "agency_label": None,
                "blurb": doc_name,
                "tags": [],
                "context_title": None,
                "context_url": None,
                "featured": True,
                "source_number": num,
                "cite_label": label,
            })
    return external


def main():
    pra_meta = load_pra_metadata()
    catalog = build_catalog(pra_meta)
    external = merge_cited_rows(catalog)

    docs = list(catalog.values()) + external
    docs.sort(key=lambda d: (not d["featured"], d["filename"] or d["blurb"] or ""))

    agency_counts: dict[tuple, int] = {}
    for d in docs:
        if not d.get("agency_key"):
            continue
        key = (d["agency_key"], d["agency_label"])
        agency_counts[key] = agency_counts.get(key, 0) + 1
    agencies = [
        {"key": key, "label": label, "count": count}
        for (key, label), count in sorted(agency_counts.items(), key=lambda kv: -kv[1])
    ]

    out = {
        "generated_at": datetime.now(UTC).isoformat(),
        "agencies": agencies,
        "docs": docs,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {OUT_PATH} ({len(docs)} documents, {len(agencies)} agencies)")


if __name__ == "__main__":
    sys.exit(main())
