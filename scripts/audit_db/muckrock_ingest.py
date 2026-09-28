"""TRUTH loader for the MuckRock CA audit-log corpus (tabular files only; PDFs deferred).

Provenance travels with every row: flock_audit_rows.src_row is the row as a reader sees it when opening the original
(spreadsheet row number; for CSV the record number with the header as row 1), and releases carries the request page
(request_url), the file's MuckRock download URL (source_url), the file / zip member / sheet names in full, the
container's sha256 (matches MANIFEST_v2.txt in the evidence dir, which is RFC 3161 + OpenTimestamps stamped) and, for a
zip member, the member's own sha256 (member_sha256), so a member extracted by a reader can be checked on its own.

Adds to truth.duckdb (called from build_truth.py):
  releases / flock_audit_rows   network + own-search audits (csv, xlsx sheets, xlsb, incl. zip members)
  flock_event_rows              Flock event logs (Timestamp, User, Event Type, Entity Type, Entity Details, Event Id)
  every release is stored in full (no collapsing); releases.content_sha256 lets identical content be grouped at read time
Values stay verbatim (VARCHAR), header included: staging writes the released header row unchanged (releases.header_raw);
the loader maps it to the Flock superset via canonical() (releases.header = the canonical names loaded; HEADER_BASIS);
unknown columns go to `extra` JSON.
Producer = the Flock org whose log it is (registry flock_names spelling): producers.json (authored, per request, or per
file inside a request via "files"), else the filename ('<dates>-<Org Name>-[Network-]Audit'), else the dominant Org Name
of the same request's own-search files, else the MuckRock agency name. The basis, the producers.json citation
(producer_source) and the registry agency_id are recorded per release.
"""
import collections
import csv
import gzip
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

from paths import sql_str  # noqa: F401  (also re-exported: other modules import it from here)

ALIASES = {"reason_1": "Reason", "test prompt": "Text Prompt", "license plates": "License Plate",
           "search date": "Search Time", "case number": "Case #"}
# the Flock audit-export superset (flock_audit_rows) and the event-log columns (flock_event_rows)
FLOCK_COLS = ["ID", "Name", "Org Name", "Total Networks Searched", "Total Devices Searched", "Time Frame", "License Plate",
              "Reason", "Case #", "Filters", "Search Time", "Search Type", "Text Prompt", "Moderation"]
EVENT_COLS = ["Timestamp", "User", "Event Type", "Entity Type", "Entity Details", "Event Id"]
KNOWN_COLS = set(FLOCK_COLS) | set(EVENT_COLS)
# how src_row locates a row in the original (releases.src_row_basis)
CSV_BASIS = "CSV record number, header = row 1 (the row shown when the file is opened in a spreadsheet); decoded {}"
SHEET_BASIS = "spreadsheet row number in the named sheet (1-based, as shown by Excel)"


def org_from_name(name):
    base = Path(name).name
    m = re.search(r"\d{2,4}[-_](?P<org>[A-Za-z][A-Za-z0-9 _.'&()/-]*?)[-_ ]+(?:Network[-_ ]?)?Audit", base)
    if not m:
        return None
    org = re.sub(r"_+", " ", m.group("org")).strip(" -_")
    return org if re.search(r"\b(CA|PD|SO|FD|Sheriff|Police)\b", org) else None


def norm_header(h):
    h = (h or "").strip()
    return ALIASES.get(h.lower(), h)


def canonical(raw_header):
    """Load-time column names for a released header: aliases mapped, blanks named, and a label already taken suffixed _2,
    _3 (the first free one), comparing without case: DuckDB column names are case-insensitive, so 'Reason' and 'reason'
    would be one column. A Flock column's exact label (FLOCK_COLS, EVENT_COLS) is taken first, wherever it stands, so a
    case variant before it ('reason' ahead of 'Reason') is the one suffixed and the Flock column keeps its values."""
    # 'Search Date' is the full timestamp only when no separate 'Search Time' column exists (San Jose splits the two)
    split_dt = "search time" in {(h or "").strip().lower() for h in raw_header}
    base = [((h or "").strip() if split_dt and (h or "").strip().lower() == "search date" else norm_header(h))
            or f"column{i:02d}" for i, h in enumerate(raw_header)]
    out, taken = [None] * len(base), set()
    for i, c in enumerate(base):
        if c in KNOWN_COLS and c.lower() not in taken:
            out[i] = c
            taken.add(c.lower())
    for i, c in enumerate(base):
        if out[i] is None:
            name, k = c, 1
            while name.lower() in taken:
                k += 1
                name = f"{c}_{k}"
            taken.add(name.lower())
            out[i] = name
    return out


