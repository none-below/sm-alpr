#!/usr/bin/env python3
"""
Build per-agency justification frequency data from audit-log JSONs.

For each agency that publishes a Public Search Audit (CSV embedded in the
Flock transparency portal), aggregates the free-text "reason" field into:

  - `verbatim`: top whole-string reasons by count (case-folded)
  - `tokens`: top word frequencies after tokenization
  - `penal_codes`: detected California penal/vehicle-code numbers with
    short descriptions (e.g. "10851" -> "Vehicle theft (CVC)")
  - `stats`: row count, unique reason count, blank-reason rate,
    median reason length, share of rows covered by the top-1 reason

Display sizing: capped at TOP_VERBATIM / TOP_TOKENS by frequency. The
underlying audit text is already public on the agency's Flock
transparency portal, so we don't impose an additional minimum-count
floor — the cap alone keeps payload size and visual density sane.

Token-level scrubbing still runs as a sanity guard against accidental
inclusion of plate fragments or case-number-shaped strings in the
token cloud:
  - Long pure-digit strings (>=8 digits) are excluded as likely case
    numbers / dates / plate fragments.
  - Plate-pattern strings (mixed letter+digit, 5-8 chars) are excluded.

Output: docs/data/justifications.json
"""

import json
import re
import statistics
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from lib import load_registry, portal_jsons  # noqa: E402

AUDIT_DIR = Path("docs/data/audit")
PORTAL_DIR = Path("assets/transparency.flocksafety.com")
OUT_PATH = Path("docs/data/justifications.json")

TOP_VERBATIM = 50
TOP_TOKENS = 50

# Words that don't carry signal in a justification cloud — common
# English stopwords plus ALPR-specific filler ("vehicle", "search",
# "case", "plate") that would otherwise dominate every cloud.
STOPWORDS = {
    "a", "an", "and", "the", "of", "to", "in", "on", "for", "with",
    "by", "at", "or", "is", "was", "be", "as", "from", "this", "that",
    "it", "its", "into", "out", "no", "not",
    # ALPR filler — uncomment lines below if a stopword turns out to be
    # interesting in some agency's data; we'd rather over-include words
    # in the prototype than silently delete signal.
    "vehicle", "veh", "vehicles", "plate", "plates", "license", "lic",
    "search", "searches", "lookup", "lookups", "case", "report",
    # Code-section suffixes (PC = Penal Code, CVC = Vehicle Code,
    # HSC = Health & Safety, WIC = Welfare & Institutions). These
    # appear glued to code numbers in some agencies' templates and
    # would otherwise dominate the cloud without carrying signal —
    # the code number itself, surfaced separately in penal_codes,
    # carries the meaning.
    "pc", "cvc", "hsc", "wic", "vc", "cvv", "hs",
}

# Case-folded raw reason strings to drop entirely (pure noise — punct
# stripped, the empty result, and the literal word "test").
DROP_VERBATIM = {"", "-", ".", "n/a", "na", "none", "test", "testing"}

