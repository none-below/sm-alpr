# Los Altos PD — ALPR audit logs (PRA 26-366)

Flock **Network Audit** and **Organization Audit** exports produced by the Los
Altos Police Department under the California Public Records Act (City of Los
Altos request **26-366**, filed 2026-07-06 by Los Altos for Representation and
Equity (LARE), closed 2026-07-09), released as public records.

Flock's two audit reports answer different questions, and both are here:

- **Network Audit** — searches of *Los Altos' Flock network* by any agency in
  the Flock system. This is the outside-agency view.
- **Organization Audit** — searches run *by Los Altos PD's own personnel*.

The city produced **more than was asked for**: the request sought August 1 2025
onward, and LAPD returned all of calendar 2025 plus 2026 through July 6.

## Format

One **gzipped NDJSON** file per workbook *sheet* (`*.ndjson.gz`): one JSON object
per line, one line per search. The workbooks carry one sheet per month, so file
names are `<workbook>__<MONTH>.ndjson.gz`. Keys are the column headers exactly as
the department produced them (e.g. `"Org Name"`, `"Search Time"`, `"Reason"`).
Empty cells are omitted; redaction markers are preserved verbatim. Read a file
with e.g. `gzip -dc Los_Altos_PD_Network_Audit_2025__JANUARY.ndjson.gz | jq .`

`_manifest.json` lists every file with its row count, gzipped size, source
workbook and sheet, the header row as produced, per-field populated/redacted
counts, and any header repairs (see *Hand-edited headers* below).

## Coverage

| Audit | Period | Rows |
| --- | --- | --- |
| Network 2025 | Jan – Dec 2025 | 3,172,647 |
| Network 2026 | Jan 1 – Jul 6 2026 | 329,897 |
| Organization 2025 | Jan – Dec 2025 | 4,882 |
| Organization 2026 | Jan 1 – Jul 6 2026 | 3,288 |
| **Total** | | **3,510,714** |

Note on reading the network-audit counts: a row's `Total Networks Searched` is
how many networks that one search spanned (often thousands), so these rows are
searches that *touched* Los Altos' network, not searches aimed at Los Altos.

## Fields & redaction

Unlike Redwood City's production (`assets/redwood-city-pras/json/`), which
withheld the `Reason` field under Gov. Code §§ 7923.600 / 7922.000, **Los Altos
released the search reason essentially in full**: `Reason` is populated in
99.5–100% of rows in every file, with 5 redacted cells across all 3.5M rows.

What *is* withheld is consistent with PII redaction: `Name` (the searching
officer) and `License Plate` are redacted in ~100% of rows throughout.

`Case #` and `Filters` changed mid-corpus. Through 2025 they were released
largely intact (of the rows where `Case #` appears at all, 3.2% are redaction
markers); from January 2026 they are redacted in ~99.8% of the rows where they
appear. The 2025 files therefore contain roughly 692k real case numbers and 894k
real filter strings.

Two redaction *mechanics* appear: the 2025 workbooks were redacted by typing the
literal string `REDACTED` over the cells in Excel, while the 2026 workbooks carry
Flock's native `***` marker. Both are kept verbatim and counted separately in
`_manifest.json`.

Schema drifts within the corpus. `Total Devices Searched` is present for 2025 and
January 2026, then disappears. `Text Prompt` (Flock's free-text / natural-language
search) and `Moderation` (Flock's allow/warn/block label on that prompt) appear as
columns in 2025 but are populated in only 117 and 32 rows respectively. `Reason`
itself shifts form: free text in 2025 (`RECKLESS DRIVING`, `459 suspects`,
`invest`), Flock's structured offense-type picker in 2026 (`Wanted Person (Arrest
Warrant/Fugitive)`, `Motor Vehicle Theft/Stolen - 9373`).

## Hand-edited headers

The workbooks were edited in Excel before release, and the header rows did not
survive intact. The converter type-checks every header label against its column's
data rather than trusting position, so these are detected and recorded, not
silently mis-keyed:

- **Organization Audit 2025 / September** — the header retains a
  `Total Devices Searched` label for a column that is not in the data. Left
  as-is, every later field would shift one column left. The orphan label is
  dropped (`phantom_headers` in the manifest); the two date columns landing
  exactly on `Time Frame` and `Search Time` confirm the resulting alignment.
- **Organization Audit 2025 / October and November** — data columns whose header
  label was blanked out. These are kept under `column_<N>` keys rather than
  dropped (`unlabeled_columns` in the manifest). Both are 100% redacted and sit
  where `Name` and `License Plate` sit in the neighbouring months.

## Also produced

`../SharedNetworks_2026_July_6.csv` — the city's answer to item 3 of the request
(networks shared with Los Altos), 259 organizations, committed verbatim.

## Provenance

Generated from the workbooks as released on the city's NextRequest portal. The
raw ~245 MB of `.xlsx` are kept local (too large for git); this parsed NDJSON is
the committed, machine-readable record.

Regenerate with:

```
uv run --with openpyxl python scripts/xlsx_to_audit_ndjson.py \
    --out assets/los-altos-pras/json <workbook.xlsx> ...
```
