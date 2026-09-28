"""Extract per-release chunks (extract.py) for the MuckRock corpus, optionally checking each against the current truth.

  uv run --locked --project scripts/audit_db python scripts/audit_db/build_chunks.py [OUT] [--only PATTERN]
      [--sample N [--seed S]] [--limit N] [--workers N] [--force] [--verify [--discard]]

OUT defaults to <audit dir>/chunks. One chunk per unit of the evidence catalog (muckrock_ingest.units()), in
<OUT>/<request_id>/<unit_id>/. A chunk whose inputs are unchanged (container sha256, catalog entry, code, versions) is
kept, not re-extracted, unless --force.
  --only PATTERN   release_id LIKE pattern ('%' any run, '_' any one character), e.g. 'mr:205259:%'
  --sample N       a random N of the selected units (--seed, default 1); --limit N: the first N
  --workers N      extraction processes (default 2; each can hold one spreadsheet in memory)
  --verify         compare each chunk with truth.duckdb in the audit dir: the release's intrinsic columns (header,
                   header_raw, member, member_sha256, sheet, source_file, content_sha256, container_sha256, n_rows,
                   src_row_basis) and a digest over every row. Writes <OUT>/verify-<UTC>.jsonl; exit 1 on any mismatch.
  --discard        with --verify: delete each chunk's rows.parquet once checked (keeps chunk.json), to save disk
Heavy on the full corpus: run it niced at background QoS (nice -n 19 taskpolicy -b).
"""
import argparse
import json
import random
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import extract  # noqa: E402
from muckrock_ingest import units  # noqa: E402
from paths import audit_dir, evidence_dir, use_spill_dir  # noqa: E402

RELEASE_FIELDS = ["header", "header_raw", "member", "member_sha256", "sheet", "source_file", "content_sha256",
                  "container_sha256", "n_rows", "src_row_basis"]


def like(pattern):
    return re.compile("".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern) + r"\Z", re.S)


CONTAINER_SHA = {}


def container_sha(u):
    """Each container hashed once per run (units of one zip share it)."""
    p = u["local_path"]
    if p not in CONTAINER_SHA:
        CONTAINER_SHA[p] = extract.sha256_file(evidence_dir_() / p)
    return CONTAINER_SHA[p]


def reusable(u, out, force):
    """The existing chunk, if its inputs are unchanged and its rows are on disk."""
    d = extract.chunk_dir(out, u)
    if force or not (d / "chunk.json").exists():
        return None
    man = json.loads((d / "chunk.json").read_text())
    same = (man.get("chunk_schema") == extract.CHUNK_SCHEMA and man.get("unit_sha256") == extract.unit_sha256(u)
            and man.get("code", {}).get("code_sha256") == extract.code_identity()["code_sha256"]
            and man.get("versions") == extract.versions())
    if not same or (man.get("status") == "ok" and not (d / "rows.parquet").exists()):
        return None
    if man.get("container_sha256") != container_sha(u):
        return None
    return man


def evidence_dir_():
    return Path(ARGS.evidence) if ARGS.evidence else evidence_dir()


def verifier(A):
    import duckdb
    con = duckdb.connect(str(A / "truth.duckdb"), read_only=True)
    use_spill_dir(con)
    con.execute("SET threads=2; SET memory_limit='2GB'; SET enable_progress_bar=false")

    def check(man):
        rid = man["release_id"]
        rel = con.execute(f"SELECT {', '.join(RELEASE_FIELDS)} FROM releases WHERE release_id = ?", [rid]).fetchone()
        if rel is None:
            return {"match": False, "why": "not in truth"}
        if man["status"] != "ok":
            return {"match": False, "why": f"chunk failed: {man.get('error')}"}
        diffs = [f for f, t in zip(RELEASE_FIELDS, rel) if man.get(f) != t]
        table, cols = man["table"], extract.table_of(man["unit"])[1]
        n, digest = con.execute(f"SELECT count(*), bit_xor(hash(row_no, src_row, {', '.join(extract.sql_ident(c) for c in cols)}, "
                                f"extra::VARCHAR)) FROM {table} WHERE release_id = ?", [rid]).fetchone()
        if (n, str(digest or 0)) != (man["n_rows"], man["row_digest"]):
            diffs.append(f"rows (truth {n} rows, chunk {man['n_rows']})")
        return {"match": not diffs, "diffs": diffs}
    return check, con


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", help="chunk directory (default <audit dir>/chunks)")
    ap.add_argument("--only")
    ap.add_argument("--sample", type=int)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--discard", action="store_true")
    ap.add_argument("--audit-dir", help="directory holding the databases (default: paths.audit_dir())")
    ap.add_argument("--evidence", help="MuckRock evidence directory (default: paths.evidence_dir())")
    ARGS = ap.parse_args()
    A = Path(ARGS.audit_dir or audit_dir())
    EV, out = evidence_dir_(), Path(ARGS.out or A / "chunks")
    sel = [u for u in units(EV) if not ARGS.only or like(ARGS.only).match(extract.release_id(u))]
    if ARGS.sample:
        sel = random.Random(ARGS.seed).sample(sel, min(ARGS.sample, len(sel)))
    if ARGS.limit:
        sel = sel[:ARGS.limit]
    print(f"{len(sel)} units selected; chunks in {out}", flush=True)
    check, tcon = verifier(A) if ARGS.verify else (None, None)
    report = out / f"verify-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl" if ARGS.verify else None
    counts, t0, done = {"ok": 0, "failed": 0, "reused": 0, "match": 0, "mismatch": 0}, time.time(), 0
    out.mkdir(parents=True, exist_ok=True)

    def finish(man, reused):
        nonlocal done
        done += 1
        counts["reused" if reused else man["status"]] += 1
        line = f"[{done}/{len(sel)}] {man['status']:6s} {man.get('n_rows', '-'):>9} rows {man.get('seconds', 0):6.1f}s {man['release_id'][:90]}"
        if check:
            v = check(man)
            counts["match" if v["match"] else "mismatch"] += 1
            line += "" if v["match"] else f"  MISMATCH {v.get('why') or v['diffs']}"
            with open(report, "a") as fh:
                fh.write(json.dumps({"release_id": man["release_id"], "status": man["status"], **v}) + "\n")
            if ARGS.discard:
                (extract.chunk_dir(out, man["unit"]) / "rows.parquet").unlink(missing_ok=True)
        elif man["status"] != "ok":
            line += f"  {man.get('error')}"
        print(line, flush=True)

    todo = []
    for u in sel:
        man = reusable(u, out, ARGS.force)
        if man:
            finish(man, True)
        else:
            todo.append(u)
    with ProcessPoolExecutor(ARGS.workers) as ex:
        futs = [ex.submit(extract.extract_unit, u, str(EV), str(out), None, container_sha(u)) for u in todo]
        for f in as_completed(futs):
            finish(f.result(), False)
    if tcon:
        tcon.close()
    summary = ", ".join(f"{k} {v}" for k, v in counts.items() if v or k in ("ok", "failed"))
    print(f"done in {time.time() - t0:.0f} s: {summary}" + (f"; report {report}" if report else ""), flush=True)
    sys.exit(1 if counts["failed"] or counts["mismatch"] else 0)


ARGS = None
if __name__ == "__main__":
    main()
