"""Layers 2 + 3 — DERIVED: views/macros computed on read over truth (attached READ_ONLY), plus a `cache`
schema holding only what is too slow to compute on read. Deleting derived.duckdb loses nothing.

  cd <audit dir> && uv run --locked --project <repo>/scripts/audit_db python <repo>/scripts/audit_db/build_derived.py \
      truth.duckdb derived.duckdb [--views-only]
Every cache table is registered in cache.builds with the truth fingerprint, this script's layer-3 sha256 and the DuckDB
version, so a stale cache is detectable (check_cache.py) rather than silently trusted.
The parse/citation SQL shared with row lookups lives in sql_templates.py (importable without running a build).
"""
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
import cache_fingerprint  # noqa: E402
from sql_templates import (flock_rows_sql, sightings_flock_sql, sightings_smpd_sql, sources_flock_sql,  # noqa: E402
                           sources_smpd_sql)

TRUTH, DERIVED = sys.argv[1], sys.argv[2]
LIMITS = (f"SET threads={os.environ.get('AUDIT_DB_THREADS', '4')}; SET memory_limit='{os.environ.get('AUDIT_DB_MEMORY', '6GB')}'; "
          "SET preserve_insertion_order=false")
con = duckdb.connect(DERIVED)
# modest defaults so the laptop stays usable; raise with AUDIT_DB_THREADS / AUDIT_DB_MEMORY for a faster build
con.execute(LIMITS)
con.execute(f"ATTACH IF NOT EXISTS '{TRUTH}' AS truth (READ_ONLY)")
t0 = time.time()
def step(m):
    print(f"{time.time() - t0:6.1f}s {m}", flush=True)

# Exemption citation typed into a cell (see exemption_cite): one or more of LABEL? SEC? NUMBER SUBDIV* LABEL?, where a
# CPRA 79xx.xxx / Civ. Code 1798.90.x number stands alone and a pre-2023 CPRA 62xx section needs a code label.
_LABEL = r"(?:c?gc|g\.\s?c\.?|(?:cal(?:ifornia|\.)?\s*)?(?:gov(?:ernment|\x27t|’t|t|\.)?|civ(?:il|\.)?)\s*code)"   # \x27 = an apostrophe (the regex is spliced into a SQL string)
_SEC = r"(?:(?:§{1,2}|sec(?:tions?|s|\.)?)\s*)?"
_SUB = r"(?:\s*\([a-z0-9]+\))*"
_CITE = (rf"(?:(?:{_LABEL}\s*,?\s*)?{_SEC}(?:79[0-9]{{2}}\.[0-9]{{3}}|1798\.90\.[0-9]{{1,2}}){_SUB}(?:\s*,?\s*{_LABEL})?"
         rf"|{_LABEL}\s*,?\s*{_SEC}62[0-9]{{2}}(?:\.[0-9]+)?{_SUB}|{_SEC}62[0-9]{{2}}(?:\.[0-9]+)?{_SUB}\s*,?\s*{_LABEL})")
EXEMPTION_RE = rf"(?i){_CITE}(?:\s*(?:,|;|/|&|\+|and)\s*{_CITE})*\.?"

