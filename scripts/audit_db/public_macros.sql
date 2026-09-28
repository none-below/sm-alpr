-- Civilian-PII scrub for public outputs. Policy (2026-09-25): tokenize civilian plates everywhere public, raw only in local
-- truth; officer fields verbatim; scrub other civilian identifiers. PRECISION FIRST (user): only transform what is very likely
-- civilian PII -- case numbers must survive. Pure functions, computed on read. Key pads are passed in (lambdas can't subquery).

-- ---- plate tokens = HMAC-SHA256(key, 'plate:v1:' || plate_norm(p)), first 16 hex ------------------------------------------
-- Key read on use from ~/.config/sm-alpr/plate_token_key (CI: plate_key.py --install writes the PLATE_TOKEN_KEY secret there);
-- it is never stored in any .duckdb file, and no error message ever carries it. Key text is normalized the same way as
-- plate_key.py: every space, tab, CR and LF removed, then it must be 64 hex digits (case-insensitive; lower-cased).
-- A missing, empty or malformed key file raises an error, so plate_token() and sightings_public fail loudly instead of
-- returning NULL tokens or an empty view. Only needed to tokenize raw plates; reading tokenized published data needs no key.
CREATE OR REPLACE MACRO plate_norm(p) AS nullif(upper(regexp_replace(p, '[^A-Za-z0-9]', '', 'g')), '');
CREATE OR REPLACE MACRO plate_pad(khex, a, b) AS unhex(list_aggregate(list_transform(range(0, 128), i ->
  substr('0123456789abcdef', xor(instr('0123456789abcdef', substr(rpad(lower(trim(khex)), 128, '0'), i + 1, 1)) - 1,
                                 CASE WHEN i % 2 = 0 THEN a ELSE b END) + 1, 1)), 'string_agg', ''));
CREATE OR REPLACE MACRO plate_key_hex() AS TABLE
  SELECT CASE WHEN regexp_full_match(k, '[0-9a-f]{64}') THEN k
              ELSE error('plate token key missing, empty or not 64 hex digits: ~/.config/sm-alpr/plate_token_key '
                         || '(create with: openssl rand -hex 32; CI: plate_key.py --install)') END AS khex
  FROM (SELECT lower(regexp_replace(coalesce(first(content), ''), '[ \t\r\n]', '', 'g')) AS k
        FROM read_text('~/.config/sm-alpr/plate_token_key'));
CREATE OR REPLACE MACRO plate_key_pads() AS TABLE
  SELECT plate_pad(khex, 3, 6) AS pi, plate_pad(khex, 5, 12) AS po FROM plate_key_hex();
CREATE OR REPLACE MACRO plate_hmac(p, pi, po) AS
  'p1_' || left(sha256(po || unhex(sha256(pi || encode('plate:v1:' || plate_norm(p))))), 16);
CREATE OR REPLACE MACRO plate_token(p) AS (SELECT plate_hmac(p, k.pi, k.po) FROM plate_key_pads() k);

-- The plate column: 'value' cells -> token; empty cells and known masks ('***', agency masks, exemption citations) as
-- released; anything else (e.g. 'partial': 'REDACTED' + text) -> NULL, so no raw plate text can reach a public output.
CREATE OR REPLACE MACRO plate_public(surface, state, pi, po) AS CASE
  WHEN state = 'value' THEN plate_hmac(surface, pi, po)
  WHEN surface IS NULL OR trim(surface) IN ('', '***') OR agency_mask(surface) OR exemption_cite(surface) THEN surface END;

-- Tier A (always): CA standard plate 9AAA999 -- a plate shape, not any agency's case-number scheme, so it is tokenized in every field (including "Case #").
-- Tier B (context only): commercial/trailer/other plate shapes, only right after a plate word or as the entire field.
-- Never auto-tokenized: AAA9999, AA99999, AAA999, A9999999 (case-number formats; A9999999 is also the CA DL format).
CREATE OR REPLACE MACRO plate_candidates(txt) AS list_distinct(list_concat(
  regexp_extract_all(coalesce(txt, ''), '(?i)\b[0-9][A-Z]{3}[0-9]{3}\b'),
  regexp_extract_all(coalesce(txt, ''),
    '(?i)\b(plate|lp|lic|license|tag|stolen plate|alert)\b[^A-Za-z0-9]{0,15}([0-9]{5}[A-Z][0-9]|[0-9][A-Z][0-9]{5}|[0-9][A-Z]{2}[0-9]{4}|[0-9][A-Z][0-9][A-Z][0-9]{3}|[A-Z]{2}[0-9]{2}[A-Z][0-9]{2})\b', 2),
  CASE WHEN regexp_full_match(trim(coalesce(txt, '')),
    '(?i)[0-9]{5}[A-Z][0-9]|[0-9][A-Z][0-9]{5}|[0-9][A-Z]{2}[0-9]{4}|[0-9][A-Z][0-9][A-Z][0-9]{3}|[A-Z]{2}[0-9]{2}[A-Z][0-9]{2}')
       THEN [trim(txt)] ELSE [] END));
-- Filters: plate search terms, any case, standalone ('7abc123'), after a tag ('plate:7ABC123') or glued onto a lower-case
-- tag ('stolen7abc123'). Filters are search terms, so tier A + B shapes. A run of letters/digits is a candidate only if the
-- whole run is a plate shape, or a lower-case tag followed by one; a shape is never cut out of a longer run ('0XXX0000').
CREATE OR REPLACE MACRO filter_plate_part(r) AS CASE
  WHEN regexp_full_match(r, '(?i)(?:[0-9][A-Z]{3}[0-9]{3}|[0-9]{5}[A-Z][0-9]|[0-9][A-Z][0-9]{5}|[0-9][A-Z]{2}[0-9]{4}|[0-9][A-Z][0-9][A-Z][0-9]{3}|[A-Z]{2}[0-9]{2}[A-Z][0-9]{2})') THEN r
  -- tier A (9AAA999, always a plate) glued onto ANY letter-only tag: Flock concatenates vehicle attributes onto the plate
  -- in Filters ('Chevrolet7ABC123', 'whitecalifornia7ABC123'); found by the pre-export check, 3,654 cells, 2026-09-26
  WHEN regexp_full_match(r, '[A-Za-z]+[0-9][A-Za-z]{3}[0-9]{3}') THEN right(r, 7)
  WHEN regexp_full_match(r, '[a-z]+(?i:[0-9][A-Z]{3}[0-9]{3}|[0-9]{5}[A-Z][0-9]|[0-9][A-Z][0-9]{5}|[0-9][A-Z]{2}[0-9]{4}|[0-9][A-Z][0-9][A-Z][0-9]{3})') THEN regexp_extract(r, '^[a-z]+(.*)$', 1) END;
CREATE OR REPLACE MACRO filter_plate_candidates(txt) AS list_distinct(list_filter(list_transform(
  regexp_extract_all(coalesce(txt, ''), '[A-Za-z0-9]+'), r -> filter_plate_part(r)), c -> c IS NOT NULL));
-- word = true: replace each candidate between word boundaries (free text). word = false (Filters): the text is cut into
-- runs of letters/digits and the rest; a run that is (or ends in, after a lower-case tag) a plate shape has that part
-- replaced, so adjacent candidates ('9ABC123,8XYZ456') are all replaced and nothing is cut out of a longer run.
CREATE OR REPLACE MACRO tokenize_in(txt, cands, word, pi, po) AS CASE WHEN txt IS NULL THEN NULL
  WHEN word THEN list_reduce(list_prepend(txt, cands),
    (acc, x) -> regexp_replace(acc, '(?i)\b' || x || '\b', '[' || plate_hmac(x, pi, po) || ']', 'g'))
  ELSE array_to_string(list_transform(regexp_extract_all(txt, '[A-Za-z0-9]+|[^A-Za-z0-9]+'),
    r -> CASE WHEN filter_plate_part(r) IS NULL THEN r
              ELSE left(r, length(r) - length(filter_plate_part(r))) || '[' || plate_hmac(filter_plate_part(r), pi, po) || ']' END), '') END;

-- Other civilian identifiers: keyword-anchored only (bare 3-3-4 / 3-2-4 digit runs are often case numbers).
-- chr(57344) (U+E000, private use) is a temporary marker, removed at the end, that keeps a number from being read as a
-- house number: code sections ('459 PC') and numbers right after a case/report keyword ('case 12345', 'DR 2024-123').
-- Released text (including any genuine '§') is otherwise untouched; only a U+E000 already in the released text would
-- also be dropped (private-use, not expected in audit logs).
-- Address: precision first -- a house number standing alone (start, space, ',', ';', ':', '(' or '@' before it; not
-- '23-12345' or '#12345'), then 1-3 Capitalized words (or ordinals: '3rd'), then a street suffix. Only the number is
-- replaced ('[addr] Main St'); lower-case prose ('12345 stolen vehicle from court') is left alone.
CREATE OR REPLACE MACRO scrub_civilian(txt) AS replace(
  regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
  regexp_replace(txt,
    '(?i)\b([0-9]{2,6})(\s*)(PC|VC|HS|HSC|WI|WIC|BP)\b', '\1' || chr(57344) || '\2\3', 'g'),             -- protect code sections
    '(?i)\b(case|report|rpt|incident|inc|cad|event|evt|cfs|file|ticket|citation|cite|booking|warrant|dr|no|nr|num|number|ref|id)(\s*[#:.-]?\s*)([0-9])',
    '\1\2' || chr(57344) || '\3', 'g'),                                                                   -- protect case numbers
    '\b([A-Z][A-Z''-]+,\s*[A-Z][A-Z''-]+)([^\n]{0,30}?)\b(DOB|D\.O\.B|dob)\b', '[name]\2\3', 'g'),              -- LAST, FIRST ... DOB
    '(?i)\b(dob|d\.o\.b\.?|date of birth)\s*[:#-]?\s*[0-9]{1,2}[/.-][0-9]{1,2}[/.-][0-9]{2,4}', '\1 [dob]', 'g'),
    '(?i)\b(ssn|social security|soc sec)\b[^0-9]{0,6}[0-9]{3}-?[0-9]{2}-?[0-9]{4}', '\1 [ssn]', 'g'),
    '(?i)\b(phone|ph|cell|tel|call|contact|rp)\b[^0-9]{0,10}\(?[0-9]{3}\)?[-. ]?[0-9]{3}[-. ][0-9]{4}|\([0-9]{3}\)\s*[0-9]{3}-[0-9]{4}', '[phone]', 'g'),
    '(?i)\b(cdl|dl|dln|driver''?s? ?lic(ense)?|cadl)\b\s*[:#]?\s*[A-Z][0-9]{7}\b', '\1 [dl]', 'g'),
    '(^|[\s,;:(@])[0-9]{2,6}[A-Za-z]?((?:\s+(?:[A-Z][A-Za-z''.-]*|[0-9]+(?:st|nd|rd|th|ST|ND|RD|TH))){1,3}\s+(?i:st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|way|ct|court|ln|lane|pl|place|cir|circle|hwy|pkwy))\b',
    '\1[addr]\2', 'g'),
  chr(57344), '');

