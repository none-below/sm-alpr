"""Read FBI crime data and derive the searches-per-reported-crime metric.

Data source: per-ORI dated snapshots under assets/cde.ucr.cjis.gov/, written
by ``refresh_fbi.py --dataset crime`` and joined here by load_crime(), keyed by
INDIVIDUAL ORI. An agency's registry ``ori`` field is a LIST
(99% length 1; umbrella agencies like CHP carry many reporting ORIs), so
every total here is summed across that list at read time.

The metric (see build_report_data.py): for the most recent full month of
FBI data, how many ALPR searches did the agency run per reported Part 1
crime? Part 1 total = violent-crime + property-crime — the two
non-overlapping CDE totals; their sub-categories (robbery, burglary, …)
are subsets and must not be added in.

All functions here are pure (no file I/O except load_crime) so the
ratio logic is unit-testable with fixtures.
"""

import calendar
import json
from pathlib import Path

SNAPSHOT_DIR = Path("assets/cde.ucr.cjis.gov")

_OFFENSES = ("violent-crime", "property-crime")
# A month's value must survive at least this many *later* snapshots
# unchanged before the metric trusts it (see _settled_flags). 1 = "held
# through one subsequent fetch." Raise for more caution at the cost of lag.
_STABILITY_MIN_HOLD = 1
_MONTHS = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def join_snapshots(snapshots):
    """Overlay dated per-ORI snapshots into one current-best monthly view.

    ``snapshots`` is an iterable of raw API pulls for a single ORI. We
    apply them oldest->newest, so the latest non-null value wins for each
    month while a month that dropped out of a later pull is retained from
    the earlier one — preserving data through FBI revisions and transient
    API nulls (each snapshot already omits null months). Same join shape
    as build_audit_log.py's union over Flock scrapes.
    """
    merged = {"agency_name": None, "max_data_date": None,
              "last_refresh_date": None,
              "offenses": {off: {} for off in _OFFENSES}}
    for snap in snapshots:
        for off in _OFFENSES:
            merged["offenses"][off].update((snap.get("offenses") or {}).get(off) or {})
        for k in ("agency_name", "max_data_date", "last_refresh_date"):
            if snap.get(k):
                merged[k] = snap[k]
    return merged


def _settled_flags(dated_snaps):
    """Per-(offense, month) stability flags from dated snapshots (ascending).

    A month is "settled" once its value has held across at least
    ``_STABILITY_MIN_HOLD`` later snapshots — i.e. it wasn't (re)written in
    the most recent fetch(es). Snapshots are deltas (a month appears only
    when it changed), so the latest snapshot date that contains a month is
    its last-revision date; counting snapshots after that gives how long it
    has held. Catches values still settling (empty→partial→whole) or being
    revised even when they sit well behind the frontier, which the frontier
    check alone can't. With <2 snapshots there's no history to judge, so we
    don't suppress (bootstrap — the frontier guard still applies downstream).
    """
    flags = {off: {} for off in _OFFENSES}
    if not dated_snaps:
        return flags
    dates = [d for d, _ in dated_snaps]
    n = len(dates)
    last_change = {off: {} for off in _OFFENSES}
    for d, snap in dated_snaps:
        for off in _OFFENSES:
            for m in (snap.get("offenses") or {}).get(off) or {}:
                last_change[off][m] = d
    for off in _OFFENSES:
        for m, lc in last_change[off].items():
            n_after = sum(1 for d in dates if d > lc)
            flags[off][m] = (n < 2) or (n_after >= _STABILITY_MIN_HOLD)
    return flags


def load_crime(path=SNAPSHOT_DIR):
    """Join per-ORI dated snapshots into a dict keyed by ORI, or {}.

    Snapshots live at ``assets/cde.ucr.cjis.gov/<ORI>/<YYYY-MM-DD>.json``
    — append-only raw API pulls, like the Flock transparency scrapes.
    Filenames sort chronologically, so we read them in order, join the
    values (join_snapshots) and attach per-month stability flags
    (_settled_flags). Each record gains a ``settled`` map that crime_monthly
    uses to drop not-yet-stable months. {} until anything has been fetched.
    """
    root = Path(path)
    if not root.is_dir():
        return {}
    out = {}
    for ori_dir in sorted(root.iterdir()):
        if not ori_dir.is_dir():
            continue
        dated = []
        for sp in sorted(ori_dir.glob("*.json")):
            try:
                dated.append((sp.stem, json.loads(sp.read_text())))
            except (OSError, json.JSONDecodeError):
                continue
        if not dated:
            continue
        rec = join_snapshots([s for _, s in dated])
        rec["settled"] = _settled_flags(dated)
        out[ori_dir.name] = rec
    return out