# ---------------- Layer 2: computed on read -------------------------------------------------------
con.execute(r"""
-- Flock export text ('09/14/2025, 3:04:05 PM UTC'), or a spreadsheet datetime as staged ('2025-09-14 15:04:05').
-- Every format observed in the corpus is UTC; values are naive TIMESTAMPs meaning UTC.
CREATE OR REPLACE MACRO flock_ts(s) AS
  try_strptime(regexp_replace(trim(replace(s, chr(13), '')), '\s*UTC$', ''),
               ['%m/%d/%Y, %I:%M:%S %p', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f']);
-- Time Frame bound i (1 = start, 2 = end): two lines ('<start> UTC\n<end> UTC'), or Lodi's one line '<start> UTC to <end> UTC'
CREATE OR REPLACE MACRO tf_bound(s, i) AS flock_ts(regexp_split_to_array(s, '\n|\s+to\s+')[i]);
CREATE OR REPLACE MACRO flock_int(s) AS TRY_CAST(TRY_CAST(s AS DOUBLE) AS INTEGER);
-- ISO 8601 with a zone ('...T10:00:00Z', '...+07:00') -> UTC; a cast to TIMESTAMP would drop the offset unconverted.
-- epoch_us of a TIMESTAMPTZ is the instant, independent of the session TimeZone.
CREATE OR REPLACE MACRO iso_ts_utc(s) AS CASE
  WHEN regexp_matches(trim(s), '[0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?\s*(Z|z|[+-][0-9]{2}(:?[0-9]{2})?)$')
  THEN make_timestamp(epoch_us(TRY_CAST(trim(s) AS TIMESTAMPTZ)))
  ELSE TRY_CAST(trim(s) AS TIMESTAMP) END;
-- agency masks seen in the corpus: REDACTED / [REDACTED], '###' (Santa Rosa), '* * *', block glyphs
CREATE OR REPLACE MACRO agency_mask(raw) AS
  upper(trim(raw)) IN ('REDACTED', '[REDACTED]') OR regexp_full_match(trim(raw), '#{2,}|\*( \*)+|[█■]+');
-- an exemption citation typed in place of the value: the agency withheld it ('7923.600 GC' in 2,056 of Port Hueneme's
-- License Plate cells; 'GC 7923.600', 'Gov. Code § 7923.600(a)', '§ 7922.000', 'Civ. Code 1798.90.55', 'GC 6254(f)').
-- Whole cell only, one or more citations; CPRA 79xx.xxx / Civ. 1798.90.x need no label (a value never has that shape),
-- pre-2023 CPRA 62xx sections need a code label.
CREATE OR REPLACE MACRO exemption_cite(raw) AS regexp_full_match(trim(raw), '__EXEMPTION_RE__');
CREATE OR REPLACE MACRO cell_state(raw, in_header, withheld, partial_ok, placeholder_ok) AS CASE
  WHEN NOT in_header AND withheld THEN 'withheld'
  WHEN NOT in_header THEN 'not_exported'
  WHEN raw IS NULL OR trim(raw) = '' THEN CASE WHEN withheld THEN 'withheld' ELSE 'empty' END
  WHEN trim(raw) = '***' THEN 'redacted_flock'
  WHEN agency_mask(raw) OR exemption_cite(raw) THEN 'redacted_agency'
  -- part masked, part left: 'REDACTED / 459 SUS', 'REDACTED YOLO COUNTY S.O.'
  WHEN regexp_matches(upper(trim(raw)), '^\[?REDACTED\]?[^A-Z0-9]*[A-Z0-9]') THEN 'partial'
  -- Redwood City's searcher names: initial + fragment ('A. Bcd', and 'a. Bcd' with a lower-case initial)
  WHEN partial_ok AND regexp_full_match(trim(raw), '[A-Za-z]?\. ?[A-Za-z''’]{1,3}') THEN 'partial'
  -- junk entries: listed words, or no letter/digit at all ('#', '..', '/', mojibake)
  WHEN placeholder_ok AND (lower(trim(raw)) IN ('none','n/a','na','-','--','xxx','x','*','.','0','test','null')
                           OR NOT regexp_matches(raw, '[A-Za-z0-9]')) THEN 'placeholder'
  ELSE 'value' END;
CREATE OR REPLACE MACRO clean_value(raw, st) AS CASE WHEN st = 'value' THEN trim(raw) END;

-- per release x field: in header? withheld per an authored disposition?
CREATE OR REPLACE VIEW release_fields AS
  SELECT r.release_id, r.producer, r.audit, f.field,
         list_contains(r.header, f.field) AS in_header,
         EXISTS (SELECT 1 FROM truth.release_dispositions d
                 WHERE r.release_id LIKE d.release_pattern AND d.field = f.field AND d.disposition = 'withheld_blanked') AS withheld
  FROM truth.releases r, (SELECT unnest(['ID','Name','Org Name','License Plate','Reason','Case #','Time Frame']) AS field) f;

-- release_meta: one row per release with what row parsing needs (header / withheld flags per field, layout
-- corrections as a list, latest-starting range first so where ranges overlap the more specific one applies), so each
-- truth row takes a single equi-join (keeps row lookups prunable).
CREATE OR REPLACE MACRO is_withheld(rid, fld) AS EXISTS (SELECT 1 FROM truth.release_dispositions d
  WHERE rid LIKE d.release_pattern AND d.field = fld AND d.disposition = 'withheld_blanked');
CREATE OR REPLACE VIEW release_meta AS
SELECT r.release_id, r.producer, r.audit,
  list_contains(r.header, 'Org Name') AS "Org Name_h", is_withheld(r.release_id, 'Org Name') AS "Org Name_w",
  list_contains(r.header, 'Reason') AS "Reason_h", is_withheld(r.release_id, 'Reason') AS "Reason_w",
  list_contains(r.header, 'Case #') AS "Case #_h", is_withheld(r.release_id, 'Case #') AS "Case #_w",
  list_contains(r.header, 'Name') AS "Name_h", is_withheld(r.release_id, 'Name') AS "Name_w",
  list_contains(r.header, 'License Plate') AS "License Plate_h", is_withheld(r.release_id, 'License Plate') AS "License Plate_w",
  (SELECT list({'src_row_from': l.src_row_from, 'src_row_to': l.src_row_to, 'mapping': l.mapping}
               ORDER BY l.src_row_from DESC, l.src_row_to NULLS LAST, l.release_pattern)
   FROM truth.release_layouts l WHERE r.release_id LIKE l.release_pattern) AS layouts
FROM truth.releases r;

-- ---- provenance: from any release back to the released original --------------------------------------------------
CREATE OR REPLACE MACRO basename(p) AS regexp_extract(p, '[^/\\]+$');
CREATE OR REPLACE MACRO url_path(p) AS replace(replace(replace(replace(p, '%', '%25'), ' ', '%20'), '#', '%23'), '?', '%3F');
-- public_release_id: release_id with the folders of a zip member path removed ('mr:<req>:<zip>!<member basename>#<sheet>');
-- sheet names and container file names cannot hold '/' or '\', so everything after '!' up to the last separator is folders.
-- The build fails unless it is unique.
CREATE OR REPLACE MACRO public_rid(rid, pra_id, container_path, member) AS CASE
  WHEN member IS NOT NULL AND starts_with(rid, 'mr:' || replace(pra_id, 'muckrock-', '') || ':' || basename(container_path) || '!')
  THEN 'mr:' || replace(pra_id, 'muckrock-', '') || ':' || basename(container_path) || '!'
       || regexp_replace(substr(rid, len('mr:' || replace(pra_id, 'muckrock-', '') || ':' || basename(container_path)) + 2),
                         '^.*[/\\]', '')
  ELSE rid END;
-- release_sources: one row per released file/sheet with everything needed to find, fetch and verify the original.
-- document = public form (zip > member basename); document_verbatim = the full member path as released.
-- link = where to get it + the hashes to verify it (the zip's and, inside a zip, the member's).
CREATE OR REPLACE VIEW release_sources AS
WITH b AS (SELECT max(value) FILTER (WHERE key = 'repo_web_url') AS web, max(value) FILTER (WHERE key = 'repo_commit') AS sha
           FROM truth.build_info),
s AS (
SELECT r.release_id, public_rid(r.release_id, r.pra_id, r.container_path, r.member) AS public_release_id,
  r.producer, r.producer_agency_id, r.audit, r.pra_id,
  CASE WHEN r.pra_id LIKE 'muckrock-%' THEN 'MuckRock request ' || replace(r.pra_id, 'muckrock-', '')
       WHEN r.release_id LIKE 'smpd:%' THEN 'San Mateo public records request ' || r.pra_id
       ELSE r.producer || ' public records request ' || r.pra_id END AS request_label,
  r.request_url, r.released_on, r.released_on_basis,
  basename(r.container_path) AS container_file, r.member, basename(r.member) AS member_file, r.sheet, r.source_file,
  -- the document a reader opens: zip > member, or the file itself; for repo NDJSON, the agency workbook it was converted from
  CASE WHEN r.member IS NOT NULL THEN basename(r.container_path) || ' > ' || basename(r.member)
       ELSE coalesce(r.source_file, basename(r.container_path)) END AS document,
  CASE WHEN r.member IS NOT NULL THEN basename(r.container_path) || ' > ' || r.member
       ELSE coalesce(r.source_file, basename(r.container_path)) END AS document_verbatim,
  r.container_root, r.container_path, r.source_url,
  CASE WHEN r.container_root = 'repo' THEN b.web || '/blob/' || b.sha || '/' || url_path(r.container_path) END AS repo_url,
  r.container_sha256, r.member_sha256, r.content_sha256, r.src_row_basis
FROM truth.releases r, b)
SELECT *, CASE
  WHEN container_root = 'repo' AND release_id LIKE 'smpd:%' THEN 'Document: ' || repo_url || ' (SHA-256 ' || container_sha256 || ')'
  WHEN container_root = 'repo' THEN 'Committed conversion of the workbook (scripts/xlsx_to_audit_ndjson.py: cell text trimmed, empty cells omitted): ' || repo_url || ' (SHA-256 of the conversion ' || container_sha256 || ')'
  ELSE 'Download: ' || source_url || ' (SHA-256 ' || container_sha256 || ')'
       || coalesce('; ' || member_file || ' inside it: SHA-256 ' || member_sha256, '') END AS link
FROM s;

CREATE OR REPLACE MACRO cite_text(producer, request_label, request_url, released_on, document, sheet, locator, link) AS
  producer || '. ' || request_label || coalesce(' (' || request_url || ')', '') || ', produced '
  || coalesce(strftime(released_on, '%Y-%m-%d'), '(production date not recorded)') || '. '
  || document || coalesce(', sheet "' || sheet || '"', '') || coalesce(', ' || locator, '') || '.' || coalesce(' ' || link, '');
""".replace("__EXEMPTION_RE__", EXEMPTION_RE))
dups = con.execute("""SELECT public_release_id, list(release_id ORDER BY release_id) FROM release_sources
                      GROUP BY 1 HAVING count(*) > 1 OR public_release_id IS NULL""").fetchall()
