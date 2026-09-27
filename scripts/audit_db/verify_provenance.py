"""Check that the database's row locations point at the right place in the original released files.

  nice -n 19 taskpolicy -b uv run --with duckdb --with openpyxl --with python-calamine python verify_provenance.py [options]
  (the SMPD check also needs poppler's pdftotext on PATH)

MuckRock releases (evidence dir): for sampled rows, opens the ORIGINAL file (zip member / sheet) with a reader
independent of staging's (openpyxl for spreadsheets, Python's csv module for CSV; calamine, the staging library, only
where openpyxl cannot open the workbook, and counted separately as not independent), goes to src_row and compares every
cell EXACTLY (no trimming) with what truth stored, including the cells kept in `extra`. Also: the header row against
releases.header_raw, each container against releases.container_sha256 and the timestamped MANIFEST_v2.txt, each zip
member against releases.member_sha256, and CSV originals against releases.content_sha256.
Repo NDJSON (Redwood City, Los Altos): the agency workbooks are not in the repo, so sampled rows are compared value by
value with the committed NDJSON line (line = src_row - 1, or src_row for an export the conversion manifest marks
header-less). Each file is streamed once, which also re-checks container_sha256 (the .gz) and content_sha256 (the
decompressed bytes).
SMPD (truth.smpd_pdf_rows, loaded with pymupdf): every PDF is re-hashed and re-read with poppler's pdftotext -raw;
per page, the search ids printed there must be exactly the ids truth puts on that page (all rows, not a sample), and
for --smpd sampled rows the stored lines must appear on src_page (or the next page, for a block split by a page break).
Image-only PDFs (n_rows = 0) are listed; the check is only that pdftotext finds no id in them either.
Sampling: --per-release rows within each release's first 3,000; --per-layout rows inside every layout-corrected range
(truth.release_layouts); --deep rows beyond row 3,000 in --deep-releases randomly chosen large MuckRock releases (-1 =
all) plus every layout-corrected one; repo NDJSON gets deep rows in every release (streamed in full anyway). Each
original is read once, up to its deepest sampled row. --only PATTERN restricts to release_id LIKE PATTERN.
Exit 1 on any mismatch. Nothing is printed from row contents: only release ids, row numbers, column names and counts.
"""
import argparse
import collections
import csv
import gzip
import hashlib
import io
import json
import random
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from muckrock_ingest import canonical  # noqa: E402  (truth stores row keys under canonical() names)

