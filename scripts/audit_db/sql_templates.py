"""SQL templates shared by the full-table views (build_derived.py) and the row lookups (event_sightings, read_field,
audit_client.py): one definition of the parse and citation rules, applied to whichever rows the caller selects first.

Importing this module runs nothing. Each function returns SQL over a relation you name (a table, a view, a CTE name or a
parenthesized subquery); run it on a connection to derived.duckdb with truth attached, because the SQL reads derived's
views and macros (release_meta, release_sources, cell_state, flock_ts, tf_bound, clean_value, cite_text).

  flock_rows_sql(src)       src shaped like truth.flock_audit_rows -> each field read from the label that holds it
  sightings_flock_sql(src)  src shaped like flock_rows              -> one parsed sighting per row (Flock branch)
  sources_flock_sql(src)    src shaped like truth.flock_audit_rows -> where the row is in the original + citation
  sightings_smpd_sql(src)   src shaped like truth.smpd_pdf_rows     -> one parsed sighting per row (SMPD branch)
  sources_smpd_sql(src)     src shaped like truth.smpd_pdf_rows     -> where the row is in the PDF + citation

Select the rows before parsing, so the truth scan prunes, e.g. for two known rows:
  raw = "(SELECT * FROM truth.flock_audit_rows WHERE release_id IN ('mr:1:a.csv#csv') AND row_no IN (7, 9))"
  con.sql(sightings_flock_sql(f"({flock_rows_sql(raw)})"))
"""

FLOCK_COLS = ["ID", "Name", "Org Name", "Total Networks Searched", "Total Devices Searched", "Time Frame", "License Plate",
              "Reason", "Case #", "Filters", "Search Time", "Search Type", "Text Prompt", "Moderation"]
FLAGS = ["Org Name", "Reason", "Case #", "Name", "License Plate"]   # fields whose header / withheld flags cell_state reads


def _q(c):
    return '"' + c.replace('"', '""') + '"'


# The truth.release_layouts entry covering a row (m = release_meta, a = the truth row). release_meta lists layouts
# latest-starting first, so where two ranges overlap the more specific (later-starting) one applies, every time.
LAYOUT_OF = ("list_filter(m.layouts, l -> a.src_row >= l.src_row_from AND (l.src_row_to IS NULL OR a.src_row <= l.src_row_to))"
             "[1].mapping")


def flock_rows_sql(src):
    """Each field read from the header label that actually holds it (truth.release_layouts), plus release_meta flags.
    A mapping value is a Flock header label, or the key of an unlabeled cell kept in `extra` (e.g. 'column13').
    A layout that maps a field to null (no such cell in those rows) clears the field's header flag -> not_exported."""
    def field(c):
        whens = " ".join(f"WHEN '{x}' THEN x.{_q(x)}" for x in FLOCK_COLS)
        return (f"CASE WHEN x.mapping IS NULL OR NOT map_contains(x.mapping, '{c}') THEN x.{_q(c)} "
                f"ELSE CASE x.mapping['{c}'] {whens} "
                f"ELSE json_extract_string(x.extra, '$.\"' || x.mapping['{c}'] || '\"') END END AS {_q(c)}")

    def in_header(f):
        return (f"CASE WHEN x.mapping IS NOT NULL AND map_contains(x.mapping, '{f}') AND x.mapping['{f}'] IS NULL THEN false "
                f"ELSE x.{_q(f + '_h')} END AS {_q(f + '_h')}")
    return ("SELECT x.release_id, x.row_no, x.src_row, x.mapping IS NOT NULL AS layout_corrected,\n  "
            + ",\n  ".join(field(c) for c in FLOCK_COLS) + ",\n  x.extra, x.producer, x.audit,\n  "
            + ",\n  ".join(f"{in_header(f)}, x.{_q(f + '_w')}" for f in FLAGS) + "\n"
            "FROM (SELECT a.*, m.* EXCLUDE (release_id, layouts), " + LAYOUT_OF + " AS mapping\n"
            f"      FROM {src} a JOIN release_meta m USING (release_id)) x")


SIGHTINGS_FLOCK_T = """WITH fl AS (
  SELECT hash(a.release_id, a.row_no) AS sighting_id, a.release_id, a.row_no, a.src_row, a.producer, a.audit,
    coalesce(nullif(trim(a."Org Name"), ''), CASE WHEN a.audit = 'own' AND NOT a."Org Name_h" THEN a.producer END) AS org,
    CASE WHEN nullif(trim(a."Org Name"), '') IS NOT NULL THEN 'released'
         WHEN a.audit = 'own' AND NOT a."Org Name_h" THEN 'producer (own-search log without an Org Name column)' END AS org_basis,
    -- San Jose releases split the timestamp: 'Search Date' (YYYY-MM-DD, kept in extra) + 'Search Time' (HH:MM:SS)
    coalesce(flock_ts(a."Search Time"),
             flock_ts(json_extract_string(a.extra, '$."Search Date"') || ' ' || trim(a."Search Time"))) AS t,
    flock_int(a."Total Networks Searched") AS nets,
    flock_int(a."Total Devices Searched") AS devices,
    tf_bound(a."Time Frame", 1) AS tf_start, tf_bound(a."Time Frame", 2) AS tf_end,
    CASE WHEN trim(a."ID") NOT IN ('', '***') THEN trim(a."ID") END AS flock_id, trim(a."Search Type") AS search_type,
    a."Reason" AS reason_surface, cell_state(a."Reason", a."Reason_h", a."Reason_w", false, true) AS reason_state,
    a."Case #" AS case_surface, cell_state(a."Case #", a."Case #_h", a."Case #_w", false, true) AS case_state,
    a."Name" AS name_surface, cell_state(a."Name", a."Name_h", a."Name_w", a.producer = 'Redwood City CA PD', false) AS name_state,
    a."License Plate" AS plate_surface, cell_state(a."License Plate", a."License Plate_h", a."License Plate_w", false, false) AS plate_state,
    a."Text Prompt" AS text_prompt, a."Filters" AS filters, a.layout_corrected
  FROM __SRC__ a)
SELECT *, clean_value(reason_surface, reason_state) AS reason, clean_value(case_surface, case_state) AS case_no FROM fl"""