if dups:
    sys.exit(f"public_release_id is not unique (or NULL) for {len(dups)} ids, e.g. {dups[:3]}: fix public_rid() before publishing")

con.execute(r"""
-- flock_rows: truth rows with each field read from the label that holds it (layout corrections) + flags.
CREATE OR REPLACE VIEW flock_rows AS
__FR_VIEW__;
-- sightings = one row per released search row, parsed. Two branch views (Flock-format logs; SMPD's PDF text) + their
-- union; row lookups use the same templates on pre-selected rows (see event_sightings, sql_templates.py).
CREATE OR REPLACE VIEW sightings_flock AS
__SF_VIEW__;
CREATE OR REPLACE VIEW sightings_smpd AS
__SM_VIEW__;
CREATE OR REPLACE VIEW sightings AS SELECT * FROM sightings_flock UNION ALL BY NAME SELECT * FROM sightings_smpd;

-- divergence of one sighting's cell from the other logs' consensus (read_field passes the clean value, not the surface)
CREATE OR REPLACE MACRO divergence(state, value, revealed) AS CASE
  WHEN revealed IS NULL THEN 'no_other_record'
  WHEN state IN ('redacted_flock', 'redacted_agency', 'withheld', 'partial') THEN 'masked_here_released_elsewhere'
  WHEN state IN ('empty', 'not_exported') THEN 'blank_here_present_elsewhere'
  WHEN state = 'placeholder' THEN 'placeholder_here'
  WHEN value = revealed THEN 'same'
  ELSE 'differs_from_other_logs' END;

-- sighting_sources: any sighting -> where it is in the original + a ready-to-paste citation. Cheap to filter by
-- release_id (and row_no); filtering by sighting_id scans three columns of truth.flock_audit_rows (seconds).
-- (two branch views + their union, so a join on (release_id, row_no) prunes each branch; see sightings)
CREATE OR REPLACE VIEW sighting_sources_flock AS
__SSF_VIEW__;
CREATE OR REPLACE VIEW sighting_sources_smpd AS
__SSM_VIEW__;
CREATE OR REPLACE VIEW sighting_sources AS
SELECT * FROM sighting_sources_flock UNION ALL BY NAME SELECT * FROM sighting_sources_smpd;

-- event_log: Flock event-log rows (user / network-sharing administration), parsed timestamp + producer + citation.
CREATE OR REPLACE VIEW event_log AS
SELECT e.release_id, rs.public_release_id, e.row_no, e.src_row, rs.producer,
  coalesce(flock_ts(e."Timestamp"), iso_ts_utc(e."Timestamp")) AS ts,
  e."User" AS "user", e."Event Type" AS event_type, e."Entity Type" AS entity_type, e."Entity Details" AS entity_details,
  e."Event Id" AS event_log_id, e.extra,
  cite_text(rs.producer, rs.request_label, rs.request_url, rs.released_on, rs.document, rs.sheet, 'row ' || e.src_row, rs.link)
    AS citation
FROM truth.flock_event_rows e JOIN release_sources rs USING (release_id);

CREATE OR REPLACE VIEW release_content_groups AS
  SELECT content_sha256, count(*) AS n_releases, list(release_id ORDER BY released_on NULLS LAST, release_id) AS releases,
         list(released_on ORDER BY released_on NULLS LAST, release_id) AS released_on
  FROM truth.releases WHERE content_sha256 IS NOT NULL GROUP BY 1 HAVING count(*) > 1;
""".replace("__FR_VIEW__", flock_rows_sql("truth.flock_audit_rows"))
   .replace("__SF_VIEW__", sightings_flock_sql("flock_rows"))
   .replace("__SM_VIEW__", sightings_smpd_sql("truth.smpd_pdf_rows"))
   .replace("__SSF_VIEW__", sources_flock_sql("truth.flock_audit_rows"))
   .replace("__SSM_VIEW__", sources_smpd_sql("truth.smpd_pdf_rows")))
