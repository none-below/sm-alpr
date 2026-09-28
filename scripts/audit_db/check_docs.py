"""Fail when the live schema has something docs/ does not mention, or when a docs link is dead.

  uv run --locked --project scripts/audit_db python scripts/audit_db/check_docs.py [--audit-dir DIR] [--owners]

Checks, against the built truth.duckdb + derived.duckdb (read-only; catalog functions and small tables only, no scan
of the linking cache or of any row table, so it takes seconds):
  every table, view and macro name; every column of every table and view; every value of the enumerations
  researchers filter on: cell states and divergence labels (from the macro definitions), link tiers (from the tier
  CASE in build_derived.py's layer-3 code), producer_basis and audit (truth.releases, one row per release);
  every relative link in docs/*.md and this directory's README.md: the file exists and a #anchor names a heading
  (GitHub slug rules) or an <a id/name> in it.
A name counts as documented when it appears in docs/*.md in backticks (`name`) or as a table cell (| name |).
Owning doc: truth objects (truth tables and their columns) should be named in truth.md, derived objects (views,
macros, cache tables and their columns) in derived.md. Gaps are listed as warnings; --owners makes them failures.
Exit 1 and list what is missing; exit 0 when complete. Run after changing a build script, before sharing docs.
"""
import argparse
import collections
import re
import sys
from pathlib import Path

import duckdb

from paths import CODE, audit_dir

ap = argparse.ArgumentParser()
ap.add_argument("--audit-dir", help="directory holding the databases (default: paths.audit_dir())")
ap.add_argument("--owners", action="store_true", help="fail (not just warn) when a name is missing from its owning doc")
args = ap.parse_args()
A = Path(args.audit_dir or audit_dir())
D = CODE / "docs"
con = duckdb.connect(str(A / "derived.duckdb"), read_only=True)
con.execute(f"ATTACH IF NOT EXISTS '{A / 'truth.duckdb'}' AS truth (READ_ONLY)")
con.execute("SET threads=1; SET memory_limit='1GB'")   # catalog + small tables only


def documented_in(text):
    names = set(re.findall(r"`([^`\n]+)`", text)) | {c.strip() for c in re.findall(r"\|([^|\n]+)(?=\|)", text)}
    # `truth.releases`, `sightings.t`, `read_field(fld, who)` also document their parts
    for d in list(names):
        names.update(p for p in re.split(r"[.(),\s]+", d) if p)
    return names


docs = {p.name: p.read_text() for p in sorted(D.glob("*.md"))}
documented = documented_in("\n".join(docs.values()))
owner_docs = {k: documented_in(docs.get(k, "")) for k in ("truth.md", "derived.md")}

need, owner = {}, {}  # item -> kind; item -> owning doc
for db, schema, name, kind in con.execute("""
    SELECT database_name, schema_name, table_name, 'table' FROM duckdb_tables() WHERE database_name IN ('truth', 'derived')
    UNION ALL SELECT database_name, schema_name, view_name, 'view' FROM duckdb_views()
      WHERE NOT internal AND database_name IN ('truth', 'derived')""").fetchall():
    doc = "truth.md" if db == "truth" else "derived.md"
    need[name] = f"{kind} {db}.{schema}.{name}"
    owner.setdefault(name, set()).add(doc)
    for (col,) in con.execute("SELECT column_name FROM duckdb_columns() WHERE database_name = ? AND schema_name = ? AND table_name = ?",
                              [db, schema, name]).fetchall():
        need.setdefault(col, f"column of {db}.{schema}.{name}")
        owner.setdefault(col, set()).add(doc)
for (fn,) in con.execute("""SELECT DISTINCT function_name FROM duckdb_functions()
                            WHERE NOT internal AND function_type IN ('macro', 'table_macro')""").fetchall():
    need[fn] = "macro"
    owner.setdefault(fn, set()).add("derived.md")

macro_src = {n: s for n, s in con.execute(
    "SELECT function_name, any_value(macro_definition) FROM duckdb_functions() WHERE NOT internal GROUP BY 1").fetchall()}
