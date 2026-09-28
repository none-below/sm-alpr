"""Regenerate <audit dir>/docs/stats.md (local, not in git): corpus-wide numbers, computed once per build (never
hand-edited).

  nice -n 19 taskpolicy -b uv run --locked --project scripts/audit_db python scripts/audit_db/gen_stats.py [--audit-dir DIR]

Heavy (full scans of sightings, sightings_public, flock_rows and the linking cache): run it after a rebuild, not while
you work. Limits: AUDIT_DB_THREADS (4), AUDIT_DB_MEMORY (6GB); spills go to this process's own subdirectory of AUDIT_DB_TEMP
(default <system tmp>/alpr_duck_tmp) and are capped at AUDIT_DB_MAX_TEMP (12GiB), so a section that would need more fails and is reported as
not computed instead of filling the disk. Exit 1 if any section failed or the pre-export check could not be reported.
The plate-token key is needed for the pre-export check (sightings_public errors without it); it is never printed.
Sections:
  link tiers · linking precision against Flock UUIDs · cell states by field (redaction census) · marker strings ·
  Org Name cells · search types · own-search vs network-audit overlap · searches by number of logs ·
  pre-export plate-residue check (reported only when sightings_public has exactly as many rows as sightings)
"""
import argparse
import collections
import os
import re
import sys
import time
from pathlib import Path

import duckdb

from paths import audit_dir, duck_temp

ap = argparse.ArgumentParser()
ap.add_argument("--audit-dir", help="directory holding the databases (default: paths.audit_dir())")
A = Path(ap.parse_args().audit_dir or audit_dir())
con = duckdb.connect(str(A / "derived.duckdb"), read_only=True)
con.execute(f"ATTACH IF NOT EXISTS '{A / 'truth.duckdb'}' AS truth (READ_ONLY)")
env = os.environ.get
con.execute(f"SET threads={env('AUDIT_DB_THREADS', '4')}; SET memory_limit='{env('AUDIT_DB_MEMORY', '6GB')}'; "
            f"SET temp_directory='{duck_temp(env('AUDIT_DB_TEMP'))}'; "
            f"SET max_temp_directory_size='{env('AUDIT_DB_MAX_TEMP', '12GiB')}'; SET preserve_insertion_order=false")
q = lambda s, p=None: con.execute(s, p or []).fetchall()
built = dict(q("SELECT key, value FROM truth.build_info"))
parts, failed, T0 = {}, [], time.time()   # section title -> markdown lines (written in ORDER, whatever the run order)
out = []
MASKED = ("redacted_flock", "redacted_agency")
lit = lambda xs: ", ".join("'" + x.replace("'", "''") + "'" for x in xs)
fmt_default = lambda v: f"{v:,}" if isinstance(v, int) else ("–" if v is None else str(v))
pct = lambda v: f"{v:.3%}" if v is not None else "–"