# civilian PII: plate tokens (key read on use, never stored), scrub macros, and the one view public exports may read
con.execute((Path(__file__).parent / "public_macros.sql").read_text())
step("layer 2 views/macros")

# Views/macros that read the cache: defined here so --views-only can refresh them without recomputing the cache.
# Every one finds its rows in cache.sighting_event first, then parses only those truth rows with the templates the full
# views use (a join to the full `sightings` view would parse all ~140M rows).
CACHE_READS = """
CREATE OR REPLACE VIEW events AS
  SELECT event_id, event_key, count(*) AS n_sightings, count(DISTINCT producer) AS n_logs, max(basis) AS weakest_link,
         list(DISTINCT producer ORDER BY producer) AS logs
  FROM cache.sighting_event GROUP BY event_id, event_key;   -- both keys, so a filter on either is applied before grouping

-- one event (same columns as events), found by its stored key: event_key = hash(event_id), then the id itself
CREATE OR REPLACE MACRO event(eid) AS TABLE
  SELECT event_id, event_key, count(*) AS n_sightings, count(DISTINCT producer) AS n_logs, max(basis) AS weakest_link,
         list(DISTINCT producer ORDER BY producer) AS logs
  FROM cache.sighting_event WHERE event_key = hash(eid) AND event_id = eid GROUP BY event_id, event_key;

CREATE OR REPLACE MACRO field_of(fld, reason_v, case_v) AS CASE lower(fld) WHEN 'reason' THEN reason_v WHEN 'case' THEN case_v
  ELSE error('read_field: field must be ''reason'' or ''case'', not ' || coalesce('''' || fld || '''', 'NULL')) END;

-- read_field('reason'|'case' [, who]): per sighting, the surface value and the leave-one-out consensus of the
-- OTHER sightings of the same search (top-2 per event, so no candidate explosion; ties broken by the value, so repeated
-- runs agree). Nothing stored. With who, only the events that producer's sightings belong to are parsed; without it,
-- every sighting is read (the full sightings view).
CREATE OR REPLACE MACRO read_field(fld, who := NULL) AS TABLE
WITH ev AS (SELECT DISTINCT event_key FROM cache.sighting_event
            WHERE (who IS NULL OR producer = who) AND basis <> 'x_ambiguous' AND field_of(fld, true, true)),
se AS (SELECT c.* FROM cache.sighting_event c SEMI JOIN ev USING (event_key)),
raw AS (SELECT a.* FROM truth.flock_audit_rows a SEMI JOIN se USING (release_id, row_no) WHERE who IS NOT NULL),
fr AS (__FR__),
sf AS (__SF__),
rsm AS (SELECT p.* FROM truth.smpd_pdf_rows p SEMI JOIN se USING (release_id, row_no) WHERE who IS NOT NULL),
s AS (SELECT * FROM sightings WHERE who IS NULL
      UNION ALL BY NAME SELECT * FROM sf UNION ALL BY NAME SELECT * FROM (__SSM__)),
fv AS (SELECT se.sighting_id, se.event_key, se.event_id, se.producer,
              field_of(fld, s.reason_state, s.case_state) AS state,
              field_of(fld, s.reason_surface, s.case_surface) AS surface,
              field_of(fld, s.reason, s.case_no) AS value
       FROM se JOIN s USING (sighting_id)),
cnt AS (SELECT event_key, value, count(DISTINCT producer) AS n FROM fv WHERE state = 'value' GROUP BY ALL),
t1 AS (SELECT event_key, first(value ORDER BY n DESC, value) AS v1, max(n) AS n1 FROM cnt GROUP BY event_key),
t2 AS (SELECT c.event_key, first(c.value ORDER BY c.n DESC, c.value) AS v2, max(c.n) AS n2 FROM cnt c JOIN t1 USING (event_key)
       WHERE c.value <> t1.v1 GROUP BY c.event_key),
r AS (SELECT fv.*,
        CASE WHEN fv.state = 'value' AND fv.value = t1.v1 THEN
                  CASE WHEN t1.n1 - 1 >= coalesce(t2.n2, 0) AND t1.n1 > 1 THEN t1.v1 WHEN t2.n2 > 0 THEN t2.v2 END
             ELSE t1.v1 END AS revealed,
        CASE WHEN fv.state = 'value' AND fv.value = t1.v1 THEN
                  CASE WHEN t1.n1 - 1 >= coalesce(t2.n2, 0) AND t1.n1 > 1 THEN t1.n1 - 1 WHEN t2.n2 > 0 THEN t2.n2 END
             ELSE t1.n1 END AS support
      FROM fv LEFT JOIN t1 USING (event_key) LEFT JOIN t2 USING (event_key))
SELECT sighting_id, producer, event_id, state, surface, revealed, support,
       divergence(state, value, revealed) AS divergence
FROM r WHERE who IS NULL OR producer = who;

-- Drill-down: every log's record of one search, with its state and where to find it in the original (citation).
CREATE OR REPLACE MACRO event_sightings(eid) AS TABLE
WITH se AS (SELECT * FROM cache.sighting_event WHERE event_key = hash(eid) AND event_id = eid),
-- select the event's truth rows first (a direct join prunes the scan), then parse/cite them with the same SQL the
-- full-table views use (sql_templates.py)
raw AS (SELECT a.* FROM truth.flock_audit_rows a SEMI JOIN se USING (release_id, row_no)),
fr AS (__FR__),
sf AS (__SF__),
rsm AS (SELECT p.* FROM truth.smpd_pdf_rows p SEMI JOIN se USING (release_id, row_no)),
s AS (SELECT * FROM sf UNION ALL BY NAME SELECT * FROM (__SSM__)),
src AS (SELECT sighting_id, src_row, citation FROM (__SSF__)
        UNION ALL SELECT sighting_id, src_row, citation FROM (__SRCSM__))
SELECT se.producer, se.basis, s.release_id, s.row_no, src.src_row, s.org, s.t, s.nets, s.reason_state, s.reason_surface,
       s.case_state, s.case_surface, s.name_state, s.plate_state, src.citation
FROM se JOIN s USING (sighting_id) LEFT JOIN src USING (sighting_id) ORDER BY se.producer, s.release_id, s.row_no;
""".replace("__FR__", flock_rows_sql("raw")).replace("__SF__", sightings_flock_sql("fr")) \
   .replace("__SSF__", sources_flock_sql("raw")).replace("__SSM__", sightings_smpd_sql("rsm")) \
   .replace("__SRCSM__", sources_smpd_sql("rsm"))
