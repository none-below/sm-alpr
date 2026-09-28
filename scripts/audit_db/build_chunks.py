"""Extract per-release chunks (extract.py) for the MuckRock corpus, optionally checking each against the current truth.

  uv run --locked --project scripts/audit_db python scripts/audit_db/build_chunks.py [OUT] [--only PATTERN]
      [--sample N [--seed S]] [--limit N] [--workers N] [--force] [--verify [--discard]] [--audit-dir DIR] [--evidence DIR]

OUT defaults to <audit dir>/chunks. One chunk per unit of the evidence catalog (muckrock_ingest.units()), in
<OUT>/<request_id>/<unit_id>/. A chunk whose inputs are unchanged (container sha256, catalog entry, code, versions) and
that extracted cleanly and still has its rows.parquet is kept, not re-extracted, unless --force; a failed chunk (or
one whose rows --discard removed) is extracted again. A unit whose container is missing, or whose worker process dies
(a crash in a reader, the OOM killer), gets a failed chunk; the run goes on.
  --only PATTERN   release_id LIKE pattern ('%' any run, '_' any one character), e.g. 'mr:205259:%'
  --sample N       a random N of the selected units (--seed, default 1); --limit N: the first N
  --workers N      extraction processes (default 2; each can hold one spreadsheet in memory)
  --verify         compare each chunk with truth.duckdb in the audit dir: the release's intrinsic columns (header,
                   header_raw, member, member_sha256, sheet, source_file, content_sha256, container_sha256, n_rows,
                   src_row_basis) and a digest over every row. Reads the evidence truth was built from
                   (build_info.evidence_dir) unless --evidence is given. Writes <OUT>/verify-<UTC>.jsonl.
                   A chunk with cells beyond its header (extra.overflow, which truth's loader dropped) is compared by
                   its row_digest_legacy, the digest without them.
  --discard        with --verify: delete a chunk's rows.parquet once it matched (keeps chunk.json; a mismatch keeps its
                   rows for inspection), to save disk. The next run extracts those units again.
Exit 1 when any selected unit's chunk failed or did not match; 2 when nothing is selected. Spills go under
<audit dir>/spill.
Heavy on the full corpus: run it niced at background QoS (nice -n 19 taskpolicy -b).
"""
import argparse
import json
import random
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pyarrow as pa

sys.path.insert(0, str(Path(__file__).parent))
import extract  # noqa: E402
from muckrock_ingest import EVENT_COLS, FLOCK_COLS, sql_ident, units  # noqa: E402
from paths import audit_dir, duck_connect, evidence_dir  # noqa: E402

RELEASE_FIELDS = ["header", "header_raw", "member", "member_sha256", "sheet", "source_file", "content_sha256",
                  "container_sha256", "n_rows", "src_row_basis"]


def like(pattern):
    return re.compile("".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern) + r"\Z", re.S)


def reusable(u, out, force, container_sha, code):
    """The existing chunk, if it extracted cleanly from the same inputs and its rows are on disk; else None."""
    d = extract.chunk_dir(out, u)
    if force or not (d / "chunk.json").exists():
        return None
    man = json.loads((d / "chunk.json").read_text())
    same = (man.get("status") == "ok" and man.get("chunk_schema") == extract.CHUNK_SCHEMA
            and man.get("unit_sha256") == extract.unit_sha256(u) and man.get("container_sha256") == container_sha
            and man.get("code", {}).get("code_sha256") == code["code_sha256"] and man.get("versions") == extract.versions())
    return man if same and (d / "rows.parquet").exists() else None


def verifier(A, spill):
    """check(man) compares a chunk with truth; prefetch(rids) reads truth's side for every selected release in one pass
    per table, in a thread of its own, while extraction runs (check waits for it)."""
    con = duck_connect(A / "truth.duckdb", read_only=True, spill_root=spill, threads=2, memory_limit="2GB",
                       enable_progress_bar=False)
    truth, pending = {}, []

    def _prefetch(rids):
        con.register("want", pa.table({"release_id": sorted(set(rids))}))
        for rid, *rel in con.execute(f"SELECT release_id, {', '.join(RELEASE_FIELDS)} FROM releases "
                                     "WHERE release_id IN (SELECT release_id FROM want)").fetchall():
            truth[rid] = {"rel": rel}
        for table, cols in (("flock_audit_rows", FLOCK_COLS), ("flock_event_rows", EVENT_COLS)):
            for rid, n, digest in con.execute(
                    f"SELECT release_id, count(*), bit_xor(hash(row_no, src_row, {', '.join(sql_ident(c) for c in cols)}, "
                    f"extra::VARCHAR)) FROM {table} WHERE release_id IN (SELECT release_id FROM want) GROUP BY release_id"
                    ).fetchall():
                if rid in truth:
                    truth[rid].setdefault("tables", {})[table] = (n, str(digest or 0))
        con.unregister("want")

    def prefetch(rids):
        tp = ThreadPoolExecutor(1)
        pending.append(tp.submit(_prefetch, rids))
        tp.shutdown(wait=False)

    def wait():
        while pending:
            pending.pop().result()

    def close():
        wait()
        con.close()

    def check(man):
        wait()
        t = truth.get(man["release_id"])
        if t is None:
            return {"match": False, "why": "not in truth" + (f"; chunk failed: {man.get('error')}" if man["status"] != "ok" else "")}
        if man["status"] != "ok":
            return {"match": False, "why": f"chunk failed: {man.get('error')}"}
        diffs = [f for f, v in zip(RELEASE_FIELDS, t["rel"]) if man.get(f) != v]
        n, digest = t.get("tables", {}).get(man["table"], (0, "0"))
        mine = man.get("row_digest_legacy") or man["row_digest"]   # truth's loader dropped cells beyond the header
        if (n, digest) != (man["n_rows"], mine):
            diffs.append(f"rows (truth {n} rows, chunk {man['n_rows']})")
        return {"match": not diffs, "diffs": diffs, **({"digest": "row_digest_legacy"} if man.get("row_digest_legacy") else {})}
    truth_ev = dict(con.execute("SELECT key, value FROM build_info").fetchall()).get("evidence_dir")
    return check, prefetch, close, truth_ev


