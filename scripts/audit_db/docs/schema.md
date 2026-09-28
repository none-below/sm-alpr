# Schema reference

Every table, view and macro in the two DuckDB files, with each column's type and meaning, and the coded values
researchers filter on.

- **`truth.duckdb`** (layer 1): the released rows, verbatim and as text, plus authored facts that each carry a
  citation. It is the only ground truth. It is frozen after build.
- **`derived.duckdb`**: views and macros in schema `main` (layer 2), computed on every read over truth. Schema `cache`
  (layer 3) stores event linking, which is too slow to compute on read. Everything in derived can be rebuilt from
  truth.

A session opens `derived.duckdb` read-only and attaches `truth.duckdb` `READ_ONLY` as `truth`. Every view reads
through that name. For setup and the Python helpers, see [../README.md](../README.md) and [api.md](api.md).

Conventions used below:

- **civilian data; local only**: the column can hold civilian identifiers (plates, reasons, case numbers, prompts,
  filters and other free text). Never publish it. `sightings_public` is the export-safe view.
- **local only**: the column holds no civilian values, but it is not for publication as it stands. This covers
  verbatim zip folder paths, local paths, and searcher or account names.
- Every `TIMESTAMP` is naive and means UTC.
- Always filter the row-level objects by `release_id` (for `sightings_public`, by `public_release_id`), by producer,
  or by event. These are the truth row tables, `flock_rows`, `sightings*`, `sighting_sources*`, `events` and
  `cache.*`. A filter on a computed column (`sighting_id`, `t`, `org`) makes DuckDB parse every row.

