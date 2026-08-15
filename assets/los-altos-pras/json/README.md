# Los Altos PD — ALPR audit logs (PRA 25-312 + 26-366)

Flock **Network Audit** and **Organization Audit** exports produced by the Los
Altos Police Department under the California Public Records Act, released as
public records on the city's NextRequest portal.

Flock's two audit reports answer different questions, and both are here:

- **Network Audit** — searches of *Los Altos' Flock network* by any agency in
  the Flock system. This is the outside-agency view.
- **Organization Audit** — searches run *by Los Altos PD's own personnel*. This
  is the source the per-agency justifications page uses.

## The two productions

| | PRA 25-312 | PRA 26-366 |
| --- | --- | --- |
| Filed | 2025-07-22 | 2026-07-06 |
| Closed | 2025-08-13 | 2026-07-09 |
| Requested by | Working Partnerships USA | Los Altos for Representation and Equity (LARE) |
| Period requested | 2024-03-01 → processing date | 2025-08-01 → processing date |
| Period produced | 2024-03 → 2025-08-13 | 2025-01 → 2026-07-06 |
| Rows | 3,790,065 | 3,510,714 |
| Portal | [requests/25-312](https://losaltosca.nextrequest.com/requests/25-312) | [requests/26-366](https://losaltosca.nextrequest.com/requests/26-366) |

Both productions returned **more than was requested** — 25-312 asked from March
2024 but got whole-year workbooks; 26-366 asked from August 2025 and got all of
calendar 2025. Together they cover **March 2024 – July 6 2026**, with
January–August 2025 produced twice.

## Format

`pra-25-312/` and `pra-26-366/` each hold one **gzipped NDJSON** per workbook
*sheet* (`<workbook>__<MONTH>.ndjson.gz`) — one JSON object per line, one line
per search. Keys are the column headers exactly as the department produced them
(e.g. `"Org Name"`, `"Search Time"`, `"Reason"`). Empty cells are omitted;
redaction markers are preserved verbatim. Read one with e.g.
`gzip -dc pra-26-366/Los_Altos_PD_Network_Audit_2025__JANUARY.ndjson.gz | jq .`

Each folder's `_manifest.json` lists every file with its row count, gzipped
size, source workbook and sheet, the header row as produced, per-field
populated/redacted counts, and any header repairs. The `SharedNetworks*.csv`
each production supplied (item 3 of both requests) sit alongside, verbatim.

## The overlap is consistent — with one exception

January–August 2025 exists in both productions, a year apart. The network audits
are **byte-identical** where they overlap (full content diff of January and July
2025: 0 differing rows; all Jan–Jul row counts and per-field populated counts
match exactly). August differs only because 25-312 was produced mid-month
(35,070 rows vs the full month's 362,205).

The one substantive difference is in the **May 2025 Organization Audit**: four
rows released with their `Reason` intact in 2025 were redacted to `REDACTED` in
2026. All four are Flock-generated reasons for alert-triggered searches that
embed a plate number in the text, e.g. `Dispatch quick search associated with
Alert: Stolen Plate <plate> - 2025-05-21T17:33:59.072Z`. The 2025 production is
therefore the more complete record for those rows.

**Neither production declared any redaction.** Both closed with identical
boilerplate ("To the extent that the City has provided all responsive and
non-privileged public records…"), citing no exemption and giving no notice that
anything had been withheld — in 26-366 despite the requester expressly asking
for "a signed notification citing the legal authorities on which you rely."

## Fields & redaction

Unlike Redwood City's production (`assets/redwood-city-pras/json/`), which
withheld the `Reason` field under Gov. Code §§ 7923.600 / 7922.000, **Los Altos
released the search reason essentially in full** — populated in 99.5–100% of
rows, with 5 redacted cells across all 3.5M rows of 26-366.

Two redaction *mechanics* appear, and they are diagnostic:

- **`REDACTED`** — typed over cells in Excel by the department.
- **`***`** — Flock's own export marker.

The 2026 *Organization* audit uses `REDACTED` while the 2026 *Network* audit
uses `***`, in the same production. So the `***` in the network audits is
Flock's export blanking partner-agency data, not the city's redaction.

That distinction matters for a claim Redwood City made about the same period —
that "due to updates by Flock… more recent network audits no longer include" the
searcher name, plate, case number or filter. In Los Altos' network audits every
one of those four columns is present in **every month through July 2026**, and
for Los Altos' *own* rows they carry real values through February 2026:

| 2026 month | Los Altos' own rows | with real Name | plate | case # | filters |
| --- | --- | --- | --- | --- | --- |
| January | 486 | 486 | 345 | 252 | 120 |
| February | 537 | 537 | 365 | 274 | 200 |
| March | 398 | 0 | 0 | 0 | 0 |
| April | 625 | 0 | 0 | 0 | 0 |

Flock's blanking of the requesting agency's own rows takes effect between
February and March 2026; before that the export delivered them intact. (A
consequence: ~1,042 plates and ~1,483 officer names survive in the network
audits, all in Los Altos' own rows, where the city's redaction pass did not
reach. They are preserved verbatim here as produced.)

What Flock actually *removed* from the export schema over this period is a
different list: `Moderation` (after Aug 2025), `Text Prompt` (after Sep 2025),
and `Total Devices Searched` (after Jan 2026).

`Case #` and `Filters` were released largely intact through 2025, then redacted
in ~99.8% of the rows where they appear from January 2026.

## Hand-edited headers

The workbooks were edited in Excel before release and the header rows did not
always survive. The converter type-checks every header label against its
column's data rather than trusting position, so these are detected and recorded
rather than silently mis-keyed:

- **26-366 Organization Audit 2025 / September** — the header retains a
  `Total Devices Searched` label for a column absent from the data. Left alone,
  every later field would shift one column left. The orphan label is dropped
  (`phantom_headers` in the manifest); the two date columns landing exactly on
  `Time Frame` and `Search Time` confirm the alignment.
- **26-366 Organization Audit 2025 / October and November** — data columns whose
  header label was blanked. Kept under `column_<N>` keys rather than dropped
  (`unlabeled_columns`). Both are 100% redacted and sit where `Name` and
  `License Plate` sit in neighbouring months.

Corroborating the editing: the 26-366 workbooks' `sheetId` sequences have gaps
(Organization Audit 2025 runs 1–8 then 12–15; both "through July 6, 2026"
workbooks skip 6), i.e. sheets were deleted and recreated — and the gap in the
Organization Audit sits exactly where the mangled headers are. No hidden
columns, hidden rows, hidden sheets or saved autofilters were found in any of
the workbooks.

## Provenance

Generated from the workbooks as released on the city's public NextRequest
portal (no login required). The raw ~520 MB of `.xlsx` are kept local (too large
for git); this parsed NDJSON is the committed, machine-readable record.

Regenerate with:

```
uv run --with openpyxl python scripts/xlsx_to_audit_ndjson.py \
    --out assets/los-altos-pras/json/pra-26-366 <workbook.xlsx> ...
```

The justifications source for the site is derived from the Organization Audits:

```
uv run python scripts/xlsx_to_audit_rows.py --slug los-altos-ca-pd \
    --pra-id 26-366 --org "Los Altos CA PD" \
    assets/los-altos-pras/json/pra-26-366/Los_Altos_PD_Organizational_Audit_*.ndjson.gz
```