if "--views-only" in sys.argv:
    if con.execute("SELECT count(*) FROM duckdb_tables() WHERE schema_name = 'cache' AND table_name = 'sighting_event'").fetchone()[0]:
        con.execute(CACHE_READS)
        step("cache-reading views refreshed (cache kept)")
    sys.exit(0)

# ---------------- Layer 3: cache (too slow on read) ----------------------------------------------
# fingerprint the view/macro definitions as stored (DuckDB normalizes SQL on reload), so check_cache.py sees the same
con.close()
con = duckdb.connect(DERIVED)
con.execute(LIMITS)
con.execute(f"ATTACH IF NOT EXISTS '{TRUTH}' AS truth (READ_ONLY)")
CODE_SHA = cache_fingerprint.code_sha(con, __file__)
fp = cache_fingerprint.truth_fp(con)
DUCKDB_VERSION = cache_fingerprint.duckdb_version(con)   # sighting_id / k3 / k5 / event_key are this version's hash()
con.execute("CREATE SCHEMA IF NOT EXISTS cache")
con.execute("CREATE TABLE IF NOT EXISTS cache.builds (name VARCHAR, built_at TIMESTAMP, truth_fingerprint VARCHAR, code_sha VARCHAR)")
con.execute("ALTER TABLE cache.builds ADD COLUMN IF NOT EXISTS duckdb_version VARCHAR")
con.execute("""CREATE OR REPLACE TABLE cache.sighting_keys AS
  SELECT sighting_id, release_id, row_no, producer, audit, flock_id, tf_start IS NOT NULL AS has_tf,
         CASE WHEN t IS NOT NULL AND org IS NOT NULL THEN hash(org, t, nets) END AS k3,
         CASE WHEN t IS NOT NULL AND org IS NOT NULL AND tf_start IS NOT NULL THEN hash(org, t, nets, tf_start, tf_end) END AS k5
  FROM sightings WHERE (t IS NOT NULL AND org IS NOT NULL) OR flock_id IS NOT NULL""")
