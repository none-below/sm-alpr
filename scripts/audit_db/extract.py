"""Per-release extraction: one released file, zip member or sheet -> a chunk (rows.parquet + chunk.json), streamed.

A chunk is a function of its inputs only: the container's bytes, the catalog entry that names the unit, and this code
with its pinned readers. Authored facts (producers.json, dispositions.json, layouts.json), the agency registry and
anything that needs other units (the own-search producer inference) are applied when chunks are assembled, never here,
so editing an authored fact never invalidates a chunk. The chunk carries what assembly needs from its rows instead
(own_org_counts).

Reading is streamed. A CSV, plain or a zip member, is read record by record through ZipFile.open() and never held
whole. It is decoded as UTF-8; if a byte turns out not to be UTF-8, the partial output is discarded and the unit is
read again as cp1252 (the same whole-file decision the old staging step made). A spreadsheet needs random access, so
its bytes are read whole and calamine iterates the sheet's rows. There is no fallback reader: a unit calamine cannot
read is a failed chunk, recorded as one. Rows reach DuckDB in Arrow batches and are written as Parquet into a
temporary directory, which is renamed into place only when the chunk is complete.

rows.parquet has exactly the columns of its truth table (flock_audit_rows, or flock_event_rows for an event log):
release_id, row_no (1-based, file order, assigned here), src_row, the Flock fields, extra (JSON). Cells are rendered the
way staging rendered them, and an empty cell is NULL.

  extract_unit(u, evidence_dir, out_root) -> the chunk.json dict       (u: one entry of muckrock_ingest.units())
"""
import csv
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from functools import lru_cache
import uuid
import zipfile
from importlib import metadata
from pathlib import Path

import pyarrow as pa

sys.path.insert(0, str(Path(__file__).parent))
from muckrock_ingest import (EVENT_COLS, FLOCK_COLS, HEADER_BASIS, _is_header, canonical, extra_json, release_id,  # noqa: E402
                             render, resolve_member, sha256_file, sniff, sql_ident, sql_str, unit_id)
from paths import duck_connect, pid_alive  # noqa: E402

CHUNK_SCHEMA = 1          # bump when chunk.json or rows.parquet change shape
BATCH = 100_000           # rows per Arrow batch handed to DuckDB
OWN_ORG_ROWS = 2001       # data rows counted for the own-search producer inference (as the loader always did)
CSV_EXT, SHEET_EXT = {".csv", ".tsv", ".txt", ""}, {".xlsx", ".xlsm", ".xls", ".xlsb"}
CSV_BASIS = "CSV record number, header = row 1 (the row shown when the file is opened in a spreadsheet); decoded {}"
SHEET_BASIS = "spreadsheet row number in the named sheet (1-based, as shown by Excel)"
CODE_FILES = ("extract.py", "muckrock_ingest.py")   # the code that decides a chunk's content


class NoHeader(ValueError):
    pass


def table_of(u):
    """Where a unit's rows go, decided by the catalog's content-based kind (an authored audit override may relabel a
    network audit as own-search or back, but never moves rows between the audit and the event tables)."""
    return ("flock_event_rows", EVENT_COLS) if u["kind"] == "event_log" else ("flock_audit_rows", FLOCK_COLS)


