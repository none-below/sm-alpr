"""TRUTH loader for the CA public-records-portal harvest (NextRequest, GovQA, JustFOIA): every Flock audit table agencies
posted on their own records portals (paths.portals_dir(); fetched by scripts/pra_portal_harvest.py, which writes
MANIFEST.jsonl with each download's sha256 and keeps a dated index per portal search).

Two steps, like the MuckRock loader whose staging it reuses (muckrock_ingest.to_csv):
  stage   profile every tabular download in MANIFEST.jsonl (table_profile.py: which sheets / zip members are audit logs
          and of what kind) and stage each audit table as a CSV whose first column is the row a reader sees
  load    (from build_truth.py) one release per staged table into releases + flock_audit_rows / flock_event_rows

Provenance per release: container_root 'portals', container_path (relative to the portals dir), member / sheet,
container_sha256 (equals the MANIFEST.jsonl sha256, checked), request_url (the request page, from the portal's newest
dated index), source_url (NextRequest: the document's page; GovQA / JustFOIA: the request page, which lists the file),
released_on (NextRequest's upload date), src_row_basis (from staging).
Producer = the Flock org whose log it is: portal_producers.json (authored: per file, per request, then per portal
host, each with its source), else the file name ('<dates>-<Org Name>-[Network-]Audit'), else the dominant Org Name of
the same request's own-search tables. Releases matching an authored exclusion (exclusions.json, or the local
exclusions.local.json next to the databases) are not loaded and are listed in release_exclusions with the reason.

  uv run --locked --project scripts/audit_db python scripts/audit_db/portal_ingest.py stage <portals dir> <tmp dir>
"""
import collections
import csv
import gzip
import json
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from fnmatch import fnmatch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from muckrock_ingest import (EVENT_COLS, FLOCK_COLS, HEADER_BASIS, canonical, extra_json, org_from_name,  # noqa: E402
                             sha256_file, sql_ident, sql_str, to_csv)
from paths import audit_dir  # noqa: E402
from table_profile import TABULAR, profile_file  # noqa: E402

HERE = Path(__file__).parent
PREFIX_RE = re.compile(r"^(\d+|ctl\d+|[0-9a-f]{8}-[0-9a-f]{3})__")   # NextRequest doc id, GovQA slot, JustFOIA GUID head
AUDIT_OF_KIND = {"network_audit": "network", "search_audit_own": "own", "event_log": "event"}


def shown_name(file_name):
    """The document's name as the portal shows it (the harvester prefixes a per-request id)."""
    return PREFIX_RE.sub("", file_name)


def request_meta(PV):
    """(platform, host, request) -> {url, uploaded: {doc id: date}} from each portal's newest dated index."""
    out = {}
    for idx in PV.glob("*/*/index.json"):
        try:
            j = json.loads(idx.read_text())
        except (OSError, ValueError):
            continue
        for r in j.get("requests") or []:
            docs = {str(d.get("id")): (d.get("uploaded") or "")[:10] or None for d in r.get("documents") or []}
            out[(idx.parent.parent.name, idx.parent.name, str(r.get("id")))] = {"url": r.get("url"), "uploaded": docs}
    return out


def manifest_files(PV):
    """Unique downloaded files from MANIFEST.jsonl (append-only; a path appears once)."""
    seen, out = set(), []
    for line in (PV / "MANIFEST.jsonl").read_text().splitlines():
        m = json.loads(line)
        if m["path"] in seen:
            continue
        seen.add(m["path"])
        out.append(m)
    return out


def _profile_and_stage(args):
    m, PV, TMP = args
    PV, TMP = Path(PV), Path(TMP)
    profs, err = profile_file(PV / m["path"], m["title"] or Path(m["path"]).name)
    err = err and f"{m['path']}: {err}"
    units = []
    for p in profs:
        u = {"local_path": m["path"], "member": p["member"], "sheet": p["sheet"][:40] if p["sheet"] else None,
             "name": p["member"] or Path(m["path"]).name, "kind": p["kind"], "n_orgs": p["n_orgs"],
             "top_org": p["top_org"], "platform": m["platform"], "host": m["host"], "request": m["request_id"],
             "doc_id": str(m["doc_id"]), "manifest_sha256": m["sha256"]}
        try:
            res = to_csv(u, PV, TMP) or "no header row found"
        except Exception as ex:  # noqa: BLE001
            res = f"{type(ex).__name__}: {str(ex)[:80]}"
        units.append({"u": u, "res": res})
    return units, err