HEADER_BASIS = ("header_raw = the released header row (the first row with >= 3 alphabetic cells); header = canonical(header_raw): "
                "labels trimmed, aliases mapped (reason_1, test prompt, license plates, search date, case number), San Jose's "
                "'Search Date' kept as is where a separate 'Search Time' exists, blank labels named columnNN (0-based position), "
                "a label already taken (ignoring case) suffixed _2, _3 (the first free one), a Flock column's exact label taken first")


def sql_ident(s):
    return '"' + s.replace('"', '""') + '"'


def extra_json(cols):
    """Columns outside the superset -> one JSON object per row; NULL when every one of them is empty (blank padding columns)."""
    if not cols:
        return "NULL"
    obj = "to_json({" + ", ".join(f"{sql_str(c)}: {sql_ident(c)}" for c in cols) + "})"
    return f"CASE WHEN coalesce({', '.join(sql_ident(c) for c in cols)}) IS NULL THEN NULL ELSE {obj} END"


OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def sniff(head, ext):
    """A unit's file type from its first bytes, over its name's extension (catalog member names can be cut short)."""
    if head[:4] == b"PK\x03\x04":
        return ext if ext in (".xlsx", ".xlsm", ".xlsb") else ".xlsx"
    return ".xls" if head[:8] == OLE2 else ext


def render(r):
    """A row's cells as text, the way they are loaded: None -> '', a whole float without its '.0', else str()."""
    return ["" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)) for v in r]


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


AUDIT_OF_KIND = {"network_audit": "network", "search_audit_own": "own", "event_log": "event"}


def audit_of(u, ov, fov):
    """A release's audit label: producers.json's (a file entry over the request's), else its catalog kind's. An authored
    label only says whether an audit is network or own-search; whether a file is an event log is the catalog kind's
    call (it decides the table: extract.table_of). So a request-level label is 'network' or 'own' and applies to the
    request's audits, never its event logs; a file-level label may not move a file across the event-log line. Anything
    else is an error."""
    default = AUDIT_OF_KIND[u["kind"]]
    if ov.get("audit") not in (None, "network", "own"):
        raise ValueError(f"producers.json {u['request_id']}: request-level audit {ov['audit']!r}: only 'network' or 'own'")
    if fov.get("audit") and (fov["audit"] == "event") != (default == "event"):
        raise ValueError(f"producers.json {u['request_id']} files entry for {u['name']!r}: audit {fov['audit']!r} on a "
                         f"{u['kind']} file would move its rows between the audit and event tables")
    return fov.get("audit") or (ov.get("audit") if default != "event" else None) or default


def resolve_member(z, member):
    """The catalog can hold a member name cut short before its extension: take the one member that starts with it."""
    names = z.namelist()
    if member in names:
        return member
    hits = [n for n in names if n.startswith(member)]
    if len(hits) != 1:
        raise ValueError(f"catalog member {member!r} matches {len(hits)} zip members")
    return hits[0]


def release_id(u):
    """A MuckRock unit's release id: mr:<request>:<container file>[!<member as cataloged>][#<sheet as cataloged>]."""
    return (f"mr:{u['request_id']}:{Path(u['local_path']).name}" + (f"!{u['member']}" if u["member"] else "")
            + (f"#{u['sheet']}" if u["sheet"] else ""))


def unit_id(u):
    """A short stable id for a unit (container path + member + sheet), for file and directory names."""
    return hashlib.sha1((u["local_path"] + str(u["member"]) + str(u["sheet"])).encode()).hexdigest()[:16]


def member_bytes(path, member):
    data = open(path, "rb").read()
    if not member:
        return data
    z = zipfile.ZipFile(io.BytesIO(data))
    return z.read(resolve_member(z, member))


def member_sha256(path, member):
    """sha256 of a zip member's decompressed bytes, streamed (fallback for a staged.json written before staging hashed it)."""
    h = hashlib.sha256()
    with zipfile.ZipFile(path) as z, z.open(resolve_member(z, member)) as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def file_override(ov, u, res):
    """producers.json per-file entry of a request ({"files": {<name>: {...}}}): matched on the zip member path, the member
    or file name, or the container file name."""
    files = ov.get("files") or {}
    names = [res.get("member"), u.get("member"), u.get("name"), Path(res.get("member") or u["name"]).name,
             Path(u["local_path"]).name]
    return next((files[n] for n in names if n and n in files), {})