def main():
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
    ap.add_argument("--evidence", help="MuckRock evidence directory (default: truth's with --verify, else paths.evidence_dir())")
    args = ap.parse_args()
    if args.discard and not args.verify:
        ap.error("--discard only applies with --verify (it deletes rows once they are checked)")
    A = Path(args.audit_dir or audit_dir())
    spill, out = A / "spill", Path(args.out or A / "chunks")
    check, prefetch, tclose, truth_ev = verifier(A, spill) if args.verify else (None, None, None, None)
    EV = Path(args.evidence or truth_ev or evidence_dir())
    if args.verify and truth_ev and EV.resolve() != Path(truth_ev).resolve():
        print(f"WARNING: verifying against truth built from {truth_ev}, but reading evidence from {EV}", flush=True)
    sel = [u for u in units(EV) if not args.only or like(args.only).match(extract.release_id(u))]
    if args.sample is not None:
        sel = random.Random(args.seed).sample(sel, min(args.sample, len(sel)))
    if args.limit is not None:
        sel = sel[:args.limit]
    if not sel:
        print(f"no units selected from {EV}" + (f" by --only {args.only!r}" if args.only else ""), file=sys.stderr)
        sys.exit(2)
    if prefetch:
        prefetch([extract.release_id(u) for u in sel])
    out.mkdir(parents=True, exist_ok=True)
    swept = extract.sweep_tmp(out)
    print(f"{len(sel)} units selected; chunks in {out}" + (f"; removed {len(swept)} leftover temp dirs" if swept else ""),
          flush=True)
    code, t0 = extract.code_identity(), time.time()
    def container_sha(p):   # a missing or unreadable container: None, and extract_unit records the failure
        try:
            return extract.sha256_file(EV / p)
        except OSError:
            return None
    with ThreadPoolExecutor(4) as tp:   # hashlib releases the GIL: hash the containers side by side, each once
        paths_ = sorted({u["local_path"] for u in sel})
        csha = dict(zip(paths_, tp.map(container_sha, paths_)))
    report = out / f"verify-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl" if args.verify else None
    counts, done = {"extracted": 0, "reused": 0, "failed": 0, "match": 0, "mismatch": 0}, 0

    def finish(man, reused):
        nonlocal done
        done += 1
        counts["reused" if reused else "extracted"] += 1
        counts["failed"] += man["status"] != "ok"
        line = f"[{done}/{len(sel)}] {man['status']:6s} {man.get('n_rows', '-'):>9} rows {man.get('seconds', 0):6.1f}s {man['release_id'][:90]}"
        if man.get("overflow_rows"):
            line += f"  ({man['overflow_rows']} rows with cells beyond the header, kept in extra.overflow)"
        if check:
            v = check(man)
            counts["match" if v["match"] else "mismatch"] += 1
            line += "" if v["match"] else f"  MISMATCH {v.get('why') or v['diffs']}"
            with open(report, "a") as fh:
                fh.write(json.dumps({"release_id": man["release_id"], "status": man["status"], **v}) + "\n")
            if args.discard and v["match"]:
                (extract.chunk_dir(out, man["unit"]) / "rows.parquet").unlink(missing_ok=True)
        elif man["status"] != "ok":
            line += f"  {man.get('error')}"
        print(line, flush=True)

    todo = []
    for u in sel:
        man = reusable(u, out, args.force, csha[u["local_path"]], code)
        if man:
            finish(man, True)
        else:
            todo.append(u)
    def run_pool(us, workers):
        """Extract us; returns the units whose worker died (a broken pool fails every unfinished future with it)."""
        broken = []
        with ProcessPoolExecutor(workers) as ex:
            futs = {ex.submit(extract.extract_unit, u, str(EV), str(out), str(spill), csha[u["local_path"]], code): u
                    for u in us}
            for f in as_completed(futs):
                try:
                    man = f.result()
                except BrokenProcessPool:
                    broken.append(futs[f])
                    continue
                finish(man, False)
        return broken

    for u in run_pool(todo, args.workers):   # one unit per pool, so a unit that kills its worker takes only itself down
        if run_pool([u], 1):
            finish(extract.failed_chunk(u, out, "the worker process died while extracting this unit (a crash in a "
                                        "reader, or killed for memory)", csha[u["local_path"]], code), False)
    if tclose:
        tclose()
    summary = ", ".join(f"{k} {v}" for k, v in counts.items() if v or k in ("extracted", "failed"))
    print(f"done in {time.time() - t0:.0f} s: {summary}" + (f"; report {report}" if report else ""), flush=True)
    sys.exit(1 if counts["failed"] or counts["mismatch"] else 0)


if __name__ == "__main__":
    main()
