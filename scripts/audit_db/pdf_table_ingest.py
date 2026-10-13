"""TRUTH loader for Flock audit spreadsheets agencies printed to PDF, from the MuckRock corpus (whose PDFs the MuckRock
loader skips) and the public-records-portal harvest. Read by position with pdf_table_reader.py.

Two steps:
  stage   read every PDF whose page 1 holds a Flock header row, once per file content (cached by sha256 under
          <tmp>/pdf_tables/), and run the layout gate (pdf_table_reader.layout_ok): on the first pages, then, for a PDF
          that passes, on all of its rows
  load    (from build_truth.py) one release per PDF that passed into releases + flock_audit_rows; a PDF that failed the
          gate is not loaded and is listed in release_exclusions with the reason

Rows: src_row = page * 1000 + row on that page (SRC_ROW_BASIS), cells as read; extra holds pdf_page, pdf_row and, when
any cell's text lies under a solid black filled rectangle drawn on the page, under_fill = those columns. Releases: release_id 'pdft:mr:<path in the MuckRock corpus>' or 'pdft:pp:<path in the portals harvest>';
container_root 'evidence' or 'portals'; producer as for the other loaders of that source (producers.json / the file
name / the request's agency for MuckRock; portal_producers.json / the file name for portals).

  uv run --locked --project scripts/audit_db python scripts/audit_db/pdf_table_ingest.py stage <evidence> <portals> <tmp>
"""
import gzip
import hashlib
import json
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import pdf_table_reader as ptr  # noqa: E402
from muckrock_ingest import FLOCK_COLS, org_from_name, sha256_file  # noqa: E402
from portal_ingest import excluded, load_exclusions, manifest_files, producer_for, request_meta, shown_name  # noqa: E402

HERE = Path(__file__).parent
SKIP_DIRS = {"_forensics", "_snapshots", "_camera_locations", "discovery", "_sweep", "_scripts", "_resweep"}
HEADER_BASIS = ("header_raw = page 1's header line as printed (one entry); header = the Flock columns its labels name, "
                "left to right (pdf_table_reader.LABELS)")


def candidates(EV, PV):
    """(root, path relative to that root, absolute path) for every PDF in the two collections."""
    out = []
    for p in sorted(EV.rglob("*.pdf")):
        rel = p.relative_to(EV)
        if p.is_file() and not (SKIP_DIRS & set(rel.parts)) and len(rel.parts) >= 2:
            out.append(("evidence", str(rel), str(p)))
    for m in manifest_files(PV):
        if m["path"].lower().endswith(".pdf") and (PV / m["path"]).is_file():
            out.append(("portals", m["path"], str(PV / m["path"])))
    return out


def _read(args):
    root, rel, path, cache_dir = args
    sha = sha256_file(path)
    cache = Path(cache_dir) / f"{sha}.json.gz"
    if not cache.exists():
        try:
            res = ptr.read_pdf(path)
        except BaseException as ex:  # noqa: BLE001  damaged PDFs raise inside pymupdf
            if isinstance(ex, (KeyboardInterrupt, SystemExit)):
                raise
            res = {"header": None, "header_raw": None, "n_pages": None, "rows": [], "ok": False,
                   "why": f"unreadable: {type(ex).__name__}: {str(ex)[:120]}"}
        tmp = cache.with_suffix(".tmp")
        with gzip.open(tmp, "wt") as fh:
            json.dump(res, fh)
        tmp.rename(cache)
    with gzip.open(cache, "rt") as fh:
        res = json.load(fh)
    return {"root": root, "rel": rel, "sha256": sha, "cache": str(cache), "header": res["header"],
            "header_raw": res["header_raw"], "n_pages": res["n_pages"], "n_rows": len(res["rows"]), "ok": res["ok"],
            "why": res["why"]}


def stage(EV, PV, TMP, workers=3):
    EV, PV, TMP = Path(EV), Path(PV), Path(TMP)
    cache_dir = TMP / "pdf_tables"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cands = candidates(EV, PV)
    with ProcessPoolExecutor(workers) as ex:
        staged = list(ex.map(_read, [(r, rel, p, str(cache_dir)) for r, rel, p in cands], chunksize=1))
    json.dump(staged, open(cache_dir / "staged.json", "w"))
    tables = [s for s in staged if s["header"]]
    print(f"pdf tables: {len(cands)} PDFs, {len(tables)} with a Flock header, {sum(s['ok'] for s in tables)} passed the "
          f"layout gate ({sum(s['n_rows'] for s in tables if s['ok']):,} rows)", flush=True)


def _mr_meta(EV):
    cat = {}
    for name in ("catalog.json", "catalog2.json", *sorted(p.name for p in EV.glob("catalog_resweep_*.json"))):
        for e in json.load(open(EV / name)):
            if e.get("local_path"):
                cat[e["local_path"]] = e
    reqs = {}
    for name in ("catalog_requests.json", *sorted(p.name for p in EV.glob("catalog_requests_resweep_*.json"))):
        for r in json.load(open(EV / name)):
            reqs.setdefault(str(r["request_id"]), r)
    urls = {}
    for name in ("MANIFEST.txt", "MANIFEST_v2.txt", *sorted(p.name for p in EV.glob("MANIFEST_resweep_*.txt"))):
        for line in (EV / name).read_text().splitlines():
            parts = line.split("  ")
            if len(parts) >= 3 and parts[2].startswith("http"):
                urls[parts[1]] = parts[2]
    return cat, reqs, urls