def unit_sha256(u):
    return hashlib.sha256(json.dumps(u, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@lru_cache(maxsize=None)
def versions():
    return {"python": platform.python_version(), **{p: metadata.version(p) for p in ("duckdb", "python-calamine", "pyarrow")}}


@lru_cache(maxsize=None)
def code_identity():
    """What made the chunk: a hash of the content-deciding source files, plus the git commit and whether this directory
    had uncommitted edits (the commit alone identifies the code only when it did not)."""
    here = Path(__file__).parent
    h = hashlib.sha256(b"".join((here / f).read_bytes() for f in CODE_FILES)).hexdigest()
    git = lambda *a: subprocess.run(["git", "-C", str(here), *a], capture_output=True, text=True).stdout.strip()
    return {"code_sha256": h, "commit": git("rev-parse", "HEAD") or None,
            "dirty": bool(git("status", "--porcelain", "--", ".")) if git("rev-parse", "HEAD") else None}


class _Tee(io.RawIOBase):
    """Hashes the bytes as the parser pulls them, so the hash covers exactly what was parsed."""

    def __init__(self, f):
        self.f, self.h = f, hashlib.sha256()

    def readable(self):
        return True

    def readinto(self, b):
        n = self.f.readinto(b)
        self.h.update(memoryview(b)[:n])
        return n

    def close(self):
        self.f.close()
        super().close()


def _write(rows, rid, cols, out, *, kind, sheet_hash, spill_root):
    """Header detection, rendering and the Parquet write. Returns the chunk's row-level facts.

    Cells beyond the header's width: trailing empty ones carry nothing and are dropped; a row with any other goes to
    extra under overflow_key ('overflow', or the first free name if the header already has one), as a JSON list. The
    old loader dropped them, so for such a chunk row_digest_legacy is the digest with them left out, comparable to it."""
    header = None
    for _, r in rows:
        vals = render(r)
        if _is_header(vals):
            header = vals
            break
    if header is None:
        raise NoHeader("no header row found")
    canon, n = canonical(header), len(header)
    rn, sr, ov, okey = (_free_name(b, canon) for b in ("__row_no", "__src_row", "__overflow", "overflow"))   # never a released label
    org_i = canon.index("Org Name") if kind == "search_audit_own" and "Org Name" in canon else None
    orgs, content, overflow = {}, hashlib.sha256(), [0]
    schema = pa.schema([(rn, pa.int64()), (sr, pa.string()), (ov, pa.string())] + [(c, pa.string()) for c in canon])

    def batches():
        buf, row_no = [], 0
        for src_row, r in rows:
            vals = render(r)
            if not any(v.strip() for v in vals):
                continue
            row_no += 1
            if sheet_hash:
                content.update(("\x1f".join(vals) + "\n").encode())
            extra_cells = None
            if len(vals) > n:
                cut = vals[n:]
                while cut and cut[-1] == "":
                    cut.pop()
                if cut:
                    extra_cells, overflow[0] = json.dumps(cut, ensure_ascii=False), overflow[0] + 1
                vals = vals[:n]
            if org_i is not None and row_no <= OWN_ORG_ROWS and org_i < len(vals) and vals[org_i].strip():
                orgs[vals[org_i].strip()] = orgs.get(vals[org_i].strip(), 0) + 1
            buf.append([row_no, str(src_row), extra_cells] + [v if v != "" else None for v in vals] + [None] * (n - len(vals)))
            if len(buf) == BATCH:
                yield pa.RecordBatch.from_arrays([pa.array(c, t.type) for c, t in zip(zip(*buf), schema)], schema=schema)
                buf = []
        if buf:
            yield pa.RecordBatch.from_arrays([pa.array(c, t.type) for c, t in zip(zip(*buf), schema)], schema=schema)

    con = duck_connect(spill_root=spill_root, threads=2, memory_limit="1GB", max_temp_directory_size="4GiB",
                       preserve_insertion_order=True, enable_progress_bar=False)
    try:
        con.register("src", pa.RecordBatchReader.from_batches(schema, batches()))
        sel = ", ".join(sql_ident(c) if c in canon else f"CAST(NULL AS VARCHAR) AS {sql_ident(c)}" for c in cols)
        ext = f"CAST({extra_json([c for c in canon if c not in cols])} AS JSON)"
        extra = (f"CASE WHEN {sql_ident(ov)} IS NULL THEN {ext} ELSE json_merge_patch(coalesce({ext}, '{{}}'::JSON), "
                 f"json_object({sql_str(okey)}, {sql_ident(ov)}::JSON)) END")
        con.execute(f"COPY (SELECT {sql_str(rid)} AS release_id, {sql_ident(rn)} AS row_no, TRY_CAST({sql_ident(sr)} AS "
                    f"BIGINT) AS src_row, {sel}, {extra} AS extra FROM src) TO {sql_str(str(out))} (FORMAT parquet, "
                    f"COMPRESSION zstd)")
        fields = ", ".join(sql_ident(c) for c in cols)
        legacy = (f"CASE WHEN list_contains(json_keys(extra), {sql_str(okey)}) THEN nullif(json_merge_patch(extra, "
                  f"json_object({sql_str(okey)}, NULL)), '{{}}'::JSON) ELSE extra END")
        n_rows, digest, legacy_digest = con.execute(
            f"SELECT count(*), bit_xor(hash(row_no, src_row, {fields}, extra::VARCHAR)), "
            + (f"bit_xor(hash(row_no, src_row, {fields}, ({legacy})::VARCHAR))" if overflow[0] else "NULL")
            + f" FROM read_parquet({sql_str(str(out))})").fetchone()
    finally:
        con.close()
    return {"header_raw": header, "header": canon, "header_basis": HEADER_BASIS, "n_rows": n_rows,
            "row_digest": str(digest or 0), "content_sha256": content.hexdigest() if sheet_hash else None,
            "own_org_counts": list(orgs.items()) if org_i is not None else None, "overflow_rows": overflow[0],
            "overflow_key": okey if overflow[0] else None,
            "row_digest_legacy": str(legacy_digest or 0) if overflow[0] else None}


def _free_name(base, taken):
    """base, or base with '_' appended until no taken name equals it ignoring case (DuckDB names are case-insensitive)."""
    low = {t.lower() for t in taken}
    while base.lower() in low:
        base += "_"
    return base


def _extract(u, EV, rid, cols, out, spill_root):
    path = EV / u["local_path"]
    zf = zipfile.ZipFile(path) if u["member"] else None
    try:
        member = resolve_member(zf, u["member"]) if zf else None
        opener = (lambda: zf.open(member)) if zf else (lambda: open(path, "rb"))
        with opener() as f:
            ext = sniff(f.read(8), Path(member or u["name"]).suffix.lower())
        base = {"member": member, "source_file": Path(member or u["local_path"]).name}
        if ext in CSV_EXT:
            for enc in ("utf-8", "cp1252"):
                tee, decode_error = _Tee(opener()), []
                try:
                    text = io.TextIOWrapper(io.BufferedReader(tee, 1 << 20), newline="",
                                            encoding="utf-8-sig" if enc == "utf-8" else "cp1252",
                                            errors="strict" if enc == "utf-8" else "replace")

                    def records(text=text, decode_error=decode_error):
                        try:
                            yield from enumerate(csv.reader(text, delimiter="\t" if ext == ".tsv" else ","), start=1)
                        except UnicodeDecodeError:
                            decode_error.append(True)
                            raise
                    try:
                        res = _write(records(), rid, cols, out, kind=u["kind"], sheet_hash=False, spill_root=spill_root)
                    except Exception:
                        out.unlink(missing_ok=True)
                        if decode_error and enc == "utf-8":
                            continue          # not UTF-8 after all: read the whole unit again as cp1252
                        raise
                    while tee.read(1 << 20):   # hash to the end, whatever the parser left unread
                        pass
                    digest = tee.h.hexdigest()
                    return {**base, **res, "content_sha256": digest, "member_sha256": digest if member else None,
                            "sheet": None, "src_row_basis": CSV_BASIS.format(enc), "reader": f"csv, {enc}"}
                finally:
                    tee.close()
        if ext in SHEET_EXT:
            from python_calamine import CalamineWorkbook
            with opener() as f:
                data = f.read()
            wb = CalamineWorkbook.from_filelike(io.BytesIO(data))
            if u["sheet"] and u["sheet"] != "csv":   # the catalog keeps the first 40 characters of a sheet name
                hits = [s for s in wb.sheet_names if s[:40] == u["sheet"]]
                if len(hits) != 1:
                    raise ValueError(f"catalog sheet {u['sheet']!r} matches {len(hits)} sheets")
                sheet = hits[0]
            else:
                sheet = wb.sheet_names[0]
            sh = wb.get_sheet_by_name(sheet)
            # iter_rows() starts at the sheet's row 1 (its columns at the used range's first), so the count is the
            # row number Excel shows; to_python() starts at the used range instead, which the old loader offset by start
            res = _write(enumerate(sh.iter_rows(), start=1), rid, cols, out, kind=u["kind"],
                         sheet_hash=True, spill_root=spill_root)
            return {**base, **res, "member_sha256": hashlib.sha256(data).hexdigest() if member else None,
                    "sheet": sheet, "src_row_basis": SHEET_BASIS, "reader": f"python-calamine {metadata.version('python-calamine')}"}
        raise ValueError(f"unsupported file type {ext!r}")
    finally:
        if zf:
            zf.close()


TMP_DIR = re.compile(r"\.(tmp|old)-[0-9a-f]{16}-(\d+)-[0-9a-f]{8}")   # .tmp-<unit_id>-<pid>-<random>


def _swap_into_place(tmp, final):
    """Rename tmp to final. An existing final is moved aside first, then removed, or put back if the rename fails; a
    leftover .old- directory is removed by sweep_tmp."""
    old = None
    if final.exists():
        old = final.with_name(f".old-{final.name}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        os.replace(final, old)
    try:
        os.replace(tmp, final)
    except BaseException:
        if old:
            os.replace(old, final)
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)


def sweep_tmp(out_root):
    """Remove .tmp-/.old- directories that workers killed mid-write left behind (their pid is gone)."""
    gone = [d for d in Path(out_root).glob("*/.*") if (m := TMP_DIR.fullmatch(d.name)) and not pid_alive(int(m.group(2)))]
    for d in gone:
        shutil.rmtree(d, ignore_errors=True)
    return gone


def chunk_dir(out_root, u):
    return Path(out_root) / str(u["request_id"]) / unit_id(u)


def _manifest(u, container_sha256, code):
    return {"chunk_schema": CHUNK_SCHEMA, "release_id": release_id(u), "unit_id": unit_id(u), "table": table_of(u)[0],
            "unit": u, "unit_sha256": unit_sha256(u), "container_sha256": container_sha256, "code": code or code_identity(),
            "versions": versions(), "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _publish(u, out_root, fill):
    """Build a chunk in a temporary directory beside its place (fill(tmp) returns its manifest), then swap it in."""
    final = chunk_dir(out_root, u)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f".tmp-{final.name}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.mkdir()
    try:
        man = fill(tmp)
        (tmp / "chunk.json").write_text(json.dumps(man, indent=1, ensure_ascii=False))
        _swap_into_place(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return man


def extract_unit(u, evidence_dir, out_root, spill_root=None, container_sha256=None, code=None):
    """Extract one unit into <out_root>/<request_id>/<unit_id>/, atomically. A bad input never raises: a unit that
    cannot be read (a missing container, or a reader that panics, included) gets a chunk.json with status 'failed' and
    the error, and no rows.parquet. container_sha256 and code: the caller's, when it already has them (a zip can hold
    dozens of units). spill_root: see paths.duck_connect."""
    EV = Path(evidence_dir)

    def fill(tmp):
        man, t0, csha = _manifest(u, container_sha256, code), time.time(), container_sha256
        try:
            csha = csha or sha256_file(EV / u["local_path"])
            res = _extract(u, EV, man["release_id"], table_of(u)[1], tmp / "rows.parquet", spill_root)
            man.update(container_sha256=csha, status="ok", **res, parquet_sha256=sha256_file(tmp / "rows.parquet"))
        except BaseException as ex:  # noqa: BLE001  pyo3 raises a Rust panic in a reader as a BaseException
            if isinstance(ex, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            (tmp / "rows.parquet").unlink(missing_ok=True)
            man.update(container_sha256=csha, status="failed", error=f"{type(ex).__name__}: {ex}"[:1000])
        man["seconds"] = round(time.time() - t0, 1)
        return man
    return _publish(u, out_root, fill)


def failed_chunk(u, out_root, error, container_sha256=None, code=None):
    """Record a unit whose extraction never returned (its worker process died) as a failed chunk."""
    return _publish(u, out_root, lambda tmp: {**_manifest(u, container_sha256, code), "status": "failed", "error": error[:1000]})