def stage(PV, TMP, workers=2):
    PV, TMP = Path(PV), Path(TMP) / "portal"
    files = [m for m in manifest_files(PV) if Path(m["path"]).suffix.lower() in TABULAR | {".zip"}]
    staged, errs = [], []
    with ProcessPoolExecutor(workers) as ex:
        for units, err in ex.map(_profile_and_stage, [(m, str(PV), str(TMP)) for m in files], chunksize=2):
            staged += units
            if err:
                errs.append(err)
    (TMP / "muckrock_units").mkdir(parents=True, exist_ok=True)
    json.dump({"units": staged, "profile_errors": errs}, open(TMP / "staged.json", "w"))
    ok = sum(1 for s in staged if not isinstance(s["res"], str))
    print(f"portal: {len(files)} tabular files, {len(staged)} audit tables, {ok} staged; {len(errs)} unreadable files",
          flush=True)


def release_id(u):
    return (f"pp:{u['platform']}:{u['host']}:{u['request']}:{Path(u['local_path']).name}"
            + (f"!{u['member']}" if u["member"] else "") + (f"#{u['sheet']}" if u["sheet"] else ""))


def load_exclusions(con):
    """Authored exclusions -> release_exclusions table (created once); returns [(pattern, reason, source)].
    Two files, same format: exclusions.json (in git) and exclusions.local.json next to the databases (paths.audit_dir(),
    not in git), for exclusions whose reasons are review notes on a specific release."""
    con.execute("CREATE TABLE IF NOT EXISTS release_exclusions (release_id VARCHAR, reason VARCHAR, source VARCHAR)")
    out = []
    for f in (HERE / "exclusions.json", audit_dir() / "exclusions.local.json"):
        if f.exists():
            out += [(e["release_pattern"], e["reason"], e["source"]) for e in json.load(open(f))]
    return out


def excluded(rid, rules):
    return next(((r, s) for p, r, s in rules if fnmatch(rid, p)), None)


def producer_for(u, prod, own_orgs, resolve=None):
    """(org, basis, source): portal_producers.json by file or request; else the file name; else the own-search org;
    else portal_producers.json by host. An own-search Org Name the registry does not know (Santa Rosa's own exports
    say 'SRPD') gives way to the host's agency when the host has one."""
    req_key = f"{u['host']}:{u['request']}"
    for scope, key in (("files", f"{req_key}:{Path(u['local_path']).name}"), ("requests", req_key), ("hosts", u["host"])):
        e = (prod.get(scope) or {}).get(key)
        if e and e.get("org") and scope != "hosts":
            return e["org"], f"portal_producers.json ({scope[:-1]})", e.get("source")
    org = org_from_name(shown_name(u["name"]))
    if org:
        return org, "filename", None
    e = (prod.get("hosts") or {}).get(u["host"])
    if own_orgs.get(req_key):
        org = own_orgs[req_key].most_common(1)[0][0]
        if resolve is None or resolve(org) or not (e and e.get("org")):
            return org, "own-search dominant Org Name", None
    if e and e.get("org"):
        return e["org"], "portal_producers.json (host)", e.get("source")
    return None, None, None