Sections: [truth](#truth) · [derived: views](#derived-views) · [derived: macros](#derived-macros) ·
[derived: cache](#derived-cache) · [Enumerations](#enumerations)

## truth

Truth holds one row per released row. Nothing in it is merged, de-duplicated, trimmed or cast. No foreign keys are
declared. The row tables join `releases` on `release_id`. `release_dispositions` and `release_layouts` apply to every
release whose `release_id` is `LIKE` their `release_pattern`.

| Table | One row per | Key |
|---|---|---|
| `releases` | one released file, workbook sheet or `smpd:` PDF | `release_id` |
| `flock_audit_rows` | one non-blank data row of a network or own-search audit | (`release_id`, `row_no`) |
| `flock_event_rows` | one non-blank data row of a Flock event log | (`release_id`, `row_no`) |
| `smpd_pdf_rows` | one search-id block printed in an `smpd:` PDF | (`release_id`, `row_no`) |
| `release_dispositions` | one authored claim that a field was withheld or redacted | (`release_pattern`, `field`) |
| `release_layouts` | one authored correction for cells released under the wrong column label | (`release_pattern`, `src_row_from`) |
| `build_info` | one fact about the build | `key` |

`release_id` has three forms:

- `mr:<request>:<container>[!<member>]#<sheet>` for a MuckRock file. `#csv` stands in for a CSV's sheet.
- `smpd:<request folder>:<pdf file name>` for San Mateo PD's PDF-derived own-search log, one release per PDF.
- `<prefix>:[<request>:]<file stem>` for other NDJSON conversions committed to the repo.

Treat `release_id` as an opaque key. It can contain zip folder names, so publish `public_release_id` instead.

### `releases`

| Column | Type | Meaning |
|---|---|---|
| `release_id` | VARCHAR | Release key (primary key). Local only |
| `producer` | VARCHAR | Flock organization whose log this is, spelled as in the agency registry's `flock_names`. It is not the organization that ran the search |
| `producer_agency_id` | VARCHAR | The producer's UUID in `assets/agency_registry.json`. NULL if it did not resolve |
| `producer_basis` | VARCHAR | How `producer` was decided ([values](#producer_basis)) |
| `producer_source` | VARCHAR | Citation of the `producers.json` entry that set `producer`. Set only when `producer_basis` = `producers.json` |
| `audit` | VARCHAR | Log type ([values](#audit)) |
| `pra_id` | VARCHAR | The records request: `muckrock-<request>` for MuckRock, otherwise the request identifier (for `smpd:`, the request folder name) |
| `container_root` | VARCHAR | The tree `container_path` is relative to ([values](#other-coded-values)) |
| `container_path` | VARCHAR | The stored file: the downloaded file or zip, or the committed NDJSON or PDF |
| `member` | VARCHAR | Full path of the file inside the zip. NULL when the file is not zipped. Local only |
| `member_sha256` | VARCHAR | SHA-256 of the extracted zip member. Set exactly when `member` is |
| `sheet` | VARCHAR | Workbook sheet name, verbatim (trailing spaces kept). NULL for CSV, PDF, and conversions that record no sheet |
| `source_file` | VARCHAR | The file a reader opens: the member or file name, the workbook an NDJSON was converted from, or the PDF |
| `container_sha256` | VARCHAR | SHA-256 of `container_path` at build time |
| `header` | VARCHAR[] | Canonical column names used to load the rows, by position. NULL for an image-only PDF |
| `header_raw` | VARCHAR[] | The header as released or printed, verbatim, where one was recorded (see `header_basis`). Otherwise NULL |
| `header_basis` | VARCHAR | How `header` and `header_raw` were obtained, in words |
| `n_rows` | BIGINT | Number of rows loaded for this release. It equals the release's count in its row table |
| `content_sha256` | VARCHAR | Fingerprint of the released content, independent of the container: file bytes for CSV and PDF, the staged cells for a sheet, the decompressed NDJSON. Equal values mark re-releases |
| `released_on` | DATE | Production date. NULL when it is not recorded |
| `released_on_basis` | VARCHAR | Where `released_on` comes from, or why it is NULL |
| `source_url` | VARCHAR | Public download URL of the container (the zip, if zipped). MuckRock releases only |
| `request_url` | VARCHAR | Public web page of the request, where one exists |
| `src_row_basis` | VARCHAR | How a row's locator (`src_row`, or page and line) maps to what a reader sees in the original |

### `flock_audit_rows`

The 14 Flock columns are the superset of labels seen in Flock audit exports. A release fills only the columns in its
`header`. Every other column is NULL in all of its rows, so check `list_contains(releases.header, '<column>')` to tell
an empty cell from a column that was not released.

| Column | Type | Meaning |
|---|---|---|
| `release_id` | VARCHAR | The row's release |
| `row_no` | BIGINT | 1-based position among the release's stored rows, in file order |
| `src_row` | BIGINT | The row's number in the original as a reader sees it (see `releases.src_row_basis`) |
| `ID` | VARCHAR | Flock search UUID. The same search carries the same UUID in every log that recorded it |
| `Name` | VARCHAR | The Flock user who ran the search, as exported. Local only |
| `Org Name` | VARCHAR | The Flock organization of that user |
| `Total Networks Searched` | VARCHAR | Number of organizations' camera networks the search queried |
| `Total Devices Searched` | VARCHAR | Number of cameras the search queried |
| `Time Frame` | VARCHAR | Period searched: start and end timestamps |
| `License Plate` | VARCHAR | Plate searched for, full or partial. Civilian data; local only |
| `Reason` | VARCHAR | Free-text reason the searcher entered. Civilian data; local only |
| `Case #` | VARCHAR | Case or incident number the searcher entered. Civilian data; local only |
| `Filters` | VARCHAR | Search filters (vehicle attributes, lists, plate terms). Civilian data; local only |
| `Search Time` | VARCHAR | When the search ran, in Flock's text form, UTC |
| `Search Type` | VARCHAR | Flock's search-type label |
| `Text Prompt` | VARCHAR | Prompt of a free-form (natural-language) search. Civilian data; local only |
| `Moderation` | VARCHAR | Flock's moderation verdict on that prompt |
| `extra` | JSON | Released columns outside the 14, as canonical name → cell text. NULL when all of them are empty. Civilian data; local only |

### `flock_event_rows`

| Column | Type | Meaning |
|---|---|---|
| `release_id` | VARCHAR | The row's release (`audit` = `event`) |
| `row_no` | BIGINT | 1-based position in file order |
| `src_row` | BIGINT | The row's number in the original as a reader sees it |
| `Timestamp` | VARCHAR | When the event happened, as exported |
| `User` | VARCHAR | The account that performed the action. Local only |
| `Event Type` | VARCHAR | The action performed |
| `Entity Type` | VARCHAR | The kind of object acted on (user, network share, hotlist entry, …) |
| `Entity Details` | VARCHAR | Description of that object. Hotlist entries can hold plates. Civilian data; local only |
| `Event Id` | VARCHAR | Flock's event UUID |
| `extra` | JSON | Released columns outside the six |

### `smpd_pdf_rows`

San Mateo PD's own-search log, read from the text layer of the PDFs in the `smpd:` releases. Each row is one printed
search-id block. Lines are stored verbatim, exactly as the PDF text layer gives them.

| Column | Type | Meaning |
|---|---|---|
| `release_id` | VARCHAR | The `smpd:` release (one PDF) |
| `row_no` | BIGINT | 1-based order of the id lines in the text layer. This is not time order |
| `src_page` | INTEGER | 1-based PDF page the id line is printed on |
| `src_line` | INTEGER | 1-based index of the id line among that page's non-empty text-layer lines |
| `id` | VARCHAR | Flock search UUID as printed, the same UUID as `ID` in other logs. It repeats when a search is printed more than once |
| `user_line` | VARCHAR | The printed `userID` cell. Local only |
| `count_time_line` | VARCHAR | The printed network count and search time, which share one line: `<networks> MM/DD/YYYY, HH:MM:SS AM UTC` |
| `reason_line` | VARCHAR | The printed Reason cell. NULL when the cell is blank. Civilian data; local only |
| `parse_note` | VARCHAR | NULL when the block's text order matched its printed row. Otherwise, how its cells were read: by printed position, or in text order |

### `release_dispositions`

| Column | Type | Meaning |
|---|---|---|
| `release_pattern` | VARCHAR | SQL `LIKE` pattern matched against `release_id` |
| `field` | VARCHAR | Canonical Flock field name |
| `disposition` | VARCHAR | The kind of claim ([values](#other-coded-values)) |
| `source` | VARCHAR | Citation of the document that makes the claim |

### `release_layouts`

| Column | Type | Meaning |
|---|---|---|
| `release_pattern` | VARCHAR | SQL `LIKE` pattern matched against `release_id` |
| `src_row_from` | BIGINT | First affected row, as a `src_row` (inclusive) |
| `src_row_to` | BIGINT | Last affected row (inclusive). NULL means to the end |
| `mapping` | MAP(VARCHAR, VARCHAR) | Canonical field → the header label, or `extra` key, whose cell holds that field in these rows. A NULL value means no cell holds the field |
| `source` | VARCHAR | Citation, and how the shift was verified |

### `build_info`

| Column | Type | Meaning |
|---|---|---|
| `key` | VARCHAR | Fact name |
| `value` | VARCHAR | Fact value, as text |

| Key | Meaning |
|---|---|
| `built_at_utc` | When truth was built (UTC) |
| `repo_checkout` | Absolute local path of the checkout the repo inputs were read from. Local only |
| `repo_commit` | That checkout's `HEAD`, which identifies both the inputs and the build code |
| `repo_inputs_dirty` | Whether the repo inputs or the build code had uncommitted changes |
| `commits_behind_local_origin_main` | Number of commits between `HEAD` and the local `origin/main` ref |
| `evidence_dir` | Absolute local path of the MuckRock evidence directory. Local only |
| `repo_web_url` | GitHub base URL, used to build repo permalinks |
| `duckdb_version`, `python_version`, `pymupdf_version` | Tool versions that built truth |

## derived: views

### `release_fields`

One row per release × field, for `ID`, `Name`, `Org Name`, `License Plate`, `Reason`, `Case #` and `Time Frame`.

| Column | Type | Meaning |
|---|---|---|
| `release_id` | VARCHAR | The release. Local only |
| `producer` | VARCHAR | Flock organization whose log this is |
| `audit` | VARCHAR | Log type |
| `field` | VARCHAR | Canonical field name |
| `in_header` | BOOLEAN | The field is in the release's `header`. Layout corrections are ignored here |
| `withheld` | BOOLEAN | A `withheld_blanked` disposition covers this release and field |

### `release_meta`

One row per release: the per-release inputs that row parsing needs.

| Column | Type | Meaning |
|---|---|---|
| `release_id`, `producer`, `audit` | VARCHAR | As in `release_fields` |
| `Org Name_h`, `Reason_h`, `Case #_h`, `Name_h`, `License Plate_h` | BOOLEAN | The field is in the release's header |
| `Org Name_w`, `Reason_w`, `Case #_w`, `Name_w`, `License Plate_w` | BOOLEAN | The field is withheld under a disposition (`is_withheld`) |
| `layouts` | STRUCT(src_row_from BIGINT, src_row_to BIGINT, "mapping" MAP(VARCHAR, VARCHAR))[] | The release's `release_layouts` entries, latest `src_row_from` first. NULL when it has none |

### `release_sources`

One row per release: everything needed to name, fetch and verify the original.

| Column | Type | Meaning |
|---|---|---|
| `release_id` | VARCHAR | The release. Local only |
| `public_release_id` | VARCHAR | The release id to publish: `release_id` with zip-member folders removed. Unique |
| `request_label` | VARCHAR | Human-readable name of the request |
| `container_file` | VARCHAR | File name of the container (the file itself, or the zip) |
| `member_file` | VARCHAR | File name of the zip member. NULL when the file is not zipped |
| `document` | VARCHAR | The document a reader opens, in public form: `<container_file> > <member_file>` for a zip member |
| `document_verbatim` | VARCHAR | The same, with the member's full path. Local only |
| `repo_url` | VARCHAR | GitHub permalink to a repo-committed file at the build's commit. NULL for evidence files |
| `link` | VARCHAR | Sentence naming where to get the document and the hashes that verify it |
| `producer`, `producer_agency_id`, `audit`, `pra_id`, `request_url`, `released_on`, `released_on_basis`, `member`, `sheet`, `source_file`, `container_root`, `container_path`, `source_url`, `container_sha256`, `member_sha256`, `content_sha256`, `src_row_basis` | VARCHAR (`released_on`: DATE) | Passed through from `truth.releases` (`member` is local only) |

### `release_content_groups`

One row per `content_sha256` that two or more releases share, meaning the same content was produced more than once.

| Column | Type | Meaning |
|---|---|---|
| `content_sha256` | VARCHAR | The shared content hash |
| `n_releases` | BIGINT | Number of releases that have it |
| `releases` | VARCHAR[] | Their `release_id`s, ordered by `released_on` (NULLs last), then id. Local only |
| `released_on` | DATE[] | Their production dates, in the same order |

### `flock_rows`

One row per `truth.flock_audit_rows` row, with each Flock field read from the label that actually holds it. This is
where `release_layouts` corrections are applied.

| Column | Type | Meaning |
|---|---|---|
| `release_id`, `row_no`, `src_row` | VARCHAR, BIGINT, BIGINT | As in `truth.flock_audit_rows` |
| `layout_corrected` | BOOLEAN | A `release_layouts` range covers this row |
| `ID`, `Name`, `Org Name`, `Total Networks Searched`, `Total Devices Searched`, `Time Frame`, `License Plate`, `Reason`, `Case #`, `Filters`, `Search Time`, `Search Type`, `Text Prompt`, `Moderation` | VARCHAR | The released value of each field. In corrected rows, it is read from the label that `mapping` names. `License Plate`, `Reason`, `Case #`, `Filters` and `Text Prompt` are civilian data; local only. `Name` is local only |
| `extra` | JSON | Released columns outside the 14. Local only |
| `producer`, `audit` | VARCHAR | From `release_meta` |
| `Org Name_h`, `Org Name_w`, `Reason_h`, `Reason_w`, `Case #_h`, `Case #_w`, `Name_h`, `Name_w`, `License Plate_h`, `License Plate_w` | BOOLEAN | Header and withheld flags from `release_meta`. A layout that maps a field to no cell sets its `_h` flag false for the rows it covers |

### `sightings`

One row per released search row, parsed. A *sighting* is one log's record of one search: a search appears once per
log that recorded it and once per re-release of that log, and none of those rows are merged. `sightings` is
`sightings_flock` (parses `flock_rows`) `UNION ALL BY NAME` `sightings_smpd` (parses `truth.smpd_pdf_rows`). All
three have the columns below. In the `smpd:` branch, `src_row`, `devices`, `tf_start`, `tf_end`, `search_type`,
`case_surface`, `plate_surface`, `text_prompt` and `filters` are NULL, and `case_state` and `plate_state` are
`not_exported`.

| Column | Type | Meaning |
|---|---|---|
| `sighting_id` | UBIGINT | Row id, `hash(release_id, row_no)`. DuckDB `hash()` is stable only within one DuckDB version |
| `release_id` | VARCHAR | The release. Local only |
| `row_no` | BIGINT | Row position in the release |
| `src_row` | BIGINT | Row number in the original. NULL for `smpd:` rows, which are located by page |
| `producer` | VARCHAR | Flock organization whose log this is |
| `audit` | VARCHAR | `network` or `own` |
| `org` | VARCHAR | Organization that ran the search: the trimmed `Org Name`; the producer for an own-search log with no `Org Name` column and for `smpd:` rows; otherwise NULL |
| `org_basis` | VARCHAR | How `org` was set ([values](#other-coded-values)) |
| `t` | TIMESTAMP | Search time |
| `nets` | INTEGER | Number of networks searched |
| `devices` | INTEGER | Number of devices searched |
| `tf_start` | TIMESTAMP | Start of the time window searched |
| `tf_end` | TIMESTAMP | End of the time window searched |
| `flock_id` | VARCHAR | Flock search UUID, trimmed. NULL when blank or `***` |
| `search_type` | VARCHAR | Flock search type, trimmed |
| `reason_surface` | VARCHAR | Reason as released, masks included. Civilian data; local only |
| `reason_state` | VARCHAR | Cell state of Reason ([values](#cell-states)) |
| `case_surface` | VARCHAR | Case # as released. Civilian data; local only |
| `case_state` | VARCHAR | Cell state of Case # |
| `name_surface` | VARCHAR | Searcher as released. Local only |
| `name_state` | VARCHAR | Cell state of Name |
| `plate_surface` | VARCHAR | License Plate as released. Civilian data; local only |
| `plate_state` | VARCHAR | Cell state of License Plate |
| `text_prompt` | VARCHAR | Free-form search prompt as released. Civilian data; local only |
| `filters` | VARCHAR | Search filters as released. Civilian data; local only |
| `layout_corrected` | BOOLEAN | The row's fields were read through a layout correction |
| `reason` | VARCHAR | Usable Reason: the trimmed text when `reason_state` = `value`, otherwise NULL. Civilian data; local only |
| `case_no` | VARCHAR | Usable Case #: the trimmed text when `case_state` = `value`, otherwise NULL. Civilian data; local only |

### `sightings_public`

The export-safe view. It has the same rows as `sightings`, named by `public_release_id`. Plates become HMAC tokens
(`p1_` + 16 hex digits), and other keyword-anchored civilian identifiers in free text are replaced with placeholders.
It raises an error without a valid plate-token key file. Key published rows on (`public_release_id`, `row_no`).

| Column | Type | Meaning |
|---|---|---|
| `sighting_id`, `row_no`, `src_row`, `producer`, `audit`, `org`, `org_basis`, `t`, `nets`, `devices`, `tf_start`, `tf_end`, `flock_id`, `layout_corrected` | as in `sightings` | Passed through. `sighting_id` is a local join key only |
| `public_release_id` | VARCHAR | The release, in public form. Filter on it the way you would filter on `release_id` |
| `search_type` | VARCHAR | `sightings.search_type` when it looks like a search-type label, otherwise NULL |
| `reason` | VARCHAR | Reason as released (masks kept), scrubbed, with plates tokenized. This is not `sightings.reason`: read it with `reason_state` |
| `case_no` | VARCHAR | Case # as released, with plate shapes tokenized |
| `searcher_name` | VARCHAR | Name as released, with plate-shaped words tokenized |
| `plate` | VARCHAR | Plate token for a `value` cell. A blank or a known mask is given as released. Anything else is NULL |
| `text_prompt` | VARCHAR | Prompt, scrubbed, with plates tokenized |
| `filters` | VARCHAR | Filters, with plate search terms tokenized |
| `reason_state`, `case_state`, `name_state`, `plate_state` | VARCHAR | As in `sightings` |

### `sighting_sources`

One row per sighting: where the row sits in the released original, plus a ready-to-paste citation. `sighting_sources`
is `sighting_sources_flock` `UNION ALL BY NAME` `sighting_sources_smpd`, and all three have these columns. It holds no
civilian cell values.

| Column | Type | Meaning |
|---|---|---|
| `sighting_id` | UBIGINT | As in `sightings` |
| `release_id` | VARCHAR | The release. Local only |
| `public_release_id` | VARCHAR | The release, in public form |
| `row_no` | BIGINT | Row position in the release |
| `src_row` | BIGINT | Row number in the original. NULL for `smpd:` |
| `pdf_pages` | VARCHAR | `<document> page <n>` for `smpd:` rows. NULL otherwise |
| `producer` | VARCHAR | Flock organization whose log this is |
| `request_label` | VARCHAR | Human-readable name of the request |
| `request_url` | VARCHAR | Web page of the request |
| `released_on` | DATE | Production date |
| `document` | VARCHAR | Document to open, in public form |
| `document_verbatim` | VARCHAR | Document with the zip member's full path. Local only |
| `sheet` | VARCHAR | Sheet name |
| `layout_corrected` | BOOLEAN | A layout correction covers the row |
| `locator` | VARCHAR | Where the row is in the document: `row <src_row>` (flagged when layout-corrected), or `page <n> (search id <id>)` |
| `open_url` | VARCHAR | URL to fetch the document: the download URL, or the repo permalink |
| `sha256` | VARCHAR | SHA-256 of what `open_url` serves |
| `member_sha256` | VARCHAR | SHA-256 of the zip member. NULL when the file is not zipped |
| `src_row_basis` | VARCHAR | How the locator maps to the original |
| `link` | VARCHAR | Where to get the document, with its hashes |
| `citation` | VARCHAR | Full citation, built by `cite_text` |

### `event_log`

One row per Flock event-log row (user administration, network-sharing changes, hotlist edits), with a parsed time and
a citation.

| Column | Type | Meaning |
|---|---|---|
| `release_id` | VARCHAR | The release. Local only |
| `public_release_id` | VARCHAR | The release, in public form |
| `row_no`, `src_row` | BIGINT | As in `truth.flock_event_rows` |
| `producer` | VARCHAR | Flock organization whose log this is |
| `ts` | TIMESTAMP | Event time, parsed from `Timestamp` (offsets converted to UTC) |
| `user` | VARCHAR | `User`, verbatim. Local only |
| `event_type` | VARCHAR | `Event Type`, verbatim |
| `entity_type` | VARCHAR | `Entity Type`, verbatim |
| `entity_details` | VARCHAR | `Entity Details`, verbatim. Civilian data; local only |
| `event_log_id` | VARCHAR | `Event Id`, verbatim |
| `extra` | JSON | Other released columns |
| `citation` | VARCHAR | Full citation |

### `events`

One row per linked search (event) in `cache.sighting_event`. For a single event, use `event(eid)`.

| Column | Type | Meaning |
|---|---|---|
| `event_id` | VARCHAR | The linked search ([forms](#link-tiers)) |
| `event_key` | UBIGINT | `hash(event_id)`, a compact join key |
| `n_sightings` | BIGINT | Number of sightings linked to the event, including re-releases and repeated printings |
| `n_logs` | BIGINT | Number of distinct producers among those sightings |
| `weakest_link` | VARCHAR | Weakest link tier among the event's sightings (`max(basis)`) |
| `logs` | VARCHAR[] | Those producers, sorted |

## derived: macros

Scalar macros:

| Macro | Returns | What it returns |
|---|---|---|
| `flock_ts(s)` | TIMESTAMP | A Flock export time (`MM/DD/YYYY, HH:MM:SS AM UTC`) or a staged spreadsheet datetime, parsed. NULL for any other format |
| `tf_bound(s, i)` | TIMESTAMP | Bound `i` of a `Time Frame` cell (1 = start, 2 = end). The cell is split on a line feed or on ` to ` |
| `flock_int(s)` | INTEGER | Text cast to DOUBLE, then to INTEGER. NULL on failure |
| `iso_ts_utc(s)` | TIMESTAMP | An ISO 8601 time. A trailing `Z` or offset is converted to UTC |
| `agency_mask(raw)` | BOOLEAN | True when the whole trimmed cell is an agency mask: `REDACTED`, `[REDACTED]`, two or more `#`, `* * *`, or block glyphs |
| `exemption_cite(raw)` | BOOLEAN | True when the whole trimmed cell is one or more California exemption citations (CPRA or Civil Code section numbers, with or without a label) |
| `cell_state(raw, in_header, withheld, partial_ok, placeholder_ok)` | VARCHAR | The cell's state ([values](#cell-states)) |
| `clean_value(raw, st)` | VARCHAR | `trim(raw)` when `st` = `value`, otherwise NULL |
| `is_withheld(rid, fld)` | BOOLEAN | True when a `withheld_blanked` disposition matches the release and field |
| `basename(p)` | VARCHAR | The last path component |
| `url_path(p)` | VARCHAR | The path with `%`, space, `#` and `?` percent-encoded |
| `public_rid(rid, pra_id, container_path, member)` | VARCHAR | The publishable release id: `rid` with zip-member folders removed |
| `cite_text(producer, request_label, request_url, released_on, document, sheet, locator, link)` | VARCHAR | An assembled citation sentence. NULL if `producer`, `request_label` or `document` is NULL |
| `field_of(fld, reason_v, case_v)` | type of the chosen argument | `reason_v` for `'reason'` and `case_v` for `'case'`. Any other `fld` raises an error |
| `divergence(state, value, revealed)` | VARCHAR | A label comparing one sighting's cell with the other logs' consensus ([values](#divergence-labels)) |
| `plate_norm(p)` | VARCHAR | Letters and digits only, upper-cased. NULL if none are left |
| `plate_pad(khex, a, b)` | BLOB | An HMAC inner or outer key pad, from a hex key |
| `plate_hmac(p, pi, po)` | VARCHAR | Plate token: `p1_` + the first 16 hex digits of HMAC-SHA256 over the normalized plate |
| `plate_token(p)` | VARCHAR | `plate_hmac` using the key file's pads. Raises an error without a valid key |
| `plate_public(surface, state, pi, po)` | VARCHAR | Public plate value: a token for `value`; a blank, `***`, agency mask or exemption citation as released; otherwise NULL |
| `plate_candidates(txt)` | VARCHAR[] | Plate-shaped substrings of free text to tokenize |
| `filter_plate_part(r)` | VARCHAR | The plate part of one run of letters and digits from a `Filters` cell, or NULL |
| `filter_plate_candidates(txt)` | VARCHAR[] | The distinct plate parts in a `Filters` cell |
| `tokenize_in(txt, cands, word, pi, po)` | VARCHAR | `txt` with each plate replaced by `[<token>]`: matched by word for free text, by run for `Filters` |
| `scrub_civilian(txt)` | VARCHAR | `txt` with keyword-anchored civilian identifiers replaced: `[name]`, `[dob]`, `[ssn]`, `[phone]`, `[dl]`, `[addr]` |

Table macros (`FROM macro(...)`):

| Macro | Returns | What it returns |
|---|---|---|
| `read_field(fld, who := NULL)` | table | Per sighting: its Reason (`fld` = `'reason'`) or Case # (`'case'`), and what the other logs released for the same search. With `who`, only that producer's sightings, and only its events are parsed |
| `event(eid)` | table | One row with the `events` columns for event `eid`. No row for an unknown id |
| `event_sightings(eid)` | table | Every sighting of one event, with its states and citation, ordered by producer, release and row |
| `plate_key_hex()` | table (`khex`) | The plate-token key from the key file. Never select it |
| `plate_key_pads()` | table (`pi`, `po`) | The key's HMAC pads, which are equivalent to the key. Use them only inside expressions |

`read_field` columns:

| Column | Type | Meaning |
|---|---|---|
| `sighting_id` | UBIGINT | The sighting |
| `producer` | VARCHAR | Its log |
| `event_id` | VARCHAR | Its linked search |
| `state` | VARCHAR | Its `reason_state` or `case_state` |
| `surface` | VARCHAR | Its released text. Civilian data; local only |
| `revealed` | VARCHAR | Consensus value of the other sightings of the search, or NULL. Civilian data; local only |
| `support` | BIGINT | Number of distinct producers behind `revealed` |
| `divergence` | VARCHAR | `divergence(state, <its clean value>, revealed)` |

`event_sightings` columns: `producer`, `basis` (link tier), `release_id` (local only), `row_no`, `src_row`, `org`, `t`,
`nets`, `reason_state`, `reason_surface` (civilian data; local only), `case_state`, `case_surface` (civilian data;
local only), `name_state`, `plate_state` and `citation`. Each is as in `sightings`, `sighting_sources` and
`cache.sighting_event`.

## derived: cache

Only event linking is stored, and every cache table can be recomputed from truth. `sighting_id`, `k3`, `k5` and
`event_key` are DuckDB `hash()` values. Run `check_cache.py` to find out whether the cache still matches truth, the
linking code and the DuckDB version. Linking never reads Reason, Case #, Name, License Plate, Search Type or devices.

### `cache.sighting_keys`

One row per sighting that can be linked: it has a Flock search UUID, or both a search time and an org.

| Column | Type | Meaning |
|---|---|---|
| `sighting_id` | UBIGINT | The sighting |
| `release_id` | VARCHAR | Its release. Local only |
| `row_no` | BIGINT | Row position in the release |
| `producer` | VARCHAR | Flock organization whose log this is |
| `audit` | VARCHAR | `network` or `own` |
| `flock_id` | VARCHAR | Flock search UUID, if one was released |
| `has_tf` | BOOLEAN | The row has a parsed time frame |
| `k3` | UBIGINT | `hash(org, t, nets)`. NULL without `t` or `org` |
| `k5` | UBIGINT | `hash(org, t, nets, tf_start, tf_end)`. NULL without `t`, `org` or `tf_start` |

### `cache.sighting_event`

One row per linkable sighting, together with the search it was linked to.

| Column | Type | Meaning |
|---|---|---|
| `sighting_id`, `release_id`, `row_no`, `producer`, `audit` | UBIGINT, VARCHAR, BIGINT, VARCHAR, VARCHAR | As in `cache.sighting_keys` |
| `event_id` | VARCHAR | The linked search. Its prefix follows the tier ([forms](#link-tiers)) |
| `basis` | VARCHAR | How the sighting was linked ([values](#link-tiers)) |
| `event_key` | UBIGINT | `hash(event_id)`, the key `event`, `event_sightings` and `read_field` look events up by |

### `cache.builds`

One row per cache table per full build.

| Column | Type | Meaning |
|---|---|---|
| `name` | VARCHAR | Cache table: `sighting_keys` or `sighting_event` |
| `built_at` | TIMESTAMP | When it was built |
| `truth_fingerprint` | VARCHAR | md5 fingerprint of the truth it was built from |
| `code_sha` | VARCHAR | Fingerprint of the linking code and of the views and macros that code reads |
| `duckdb_version` | VARCHAR | DuckDB `version()` at build time |

## Enumerations

### Cell states

Values returned by `cell_state()` and held in `reason_state`, `case_state`, `name_state` and `plate_state`. The macro
tries the rules in this order and returns the first that matches.

| Value | Meaning |
|---|---|
| `withheld` | The field is absent or the cell is blank, and a `withheld_blanked` disposition covers the release and field |
| `not_exported` | The field is not in the release's header, or a layout correction maps it to no cell |
| `empty` | The field was exported and the cell is blank |
| `redacted_flock` | The cell is exactly `***`, Flock's mask marker. The marker alone does not show who applied it |
| `redacted_agency` | The whole cell is an agency mask (`agency_mask`) or an exemption citation (`exemption_cite`) |
| `partial` | Part masked, part readable. This is a `REDACTED` marker followed by text, or, where the call sets `partial_ok`, an initial plus a short fragment |
| `placeholder` | A junk entry: a listed word (`none`, `n/a`, `test`, …) or no letter or digit. Only where the call sets `placeholder_ok`, which is the case for Reason and Case # |
| `value` | Any other text. `clean_value` returns it. This state does not check that the text is genuine |

### Divergence labels

Values returned by `divergence()` in `read_field`. `revealed` is the consensus of the other sightings of the same
search. The rules are tried in this order.

| Value | Meaning |
|---|---|
| `no_other_record` | No other sighting of the search has a value (`revealed` is NULL) |
| `masked_here_released_elsewhere` | This cell is masked, withheld or partial, and a value exists in another log or another production of this log |
| `blank_here_present_elsewhere` | This cell is empty or not exported, and a value exists elsewhere |
| `placeholder_here` | This cell is a placeholder, and a value exists elsewhere |
| `same` | This value equals the consensus (exact, case-sensitive) |
| `differs_from_other_logs` | This value differs from the consensus |

### Link tiers

Values of `cache.sighting_event.basis` (also `events.weakest_link` and `event_sightings.basis`), strongest first. `k3`
matches on (org, time, networks), and `k5` adds the time frame. A lookup is used only when exactly one candidate
matches.

| Value | Meaning | `event_id` |
|---|---|---|
| `1_uuid` | The row carries a Flock search UUID | `u:<UUID>` |
| `2_k5` | Its `k5` belongs to exactly one UUID | `u:<UUID>` |
| `3_k3` | It has no time frame, and its `k3` belongs to exactly one UUID | `u:<UUID>` |
| `3b_k3_to_tf_less_uuid` | It has a time frame but no `k5` match, and its `k3` belongs to exactly one UUID among the UUID rows without a time frame | `u:<UUID>` |
| `4_k5_group` | No UUID was found. It has a time frame, and it is grouped with the rows that share its `k5` | `k5:<k5>` |
| `5_k3_to_group` | No UUID was found. It has no time frame, and its `k3` maps to exactly one `k5` group | `k5:<k5>` |
| `6_k3_group` | No UUID was found. It has no time frame and no `k5` group, and it is grouped by `k3` | `k3:<k3>` |
| `x_ambiguous` | Its key fits two or more UUIDs or groups, so it forms an event of its own | `x:<sighting_id>` |

Only `u:` ids stay the same across rebuilds. The other forms are `hash()` values tied to one build.

### `producer_basis`

Values of `truth.releases.producer_basis`. For `mr:` releases the rules are tried in the order listed, from
`producers.json` on.

| Value | Meaning |
|---|---|
| `fixed (dedicated loader)` | Fixed by the loader of a repo-committed NDJSON source |
| `fixed (SMPD PRA)` | Fixed for the `smpd:` releases (San Mateo PD's PDF-derived log) |
| `producers.json` | An authored entry in `producers.json`, per file or per request, cited in `producer_source` |
| `filename` | Parsed from the file or zip-member name (`<digits>_<Org Name>_[Network-]Audit`) |
| `own-search dominant Org Name` | The most frequent non-blank `Org Name` in the request's own-search files |
| `MuckRock agency name` | Last resort: the agency named on the MuckRock request |

### `audit`

Values of `truth.releases.audit`, carried into the derived views.

| Value | Meaning | Rows in |
|---|---|---|
| `network` | Network audit: every search by any organization that touched the producer's cameras, including the producer's own searches | `flock_audit_rows` |
| `own` | Own-search (organization) audit: the searches run by the producer's users | `flock_audit_rows`; `smpd_pdf_rows` for `smpd:` |
| `event` | Event log: administrative events (users, network sharing, hotlists) | `flock_event_rows` |

### Other coded values

| Value | Column | Meaning |
|---|---|---|
| `withheld_blanked` | `release_dispositions.disposition` | The cited document says the field was withheld, so a blank cell reads `withheld` |
| `redacted` | `release_dispositions.disposition` | The cited document says the field was masked. This is informational only: `cell_state` does not read it |
| `released` | `sightings.org_basis` | `org` is the released `Org Name` |
| `producer (own-search log without an Org Name column)` | `sightings.org_basis` | `org` is the producer, because the own-search log has no `Org Name` column |
| `producer (SMPD PDF export has no org field)` | `sightings.org_basis` | `org` is the producer of an `smpd:` release |
| `repo` | `releases.container_root` | `container_path` is relative to the repo checkout (`build_info.repo_checkout`) |
| `evidence` | `releases.container_root` | `container_path` is relative to the evidence directory (`build_info.evidence_dir`) |