def table(title, note, header, rows, fmt=None):
    out.extend([f"## {title}", "", note, "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)])
    for r in rows:
        out.append("| " + " | ".join((fmt or {}).get(i, fmt_default)(v) for i, v in enumerate(r)) + " |")
    out.append("")


def section(title, fn):
    """Run one section; on error write 'not computed' (error kind only: a DuckDB message can quote a cell value)."""
    global out
    t, out = time.time(), parts.setdefault(title, [])
    try:
        fn()
    except Exception as ex:  # noqa: BLE001
        msg = str(ex)
        kind = ("plate token key missing or invalid" if "plate token key" in msg else
                "memory / temp-space cap reached" if re.search(r"out of memory|memory_limit|temp.*(director|size)", msg, re.I)
                else type(ex).__name__)
        print(f"  {title}: {type(ex).__name__}: {msg[:500]}", file=sys.stderr)
        failed.append(title)
        out.extend([f"## {title}", "", f"**Not computed in this run** ({kind}). Re-run gen_stats.py after fixing it.", ""])
    print(f"{time.time() - t:7.1f}s {title}", flush=True)


def link_tiers():
    rows = q("SELECT basis, count(*) FROM cache.sighting_event GROUP BY 1 ORDER BY 1")
    total = sum(n for _, n in rows)
    none = [("(no cache row: no UUID, and no parsed time or org)", N_SIGHTINGS[0] - total)] if N_SIGHTINGS else []
    table("Link tiers", "Sightings by the rule that assigned their event (see linking.md). From `cache.sighting_event`; "
          "sightings with nothing to link on have no cache row and are counted last.",
          ["Tier", "Sightings"], rows + [("total in the cache", total)] + none)


def precision():
    # precision of the fallback keys, measured on sightings that carry a Flock UUID (the ground truth)
    prec = q("""
      WITH u AS (SELECT flock_id, k3, k5 FROM cache.sighting_keys WHERE flock_id IS NOT NULL),
      k3 AS (SELECT k3, count(DISTINCT flock_id) n FROM u WHERE k3 IS NOT NULL GROUP BY 1),
      k5 AS (SELECT k5, count(DISTINCT flock_id) n FROM u WHERE k5 IS NOT NULL GROUP BY 1),
      s3 AS (SELECT flock_id, count(DISTINCT k3) n FROM u WHERE k3 IS NOT NULL GROUP BY 1),
      s5 AS (SELECT flock_id, count(DISTINCT k5) n FROM u WHERE k5 IS NOT NULL GROUP BY 1)
      SELECT 'k3 = hash(org, t, nets)', (SELECT count(*) FROM k3), (SELECT avg((n = 1)::INT) FROM k3), (SELECT avg((n = 1)::INT) FROM s3)
      UNION ALL
      SELECT 'k5 = k3 + time frame', (SELECT count(*) FROM k5), (SELECT avg((n = 1)::INT) FROM k5), (SELECT avg((n = 1)::INT) FROM s5)""")
    table("Linking precision",
          "Measured on sightings that carry a Flock search UUID. *Key precision* = share of distinct keys that belong to exactly one "
          "UUID (a key shared by two UUIDs would wrongly merge two searches; such keys are left `x_ambiguous`, not linked). "
          "*UUID coherence* = share of UUIDs whose sightings all produce the same key (otherwise one search is split).",
          ["Key", "Distinct keys", "Key precision", "UUID coherence"], prec, {2: pct, 3: pct})


N_SIGHTINGS = []   # set by the sightings scan; the pre-export check compares sightings_public against it


def sightings_census():
    # ONE pass over sightings for three tables: cell states + redaction markers (by producer), and search types
    rows = q(f"""
      SELECT GROUPING(search_type) AS by_state, producer, search_type, reason_state, case_state, name_state, plate_state,
             rm, cm, nm, pm, count(*) AS n
      FROM (SELECT producer, coalesce(search_type, '(none)') AS search_type, reason_state, case_state, name_state, plate_state,
                   CASE WHEN reason_state IN ({lit(MASKED)}) THEN reason_surface END AS rm,
                   CASE WHEN case_state IN ({lit(MASKED)}) THEN case_surface END AS cm,
                   CASE WHEN name_state IN ({lit(MASKED)}) THEN name_surface END AS nm,
                   CASE WHEN plate_state IN ({lit(MASKED)}) THEN plate_surface END AS pm
            FROM sightings)
      GROUP BY GROUPING SETS ((producer, reason_state, case_state, name_state, plate_state, rm, cm, nm, pm), (search_type))""")
    states, markers, prods, types = collections.Counter(), collections.Counter(), collections.defaultdict(set), []
    for by_state, producer, st, rs, cs, ns, ps, rm, cm, nm, pm, n in rows:
        if not by_state:
            types.append((st, n))
            continue
        for f, s, m in (("reason", rs, rm), ("case", cs, cm), ("name", ns, nm), ("plate", ps, pm)):
            states[(f, s)] += n
            if m is not None:
                markers[(f, m)] += n
                prods[(f, m)].add(producer)
    N_SIGHTINGS.append(sum(r[-1] for r in rows if r[0]))
    table("Cell states by field",
          f"How every cell was classified (see semantics.md for each state), over all {N_SIGHTINGS[0]:,} sightings. From `sightings`.",
          ["Field", "State", "Cells"], sorted(((f, s, n) for (f, s), n in states.items()), key=lambda r: (r[0], -r[2])))
    table("Redaction marker strings", "Exact released strings classified as redactions (`redacted_flock`, `redacted_agency`), by field.",
          ["Field", "Marker", "Cells", "Producers"],
          sorted(((f, m, n, len(prods[(f, m)])) for (f, m), n in markers.items()), key=lambda r: (r[0], -r[2], r[1])))
    # only word-shaped values are printed: in a misaligned release this column can hold other cells' text (plates)
    shown = sorted(((t, n) for t, n in types if re.fullmatch(r"\(none\)|[A-Za-z][A-Za-z -]{0,40}", t)), key=lambda r: (-r[1], r[0]))
    rest = [n for t, n in types if not re.fullmatch(r"\(none\)|[A-Za-z][A-Za-z -]{0,40}", t)] + [n for _, n in shown[25:]]
    table("Search types", "`sightings.search_type` values (rows, not searches): the 25 most frequent word-shaped values, "
          "then every other value together (values with digits or punctuation are never printed).", ["Search type", "Sightings"],
          shown[:25] + ([(f"(other {len(rest):,} values)", sum(rest))] if rest else []))


def org_cells():
    # Org Name is not a sightings column (org falls back to the producer): classify the released cell itself
    rows = q(f"""
      WITH c AS (SELECT producer, trim("Org Name") AS v,
                        cell_state("Org Name", "Org Name_h", "Org Name_w", false, false) AS st FROM flock_rows)
      SELECT st, CASE WHEN st IN ({lit(MASKED + ('partial',))}) THEN v END AS marker, count(*), count(DISTINCT producer)
      FROM c GROUP BY 1, 2 ORDER BY 1, 3 DESC""")
    by_state = collections.Counter()
    for st, _, n, _ in rows:
        by_state[st] += n
    table("Org Name cells",
          "Cell states of the released Org Name cell over `flock_rows` (Flock-format logs; the SMPD PDFs have no Org Name "
          "column). A masked or partial Org Name is listed with its exact string (agency names, not civilian data).",
          ["State", "Marker", "Cells", "Producers"],
          [(st, "", n, "") for st, n in sorted(by_state.items(), key=lambda r: -r[1])]
          + [(st, m, n, p) for st, m, n, p in rows if m is not None])


def own_vs_network():
    both = [p for (p,) in q("""SELECT producer FROM truth.releases WHERE audit = 'own'
                               INTERSECT SELECT producer FROM truth.releases WHERE audit = 'network' ORDER BY 1""")]
    rel = q(f"SELECT release_id, audit FROM truth.releases WHERE producer IN ({lit(both)}) AND audit IN ('own', 'network')") if both else []
    own_r, net_r = [r for r, a in rel if a == "own"], [r for r, a in rel if a == "network"]
    if not (own_r and net_r):
        table("Own-search log vs network audit", "No producer released both logs.", ["Producer"], [])
        return
    # literal release lists so every scan prunes to these producers' releases
    con.execute(f"""CREATE OR REPLACE TEMP TABLE span AS SELECT s.release_id, s.producer, min(s.t) AS t0, max(s.t) AS t1
                    FROM sightings s WHERE s.release_id IN ({lit(net_r)}) AND s.t IS NOT NULL GROUP BY 1, 2""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE own AS SELECT s.release_id, s.row_no, s.producer,
                      EXISTS (SELECT 1 FROM span WHERE span.producer = s.producer AND s.t BETWEEN span.t0 AND span.t1) AS covered
                    FROM sightings s WHERE s.release_id IN ({lit(own_r)})""")
    rows = q(f"""
      WITH se AS (SELECT release_id, row_no, event_key, basis FROM cache.sighting_event WHERE release_id IN ({lit(own_r)})),
      net AS (SELECT DISTINCT producer, event_key FROM cache.sighting_event WHERE release_id IN ({lit(net_r)})),
      o AS (SELECT own.producer, own.covered, se.event_key, se.basis FROM own LEFT JOIN se USING (release_id, row_no))
      SELECT o.producer, count(*) AS own_rows, count(*) FILTER (WHERE covered) AS in_span,
             count(*) FILTER (WHERE covered AND basis = 'x_ambiguous') AS x_amb,
             count(DISTINCT o.event_key) FILTER (WHERE covered AND basis <> 'x_ambiguous') AS searches,
             count(DISTINCT o.event_key) FILTER (WHERE covered AND basis <> 'x_ambiguous' AND net.event_key IS NOT NULL) AS in_net
      FROM o LEFT JOIN net ON net.producer = o.producer AND net.event_key = o.event_key
      GROUP BY 1 ORDER BY 5 DESC, 1""")
    table("Own-search log vs network audit",
          "For producers that released both: of the searches in their own-search log that fall inside the time span of one of "
          "their network-audit releases (min to max search time of that release), the share that also appears in their network "
          "audit (matched by event). Own rows outside every network span are left out (the two productions cover different "
          "periods), and so are `x_ambiguous` own rows, which can never match (counted). Searches = distinct events, so "
          "re-released rows count once. Below 100% means own searches that did not touch the producer's own cameras or were "
          "not linked (semantics.md, linking.md).",
          ["Producer", "Own-log rows", "In a network span", "x_ambiguous (left out)", "Own searches in span",
           "Also in network audit", "Share"],
          [(p, a, b, c, d, e, e / d if d else None) for p, a, b, c, d, e in rows], {6: pct})


def logs_per_search():
    # distinct (event, producer) pairs, then producers per event: two plain GROUP BYs over the cache (spill-capped)
    rows = q("""WITH p AS (SELECT event_key, producer FROM cache.sighting_event GROUP BY ALL),
                e AS (SELECT event_key, count(*) AS n_logs FROM p GROUP BY 1)
                SELECT n_logs, count(*) FROM e GROUP BY 1 ORDER BY 1""")
    table("Searches by number of logs",
          "How many distinct producers' logs recorded each search (`events.n_logs`; grouped by `event_key`, the stored hash of "
          "`event_id`).", ["Logs", "Searches"], rows + [("total", sum(n for _, n in rows))])


def residue():
    # counts only, values never printed; tokens are removed before looking for plate shapes, so a token's hex digits
    # cannot look like a plate. Shapes = the tokenizer's tier A (9AAA999, any case, leading 0 allowed).
    tok, word = r"\[p1_[0-9a-f]{16}\]", r"(?i)\b[0-9][A-Z]{3}[0-9]{3}\b"
    glued = r"(?i)(?:^|[^a-z0-9]|[a-z])[0-9][a-z]{3}[0-9]{3}(?:$|[^a-z0-9])"   # standalone or glued onto a tag, never cut out of a longer run
    strip = lambda c: f"regexp_replace({c}, '{tok}', ' ', 'g')"
    r = q(f"""
      SELECT count(*),
        count(*) FILTER (WHERE regexp_matches({strip('reason')}, '{word}')),
        count(*) FILTER (WHERE regexp_matches({strip('case_no')}, '{word}')),
        count(*) FILTER (WHERE regexp_matches({strip('text_prompt')}, '{word}')),
        count(*) FILTER (WHERE regexp_matches({strip('filters')}, '{word}')),
        count(*) FILTER (WHERE regexp_matches({strip('filters')}, '{glued}')),
        count(*) FILTER (WHERE plate_state = 'value' AND NOT regexp_full_match(plate, 'p1_[0-9a-f]{{16}}')),
        count(*) FILTER (WHERE regexp_matches({strip('search_type')}, '{glued}')),
        count(*) FILTER (WHERE regexp_matches({strip('searcher_name')}, '{word}')),
        count(*) FILTER (WHERE plate IS NOT NULL AND NOT regexp_full_match(plate, 'p1_[0-9a-f]{{16}}')
                           AND NOT (trim(plate) IN ('', '***') OR agency_mask(plate) OR exemption_cite(plate)))
      FROM sightings_public""")[0]
    n_public = r[0]
    n_all = N_SIGHTINGS[0] if N_SIGHTINGS else q("SELECT count(*) FROM sightings")[0][0]
    if n_public != n_all or n_all == 0:
        failed.append("pre-export check")
        out.extend(["## Pre-export check: plate residue in `sightings_public`", "",
                    f"**Not reported:** `sightings_public` returned {n_public:,} rows but `sightings` has {n_all:,}. Residue "
                    "counts over a view that lost rows would read as a false pass (a missing key used to empty the view).", ""])
        return
    table("Pre-export check: plate residue in `sightings_public`",
          f"Over all {n_public:,} rows of `sightings_public` (= `sightings`, so no row was lost). Cells still holding a California "
          "standard plate shape (9AAA999, any case, leading 0 allowed) after the `[p1_…]` tokens are removed, and plate cells "
          "that are not tokens. Filters are tokenized without word boundaries (plates glued onto tags), so they are also "
          "counted glued. Every count should be 0; counts only, values are never printed. Per-release checks: pii.md.",
          ["Column", "Check", "Cells"],
          [("reason", "9AAA999 as a word", r[1]), ("case_no", "9AAA999 as a word", r[2]),
           ("text_prompt", "9AAA999 as a word", r[3]), ("filters", "9AAA999 as a word", r[4]),
           ("filters", "9AAA999 anywhere (glued)", r[5]), ("plate", "`value` cell not a token", r[6]),
           ("plate", "not a token, empty or a known mask (raw text passed through)", r[9]),
           ("search_type", "9AAA999 anywhere", r[7]), ("searcher_name", "9AAA999 as a word", r[8])])
    if any(r[1:]):
        failed.append("pre-export check (residue found)")


ORDER = [("Link tiers", link_tiers), ("Linking precision", precision), ("Cell states by field", sightings_census),
         ("Org Name cells", org_cells), ("Own-search log vs network audit", own_vs_network),
         ("Searches by number of logs", logs_per_search), ("Pre-export check: plate residue in `sightings_public`", residue)]
for title, fn in [ORDER[2]] + ORDER[:2] + ORDER[3:]:   # the sightings scan first: link tiers and the residue check use its count
    section(title, fn)

head = ["# Corpus statistics", "",
        f"Generated by `gen_stats.py` from the databases built {built.get('built_at_utc')} (repo inputs at "
        f"`{built.get('repo_commit', '')[:9]}`, DuckDB {duckdb.__version__}) in {time.time() - T0:.0f} s. Do not edit by hand; "
        "re-run after a rebuild." + (f" **Incomplete run:** {', '.join(failed)}." if failed else ""), ""]
(A / "docs").mkdir(exist_ok=True)
(A / "docs" / "stats.md").write_text("\n".join(head + [x for t, _ in ORDER for x in parts.get(t, [])]) + "\n")
print(f"wrote {A / 'docs' / 'stats.md'} in {time.time() - T0:.0f} s" + (f"; FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