step("cache.sighting_keys (parsed once)")
# Linking intermediates (140M rows) go to a scratch database file, compressed on disk and deleted afterwards: as TEMP
# tables they spilled uncompressed and ran the disk out; inside derived.duckdb their freed blocks would stay in the file.
CB_PATH = Path(DERIVED).with_name(Path(DERIVED).name + ".link_tmp.duckdb")
CB_PATH.unlink(missing_ok=True)
con.execute(f"ATTACH '{CB_PATH}' AS cb")
# key -> UUID lookups from every UUID-bearing sighting's keys (deterministic: count(DISTINCT) + min, no any_value).
# A key maps to one UUID only if every UUID-bearing sighting with that key has the same UUID (n = 1).
con.execute("""CREATE OR REPLACE TABLE cb.k3u AS SELECT k3, count(DISTINCT flock_id) n, min(flock_id) fid
  FROM cache.sighting_keys WHERE flock_id IS NOT NULL AND k3 IS NOT NULL GROUP BY k3""")
con.execute("""CREATE OR REPLACE TABLE cb.k3n AS SELECT k3, count(DISTINCT flock_id) n, min(flock_id) fid
  FROM cache.sighting_keys WHERE flock_id IS NOT NULL AND k3 IS NOT NULL AND NOT has_tf GROUP BY k3""")  # UUID sightings with no time frame (e.g. SMPD own log)