def _ucr_to_iso(m):
    """'04-2026' (CDE MM-YYYY) -> '2026-04' for sortable comparisons."""
    mm, yyyy = m.split("-")
    return f"{yyyy}-{int(mm):02d}"


def month_label(iso_month):
    """'2026-04' -> 'April 2026'."""
    y, m = int(iso_month[:4]), int(iso_month[5:7])
    return f"{_MONTHS[m]} {y}"


def crime_monthly(oris, crime):
    """Sum violent + property per month across an agency's ORI list.

    Returns (months, max_data_iso) where months maps iso-month ->
    {"violent", "property", "total"}, including only months where BOTH
    series are present (so "total" is a real Part 1 sum, never a
    half-reported month). For multi-ORI umbrella agencies the per-month
    sum reflects whichever ORIs reported that month — fine for the
    single-ORI city PDs this metric actually targets; documented fuzz
    for umbrellas.
    """
    violent, property_ = {}, {}
    v_ok, p_ok = {}, {}  # iso-month -> settled for every contributing ORI?
    max_data = ""
    for ori in oris or []:
        rec = crime.get(ori)
        if not rec:
            continue
        md = rec.get("max_data_date")
        if md and "/" in str(md):
            mm, yyyy = str(md).split("/")[:2]
            iso = f"{yyyy}-{int(mm):02d}"
            max_data = max(max_data, iso)
        offenses = rec.get("offenses") or {}
        settled = rec.get("settled") or {}  # absent (e.g. test fixtures) -> assume settled
        for key, dest, okdest in (("violent-crime", violent, v_ok),
                                  ("property-crime", property_, p_ok)):
            sflags = settled.get(key) or {}
            for m, n in (offenses.get(key) or {}).items():
                if n is None:
                    continue
                iso = _ucr_to_iso(m)
                dest[iso] = dest.get(iso, 0) + n
                okdest[iso] = okdest.get(iso, True) and bool(sflags.get(m, True))
    months = {}
    for iso in set(violent) & set(property_):
        # Include only months that are settled across every contributing ORI
        # for both offenses — a not-yet-stable month is held back until the
        # join confirms its value (see _settled_flags).
        if v_ok.get(iso, True) and p_ok.get(iso, True):
            months[iso] = {"violent": violent[iso], "property": property_[iso],
                           "total": violent[iso] + property_[iso]}
    return months, max_data


def latest_full_month(months):
    """Latest iso-month with both series present, or None."""
    return max(months) if months else None


def audit_month_coverage(rows, iso_month):
    """Count audit rows in a calendar month, plus a coverage signal.

    Returns {count, first, last, distinct_days, days_in_month,
    covers_full_month}. ``covers_full_month`` is a heuristic — first row
    on/before the 2nd and last row on/after the penultimate day — meant
    to flag the common partial-month case (e.g. an agency whose public
    audit CSV only started mid-month) rather than to prove gap-free
    coverage. It can be fooled by a large interior gap bracketed by rows
    at both ends, which the rolling-30-day scrape mechanism doesn't
    produce in practice.
    """
    y, m = int(iso_month[:4]), int(iso_month[5:7])
    days_in_month = calendar.monthrange(y, m)[1]
    in_month = []
    for r in rows or []:
        d = r.get("searchDate")
        if not d:
            continue
        d10 = str(d)[:10]
        if len(d10) == 10 and d10[:7] == iso_month:
            in_month.append(d10)
    if not in_month:
        return {"count": 0, "first": None, "last": None, "distinct_days": 0,
                "days_in_month": days_in_month, "covers_full_month": False}
    days = sorted(set(in_month))
    first_dom, last_dom = int(days[0][8:10]), int(days[-1][8:10])
    covers = first_dom <= 2 and last_dom >= days_in_month - 1
    return {"count": len(in_month), "first": days[0], "last": days[-1],
            "distinct_days": len(days), "days_in_month": days_in_month,
            "covers_full_month": covers}


