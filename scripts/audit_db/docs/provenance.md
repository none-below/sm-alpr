# From a row to the original document

For anyone who quotes a number or a row from this database in an article, a report or a court filing, and for the
engineers who hand them the rows. It answers: which released document a row came from, where in that document it is,
how to get the document, how to show it is the same row, and how to cite it. The rule: never publish a figure you
cannot walk back to released rows this way.

Examples run after the standard session in [README.md](README.md) (`con` open read-only on `derived.duckdb`, `truth`
attached read-only, 4 threads / 4 GB). Row numbers, hashes and links are as of the build in `truth.build_info`
(`built_at_utc` 2026-09-26T20:38:17Z, repo inputs at `e138455bb`). Corpus totals are in [coverage.md](coverage.md) and
[stats.md](stats.md) (both generated); this document does not repeat them.

## Terms

- **Release**: one released file, or one sheet of a workbook (`release_id`). One agency production can hold many.
- **Row**: one data row of a release, keyed by (`release_id`, `row_no`). `src_row` is where a reader finds it.
- **Sighting**: a row parsed (`sightings`): one log's record of one search.
- **Event**: one Flock search, linked across logs (`cache.sighting_event.event_id`). Its **tier** (`basis`, e.g.
  `1_uuid` = same Flock search UUID) says how the link was made ([linking.md](linking.md)).
- **Producer**: the organization whose log the file is, not the organization that searched (`org`).
- **Network audit** vs **own-search log**: a network audit lists every search, by anyone, that included the
  producer's cameras; an own-search log lists the producer's own users' searches ([semantics.md](semantics.md),
  "Three kinds of log").

## Quick recipe

```python
import sys; sys.path.insert(0, "<code>")     # scripts/audit_db, as in the standard session
import audit_client as ac

# 1. (release_id, row_no) -> ready-to-paste citation. About 0.01 s: the release_id filter prunes the scan.
rid, n = "mr:197131:Mountain_View_CA_PD_Santa_Clara_County_network_Dec_2025.csv#csv", 10
print(con.execute("SELECT citation FROM sighting_sources WHERE release_id = ? AND row_no = ?", [rid, n]).fetchone()[0])

#    Many rows, any releases: one pruned lookup per release, same columns as sighting_sources.
cites = ac.citations_for(con, [(rid, 10), (rid, 11)]).select("public_release_id, locator, citation").fetchall()

# 2. sighting_id -> (release_id, row_no) once; keep the pair and use it from then on.
sid = 16912114210866814136
rid, n = con.execute("SELECT release_id, row_no FROM cache.sighting_event WHERE sighting_id = ?", [sid]).fetchone()

# 3. (release_id, row_no) -> its event -> every log's record of that search, each with its citation.
eid = con.execute("SELECT event_id FROM cache.sighting_event WHERE release_id = ? AND row_no = ?", [rid, n]).fetchone()[0]
for r in ac.drill(con, eid):                 # 0.2-1 s; SQL: SELECT * FROM event_sightings(?)  (about 3 s)
    print(r["producer"], r["basis"], r["public_release_id"], r["src_row"], r["citation"])

# 4. A Flock search UUID (the ID column) -> its event: event_id = 'u:' + UUID.
print(ac.event(con, "u:4cf5833b-32bf-484c-8603-e2c877f6cecb")["n_logs"])          # 10

# 5. A Flock event-log row (users, network-sharing changes) -> citation.
erid = "mr:214823:26-996_2026-09-01_00_44_52_-0700.zip!Event Logs - Network Share Log.csv#csv"
print(con.execute("SELECT citation FROM event_log WHERE release_id = ? AND row_no = 3", [erid]).fetchone()[0])

# 6. A San Mateo PD search id -> its PDF row(s) -> citation (PDF file, page, search id).
for rid, n in con.execute("SELECT release_id, row_no FROM truth.smpd_pdf_rows WHERE id = ?",
                          ["8189f4a2-f9e9-44e4-8654-1fa531338455"]).fetchall():
    print(con.execute("SELECT citation FROM sighting_sources WHERE release_id = ? AND row_no = ?", [rid, n]).fetchone()[0])
```

- Keep `release_id` and `row_no` in every result you may have to cite, and join
  `sighting_sources USING (release_id, row_no)` with the release filtered on both sides.
- `sighting_id` is `hash(release_id, row_no)` from this DuckDB version: a working key, not an identifier to store or
  publish. Event ids other than `u:` (`k5:`, `k3:`, `x:`) are also build-specific. Persist (`release_id`, `row_no`).
- A row with neither search time + org nor a Flock ID has no cache row (249 in this build) and so no event. Cite it by
  (`release_id`, `row_no`) as in step 1.
