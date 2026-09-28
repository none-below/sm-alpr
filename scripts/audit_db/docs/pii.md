# Civilian PII, plate tokens and the public surface

For engineers who publish data derived from this database. It covers what counts as civilian data, how licence plates
become tokens you can follow across agencies without the key, how the key is read and checked, exactly what
`sightings_public` changes and what it leaves alone, the rules that find plates and other identifiers in free text
(and what they miss on purpose), how release names lose zip folder names, which objects and columns may leave the
machine, and the check to run before any export. Session setup is in [README.md](README.md). Citing a row is in
[provenance.md](provenance.md). Counts and timings are from the build with `truth.build_info.built_at_utc` =
2026-09-26T20:38:17Z (repo inputs `e138455bb`, DuckDB 1.5.5), measured at `threads=4`, `memory_limit='4GB'` under
`nice -n 19 taskpolicy -b`. Corpus-wide numbers are in [stats.md](stats.md).

## Rules in brief

- The local databases hold civilian data exactly as agencies released it. **Export rows only from `sightings_public`**,
  optionally joined to the public provenance columns listed under [Public-safe versus local-only](#public-safe-versus-local-only).
- Licence plates become `p1_` + 16 hex tokens. The same plate string gets the same token in every agency's data, so
  you can follow a plate across agencies. Reading, joining and counting tokens needs no key.
- Making tokens needs the key file. Without a valid one, `sightings_public` and `plate_token` raise an error. They
  never return an empty view or NULL tokens.
- Searcher names (police employees) and organization names stay verbatim, except that a plate typed into a name is
  tokenized. These docs still do not print people's names.
- Releases are published as `public_release_id`, which never contains folder names from inside an agency's zip. The
  verbatim `release_id` stays local ([Release names and zip folders](#release-names-and-zip-folders)).
- Free-text detection is **precision first**: it would rather leave an ambiguous string than damage a case number.
  Some plates, addresses and other identifiers survive. Run the [pre-export check](#pre-export-check) and read
  [Residual risk](#residual-risk).
- A citation leads to the released original, which holds whatever the agency released, including raw plates in some
  releases. Tokenizing controls what *this* dataset republishes. It does not hide the public records.

## Policy

| Data | Treatment in `sightings_public` | Why |
|---|---|---|
| Searcher (`Name`; SMPD: the name line of each PDF row) | Verbatim as `searcher_name`, except plate shapes, which are tokenized as in free text | Public-employee data (owner policy, 2026-09-25). A plate in the wrong cell is still civilian |
| Searching and producing organization (`org`, `producer`) | Verbatim | Agencies, not people |
| Plate column (`License Plate`) | `value` cells become tokens. Blank cells, known masks and exemption citations pass through as released. Every other cell becomes NULL | Civilian: a vehicle, and through it a person |
| Plates inside `Reason`, `Case #`, `Filters`, `Text Prompt` | Detected shapes become `[p1_…]` in place | Officers type plates into free text, and Flock writes plate search terms into Filters |
| Other civilian identifiers in `Reason` and `Text Prompt` | Keyword-anchored DOB, phone, SSN and driver's licence, a house number before capitalized street words, and `LAST, FIRST … DOB` become `[dob]` `[phone]` `[ssn]` `[dl]` `[addr]` `[name]` | Civilian PII |
| `Search Type` | Exported only when it looks like a search-type label, otherwise NULL | A misaligned cell (a plate, a time) must not leak through a label column |
| Case numbers, offense codes (`459 PC`), UUIDs, times, counts | Unchanged | Not civilian PII, and needed to cite and to cross-check |
| Release names | `public_release_id`, with no folders from inside zips | A folder name can hold a MuckRock requester's name |
| Columns outside the parsed set (`extra`, `Moderation`), the raw `Time Frame` text, and the event log | Not in `sightings_public` at all | Unreviewed. Hotlist entries hold plates |

## Plate tokens

### Construction

```
token(p) = 'p1_' || first 16 hex digits of HMAC-SHA256(key, 'plate:v1:' || plate_norm(p))
plate_norm(p) = upper(p with every character outside A-Z a-z 0-9 removed); NULL if nothing is left
```

- **Key**: 32 random bytes, stored as 64 hex digits ([Key handling](#key-handling)).
- **`'plate:v1:'`** is a domain prefix. It keeps plate tokens apart from any other HMAC made with the same key and
  names the scheme version. **`'p1_'`** marks version 1 in the output. A new key or new normalization would be v2 with
  a new prefix, and v2 tokens would not join to v1 tokens.
- **Truncation** to 16 hex digits (64 bits). The chance of any collision among 10 million distinct plates is about
  n²/2⁶⁵ ≈ 3 × 10⁻⁶.

The implementation, in `derived.duckdb` (source: `public_macros.sql`, loaded by `build_derived.py`):

| Macro | Does | Notes |
|---|---|---|
| `plate_norm(p)` | The normalization above | NULL or blank input gives NULL, so the token is NULL |
| `plate_key_hex()` | Table macro, one row: `khex` = the key file's text with every space, tab, CR and LF removed (anywhere, not only at the ends), lower-cased. Raises an error unless exactly 64 hex digits remain | **Never select it: it returns the key.** Reads `~/.config/sm-alpr/plate_token_key` on every use |
| `plate_pad(khex, a, b)` | HMAC key pad in SQL: right-pads the hex key with `0` to 128 hex digits (64 bytes, the SHA-256 block size) and XORs each byte's high and low hex digit with `a` and `b`. (3, 6) = inner pad `0x36`; (5, 12) = outer pad `0x5c` | Does not validate. The views get the key only through `plate_key_hex()` |
| `plate_key_pads()` | Table macro, one row (`pi`, `po`) = both pads of `plate_key_hex()` | **Never select its columns: they are the key XORed with a constant.** Raises the same error |
| `plate_hmac(p, pi, po)` | `'p1_' \|\| left(sha256(po \|\| unhex(sha256(pi \|\| encode('plate:v1:' \|\| plate_norm(p))))), 16)` | Standard HMAC from pads passed in. The tokenizing lambdas cannot run subqueries, so pads are arguments |
| `plate_token(p)` | The same token, reading the key file itself | For a key holder. Raises the key error without a valid key file |
| `plate_public(surface, state, pi, po)` | The plate-column rule | See `plate` in [`sightings_public`](#sightings_public) |

`plate_key.py` computes the same token in Python (`token_py`), with `hmac.new(key, b'plate:v1:' + norm, sha256)`.

The SQL construction is standard HMAC-SHA256. This check uses a throw-away key, so it needs no access to the real one
and prints nothing secret:

```python
import hmac, hashlib, re
DUMMY = "00112233445566778899aabbccddeeff" * 2          # NOT the real key: any 64 hex digits

def ref_token(key_hex, p):                                # reference HMAC in Python
    norm = re.sub(r"[^A-Za-z0-9]", "", p).upper()
    return "p1_" + hmac.new(bytes.fromhex(key_hex), ("plate:v1:" + norm).encode(), hashlib.sha256).hexdigest()[:16]

sql_tok = con.execute("SELECT plate_hmac($p, plate_pad($k, 3, 6), plate_pad($k, 5, 12))",
                      {"p": "0xx-x 000", "k": DUMMY}).fetchone()[0]
print(sql_tok == ref_token(DUMMY, "0XXX000"))             # True: same HMAC, and normalization folds case/punctuation
```

### What a token does and does not tell you

- **Same normalized string → same token, everywhere.** The HMAC input is the plate text only. No producer, release or
  date goes in, so a token found in one agency's log joins to the same token in any other agency's log, month or
  re-release.
- **Spelling variants merge.** `0xx-x 000` and `0XXX000` share a token. A release's plate column can therefore hold
  more distinct raw strings than distinct tokens: it gives exactly one token per distinct plate after `plate_norm`,
  whatever the case and punctuation of the raw strings.
- **A token is a string, not a vehicle.** A typo, a misread character or a partial plate gets a different token. The
  issuing state is not part of the input, so the same characters on plates from two states share one token.
- **A token hides length and shape.** A partial search (a plate cell holding only a shape such as `9AA` or `999`)
  looks exactly like a full plate.
- **`value` does not mean genuine.** Every `value` cell in the plate column is tokenized, whatever it holds. Known
  masks and exemption citations are recognized (`agency_mask`, `exemption_cite`) and pass through as released: a
  plate cell holding `7923.600 GC` is `redacted_agency` and stays that text. Before `exemption_cite` existed, such
  cells all became one token that looked like one plate searched very often. A marker the rules do not know would
  still become a frequent token, so check the most frequent tokens' `plate_surface` locally before ranking tokens.
  See [semantics.md](semantics.md) §10.
- **A plate can be in free text only.** A row whose plate column is `empty` can still carry a plate in its Reason, for
  example a Reason that is only a `9AAA999`-shaped string. Following a plate means reading the plate column **and**
  the bracketed tokens in the text columns.

### Working with tokens (no key needed)

In `plate` the token is bare (`p1_…`). In `reason`, `case_no`, `filters`, `text_prompt` and `searcher_name` it is
bracketed (`[p1_…]`). One pattern extracts both: `p1_[0-9a-f]{16}`.

Every token a release mentions, by column (tokens themselves are not printed):

```python
PRID = "<public_release_id>"
TOKENS = r"""
WITH p AS (SELECT * FROM sightings_public WHERE public_release_id = $prid),
t AS (
  SELECT row_no, 'plate' AS field, plate AS token FROM p WHERE plate_state = 'value'
  UNION ALL
  SELECT row_no, field, unnest(regexp_extract_all(v, 'p1_[0-9a-f]{16}')) AS token
  FROM (SELECT row_no, unnest(['reason', 'case_no', 'filters', 'text_prompt', 'searcher_name']) AS field,
               unnest([reason, case_no, filters, text_prompt, searcher_name]) AS v FROM p))
SELECT field, count(*) AS mentions, count(DISTINCT token) AS distinct_tokens, count(DISTINCT row_no) AS rows_
FROM t GROUP BY field ORDER BY field"""
print(con.execute(TOKENS, {"prid": PRID}).fetchall())
# [(field, mentions, distinct_tokens, rows_), …]: one tuple per column that holds at least one token
```

Following plates across releases: how many of one producer's own-search plate tokens recur in more than one of its
monthly worksheets (the sheet name comes from `release_sources`, joined on `public_release_id`):

```python
print(con.execute(r"""
WITH m AS (
  SELECT r.sheet, p.plate AS token
  FROM sightings_public p JOIN release_sources r USING (public_release_id)
  WHERE p.producer = $producer AND p.audit = 'own' AND p.plate_state = 'value')
SELECT count(DISTINCT token) AS tokens, count(*) FILTER (WHERE n_sheets > 1) AS tokens_in_2plus_months,
       max(n_sheets) AS max_months
FROM (SELECT token, count(DISTINCT sheet) AS n_sheets FROM m GROUP BY token)""", {"producer": "<producer>"}).fetchall())
# [(tokens, tokens_in_2plus_months, max_months)]
```

Before you publish a pattern like this, cite the rows behind it: join on (`public_release_id`, `row_no`) to
`sighting_sources` ([Exporting with citations](#exporting-with-citations)). One search appears in many logs, so count
events, not sightings, when you mean searches ([linking.md](linking.md)).

**Cost.** `sightings_public` is a view. It computes every token on read, so a filter such as `WHERE plate = 'p1_…'`
cannot be pushed into `truth`: it computes the token for every row it scans. Filter by `public_release_id` (it
prunes to one release) or by `producer` first. For corpus-wide token searches, use the materialized export (planned),
or materialize the subset you need once.

### Who needs the key

- **Nobody who reads tokens.** Joining, counting and following tokens in the export, or in `sightings_public` on
  this machine, works on the strings.
- **Only whoever turns a raw plate into a token**: building `sightings_public` output or the export, or looking up a
  plate someone has in hand. A reporter with a plate from a source cannot search the export for it without asking the
  key holder to run `plate_token`. Where a row's cited original shows the plate, it can still be read there (last
  point below).
- **The key is the whole cryptographic protection.** The plate space is small (the standard California shape `9AAA999`
  has 10 × 26³ × 10³ = 175,760,000 strings), so anyone holding the key can reverse every token by enumeration. Without
  the key, enumeration is useless because HMAC outputs cannot be computed.
- **A token is not anonymous against its own citation.** Every public row cites the agency's released file, which is
  public, and some of those files carry the plate in the clear. Anyone can follow a token's citation to the original
  row and read the plate there. Tokenizing keeps plates out of this export and makes them unsearchable across it; it
  does not hide a plate that the agency already published.

### Key handling

| Where | What |
|---|---|
| `~/.config/sm-alpr/plate_token_key` | The key file, the only key source SQL reads. `plate_key.py --check` on 2026-09-26: 64 hex digits, file `0600`, directory `0700` (status only; the key was never read or printed) |
| SQL: `plate_key_hex()` | Reads the file on every use. Removes every space, tab, CR and LF, lower-cases, and raises `plate token key missing, empty or not 64 hex digits: ~/.config/sm-alpr/plate_token_key (create with: openssl rand -hex 32; CI: plate_key.py --install)` unless exactly 64 hex digits remain. A missing file (DuckDB's `read_text` returns zero rows for it), an empty file, 63 or 66 digits, or a non-hex character all raise it. The message never contains key text. `plate_key_pads()`, `plate_token()` and `sightings_public` all read the key through it |
| Python: `plate_key.py` | `normalize_key` applies the same normalization and exits on the same failures. `load_key` reads env `PLATE_TOKEN_KEY` and the file. If both are set they must be equal after normalization (SQL reads only the file), otherwise it exits. Every file read sets the file to `0600` and its directory to `0700` if they differ, with a note on stderr. `token_py(p)` returns the token. `set_plate_key` is legacy (session variables), and no view uses it |
| `<code>/plate_key.py --check` (pinned env) | Validates the file and prints status only. Exit 0 with `key file OK: 64 hex chars, file 0600, directory 0700` (plus what it tightened, if anything); exit 1 with `no key file at …` or the normalization error |
| CI: `<code>/plate_key.py --install` (pinned env) | `install_from_env`: normalizes env `PLATE_TOKEN_KEY` (exits if invalid), creates the directory as `0700`, and writes the normalized key, with no newline, as a `0600` file, also when the file already existed |
| GitHub secret `PLATE_TOKEN_KEY` | Exists on `none-below/sm-alpr` (name confirmed with `gh secret list` on 2026-09-26; value never read). No workflow at the build's repo inputs (`e138455bb`) references it yet |
| `.duckdb` files | Never hold the key. Views and macros store only the path |

How this was checked, with throw-away keys only: `SET home_directory = '<scratch dir>'` points `~` at a scratch
directory for the session, so the real key is never touched. Missing, empty, 63-digit, 66-digit and non-hex key files
each made both `plate_token('0xxx-000')` and `SELECT count(*) FROM sightings_public WHERE public_release_id = …` raise
the error. A dummy key ending in `\n`, and the same key upper-cased with CRLF and inner spaces, each gave exactly the
Python `hmac` token, and a test release's full row count from `sightings_public`. With `HOME` set the same way,
`plate_key.py --check` and `token_py` agreed. Use the same trick to test the failure path of anything you build.

Rules:

- Never `cat` the file, read it with `read_text`, select from `plate_key_hex()`, or select `pi` / `po` from
  `plate_key_pads()`.
- A trailing newline or CRLF in the key file is harmless now: SQL and Python both strip it and still agree.
- Rotating the key changes every token. Publish rotated tokens under a new prefix (`p2_`, `plate:v2:`) so old and new
  tokens are never compared.

## `sightings_public`

```sql
FROM sightings s JOIN (SELECT release_id, public_release_id FROM release_sources) p USING (release_id),
     plate_key_pads() k
WHERE k.pi IS NOT NULL
```

One row per released search row: the same rows as `sightings` (`sightings_flock` ∪ `sightings_smpd`), named by
`public_release_id` instead of `release_id`, with the one row of key pads joined on. `k.pi IS NOT NULL` is always
true with a valid key. It is there so that every read, even `count(*)`, reads the key, so a missing or malformed key
fails the query instead of emptying the view. Pass-through columns are identical to `sightings`. To confirm this on a
release, join it to `sightings` on `sighting_id`: no row should differ in any pass-through column or `*_state`,
`search_type` and `searcher_name` should differ only where the label rule or a plate token applies, and a row whose
`reason` differs from `reason_surface` should carry a token or a scrub marker.

| Column | Type | Meaning | Notes |
|---|---|---|---|
| `sighting_id` | UBIGINT | `hash(release_id, row_no)`, over the **verbatim** `release_id` | A local join key. DuckDB `hash()` is not guaranteed stable across DuckDB versions. Key published rows on (`public_release_id`, `row_no`) |
| `public_release_id` | VARCHAR | Release the row came from, public form | `release_id` without folders inside a zip ([Release names and zip folders](#release-names-and-zip-folders)). Unique |
| `row_no` | BIGINT | Row position in the release | With `public_release_id`, the key to `sighting_sources` |
| `src_row` | BIGINT | Row as a reader of the original sees it | NULL for SMPD (cited by PDF page) |
| `producer` | VARCHAR | Organization whose log this is | |
| `audit` | VARCHAR | `network` or `own` | |
| `org` | VARCHAR | Organization that ran the search | Verbatim |
| `org_basis` | VARCHAR | Where `org` came from | |
| `t` | TIMESTAMP | Search time, UTC | |
| `nets` | INTEGER | Networks searched | |
| `devices` | INTEGER | Devices searched | |
| `tf_start` | TIMESTAMP | Searched period, start (UTC) | |
| `tf_end` | TIMESTAMP | Searched period, end (UTC) | |
| `flock_id` | VARCHAR | Flock search UUID | |
| `search_type` | VARCHAR | Flock search-type label | `sightings.search_type` only when it fully matches `[A-Za-z][A-Za-z0-9 -]{0,40}` (a letter, then at most 40 letters, digits, spaces or hyphens) and contains no `9AAA999` plate shape, otherwise NULL. No punctuation, so a time or a shifted cell cannot pass; digits are allowed so a genuine label such as `apiV1` is kept |
| `layout_corrected` | BOOLEAN | Row read through a `release_layouts` correction | |
| `reason` | VARCHAR | Reason **as released** (surface), scrubbed, then plate-tokenized | `tokenize_in(scrub_civilian(reason_surface), plate_candidates(scrub_civilian(reason_surface)), true, pi, po)`. Masks (`***`, `REDACTED`) and placeholders stay, untrimmed. **Not** `sightings.reason` (the clean value): filter `reason_state = 'value'` and `trim` it yourself |
| `reason_state` | VARCHAR | Cell state of Reason | As in `sightings` ([semantics.md](semantics.md) §10) |
| `case_no` | VARCHAR | Case # as released, plate-tokenized | `tokenize_in(case_surface, plate_candidates(case_surface), true, pi, po)`. **Not** scrubbed, so digit runs survive. Surface, not `sightings.case_no`. NULL for SMPD (not exported) |
| `case_state` | VARCHAR | Cell state of Case # | |
| `searcher_name` | VARCHAR | Name as released, plate-tokenized | `tokenize_in(name_surface, plate_candidates(name_surface), true, pi, po)`: the free-text plate rules, no scrub. Police employees stay verbatim; a plate shape in the cell becomes `[p1_…]`. SMPD: the name line printed in the PDF, mostly `***` |
| `name_state` | VARCHAR | Cell state of Name | |
| `plate` | VARCHAR | Token, the released mask, or NULL | `plate_public(plate_surface, plate_state, pi, po)`: `value` → `plate_hmac(plate_surface, pi, po)`. NULL, blank, `***`, an `agency_mask` or an `exemption_cite` → the released text. **Anything else → NULL**, including `partial` (`REDACTED` plus text) and any state a future rule adds, so raw plate text never passes. No `partial` plate cells in this build ([stats.md](stats.md), "Cell states by field") |
| `plate_state` | VARCHAR | Cell state of License Plate | |
| `text_prompt` | VARCHAR | Free-form search prompt, scrubbed, plate-tokenized | `tokenize_in(scrub_civilian(text_prompt), plate_candidates(scrub_civilian(text_prompt)), true, pi, po)`: candidates come from the scrubbed text, as for `reason` |
| `filters` | VARCHAR | Search filters, plate-tokenized | `tokenize_in(filters, filter_plate_candidates(filters), false, pi, po)` ([Filters](#filters)). Not scrubbed |

Not in `sightings_public`: `release_id`, the `*_surface` columns, `sightings.reason` and `sightings.case_no` (clean
values), `extra` (released columns outside the 14 Flock labels), `Moderation`, the raw `Time Frame` text, and anything
from the event log.

## Release names and zip folders

For a file inside a MuckRock zip, `release_id` is `mr:<request>:<zip file>!<member path>#<sheet>`, and the member path
includes the folders inside the agency's zip. Folder names can identify people: in some zipped releases, a folder name
includes the MuckRock requester's name. MuckRock publishes requester names itself; this dataset does not republish
them.

The public forms, all in `release_sources` and built from the member's file name only:

- **`public_release_id`** = `public_rid(release_id, pra_id, container_path, member)`: for a zip member, everything
  between `!` and the last `/` or `\` is removed, giving `mr:<request>:<zip file>!<member file name>#<sheet>`. Every
  other release keeps its `release_id`. It differs from `release_id` in 325 of 907 releases (every zip member that
  sits in a folder). `build_derived.py` exits if it is not unique or is NULL.
- **`document`** = `<zip file> > <member file name>`, or the file itself.
- **`link`**, **`locator`** and **`citation`** are built from `document`. `open_url` for a zip is the zip's download
  URL.

Checked on every release whose folder names include a requester's name: the folder text occurs in none of their
`release_sources` values of `public_release_id`, `document`, `link`, `source_url`, `container_path`, `sheet`,
`member_file` or `source_file`, nor in any `citation`, `document`, `link` or `open_url` of one such release's
`sighting_sources` rows. It occurs in every `release_id`, `member` and `document_verbatim`.

Where the verbatim member path still lives (all local only):

| Object | Column |
|---|---|
| `truth.releases` | `release_id`, `member` |
| `release_sources` | `release_id`, `member`, `document_verbatim` |
| `sighting_sources`, `sighting_sources_flock`, `sighting_sources_smpd` | `release_id`, `document_verbatim` |
| `truth.flock_audit_rows`, `truth.flock_event_rows`, `flock_rows`, `sightings`, `sightings_flock`, `sightings_smpd`, `event_log` | `release_id` |
| `cache.sighting_keys`, `cache.sighting_event` | `release_id` |
| `release_content_groups` | `releases`: a list of verbatim `release_id`s, including re-released zip members |

`sighting_id`, and `x:<sighting_id>` event ids, are hashes of the verbatim `release_id`, not the text. To carry
`event_id` into an export, join `cache.sighting_event` to `release_sources` on `release_id` and export only
`public_release_id`.

## Detecting plates in free text

### Plate candidates

`plate_candidates(txt)` (for `reason`, `case_no`, `text_prompt` and `searcher_name`) returns the distinct substrings to
tokenize, from three rules. Shapes: `9` = digit, `A` = letter.

```text
Tier A, anywhere, as a word, any case — the California standard plate 9AAA999:
  (?i)\b[0-9][A-Z]{3}[0-9]{3}\b

Tier B shapes (commercial, trailer and other plate shapes) — 99999A9, 9A99999, 9AA9999, 9A9A999, AA99A99:
  [0-9]{5}[A-Z][0-9]|[0-9][A-Z][0-9]{5}|[0-9][A-Z]{2}[0-9]{4}|[0-9][A-Z][0-9][A-Z][0-9]{3}|[A-Z]{2}[0-9]{2}[A-Z][0-9]{2}

Tier B, context: within 15 non-alphanumeric characters after a plate word (group 2 is taken), any case:
  (?i)\b(plate|lp|lic|license|tag|stolen plate|alert)\b[^A-Za-z0-9]{0,15}(<tier B shapes>)\b

Tier B, whole field: the trimmed cell is exactly one tier B shape, any case:
  regexp_full_match(trim(txt), '(?i)<tier B shapes>')
```

**Never tokenized in free text: `AAA9999`, `AA99999`, `AAA999`, `A9999999`.** These are case-number formats, and
`A9999999` is also the California driver's licence format (handled by `[dl]` when anchored). The comment in
`public_macros.sql` gives the reason tier A is safe everywhere: `9AAA999` is the California standard plate shape, not
one agency's case-number scheme.

What the rules do, on synthetic strings (`0` and `X` stand for any digit and letter; reproduce with
`con.sql("SELECT plate_candidates('…')")`):

| Input | Candidates | Rule |
|---|---|---|
| `0XXX000`, `0xxx000`, `CA 0XXX000`, `CA:0XXX000`, `24-0XXX000` | the plate | Tier A |
| `x0XXX000`, `0XXX0000`, `0XXX000CA`, `CA0XXX000` | none | Tier A needs word boundaries on both sides |
| `0XXX 000`, `0XXX-000`, `plate 0XXX-000` | none | Separators inside a plate are not bridged |
| `plate: 0XX0000`, `plate#0XX0000`, `license plate 0XX0000`, `lic. 0XX0000`, `LP 00000X0`, `tag 0X00000`, `alert 0X0X000`, `alert: XX00X00` | the plate | Tier B with context |
| `0XX0000`, `  0XX0000  ` (whole cell) | the plate | Tier B, whole field |
| `case 0XX0000`, `veh 0XX0000`, `LPR hit 0XX0000`, `plates 0XX0000`, `0XX0000 extra` | none | No plate word (`plates`, `LPR` are not in the list), and not the whole field |
| `plate` + 16 spaces or dashes + `0XX0000` | none | Gap longer than 15 characters |
| `stolen plate 0XX0000 then 0XX0001` | first only | The second has no context word of its own |
| `plate XXX0000`, `XXX0000`, `plate XX00000`, `X0000000`, `XXX000` | none | Never-tokenized shapes, even with context |

### Filters

Flock writes plate search terms into Filters, often glued onto a tag or onto vehicle attributes (`stolen0XXX000`,
`Chevrolet0XXX000`, `whitecalifornia0XXX000`). The Filters rule, `filter_plate_part(run)`, looks at each maximal run
of letters and digits on its own. Any other character (`,` `:` space `-` `_` …) ends a run and is never bridged. A run
gives a candidate only in these cases, tried in order:

```text
1. The whole run is a tier A or tier B shape, any case → the whole run:
   (?i)[0-9][A-Z]{3}[0-9]{3}|[0-9]{5}[A-Z][0-9]|[0-9][A-Z][0-9]{5}|[0-9][A-Z]{2}[0-9]{4}|[0-9][A-Z][0-9][A-Z][0-9]{3}|[A-Z]{2}[0-9]{2}[A-Z][0-9]{2}
2. Letters of any case, then a tier A plate (9AAA999, any case) that ends the run → the last 7 characters:
   [A-Za-z]+[0-9][A-Za-z]{3}[0-9]{3}
3. Lower-case letters, then a digit-first tier A or tier B shape (any case) that ends the run → what follows the letters:
   [a-z]+(?i:[0-9][A-Z]{3}[0-9]{3}|[0-9]{5}[A-Z][0-9]|[0-9][A-Z][0-9]{5}|[0-9][A-Z]{2}[0-9]{4}|[0-9][A-Z][0-9][A-Z][0-9]{3})
Otherwise: no candidate. A shape is never cut out of a longer run.
```

Rule 2 is there because Flock concatenates vehicle attributes onto the plate in Filters. On 2026-09-26 the pre-export
check found 3,654 Filters cells that still held a raw `9AAA999` plate glued onto such a tag. Rule 2 was added the
same day, and the check now reports 0 ([stats.md](stats.md)). Tier B shapes glued onto a tag that is not all lower
case, and the letter-first `AA99A99` glued onto any tag, are not tokenized: the boundary between tag and plate is
ambiguous there.

`filter_plate_candidates(txt)` lists the distinct parts that would be tokenized, for inspection. On synthetic strings
(reproduce with `con.sql("SELECT filter_plate_candidates('…')")`):

| Filters input | Candidates | Rule |
|---|---|---|
| `0XXX000`, `0xxx000`, `XX00X00`, `xx00x00`, `plate:0xx0000`, `tag 0X00000` | the plate | 1: the whole run |
| `0XXX000,0XX0000` | both | 1: two runs |
| `stolen0XXX000,hotlist`, `stolen0xxx000`, `STOLEN0XXX000`, `Chevrolet0XXX000`, `whitecalifornia0XXX000` | the plate (the tag stays: `STOLEN[p1_…]`) | 2 |
| `Chevrolet-0XXX000`, `stolen_0XXX000` | the plate | 1: the separator splits the runs |
| `stolen0XX0000` | `0XX0000` | 3 |
| `STOLEN0XX0000`, `Chevrolet0XX0000`, `stolenXX00X00` | none | Tier B after a tag that is not all lower case; letter-first shape |
| `0XXX0000`, `X0XX0000`, `10XXX000`, `0XXX000stolen` | none | Never cut out of a longer run |
| `0XXX 000` | none | Separators are not bridged |

### `tokenize_in(txt, cands, word, pi, po)`

- **`word = true`** (Reason, Case #, Text Prompt, searcher name): replaces **every occurrence** of each candidate,
  matched as `(?i)\b<candidate>\b`, with `[` + `plate_hmac(candidate)` + `]`. A tier B plate that had context once
  is therefore also replaced where it appears again without context (`plate 0XX0000 later 0xx0000` → both tokenized,
  one token). Candidates contain only letters and digits, so they are safe to use as regular expressions. A later
  candidate cannot match inside a token: `p1_` + hex is one word, with no `\b` inside.
- **`word = false`** (Filters): ignores `cands`. It cuts the cell into runs of letters and digits and runs of other
  characters, and in each letter/digit run replaces the part `filter_plate_part` returns (always the end of the run)
  with `[token]`, keeping the tag: `stolen0XXX000,hotlist` → `stolen[p1_…],hotlist`, `0XXX000,0XX0000` →
  `[p1_…],[p1_…]`. One pass, so nothing is matched twice.
- NULL `txt` gives NULL.

### `scrub_civilian(txt)` (Reason and Text Prompt only)

Applied in this order (source in `public_macros.sql`; SQL doubles the `'` shown here as `''`). `⟨E000⟩` is U+E000, a
private-use character used as a temporary marker. It is inserted to keep a number from being read as a house number
and is removed at the end, so released text, including any genuine `§`, comes out unchanged except for the
replacements. Only a U+E000 already present in the released text would also be dropped.

```text
1. Protect code sections (459 PC, 10851 VC, 11350 HS; also HSC, WI, WIC, BP)        → \1⟨E000⟩\2\3
   (?i)\b([0-9]{2,6})(\s*)(PC|VC|HS|HSC|WI|WIC|BP)\b
2. Protect a number right after a case/report keyword (case 12345, DR 2024-123)       → \1\2⟨E000⟩\3
   (?i)\b(case|report|rpt|incident|inc|cad|event|evt|cfs|file|ticket|citation|cite|booking|warrant|dr|no|nr|num|number|ref|id)(\s*[#:.-]?\s*)([0-9])
3. Upper-case LAST, FIRST up to 30 characters before DOB (case-sensitive)             → [name]\2\3
   \b([A-Z][A-Z'-]+,\s*[A-Z][A-Z'-]+)([^\n]{0,30}?)\b(DOB|D\.O\.B|dob)\b
4. A date after a DOB keyword                                                          → \1 [dob]
   (?i)\b(dob|d\.o\.b\.?|date of birth)\s*[:#-]?\s*[0-9]{1,2}[/.-][0-9]{1,2}[/.-][0-9]{2,4}
5. An SSN after an SSN keyword                                                         → \1 [ssn]
   (?i)\b(ssn|social security|soc sec)\b[^0-9]{0,6}[0-9]{3}-?[0-9]{2}-?[0-9]{4}
6. A 3-3-4 phone after a phone keyword (the keyword goes too), or any (999) 999-9999  → [phone]
   (?i)\b(phone|ph|cell|tel|call|contact|rp)\b[^0-9]{0,10}\(?[0-9]{3}\)?[-. ]?[0-9]{3}[-. ][0-9]{4}|\([0-9]{3}\)\s*[0-9]{3}-[0-9]{4}
7. A licence number A9999999 after a licence keyword                                   → \1 [dl]
   (?i)\b(cdl|dl|dln|driver'?s? ?lic(ense)?|cadl)\b\s*[:#]?\s*[A-Z][0-9]{7}\b
8. Address, precision first: a standalone house number (start of text, or after whitespace , ; : ( @),
   2-6 digits and an optional letter, then 1-3 Capitalized words or ordinals (3rd), then a street
   suffix in any case. Only the number is replaced; the street words stay                → \1[addr]\2
   (^|[\s,;:(@])[0-9]{2,6}[A-Za-z]?((?:\s+(?:[A-Z][A-Za-z'.-]*|[0-9]+(?:st|nd|rd|th|ST|ND|RD|TH))){1,3}\s+(?i:st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|way|ct|court|ln|lane|pl|place|cir|circle|hwy|pkwy))\b
9. Remove every ⟨E000⟩.
```

On synthetic strings:

| Input | Output |
|---|---|
| `DOE, JOHN DOB 01/02/1990` | `[name] DOB [dob]` |
| `Doe, John DOB 01/02/1990` | `Doe, John DOB [dob]` (the name rule is upper case only) |
| `DOB: 1/2/90` | `DOB [dob]` |
| `dob 1990-01-02`, `DOB 010290` | unchanged (year-first and undelimited dates) |
| `SSN 000-00-0000` / `000-00-0000` | `SSN [ssn]` / unchanged |
| `call 555-555-0100`, `ph: 555.555.0100`, `contact 555 555 0100`, `phone number 555-555-0100`, `(555) 555-0100` | `[phone]` |
| `555-555-0100`, `rp 5555550100` | unchanged (no keyword; no separator) |
| `dl X0000000`, `DL# X0000000` / `X0000000` | `dl [dl]`, `DL [dl]` / unchanged |
| `100 Main St`, `100 N San Mateo Dr`, `100 MAIN ST`, `100A Main St`, `100 3rd Ave`, `(100 Main St)`, `100 Main street` | `[addr] Main St`, `[addr] N San Mateo Dr`, `[addr] MAIN ST`, `[addr] Main St`, `[addr] 3rd Ave`, `([addr] Main St)`, `[addr] Main street` |
| `Stolen 1234 Oak Ln` | `Stolen [addr] Oak Ln` (a word that is not a case keyword does not protect the number) |
| `100 main st`, `100 main St` | **unchanged: lower-case street words are not scrubbed** |
| `5 Main St`, `100 El Camino Real`, `100 One Two Three Four St` | unchanged (one-digit number; no listed suffix; more than 3 words) |
| `23-12345 stolen vehicle from court`, `#12345 Main St` | unchanged (the number is not standalone) |
| `case 12345 red honda civic ln`, `Report 2024 on the way`, `case 12345 Main St`, `DR 2024 Elm Way` | unchanged (case keyword before the number) |
| `459 PC`, `24-001234 10851 VC` | unchanged |
| `PC §459`, `§ 459 PC` | unchanged (a genuine `§` is kept) |

**The address trade-off.** Rule 8 is precision first. Until the 2026-09-26 build it matched any case and removed the
number part of case and report numbers followed by lower-case prose with a suffix-like word (`23-12345 stolen vehicle
from court` → `23-[addr] …`, `case 12345 red honda civic ln` → `case [addr] …`). It now requires a standalone number
and Capitalized street words, and numbers after a case keyword are protected. The cost: **a house number followed by
lower-case street words (`100 main st`) is no longer scrubbed and is published verbatim in `reason` and
`text_prompt`**, as is a Capitalized address that follows a case keyword (`case 12345 Main St`). The `addr_like`
column of the pre-export check counts cells that still look like an address, for review.

## Residual risk

What can still be in `sightings_public`, and why:

- **Plate crumbs in Reason, Case #, Text Prompt and searcher names**: tier B shapes with no plate word; the
  never-tokenized shapes (`AAA9999` is also a common out-of-state plate shape); plates written with a space or dash
  (`0XXX 000`); a plate glued to other letters or digits (`CA0XXX000`, `0XXX000CA`). They cannot be told apart from
  case numbers by shape alone. The check below counts them for review.
- **Plate crumbs in Filters**: a never-tokenized shape standing alone (a Filters cell that is exactly an `AA99999` or
  `AAA9999` shape is probably a plate search, and is left as released); a tier B shape glued onto a tag that is not
  all lower case; any shape inside a longer run; a plate split by a separator.
- **Addresses and other civilian identifiers without an anchor**: lower-case addresses, and addresses with a one-digit
  number, no listed suffix, more than three street words, or a case keyword before the number; names without a nearby
  `DOB`, and mixed-case names; dates of birth without a keyword or written year-first; phone numbers without a
  keyword; and anything typed into `Case #` or a searcher name, which are never scrubbed.
- **Non-plates tokenized as plates** in the plate column: a marker that is not a known mask or exemption citation.
  This is an analysis risk, not a leak.
- **Tokens are pseudonyms, not anonymity.** They are linkable by design. A token plus a search time, the searching
  agency and a reason (a case number in a court record, an incident in the news) can identify the vehicle.
- **Folder names in verbatim release names.** The public forms carry none, but any export that takes `release_id`,
  `member`, `document_verbatim` or `release_content_groups` puts a requester's name back
  ([Release names and zip folders](#release-names-and-zip-folders)).
- **Citations lead to originals.** `open_url` and `link` point to the released files (MuckRock downloads, repo
  copies), which hold raw plates where the agency released them, and the MuckRock zips hold their folder names. That
  is intended: it makes every row checkable.

## Public-safe versus local-only

| Object | Released civilian values? | Export? |
|---|---|---|
| `sightings_public` | No: tokenized and scrubbed ([Residual risk](#residual-risk) applies) | **Yes, the only row-level export.** Key rows on (`public_release_id`, `row_no`) |
| `sighting_sources`, `sighting_sources_flock`, `sighting_sources_smpd` | No cell values | **Only these columns:** `public_release_id`, `row_no`, `src_row`, `pdf_pages`, `producer`, `request_label`, `request_url`, `released_on`, `document`, `sheet`, `layout_corrected`, `locator`, `open_url`, `sha256`, `member_sha256`, `src_row_basis`, `link`, `citation`. **Not** `release_id` or `document_verbatim` |
| `release_sources` | No row values | Release metadata without verbatim member paths: every column except `release_id`, `member` and `document_verbatim`. `container_path` is relative to the repo or evidence directory |
| `truth.releases` | No row values | Not `release_id` or `member`. For release metadata, export from `release_sources` as above |
| `truth.release_dispositions`, `truth.release_layouts` | No row values | Safe by content: their `release_pattern`s hold no folder names |
| `release_content_groups` | No row values | Local only as is: `releases` lists verbatim `release_id`s. Map them to `public_release_id` first |
| `events` | No: ids, counts and producer names | Safe by content. `event_id` lets readers count searches rather than sightings |
| `cache.sighting_event` | No: ids and hashes | Not `release_id`: join through `release_sources` and export `public_release_id` |
| `truth.build_info` | No | Local only: it holds absolute local paths (`repo_checkout`, `evidence_dir`) |
| `truth.flock_audit_rows`, `truth.smpd_pdf_rows`, `truth.flock_event_rows` | **Yes**: raw plates in some releases, free text, verbatim release names | Local only |
| `flock_rows`, `sightings`, `sightings_flock`, `sightings_smpd` | **Yes**: `*_surface`, `reason`, `case_no`, `text_prompt`, `filters` raw | Local only |
| `event_log` | **Yes**: hotlist entries hold plates; `user` includes e-mail addresses | Local only (no public version exists) |
| `read_field`, `event_sightings` | **Yes**: surface text | Local only |
| `plate_key_hex()`, `plate_key_pads()` | The key | Never select them |
| `truth.duckdb`, `derived.duckdb` as files | Yes | Never published |

## Pre-export check

Run it on every release (or producer) you export. It returns counts only, never a value. `PLATE_CHECK` guards row
parity and the plate column. `TEXT_CHECK` removes the tokens and then looks for plate shapes, stray token text and
address shapes in the text columns. Its plate checks and its `ca_word` and `ca_glued` shapes are the ones the
corpus-wide check in `gen_stats.py` uses (below); the other columns are for review.

```python
PRID = "<public_release_id>"
RID = con.execute("SELECT release_id FROM release_sources WHERE public_release_id = $p",   # local only: may hold
                  {"p": PRID}).fetchone()[0]                                             # zip folder names
PLATE_CHECK = r"""
SELECT count(*)                                                                  AS rows_public,
       (SELECT count(*) FROM sightings WHERE release_id = $rid)                  AS rows_sightings,
       count(*) FILTER (WHERE plate_state = 'value')                             AS plate_values,
       count(*) FILTER (WHERE plate_state = 'value'
                          AND NOT regexp_full_match(plate, 'p1_[0-9a-f]{16}'))   AS plate_value_not_token,
       count(*) FILTER (WHERE plate IS NOT NULL AND NOT regexp_full_match(plate, 'p1_[0-9a-f]{16}')
                          AND NOT (trim(plate) IN ('', '***') OR agency_mask(plate) OR exemption_cite(plate)))
                                                                                 AS plate_raw_passed
FROM sightings_public WHERE public_release_id = $prid"""
TEXT_CHECK = r"""
WITH f AS (
  SELECT unnest(['reason', 'case_no', 'filters', 'text_prompt', 'searcher_name', 'search_type']) AS field,
         unnest([reason, case_no, filters, text_prompt, searcher_name, search_type])         AS v
  FROM sightings_public WHERE public_release_id = $prid),
g AS (SELECT field, v, regexp_replace(v, '\[p1_[0-9a-f]{16}\]', ' ', 'g') AS rest FROM f WHERE v IS NOT NULL)
SELECT field,
  count(*)                                                                         AS cells,
  count(*) FILTER (WHERE regexp_matches(v, '\[p1_[0-9a-f]{16}\]'))                AS with_token,
  count(*) FILTER (WHERE regexp_matches(rest, 'p1_'))                              AS stray_p1,
  count(*) FILTER (WHERE regexp_matches(rest, '(?i)\b[0-9][A-Z]{3}[0-9]{3}\b'))    AS ca_word,
  count(*) FILTER (WHERE regexp_matches(rest,
    '(?i)(?:^|[^a-z0-9]|[a-z])[0-9][a-z]{3}[0-9]{3}(?:$|[^a-z0-9])'))              AS ca_glued,
  count(*) FILTER (WHERE regexp_matches(rest, '(?i)[0-9][A-Z]{3}[0-9]{3}'))        AS ca_in_run,
  count(*) FILTER (WHERE regexp_matches(rest,
    '(?i)\b([0-9]{5}[A-Z][0-9]|[0-9][A-Z][0-9]{5}|[0-9][A-Z]{2}[0-9]{4}|[0-9][A-Z][0-9][A-Z][0-9]{3}|[A-Z]{2}[0-9]{2}[A-Z][0-9]{2})\b'))
                                                                                   AS tier_b_shape,
  count(*) FILTER (WHERE regexp_matches(rest, '(?i)\b([A-Z]{3}[0-9]{4}|[A-Z]{2}[0-9]{5}|[A-Z][0-9]{7})\b'))
                                                                                   AS never_shapes,
  count(*) FILTER (WHERE regexp_matches(rest, '(?i)(^|[^0-9a-z#-])[0-9]{2,6}[a-z]?(\s+[a-z0-9.''-]+){1,3}\s+'
    || '(st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|way|ct|court|ln|lane|pl|place|cir|circle|hwy|pkwy)\b'))
                                                                                   AS addr_like,
  count(*) FILTER (WHERE regexp_matches(v, '\[(dob|phone|ssn|dl|addr|name)\]'))    AS with_scrub_marker
FROM g GROUP BY field ORDER BY field"""
print(con.execute(PLATE_CHECK, {"rid": RID, "prid": PRID}).fetchall())
for r in con.execute(TEXT_CHECK, {"prid": PRID}).fetchall():
    print(r)
```

A field with no non-NULL cells has no row. `with_token` counts cells that carry at least one token; it is not a
failure. To check a whole producer, filter on `producer = '…'` instead, in the `rows_sightings` subquery too.

| Column | Must be | Meaning if not |
|---|---|---|
| `rows_public` = `rows_sightings`, and > 0 | equal, non-zero | Rows lost between `sightings` and `sightings_public`, or the wrong release. A missing or malformed key raises an error before this point |
| `plate_value_not_token` | 0 | A plate value escaped tokenization |
| `plate_raw_passed` | 0 | A plate cell that is not a token, blank or a known mask reached the output. `plate_public` makes this 0 by construction; the count guards the code |
| `stray_p1` | 0 | Token-like text outside the `[p1_<16 hex>]` form |
| `ca_word` | 0 in every field | A `9AAA999` word left in text. 0 by construction: it is tokenized in every free-text field and in Filters, and `search_type` rejects digits |
| `ca_glued` | 0 in `filters` and `search_type`; review elsewhere | In Filters, a `9AAA999` standing alone or after letters that the Filters rules missed. In the other fields it counts crumbs such as `CA0XXX000` |
| `ca_in_run`, `tier_b_shape`, `never_shapes` | review | Crumbs left on purpose: shapes the rules do not tokenize because they are also case-number formats or lack context ([Residual risk](#residual-risk)). Look at the flagged rows locally in `sightings`, by shape, before publishing |
| `addr_like` | review | Text that still looks like a house number and street: lower-case addresses, an address after a case keyword, and prose false positives (`Report 2024 on the way`) |
| `with_scrub_marker` | review | Cells the scrub changed |

To look at flagged rows without printing a value, print shapes:
`regexp_replace(regexp_replace(reason_surface, '[0-9]', '9', 'g'), '[A-Za-z]', 'A', 'g')`.

**Corpus-wide: `gen_stats.py`.** It writes the section "Pre-export check: plate residue in `sightings_public`" of
[stats.md](stats.md), over every row of `sightings_public`, with tokens removed first and counts only:

- `9AAA999` as a word (any case, leading `0` allowed) in `reason`, `case_no`, `text_prompt`, `filters` and
  `searcher_name`;
- the `ca_glued` shape in `filters` and in `search_type` (the row labelled "9AAA999 anywhere");
- plate `value` cells that are not tokens, and plate cells that are not a token, blank or a known mask
  (`plate_raw_passed`).

**Row-count guard:** it reports residue only when `sightings_public` has exactly as many rows as `sightings`, counted
in the same run. Otherwise it writes "Not reported", marks the run incomplete and exits 1, because residue counts over
a view that lost rows would read as a pass. Any non-zero residue count also makes it exit 1. It needs the key (the
view errors without it) and never prints the key or a value. It does not count `tier_b_shape`, `never_shapes`,
`addr_like` or scrub markers: use the per-release check for those. In the current [stats.md](stats.md) every residue
count is 0.

## Exporting with citations

A public row is only useful if a reader can find it in the original. Join the public provenance columns on
(`public_release_id`, `row_no`) and filter both sides by release so each branch prunes:

```python
PRID = "<public_release_id>"
con.execute("""COPY (
  SELECT p.* EXCLUDE (sighting_id), ss.citation, ss.open_url, ss.sha256 AS source_sha256,
         ss.member_sha256 AS source_member_sha256
  FROM sightings_public p JOIN sighting_sources ss USING (public_release_id, row_no)
  WHERE p.public_release_id = $prid AND ss.public_release_id = $prid) TO 'release_public.parquet' (FORMAT parquet)""",
  {"prid": PRID})
# one row per released row of the release, each with its own citation
```

Reading the Parquet file needs no key and no database. In anything you publish, cite a row by its `citation` (file,
sheet or page, row, link, SHA-256), and refer to plates only by token. The released original shows the raw value to
anyone who opens it, so do not quote it next to the token.
