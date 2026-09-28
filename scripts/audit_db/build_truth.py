"""Layer 1 — TRUTH: verbatim load of released audit files into truth.duckdb.

Rules: one row per released row (no de-duplication), every value stored exactly as released (VARCHAR),
keyed by (release_id, row_no). No parsing, cleaning or inference. Authored facts that come from
documents (cover-letter withholding claims, rows released under the wrong labels, who produced a file) are loaded
from dispositions.json / layouts.json / producers.json and cite their source.
Open read-only after building; derived.duckdb ATTACHes it READ_ONLY.

  uv run --locked --project scripts/audit_db python scripts/audit_db/build_truth.py OUT.duckdb TMPDIR [--repo CHECKOUT]
--repo defaults to the git checkout containing the current directory, so run it from a fresh worktree.
Local evidence (.claude/ exists only in the primary checkout) is found via git's common dir.
The repo commit the inputs came from is recorded in truth.build_info.

Sources: Redwood City + Los Altos (committed NDJSON conversions of the agency workbooks), San Mateo PD (the committed
PDFs it produced; smpd_pdf_loader.py), the MuckRock CA corpus (muckrock_ingest.py; staged first).
"""
import argparse
import glob
import gzip
import hashlib
import json
import multiprocessing
import os
import platform
import re
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
import smpd_pdf_loader as smpd  # noqa: E402
from muckrock_ingest import add_muckrock, canonical, sql_ident, sql_str  # noqa: E402

HERE = Path(__file__).parent
FLOCK_COLS = ["ID", "Name", "Org Name", "Total Networks Searched", "Total Devices Searched", "Time Frame", "License Plate",
              "Reason", "Case #", "Filters", "Search Time", "Search Type", "Text Prompt", "Moderation"]
# repo inputs, and this build code, whose uncommitted edits would make the recorded commit a lie
REPO_INPUTS = ["assets/redwood-city-pras", "assets/los-altos-pras", "assets/san-mateo-public-records/W012541-*",
               "assets/san-mateo-public-records/W012818-*", "assets/agency_registry.json", "scripts/audit_db"]