con.execute("""CREATE OR REPLACE TABLE cb.k5u AS SELECT k5, count(DISTINCT flock_id) n, min(flock_id) fid
  FROM cache.sighting_keys WHERE flock_id IS NOT NULL AND k5 IS NOT NULL GROUP BY k5""")
con.execute("""CREATE OR REPLACE TABLE cb.a AS
  SELECT k.sighting_id, k.release_id, k.row_no, k.producer, k.audit, k.has_tf, k.k3, k.k5,
    CASE WHEN k.flock_id IS NOT NULL THEN 'u:' || k.flock_id
         WHEN k5u.n = 1 THEN 'u:' || k5u.fid
         WHEN NOT k.has_tf AND k3u.n = 1 THEN 'u:' || k3u.fid
         WHEN k.has_tf AND k5u.n IS NULL AND k3n.n = 1 THEN 'u:' || k3n.fid END AS eid,
    CASE WHEN k.flock_id IS NOT NULL THEN '1_uuid' WHEN k5u.n = 1 THEN '2_k5'
         WHEN NOT k.has_tf AND k3u.n = 1 THEN '3_k3'
         WHEN k.has_tf AND k5u.n IS NULL AND k3n.n = 1 THEN '3b_k3_to_tf_less_uuid'
         WHEN k5u.n > 1 OR (NOT k.has_tf AND k3u.n > 1) OR (k.has_tf AND k5u.n IS NULL AND k3n.n > 1) THEN 'x_ambiguous' END AS basis
  FROM cache.sighting_keys k LEFT JOIN cb.k5u k5u USING (k5) LEFT JOIN cb.k3u k3u USING (k3)
       LEFT JOIN cb.k3n k3n ON k3n.k3 = k.k3""")