# California penal / vehicle / health-and-safety codes commonly seen as
# ALPR justifications. Numbers only — letters in case numbers (suffix
# like 484a, 211(a)(2)) get normalized to the bare number for matching.
# Keep this list tight and well-cited; an out-of-date or wrong gloss
# would be embarrassing in a public tool. See CA Penal Code, CA Vehicle
# Code, CA Health & Safety Code, CA Welfare & Institutions Code.
PENAL_CODES = {
    # Vehicle Code
    "10851": "Vehicle theft (CVC)",
    "20001": "Hit and run with injury (CVC)",
    "20002": "Hit and run, property (CVC)",
    "23103": "Reckless driving (CVC)",
    "23152": "DUI (CVC)",
    "23153": "DUI causing injury (CVC)",
    "2800":  "Failure to yield to officer (CVC)",
    "14601": "Driving on suspended license (CVC)",
    # Penal Code — violence
    "187":   "Murder (PC)",
    "192":   "Manslaughter (PC)",
    "211":   "Robbery (PC)",
    "215":   "Carjacking (PC)",
    "220":   "Assault to commit a felony (PC)",
    "240":   "Assault (PC)",
    "242":   "Battery (PC)",
    "243":   "Battery — specific (PC)",
    "245":   "Assault with a deadly weapon (PC)",
    "246":   "Shooting at occupied vehicle/dwelling (PC)",
    "261":   "Rape (PC)",
    "273.5": "Domestic violence (PC)",
    "288":   "Lewd act on child (PC)",
    # Penal Code — property
    "459":   "Burglary (PC)",
    "460":   "Burglary — degree (PC)",
    "466":   "Burglary tools (PC)",
    "470":   "Forgery (PC)",
    "484":   "Theft (PC)",
    "487":   "Grand theft (PC)",
    "488":   "Petty theft (PC)",
    "490.4": "Organized retail theft (PC)",
    "496":   "Receiving stolen property (PC)",
    "496d":  "Receiving stolen vehicle (PC)",
    "530.5": "Identity theft (PC)",
    "594":   "Vandalism (PC)",
    # Penal Code — other
    "207":   "Kidnapping (PC)",
    "314":   "Indecent exposure (PC)",
    "415":   "Disturbing the peace (PC)",
    "422":   "Criminal threats (PC)",
    "451":   "Arson (PC)",
    "452":   "Reckless burning (PC)",
    # Penal Code — weapons
    "417":   "Brandishing a weapon (PC)",
    "25400": "Carrying a concealed firearm (PC)",
    "25850": "Carrying a loaded firearm (PC)",
    "29800": "Felon in possession of firearm (PC)",
    # Health & Safety Code (drugs)
    "11350": "Possession of controlled substance (HSC)",
    "11351": "Possession for sale (HSC)",
    "11352": "Sale/transport, narcotics (HSC)",
    "11377": "Possession of methamphetamine (HSC)",
    "11378": "Possession for sale, methamphetamine (HSC)",
    "11379": "Sale/transport, methamphetamine (HSC)",
    # Welfare & Institutions Code
    "5150":  "Mental-health hold (WIC)",
    "300":   "Dependent child (WIC)",
    "601":   "Status offense — minor (WIC)",
    "602":   "Delinquent minor (WIC)",
    # Common radio / status codes (10-codes vary by agency, but these
    # are widely standardized in CA law-enforcement usage)
    "1030":  "Stolen vehicle (radio code)",
    "1065":  "Missing person (radio)",
    "10-65": "Missing person (radio)",
    # CVC sections that no longer exist as primary statutes (repealed
    # / renumbered) but still appear in agency justification text as
    # informal shorthand. Labelled "(historic)" so the reader knows the
    # current code section is different.
    "23101": "Reckless / DUI causing injury (CVC, historic)",
}

PUNCT_RE = re.compile(r"[^\w\s./()-]+", re.UNICODE)
TOKEN_RE = re.compile(r"[A-Za-z]{2,}|[0-9]{2,5}(?:\.[0-9]+)?")
CODE_RE = re.compile(r"\b([0-9]{3,5}(?:\.[0-9]+)?)\b")
LONG_DIGITS_RE = re.compile(r"\b\d{8,}\b")
# Code-section abbreviations seen in agency audit data. "vc" is short
# for CVC; "cvv" is a common typo for CVC. Order in the alternation
# matters only for greedy matching — we put longer ones first so
# "cvc" matches before "vc" would. Strip these so the bare number
# can be tokenized and matched against PENAL_CODES regardless of
# whether the agency writes them prefix-first ("PC459") or suffix-
# style ("459PC", "10851VC"). Without the strip, the combined string
# is mixed letter+digit, 5-8 chars — plate-shaped — and PLATE_RE
# would silently drop it.
_CODE_AFFIX = r"(?:cvc|cvv|hsc|wic|hs|pc|vc)"
CODE_PREFIX_RE = re.compile(r"\b" + _CODE_AFFIX + r"\s*(\d+(?:\.\d+)?)\b")
CODE_SUFFIX_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*" + _CODE_AFFIX + r"\b")
# Case/report numbers used as the entire justification — typical formats
# are YY-NNNNN, YYYY-NNNNN, or RDNN-NNNNN. A case number identifies a
# single incident, so re-using one across many searches over many days
# is the pattern audit systems are supposed to surface for review.
CASE_NUMBER_RE = re.compile(r"^\d{2,4}-\d{3,8}$")
# Plate-like: a mix of letters and digits, 6-8 chars total (CA plates
# are typically 7 chars). 5-char strings caught up too many short
# code-shaped tokens like "1030f" or "459pc" (without an affix to
# strip), so we tightened from 5- to 6-char minimum. The cost is that
# rare 5-char specialty plates can leak through; the win is that
# agency dispatch codes don't get silently dropped.
PLATE_RE = re.compile(r"\b(?=[A-Za-z0-9]{6,8}\b)(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]+\b")