def units(EV):
    cat = json.load(open(EV / "catalog.json")) + json.load(open(EV / "catalog2.json"))
    reqs = {r["request_id"]: r for r in json.load(open(EV / "catalog_requests.json"))}
    seen, out = set(), []
    for e in cat:
        if e.get("http") != 200 or not e.get("local_path"):
            continue
        for p in e.get("profile") or []:
            kind = p.get("kind")
            if kind not in ("network_audit", "search_audit_own", "event_log"):
                continue
            name = p.get("member") or e["file_name"]
            if name.lower().endswith(".pdf") or p.get("sheet") == "pdf":
                continue
            hdr = [norm_header(h) for h in (p.get("headers") or [])]
            if kind == "event_log" and not {"Event Type", "Entity Type"} <= set(hdr):
                continue
            if kind != "event_log" and not ({"Org Name", "Search Time"} <= set(hdr) or {"Search Time", "Total Networks Searched"} <= set(hdr)):
                continue
            key = (e["local_path"], p.get("member"), p.get("sheet"))
            if key in seen:
                continue
            seen.add(key)
            req = reqs.get(e["request_id"], {})
            out.append({"request_id": e["request_id"], "agency": req.get("agency") or e.get("agency"), "request_url": req.get("url"),
                        "local_path": e["local_path"], "url": e["url"], "member": p.get("member"), "sheet": p.get("sheet"),
                        "name": name, "kind": kind, "file_date": e.get("file_date")})
    return out


def _is_header(vals):
    return sum(1 for v in vals if re.search(r"[A-Za-z]", v)) >= 3  # first row with >=3 alphabetic cells (matches the profiler)


def to_csv(u, EV, TMP):
    """Stage one unit as a UTF-8 CSV whose first column `__src_row` is the row number a reader sees in the original.

    Returns {path, header (as released), content_sha256, member (full zip path), member_sha256, sheet (full name),
    src_row_basis}. CSV originals: content_sha256 = sha256 of the file bytes. Spreadsheets: sha256 over the non-empty data
    rows' values. member_sha256 = sha256 of the zip member's bytes (None when the unit is not in a zip).
    """
    raw = member_bytes(EV / u["local_path"], u["member"])
    member_sha = hashlib.sha256(raw).hexdigest() if u["member"] else None
    member_full = None
    if u["member"]:
        with zipfile.ZipFile(EV / u["local_path"]) as z:
            member_full = resolve_member(z, u["member"])
    ext = sniff(raw[:8], Path(member_full or u["name"]).suffix.lower())
    dest = TMP / "muckrock_units" / f"{unit_id(u)}.csv.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet_full, rows = None, None
    if ext in (".csv", ".tsv", ".txt") or ext == "":
        try:
            text, enc = raw.decode("utf-8-sig"), "utf-8"
        except UnicodeDecodeError:
            text, enc = raw.decode("cp1252", errors="replace"), "cp1252"
        rows = enumerate(csv.reader(io.StringIO(text, newline=""), delimiter="\t" if ext == ".tsv" else ","), start=1)
        basis = CSV_BASIS.format(enc)
        content_sha = hashlib.sha256(raw).hexdigest()
    elif ext in (".xlsx", ".xlsm", ".xls", ".xlsb"):
        try:  # calamine (Rust): ~100x faster than openpyxl on 500k-row sheets
            from python_calamine import CalamineWorkbook
            wb = CalamineWorkbook.from_filelike(io.BytesIO(raw))
            names = wb.sheet_names
            sheet_full = next(n for n in names if n[:40] == u["sheet"]) if u["sheet"] and u["sheet"] != "csv" else names[0]
            sh = wb.get_sheet_by_name(sheet_full)
            r0 = (sh.start or (0, 0))[0]  # to_python() starts at the used range; start is 0-based
            rows = ((r0 + i + 1, r) for i, r in enumerate(sh.to_python()))
        except Exception:  # noqa: BLE001  fall back to the pure-Python readers
            if ext in (".xlsx", ".xlsm"):
                import openpyxl
                wbo = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
                ws = next(w for w in wbo.worksheets if w.title[:40] == u["sheet"]) if u["sheet"] else wbo.worksheets[0]
                sheet_full = ws.title
                rows = enumerate(ws.iter_rows(min_row=1, values_only=True), start=1)
            elif ext == ".xlsb":
                from pyxlsb import open_workbook
                wbb = open_workbook(io.BytesIO(raw))
                sheet_full = next(x for x in wbb.sheets if x[:40] == u["sheet"]) if u["sheet"] else wbb.sheets[0]
                rows = ((r[0].r + 1, [c.v for c in r]) for r in wbb.get_sheet(sheet_full).rows() if r)
        basis = SHEET_BASIS
        content_sha = None
    if rows is None:
        return None
    h = hashlib.sha256()
    header = None
    # gzip: staging all units as plain CSV needs ~19 GB; read_csv reads .csv.gz directly
    with gzip.open(dest, "wt", newline="", encoding="utf-8", compresslevel=3) as fh:
        w = csv.writer(fh)
        for src_row, r in rows:
            vals = render(r)
            if header is None:
                if _is_header(vals):
                    header = vals
                    w.writerow(["__src_row"] + header)
                continue
            if any(v.strip() for v in vals):
                w.writerow([src_row] + vals)
                h.update(("\x1f".join(vals) + "\n").encode())
    if header is None:
        return None
    return {"path": str(dest), "header": header, "content_sha256": content_sha or h.hexdigest(), "member": member_full,
            "member_sha256": member_sha, "sheet": sheet_full, "src_row_basis": basis}