-- The ONLY view public exports may read. Officer fields verbatim (public employees, user policy 2026-09-25);
-- civilian plates -> HMAC tokens (plate column, Reason, Case #, Filters, Text Prompt; precision-first shapes);
-- other civilian identifiers in free text -> [dob]/[phone]/[ssn]/[dl]/[addr]/[name]. Errors without a valid key file.
-- Releases are named by public_release_id (no zip folder paths).
CREATE OR REPLACE VIEW sightings_public AS
SELECT s.sighting_id, p.public_release_id, s.row_no, s.src_row, s.producer, s.audit, s.org, s.org_basis, s.t, s.nets, s.devices,
  s.tf_start, s.tf_end, s.flock_id,
  -- a search type (letters/digits/spaces/dashes, e.g. 'apiV1'), never a plate shape: a misaligned cell never leaks
  CASE WHEN regexp_full_match(s.search_type, '[A-Za-z][A-Za-z0-9 -]{0,40}') AND NOT regexp_matches(s.search_type, '(?i)[0-9][a-z]{3}[0-9]{3}')
       THEN s.search_type END AS search_type,
  s.layout_corrected,
  tokenize_in(scrub_civilian(s.reason_surface), plate_candidates(scrub_civilian(s.reason_surface)), true, k.pi, k.po) AS reason,
  s.reason_state,
  tokenize_in(s.case_surface, plate_candidates(s.case_surface), true, k.pi, k.po) AS case_no, s.case_state,
  tokenize_in(s.name_surface, plate_candidates(s.name_surface), true, k.pi, k.po) AS searcher_name, s.name_state,   -- officer names pass; a stray plate does not
  plate_public(s.plate_surface, s.plate_state, k.pi, k.po) AS plate, s.plate_state,
  tokenize_in(scrub_civilian(s.text_prompt), plate_candidates(scrub_civilian(s.text_prompt)), true, k.pi, k.po) AS text_prompt,
  tokenize_in(s.filters, []::VARCHAR[], false, k.pi, k.po) AS filters   -- word = false finds its own runs (filter_plate_part)
FROM sightings s JOIN (SELECT release_id, public_release_id FROM release_sources) p USING (release_id), plate_key_pads() k
WHERE k.pi IS NOT NULL;   -- always true with a valid key; makes every read (even count(*)) check the key