# One row per released file (or sheet). Provenance: where the original is (container_root + container_path, member,
# sheet, source_file), how to get it (source_url = download link, request_url = the request's page), how to verify it
# (container_sha256; member_sha256 for a zip member; content_sha256 groups identical content across re-releases), and
# how src_row relates to what a reader sees in the original (src_row_basis).
RELEASES_DDL = """CREATE OR REPLACE TABLE releases (release_id VARCHAR PRIMARY KEY, producer VARCHAR, producer_agency_id VARCHAR,
  producer_basis VARCHAR, producer_source VARCHAR, audit VARCHAR, pra_id VARCHAR, container_root VARCHAR,
  container_path VARCHAR, member VARCHAR, member_sha256 VARCHAR, sheet VARCHAR, source_file VARCHAR,
  container_sha256 VARCHAR, header VARCHAR[], header_raw VARCHAR[], header_basis VARCHAR, n_rows BIGINT,
  content_sha256 VARCHAR, released_on DATE, released_on_basis VARCHAR, source_url VARCHAR, request_url VARCHAR,
  src_row_basis VARCHAR)"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def ndjson_sha256(p):
    """sha256 of the decompressed NDJSON: equal for identical conversions, whatever the gzip settings (re-releases group)."""
    h = hashlib.sha256()
    with gzip.open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def insert_release(con, **f):
    con.execute(f"INSERT INTO releases ({', '.join(f)}) VALUES ({', '.join('?' * len(f))})", list(f.values()))


def la_readme_facts(readme):
    """PRA id -> {closed, url} from the committed Los Altos README's productions table ('| | PRA 25-312 | PRA 26-366 |',
    then '| Closed | <date> | ... |' and '| Portal | [..](url) | ... |'). Missing facts stay None: never invented."""
    ids, facts = None, {}
    for line in open(readme, encoding="utf-8"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if ids is None and any(re.fullmatch(r"PRA \d{2}-\d{3}", c) for c in cells):
            ids = [c.removeprefix("PRA ") if re.fullmatch(r"PRA \d{2}-\d{3}", c) else None for c in cells]
        elif ids and len(cells) == len(ids) and cells[0] in ("Closed", "Portal"):
            for pid, c in zip(ids, cells):
                if pid:
                    facts.setdefault(pid, {})[cells[0]] = c
    out = {}
    for pid, d in facts.items():
        closed = d.get("Closed") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.get("Closed") or "") else None
        m = re.search(r"\((https://[^)\s]+/requests/" + re.escape(pid) + r")\)", d.get("Portal") or "")
        out[pid] = {"closed": closed, "url": m.group(1) if m else None}
    return out


def ndjson_keys(con, path):
    """NDJSON keys in the workbook's column order, deterministically. The converter writes each line's keys in column order
    but omits empty cells, so every line is a subsequence of that order: merge the distinct key sequences (topological
    order, ties broken by first appearance in the file). Falls back to plain first appearance if they ever conflict."""
    seqs = [ks for ks, _ in con.execute(f"""SELECT json_keys(j) AS ks, min(rn) AS first FROM (SELECT row_number() OVER () AS rn, j
        FROM read_ndjson_objects({sql_str(str(path))}) t(j)) GROUP BY ks ORDER BY first""").fetchall()]
    first = list(dict.fromkeys(k for ks in seqs for k in ks))
    after = {k: set() for k in first}
    for ks in seqs:
        for a, b in zip(ks, ks[1:]):
            after[a].add(b)
    indeg = {k: 0 for k in first}
    for a in after:
        for b in after[a]:
            indeg[b] += 1
    out, ready = [], [k for k in first if indeg[k] == 0]
    while ready:
        k = min(ready, key=first.index)
        ready.remove(k)
        out.append(k)
        for b in after[k]:
            indeg[b] -= 1
            if indeg[b] == 0:
                ready.append(b)
    return out if len(out) == len(first) else first


def la_keys(m):
    """The keys scripts/xlsx_to_audit_ndjson.py gave a Los Altos sheet's columns, rebuilt from its manifest entry: the
    released header minus the phantom labels (a label with no data column; the converter drops it, binding the earlier of
    two equal labels), blank labels and unlabeled data columns past the header as column_<N> (N = 1-based data column)."""
    labels = list(m["header"])
    for p in m.get("phantom_headers") or []:
        del labels[len(labels) - 1 - labels[::-1].index(p)]
    keys = [lab or f"column_{d + 1}" for d, lab in enumerate(labels)]
    return keys + [f"column_{d + 1}" for d in sorted(m.get("unlabeled_columns") or []) if d >= len(labels)]


def load_repo_ndjson(con, WT, resolve):
    """Redwood City + Los Altos NDJSON (committed conversions of the agency workbooks) -> releases + flock_audit_rows."""
    rel, man = [], {}
    for mf in [WT / "assets/redwood-city-pras/json/_manifest.json",
               *map(Path, glob.glob(str(WT / "assets/los-altos-pras/json/pra-*/_manifest.json")))]:
        for e in json.load(open(mf))["files"]:
            man[(mf.parent, e["output"])] = e
    for f in sorted(glob.glob(str(WT / "assets/redwood-city-pras/json/*.ndjson.gz"))):
        stem = Path(f).name.removesuffix(".ndjson.gz")
        rel.append((f"rwc:{stem}", "Redwood City CA PD", "network", Path(f), "26-741" if "26_741" in stem else "26-217",
                    man.get((Path(f).parent, Path(f).name), {})))
    for f in sorted(glob.glob(str(WT / "assets/los-altos-pras/json/pra-*/*.ndjson.gz"))):
        pra = Path(f).parent.name.removeprefix("pra-")
        rel.append((f"la:{pra}:{Path(f).name.removesuffix('.ndjson.gz')}", "Los Altos CA PD",
                    "network" if re.search("network", Path(f).name, re.I) else "own", Path(f), pra,
                    man.get((Path(f).parent, Path(f).name), {})))
    la_facts = la_readme_facts(WT / "assets/los-altos-pras/json/README.md")
    jx = lambda k: f"json_extract_string(j, {sql_str('$.' + json.dumps(k))})"
    for rid, prod, audit, path, pra, m in rel:
        data_keys = ndjson_keys(con, path)
        headerless = bool(m.get("headerless"))
        if rid.startswith("la:") and m.get("header"):
            keys = la_keys(m)
            miss = [k for k in data_keys if k not in keys]
            if miss:   # the manifest does not explain the data: keep every key, and say so
                print(f"WARNING: {rid}: NDJSON keys {miss} not in the manifest header; appended to header", flush=True)
            keys += miss
            fix = [f"phantom labels dropped: {m['phantom_headers']}"] if m.get("phantom_headers") else []
            fix += [f"unlabeled data columns (0-based): {m['unlabeled_columns']}"] if m.get("unlabeled_columns") else []
            header_raw = m["header"]
            header_basis = ("header_raw = the released header row, from the converter manifest (blank labels as ''); header = "
                            "the keys scripts/xlsx_to_audit_ndjson.py gave the data columns (phantom labels dropped, blank or "
                            "missing labels as column_<N>, N = 1-based data column), canonicalized like MuckRock headers"
                            + (" (" + "; ".join(fix) + ")" if fix else "") + (f"; not in manifest: {miss}" if miss else ""))
        elif headerless:
            keys, header_raw = data_keys, None
            header_basis = ("no header row: the export is header-less; the converter labeled the columns with the standard "
                            "Flock network-audit order (SCHEMA_A in scripts/xlsx_to_audit_ndjson.py). header = those keys in "
                            "column order (a column empty in every row has no key, so is absent)")
        else:
            keys, header_raw = data_keys, data_keys
            header_basis = ("the converter manifest records no header row: header_raw = the NDJSON keys in the workbook's column "
                            "order (each line lists its keys in column order; merged across lines). The converter omits empty "
                            "cells, so a column the workbook had but left blank in every row is absent (it reads as not "
                            "exported, not empty); header = canonicalized like MuckRock")
        header = canonical(keys)
        canon = dict(zip(keys, header))
        sel = ", ".join(next((f"{jx(k)} AS {sql_ident(c)}" for k in data_keys if canon[k] == c), f"NULL AS {sql_ident(c)}")
                        for c in FLOCK_COLS)
        other = [k for k in data_keys if canon[k] not in FLOCK_COLS]   # e.g. column_<N> for unlabeled columns: kept
        ext = "NULL" if not other else (f"CASE WHEN coalesce({', '.join(jx(k) for k in other)}) IS NULL THEN NULL ELSE "
                                        f"json_object({', '.join(f'{sql_str(canon[k])}, {jx(k)}' for k in other)}) END")
        off = 0 if headerless else 1   # the converter reads row 1 as the header unless the export was header-less
        con.execute(f"INSERT INTO flock_audit_rows SELECT ?, row_number() OVER (), row_number() OVER () + {off}, {sel}, {ext} "
                    f"FROM read_ndjson_objects({sql_str(str(path))}) t(j)", [rid])
        n = con.execute("SELECT count(*) FROM flock_audit_rows WHERE release_id = ?", [rid]).fetchone()[0]
        lf = la_facts.get(pra, {}) if rid.startswith("la:") else {}
        insert_release(
            con, release_id=rid, producer=prod, producer_agency_id=resolve(prod), producer_basis="fixed (dedicated loader)",
            audit=audit, pra_id=pra, container_root="repo", container_path=str(path.relative_to(WT)), sheet=m.get("sheet"),
            source_file=m.get("source"), container_sha256=sha256(path), content_sha256=ndjson_sha256(path),
            header=header, header_raw=header_raw, header_basis=header_basis, n_rows=n,
            released_on=lf.get("closed"), request_url=lf.get("url"),
            released_on_basis=(f"Los Altos README: PRA {pra} closed {lf['closed']}" if lf.get("closed") else
                               "not in the Los Altos README" if rid.startswith("la:") else
                               "not recorded in manifest (RWC release dates estimated elsewhere from workbook creation)"),
            src_row_basis=(("workbook row = NDJSON line (no header row: the export is header-less)" if headerless else
                            "workbook row = NDJSON line + 1 (header on row 1)")
                           + "; the committed conversion (scripts/xlsx_to_audit_ndjson.py) drops all-empty rows, so exact "
                             "unless the workbook has blank rows mid-sheet; it trims cell text and omits empty cells"))


def load_smpd(con, WT, resolve, pdfs=None, workers=2):
    """San Mateo PD's own search log from the PDFs it produced -> releases + smpd_pdf_rows (one release per PDF)."""
    con.execute("""CREATE OR REPLACE TABLE smpd_pdf_rows (release_id VARCHAR, row_no BIGINT, src_page INTEGER,
      src_line INTEGER, id VARCHAR, user_line VARCHAR, count_time_line VARCHAR, reason_line VARCHAR, parse_note VARCHAR)""")
    pdfs = smpd.audit_pdfs(WT) if pdfs is None else pdfs
    # pure-Python text extraction (~3 min for the 34 PDFs on one core); spawn: no fork of a process holding DuckDB
    with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as ex:
        parsed = list(zip(pdfs, ex.map(smpd.parse_pdf, pdfs)))
    for pdf, res in parsed:
        rid = f"smpd:{pdf.parent.name}:{pdf.name}"
        rows = res["rows"]
        if rows:   # one statement per PDF: columns as typed lists, unnested in lockstep
            cols = [("row_no", "BIGINT"), ("src_page", "INTEGER"), ("src_line", "INTEGER"), ("id", "VARCHAR"),
                    ("user_line", "VARCHAR"), ("count_time_line", "VARCHAR"), ("reason_line", "VARCHAR"), ("parse_note", "VARCHAR")]
            con.execute("INSERT INTO smpd_pdf_rows SELECT ?, " + ", ".join(f"unnest(?::{t}[])" for _, t in cols),
                        [rid] + [[r[c] for r in rows] for c, _ in cols])
        else:
            print(f"WARNING: {pdf.name}: 0 rows (no search id in the text layer; image-only?)", flush=True)
        st = res["stats"]
        print(f"  smpd {pdf.name}: {len(rows):,} rows, {res['n_pages']} pages"
              + (f", {st['read_by_position']} read by position" if st["read_by_position"] else "")
              + (f", {st['with_note'] - st['read_by_position']} other parse notes" if st["with_note"] > st["read_by_position"] else ""),
              flush=True)
        sha = sha256(pdf)
        insert_release(
            con, release_id=rid, producer="San Mateo CA PD", producer_agency_id=resolve("San Mateo CA PD"),
            producer_basis="fixed (SMPD PRA)", audit="own", pra_id=pdf.parent.name, container_root="repo",
            container_path=str(pdf.relative_to(WT)), source_file=pdf.name, container_sha256=sha, content_sha256=sha,
            header=smpd.HEADER if rows else None, header_raw=res["header_raw"] if rows else None,
            header_basis=(smpd.IMAGE_ONLY_BASIS if not rows else
                          "header_raw = the header row as printed on page 1 (one entry per text-layer line; two labels can "
                          "share a line); header = the five columns in Flock superset names (userID -> Name, networkCount -> Total "
                          "Networks Searched)" if res["header_raw"] else
                          "no header row printed (a continuation part); header = the five columns of Flock's search-audit "
                          "export, as in the part it continues"),
            n_rows=len(rows),
            released_on_basis=("rolling production; the date each PDF was released is not recorded here (see the "
                               "request's message-history PDF in the same folder)"),
            src_row_basis=smpd.SRC_ROW_BASIS)
    n, n_pdf, n_note = con.execute("""SELECT count(*), count(DISTINCT release_id), count(parse_note) FROM smpd_pdf_rows""").fetchone()
    print(f"smpd_pdf_rows: {n:,} rows from {n_pdf} PDFs ({len(pdfs)} audit PDFs), {n_note} with a parse_note", flush=True)