def _stage(args):
    u, EV, TMP = args
    try:
        return to_csv(u, Path(EV), Path(TMP)) or "no header row found"
    except Exception as ex:  # noqa: BLE001
        return f"{type(ex).__name__}: {str(ex)[:80]}"


def add_muckrock(con, EV, TMP, resolve=lambda org: None, log=lambda m: print(m, flush=True)):
    """resolve(org) -> registry agency_id or None (build_truth passes the repo registry's resolver)."""
    EV, TMP = Path(EV), Path(TMP)
    us = units(EV)
    overrides = {k: v for k, v in json.load(open(Path(__file__).parent / "producers.json")).items() if not k.startswith("_")}
    # producer inference: own-search files' dominant org per request
    con.execute("CREATE TABLE IF NOT EXISTS flock_event_rows (release_id VARCHAR, row_no BIGINT, src_row BIGINT, "
                + ", ".join(f'"{c}" VARCHAR' for c in EVENT_COLS) + ", extra JSON)")
    own_orgs = collections.defaultdict(collections.Counter)
    staged = []
    manifest = TMP / "muckrock_units" / "staged.json"
    if not manifest.exists():
        raise SystemExit(f"run the staging step first: uv run --locked --project scripts/audit_db python scripts/audit_db/muckrock_ingest.py stage {EV} {TMP}")
    staged_map = {(r["u"]["local_path"], r["u"]["member"], r["u"]["sheet"]): r["res"] for r in json.load(open(manifest))}
    results = [staged_map.get((u["local_path"], u["member"], u["sheet"]), "not staged") for u in us]
    for i, (u, res) in enumerate(zip(us, results), 1):
        if isinstance(res, str):
            log(f"  skip {u['request_id']} {u['name'][:60]}: {res}")
            continue
        path, raw_header = Path(res["path"]), res["header"]
        header = canonical(raw_header)
        rid = release_id(u)
        staged.append((u, path, raw_header, header, res, rid))
        if u["kind"] == "search_audit_own" and "Org Name" in header:
            oi = header.index("Org Name") + 1  # staged CSV starts with __src_row
            with gzip.open(path, "rt", encoding="utf-8-sig", errors="replace") as fh:
                rd = csv.reader(fh); next(rd)
                for j, row in enumerate(rd):
                    if j > 2000:
                        break
                    if oi < len(row) and row[oi].strip():
                        own_orgs[u["request_id"]][row[oi].strip()] += 1
    log(f"  staged {len(staged)} of {len(us)} units")
    container_sha, member_sha_missing = {}, 0
    for u, path, raw_header, header, res, rid in staged:
        ov = overrides.get(u["request_id"], {})
        fov = file_override(ov, u, res)   # a file-level entry wins over the request-level one, field by field
        org, basis = fov.get("org") or ov.get("org"), "producers.json"
        if fov.get("org") and not fov.get("source"):
            raise SystemExit(f"producers.json {u['request_id']}: a files entry that sets org must cite its own source")
        source = (fov.get("source") if fov.get("org") else ov.get("source")) if org else None   # producer_source: the authored citation
        if not org:
            org, basis = org_from_name(u["name"]), "filename"
        if not org and own_orgs.get(u["request_id"]):
            org, basis = own_orgs[u["request_id"]].most_common(1)[0][0], "own-search dominant Org Name"
        if not org:
            org, basis = u["agency"], "MuckRock agency name"
        agency_id = resolve(org)
        try:
            audit = audit_of(u, ov, fov)
        except ValueError as ex:
            raise SystemExit(str(ex)) from None
        names = "[" + ", ".join(sql_str(c) for c in ["__src_row"] + header) + "]"
        src = f"read_csv({sql_str(str(path))}, all_varchar=true, header=true, names={names}, null_padding=true, parallel=false)"
        cols = EVENT_COLS if audit == "event" else FLOCK_COLS
        sel = ", ".join(sql_ident(c) if c in header else f"NULL AS {sql_ident(c)}" for c in cols)
        extra = [h for h in header if h not in cols]
        ext = extra_json(extra)
        table = "flock_event_rows" if audit == "event" else "flock_audit_rows"
        try:
            con.execute(f"INSERT INTO {table} SELECT ?, row_number() OVER (), TRY_CAST(\"__src_row\" AS BIGINT), {sel}, {ext} FROM {src}", [rid])
        except Exception as ex:  # noqa: BLE001
            log(f"  load error {rid[:80]}: {str(ex)[:160]}")
            continue
        n = con.execute(f"SELECT count(*) FROM {table} WHERE release_id = ?", [rid]).fetchone()[0]
        rel_on = (u.get("file_date") or "")[:10] or None
        if u["local_path"] not in container_sha:   # a zip holds many units: hash it once
            container_sha[u["local_path"]] = sha256_file(EV / u["local_path"])
        msha = res.get("member_sha256")
        if msha is None and res["member"]:   # staged.json from before staging hashed members: hash now (restage to skip)
            member_sha_missing += 1
            msha = member_sha256(EV / u["local_path"], res["member"])
        con.execute("""INSERT INTO releases (release_id, producer, producer_agency_id, producer_basis, producer_source, audit, pra_id,
                         container_root, container_path, member, member_sha256, sheet, source_file, container_sha256, header,
                         header_raw, header_basis, n_rows, content_sha256, released_on, released_on_basis, source_url,
                         request_url, src_row_basis)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,TRY_CAST(? AS DATE),?,?,?,?)""",
                    [rid, org, agency_id, basis, source, audit, f"muckrock-{u['request_id']}",
                     "evidence", u["local_path"], res["member"], msha, res["sheet"], Path(res["member"] or u["local_path"]).name,
                     container_sha[u["local_path"]], header, raw_header, HEADER_BASIS,
                     n, res["content_sha256"], rel_on, "MuckRock attachment date" if rel_on else None, u["url"],
                     u.get("request_url"), res["src_row_basis"]])
    if member_sha_missing:
        log(f"  NOTE: staged.json predates member hashing; hashed {member_sha_missing} zip members at load (restage to skip)")
    log(f"  muckrock: {len(staged)} releases loaded in full")


def stage_all(EV, TMP, workers=2):  # each worker can hold a whole sheet in memory
    """Separate step (own process tree): convert every unit to CSV in parallel; write staged.json."""
    from concurrent.futures import ProcessPoolExecutor
    EV, TMP = Path(EV), Path(TMP)
    us = units(EV)
    with ProcessPoolExecutor(workers) as ex:
        results = list(ex.map(_stage, [(u, str(EV), str(TMP)) for u in us], chunksize=1))
    out = [{"u": u, "res": r} for u, r in zip(us, results)]
    (TMP / "muckrock_units").mkdir(parents=True, exist_ok=True)
    json.dump(out, open(TMP / "muckrock_units" / "staged.json", "w"))
    ok = sum(1 for r in results if not isinstance(r, str))
    print(f"staged {ok} of {len(us)} units; {sum(isinstance(r, str) for r in results)} errors", flush=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 4 and sys.argv[1] == "stage":
        stage_all(sys.argv[2], sys.argv[3])