# locator/link/citation per row; the release-level parts (document, link with hashes) come from release_sources
SOURCES_FLOCK_T = """WITH fl AS (
  SELECT hash(a.release_id, a.row_no) AS sighting_id, a.release_id, rs.public_release_id, a.row_no, a.src_row,
    NULL::VARCHAR AS pdf_pages, rs.producer, rs.request_label, rs.request_url, rs.released_on, rs.document,
    rs.document_verbatim, rs.sheet, a.mapping IS NOT NULL AS layout_corrected,
    'row ' || a.src_row || CASE WHEN a.mapping IS NOT NULL
                                THEN '; cells released under other column labels (see release_layouts)' ELSE '' END AS locator,
    coalesce(rs.source_url, rs.repo_url) AS open_url, rs.container_sha256 AS sha256, rs.member_sha256, rs.src_row_basis, rs.link
  FROM (SELECT a.release_id, a.row_no, a.src_row, __LAYOUT_OF__ AS mapping
        FROM __SRC__ a JOIN release_meta m USING (release_id)) a
  JOIN release_sources rs USING (release_id))
SELECT *, cite_text(producer, request_label, request_url, released_on, document, sheet, locator, link) AS citation FROM fl"""

# SMPD's own log, read from the produced PDFs' text layer: one truth row per search-id block, lines verbatim.
# count_time_line = '<networks> MM/DD/YYYY, HH:MM:SS AM|PM UTC' (the PDF prints both cells on one line).
SMPD_COUNT_TIME_RE = r"^\s*([0-9]+)\s+([0-9]{1,2}/[0-9]{1,2}/[0-9]{4},\s*[0-9]{1,2}:[0-9]{2}:[0-9]{2}\s*[AaPp][Mm]\s*UTC)\s*$"
SIGHTINGS_SMPD_T = """WITH sm AS (
  SELECT hash(s.release_id, s.row_no) AS sighting_id, s.release_id, s.row_no, NULL::BIGINT AS src_row, r.producer, r.audit,
    r.producer AS org, 'producer (SMPD PDF export has no org field)' AS org_basis,
    flock_ts(nullif(regexp_extract(s.count_time_line, '__CT__', 2), '')) AS t,
    TRY_CAST(nullif(regexp_extract(s.count_time_line, '__CT__', 1), '') AS INTEGER) AS nets,
    NULL::INTEGER AS devices, NULL::TIMESTAMP AS tf_start, NULL::TIMESTAMP AS tf_end,
    CASE WHEN trim(s.id) NOT IN ('', '***') THEN trim(s.id) END AS flock_id, NULL::VARCHAR AS search_type,
    s.reason_line AS reason_surface, cell_state(s.reason_line, true, false, false, true) AS reason_state,
    NULL::VARCHAR AS case_surface, 'not_exported' AS case_state,
    -- name only from a trusted block: a text-order fallback block may hold a neighbour's line in user_line
    CASE WHEN s.parse_note IS NULL OR starts_with(s.parse_note, 'text order differs from the printed row') THEN s.user_line END AS name_surface,
    cell_state(CASE WHEN s.parse_note IS NULL OR starts_with(s.parse_note, 'text order differs from the printed row') THEN s.user_line END,
               true, false, false, false) AS name_state,
    NULL::VARCHAR AS plate_surface, 'not_exported' AS plate_state, NULL::VARCHAR AS text_prompt, NULL::VARCHAR AS filters,
    false AS layout_corrected
  FROM __SRC__ s JOIN truth.releases r USING (release_id))
SELECT *, clean_value(reason_surface, reason_state) AS reason, clean_value(case_surface, case_state) AS case_no FROM sm""".replace(
    "__CT__", SMPD_COUNT_TIME_RE)

SOURCES_SMPD_T = """WITH sm AS (
  SELECT hash(s.release_id, s.row_no) AS sighting_id, s.release_id, rs.public_release_id, s.row_no, NULL::BIGINT AS src_row,
    rs.document || ' page ' || s.src_page AS pdf_pages, rs.producer, rs.request_label, rs.request_url, rs.released_on,
    rs.document, rs.document_verbatim, NULL::VARCHAR AS sheet, false AS layout_corrected,
    'page ' || s.src_page || ' (search id ' || s.id || ')' AS locator,
    rs.repo_url AS open_url, rs.container_sha256 AS sha256, rs.member_sha256, rs.src_row_basis, rs.link
  FROM __SRC__ s JOIN release_sources rs USING (release_id))
SELECT *, cite_text(producer, request_label, request_url, released_on, document, sheet, locator, link) AS citation FROM sm"""


def sightings_flock_sql(src):
    return SIGHTINGS_FLOCK_T.replace("__SRC__", src)


def sources_flock_sql(src):
    return SOURCES_FLOCK_T.replace("__LAYOUT_OF__", LAYOUT_OF).replace("__SRC__", src)


def sightings_smpd_sql(src):
    return SIGHTINGS_SMPD_T.replace("__SRC__", src)


def sources_smpd_sql(src):
    return SOURCES_SMPD_T.replace("__SRC__", src)