ap = argparse.ArgumentParser()
ap.add_argument("--audit-dir", default=str(Path(__file__).parent))
ap.add_argument("--truth", help="truth database (default <audit-dir>/truth.duckdb)")
ap.add_argument("--per-release", type=int, default=2, help="rows sampled within each release's first 3,000")
ap.add_argument("--per-layout", type=int, default=3, help="rows sampled inside each layout-corrected range")
ap.add_argument("--deep", type=int, default=2, help="rows sampled beyond row 3,000 in each deep release")
ap.add_argument("--deep-releases", type=int, default=16, help="large MuckRock releases that get deep rows (-1 = all; slow)")
ap.add_argument("--smpd", type=int, default=40, help="SMPD rows whose stored lines are checked on their page")
ap.add_argument("--only", help="release_id LIKE pattern: check only these releases")
ap.add_argument("--seed", type=int, default=1)
args = ap.parse_args()
A = Path(args.audit_dir)
con = duckdb.connect(args.truth or str(A / "truth.duckdb"), read_only=True)
con.execute("SET threads=2; SET memory_limit='2GB'")   # lookups by literal release_id/row_no only
info = dict(con.execute("SELECT key, value FROM build_info").fetchall())
EV, WT = Path(info["evidence_dir"]), Path(info["repo_checkout"])
TABLES = {t for (t,) in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
RCOLS = {c for (c,) in con.execute("SELECT column_name FROM duckdb_columns() WHERE table_name = 'releases'").fetchall()}
FLOCK = ["ID", "Name", "Org Name", "Total Networks Searched", "Total Devices Searched", "Time Frame", "License Plate",
         "Reason", "Case #", "Filters", "Search Time", "Search Type", "Text Prompt", "Moderation"]
ROWKEYS = {"release_id", "row_no", "src_row", "extra"}
FIRST = 3000
rnd = random.Random(args.seed)
bad = []
readers = collections.defaultdict(lambda: collections.Counter())   # reader -> releases / rows / cells / problems
hashes = collections.Counter()


def problem(reader, msg):
    bad.append(msg)
    readers[reader]["problems"] += 1


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def q(s):
    return "'" + s.replace("'", "''") + "'"


def cmp(reader, where, col, orig, stored):
    """Exact comparison; a difference only in surrounding whitespace is still a difference (truth is verbatim).
    One reader equivalence is allowed and counted: a date-only cell is a midnight datetime to openpyxl and a date to
    calamine (staging), so openpyxl's 'YYYY-MM-DD 00:00:00' matches a stored 'YYYY-MM-DD'."""
    readers[reader]["cells"] += 1
    o, v = orig if orig is not None else "", stored if stored is not None else ""
    if o != v and reader == "openpyxl" and re.fullmatch(r"\d{4}-\d{2}-\d{2} 00:00:00", o) and v == o[:10]:
        readers[reader]["date-only cells (openpyxl midnight = staged date)"] += 1
        return
    if o != v:
        problem(reader, f"{where}: column {col!r} differs from the original"
                        + (" (whitespace only)" if o.strip() == v.strip() else ""))


def stored_rows(table, rid, row_nos):
    if not row_nos:
        return {}
    cur = con.execute(f"SELECT * FROM {table} WHERE release_id = {q(rid)} AND row_no IN ({', '.join(map(str, sorted(row_nos)))})")
    cols = [d[0] for d in cur.description]
    return {r["row_no"]: r for r in (dict(zip(cols, t)) for t in cur.fetchall())}


manifest = {}
mf = EV / "MANIFEST_v2.txt"
if mf.exists():
    for line in mf.read_text().splitlines():   # "<sha256>  <local_path>  <source_url>"
        parts = line.split("  ")
        if len(parts) >= 2 and len(parts[0]) == 64:
            manifest[parts[1].strip()] = parts[0]

# ---- which rows to check ------------------------------------------------------------------------------------------
sel = ", ".join(c if c in RCOLS else f"NULL AS {c}" for c in
                ["release_id", "container_root", "container_path", "member", "sheet", "container_sha256", "content_sha256",
                 "member_sha256", "header", "header_raw", "audit", "n_rows"])
rels = [dict(zip(["rid", "root", "path", "member", "sheet", "csha", "content", "msha", "header", "header_raw", "audit", "n"], r))
        for r in con.execute(f"SELECT {sel} FROM releases" + (f" WHERE release_id LIKE {q(args.only)}" if args.only else "")
                             + " ORDER BY release_id").fetchall()]
layouts = collections.defaultdict(list)
for rid, a, b in con.execute("SELECT r.release_id, l.src_row_from, l.src_row_to FROM releases r JOIN release_layouts l "
                             "ON r.release_id LIKE l.release_pattern").fetchall():
    layouts[rid].append((a, b))
is_ndjson = lambda r: r["root"] == "repo" and r["path"].endswith(".ndjson.gz")
big = [r["rid"] for r in rels if r["root"] == "evidence" and r["n"] > FIRST]
deep = set(big if args.deep_releases < 0 else rnd.sample(big, min(args.deep_releases, len(big)))) | set(layouts)


def pick(r):
    table = "flock_event_rows" if r["audit"] == "event" else "flock_audit_rows"
    n = r["n"] or 0
    want = set(rnd.sample(range(1, min(n, FIRST) + 1), min(args.per_release, min(n, FIRST))))
    if n > FIRST and (r["rid"] in deep or is_ndjson(r)):
        want |= set(rnd.sample(range(FIRST + 1, n + 1), min(args.deep, n - FIRST)))
    for a, b in layouts.get(r["rid"], []):   # row_no range of the layout's src_row range (src_row rises with row_no)
        lo, hi = con.execute(f"SELECT min(row_no), max(row_no) FROM {table} WHERE release_id = {q(r['rid'])} "
                             f"AND src_row >= {int(a)}" + (f" AND src_row <= {int(b)}" if b is not None else "")).fetchone()
        if lo is not None:
            want |= set(rnd.sample(range(lo, hi + 1), min(args.per_layout, hi - lo + 1)))
    return table, want


# ---- MuckRock originals ---------------------------------------------------------------------------------------------
def norm(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def is_header(r):
    return sum(1 for v in r if re.search(r"[A-Za-z]", v)) >= 3   # staging's rule (muckrock_ingest._is_header)


WB = {}   # the last opened workbook: consecutive releases are often sheets of one file


def workbook(key, data, calamine=False):
    if (key, calamine) not in WB:
        WB.clear()
        if calamine:
            from python_calamine import CalamineWorkbook
            WB[(key, calamine)] = CalamineWorkbook.from_filelike(io.BytesIO(data))
        else:
            import openpyxl
            WB[(key, calamine)] = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    return WB[(key, calamine)]


def original_rows(key, data, sheet, wanted):
    """Header + the wanted src_rows of the original, read once up to the deepest one. Spreadsheets via openpyxl
    (independent of staging); workbooks openpyxl cannot open fall back to calamine (the staging library)."""
    last, out, hdr = max(wanted), {}, None
    if data[:4] == b"PK\x03\x04":
        try:
            wb = workbook(key, data)
            ws = wb[sheet] if sheet else wb.worksheets[0]   # releases.sheet is the full sheet name
            it, reader = enumerate(ws.iter_rows(min_row=1, max_row=last, values_only=True), start=1), "openpyxl"
        except Exception:  # noqa: BLE001
            wbc = workbook(key, data, calamine=True)
            sh = wbc.get_sheet_by_name(sheet or wbc.sheet_names[0])
            r0 = (sh.start or (0, 0))[0]
            it, reader = ((r0 + i + 1, r) for i, r in enumerate(sh.to_python(nrows=last))), "calamine (staging's library)"
        off = None
        for i, r in it:
            r = [norm(v) for v in r]
            if hdr is None and is_header(r):
                hdr, off = r, next(j for j, v in enumerate(r) if v)
            if i in wanted:
                out[i] = r
            if i >= last:
                break
        if reader == "openpyxl" and off:   # staging's reader starts at the first used column
            hdr, out = hdr[off:], {k: v[off:] for k, v in out.items()}
        return hdr, out, reader
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    for i, r in enumerate(csv.reader(io.StringIO(text, newline="")), start=1):
        if hdr is None and is_header(r):
            hdr = r
        if i in wanted:
            out[i] = r
        if i >= last:
            break
    return hdr, out, "csv module"


def trail(xs):
    xs = list(xs or [])
    while xs and xs[-1] == "":
        xs.pop()
    return xs


containers = {}
zip_cache = {}
evid = [r for r in rels if r["root"] == "evidence"]
for k, r in enumerate(evid, 1):
    rid, f = r["rid"], EV / r["path"]
    if f not in containers:
        containers[f] = sha(f)
        hashes["evidence containers"] += 1
        if containers[f] != r["csha"]:
            problem("hash", f"{rid}: container sha256 differs from releases.container_sha256")
        m = manifest.get(r["path"])
        if m is None:
            problem("hash", f"{r['path']}: not listed in MANIFEST_v2.txt")
        elif m != containers[f]:
            problem("hash", f"{r['path']}: sha256 differs from MANIFEST_v2.txt")
    if r["member"]:
        if f not in zip_cache:
            zip_cache.clear()
            zip_cache[f] = zipfile.ZipFile(f)
        data = zip_cache[f].read(r["member"])
        if r["msha"] is not None:
            hashes["zip members"] += 1
            if hashlib.sha256(data).hexdigest() != r["msha"]:
                problem("hash", f"{rid}: zip member sha256 differs from releases.member_sha256")
    else:
        data = f.read_bytes()
    is_csv = data[:4] != b"PK\x03\x04" and data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    if is_csv and r["content"] is not None:   # staging: content_sha256 of a CSV = sha256 of its bytes
        hashes["CSV contents"] += 1
        if hashlib.sha256(data).hexdigest() != r["content"]:
            problem("hash", f"{rid}: CSV bytes differ from releases.content_sha256")
    table, want = pick(r)
    recs = stored_rows(table, rid, want)
    if not recs:
        continue
    for n in want - set(recs):
        problem("truth", f"{rid} row_no {n}: sampled row_no missing from truth.{table} (n_rows = {r['n']})")
    hdr, orig, reader = original_rows((f, r["member"]), data, r["sheet"], {rec["src_row"] for rec in recs.values()})
    readers[reader]["releases"] += 1
    if trail(hdr) != trail(r["header_raw"]):
        problem(reader, f"{rid}: header row differs from releases.header_raw")
    for rec in recs.values():
        readers[reader]["rows"] += 1
        where = f"{rid} row_no {rec['row_no']} src_row {rec['src_row']}"
        o_row = orig.get(rec["src_row"])
        if o_row is None:
            problem(reader, f"{where}: src_row not found in the original")
            continue
        extra = json.loads(rec["extra"]) if rec.get("extra") else {}
        for i, canon in enumerate(r["header"]):   # header position i = original column i
            if canon in ROWKEYS:
                continue
            stored = rec[canon] if canon in rec else extra.get(canon)
            cmp(reader, where, canon, o_row[i] if i < len(o_row) else "", stored)
        if any(v.strip() for v in o_row[len(r["header"]):]):
            problem(reader, f"{where}: the original has non-empty cells beyond the header width that truth does not hold")
    if k % 100 == 0:
        print(f"  {k}/{len(evid)} MuckRock releases", flush=True)

# ---- repo NDJSON (Redwood City, Los Altos): the committed conversion, streamed once per file -------------------------
headerless = {}
for mfp in [WT / "assets/redwood-city-pras/json/_manifest.json", *WT.glob("assets/los-altos-pras/json/pra-*/_manifest.json")]:
    if mfp.exists():
        for e in json.load(open(mfp))["files"]:
            headerless[str((mfp.parent / e["output"]).relative_to(WT))] = bool(e.get("headerless"))
MISSING = object()


def jstr(v):   # what json_extract_string returns for a JSON value
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    return str(v)


for r in [r for r in rels if is_ndjson(r)]:
    rid, f, reader = r["rid"], WT / r["path"], "NDJSON line (committed conversion)"
    hashes["repo NDJSON files"] += 1
    if sha(f) != r["csha"]:
        problem("hash", f"{rid}: .ndjson.gz sha256 differs from releases.container_sha256")
    if r["path"] not in headerless:
        problem(reader, f"{rid}: no conversion-manifest entry for {r['path']} (header-less or not: unknown)")
        continue
    off = 0 if headerless[r["path"]] else 1   # the converter reads row 1 as the header unless the export was header-less
    table, want = pick(r)
    recs = stored_rows(table, rid, want)
    by_line = {rec["src_row"] - off: rec for rec in recs.values()}
    h, got = hashlib.sha256(), {}
    with gzip.open(f, "rb") as fh:
        for i, line in enumerate(fh, 1):
            h.update(line)
            if i in by_line:
                got[i] = json.loads(line)
    if r["content"] is not None:
        hashes["repo NDJSON contents"] += 1
        if h.hexdigest() != r["content"]:
            problem("hash", f"{rid}: decompressed NDJSON sha256 differs from releases.content_sha256")
    readers[reader]["releases"] += 1
    for line_no, rec in by_line.items():
        readers[reader]["rows"] += 1
        where = f"{rid} row_no {rec['row_no']} src_row {rec['src_row']} (NDJSON line {line_no})"
        obj = got.get(line_no)
        if obj is None:
            problem(reader, f"{where}: line not in the file")
            continue
        extra = json.loads(rec["extra"]) if rec.get("extra") else {}
        for key, v in obj.items():
            ck = canonical([key])[0]   # aliases / trimmed labels, as build_truth maps them (keys are unique within a line)
            stored = rec[ck] if ck in FLOCK else extra.get(ck, extra.get(key, MISSING))
            if stored is MISSING:
                problem(reader, f"{where}: NDJSON key {key!r} is not stored in truth")
                continue
            cmp(reader, where, key, jstr(v), stored)
        mapped = {canonical([k])[0] for k in obj}
        for c in FLOCK:
            if c not in mapped and rec.get(c) is not None:
                problem(reader, f"{where}: truth holds {c!r} but the NDJSON line has no such key")
for r in rels:
    if r["root"] == "repo" and not is_ndjson(r) and not r["rid"].startswith("smpd:"):
        print(f"  not checked (no reader for this repo input): {r['rid']}")

# ---- SMPD PDFs: every page's printed search ids vs truth, and sampled rows' lines on their page ---------------------
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
ws = lambda s: "".join((s or "").split())   # all whitespace dropped: poppler -raw omits spaces pymupdf keeps ('10/28/2025,10:48:17AMUTC')
smpd = [r for r in rels if r["rid"].startswith("smpd:")]
page_text, image_only = {}, []
if smpd and "smpd_pdf_rows" not in TABLES:
    print("  SMPD: this truth has no smpd_pdf_rows (built before the PDF loader); SMPD not checked")
    smpd = []
reader = "pdftotext -raw (poppler)"
for r in smpd:
    rid, f = r["rid"], WT / r["path"]
    hashes["SMPD PDFs"] += 1
    s = sha(f)
    if s != r["csha"]:
        problem("hash", f"{rid}: PDF sha256 differs from releases.container_sha256")
    if r["content"] is not None and s != r["content"]:
        problem("hash", f"{rid}: PDF sha256 differs from releases.content_sha256")
    p = subprocess.run(["pdftotext", "-raw", str(f), "-"], capture_output=True, text=True)
    if p.returncode != 0:
        problem(reader, f"{rid}: pdftotext failed (exit {p.returncode})")
        continue
    pages = p.stdout.split("\f")
    for i, t in enumerate(pages, 1):
        page_text[(rid, i)] = t
    printed = {i: collections.Counter(UUID.findall(t)) for i, t in enumerate(pages, 1)}
    loaded = collections.defaultdict(collections.Counter)
    for pg, sid in con.execute(f"SELECT src_page, id FROM smpd_pdf_rows WHERE release_id = {q(rid)}").fetchall():
        loaded[pg][sid] += 1
    readers[reader]["releases"] += 1
    n_loaded = sum(sum(c.values()) for c in loaded.values())
    readers[reader]["rows"] += n_loaded
    if not r["n"]:
        image_only.append(rid)
        if sum(sum(c.values()) for c in printed.values()):
            problem(reader, f"{rid}: n_rows = 0 but pdftotext finds search ids in its text layer")
        continue
    if n_loaded != r["n"]:
        problem("truth", f"{rid}: releases.n_rows = {r['n']} but smpd_pdf_rows holds {n_loaded}")
    for pg in sorted(set(printed) | set(loaded)):
        a, b = printed.get(pg, collections.Counter()), loaded.get(pg, collections.Counter())
        readers[reader]["ids checked on their page"] += sum(b.values())
        if a != b:
            problem(reader, f"{rid} page {pg}: {sum((b - a).values())} stored ids not printed on that page, "
                            f"{sum((a - b).values())} printed ids not stored for it")
if smpd and args.smpd:
    only = f" WHERE release_id LIKE {q(args.only)}" if args.only else ""
    for rid, n, pg, sid, *lines in con.execute(
            f"""SELECT release_id, row_no, src_page, id, user_line, count_time_line, reason_line
                FROM (SELECT * FROM smpd_pdf_rows{only}) USING SAMPLE reservoir({int(args.smpd)} ROWS) REPEATABLE ({args.seed})""").fetchall():
        if (rid, pg) not in page_text:
            continue
        readers[reader]["sampled rows"] += 1
        here = ws(page_text[(rid, pg)] + "\n" + page_text.get((rid, pg + 1), ""))   # a block can run onto the next page
        for name, line in zip(("user_line", "count_time_line", "reason_line"), lines):
            if line is not None:
                readers[reader]["lines checked"] += 1
                if ws(line) not in here:
                    problem(reader, f"{rid} row_no {n} page {pg}: {name} not found on that page or the next")

# ---- report --------------------------------------------------------------------------------------------------------
print(f"releases: {len(evid)} MuckRock, {sum(1 for r in rels if is_ndjson(r))} repo NDJSON, {len(smpd)} SMPD PDFs "
      f"({len(image_only)} image-only: {', '.join(image_only) or 'none'}); deep rows in {len(deep & set(big))} of {len(big)} "
      f"large MuckRock releases")
print("re-hashed: " + (", ".join(f"{v} {k}" for k, v in hashes.items()) or "nothing"))
for name, c in sorted(readers.items()):
    print(f"  {name:32s} " + ", ".join(f"{v:,} {k}" for k, v in c.items()))
if not any(c["rows"] for c in readers.values()) and not bad:
    sys.exit("nothing checked (no rows sampled: check --only, or the truth predates the loader for these releases)")
if bad:
    print(f"{len(bad)} problems:")
    for b in bad[:200]:
        print("  " + b)
    sys.exit(1)
print("all checked rows match their originals")