def audit_searches_30d(rows):
    """Trailing 30-day audit-row count, anchored to the latest searchDate.

    Flock's CSV is a rolling 30-day window and PRA-imported rows extend
    further back, so the window slides with whatever data we have rather
    than real time. Returns {count, window_start, window_end} or None.
    Shared by build_report_data.py and build_scoreboard.py so the report
    and the scoreboard derive identical numbers.
    """
    from datetime import date as _date, timedelta
    dates = []
    for r in rows or []:
        d = r.get("searchDate")
        if not d:
            continue
        d = str(d)[:10]
        if len(d) == 10:
            dates.append(d)
    if not dates:
        return None
    try:
        max_d = _date.fromisoformat(max(dates))
    except ValueError:
        return None
    cutoff = (max_d - timedelta(days=29)).isoformat()
    max_str = max_d.isoformat()
    return {
        "count": sum(1 for d in dates if d >= cutoff),
        "window_start": cutoff,
        "window_end": max_str,
    }


def searches_per_crime(oris, crime, audit_rows, portal_searches_30d, audit_30d=None):
    """Resolve the searches-per-reported-crime block for one agency.

    Denominator: Part 1 crime (violent + property) in the latest full
    month of FBI data.

    Numerator, in priority order — chosen to estimate "searches in one
    month" as faithfully as the data allows:
      1. ``audit_month``           — audit rows in that exact calendar
                                      month, when the audit log covers the
                                      whole month. Real, same-period.
      2. ``audit_month_prorated``  — exact-month rows scaled to a full
                                      month by (days_in_month / covered
                                      span) when the audit log only covers
                                      part of the month (e.g. an agency
                                      whose public audit CSV started
                                      mid-month). An estimate; flagged.
                                      Preferred over the raw partial count,
                                      which would *understate* intensity.
      3. ``audit_trailing_30d``    — the most recent 30 days of audit data,
                                      used when the FBI month has no audit
                                      coverage at all but audit data exists
                                      elsewhere. Real, but time-shifted.
      4. ``portal_30d``            — the portal's self-reported rolling
                                      30-day count.

    Raw counts, covered days, and the trailing-30-day cross-check are all
    carried in the block so the headline can never hide its inputs.
    Returns the block dict, or None when there's no ORI / no FBI month.
    """
    oris = oris or []
    if not oris:
        return None
    months, max_data = crime_monthly(oris, crime)
    # Exclude the dataset frontier month (== max_data_date). The most recent
    # month is still being populated and can surface as a partial undercount
    # before all reports land; using it would shrink the denominator and
    # inflate the ratio. Take the latest *settled* month strictly behind the
    # frontier — the join's revision history backfills it once it's whole.
    settled = {m: v for m, v in months.items() if not max_data or m < max_data}
    month = latest_full_month(settled)
    if not month:
        return None
    cm = settled[month]

    cov = audit_month_coverage(audit_rows, month) if audit_rows else None
    # Raw exact-month inputs (transparency — surfaced regardless of source).
    month_count = cov["count"] if cov else None
    month_first = cov["first"] if cov else None
    month_last = cov["last"] if cov else None
    days_in_month = cov["days_in_month"] if cov else None
    covered_span = None
    month_prorated = None
    if cov and cov["count"] > 0:
        covered_span = int(month_last[8:10]) - int(month_first[8:10]) + 1
        if covered_span > 0:
            month_prorated = round(cov["count"] * cov["days_in_month"] / covered_span)

    searches = source = None
    estimated = partial = False
    if cov and cov["count"] > 0 and cov["covers_full_month"]:
        searches, source = cov["count"], "audit_month"
    elif cov and cov["count"] > 0:
        searches, source = month_prorated, "audit_month_prorated"
        estimated = partial = True
    elif audit_30d and audit_30d.get("count"):
        searches, source = audit_30d["count"], "audit_trailing_30d"
        partial = True  # rolling 30-day window, not aligned to the FBI month
    elif portal_searches_30d is not None:
        searches, source = portal_searches_30d, "portal_30d"
        partial = True

    ratio = None
    if searches is not None and cm["total"]:
        ratio = round(searches / cm["total"], 1)

    return {
        "month": month,
        "month_label": month_label(month),
        "crime_total": cm["total"],
        "crime_violent": cm["violent"],
        "crime_property": cm["property"],
        "crime_max_data_date": max_data,
        "searches": searches,
        "searches_source": source,
        "estimated": estimated,
        "partial": partial,
        "ratio": ratio,
        # transparency / cross-checks
        "month_count": month_count,
        "month_first": month_first,
        "month_last": month_last,
        "month_covered_days": covered_span,
        "days_in_month": days_in_month,
        "month_prorated": month_prorated,
        "trailing_30d": audit_30d,
        "oris": list(oris),
    }