step("tiers 1-3")
# tiers 4-6 only for the unresolved rows, joined on a pre-filtered set (no residual predicates in the join)
con.execute("""CREATE OR REPLACE TABLE cb.k3g AS SELECT k3, count(DISTINCT k5) n, min(k5) k5 FROM cb.a
  WHERE basis IS NULL AND has_tf GROUP BY k3""")
con.execute("""CREATE OR REPLACE TABLE cb.rest AS
  SELECT a.sighting_id, a.release_id, a.row_no, a.producer, a.audit,
    CASE WHEN a.has_tf THEN 'k5:' || a.k5 WHEN g.n = 1 THEN 'k5:' || g.k5 WHEN g.n > 1 THEN 'x:' || a.sighting_id ELSE 'k3:' || a.k3 END AS event_id,
    CASE WHEN a.has_tf THEN '4_k5_group' WHEN g.n = 1 THEN '5_k3_to_group' WHEN g.n > 1 THEN 'x_ambiguous' ELSE '6_k3_group' END AS basis
  FROM (SELECT * FROM cb.a WHERE basis IS NULL) a LEFT JOIN cb.k3g g USING (k3)""")
con.execute("""CREATE OR REPLACE TABLE cache.sighting_event AS
  SELECT *, hash(event_id) AS event_key FROM (
  SELECT sighting_id, release_id, row_no, producer, audit, CASE WHEN basis = 'x_ambiguous' THEN 'x:' || sighting_id ELSE eid END AS event_id, basis
  FROM cb.a WHERE basis IS NOT NULL
  UNION ALL SELECT sighting_id, release_id, row_no, producer, audit, event_id, basis FROM cb.rest)""")
con.execute("DETACH cb")
CB_PATH.unlink(missing_ok=True)
step("cache.sighting_event")
BUILT_AT = datetime.now(timezone.utc).replace(tzinfo=None)   # UTC, like truth.build_info.built_at_utc
for name in ("sighting_keys", "sighting_event"):
    con.execute("INSERT INTO cache.builds (name, built_at, truth_fingerprint, code_sha, duckdb_version) VALUES (?, ?, ?, ?, ?)",
                [name, BUILT_AT, fp, CODE_SHA, DUCKDB_VERSION])

# ---------------- Layer 2 again: reads that use the cache ----------------------------------------
con.execute(CACHE_READS)
step("read_field / events")
print(con.execute("SELECT basis, count(*) FROM cache.sighting_event GROUP BY 1 ORDER BY 1").fetchall())
con.close()