def add_portals(con, PV, TMP, resolve=lambda org: None, log=lambda m: print(m, flush=True)):
    PV, TMP = Path(PV), Path(TMP) / "portal"
    manifest = TMP / "staged.json"
    if not manifest.exists():
        raise SystemExit(f"run the staging step first: uv run --locked --project scripts/audit_db python "
                         f"scripts/audit_db/portal_ingest.py stage {PV} {TMP.parent}")
    staged = json.load(open(manifest))["units"]
    prod = json.load(open(HERE / "portal_producers.json"))
    rules = load_exclusions(con)
    meta = request_meta(PV)
    con.execute("CREATE TABLE IF NOT EXISTS flock_event_rows (release_id VARCHAR, row_no BIGINT, src_row BIGINT, "
                + ", ".join(f'"{c}" VARCHAR' for c in EVENT_COLS) + ", extra JSON)")
    own_orgs = collections.defaultdict(collections.Counter)
    for s in staged:
        u = s["u"]
        if u["kind"] == "search_audit_own" and u.get("top_org"):
            own_orgs[f"{u['host']}:{u['request']}"][u["top_org"]] += 1
    container_sha, loaded, n_excl, unresolved = {}, 0, 0, set()
    for s in staged:
        u, res = s["u"], s["res"]
        if isinstance(res, str):
            log(f"  skip {u['host']} {u['request']} {u['name'][:60]}: {res}")
            continue
        rid = release_id(u)
        ex = excluded(rid, rules)
        if ex:
            con.execute("INSERT INTO release_exclusions VALUES (?,?,?)", [rid, *ex])
            n_excl += 1
            continue
        org, basis, source = producer_for(u, prod, own_orgs, resolve)
        if not org:
            unresolved.add(u["host"])
            org, basis = u["host"], "portal host (no producer known)"
        header, raw_header = canonical(res["header"]), res["header"]
        audit = AUDIT_OF_KIND[u["kind"]]
        names = "[" + ", ".join(sql_str(c) for c in ["__src_row"] + header) + "]"
        src = f"read_csv({sql_str(res['path'])}, all_varchar=true, header=true, names={names}, null_padding=true, parallel=false)"
        cols = EVENT_COLS if audit == "event" else FLOCK_COLS
        sel = ", ".join(sql_ident(c) if c in header else f"NULL AS {sql_ident(c)}" for c in cols)
        ext = extra_json([h for h in header if h not in cols])
        table = "flock_event_rows" if audit == "event" else "flock_audit_rows"
        try:
            con.execute(f"INSERT INTO {table} SELECT ?, row_number() OVER (), TRY_CAST(\"__src_row\" AS BIGINT), {sel}, {ext} FROM {src}", [rid])
        except Exception as e:  # noqa: BLE001
            log(f"  load error {rid[:80]}: {str(e)[:160]}")
            continue
        n = con.execute(f"SELECT count(*) FROM {table} WHERE release_id = ?", [rid]).fetchone()[0]
        if u["local_path"] not in container_sha:
            sha = sha256_file(PV / u["local_path"])
            if sha != u["manifest_sha256"]:
                raise SystemExit(f"{u['local_path']}: sha256 differs from MANIFEST.jsonl (file changed after download?)")
            container_sha[u["local_path"]] = sha
        rm = meta.get((u["platform"], u["host"], u["request"]), {})
        uploaded = rm.get("uploaded", {}).get(u["doc_id"])
        source_url = (f"https://{u['host']}/documents/{u['doc_id']}" if u["platform"] == "nextrequest" and u["doc_id"].isdigit()
                      else rm.get("url"))
        con.execute("""INSERT INTO releases (release_id, producer, producer_agency_id, producer_basis, producer_source, audit, pra_id,
                         container_root, container_path, member, member_sha256, sheet, source_file, container_sha256, header,
                         header_raw, header_basis, n_rows, content_sha256, released_on, released_on_basis, source_url,
                         request_url, src_row_basis)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,TRY_CAST(? AS DATE),?,?,?,?)""",
                    [rid, org, resolve(org), basis, source, audit, f"{u['host']}:{u['request']}", "portals", u["local_path"],
                     res["member"], res.get("member_sha256"), res["sheet"],
                     shown_name(Path(res["member"] or u["local_path"]).name), container_sha[u["local_path"]], header,
                     raw_header, HEADER_BASIS, n, res["content_sha256"], uploaded,
                     "portal upload date" if uploaded else None, source_url, rm.get("url"), res["src_row_basis"]])
        loaded += 1
    log(f"  portals: {loaded} releases loaded, {n_excl} excluded (release_exclusions)"
        + (f"; no producer for hosts {sorted(unresolved)}" if unresolved else ""))


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "stage":
        stage(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit(__doc__)