def normalize_reason(raw):
    """Return a normalized whole-reason string.

    Truly empty / whitespace-only input becomes BLANK_LABEL so it
    surfaces in the chart as a discrete row. Filler entries that are
    in DROP_VERBATIM (test, n/a, etc.) return None and are dropped
    from the chart entirely.
    """
    if raw is None:
        return BLANK_LABEL
    s = str(raw).strip().lower()
    if not s:
        return BLANK_LABEL
    s = PUNCT_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return BLANK_LABEL
    if s in DROP_VERBATIM:
        return None
    return s


def tokenize(reason_norm):
    """Yield meaningful tokens from a normalized reason string.

    Drops stopwords, plate-pattern fragments, and >=8-digit runs.
    Keeps short numeric codes (3-5 digits) for penal-code matching.
    """
    if not reason_norm:
        return
    s = CODE_PREFIX_RE.sub(r"\1", reason_norm)
    s = CODE_SUFFIX_RE.sub(r"\1", s)
    s = LONG_DIGITS_RE.sub(" ", s)
    s = PLATE_RE.sub(" ", s)
    for m in TOKEN_RE.finditer(s):
        tok = m.group(0)
        if tok in STOPWORDS:
            continue
        yield tok


def detect_codes(phrase):
    """Return ordered list of [code, label] for codes detected in a phrase.

    `phrase` is the already-normalized (lowercased, punct-tamed) reason
    string. Runs the same prefix-strip + plate/long-digit guards as the
    aggregate tokenizer so a phrase like "pc459/cvc10851" yields both
    Burglary and Vehicle theft labels in their original order.
    """
    if not phrase:
        return []
    s = CODE_PREFIX_RE.sub(r"\1", phrase)
    s = CODE_SUFFIX_RE.sub(r"\1", s)
    s = LONG_DIGITS_RE.sub(" ", s)
    s = PLATE_RE.sub(" ", s)
    out = []
    seen = set()
    for m in TOKEN_RE.finditer(s):
        tok = m.group(0)
        label = PENAL_CODES.get(tok)
        if label and tok not in seen:
            seen.add(tok)
            out.append([tok, label])
    return out


def codes_from_tokens(token_counter):
    """Pull penal-code-like tokens out of the token counter.

    Only emits codes we have a label for, so unmatched numbers stay in
    the generic token list.
    """
    out = []
    for tok, count in token_counter.most_common():
        label = PENAL_CODES.get(tok)
        if label:
            out.append([tok, label, count])
    return out


def latest_portal_uses(slug):
    """Return (acceptable_use_policy, prohibited_uses) from the most recent
    portal JSON for `slug`, or (None, None) if no portal data is available.

    The agency's own published statement of permissible / prohibited uses
    is the contract the page's free-text reasons should be measured
    against — surfacing it inline lets the reader judge fit without
    cross-referencing the transparency portal.
    """
    portal_dir = PORTAL_DIR / slug
    if not portal_dir.is_dir():
        return None, None
    paths = portal_jsons(portal_dir)
    if not paths:
        return None, None
    try:
        d = json.loads(paths[-1].read_text())
    except (OSError, json.JSONDecodeError):
        return None, None
    aup = (d.get("acceptable_use_policy") or "").strip() or None
    pu = (d.get("prohibited_uses") or "").strip() or None
    return aup, pu