def load_authored(con):
    """Authored facts from documents (not computed), each with its citation: dispositions and layout corrections."""
    con.execute("CREATE OR REPLACE TABLE release_dispositions (release_pattern VARCHAR, field VARCHAR, disposition VARCHAR, source VARCHAR)")
    con.executemany("INSERT INTO release_dispositions VALUES (?,?,?,?)",
                    [(d["release_pattern"], d["field"], d["disposition"], d["source"]) for d in json.load(open(HERE / "dispositions.json"))])
    # Rows whose cells sit under the wrong header label as released (verified against other logs, cited). Truth rows stay
    # verbatim; derived reads each field from the label named here for the affected rows. Row ranges are spreadsheet rows
    # as a reader sees them (src_row), not load order; an entry may list single rows ("src_rows") instead of a range.
    con.execute("""CREATE OR REPLACE TABLE release_layouts (release_pattern VARCHAR, src_row_from BIGINT, src_row_to BIGINT,
      mapping MAP(VARCHAR, VARCHAR), source VARCHAR)""")
    for d in json.load(open(HERE / "layouts.json")):
        spans = [(r, r) for r in d["src_rows"]] if "src_rows" in d else [(d["src_row_from"], d["src_row_to"])]
        con.executemany("INSERT INTO release_layouts VALUES (?, ?, ?, MAP(?, ?), ?)",
                        [[d["release_pattern"], a, b, list(d["mapping"]), list(d["mapping"].values()), d["source"]] for a, b in spans])
        rels = [r for (r,) in con.execute("SELECT release_id FROM releases WHERE release_id LIKE ?", [d["release_pattern"]]).fetchall()]
        if not rels:
            print(f"WARNING: layouts.json pattern matches no release: {d['release_pattern']}")
        elif "src_rows" in d:   # every listed row must exist in every matched release
            for rid in rels:
                got = con.execute(f"SELECT count(DISTINCT src_row) FROM flock_audit_rows WHERE release_id = ? AND src_row IN "
                                  f"({', '.join(str(int(r)) for r in d['src_rows'])})", [rid]).fetchone()[0]
                if got != len(set(d["src_rows"])):
                    print(f"WARNING: layouts.json: {rid} has {got} of the {len(set(d['src_rows']))} listed src_rows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("tmp")
    ap.add_argument("--repo", default=".", help="repo checkout (any worktree) to read committed inputs from; default: current")
    args = ap.parse_args()
    try:
        import pymupdf
    except ImportError:
        raise SystemExit("pymupdf is needed for the SMPD PDFs: run in the pinned env, "
                         "uv run --locked --project scripts/audit_db python scripts/audit_db/build_truth.py ...") from None
    WT = Path(git(args.repo, "rev-parse", "--show-toplevel"))
    # repo_commit identifies the build code only if the code runs from the checkout it reads inputs from
    if Path(git(HERE, "rev-parse", "--show-toplevel")).resolve() != WT.resolve():
        raise SystemExit(f"build code ({HERE.resolve()}) and --repo ({WT}) are different checkouts, so the recorded commit "
                         "would not identify the code: run the build_truth.py of the checkout you build from")
    PRIMARY = Path(git(WT, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
    EV = PRIMARY / ".claude/local_evidence/muckrock-ca-audit-logs"
    TMP = Path(args.tmp)
    repo_commit = git(WT, "rev-parse", "HEAD")
    repo_dirty = bool(git(WT, "status", "--porcelain", "--", *REPO_INPUTS))
    print(f"repo inputs: {WT} @ {repo_commit[:9]}{' (DIRTY inputs)' if repo_dirty else ''}; evidence: {EV}", flush=True)
    # registry: Flock org name / alias -> agency_id (same fields lib.resolve_agency(name=) searches)
    name_to_id = {}
    for e in json.load(open(WT / "assets/agency_registry.json")):
        for n in (e.get("flock_names") or []) + (e.get("aliases") or []):
            name_to_id.setdefault(n, e["agency_id"])

    con = duckdb.connect(str(args.out))
    # modest defaults so the laptop stays usable; raise with AUDIT_DB_THREADS / AUDIT_DB_MEMORY for a faster build
    con.execute(f"SET temp_directory='{TMP}/duck_tmp'; SET threads={os.environ.get('AUDIT_DB_THREADS', '4')}; "
                f"SET memory_limit='{os.environ.get('AUDIT_DB_MEMORY', '6GB')}'")   # insertion order kept: row_no = file order
    con.execute(RELEASES_DDL)
    con.execute(f"CREATE OR REPLACE TABLE flock_audit_rows (release_id VARCHAR, row_no BIGINT, src_row BIGINT, "
                f"{', '.join(f'{sql_ident(c)} VARCHAR' for c in FLOCK_COLS)}, extra JSON)")
    load_repo_ndjson(con, WT, name_to_id.get)
    # MuckRock CA corpus (all other tabular audit logs + event logs); see muckrock_ingest.py
    add_muckrock(con, EV, TMP, FLOCK_COLS, sha256, resolve=name_to_id.get)
    load_smpd(con, WT, name_to_id.get)
    load_authored(con)

    # Where the inputs came from and what read them (so a build from a stale checkout or another toolchain is visible)
    try:
        behind = int(git(WT, "rev-list", "--count", "HEAD..origin/main"))
    except subprocess.CalledProcessError:
        behind = None
    con.execute("CREATE OR REPLACE TABLE build_info (key VARCHAR, value VARCHAR)")
    con.executemany("INSERT INTO build_info VALUES (?, ?)", [
        ("built_at_utc", con.execute("SELECT strftime(now() AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%SZ')").fetchone()[0]),
        ("repo_checkout", str(WT)), ("repo_commit", repo_commit), ("repo_inputs_dirty", str(repo_dirty)),
        ("commits_behind_local_origin_main", str(behind)), ("evidence_dir", str(EV)),
        # web base for repo permalinks: <repo_web_url>/blob/<repo_commit>/<container_path>
        ("repo_web_url", re.sub(r"^git@github\.com:(.*?)(\.git)?$", r"https://github.com/\1",
                                git(WT, "remote", "get-url", "origin")).removesuffix(".git")),
        ("duckdb_version", duckdb.__version__), ("python_version", platform.python_version()),
        ("pymupdf_version", getattr(pymupdf, "__version__", None) or pymupdf.VersionBind)])   # SMPD rows are pymupdf's text layer: record the reader
    if behind:
        print(f"WARNING: checkout is {behind} commits behind its local origin/main ref (fetch + fresh worktree?)")
    unresolved = con.execute("SELECT DISTINCT producer FROM releases WHERE producer_agency_id IS NULL").fetchall()
    if unresolved:
        print(f"WARNING: producers not in the registry (flock_names/aliases): {[u for (u,) in unresolved]}")
    print(con.execute("SELECT producer, audit, count(*) AS n_releases, sum(n_rows) AS n_rows_total FROM releases GROUP BY ALL ORDER BY 1, 2").fetchall())
    con.close()


if __name__ == "__main__":
    main()