- What to hand a reporter or lawyer: `citation`, `public_release_id`, `src_row` (or page and search id), `open_url`,
  `sha256`, `member_sha256`. Not `release_id` or `document_verbatim` (see [Public and verbatim names](#public-and-verbatim-names)).

## What a citation says

`sighting_sources.citation`, `event_log.citation`, the `citation` of `event_sightings()` and of `audit_client.drill()` /
`citations_for()` are all built by `cite_text()` from `release_sources` and read the same way. Marin County SO, network
audit for June 2024:

```
Marin County CA SO. MuckRock request 181227 (https://www.muckrock.com/foi/marin-county-3047/cpra-request-alpr-audit-and-data-sharing-181227/),
produced 2025-03-20. 25-112_2025-03-19_21_09_10_-0700.zip > Marin County CA SO_Network_Audit_6_1_2024_7_1_2024_REDACTED.xlsx,
sheet "Marin County CA SO_Network_Audi", row 4. Download: https://cdn.muckrock.com/foia_files/2025/03/20/25-112_2025-03-19_21_09_10_-0700.zip
(SHA-256 5e283194c9cf0a8ed403f20273818a95a7ea971a2bff34f6a3e69da5c3f682ab); Marin County CA SO_Network_Audit_6_1_2024_7_1_2024_REDACTED.xlsx
inside it: SHA-256 9e24f18d3db2155d049baf27ea67ab9b098f9204bd6dd0f4b39c3a547db27831
```

(A citation is one line; the examples here are wrapped for reading.)

| Part | Comes from | Meaning |
|---|---|---|
| `Marin County CA SO` | `releases.producer` | Whose log this file is (registry `flock_names` spelling) |
| `MuckRock request 181227 (…)` | `request_label`, `request_url` | The public-records request, and its public page when one is recorded |
| `produced 2025-03-20` | `releases.released_on` | Production date; basis in `released_on_basis`. NULL prints `produced (production date not recorded)` |
| `… .zip > … .xlsx` | `release_sources.document` | Public form: zip file `>` member file name (folders inside the zip dropped), else the file itself; for repo NDJSON the agency workbook |
| `sheet "…"` | `releases.sheet` | Worksheet name as stored (Excel caps names at 31 characters) |
| `row 4` | `locator` | `row <src_row>`; SMPD: `page <n> (search id <id>)`. Layout-corrected rows add `; cells released under other column labels (see release_layouts)` |
| `Download: … (SHA-256 …)` | `link` | Where to get the file and its SHA-256; inside a zip also the member's own SHA-256 |

`link` takes one of three forms: `Download: <MuckRock CDN URL> (SHA-256 <zip or file>)[; <member> inside it: SHA-256
<member>]`; `Committed conversion of the workbook (scripts/xlsx_to_audit_ndjson.py: cell text trimmed, empty cells
omitted): <GitHub permalink> (SHA-256 of the conversion <hash>)` for Los Altos and Redwood City; `Document: <GitHub
permalink> (SHA-256 <PDF>)` for San Mateo PD.

### One real citation per source type

**MuckRock, workbook inside a zip**: the Marin citation above (`row_no` 3).

**MuckRock, CSV** (Mountain View PD, `mr:197131:…_Dec_2025.csv#csv`, `row_no` 10):

```
Mountain View Police Department. MuckRock request 197131 (https://www.muckrock.com/foi/mountain-view-3340/flock-safety-search-audits-network-sharing-and-general-order-197131/),
produced 2026-02-13. Mountain_View_CA_PD_Santa_Clara_County_network_Dec_2025.csv, row 11. Download:
https://cdn.muckrock.com/foia_files/2026/02/13/Mountain_View_CA_PD_Santa_Clara_County_network_Dec_2025.csv (SHA-256 3ab2f047a7cc277ca73e8352f112e42d3cdc37e535b5dc9a834852e8f35b0169)
```

**Los Altos, committed NDJSON** (`la:26-366:Los_Altos_PD_Network_Audit_2025__JANUARY`, `row_no` 5):

```
Los Altos CA PD. Los Altos CA PD public records request 26-366 (https://losaltosca.nextrequest.com/requests/26-366), produced 2026-07-09.
Los Altos PD Network Audit 2025.xlsx, sheet "JANUARY", row 6. Committed conversion of the workbook (scripts/xlsx_to_audit_ndjson.py:
cell text trimmed, empty cells omitted): https://github.com/none-below/sm-alpr/blob/e138455bb5582041266ea613ccfb74d8fcf737c4/assets/los-altos-pras/json/pra-26-366/Los_Altos_PD_Network_Audit_2025__JANUARY.ndjson.gz
(SHA-256 of the conversion 842285f3b4a485f7f3ceed524ffb487aee46882a80412af30c1a92914b0b66f1)
```

**Redwood City, committed NDJSON** (`rwc:PRA_26_217_2024_Q3`, `row_no` 10):

```
Redwood City CA PD. Redwood City CA PD public records request 26-217, produced (production date not recorded). PRA 26-217 2024 Q3 (1).xlsx,
row 11. Committed conversion of the workbook (scripts/xlsx_to_audit_ndjson.py: cell text trimmed, empty cells omitted):
https://github.com/none-below/sm-alpr/blob/e138455bb5582041266ea613ccfb74d8fcf737c4/assets/redwood-city-pras/json/PRA_26_217_2024_Q3.ndjson.gz
(SHA-256 of the conversion 7ffceb26129bf1b7686d54b610aa72f9308ca6b434a0cd035c6e191c0b19edfa)
```

**San Mateo PD, PDF page** (`smpd:W012818-053026:4_1_2026-4_30_2026-San_Mateo_CA_PD-Audit__1_.pdf`, `row_no` 3174):

```
San Mateo CA PD. San Mateo public records request W012818-053026, produced (production date not recorded).
4_1_2026-4_30_2026-San_Mateo_CA_PD-Audit__1_.pdf, page 71 (search id 8189f4a2-f9e9-44e4-8654-1fa531338455).
Document: https://github.com/none-below/sm-alpr/blob/e138455bb5582041266ea613ccfb74d8fcf737c4/assets/san-mateo-public-records/W012818-053026/4_1_2026-4_30_2026-San_Mateo_CA_PD-Audit__1_.pdf
(SHA-256 39166d103a5e587800881725f821be53110bca575acf5a749b06c3eec797306b)
```

**Event log** (Santa Rosa PD network-share log, `row_no` 3):

```
Santa Rosa CA PD. MuckRock request 214823 (https://www.muckrock.com/foi/santa-rosa-3437/flock-safety-alpr-records-contracts-audit-logs-sb-34-communications-santa-rosa-police-department-214823/),
produced 2026-09-01. 26-996_2026-09-01_00_44_52_-0700.zip > Event Logs - Network Share Log.csv, row 4. Download:
https://cdn.muckrock.com/foia_files/2026/09/01/26-996_2026-09-01_00_44_52_-0700.zip (SHA-256 b9447e6509a89c90fdf6c2890c210125eec2e62f3f06a47e5434440f45bfffd9);
Event Logs - Network Share Log.csv inside it: SHA-256 12f5bf960cdf05eee1314110492a7242f590632f4ec37b15c5d321355d9a0396
```

A layout-corrected row is in [Citing a layout-corrected row](#citing-a-layout-corrected-row).

## Public and verbatim names

Folders inside an agency's zip can carry personal names (a folder named after a requester or an employee, for
example). Everything that leaves the machine uses the public forms; the verbatim forms are for opening the file
locally.

| Public form | Verbatim form (local only) | Difference |
|---|---|---|
| `public_release_id` | `release_id` | Folders of the zip member removed: `mr:<request>:<zip>!<member file name>#<sheet>`. Unique (the build fails otherwise) |
| `document` (`<zip> > <member file name>`) | `document_verbatim` (`<zip> > <full member path>`) | Same |
| `member_file` | `member` | Same |

In this build 325 zip members sit in folders, so their public and verbatim forms differ; for every other release they
are identical. The Marin release above is one of them:

| Column | Value |
|---|---|
| `release_id` | `mr:181227:25-112_2025-03-19_21_09_10_-0700.zip!To Release/Marin County CA SO_Network_Audit_6_1_2024_7_1_2024_REDACTED.xlsx#Marin County CA SO_Network_Audi` |
| `public_release_id` | `mr:181227:25-112_2025-03-19_21_09_10_-0700.zip!Marin County CA SO_Network_Audit_6_1_2024_7_1_2024_REDACTED.xlsx#Marin County CA SO_Network_Audi` |

The member's SHA-256 identifies it inside the zip without its path. To go back from a public id:
`SELECT release_id FROM release_sources WHERE public_release_id = ?`. Zip file names are in the public MuckRock URL
already. Cell values are a separate question: see [pii.md](pii.md).

## Where rows come from

Four kinds of source, told apart by the prefix of `release_id`. Release and row counts: [coverage.md](coverage.md).

| Prefix | What the reader opens | `container_root` | `src_row` means (`src_row_basis`) | `link` | Where to get it |
|---|---|---|---|---|---|
| `mr:` MuckRock | The agency's CSV or workbook sheet, often inside a zip | `evidence` | Exact row in the original: spreadsheet row, or CSV record with header = row 1 | `Download:` | MuckRock request page; `cdn.muckrock.com`; local copy under `evidence_dir` |
| `la:` Los Altos PD | The agency workbook, sheet in `sheet` | `repo` | Workbook row = NDJSON line + 1 | `Committed conversion of the workbook` | Workbook: Los Altos NextRequest portal (`request_url`, public). Conversion: GitHub |
| `rwc:` Redwood City PD | The agency workbook (its one Flock sheet; `sheet` not recorded) | `repo` | Workbook row = NDJSON line + 1 (+ 0 for the header-less `rwc:PRA_26_217_4th_Release_Dec2023`) | `Committed conversion of the workbook` | Workbook: not public (see walkthrough c). Conversion: GitHub |
| `smpd:` San Mateo PD | One monthly audit PDF SMPD produced | `repo` | None: page `src_page`, text-layer line `src_line`; the search id is printed there | `Document:` | PDF committed in the repo (GitHub permalink) |

`release_id` formats: `mr:<request>:<file>[!<zip member path>]#<sheet>` (`#csv` for CSV), `la:<request>:<NDJSON stem>`,
`rwc:<NDJSON stem>`, `smpd:<request folder>:<PDF file name>`. `pra_id`: `muckrock-181227`, `26-366`, `26-217`,
`W012818-053026`.

## Provenance fields

### `releases` (layer 1, `truth`)

One row per released file or sheet. Every column:

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `release_id` | VARCHAR | Key; spells out request, file, zip member path and sheet | Local only when the member sits in folders |
| `producer` | VARCHAR | Organization whose log this is | Not the searching org (`sightings.org`) |
| `producer_agency_id` | VARCHAR | Registry UUID of the producer (`assets/agency_registry.json`) | |
| `producer_basis` | VARCHAR | How `producer` was decided | `producers.json`, `filename`, `own-search dominant Org Name`, `fixed (dedicated loader)`, `fixed (SMPD PRA)` |
| `producer_source` | VARCHAR | The citation in `producers.json` that attributes the file | Set only when `producer_basis = 'producers.json'` |
| `audit` | VARCHAR | Log type | `network`, `own`, `event` |
| `pra_id` | VARCHAR | The request | |
| `container_root` | VARCHAR | Where `container_path` is rooted | `evidence` = `build_info.evidence_dir`; `repo` = `build_info.repo_checkout` at `repo_commit` |
| `container_path` | VARCHAR | The downloaded file (zip, CSV, workbook) or committed file, relative to its root | MuckRock: `<request>-<agency>/<file>`, as in `MANIFEST_v2.txt` |
| `member` | VARCHAR | Full path of the file inside a zip | NULL when not in a zip. Local only (folders) |
| `member_sha256` | VARCHAR | SHA-256 of the zip member's bytes | NULL when not in a zip |
| `sheet` | VARCHAR | Worksheet name | NULL for CSV, Redwood City and SMPD |
| `source_file` | VARCHAR | File name of the document itself | `la:`/`rwc:`: the agency workbook the NDJSON was converted from; `smpd:`: the PDF |
| `container_sha256` | VARCHAR | SHA-256 of the file at `container_path` | MuckRock: matches `MANIFEST_v2.txt`. Repo: the committed file (`.ndjson.gz` or PDF), not the agency workbook |
| `header` | VARCHAR[] | Column names as loaded (Flock superset names, aliases mapped, blanks named) | MuckRock: position i = column i of the original, counted from its first used column |
| `header_raw` | VARCHAR[] | Header row as released (leading spaces kept: `' Org Name'`) | SMPD: the printed labels; Redwood City: see Known limits |
| `header_basis` | VARCHAR | Where `header_raw` came from | |
| `n_rows` | BIGINT | Data rows loaded | 0 for the two image-only SMPD PDFs |
| `content_sha256` | VARCHAR | Hash for grouping identical re-releases | CSV: the bytes. Spreadsheet: the non-empty data rows' values. Repo NDJSON: the decompressed NDJSON. SMPD: the PDF |
| `released_on` | DATE | Production date | NULL = not recorded |
| `released_on_basis` | VARCHAR | Source of `released_on` | `MuckRock attachment date`; `Los Altos README: PRA … closed …`; not recorded for Redwood City and SMPD (the text says why) |
| `source_url` | VARCHAR | Direct download | `https://cdn.muckrock.com/…` for every `mr:` release; NULL for repo |
| `request_url` | VARCHAR | The request's public page | MuckRock; Los Altos NextRequest; NULL for Redwood City and SMPD |
| `src_row_basis` | VARCHAR | What `src_row` counts, in words | Five values, quoted in the walkthroughs |

### Row keys: `row_no`, `src_row`, `src_page`

| Table | `row_no` | Where a reader finds the row |
|---|---|---|
| `truth.flock_audit_rows`, `truth.flock_event_rows` (MuckRock) | Load order: 1 = first non-blank data row after the header | `src_row`: spreadsheet row, or CSV record with the header as row 1. Blank rows and rows above the header keep their numbers, so `src_row` can run ahead of `row_no` |
| `truth.flock_audit_rows` (`la:`, `rwc:`) | NDJSON line number | `src_row = row_no + 1`; `row_no + 0` for the header-less Redwood City release |
| `truth.smpd_pdf_rows` | Block order in the PDF's text layer (1-based) | `src_page`; the search id `id` is printed on that page. `src_row` is NULL |

`(release_id, row_no)` is the database key. `src_row` (or page and search id) goes in a citation.

### `release_sources` (layer 2 view)

Everything needed to find, fetch and verify one release. Every column:

| Column | Type | Meaning |
|---|---|---|
| `release_id` | VARCHAR | As in `releases` (local only) |
| `public_release_id` | VARCHAR | `release_id` without the zip member's folders; unique |
| `producer`, `producer_agency_id`, `audit`, `pra_id` | VARCHAR | As in `releases` |
| `request_label` | VARCHAR | `MuckRock request 181227`, `San Mateo public records request W012818-053026`, `Los Altos CA PD public records request 26-366` |
| `request_url`, `released_on`, `released_on_basis` | VARCHAR, DATE, VARCHAR | As in `releases` |
| `container_file` | VARCHAR | Last path component of `container_path`: the file you download |
| `member` | VARCHAR | Full member path (local only) |
| `member_file` | VARCHAR | The member's file name, no folders |
| `sheet`, `source_file` | VARCHAR | As in `releases` |
| `document` | VARCHAR | Public form: `<container_file> > <member_file>`, else `source_file` (the agency workbook for repo NDJSON) |
| `document_verbatim` | VARCHAR | `<container_file> > <member>` with the full path (local only), else as `document` |
| `container_root`, `container_path`, `source_url` | VARCHAR | As in `releases` |
| `repo_url` | VARCHAR | Repo releases: `<repo_web_url>/blob/<repo_commit>/<container_path>` (URL-escaped). Raw bytes: `https://raw.githubusercontent.com/none-below/sm-alpr/<repo_commit>/<container_path>` |
| `container_sha256`, `member_sha256`, `content_sha256`, `src_row_basis` | VARCHAR | As in `releases` |
| `link` | VARCHAR | Where to get it + the hashes to check it (three forms, above) |

### `sighting_sources` (layer 2 view)

One row per search row in any log, with its location and citation: the union of `sighting_sources_flock` (from
`truth.flock_audit_rows`) and `sighting_sources_smpd` (from `truth.smpd_pdf_rows`). A filter on `release_id` prunes
both branches. No cell values, except the SMPD search id in `locator`. Every column:

| Column | Type | Meaning |
|---|---|---|
| `sighting_id` | UBIGINT | `hash(release_id, row_no)`; joins `sightings`, `cache.sighting_event`. Working key only |
| `release_id`, `public_release_id` | VARCHAR | Release (local), and its public form |
| `row_no` | BIGINT | Database row key |
| `src_row` | BIGINT | Row as a reader sees it; NULL for SMPD |
| `pdf_pages` | VARCHAR | SMPD: `<pdf file> page <n>`; NULL otherwise |
| `producer`, `request_label`, `request_url`, `released_on` | VARCHAR (`released_on` DATE) | From `release_sources` |
| `document`, `document_verbatim`, `sheet` | VARCHAR | From `release_sources`; `sheet` NULL for SMPD |
| `layout_corrected` | BOOLEAN | The row is covered by a `truth.release_layouts` entry: some values sit under other labels in the original |
| `locator` | VARCHAR | `row <src_row>` (+ layout note), or SMPD `page <src_page> (search id <id>)` |
| `open_url` | VARCHAR | The one link to click: MuckRock download, or GitHub permalink to the NDJSON or PDF |
| `sha256` | VARCHAR | SHA-256 of the file at `open_url` (`container_sha256`) |
| `member_sha256` | VARCHAR | SHA-256 of the zip member; NULL when not in a zip |
| `src_row_basis`, `link` | VARCHAR | From `release_sources` |
| `citation` | VARCHAR | `cite_text()` of the above |

`cite_text(producer, request_label, request_url, released_on, document, sheet, locator, link)` returns
`<producer>. <request_label> (<request_url>), produced <YYYY-MM-DD>. <document>, sheet "<sheet>", <locator>. <link>`;
NULL parts drop out.

### `event_log` (layer 2 view)

Flock event-log rows (users created or deleted, network-sharing changes) with a citation. Every column:

| Column | Type | Meaning |
|---|---|---|
| `release_id`, `public_release_id` | VARCHAR | Release (`audit = 'event'`), and its public form |
| `row_no`, `src_row` | BIGINT | Database key; row as a reader sees it |
| `producer` | VARCHAR | Whose event log |
| `ts` | TIMESTAMP | `Timestamp`, parsed, UTC (ISO offsets converted) |
| `user`, `event_type`, `entity_type`, `entity_details`, `event_log_id` | VARCHAR | `User`, `Event Type`, `Entity Type`, `Entity Details`, `Event Id` cells, verbatim. `user` and `entity_details` can hold agency users' names or e-mail addresses: local only |
| `extra` | JSON | Released columns outside the six standard ones |
| `citation` | VARCHAR | `cite_text()` with `row <src_row>` |

Match `Event Id` and `Timestamp` when you check a row against the original.

### `truth.smpd_pdf_rows` (layer 1)

San Mateo PD's own search log, read from the PDFs it produced (requests W012541-041426 and W012818-053026) by
`smpd_pdf_loader.py` (pymupdf text layer). One release per PDF; one row per search-id block as printed; no block is
dropped. Every column:

| Column | Type | Meaning |
|---|---|---|
| `release_id` | VARCHAR | `smpd:<request folder>:<pdf name>` |
| `row_no` | BIGINT | Block order in the text layer, 1-based |
| `src_page` | INTEGER | 1-based page the search id is printed on |
| `src_line` | INTEGER | 1-based index of the id's line among the page's non-empty text-layer lines (for tools; a reader searches the page for the id) |
| `id` | VARCHAR | Search id (Flock search UUID), verbatim |
| `user_line` | VARCHAR | `userID` cell line, verbatim |
| `count_time_line` | VARCHAR | `<networkCount> MM/DD/YYYY, HH:MM:SS AM/PM UTC`, verbatim (the PDF prints both cells on one line) |
| `reason_line` | VARCHAR | `Reason` cell, verbatim (trailing spaces kept); NULL when the cell is blank. Can hold plates: local only |
| `parse_note` | VARCHAR | NULL for a normal block; otherwise how the block was read (below) |

The printed labels are in `releases.header_raw`; `header` gives the Flock names (`ID`, `Name`, `Total Networks
Searched`, `Search Time`, `Reason`).

### `release_content_groups` (layer 2 view)

Releases whose content is identical (`content_sha256`), groups of two or more: `content_sha256`, `n_releases`,
`releases` (earliest `released_on` first), `released_on`. CSV, spreadsheet, NDJSON and PDF hashes are computed
differently, so the same rows in two formats do not group.

## Walkthroughs

Each example is a real row; only cells with no civilian data are shown. For any row, match `ID` (the Flock search
UUID, when the release has one), `Search Time` (to the second, UTC), `Org Name` and `Total Networks Searched`; for
event-log rows `Event Id` and `Timestamp`; for SMPD the search id and the count/time line.

Helpers that read a MuckRock original with a different reader from the loader (openpyxl or Python's `csv`; the loader
uses calamine), and a committed NDJSON line. Run with `uv run --locked --project <code> python`:

```python
import csv, gzip, io, json, zipfile
from pathlib import Path
import openpyxl

info = dict(con.execute("SELECT key, value FROM truth.build_info").fetchall())
EV, REPO = Path(info["evidence_dir"]), Path(info["repo_checkout"])

def cell(v):
    """A workbook cell as text, the way truth stores it: whole numbers without '.0', empty as ''."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)

def original_rows(release_id, src_rows):
    """{src_row: [cells]} of a MuckRock original, read in one pass with openpyxl / csv (not the loader's reader)."""
    path, member, sheet = con.execute(
        "SELECT container_path, member, sheet FROM truth.releases WHERE release_id = ?", [release_id]).fetchone()
    if (member or path).lower().endswith(".xlsb"):
        raise ValueError(".xlsb: openpyxl cannot read it; open it in Excel or with python-calamine")
    data = zipfile.ZipFile(EV / path).read(member) if member else (EV / path).read_bytes()
    want, last, out = set(src_rows), max(src_rows), {}
    if data[:4] == b"PK\x03\x04":                                   # .xlsx
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = (wb[sheet] if sheet else wb.worksheets[0]).iter_rows(max_row=last, values_only=True)
    else:                                                          # CSV: src_row counts records, not text lines
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252")
        rows = csv.reader(io.StringIO(text, newline=""))
    for i, r in enumerate(rows, start=1):
        if i in want:
            out[i] = [cell(v) for v in r]
        if i >= last:
            break
    return out

def ndjson_line(release_id, row_no):
    """The committed NDJSON line behind a repo row (Los Altos, Redwood City): line number = row_no."""
    path = con.execute("SELECT container_path FROM truth.releases WHERE release_id = ?", [release_id]).fetchone()[0]
    with gzip.open(REPO / path, "rt", encoding="utf-8") as fh:
        for i, line in enumerate(fh, start=1):
            if i == row_no:
                return json.loads(line)

SAFE = ["ID", "Org Name", "Total Networks Searched", "Search Time"]   # cells with no civilian data
def db_row(release_id, row_no, cols=SAFE):
    """src_row, header and the stored (verbatim) cells of one truth row."""
    header = con.execute("SELECT header FROM truth.releases WHERE release_id = ?", [release_id]).fetchone()[0]
    cols = [c for c in cols if c in header]
    src_row, *vals = con.execute(f"SELECT src_row, {', '.join(chr(34) + c + chr(34) for c in cols)} FROM truth.flock_audit_rows "
                                 "WHERE release_id = ? AND row_no = ?", [release_id, row_no]).fetchone()
    return src_row, header, dict(zip(cols, vals))
```

`header` position i is column i of the original, so `orig[header.index(c)]` is the released cell under that label
(for a sheet whose header starts in column A, as Flock exports do). The comparisons below are exact: truth stores
cells verbatim. The helper reads the file up to the wanted row once; a row near 364,000 of a Santa Rosa sheet took
12 s. The four `.xlsb` releases need python-calamine or Excel.

### (a) MuckRock: a worksheet inside a zip

Marin County SO's network audit for June 2024, MuckRock request 181227.

```python
rid = ("mr:181227:25-112_2025-03-19_21_09_10_-0700.zip!To Release/Marin County CA SO_Network_Audit_6_1_2024_7_1_2024_REDACTED.xlsx"
       "#Marin County CA SO_Network_Audi")
src_row, header, db = db_row(rid, 3)
orig = original_rows(rid, [src_row])[src_row]
print(src_row, db == {c: orig[header.index(c)] for c in db}, db)
# 4 True {'Org Name': 'Alameda County CA SO', 'Total Networks Searched': '5109', 'Search Time': '6/26/2024, 05:52:14 PM UTC'}
```

- **Get it.** Request page `request_url`, which lists the attachment; direct download `source_url`
  (`https://cdn.muckrock.com/foia_files/2025/03/20/25-112_2025-03-19_21_09_10_-0700.zip`); local copy
  `<evidence_dir>/181227-marin-county-sheriff/25-112_2025-03-19_21_09_10_-0700.zip`.
- **Open it.** Unzip; the member is `Marin County CA SO_Network_Audit_6_1_2024_7_1_2024_REDACTED.xlsx` (the full
  path inside the zip is `document_verbatim`); sheet `Marin County CA SO_Network_Audi`; row 4 (Excel: Ctrl+G, `A4`).
  This release has no `ID` column. The workbook holds `5109` as a number; truth stores the text `'5109'`.
- **Hash.** Zip `5e283194…82ab` = `sha256` = its `MANIFEST_v2.txt` line; member `9e24f18d…7831` = `member_sha256`
  (`unzip -p <zip> '<member>' | shasum -a 256`, with the full path from `member`; checked 2026-09-26).

`src_row_basis`: `spreadsheet row number in the named sheet (1-based, as shown by Excel)`.

### (b) MuckRock: a CSV

Mountain View PD's network audit for December 2025, MuckRock request 197131.

```python
rid = "mr:197131:Mountain_View_CA_PD_Santa_Clara_County_network_Dec_2025.csv#csv"
src_row, header, db = db_row(rid, 10)
orig = original_rows(rid, [src_row])[src_row]
print(src_row, db == {c: orig[header.index(c)] for c in db}, db["ID"])
# 11 True 4cf5833b-32bf-484c-8603-e2c877f6cecb
```

- **Open it.** In a spreadsheet application, row 11. `src_row_basis`: `CSV record number, header = row 1 (the row
  shown when the file is opened in a spreadsheet); decoded utf-8`. In a text editor the line number differs: `Time
  Frame` cells hold a line break, so one record spans several text lines (`sed -n 11p` shows the wrong record).
- **Confirm.** `ID` `4cf5833b-32bf-484c-8603-e2c877f6cecb`, `Search Time` `12/07/2025, 06:04:58 AM UTC`, `Org Name`
  Riverside County CA SO, `Total Networks Searched` 928. The released header has leading spaces (`' Org Name'`);
  `header_raw` keeps them.
- **Other logs.** The row is event `u:4cf5833b-32bf-484c-8603-e2c877f6cecb` (`1_uuid`): 16 sightings in 10 producers'
  logs. `ac.drill(con, eid)` gives each one's citation, so a claim about this search can cite several agencies' own
  records. The same CSV was also produced in request 197815 (`release_content_groups`).

### (c) Repo NDJSON: Los Altos and Redwood City

The agency workbooks are too large for git. The repo commits a conversion (`scripts/xlsx_to_audit_ndjson.py`): one
JSON object per workbook row, keys = the workbook's header labels, empty cells omitted, text trimmed, `***` and
`REDACTED` kept. The citation names the agency workbook (`document`) and links the conversion (`Committed conversion
of the workbook …`); `sha256` is the conversion's hash, not the workbook's.

```python
rid = "la:26-366:Los_Altos_PD_Network_Audit_2025__JANUARY"
src_row, header, db = db_row(rid, 5)
line = ndjson_line(rid, 5)                   # NDJSON line = row_no; workbook row = src_row
print(src_row, db, {c: line.get(c) for c in db})
# 6 {'Org Name': 'Arizona Department of Public Safety', 'Total Networks Searched': '5817', 'Search Time': '01/10/2025, 03:11:44 PM UTC'}
#   {'Org Name': 'Arizona Department of Public Safety', 'Total Networks Searched': 5817, 'Search Time': '01/10/2025, 03:11:44 PM UTC'}
```

- **Get it.** The workbook `Los Altos PD Network Audit 2025.xlsx` is public on the city's NextRequest portal, no
  login: `request_url` `https://losaltosca.nextrequest.com/requests/26-366` (and `/requests/25-312` for the earlier
  production). The conversion: `open_url` (GitHub permalink at `e138455bb`), or raw
  `https://raw.githubusercontent.com/none-below/sm-alpr/e138455bb5582041266ea613ccfb74d8fcf737c4/assets/los-altos-pras/json/pra-26-366/Los_Altos_PD_Network_Audit_2025__JANUARY.ndjson.gz`.
- **Open it.** Workbook sheet `JANUARY`, row 6. In the conversion:
  `gzip -dc Los_Altos_PD_Network_Audit_2025__JANUARY.ndjson.gz | sed -n 5p`.
- **Confirm.** `Org Name`, `Total Networks Searched`, `Search Time` as above. Checked 2026-09-25 against the workbook
  itself (openpyxl): identical.

Redwood City PD, PRA 26-217: `rwc:PRA_26_217_2024_Q3` `row_no` 10 is `PRA 26-217 2024 Q3 (1).xlsx` row 11: San
Francisco CA PD, 270 networks, `7/25/2024, 11:54:30 PM UTC`. Checked 2026-09-25 against the agency workbook:
identical, on its only sheet `Redwood City CA PD_Network_Audi`. Differences from Los Altos:

- The agency workbooks are not posted anywhere public, and this database records no hash for them (`sha256` and
  `container_sha256` are the conversion's). The committed NDJSON is the only public, hash-pinned copy. For a filing,
  cite the workbook by file name and row with request number 26-217 or 26-741, and attach the NDJSON line or request
  the workbook from the Redwood City PD. The project owner holds the workbooks locally for checking a row.
- `request_url` and `released_on` are NULL (`released_on_basis` says why).
- `sheet` is NULL: the converter reads only the sheet whose header looks like a Flock export. Some workbooks also
  carry pivot-table tabs; ignore those.
- `rwc:PRA_26_217_4th_Release_Dec2023` was exported without a header row: `src_row` = NDJSON line, and its column
  labels were assigned by the converter in Flock's standard order, not released by the agency (`header_raw` NULL).
- Two workbooks contain rows hidden by a saved filter (`PRA 26-217 1st Release`, `PRA 26-217 2025 1`). Hidden rows
  keep their row numbers and are in the NDJSON; clear the filter in Excel to see them.

`src_row_basis`: `workbook row = NDJSON line + 1 (header on row 1); the committed conversion
(scripts/xlsx_to_audit_ndjson.py) drops all-empty rows, so exact unless the workbook has blank rows mid-sheet; it
trims cell text and omits empty cells`.

### (d) San Mateo PD: a PDF page

SMPD produced its own search log as monthly PDFs. Each PDF is one release, loaded straight from the committed file
into `truth.smpd_pdf_rows`; a row is cited by PDF page and search id.

```python
import subprocess
search_id = "8189f4a2-f9e9-44e4-8654-1fa531338455"
for rid, n, page, line in con.execute("SELECT release_id, row_no, src_page, src_line FROM truth.smpd_pdf_rows "
                                      "WHERE id = ?", [search_id]).fetchall():   # a search can be printed twice
    print(con.execute("SELECT locator, open_url, sha256 FROM sighting_sources WHERE release_id = ? AND row_no = ?",
                      [rid, n]).fetchone())
    path = con.execute("SELECT container_path FROM truth.releases WHERE release_id = ?", [rid]).fetchone()[0]
    text = subprocess.run(["pdftotext", "-layout", "-f", str(page), "-l", str(page), str(REPO / path), "-"],
                          capture_output=True, text=True, check=True).stdout
    print(n, page, line, search_id in text)
    print(con.execute("SELECT count_time_line, parse_note FROM truth.smpd_pdf_rows WHERE release_id = ? AND row_no = ?",
                      [rid, n]).fetchone())
# ('page 71 (search id 8189f4a2-f9e9-44e4-8654-1fa531338455)', 'https://github.com/none-below/sm-alpr/blob/e138455bb…/4_1_2026-4_30_2026-San_Mateo_CA_PD-Audit__1_.pdf', '39166d10…306b')
# 3174 71 97 True
# ('868 04/02/2026, 11:33:20 PM UTC', None)
```

- **Get it.** `open_url` (GitHub permalink to the PDF at `e138455bb`, 78 pages); raw
  `https://raw.githubusercontent.com/none-below/sm-alpr/e138455bb5582041266ea613ccfb74d8fcf737c4/assets/san-mateo-public-records/W012818-053026/4_1_2026-4_30_2026-San_Mateo_CA_PD-Audit__1_.pdf`
  hashed to `39166d10…306b` on 2026-09-26. No public request page is recorded (`request_url` NULL); the request's
  message history is committed in the same folder.
- **Open it.** Page 71; search the page for the id. The printed columns are `ID`, `userID`, `networkCount`,
  `Search Time`, `Reason`.
- **Confirm.** The row shows the id, `868` and `04/02/2026, 11:33:20 PM UTC`; `sightings_smpd` parses them to `nets`
  868 and `t` 2026-04-02 23:33:20 (UTC).
- `released_on` is NULL: W012541 is a rolling production and the per-PDF release dates are not recorded here. Take the
  date from the message history when a filing needs it.

**Acrobat-edited pages (`parse_note`).** SMPD edited Reason cells in Acrobat before producing some PDFs. The re-save
moves the edited page's text out of reading order (reason lines collect after the page's last row), so a reader that
follows text order gives a Reason to the wrong row or swallows the next row. The loader assigns each line to the
printed row whose vertical band holds it. Where text order and the printed row disagree, the printed row wins and
`parse_note` names the page line of each cell. 69 rows in this build, in seven PDFs, for example:

```
smpd:W012541-041426:1_1_2025-1_31_2025-San_Mateo_CA_PD-Audit__Part_1_.pdf  row_no 1686  page 47:
text order differs from the printed row: cells read by position on page 47 (userID line 122, count/time line 123, reason line 127)
```

The citation is unchanged: page and search id. Quote the Reason from the printed row in the PDF. Other `parse_note`
forms (`read in text order only …`, `printed row holds N line(s) …`, `block continues on page N …`) flag blocks the
loader could not read by position; none occur in this build.

- **Do not cite the repo's merged JSON** (`pra-W012541-041426.json`). It was built from 28 of the 32 PDFs with rows,
  and on Acrobat-edited pages it holds 35 wrong reasons (33 are the next row's UUID, 2 a neighbour's reason) and is
  missing 33 rows. The PDF loader reads the printed row.
- **Repeats.** 179 rows repeat a search id already printed in the same PDF; no id is printed in two PDFs in this
  build. Each printing is its own sighting of one event: count distinct `flock_id` or `event_id`, not rows.
- **June 2024.** The two June 2024 PDFs (`…6_1_2024-6_30_2024-San_Mateo_CA_PD-Audit_-_PART_1.pdf`, `PART_2`) are
  image-only: no text layer, `n_rows` 0, `header_basis` says so. OCR is not loaded, so June 2024 SMPD searches are
  not in the database.

`src_row_basis`: `PDF page src_page, line src_line of that page; the search id is printed there`.

### (e) Event-log rows

```python
erid = "mr:214823:26-996_2026-09-01_00_44_52_-0700.zip!Event Logs - Network Share Log.csv#csv"
print(con.execute("SELECT public_release_id, src_row, ts, event_type, entity_type, event_log_id FROM event_log "
                  "WHERE release_id = ? AND row_no = 3", [erid]).fetchone())
# ('mr:214823:26-996_2026-09-01_00_44_52_-0700.zip!Event Logs - Network Share Log.csv#csv', 4,
#  datetime.datetime(2026, 2, 12, 18, 46, 31), 'delete', 'networkShare', '15e9d015-b912-4253-9bdb-b9bca141fb3d')
```

Match `Event Id` and `Timestamp` in row 4 of the CSV inside the zip. The same CSV was also produced in the 2026-08-20
zip (`release_content_groups`).

## Citing a layout-corrected row

Some releases put values under the wrong header labels: an export with a missing cell, or two exports stacked with no
second header row. Truth keeps the cells as released; `truth.release_layouts` (from `layouts.json`, each entry with its
evidence in `source`) says, for a row range or listed rows, which label's cell holds each field, and the derived views
read the field from there. For such a row, `layout_corrected` is true and the citation says so:

```
Santa Rosa CA PD. MuckRock request 214823 (https://www.muckrock.com/foi/santa-rosa-3437/flock-safety-alpr-records-contracts-audit-logs-sb-34-communications-santa-rosa-police-department-214823/),
produced 2026-08-20. 26-996_2026-08-20_232659_-0700.zip > REDACTED_5_1_2026-5_31_2026-Santa Rosa CA PD-Network-Audit.xlsx,
sheet "5_1_2026-5_31_2026-Santa Rosa C", row 363784; cells released under other column labels (see release_layouts).
Download: https://cdn.muckrock.com/foia_files/2026/08/21/26-996_2026-08-20_232659_-0700.zip (SHA-256 ce76b3126691a06338536567326dcaf6a3b94287bd512900b35c03b6d745b6d0);
REDACTED_5_1_2026-5_31_2026-Santa Rosa CA PD-Network-Audit.xlsx inside it: SHA-256 bf4932d948401c77fcb2547acc8df6cbfcfc2687fb9a6eee3e0bb05148ac4acd
```

Which released column holds each field for one row (the same rule the views use: the layout entry covering the row,
later-starting first; fields not listed stay under their own label):

```python
COLS = """
WITH x AS (SELECT a.src_row, m.layouts FROM truth.flock_audit_rows a JOIN release_meta m USING (release_id)
           WHERE a.release_id = $rid AND a.row_no = $n),
lay AS (SELECT list_filter(layouts, l -> src_row >= l.src_row_from
                                     AND (l.src_row_to IS NULL OR src_row <= l.src_row_to))[1].mapping AS mapping FROM x),
f AS (SELECT unnest(map_keys(mapping)) AS field, unnest(map_values(mapping)) AS label FROM lay),
p AS (SELECT f.field, f.label, list_position(r.header, f.label) AS col, r.header_raw
      FROM f, truth.releases r WHERE r.release_id = $rid)
SELECT field, label AS read_from_label, col AS col_no,
       CASE WHEN col <= 26 THEN chr(64 + col) ELSE chr(64 + (col - 1) // 26) || chr(65 + (col - 1) % 26) END AS excel_col,
       header_raw[col] AS printed_label
FROM p ORDER BY col NULLS LAST, field"""
rid = ("mr:214823:26-996_2026-08-20_232659_-0700.zip!REDACTED_5_1_2026-5_31_2026-Santa Rosa CA PD-Network-Audit.xlsx"
       "#5_1_2026-5_31_2026-Santa Rosa C")
for r in con.execute(COLS, {"rid": rid, "n": 363783}).fetchall():
    print(r)
# ('Total Networks Searched', 'Time Frame', 5, 'E', ' Time Frame')
# ('License Plate', 'License Plate', 6, 'F', ' License Plate')
# ('Time Frame', 'Reason', 7, 'G', ' Reason')
# ('Case #', 'Case #', 8, 'H', ' Case #')
# ('Filters', 'Filters', 9, 'I', ' Filters')
# ('Reason', 'Search Time', 10, 'J', ' Search Time')
# ('Search Time', 'Search Type', 11, 'K', ' Search Type')
# ('Text Prompt', 'Text Prompt', 12, 'L', ' Text Prompt')
# ('Search Type', 'Moderation', 13, 'M', ' Moderation')
# ('Moderation', None, None, None, None)                     # no cell holds it in these rows
print(con.execute("SELECT src_row_from, src_row_to, source FROM truth.release_layouts WHERE ? LIKE release_pattern",
                  [rid]).fetchall())                           # the range and the evidence for the correction
```

- Match on `header`, not `header_raw`: the mapping uses the loaded names, and released labels can carry leading
  spaces. The letter assumes the header starts in column A (Flock exports do; check the header row).
- A mapping value that is not a header name is an unlabeled cell kept in `extra`, named by its 0-based column
  (`column14` = the 15th column, O); the query returns NULL for it. No current layout uses one.
- Checked against the workbook (openpyxl, 2026-09-26): row 363784 has the `ID` in A, `680` in E and
  `05/06/2026, 06:03:25 PM UTC` in K, equal to the corrected `Total Networks Searched` and `Search Time` in
  `flock_rows`, and `search` in M.
- Cite the cell by row and column letter and state the correction, e.g. "…, row 363784, column K (labelled "Search
  Type"; from row 92527 the sheet's values sit under other labels: two exports stacked without a second header row)".
  Name the evidence (`release_layouts.source`) in the methods note.
- Affected releases: `SELECT DISTINCT r.release_id FROM truth.releases r JOIN truth.release_layouts l ON r.release_id
  LIKE l.release_pattern`. In this build: Cathedral City `PRA25-746.csv`; San Bruno's April 2024 own-search audit;
  Santa Rosa's February 2026 network audit (19 listed rows, both productions: the Moderation verdict under `Search
  Time`, no cell holds the search time, so `t` is NULL); Santa Rosa's May 2026 network audit from row 92527 (both
  productions). [semantics.md](semantics.md), "Layout corrections", has the detail.

## Verification and chain of custody

### 1. The file is the one the database read

```sh
cd <evidence_dir>          # truth.build_info.evidence_dir
shasum -a 256 181227-marin-county-sheriff/25-112_2025-03-19_21_09_10_-0700.zip
grep -F '181227-marin-county-sheriff/25-112_2025-03-19_21_09_10_-0700.zip' MANIFEST_v2.txt
unzip -p 181227-marin-county-sheriff/25-112_2025-03-19_21_09_10_-0700.zip \
  'To Release/Marin County CA SO_Network_Audit_6_1_2024_7_1_2024_REDACTED.xlsx' | shasum -a 256
```

- Three values must agree: the hash you compute, `releases.container_sha256`, and the first field of the
  `MANIFEST_v2.txt` line (`<sha256>  <local_path>  <source_url>`; one header line, then one line per downloaded file).
- The member's hash must equal `member_sha256`. The manifest lists containers, not members: the member is covered
  because it is inside a zip whose hash is in the time-stamped manifest.
- A copy downloaded fresh from `source_url` should hash the same. If it does not, MuckRock is serving a different
  file, and the local copy is the one the database describes.
- Repo files: `sha256` is the committed file's hash; `git log` on the path shows when it was committed and whether it
  changed since; `repo_url` pins the build's commit.

### 2. RFC 3161 timestamp (FreeTSA) on the manifest

`MANIFEST_v2.tsq` is the request, `MANIFEST_v2.tsr` the signed reply, `tsa.crt` / `cacert.pem` FreeTSA's
certificates, all in the evidence directory. Re-run 2026-09-26:

```sh
cd <evidence_dir>
shasum -a 256 MANIFEST_v2.txt
#   47a8baec8a6944c9cb445ddd707de872fbb38629008ba4e91e2eec20e388fa1e
openssl ts -reply -in MANIFEST_v2.tsr -text
#   Status: Granted.  Hash Algorithm: sha256  Message data: 47 a8 ba ec … fa 1e (the hash above)
#   Time stamp: Sep 25 06:44:33 2026 GMT   TSA: … CN=www.freetsa.org …
openssl ts -verify -in MANIFEST_v2.tsr -data MANIFEST_v2.txt -CAfile cacert.pem -untrusted tsa.crt
#   Verification: OK
openssl ts -verify -in MANIFEST_v2.tsr -queryfile MANIFEST_v2.tsq -CAfile cacert.pem -untrusted tsa.crt
#   Verification: OK
```

Do not rely only on the certificates saved beside the token. Fetch them from FreeTSA and compare fingerprints:

```sh
curl -sO https://freetsa.org/files/cacert.pem && openssl x509 -in cacert.pem -noout -fingerprint -sha256
#   A6:37:9E:7C:EC:C0:5F:AA:3C:BF:07:60:13:D7:45:E3:27:BB:BA:A3:8C:0B:9A:F2:24:69:D4:70:1D:18:AA:BC
curl -sO https://freetsa.org/files/tsa.crt && openssl x509 -in tsa.crt -noout -fingerprint -sha256
#   32:E8:41:A9:5C:C1:16:41:01:FF:DE:41:29:8E:F2:FC:75:C1:C4:37:2E:F0:95:E8:8A:6B:BD:47:DF:B1:91:FC
```

Both matched the saved copies on 2026-09-25. (macOS ships LibreSSL as `openssl`; these commands work with it.)
`MANIFEST.txt` and its stamps (FreeTSA 2026-09-25 04:27:10 GMT, verified OK) are the first capture, superseded by v2
after same-name re-sends were re-fetched; see `PROVENANCE.md` in the evidence directory.

### 3. OpenTimestamps (Bitcoin) on the manifest

The `.ots` proofs in the evidence directory have been upgraded: each carries Bitcoin block-header attestations. The
pre-upgrade proofs (calendar `PendingAttestation`s only) are kept as `*.ots.bak`.

| Proof | File SHA-256 | Bitcoin blocks | Earliest block time (UTC) |
|---|---|---|---|
| `MANIFEST_v2.txt.ots` | `47a8baec…fa1e` | 968519, 968537, 968555 | 2026-09-25 07:14:16 |
| `MANIFEST.txt.ots` | `e0ca30fb…c312` | 968498, 968499, 968537 | 2026-09-25 04:29:38 |

```sh
# pip install opentimestamps-client; on macOS `ots` lands in ~/Library/Python/3.x/bin (not on PATH by default)
cd <evidence_dir>
ots info MANIFEST_v2.txt.ots                  # lists BitcoinBlockHeaderAttestation(968519), (968537), (968555)
ots verify MANIFEST_v2.txt.ots                # full check; needs a local Bitcoin Core node
ots --no-bitcoin verify MANIFEST_v2.txt.ots   # no node: prints each block and the merkle root it must have
#   To verify manually, check that Bitcoin block 968519 has merkleroot e74a4973059f2afafed13aaa3bce0621971bea0ba378d9f5aaa9f62bc911be57
#   To verify manually, check that Bitcoin block 968537 has merkleroot 025e385f3578febe3ba0d0b4185150646dc35249c51ed9a66af9f36d89eed8b7
#   To verify manually, check that Bitcoin block 968555 has merkleroot ff3935acae85d700f8d3bc2bc534e834df7caa3b7a711bb264d38af3412e3833
for h in 968519 968537 968555; do     # the same roots from a public block explorer
  curl -s "https://blockstream.info/api/block/$(curl -s https://blockstream.info/api/block-height/$h)" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print($h, d['merkle_root'], d['timestamp'])"
done
```

`ots verify` looks for `MANIFEST_v2.txt` in the same directory. On 2026-09-26, run on byte-identical copies of both
proofs, `ots info` showed the attestations above and the merkle roots of all five blocks matched the public chain
(blockstream.info). Same commands for `MANIFEST.txt.ots` (blocks 968498 `e9bebc8f…dc2c`, 968499 `78de79bf…c321`,
968537).

### What the stamps prove

`MANIFEST_v2.txt`, and so every SHA-256 in it, existed by 2026-09-25 06:44:33 UTC (FreeTSA) and by the time of Bitcoin
block 968519, 07:14:16 UTC (a block's timestamp can differ from real time by up to about two hours). A file with the
same hash is therefore the file captured then. The stamps do not show what the agency sent: that rests on MuckRock
(the request page and the CDN copy) or the agency's own portal. No Wayback Machine captures were made (the Internet
Archive was offline during capture). Repo inputs are covered by git history instead: GitHub records the commit, and
`repo_url` pins it.

### 4. `verify_provenance.py`: rows against originals

```sh
nice -n 19 taskpolicy -b uv run --locked --project <code> python <code>/verify_provenance.py      # databases: --audit-dir, default <audit_db>
nice -n 19 taskpolicy -b uv run --locked --project <code> python <code>/verify_provenance.py \
  --only 'smpd:%' --smpd 400                  # SMPD only; needs poppler's pdftotext on PATH
```

Options: `--per-release N` rows within each release's first 3,000 (default 2); `--deep N` rows beyond row 3,000 in
`--deep-releases K` randomly chosen large MuckRock releases (default 2 in 16; `-1` = all, slow) plus every
layout-corrected release; `--per-layout N` rows inside each layout-corrected range (default 3); `--smpd N` SMPD rows
whose lines are checked (default 40); `--only PATTERN` (`release_id LIKE`); `--seed`.

What it checks:

- **MuckRock originals.** Re-opens the zip member or file with a reader other than the loader's (openpyxl for
  workbooks, Python `csv` for CSV; calamine, the loader's library, only for the four `.xlsb` workbooks openpyxl cannot
  open, counted separately as not independent). Goes to `src_row` and compares every cell exactly, no trimming,
  including the cells kept in `extra`. Fails a row whose original has non-empty cells beyond the header width. Compares
  the header row with `header_raw`.
- **Hashes.** Every container against `container_sha256` and `MANIFEST_v2.txt`; every zip member against
  `member_sha256`; CSV bytes against `content_sha256`.
- **Repo NDJSON (Los Altos, Redwood City).** Every release, sampled rows including deep ones, compared value by value
  with the committed NDJSON line (line = `src_row` − 1, or `src_row` for the header-less export): every key of the line
  must be stored, and no stored field may be missing from the line. Re-hashes the `.gz` and its decompressed content.
- **SMPD PDFs.** Re-hashes every PDF and re-reads it with poppler's `pdftotext -raw` (a different extractor from the
  loader's pymupdf). For every page, the search ids printed there must be exactly the ids truth puts on that page:
  every row, not a sample. For `--smpd` sampled rows, the stored lines must appear on `src_page` (or the next page, for
  a block split by a page break), compared whitespace-insensitively (poppler `-raw` drops spaces pymupdf keeps, e.g.
  `10/28/2025,10:48:17AMUTC`). Image-only PDFs: pdftotext must find no id either.
- It prints only release ids, row numbers, column names and counts, and exits 1 on any mismatch.

Latest full run, 2026-09-26, on this build: **all checked rows match their originals.**

- MuckRock: all 771 releases. 1,617 sampled rows read independently from the originals: csv module 480 releases /
  986 rows / 8,694 cells; openpyxl 287 releases / 631 rows / 6,524 cells (24 date-only cells where openpyxl's midnight
  datetime equals the staged date, an accepted reader equivalence). Plus calamine (the loader's library, not
  independent) 4 releases / 8 rows / 84 cells. Deep rows in 20 of the 516 large releases (16 at random plus the 4
  large layout-corrected ones).
- Re-hashed: 184 evidence containers (against `releases` and `MANIFEST_v2.txt`), 480 CSV contents, 535 zip members,
  102 repo NDJSON files and their decompressed contents, 34 SMPD PDFs: all match.
- Repo NDJSON: 102 releases, 334 rows, 3,010 cells against the committed NDJSON lines: all match.
- SMPD: all 110,705 search ids found printed on their `src_page`; 400 sampled rows / 1,186 lines matched their page
  text. The two June 2024 PDFs are image-only and have no rows.

Not covered:

- **The four `.xlsb` releases** are read with the loader's own library, so they are not independently checked.
- **Agency workbooks behind repo NDJSON.** The script checks truth against the committed conversion, not the
  conversion against the workbook. The walkthrough rows (Los Altos, Redwood City) were checked by hand against the
  workbooks on 2026-09-25: identical.
- **Sampling.** Apart from the SMPD ids, rows are sampled, not all checked.

### Evidence packs (planned)

A tool is planned that bundles the original files behind a query result (each cited file, its hashes and the
manifest/stamp proofs, with the cited rows marked) so a reporter can spot-check the rows without this database. It is
not built yet. Until then, give the reporter the `citation`s; each names the file, the row and the hash to check.

## Re-releases: which copy to cite

Every release is loaded in full, so the same search can sit in several releases of one producer: the same file
attached twice, or a month re-produced later with different masking. Cite the release you actually used, and say that
others exist.

- **Identical content.** `release_content_groups` lists these. Mountain View's December 2025 network audit is
  byte-identical in requests 197131 and 197815. Santa Rosa's December 2025 sheet and its network-share event log are
  identical in the 2026-08-20 and 2026-09-01 zips. Los Altos's January–July 2025 network sheets, and its
  organizational sheets for the same months except May, are identical in 25-312 and 26-366 (e.g.
  `la:25-312:Los_Altos_CA_PD_NETWORK_AUDIT_2025__JANUARY` and `la:26-366:Los_Altos_PD_Network_Audit_2025__JANUARY`);
  August 2025 and the May organizational sheet differ between the two. Cite one and add "also produced in …".
- **Same period, different content.** These do not group. Port Hueneme's December 2025 network audit was produced on
  2026-07-17 and again on 2026-09-21: 386,058 rows each, different `content_sha256`. Compare the two before quoting a
  cell, and cite the one whose value you quote. Find candidates by `producer`, `audit` and
  search-time range, or through `ac.drill()` (two rows from one producer in one event).
- **Different formats** never group together (hashes are computed per format): a MuckRock workbook and a repo
  NDJSON of the same rows are not linked by `release_content_groups`.
- **Counting.** Count distinct searches through `events` or `event_id`, or pick one release per period; never count raw
  rows across releases ([semantics.md](semantics.md)).

## Citation formats

Always include agency, request, production date, document (container and member), sheet or page, row, URL and
SHA-256. `citation` has all of these. The formats below add what an editor or a court expects. Use the public forms
(`document`, `member_file`); a folder path inside a zip (`document_verbatim`) can hold personal names, so check it
before putting it in a filing ([pii.md](pii.md)).

**Article** (footnote or methods note):

> Marin County Sheriff's Office, Flock "Network Audit" export for June 2024, produced 2025-03-20 in response to a
> California Public Records Act request (MuckRock request 181227,
> https://www.muckrock.com/foi/marin-county-3047/cpra-request-alpr-audit-and-data-sharing-181227/); file
> "Marin County CA SO_Network_Audit_6_1_2024_7_1_2024_REDACTED.xlsx", row 4.

In the methods note, name the database build (`built_at_utc`, `repo_commit`), say whether counts are distinct
searches or rows, list the requests behind them ([coverage.md](coverage.md)), and name any layout corrections used.

**Legal filing** (exhibit description or declaration):

> Marin County Sheriff's Office, Flock Safety "Network Audit" export, June 1 – July 1, 2024, produced 2025-03-20 in
> response to California Public Records Act request MuckRock No. 181227
> (https://www.muckrock.com/foi/marin-county-3047/cpra-request-alpr-audit-and-data-sharing-181227/), as file
> "Marin County CA SO_Network_Audit_6_1_2024_7_1_2024_REDACTED.xlsx" (SHA-256
> 9e24f18d3db2155d049baf27ea67ab9b098f9204bd6dd0f4b39c3a547db27831) inside archive
> "25-112_2025-03-19_21_09_10_-0700.zip" (retrieved from
> https://cdn.muckrock.com/foia_files/2025/03/20/25-112_2025-03-19_21_09_10_-0700.zip; SHA-256
> 5e283194c9cf0a8ed403f20273818a95a7ea971a2bff34f6a3e69da5c3f682ab), worksheet "Marin County CA SO_Network_Audi",
> row 4 (row numbers as displayed by Microsoft Excel, header row = 1). The archive's SHA-256 is listed in a manifest
> time-stamped by FreeTSA (RFC 3161) on 2026-09-25 06:44:33 UTC and anchored in Bitcoin block 968519.

- **SMPD:** replace file and row with the PDF and page: `San Mateo Police Department, Flock search-audit export for
  April 2026, produced in response to public records request W012818-053026 as
  "4_1_2026-4_30_2026-San_Mateo_CA_PD-Audit__1_.pdf" (SHA-256 39166d10…306b), page 71, row with search ID
  8189f4a2-f9e9-44e4-8654-1fa531338455`. Link the GitHub permalink as the copy.
- **Los Altos:** cite the workbook on the NextRequest portal with sheet and row; mention the GitHub NDJSON only as the
  machine-readable copy.
- **Redwood City:** cite the workbook by file name, row and request number (26-217 or 26-741); the NDJSON is the only
  public, hash-pinned copy (walkthrough c).
- **Layout-corrected rows:** add the column letter and the correction ([above](#citing-a-layout-corrected-row)).
- For an exhibit, attach the original file (or a print of the page or row), not a database extract.

## Known limits

- **`src_row` exactness by basis.**
  - MuckRock workbook and CSV rows: exact (sample-verified, deep rows included).
  - Repo NDJSON: exact unless the workbook has blank rows mid-sheet (the converter drops all-empty rows). Checked
    against the conversion by script, against the workbooks only by hand.
  - SMPD: no row number; page and search id. `src_line` counts text-layer lines, not printed rows.
  - CSV `src_row` counts records, not text lines.
- **Values as stored.** Truth is verbatim text: workbook numbers as text (`5109`; some Redwood City cells `164.0`),
  header labels with their leading spaces. Excel may display dates and numbers in its own format; the NDJSON
  conversion trims text. Compare meaning, not formatting.
- **Redwood City `header_raw`** is the NDJSON keys in workbook column order, not the released header row. The
  conversion omits empty cells, so a column empty in every row of a release has no key (2024 Q3's `header_raw` has no
  `ID`, `License Plate`, `Reason` or `Case #`). Check the workbook's header row before stating that a column was
  absent. NULL for the header-less release.
- **Redwood City workbooks** are not public and have no recorded hash (walkthrough c).
- **SMPD.** June 2024 is image-only and not loaded. Per-PDF production dates are not recorded. Cite the PDFs, not the
  repo's merged JSON.
- **Production dates.** Not recorded for Redwood City or SMPD. MuckRock's date is the attachment date on MuckRock,
  which can differ by a day from the agency's letter or the CDN path.
- **Not loaded.** MuckRock productions released only as PDFs (the loader reads tabular files), and records an agency
  posted off MuckRock (see `OFFSITE_LINKS.md` in the evidence directory).
- **Links rot; hashes do not.** MuckRock CDN URLs, NextRequest pages and GitHub permalinks can move or disappear. Keep
  the SHA-256 in every citation and keep your own copy of anything you cite. GitHub permalinks are pinned to a commit
  and resolve only if `repo_commit` has been pushed; `e138455bb` is on `origin/main`, and its raw PDF URL answered and
  hashed correctly on 2026-09-26. A build from an unpushed commit produces dead `repo_url`s.
- **`sighting_id` and non-`u:` event ids** come from DuckDB's `hash()` and this build's linking; `cache.builds`
  records the DuckDB version. Never publish them; cite `public_release_id` + `src_row` (or page and search id).

## Timings

Measured 2026-09-26 at the standard session (4 threads / 4 GB), niced, with a verification job running, OS cache
warm. A cold first query is slower.

| Lookup | Time |
|---|---|
| `sighting_sources` by `release_id` + `row_no` (any source) | 0.01–0.02 s |
| `ac.citations_for()`, 16 rows in 16 releases | 0.04 s |
| `cache.sighting_event` by `release_id` + `row_no` | 0.05 s |
| `sighting_sources` or `cache.sighting_event` by `sighting_id` | 0.07–0.95 s |
| `truth.smpd_pdf_rows` by `id` | under 0.01 s |
| `event_log` by `release_id` + `row_no` | under 0.01 s |
| `ac.event(eid)` / `SELECT * FROM event(eid)` | 0.45–0.62 s |
| `ac.drill(eid)`, 16–17 sightings | 0.2–1.0 s |
| `event_sightings(eid)`, 16–17 sightings | 3.2–3.6 s |
| One deep row (363,784) of a 548,109-row workbook via `original_rows` | 12 s |