PT = ZoneInfo("America/Los_Angeles")

# Sentinel for "the agency entered no reason / blank justification."
# Surfacing this as a real chart row makes the blank-rate visible at
# a glance — the alternative (a side stat tile) buries the signal.
BLANK_LABEL = "(blank)"


def parse_search_date_pt(s):
    """Parse a Flock searchDate ISO timestamp and return aware-local PT.

    Flock emits ISO-8601 with a trailing 'Z' (UTC). Returns None on
    parse failure so a single bad row doesn't tank the whole agency.
    """
    if not s:
        return None
    try:
        # datetime.fromisoformat in 3.11+ accepts trailing 'Z'; for
        # older versions, swap it for +00:00 explicitly.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(PT)


LONG_ACTIVE_MIN_COUNT = 10
LONG_ACTIVE_MIN_DAYS = 7
LONG_ACTIVE_TOP = 8


def compute_phrase_details(phrases_of_interest, rows):
    """Return {phrase: detail-dict} for each phrase in the input set.

    detail-dict: {days, hourly[24], weekly[7], from, to,
    nc_min, nc_med, nc_max, nc_over_100} — a per-phrase analog of the
    agency-wide stats. Phrases not present in the rows yield None.
    Single pass over rows so cost is O(N) per agency, not O(N*K).
    """
    if not phrases_of_interest:
        return {}
    accum = {
        p: {"hourly": [0] * 24, "weekly": [0] * 7, "ncs": [],
            "dates": Counter()}
        for p in phrases_of_interest
    }
    for r in rows:
        raw = r.get("reason")
        if raw is None or not str(raw).strip():
            raw = r.get("offenseType")
        if raw is None or not str(raw).strip():
            raw = r.get("caseNumber")
        norm = normalize_reason(raw)
        if norm not in accum:
            continue
        a = accum[norm]
        dt = parse_search_date_pt(r.get("searchDate"))
        if dt is not None:
            a["hourly"][dt.hour] += 1
            a["weekly"][dt.weekday()] += 1
            a["dates"][dt.date().isoformat()] += 1
        nc_raw = r.get("networkCount")
        try:
            if nc_raw not in (None, ""):
                a["ncs"].append(int(nc_raw))
        except (TypeError, ValueError):
            pass
    out = {}
    for phrase, a in accum.items():
        ncs = sorted(a["ncs"])
        n = len(ncs)
        date_counts = a["dates"]
        d = {
            "days": len(date_counts),
            "hourly": a["hourly"],
            "weekly": a["weekly"],
        }
        if date_counts:
            from_iso = min(date_counts)
            to_iso = max(date_counts)
            from_d = date.fromisoformat(from_iso)
            to_d = date.fromisoformat(to_iso)
            span = (to_d - from_d).days + 1
            d["from"] = from_iso
            d["to"] = to_iso
            d["span_days"] = span
            # daily series across the calendar span (zero-filled), so
            # the JS sparkline shows gaps as empty cells. Cap at 365
            # to keep payload bounded for multi-year cases; if longer,
            # downsample to weekly buckets.
            if span <= 365:
                series = []
                cur = from_d
                while cur <= to_d:
                    series.append(date_counts.get(cur.isoformat(), 0))
                    cur += timedelta(days=1)
                d["daily"] = series
                d["daily_unit"] = "day"
            else:
                # Weekly buckets: each entry is searches in a 7-day
                # window starting at from_d.
                series = []
                cur = from_d
                while cur <= to_d:
                    week_total = 0
                    for offset in range(7):
                        week_total += date_counts.get(
                            (cur + timedelta(days=offset)).isoformat(), 0
                        )
                    series.append(week_total)
                    cur += timedelta(days=7)
                d["daily"] = series
                d["daily_unit"] = "week"
        if ncs:
            d["nc_min"] = ncs[0]
            d["nc_med"] = ncs[n // 2]
            d["nc_max"] = ncs[-1]
            d["nc_over_100"] = sum(1 for x in ncs if x >= 100)
        out[phrase] = d
    return out


def find_long_active_cases(rows):
    """Return case-number-shaped phrases reused across many searches and days.

    Each entry: [phrase, count, distinct_days, date_min, date_max,
    median_network_count]. Capped at LONG_ACTIVE_TOP, sorted by count.
    A case number is meant to identify one incident; reuse across many
    searches over a long span is what audit-trail review is supposed
    to flag.
    """
    by_phrase = {}
    for r in rows:
        raw = r.get("reason")
        if raw is None or not str(raw).strip():
            raw = r.get("offenseType")
        if raw is None or not str(raw).strip():
            raw = r.get("caseNumber")
        if not raw:
            continue
        norm = normalize_reason(raw)
        if not norm or not CASE_NUMBER_RE.match(norm):
            continue
        date = (r.get("searchDate") or "")[:10]
        nc_raw = r.get("networkCount")
        try:
            nc = int(nc_raw) if nc_raw not in (None, "") else None
        except (TypeError, ValueError):
            nc = None
        bucket = by_phrase.setdefault(norm, {"dates": [], "ncs": []})
        if date:
            bucket["dates"].append(date)
        if nc is not None:
            bucket["ncs"].append(nc)
    out = []
    for phrase, b in by_phrase.items():
        dates = b["dates"]
        if not dates:
            continue
        distinct = len(set(dates))
        count = len(dates)
        if count < LONG_ACTIVE_MIN_COUNT or distinct < LONG_ACTIVE_MIN_DAYS:
            continue
        ncs = sorted(b["ncs"])
        median_nc = ncs[len(ncs) // 2] if ncs else None
        out.append([
            phrase,
            count,
            distinct,
            min(dates),
            max(dates),
            median_nc,
        ])
    out.sort(key=lambda x: -x[1])
    return out[:LONG_ACTIVE_TOP]


def process_agency(audit_path):
    """Return a per-agency dict, or None if the agency has no usable rows."""
    try:
        data = json.loads(audit_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    rows = data.get("rows") or []
    if not rows:
        return None

    verbatim_counts = Counter()
    token_counts = Counter()
    blank = 0
    lengths = []
    # 7 (Mon=0..Sun=6) x 24 (hour-of-day, PT). Sized at build time so
    # agencies with no overlap with a particular cell still emit a 0
    # in that slot — the heatmap renders sparser cells correctly.
    hour_dow = [[0] * 24 for _ in range(7)]
    timed_rows = 0

    for r in rows:
        # Field-fallback chain so each agency's most populated field
        # gets used: reason (free-text) > offenseType (NIBRS-aligned
        # dropdown) > caseNumber (raw case ID). Different agencies
        # publish different combinations; some publish only one of
        # the three. The conceptual differences are real but for an
        # aggregate justification view they serve the same role.
        raw = r.get("reason")
        if raw is None or not str(raw).strip():
            raw = r.get("offenseType")
        if raw is None or not str(raw).strip():
            raw = r.get("caseNumber")
        norm = normalize_reason(raw)
        if norm is None:
            # Filler (test / n/a) — counted as blank-equivalent for the
            # blank-rate stat but not surfaced as its own row.
            blank += 1
        elif norm == BLANK_LABEL:
            blank += 1
            verbatim_counts[norm] += 1
        else:
            verbatim_counts[norm] += 1
            lengths.append(len(norm))
            for tok in tokenize(norm):
                token_counts[tok] += 1
        dt = parse_search_date_pt(r.get("searchDate"))
        if dt is not None:
            hour_dow[dt.weekday()][dt.hour] += 1
            timed_rows += 1

    if not verbatim_counts:
        return None

    # Verbatim row format: [phrase, count, codes_or_null, detail]
    # codes_or_null is [[code, label], ...] or null.
    # detail is a per-phrase stats dict (see compute_phrase_details).
    top_verbatim_pairs = list(verbatim_counts.most_common(TOP_VERBATIM))
    top_phrase_set = {p for p, _ in top_verbatim_pairs}
    # Also include long-active case-number phrases so the callout
    # rows can expand to the same detail panel as bars/cloud.
    long_active = find_long_active_cases(rows)
    for entry in long_active:
        top_phrase_set.add(entry[0])
    phrase_details = compute_phrase_details(top_phrase_set, rows)
    top_verbatim_filtered = []
    for phrase, c in top_verbatim_pairs:
        # BLANK_LABEL is a synthetic phrase — it has no meaningful
        # tokens or codes to detect, so don't run the matcher on the
        # literal string "(blank)".
        codes = [] if phrase == BLANK_LABEL else detect_codes(phrase)
        top_verbatim_filtered.append([
            phrase,
            c,
            codes if codes else None,
            phrase_details.get(phrase),
        ])
    top_tokens_filtered = [
        [tok, c]
        for tok, c in token_counts.most_common(TOP_TOKENS)
        if tok not in PENAL_CODES
    ]
    code_rows = codes_from_tokens(token_counts)

    total = len(rows)
    top1 = verbatim_counts.most_common(1)[0][1] if verbatim_counts else 0
    top3 = sum(c for _, c in verbatim_counts.most_common(3))

    # Schema-level transparency check: did the agency's audit CSV
    # include any of the three fields that could record WHY a search
    # was run? Some agencies (Fontana, Menlo Park) publish audits
    # whose schema is just [id, networkCount, searchDate, userId] —
    # the justification field is structurally absent, not just blank.
    schema = set(data.get("schema_seen") or [])
    has_justification_column = bool(
        schema & {"reason", "offenseType", "caseNumber"}
    )

    return {
        "row_count": total,
        "audit_schema": sorted(schema),
        "has_justification_column": has_justification_column,
        "blank_reasons": blank,
        "unique_reasons": len(verbatim_counts),
        "verbatim_shown": len(top_verbatim_filtered),
        "tokens_shown": len(top_tokens_filtered),
        "median_length_chars": int(statistics.median(lengths)) if lengths else 0,
        "top1_share_pct": round(100.0 * top1 / total, 1) if total else 0.0,
        "top3_share_pct": round(100.0 * top3 / total, 1) if total else 0.0,
        "search_date_min": data.get("search_date_min"),
        "search_date_max": data.get("search_date_max"),
        "verbatim": top_verbatim_filtered,
        "tokens": top_tokens_filtered,
        "penal_codes": code_rows,
        # Day-of-week × hour-of-day heatmap, in America/Los_Angeles.
        # Rows are weekdays Monday=0..Sunday=6 (ISO ordering).
        "hour_dow": hour_dow,
        "timed_rows": timed_rows,
        # Case-number-only phrases reused across many searches and days.
        "long_active_cases": long_active,
        # Per-phrase detail keyed by the verbatim phrase string. The
        # JS expand panel reads from this so all three entry points
        # (bar row, cloud word, callout row) hit the same data.
        "phrase_details": phrase_details,
    }


def main():
    if not AUDIT_DIR.exists():
        print(f"missing {AUDIT_DIR}; run build_audit_log.py first", file=sys.stderr)
        return 1
    registry = load_registry()
    by_slug = {}
    for entry in registry:
        slug = entry.get("slug")
        if not slug:
            continue
        by_slug[slug] = entry
        for fs in entry.get("flock_slugs") or []:
            by_slug.setdefault(fs, entry)

    out = {}
    skipped = 0
    for path in sorted(AUDIT_DIR.glob("*.json")):
        slug = path.stem
        entry = by_slug.get(slug)
        agency_data = process_agency(path)
        if agency_data is None:
            skipped += 1
            continue
        agency_data["slug"] = slug
        agency_data["display_name"] = (
            (entry.get("display_name") if entry else None)
            or (entry.get("flock_names") or [None])[0] if entry else None
        ) or slug
        aup, pu = latest_portal_uses(slug)
        if aup:
            agency_data["acceptable_use_policy"] = aup
        if pu:
            agency_data["prohibited_uses"] = pu
        out[slug] = agency_data

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_from": str(AUDIT_DIR),
        "top_verbatim_cap": TOP_VERBATIM,
        "top_tokens_cap": TOP_TOKENS,
        "agency_count": len(out),
        "agencies": out,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    print(f"wrote {OUT_PATH}: {len(out)} agencies, skipped {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