def add_pdf_tables(con, EV, PV, TMP, resolve=lambda org: None, log=lambda m: print(m, flush=True)):
    EV, PV, TMP = Path(EV), Path(PV), Path(TMP)
    manifest = TMP / "pdf_tables" / "staged.json"
    if not manifest.exists():
        raise SystemExit(f"run the staging step first: uv run --locked --project scripts/audit_db python "
                         f"scripts/audit_db/pdf_table_ingest.py stage {EV} {PV} {TMP}")
    staged = [s for s in json.load(open(manifest)) if s["header"]]
    rules = load_exclusions(con)
    mr_prod = {k: v for k, v in json.load(open(HERE / "producers.json")).items() if not k.startswith("_")}
    pp_prod = json.load(open(HERE / "portal_producers.json"))
    cat, reqs, urls = _mr_meta(EV)
    meta = request_meta(PV)
    pp_docs = {m["path"]: m for m in manifest_files(PV)}
    loaded = n_excl = 0
    for s in staged:
        rid = f"pdft:{'mr' if s['root'] == 'evidence' else 'pp'}:{s['rel']}"
        ex = excluded(rid, rules) or (None if s["ok"] else (s["why"], "pdf_table_reader.layout_ok"))
        if ex:
            con.execute("INSERT INTO release_exclusions VALUES (?,?,?)", [rid, *ex])
            n_excl += 1
            continue
        with gzip.open(s["cache"], "rt") as fh:
            rows = json.load(fh)["rows"]
        name = Path(s["rel"]).name
        if s["root"] == "evidence":
            req = s["rel"].split("/", 1)[0].split("-", 1)[0]
            ov = mr_prod.get(req, {})
            fov = (ov.get("files") or {}).get(name, {})
            org, basis = fov.get("org") or ov.get("org"), "producers.json"
            source = (fov.get("source") if fov.get("org") else ov.get("source")) if org else None
            if not org:
                org, basis = org_from_name(name), "filename"
            r = reqs.get(req, {})
            if not org:
                org, basis = r.get("agency"), "MuckRock agency name"
            pra_id, request_url = f"muckrock-{req}", r.get("url")
            source_url = urls.get(s["rel"])
            m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", source_url or "")
            released_on, rel_basis = ("-".join(m.groups()), "MuckRock attachment date") if m else (None, None)
        else:
            d = pp_docs[s["rel"]]
            u = {"host": d["host"], "request": d["request_id"], "local_path": s["rel"], "name": name}
            org, basis, source = producer_for(u, pp_prod, {})
            rm = meta.get((d["platform"], d["host"], str(d["request_id"])), {})
            pra_id, request_url = f"{d['host']}:{d['request_id']}", rm.get("url")
            doc_id = str(d["doc_id"])
            source_url = (f"https://{d['host']}/documents/{doc_id}" if d["platform"] == "nextrequest" and doc_id.isdigit()
                          else request_url)
            released_on = rm.get("uploaded", {}).get(doc_id)
            rel_basis = "portal upload date" if released_on else None
        orgs = {(r.get("Org Name") or "").strip() for r in rows} - {""}
        audit = "own" if (len(orgs) <= 1 and "network" not in name.lower()) else "network"
        df = pd.DataFrame({"release_id": rid, "row_no": range(1, len(rows) + 1),
                           "src_row": [r["_page"] * 1000 + r["_row"] for r in rows],
                           **{c: [r.get(c) for r in rows] for c in FLOCK_COLS},
                           "extra": [json.dumps({"pdf_page": r["_page"], "pdf_row": r["_row"],
                                                 **({"under_fill": r["_under_fill"]} if r["_under_fill"] else {})})
                                     for r in rows]})
        con.register("pdft_df", df)
        con.execute("INSERT INTO flock_audit_rows SELECT release_id, row_no, src_row, "
                    + ", ".join(f'"{c}"' for c in FLOCK_COLS) + ", extra::JSON FROM pdft_df")
        con.unregister("pdft_df")
        con.execute("""INSERT INTO releases (release_id, producer, producer_agency_id, producer_basis, producer_source, audit, pra_id,
                         container_root, container_path, member, member_sha256, sheet, source_file, container_sha256, header,
                         header_raw, header_basis, n_rows, content_sha256, released_on, released_on_basis, source_url,
                         request_url, src_row_basis)
                       VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?,?,?,?,TRY_CAST(? AS DATE),?,?,?,?)""",
                    [rid, org, resolve(org), basis, source, audit, pra_id, s["root"], s["rel"], shown_name(name),
                     s["sha256"], s["header"], s["header_raw"], HEADER_BASIS, len(rows), s["sha256"], released_on,
                     rel_basis, source_url, request_url, ptr.SRC_ROW_BASIS])
        loaded += 1
    log(f"  pdf tables: {loaded} releases loaded, {n_excl} excluded (release_exclusions)")


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "stage":
        stage(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        raise SystemExit(__doc__)