for fn in ("cell_state", "divergence"):
    for v in re.findall(r"THEN '([a-z_]+)'", macro_src.get(fn) or ""):
        need.setdefault(v, f"value returned by {fn}()")
# link tiers: the literals the layer-3 tier CASE assigns (no scan of the ~140M-row cache)
src = (Path(__file__).parent / "build_derived.py").read_text()
i, j = src.find("# ---------------- Layer 3"), src.find("# ---------------- Layer 2 again")
tiers = set(re.findall(r"'(\d[a-z0-9]*_[a-z0-9_]+|x_ambiguous)'", src[i:j] if 0 <= i < j else src))
if not tiers:
    print("WARNING: no link-tier literals found in build_derived.py; tier values not checked")
for v in tiers:
    need.setdefault(v, "link tier (cache.sighting_event.basis)")
for col in ("producer_basis", "audit"):
    for (v,) in con.execute(f"SELECT DISTINCT {col} FROM truth.releases WHERE {col} IS NOT NULL").fetchall():
        need.setdefault(v, f"value of truth.releases.{col}")


# ---- links: every relative [text](target) in the docs resolves to a file, and #anchor to a heading in it ----
def anchors(text):
    """GitHub heading slugs (lower-case; drop everything but word chars, '-' and ' '; ' ' -> '-'; repeats get -1, -2)
    plus explicit <a id=...>/<a name=...>."""
    out, seen, fence = set(), collections.Counter(), False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        m = None if fence else re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(1)).replace("`", "")
            s = re.sub(r"[^\w\- ]", "", t.lower()).replace(" ", "-")
            out.add(s if not seen[s] else f"{s}-{seen[s]}")
            seen[s] += 1
    out.update(re.findall(r"<a\s+(?:id|name)=\"([^\"]+)\"", text))
    return out


def links(text):
    fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        for target in re.findall(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", re.sub(r"`[^`]*`", "", line)):
            yield n, target


dead = []
pages = [(p.name, docs[p.name], D) for p in sorted(D.glob("*.md"))]
if (CODE / "README.md").exists():
    pages.append(("../README.md", (CODE / "README.md").read_text(), CODE))
anchor_cache = {}
for name, text, base in pages:
    for n, target in links(text):
        if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I):   # http:, https:, mailto: ...
            continue
        path, _, frag = target.partition("#")
        f = (base / path).resolve() if path else (base / name).resolve()
        if not f.exists():
            dead.append(f"{name}:{n}: {target} (no such file"
                        + ("; generated: run gen_stats.py / gen_coverage.py)" if f.name in ("stats.md", "coverage.md") else ")"))
            continue
        if frag and f.suffix == ".md":
            if f not in anchor_cache:
                anchor_cache[f] = anchors(f.read_text())
            if frag not in anchor_cache[f]:
                dead.append(f"{name}:{n}: {target} (no heading with that anchor in {f.name})")

missing = sorted((k, v) for k, v in need.items() if k not in documented)
not_in_owner = sorted((k, d) for k, ds in owner.items() for d in sorted(ds) if k in documented and k not in owner_docs[d])
fail = bool(missing or dead or (args.owners and not_in_owner))
if missing:
    print(f"{len(missing)} undocumented of {len(need)}:")
    for k, v in missing:
        print(f"  {k!r:40s} {v}")
if dead:
    print(f"{len(dead)} dead links:")
    for d in dead:
        print(f"  {d}")
if not_in_owner:
    print(f"{len(not_in_owner)} names documented elsewhere but not in their owning doc"
          + (":" if args.owners else " (warning; --owners makes this fail):"))
    for k, d in not_in_owner:
        print(f"  {k!r:40s} not named in {d}")
if not fail:
    print(f"docs complete: all {len(need)} schema names and enumeration values are documented; "
          f"{sum(1 for _, t, _ in pages for _ in links(t))} links checked")
sys.exit(1 if fail else 0)
