"""Read FBI crime data and derive the searches-per-reported-crime metric.

Data source: data/fbi/crime.json, written by refresh_fbi_crime.py — flat,
keyed by INDIVIDUAL ORI. An agency's registry ``ori`` field is a LIST
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

CRIME_PATH = Path("data/fbi/crime.json")

_MONTHS = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def load_crime(path=CRIME_PATH):
    """Load data/fbi/crime.json, or {} if it hasn't been fetched yet."""
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {}


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
        for key, dest in (("violent-crime", violent), ("property-crime", property_)):
            for m, n in (offenses.get(key) or {}).items():
                if n is None:
                    continue
                iso = _ucr_to_iso(m)
                dest[iso] = dest.get(iso, 0) + n
    months = {}
    for iso in set(violent) & set(property_):
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
    month = latest_full_month(months)
    if not month:
        return None
    cm = months[month]

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
