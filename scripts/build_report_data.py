#!/usr/bin/env python3
"""
Generate per-agency report data for the printable city report.

For each CA agency, precomputes:
  - Core stats (cameras, retention, 30-day activity)
  - Transparency/best-practices checklist (what they publish vs. peers)
  - Sharing summary (outbound/inbound counts, flagged recipients)
  - Regional context (neighbors within 50 km)
  - Peer-group percentiles (by agency_type within CA)

Reads:
  - assets/transparency.flocksafety.com/.sharing_graph_full.json
  - assets/agency_registry.json
  - assets/transparency.flocksafety.com/<slug>/*.json (latest per agency)

Writes:
  - docs/data/report_data.json

Usage:
  uv run python scripts/build_report_data.py
"""

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import (
    agency_active_slug,
    agency_coords,
    agency_display_name,
    agency_state,
    crawl_status,
    has_tag,
    load_registry,
    registry_by_id,
)
from gazetteer import (
    county_fips_for_place,
    lookup_area_sqmi,
    lookup_place,
    lookup_population,
    lookup_vehicles,
    population_meta,
)

GRAPH_PATH = Path("assets/transparency.flocksafety.com/.sharing_graph_full.json")
DATA_DIR = Path("assets/transparency.flocksafety.com")
OUT_PATH = Path("docs/data/report_data.json")

RANKED_STATE = "CA"
# 25 miles, expressed in km (the native unit of dist_km). Matches the
# local-rank radius so "regional context" and "local rank" use the same
# geographic scope. 25 mi is tight enough to feel local (peninsula vs.
# all of the Bay Area) while still giving ~30 peers in most CA metros.
REGIONAL_RADIUS_KM = 25 * 1.60934
MIN_PEER_SAMPLE = 10  # fall back to all-CA if peer group is smaller


# ── Checklist definitions ───────────────────────────────────────────────────
#
# Two sections:
#   1. SB 34 — explicit compliance signals tied to CA Civil Code §1798.90.51–.55
#   2. Transparency — what the agency publishes on its Flock portal
#
# Each item checks whether an agency satisfies a given signal. The result is
# paired with "X of N CA peers also do this" so the reader can gauge how
# common each practice is. Uncrawled agencies get X for every item (no
# transparency page → no signals available).

_URL_RE = __import__("re").compile(r"https?://", __import__("re").IGNORECASE)


def _has_value(portal, key):
    """True if the portal field is a non-empty string."""
    v = portal.get(key)
    return isinstance(v, str) and v.strip() != ""


def _has_number(portal, key):
    """True if the portal field is a non-null number."""
    v = portal.get(key)
    return isinstance(v, (int, float)) and v is not None


def _field_has_url(portal, key):
    """True if the portal field contains an http(s) URL."""
    v = portal.get(key)
    if not isinstance(v, str):
        return False
    return bool(_URL_RE.search(v))


def _any_field_has_url(portal, keys):
    return any(_field_has_url(portal, k) for k in keys)


def _no_flagged_outbound(portal, reg, crawled, *, outbound_ids, reg_by_id):
    if not crawled or not outbound_ids:
        # Uncrawled agencies don't get credit — we can't verify
        return False if not crawled else True
    return not any(is_flagged_entity(t, reg_by_id) for t in outbound_ids)


# SB 34 compliance checks. Each item carries three labels so the UI can
# show the correct reading for the agency's actual state:
#   label_pass: what a passing agency has done (green check)
#   label_fail: what a failing agency did instead (red X) — concrete
#   label_unknown: why we can't tell (orange ?)
# This avoids the "No private entities in sharing list" ✗ contradiction
# where a static positive label reads as an assertion the ✗ negates.
#
# Note: "Publishes downloadable audit records" is not currently detectable —
# the Flock portal renders "Download CSV" as a button with a URL, not a
# text block, and our parser captures it as an empty string. Re-add this
# check once flock_transparency.py extracts the CSV download URL.
SB34_CHECKLIST = [
    {
        "id": "no_private_sharing",
        "label_pass": "Shares only with public agencies",
        "label_fail": "Shares with at least one private entity",
        "label_unknown": "Private sharing not verifiable (no public transparency page)",
        "detail": "CA Civil Code \u00a71798.90.55(b) restricts ALPR sharing to public agencies.",
    },
    {
        "id": "no_out_of_state_sharing",
        "label_pass": "Does not share with out-of-state entities",
        "label_fail": "Shares with at least one out-of-state entity",
        "label_unknown": "Out-of-state sharing not verifiable (no public transparency page)",
        "detail": "CA Civil Code \u00a71798.90.55(b) and AG Bulletin 2023-DLE-06 prohibit out-of-state sharing.",
    },
    {
        "id": "no_federal_sharing",
        "label_pass": "Does not share with federal agencies",
        "label_fail": "Shares with at least one federal agency",
        "label_unknown": "Federal sharing not verifiable (no public transparency page)",
        "detail": "Federal agencies are not \u201cagencies of the state\u201d under \u00a71798.90.5(f).",
    },
    {
        "id": "no_fusion_center_sharing",
        "label_pass": "Does not share with fusion centers",
        "label_fail": "Shares with at least one fusion center",
        "label_unknown": "Fusion-center sharing not verifiable (no public transparency page)",
        "detail": "Fusion centers (e.g., NCRIC) re-distribute ALPR data to their member networks. Their status as \u201cpublic agencies\u201d under \u00a71798.90.5(f) varies by charter; NCRIC in particular has federal staffing and funding ties. Sharing with a fusion center forwards this agency's data to every member of that center.",
    },
    {
        "id": "no_ag_lawsuit_sharing",
        "label_pass": "Does not share with agencies the CA AG has sued",
        "label_fail": "Shares with an agency the CA AG has sued for illegal sharing",
        "label_unknown": "Cannot verify (no public transparency page)",
        "detail": "The CA Attorney General has sued specific CA agencies for illegal out-of-state ALPR sharing in violation of SB 34. Sending data to those agencies continues the same illegal flow.",
    },
    {
        "id": "published_policy",
        "label_pass": "Publishes an ALPR usage and privacy policy",
        "label_fail": "Does not publish an ALPR usage and privacy policy",
        "label_unknown": "Policy publication not verifiable (no public transparency page)",
        "detail": "\u00a71798.90.51(a) requires conspicuously posting a usage and privacy policy.",
    },
    {
        "id": "documented_audit",
        "label_pass": "Documents an audit process",
        "label_fail": "No documented audit process",
        "label_unknown": "Audit process not verifiable (no public transparency page)",
        "detail": "\u00a71798.90.51(b)(5) requires audit provisions for ALPR access.",
    },
]

# Transparency checks — what they publish on the Flock portal.
# Same pass/fail/unknown label structure as SB34_CHECKLIST above.
TRANSPARENCY_CHECKLIST = [
    {"id": "has_portal",       "label_pass": "Has a public Flock transparency page",   "label_fail": "No public Flock transparency page"},
    {"id": "camera_count",     "label_pass": "Reports camera count",                    "label_fail": "Does not report camera count"},
    {"id": "retention",        "label_pass": "Reports data retention days",              "label_fail": "Does not report data retention"},
    {"id": "vehicles_30d",     "label_pass": "Reports 30-day vehicle detections",        "label_fail": "Does not report 30-day vehicle detections"},
    {"id": "hotlist_hits",     "label_pass": "Reports 30-day hotlist hits",              "label_fail": "Does not report 30-day hotlist hits"},
    {"id": "searches_30d",     "label_pass": "Reports 30-day search counts",             "label_fail": "Does not report 30-day search counts"},
    {"id": "policy_link",      "label_pass": "Links to a full ALPR/department policy",    "label_fail": "No link to a full policy document"},
    {"id": "access_policy",    "label_pass": "Publishes access policy",                  "label_fail": "Does not publish access policy"},
    {"id": "hotlist_policy",   "label_pass": "Publishes hotlist policy",                  "label_fail": "Does not publish hotlist policy"},
    {"id": "acceptable_use",   "label_pass": "Publishes acceptable-use policy",          "label_fail": "Does not publish acceptable-use policy"},
    {"id": "prohibited_uses",  "label_pass": "Publishes prohibited-uses statement",       "label_fail": "Does not publish prohibited-uses statement"},
    {"id": "sb54_statement",   "label_pass": "Publishes SB 54 compliance statement",     "label_fail": "Does not publish SB 54 compliance statement"},
    {"id": "svs_statement",    "label_pass": "Publishes CA SVS statement",              "label_fail": "Does not publish CA SVS statement"},
    {"id": "outbound_list",    "label_pass": "Publishes full outbound sharing list",     "label_fail": "Does not publish outbound sharing list"},
    {"id": "inbound_list",     "label_pass": "Publishes full inbound sharing list",      "label_fail": "Does not publish inbound sharing list"},
]


def evaluate_checklist(portal, reg, crawled, outbound_ids, reg_by_id):
    """Return dict of check_id -> bool (or None for unknown).

    Uncrawled agencies get False for every check except where noted.
    """
    results = {}

    # ── SB 34 items ──
    # Sharing-based checks: require crawled (unless we can infer outbound)
    if crawled and outbound_ids:
        results["no_private_sharing"] = not any(
            is_flagged_entity(t, reg_by_id) == "private" for t in outbound_ids
        )
        results["no_out_of_state_sharing"] = not any(
            is_flagged_entity(t, reg_by_id) == "out_of_state" for t in outbound_ids
        )
        results["no_federal_sharing"] = not any(
            is_flagged_entity(t, reg_by_id) == "federal" for t in outbound_ids
        )
        # Fusion centers are their own kind — distinct from federal,
        # but still frequently tied to federal staffing and funding.
        # A separate check surfaces this so readers see the signal
        # even when the federal-sharing check passes.
        results["no_fusion_center_sharing"] = not any(
            is_flagged_entity(t, reg_by_id) == "fusion_center" for t in outbound_ids
        )
        # AG-lawsuit check: any recipient tagged ag-lawsuit. These are
        # specific CA agencies the AG sued over illegal out-of-state
        # sharing — distinct from the flag categories above.
        results["no_ag_lawsuit_sharing"] = not any(
            has_tag(reg_by_id.get(t, {}), "ag-lawsuit") for t in outbound_ids
        )
    elif crawled:
        # Crawled, no outbound list published — we don't know
        results["no_private_sharing"] = None
        results["no_out_of_state_sharing"] = None
        results["no_federal_sharing"] = None
        results["no_fusion_center_sharing"] = None
        results["no_ag_lawsuit_sharing"] = None
    else:
        # Uncrawled — no way to verify
        results["no_private_sharing"] = None
        results["no_out_of_state_sharing"] = None
        results["no_federal_sharing"] = None
        results["no_fusion_center_sharing"] = None
        results["no_ag_lawsuit_sharing"] = None

    # Policy published — requires a portal to verify (uncrawled => None)
    if crawled:
        results["published_policy"] = bool(
            _has_value(portal, "alpr_policy")
            or _any_field_has_url(portal, ["policy_info", "alpr_policy", "access_policy"])
        )
    else:
        results["published_policy"] = None

    # Audit process documented — broader than just the search_audit field.
    # Counts as "yes" if:
    #   - the dedicated search_audit field has content, OR
    #   - policy_info links to an external policy document, OR
    #   - any free-form text field mentions audit-related terms
    #     (audit, review, oversight, accountability)
    _AUDIT_TERMS = __import__("re").compile(
        r"\baudit|\breview\b|oversight|accountability",
        __import__("re").IGNORECASE,
    )
    if crawled:
        audit_documented = False
        if _has_value(portal, "search_audit"):
            audit_documented = True
        elif _field_has_url(portal, "policy_info"):
            audit_documented = True
        else:
            for field in ("access_policy", "alpr_policy", "acceptable_use_policy",
                          "policy_info", "overview", "additional_info",
                          "hotlist_policy", "restrictions_on_deployment"):
                v = portal.get(field)
                if isinstance(v, str) and _AUDIT_TERMS.search(v):
                    audit_documented = True
                    break
        results["documented_audit"] = audit_documented
    else:
        results["documented_audit"] = None

    # ── Transparency items ──
    # has_portal is always answerable (True/False, never None) — absence
    # of a portal is itself the answer. Every other transparency check
    # returns None for uncrawled agencies (you can't verify what they
    # publish without their portal existing).
    results["has_portal"] = crawled
    if crawled:
        results["camera_count"] = _has_number(portal, "camera_count")
        results["retention"] = _has_number(portal, "data_retention_days")
        results["vehicles_30d"] = _has_number(portal, "vehicles_detected_30d")
        results["hotlist_hits"] = _has_number(portal, "hotlist_hits_30d")
        results["searches_30d"] = _has_number(portal, "searches_30d")
        results["policy_link"] = _any_field_has_url(
            portal, ["policy_info", "alpr_policy", "access_policy"]
        )
        results["access_policy"] = _has_value(portal, "access_policy")
        results["hotlist_policy"] = _has_value(portal, "hotlist_policy")
        results["acceptable_use"] = _has_value(portal, "acceptable_use_policy")
        results["prohibited_uses"] = _has_value(portal, "prohibited_uses")
        results["sb54_statement"] = _has_value(portal, "sb54")
        results["svs_statement"] = _has_value(portal, "california_svs")
        results["outbound_list"] = bool(
            isinstance(portal.get("sharing_outbound"), list)
            and len(portal.get("sharing_outbound", [])) > 0
        )
        results["inbound_list"] = bool(
            isinstance(portal.get("sharing_inbound"), list)
            and len(portal.get("sharing_inbound", [])) > 0
        )
    else:
        for cid in ("camera_count", "retention", "vehicles_30d", "hotlist_hits",
                    "searches_30d", "policy_link", "access_policy", "hotlist_policy",
                    "acceptable_use", "prohibited_uses", "sb54_statement",
                    "svs_statement", "outbound_list", "inbound_list"):
            results[cid] = None

    return results


# Combined list for computing peer counts (dedup'd)
ALL_CHECKS = SB34_CHECKLIST + TRANSPARENCY_CHECKLIST


# ── Flag classification (mirrors build_scoreboard.py) ──────────────────────

def is_flagged_entity(aid, reg_by_id):
    r = reg_by_id.get(aid, {})
    if has_tag(r, "private"):
        return "private"
    r_state = agency_state(r)
    if r_state and r_state != "CA":
        return "out_of_state"
    t = r.get("agency_type")
    if t in ("federal", "decommissioned", "test", "fusion_center"):
        return t
    return None


# ── Portal data loader ──

def _load_portal_json(reg_entry):
    """Return the latest crawled portal JSON dict, or {} if none found."""
    slugs = reg_entry.get("flock_slugs") or []
    base_slug = reg_entry.get("slug")
    if base_slug and base_slug not in slugs:
        slugs = list(slugs) + [base_slug]
    latest_path = None
    latest_date = ""
    for slug in slugs:
        slug_dir = DATA_DIR / slug
        if not slug_dir.is_dir():
            continue
        for p in slug_dir.glob("*.json"):
            if p.stem > latest_date:
                latest_date = p.stem
                latest_path = p
    if latest_path is None:
        return {}
    try:
        return json.loads(latest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# ── Geography ──

def dist_km(lat1, lng1, lat2, lng2):
    R = 6371
    dLat = math.radians(lat2 - lat1)
    dLng = math.radians(lng2 - lng1)
    a = (math.sin(dLat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dLng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Percentile helper ──

import re as _re_mod
_AGENCY_SUFFIXES_RE = _re_mod.compile(
    r"\s*\b(PD|SO|SD|DA|FD|DPS|Police Department|Sheriff'?s? Office|"
    r"Sheriff'?s? Department|District Attorney|Fire Department|"
    r"Department of Public Safety|Public Safety|Parks Department|"
    r"Marshal'?s? Office)\b\s*",
    _re_mod.IGNORECASE,
)
_STATE_TOKEN_RE = _re_mod.compile(r"\s*\b[A-Z]{2}\b\s*")
_PARENS_RE = _re_mod.compile(r"\s*\([^)]*\)\s*")
_CITY_OF_RE = _re_mod.compile(r"^(City|Town|Village|Borough)\s+of\s+", _re_mod.IGNORECASE)


def _place_name_from_agency(agency_name):
    """Heuristic extraction of a place name from an agency display name.

    Mirrors scripts/geocode_agencies.py but standalone so this module's
    fallback population lookup doesn't depend on the geocoder's private
    regexes. Returns an empty string if nothing plausible remains.
    """
    n = agency_name or ""
    n = _PARENS_RE.sub(" ", n)
    n = _AGENCY_SUFFIXES_RE.sub(" ", n)
    n = _STATE_TOKEN_RE.sub(" ", n)
    n = _CITY_OF_RE.sub("", n)
    n = _re_mod.sub(r"\s+", " ", n).strip()
    return n


def histogram(sorted_values, bins=20):
    """Bin a sorted numeric series for sparkline rendering.

    Returns {bins: [count, ...], min, max}. When min == max (all values
    equal), the single bin holds the whole series. Empty series → None.
    Fixed bin count so the rendered sparkline has a predictable width.
    """
    if not sorted_values:
        return None
    mn = sorted_values[0]
    mx = sorted_values[-1]
    if mn == mx:
        return {"bins": [len(sorted_values)], "min": mn, "max": mx}
    counts = [0] * bins
    width = (mx - mn) / bins
    for v in sorted_values:
        # Put values at the max edge into the last bin, not bin+1.
        idx = int((v - mn) / width)
        if idx >= bins:
            idx = bins - 1
        counts[idx] += 1
    return {"bins": counts, "min": mn, "max": mx}


def _per_1000(metrics, population):
    """Convert raw metrics to per-1,000-residents rates.

    Returns dict of same keys mapped to float or None.
    """
    if not population or population <= 0:
        return {k: None for k in metrics}
    scale = 1000.0 / population
    result = {}
    for k, v in metrics.items():
        if v is None:
            result[k] = None
        else:
            result[k] = round(v * scale, 2)
    return result


def percentile_of(value, sorted_values):
    """Return the percentile (0-100) for ``value`` within ``sorted_values``.

    Uses rank-based percentile: pct of values strictly less than ``value``.
    Returns None for value=None or empty sample.
    """
    if value is None or not sorted_values:
        return None
    below = sum(1 for v in sorted_values if v < value)
    return round(100 * below / len(sorted_values))


# ── Main build ──

def main():
    registry = load_registry()
    reg_by_id = registry_by_id()
    id_to_slug = {e["agency_id"]: e["slug"] for e in registry}
    slug_to_id = {e["slug"]: e["agency_id"] for e in registry}
    # Also handle legacy flock_slugs → primary slug
    for e in registry:
        for fs in e.get("flock_slugs", []):
            slug_to_id.setdefault(fs, e["agency_id"])

    graph = json.loads(GRAPH_PATH.read_text())
    graph_agencies = graph["agencies"]

    # ── Pass 1: load portal data and core stats for every CA crawled agency ──
    #
    # We build a full peer dataset first so percentiles can be computed
    # within each agency_type.

    ca_crawled = []  # list of dicts with agency_id, type, portal, stats
    for aid, gdata in graph_agencies.items():
        reg = reg_by_id.get(aid, {})
        if agency_state(reg) != RANKED_STATE:
            continue
        if not gdata.get("crawled"):
            continue
        portal = _load_portal_json(reg)
        atype = reg.get("agency_type") or "other"
        ca_crawled.append({
            "agency_id": aid,
            "slug": id_to_slug.get(aid, aid),
            "type": atype,
            "portal": portal,
            "graph": gdata,
            "reg": reg,
        })

    print(f"CA crawled agencies: {len(ca_crawled)}")

    # ── Checklist completion counts (by agency_type and overall) ──
    #
    # We compute peer totals against ALL CA agencies with Flock cameras,
    # crawled or not. This makes "has transparency page" a meaningful
    # comparison (denominator includes cities without portals).

    # All CA agencies in registry that appear in the graph (have any
    # outbound/inbound activity or crawl data)
    ca_all = []
    for aid, gdata in graph_agencies.items():
        reg = reg_by_id.get(aid, {})
        if agency_state(reg) != RANKED_STATE:
            continue
        portal = _load_portal_json(reg) if gdata.get("crawled") else {}
        atype = reg.get("agency_type") or "other"
        ca_all.append({
            "agency_id": aid,
            "slug": id_to_slug.get(aid, aid),
            "type": atype,
            "portal": portal,
            "graph": gdata,
            "reg": reg,
            "crawled": bool(gdata.get("crawled")),
            "outbound_ids": gdata.get("sharing_outbound_ids", []),
        })

    check_ids = [item["id"] for item in ALL_CHECKS]
    # For each check, track how many agencies PASS (value == True) and
    # how many agencies the check is ANSWERABLE for (value != None).
    # The denominator for peer stats is the answerable count, so
    # uncrawled agencies don't artificially inflate the "failing" pool
    # for checks we literally can't evaluate without their portal.
    #
    # Exception: "has_portal" is always answerable — lack of a portal IS
    # the failing answer.
    checklist_pass_all = {cid: 0 for cid in check_ids}
    checklist_applicable_all = {cid: 0 for cid in check_ids}
    checklist_pass_by_type = defaultdict(lambda: defaultdict(int))
    checklist_applicable_by_type = defaultdict(lambda: defaultdict(int))
    type_totals = defaultdict(int)  # agency_type -> total CA-registry count
    # Cache per-agency results so we can roll them up for local peer
    # comparisons on each report (e.g. "X of 13 agencies in the same
    # county pass this check").
    checklist_results_by_aid = {}
    for a in ca_all:
        type_totals[a["type"]] += 1
        results = evaluate_checklist(
            a["portal"], a["reg"], a["crawled"], a["outbound_ids"], reg_by_id
        )
        checklist_results_by_aid[a["agency_id"]] = results
        for cid, v in results.items():
            if v is True:
                checklist_pass_all[cid] += 1
                checklist_pass_by_type[a["type"]][cid] += 1
            if v is not None:
                checklist_applicable_all[cid] += 1
                checklist_applicable_by_type[a["type"]][cid] += 1

    # ── Sorted numeric series for percentile computation ──
    #
    # Built per agency_type (and "ALL" as fallback). Each key is a metric name.
    #
    # We compute both raw metrics and per-1,000-residents rates. Per-capita
    # rates are typically the more meaningful comparison — a small city
    # will show high raw-per-capita detections purely because its denominator
    # is small, but the per-capita percentile tells you whether it's
    # unusually high *among peers of similar size*.

    def _pop(a):
        fips = (a["reg"].get("geo") or {}).get("fips")
        pop = lookup_population(fips) if fips else None
        # Same fallback as the per-agency loop: for manual-geocoded
        # entries without FIPS, try to resolve by agency name + state.
        if pop is None:
            st = agency_state(a["reg"])
            if st:
                candidate = _place_name_from_agency(
                    agency_display_name(a["reg"], a["slug"])
                )
                if candidate:
                    place = lookup_place(candidate, st)
                    if place:
                        pop = lookup_population(place["fips"])
        return pop

    def _area(a):
        """Land area in sqmi, with name-based fallback like _pop."""
        fips = (a["reg"].get("geo") or {}).get("fips")
        area = lookup_area_sqmi(fips) if fips else None
        if area is None:
            st = agency_state(a["reg"])
            if st:
                candidate = _place_name_from_agency(
                    agency_display_name(a["reg"], a["slug"])
                )
                if candidate:
                    place = lookup_place(candidate, st)
                    if place:
                        area = lookup_area_sqmi(place["fips"])
        return area

    def _rate(value, pop):
        if value is None or not pop:
            return None
        return 1000.0 * value / pop

    METRICS = [
        ("cameras", lambda a: a["graph"].get("camera_count")),
        ("vehicles_30d", lambda a: a["portal"].get("vehicles_detected_30d")),
        ("hotlist_hits_30d", lambda a: a["portal"].get("hotlist_hits_30d")),
        ("searches_30d", lambda a: a["portal"].get("searches_30d")),
        ("outbound", lambda a: a["graph"].get("sharing_outbound_count") or 0),
    ]

    # {metric: {type: sorted_list}}
    series_by_type = defaultdict(lambda: defaultdict(list))
    series_all = defaultdict(list)
    # Per-capita series — only computed for agencies with population data.
    rate_series_by_type = defaultdict(lambda: defaultdict(list))
    rate_series_all = defaultdict(list)
    # Local ranking requires per-agency metric snapshots so we can
    # filter to "peers in the same county / within 50 miles" at
    # report-build time. Cache each crawled agency's raw + per-capita +
    # density (per-sqmi) metric values along with geography.
    LOCAL_RADIUS_MI = 25
    LOCAL_RADIUS_KM = LOCAL_RADIUS_MI * 1.60934
    # Separate series for camera-density (cameras/sqmi). Only meaningful
    # for cameras; vehicles-per-sqmi and hits-per-sqmi are dominated by
    # traffic volume, not camera layout, so we don't compute them.
    density_series_by_type = defaultdict(list)  # type -> sorted list of cameras/sqmi
    density_series_all = []
    # Downstream-search series: each agency's total = self.searches +
    # sum of crawled recipients' searches. Requires two passes — we
    # build a searches-by-aid lookup first, then compute downstream
    # totals from it. Peer percentiles come from this series.
    downstream_series_by_type = defaultdict(list)
    downstream_series_all = []
    searches_by_aid = {}
    for a in ca_crawled:
        s = a["portal"].get("searches_30d")
        if isinstance(s, (int, float)):
            searches_by_aid[a["agency_id"]] = s
    crawled_agency_metrics = []  # list of dicts: {agency_id, lat, lng, county_fips, values, rates, density, downstream}
    for a in ca_crawled:
        pop = _pop(a)
        area = _area(a)
        values = {}
        rates = {}
        density = None
        for metric, getter in METRICS:
            v = getter(a)
            if v is not None:
                series_by_type[metric][a["type"]].append(v)
                series_all[metric].append(v)
                values[metric] = v
            rate = _rate(v, pop)
            if rate is not None:
                rate_series_by_type[metric][a["type"]].append(rate)
                rate_series_all[metric].append(rate)
                rates[metric] = rate
        # Camera density
        cams = a["graph"].get("camera_count")
        if cams is not None and area and area > 0:
            density = round(cams / area, 2)
            density_series_by_type[a["type"]].append(density)
            density_series_all.append(density)
        # Downstream searches total (self + crawled recipients that
        # publish search counts). Always defined for crawled agencies,
        # even if the agency itself doesn't publish searches.
        ds_total = 0
        self_s = a["portal"].get("searches_30d")
        if isinstance(self_s, (int, float)):
            ds_total += int(self_s)
        for t_id in a["graph"].get("sharing_outbound_ids", []):
            if t_id in searches_by_aid:
                ds_total += int(searches_by_aid[t_id])
        downstream_total = ds_total
        downstream_series_by_type[a["type"]].append(downstream_total)
        downstream_series_all.append(downstream_total)
        a_lat, a_lng = agency_coords(a["reg"])
        a_geo = a["reg"].get("geo") or {}
        a_fips = a_geo.get("fips") or ""
        a_kind = a_geo.get("kind")
        # County FIPS extraction depends on the geo kind:
        #   - county / cousub: first 5 chars ARE the county FIPS
        #   - place: first 5 chars are state(2) + place(3), not county;
        #     look up the county via nearest-centroid in the same state
        #   - state-only / manual / absent: no county data
        if a_kind in ("county", "cousub") and len(a_fips) >= 5:
            a_county_fips = a_fips[:5]
        elif a_kind == "place" and len(a_fips) == 7:
            a_county_fips = county_fips_for_place(a_fips) or ""
        else:
            a_county_fips = ""
        crawled_agency_metrics.append({
            "agency_id": a["agency_id"],
            "slug": a["slug"],
            "lat": a_lat,
            "lng": a_lng,
            "county_fips": a_county_fips,
            "values": values,
            "rates": rates,
            # Only one density metric (cameras/sqmi), stored under the
            # "cameras" key to mirror the other metric-keyed dicts.
            "density": {"cameras": density} if density is not None else {},
            "downstream_searches": downstream_total,
        })
    for metric in list(series_all.keys()):
        series_all[metric].sort()
        for t in series_by_type[metric]:
            series_by_type[metric][t].sort()
    for metric in list(rate_series_all.keys()):
        rate_series_all[metric].sort()
        for t in rate_series_by_type[metric]:
            rate_series_by_type[metric][t].sort()
    density_series_all.sort()
    for t in density_series_by_type:
        density_series_by_type[t].sort()
    downstream_series_all.sort()
    for t in downstream_series_by_type:
        downstream_series_by_type[t].sort()

    def median(values):
        if not values:
            return None
        n = len(values)
        if n % 2:
            return values[n // 2]
        return (values[n // 2 - 1] + values[n // 2]) / 2

    def peer_series(metric, atype):
        """Return (series, is_fallback, peer_type)."""
        s = series_by_type[metric].get(atype, [])
        if len(s) >= MIN_PEER_SAMPLE:
            return s, False, atype
        return series_all[metric], True, "all"

    def peer_rate_series(metric, atype):
        """Return (series, is_fallback, peer_type) for per-1,000 rates."""
        s = rate_series_by_type[metric].get(atype, [])
        if len(s) >= MIN_PEER_SAMPLE:
            return s, False, atype
        return rate_series_all[metric], True, "all"

    def peer_density_series(atype):
        """Return (series, is_fallback, peer_type) for cameras-per-sqmi."""
        s = density_series_by_type.get(atype, [])
        if len(s) >= MIN_PEER_SAMPLE:
            return s, False, atype
        return density_series_all, True, "all"

    def peer_downstream_series(atype):
        """Return (series, is_fallback, peer_type) for downstream-search totals."""
        s = downstream_series_by_type.get(atype, [])
        if len(s) >= MIN_PEER_SAMPLE:
            return s, False, atype
        return downstream_series_all, True, "all"

    def local_peers_for(source_aid, source_lat, source_lng, source_county_fips):
        """Return (peers, scope) for an agency's local comparison pool.

        Primary: crawled agencies sharing the same 5-digit county FIPS.
        Fallback: crawled agencies within ~50 miles.
        Agencies without coordinates can't fall back to distance.
        Excludes the source agency itself.

        Scope is one of "county", "nearby" (50mi), or None if the pool
        is too small to be meaningful (< MIN_PEER_SAMPLE). In that case
        the local ranking is omitted for this agency.
        """
        # Try same county first
        if source_county_fips:
            county_peers = [
                p for p in crawled_agency_metrics
                if p["agency_id"] != source_aid and p["county_fips"] == source_county_fips
            ]
            if len(county_peers) >= MIN_PEER_SAMPLE:
                return county_peers, "county"

        # Fallback to distance
        if source_lat is not None and source_lng is not None:
            nearby = [
                p for p in crawled_agency_metrics
                if p["agency_id"] != source_aid
                and p["lat"] is not None and p["lng"] is not None
                and dist_km(source_lat, source_lng, p["lat"], p["lng"]) <= LOCAL_RADIUS_KM
            ]
            if len(nearby) >= MIN_PEER_SAMPLE:
                return nearby, "nearby"

        # Not enough local peers; report will omit local rank
        return [], None

    def local_series(peers, metric, per_capita):
        """Build a sorted value series for the given metric from a peer list."""
        key = "rates" if per_capita else "values"
        s = sorted(p[key].get(metric) for p in peers if p[key].get(metric) is not None)
        return s

    # ── Build graph-wide flag classification cache ──
    outbound_by_id = {aid: d.get("sharing_outbound_ids", []) for aid, d in graph_agencies.items()}

    # ── Pass 2: build a report entry for EVERY agency in the registry ──
    #
    # (Including uncrawled — they get a skeletal report using inbound
    # inferred from other agencies' outbound.)

    # Pre-compute "who shares with agency X" across the graph
    # (source_id -> list of target_ids). Invert for reverse lookup.
    inbound_inferred_by_id = defaultdict(set)
    for source_id, out_ids in outbound_by_id.items():
        for target_id in out_ids:
            if target_id != source_id:
                inbound_inferred_by_id[target_id].add(source_id)

    # Inverse: "agency X shares with Y" inferred from Y publishing an
    # inbound list that names X. Only ~10% of CA portals populate the
    # inbound section, so this yields far fewer edges than inferred
    # inbound — but when an uncrawled agency is named in someone's
    # inbound list we can actually tell one of its outbound partners.
    outbound_inferred_by_id = defaultdict(set)
    for target_id, gdata in graph_agencies.items():
        if not gdata.get("crawled"):
            continue
        for source_id in gdata.get("sharing_inbound_ids", []):
            if source_id != target_id:
                outbound_inferred_by_id[source_id].add(target_id)

    reports = {}
    for e in registry:
        aid = e["agency_id"]
        slug = e["slug"]
        reg = e
        gdata = graph_agencies.get(aid, {})
        crawled = bool(gdata.get("crawled"))
        portal = _load_portal_json(reg) if crawled else {}
        atype = reg.get("agency_type") or "other"

        lat, lng = agency_coords(reg)
        state = agency_state(reg)
        geo_fips = (reg.get("geo") or {}).get("fips")
        population = lookup_population(geo_fips) if geo_fips else None
        # ACS B25046 — vehicles available to households in this geo.
        # Used as a denominator for per-vehicle rates (plates
        # detected per household vehicle, etc.).
        household_vehicles = lookup_vehicles(geo_fips) if geo_fips else None
        # Land area in square miles (for per-sqmi camera-density metric).
        # Only meaningful for place/county-FIPS agencies; manual and
        # state-only entries get None.
        land_sqmi = lookup_area_sqmi(geo_fips) if geo_fips else None
        # Fallback: if the registry has no FIPS (e.g. kind=manual hand-
        # curated before geocoding), try looking up the place by name so
        # we can still report population / vehicles. This is a read-time
        # fallback; the proper fix is to upgrade the registry entry to
        # kind=place with a FIPS code via geocode_agencies.py.
        if (population is None or household_vehicles is None) and state:
            candidate = _place_name_from_agency(agency_display_name(reg, slug))
            if candidate:
                place = lookup_place(candidate, state)
                if place:
                    if population is None:
                        p = lookup_population(place["fips"])
                        if p:
                            population = p
                    if household_vehicles is None:
                        v = lookup_vehicles(place["fips"])
                        if v:
                            household_vehicles = v

        # ── Core stats ──
        cameras = gdata.get("camera_count")
        retention = gdata.get("data_retention_days")
        vehicles_30d = portal.get("vehicles_detected_30d")
        hotlist_hits = portal.get("hotlist_hits_30d")
        searches_30d = portal.get("searches_30d")
        outbound_count = gdata.get("sharing_outbound_count", 0)
        inbound_count = gdata.get("sharing_inbound_count", 0)
        outbound_ids = gdata.get("sharing_outbound_ids", [])
        inbound_ids = gdata.get("sharing_inbound_ids", [])

        # ── Checklist ──
        # Only meaningful for CA agencies (peer pool is CA). For non-CA,
        # we emit an empty checklist; the report UI can skip the section.
        checklist_results = {}
        checklist_sb34 = []
        checklist_transparency = []
        # Build failure-detail strings for the sharing-based SB 34 checks.
        # When an agency fails "no private sharing", we want the UI to
        # name which private entities triggered the failure (up to a small
        # limit — long lists are referenced to the Flagged Recipients
        # section instead). Computed once here because we already have
        # the flagged_recipients list ready.
        failure_details = {}
        # Caveat entities: cases where a check technically passes but a
        # related concern applies. Today this is only used for the
        # federal-sharing check: an agency that doesn't share with any
        # entity tagged "federal" still may be sharing with a fusion
        # center (e.g., NCRIC) that has federal funding, staffing, or
        # governance ties. The check stays green but the report surfaces
        # the caveat.
        caveat_details = {}
        if state == RANKED_STATE:
            checklist_results = evaluate_checklist(
                portal, reg, crawled, outbound_ids, reg_by_id
            )
            # Collect names by flag kind for failure detail
            names_by_kind = defaultdict(list)
            for target_id in outbound_ids:
                k = is_flagged_entity(target_id, reg_by_id)
                if k:
                    tr = reg_by_id.get(target_id, {})
                    names_by_kind[k].append(
                        agency_display_name(tr, id_to_slug.get(target_id, target_id))
                    )
            # Names of AG-lawsuit recipients — collected separately from
            # flag-kind since ag-lawsuit is a tag, not a kind.
            ag_lawsuit_names = []
            for target_id in outbound_ids:
                tr = reg_by_id.get(target_id, {})
                if has_tag(tr, "ag-lawsuit"):
                    ag_lawsuit_names.append(
                        agency_display_name(tr, id_to_slug.get(target_id, target_id))
                    )
            failure_details = {
                "no_private_sharing": names_by_kind["private"],
                "no_out_of_state_sharing": names_by_kind["out_of_state"],
                "no_federal_sharing": names_by_kind["federal"],
                "no_fusion_center_sharing": names_by_kind["fusion_center"],
                "no_ag_lawsuit_sharing": ag_lawsuit_names,
            }
            # Fusion-center sharing is now its own standalone check, so
            # we no longer piggyback it as a caveat on the federal
            # check. Caveat dict kept in case future checks need it.
            caveat_details = {}
            peer_t = atype if type_totals.get(atype, 0) >= MIN_PEER_SAMPLE else "all"
            if peer_t == "all":
                total_peers = sum(type_totals.values())
            else:
                total_peers = type_totals[peer_t]

            for section_items, out_list in (
                (SB34_CHECKLIST, checklist_sb34),
                (TRANSPARENCY_CHECKLIST, checklist_transparency),
            ):
                for item in section_items:
                    cid = item["id"]
                    if peer_t == "all":
                        peer_pass = checklist_pass_all[cid]
                        peer_applicable = checklist_applicable_all[cid]
                    else:
                        peer_pass = checklist_pass_by_type[peer_t][cid]
                        peer_applicable = checklist_applicable_by_type[peer_t][cid]
                    out_list.append({
                        "id": cid,
                        # Three labels so the UI can show text that
                        # matches the pass/fail/unknown state instead
                        # of a static assertion.
                        "label_pass": item.get("label_pass") or item.get("label"),
                        "label_fail": item.get("label_fail") or item.get("label"),
                        "label_unknown": item.get("label_unknown")
                            or (item.get("label_fail") or item.get("label")),
                        "detail": item.get("detail"),
                        "value": checklist_results.get(cid),  # True/False/None
                        # For sharing-based failures, list of entity names
                        # that triggered the failure. UI shows the first
                        # few and refers to Flagged Recipients for more.
                        "failure_entities": failure_details.get(cid) or [],
                        "caveat_entities": caveat_details.get(cid) or [],
                        # peer_count: # passing the check
                        # peer_applicable: # where the check is answerable
                        # peer_total: all CA peers (for checks like has_portal)
                        "peer_count": peer_pass,
                        "peer_applicable": peer_applicable,
                        "peer_total": total_peers,
                        "peer_type": peer_t,
                        # Statewide (all CA agencies, not type-scoped).
                        # Lets the UI show "X% of CA city police pass
                        # AND Y% of all CA agencies pass" so the reader
                        # sees whether the concern is widespread or
                        # specific to this agency's peer group.
                        "state_count": checklist_pass_all[cid],
                        "state_applicable": checklist_applicable_all[cid],
                        "state_total": sum(type_totals.values()),
                    })

        # ── Percentiles (only for CA crawled) ──
        #
        # Emits both raw and per-1,000-residents percentiles. Per-capita
        # percentiles use the population of this agency to normalize; the
        # comparison is against other agencies' per-capita rates in the
        # same peer group.
        percentiles = {}
        medians = {}
        peer_sample = {}
        percentiles_per_1000 = {}
        medians_per_1000 = {}
        peer_sample_per_1000 = {}
        # Local (county / 50-mile) percentiles, parallel to statewide.
        percentiles_local = {}
        medians_local = {}
        peer_sample_local = {}
        percentiles_per_1000_local = {}
        medians_per_1000_local = {}
        peer_sample_per_1000_local = {}
        # Camera density: cameras per square mile of jurisdiction.
        # Only meaningful for place/county-FIPS agencies.
        cameras_per_sqmi = None
        percentile_density = None
        median_density = None
        peer_sample_density = None
        percentile_density_local = None
        median_density_local = None
        peer_sample_density_local = None
        # Downstream-searches comparison (state + local).
        downstream_total = None
        percentile_downstream = None
        median_downstream = None
        peer_sample_downstream = None
        percentile_downstream_local = None
        median_downstream_local = None
        peer_sample_downstream_local = None
        if state == RANKED_STATE and crawled:
            metric_values = {
                "cameras": cameras,
                "vehicles_30d": vehicles_30d,
                "hotlist_hits_30d": hotlist_hits,
                "searches_30d": searches_30d,
                "outbound": outbound_count,
            }

            # Compute the local peer pool once for this agency. The same
            # pool is reused for raw and per-capita rankings. See the
            # county_fips extraction above for the kind-specific logic.
            geo = reg.get("geo") or {}
            fips = geo.get("fips") or ""
            kind = geo.get("kind")
            if kind in ("county", "cousub") and len(fips) >= 5:
                source_county = fips[:5]
            elif kind == "place" and len(fips) == 7:
                source_county = county_fips_for_place(fips) or ""
            else:
                source_county = ""
            local_peers, local_scope = local_peers_for(
                aid, lat, lng, source_county
            )

            for metric, v in metric_values.items():
                series, fallback, peer_type = peer_series(metric, atype)
                percentiles[metric] = percentile_of(v, series)
                medians[metric] = median(series)
                peer_sample[metric] = {
                    "size": len(series),
                    "type": peer_type,
                    "fallback": fallback,
                }

                # Per-capita percentile (only if we have population)
                if population and v is not None:
                    rate = 1000.0 * v / population
                    r_series, r_fallback, r_peer_type = peer_rate_series(metric, atype)
                    percentiles_per_1000[metric] = percentile_of(rate, r_series)
                    medians_per_1000[metric] = median(r_series)
                    peer_sample_per_1000[metric] = {
                        "size": len(r_series),
                        "type": r_peer_type,
                        "fallback": r_fallback,
                    }

                # Local percentiles (same peer pool for all metrics).
                if local_scope:
                    l_series = local_series(local_peers, metric, per_capita=False)
                    if l_series:
                        percentiles_local[metric] = percentile_of(v, l_series)
                        medians_local[metric] = median(l_series)
                        peer_sample_local[metric] = {
                            "size": len(l_series),
                            "scope": local_scope,  # "county" or "nearby"
                        }
                    # Per-capita local
                    if population and v is not None:
                        l_rate_series = local_series(local_peers, metric, per_capita=True)
                        if l_rate_series:
                            rate = 1000.0 * v / population
                            percentiles_per_1000_local[metric] = percentile_of(rate, l_rate_series)
                            medians_per_1000_local[metric] = median(l_rate_series)
                            peer_sample_per_1000_local[metric] = {
                                "size": len(l_rate_series),
                                "scope": local_scope,
                            }

            # ── Downstream-searches peer comparison ──
            # Read the pre-computed total from our pass-1 cache — keeps
            # this block O(peers) instead of re-iterating recipients.
            for cm in crawled_agency_metrics:
                if cm["agency_id"] == aid:
                    downstream_total = cm["downstream_searches"]
                    break
            if downstream_total is not None:
                ds_series, ds_fb, ds_pt = peer_downstream_series(atype)
                percentile_downstream = percentile_of(downstream_total, ds_series)
                median_downstream = median(ds_series)
                peer_sample_downstream = {
                    "size": len(ds_series),
                    "type": ds_pt,
                    "fallback": ds_fb,
                }
                if local_scope:
                    l_ds = sorted(
                        p["downstream_searches"]
                        for p in local_peers
                        if p.get("downstream_searches") is not None
                    )
                    if l_ds:
                        percentile_downstream_local = percentile_of(downstream_total, l_ds)
                        median_downstream_local = median(l_ds)
                        peer_sample_downstream_local = {
                            "size": len(l_ds),
                            "scope": local_scope,
                        }

            # ── Camera density (cameras per sq mi) ──
            # Name-based fallback for land area on manual-geocoded entries,
            # mirroring the _pop / _area fallback logic.
            if not land_sqmi and state:
                candidate = _place_name_from_agency(
                    agency_display_name(reg, slug)
                )
                if candidate:
                    place = lookup_place(candidate, state)
                    if place:
                        land_sqmi = lookup_area_sqmi(place["fips"])
            if cameras is not None and land_sqmi and land_sqmi > 0:
                cameras_per_sqmi = round(cameras / land_sqmi, 2)
                d_series, d_fallback, d_peer_type = peer_density_series(atype)
                percentile_density = percentile_of(cameras_per_sqmi, d_series)
                median_density = median(d_series)
                peer_sample_density = {
                    "size": len(d_series),
                    "type": d_peer_type,
                    "fallback": d_fallback,
                }
                # Local density (using the same local peer pool)
                if local_scope:
                    l_d_series = sorted(
                        p["density"].get("cameras")
                        for p in local_peers
                        if p["density"].get("cameras") is not None
                    )
                    if l_d_series:
                        percentile_density_local = percentile_of(cameras_per_sqmi, l_d_series)
                        median_density_local = median(l_d_series)
                        peer_sample_density_local = {
                            "size": len(l_d_series),
                            "scope": local_scope,
                        }

        # ── Downstream search reach ──
        # Self searches plus all crawled outbound recipients' searches
        # over the last 30 days. This approximates how many times the
        # agency's ALPR data is being queried across the network it
        # shares with — the agency may run 100 searches itself but its
        # data could be touched by 50,000 queries when redistributed to
        # hundreds of recipients.
        #
        # Caveat: many portals don't publish searches_30d. Coverage is
        # the share of outbound recipients where we have a number.
        #
        # Also collects a per-recipient breakdown so the report can show
        # the top re-searchers (recipients that query the most) alongside
        # the aggregate number.
        ds_total = searches_30d if isinstance(searches_30d, (int, float)) else 0
        ds_outbound_with_data = 0
        # Split the "why aren't we counting this recipient?" into two
        # populations so the caveat can distinguish "no portal at all"
        # from "has portal but didn't publish searches". These are
        # different accountability stories: the first is the recipient
        # not operating a transparency page, the second is the page
        # existing but omitting the number.
        ds_no_portal = 0
        ds_portal_no_field = 0
        top_researchers = []
        for target_id in outbound_ids:
            t_gdata = graph_agencies.get(target_id, {})
            tr = reg_by_id.get(target_id, {})
            if not t_gdata.get("crawled"):
                ds_no_portal += 1
                continue
            t_portal = _load_portal_json(tr)
            t_searches = t_portal.get("searches_30d")
            if isinstance(t_searches, (int, float)):
                ds_total += t_searches
                ds_outbound_with_data += 1
                top_researchers.append({
                    "slug": id_to_slug.get(target_id, target_id),
                    "name": agency_display_name(tr, id_to_slug.get(target_id, target_id)),
                    "searches": int(t_searches),
                })
            else:
                ds_portal_no_field += 1
        top_researchers.sort(key=lambda r: -r["searches"])
        downstream_searches = {
            "total": int(ds_total) if ds_total else 0,
            "recipients_total": len(outbound_ids),
            "recipients_with_data": ds_outbound_with_data,
            "recipients_no_portal": ds_no_portal,
            "recipients_portal_no_search_field": ds_portal_no_field,
            "self_included": isinstance(searches_30d, (int, float)),
            "top_researchers": top_researchers[:10],
        } if outbound_ids else None

        # ── Outbound reach metrics ──
        # farthest: single farthest recipient — shows extreme reach.
        # average: mean distance across geocoded recipients — shows
        #   typical breadth; a high average signals widespread sharing
        #   even when the farthest point is just one outlier.
        # Skips recipients without geocoded coordinates.
        farthest = None
        outbound_avg_km = None
        if lat is not None and lng is not None:
            best_km = 0
            best_entry = None
            dists = []
            for target_id in outbound_ids:
                tr = reg_by_id.get(target_id, {})
                t_lat, t_lng = agency_coords(tr)
                if t_lat is None or t_lng is None:
                    continue
                d = dist_km(lat, lng, t_lat, t_lng)
                dists.append(d)
                if d > best_km:
                    best_km = d
                    best_entry = tr
            if best_entry:
                farthest = {
                    "name": agency_display_name(best_entry, id_to_slug.get(best_entry.get("agency_id"), "")),
                    "slug": id_to_slug.get(best_entry.get("agency_id"), ""),
                    "state": agency_state(best_entry),
                    "distance_km": round(best_km, 1),
                }
            if dists:
                outbound_avg_km = round(sum(dists) / len(dists), 1)

        # ── Flag classification of outbound recipients ──
        # Registry notes are HTML (contain anchor tags etc.); we pass them
        # through unescaped on the report side, so this is a data-flow
        # XSS surface. Source is the curated agency_registry.json (not
        # user input), but anyone editing the registry must avoid script
        # content — there's no sanitizer layer.
        flagged_recipients = []
        for target_id in outbound_ids:
            kind = is_flagged_entity(target_id, reg_by_id)
            if kind:
                tr = reg_by_id.get(target_id, {})
                flagged_recipients.append({
                    "agency_id": target_id,
                    "slug": id_to_slug.get(target_id, target_id),
                    "name": agency_display_name(tr, id_to_slug.get(target_id, target_id)),
                    "kind": kind,
                    "ag_lawsuit": has_tag(tr, "ag-lawsuit"),
                    "notes": tr.get("notes"),
                })

        # ── Inferred inbound (for uncrawled agencies mostly) ──
        inbound_source_ids = set(inbound_ids) | inbound_inferred_by_id.get(aid, set())
        inbound_list = []
        for source_id in sorted(inbound_source_ids):
            if source_id == aid:
                continue
            sr = reg_by_id.get(source_id, {})
            s_slug = id_to_slug.get(source_id, source_id)
            inbound_list.append({
                "agency_id": source_id,
                "slug": s_slug,
                "name": agency_display_name(sr, s_slug),
                "inferred": source_id not in inbound_ids,
            })

        # Outbound list (for display). Combines direct (agency's own
        # portal) with inferred (agencies that name this one in their
        # inbound list). For crawled agencies the direct list is the
        # canonical source; inferred edges are rare. For uncrawled
        # agencies, direct is empty and inferred is the only signal.
        direct_out_ids = set(outbound_ids)
        inferred_out_ids = outbound_inferred_by_id.get(aid, set()) - direct_out_ids - {aid}
        outbound_list = []
        def _outbound_entry(target_id, is_inferred):
            tr = reg_by_id.get(target_id, {})
            t_slug = id_to_slug.get(target_id, target_id)
            # Lat/lng needed for the mini regional map. Not every
            # recipient is geocoded (e.g. HOAs, shopping centers);
            # missing coords simply omit the recipient from the map.
            t_lat, t_lng = agency_coords(tr)
            return {
                "agency_id": target_id,
                "slug": t_slug,
                "name": agency_display_name(tr, t_slug),
                "kind": is_flagged_entity(target_id, reg_by_id),
                "inferred": is_inferred,
                "lat": t_lat,
                "lng": t_lng,
                "state": agency_state(tr),
            }
        for target_id in outbound_ids:
            outbound_list.append(_outbound_entry(target_id, False))
        for target_id in sorted(inferred_out_ids):
            outbound_list.append(_outbound_entry(target_id, True))

        # ── Regional context: crawled CA agencies within 50 km ──
        regional = []
        if lat is not None and lng is not None:
            for a in ca_crawled:
                if a["agency_id"] == aid:
                    continue
                a_lat, a_lng = agency_coords(a["reg"])
                if a_lat is None or a_lng is None:
                    continue
                d = dist_km(lat, lng, a_lat, a_lng)
                if d > REGIONAL_RADIUS_KM:
                    continue
                a_cameras = a["graph"].get("camera_count") or 0
                a_vehicles = a["portal"].get("vehicles_detected_30d")
                a_searches = a["portal"].get("searches_30d")
                a_outbound = a["graph"].get("sharing_outbound_count", 0)
                a_flagged = sum(
                    1 for tid in a["graph"].get("sharing_outbound_ids", [])
                    if is_flagged_entity(tid, reg_by_id)
                )
                a_pop = _pop(a)
                regional.append({
                    "slug": a["slug"],
                    "name": agency_display_name(a["reg"], a["slug"]),
                    "type": a["type"],
                    "distance_km": round(d, 1),
                    "population": a_pop,
                    "cameras": a_cameras,
                    "vehicles_30d": a_vehicles,
                    "searches_30d": a_searches,
                    "outbound": a_outbound,
                    "flagged": a_flagged,
                    "cameras_per_1000": round(1000.0 * a_cameras / a_pop, 2) if a_pop else None,
                    "vehicles_per_1000": round(1000.0 * a_vehicles / a_pop, 0) if (a_pop and a_vehicles is not None) else None,
                    "searches_per_1000": round(1000.0 * a_searches / a_pop, 2) if (a_pop and a_searches is not None) else None,
                })
            regional.sort(key=lambda r: r["distance_km"])

        reports[slug] = {
            "slug": slug,
            "agency_id": aid,
            "name": agency_display_name(reg, slug),
            "active_slug": agency_active_slug(reg),
            "state": state,
            "agency_type": atype,
            "agency_role": reg.get("agency_role"),
            "tags": reg.get("tags") or [],
            "notes": reg.get("notes"),
            # Agency-specific documented discrepancies or
            # misrepresentations we've found in public records
            # (e.g., portal says X but internal dashboard says Y).
            # Renders as a warning callout on the report header.
            "data_concerns": reg.get("data_concerns") or [],
            "ag_lawsuit": has_tag(reg, "ag-lawsuit"),
            "geo": reg.get("geo") or {},
            "crawled": crawled,
            "crawled_date": crawl_status(reg, DATA_DIR)[1],
            "population": population,
            "household_vehicles": household_vehicles,
            "land_sqmi": land_sqmi,
            "cameras_per_sqmi": cameras_per_sqmi,
            "percentile_density": percentile_density,
            "median_density": median_density,
            "peer_sample_density": peer_sample_density,
            "percentile_density_local": percentile_density_local,
            "median_density_local": median_density_local,
            "peer_sample_density_local": peer_sample_density_local,
            "downstream_total": downstream_total,
            "percentile_downstream": percentile_downstream,
            "median_downstream": median_downstream,
            "peer_sample_downstream": peer_sample_downstream,
            "percentile_downstream_local": percentile_downstream_local,
            "median_downstream_local": median_downstream_local,
            "peer_sample_downstream_local": peer_sample_downstream_local,
            "stats": {
                "cameras": cameras,
                "retention_days": retention,
                "vehicles_30d": vehicles_30d,
                "hotlist_hits_30d": hotlist_hits,
                "searches_30d": searches_30d,
                "outbound_count": outbound_count,
                "inbound_count": inbound_count,
            },
            "per_1000": _per_1000({
                "cameras": cameras,
                "vehicles_30d": vehicles_30d,
                "hotlist_hits_30d": hotlist_hits,
                "searches_30d": searches_30d,
            }, population),
            "percentiles": percentiles,
            "medians": medians,
            "peer_sample": peer_sample,
            "percentiles_per_1000": percentiles_per_1000,
            "medians_per_1000": medians_per_1000,
            "peer_sample_per_1000": peer_sample_per_1000,
            "farthest_outbound": farthest,
            "outbound_avg_km": outbound_avg_km,
            "downstream_searches": downstream_searches,
            "percentiles_local": percentiles_local,
            "medians_local": medians_local,
            "peer_sample_local": peer_sample_local,
            "percentiles_per_1000_local": percentiles_per_1000_local,
            "medians_per_1000_local": medians_per_1000_local,
            "peer_sample_per_1000_local": peer_sample_per_1000_local,
            "checklist_sb34": checklist_sb34,
            "checklist_transparency": checklist_transparency,
            "flagged_recipients": flagged_recipients,
            "outbound": outbound_list,
            "inbound": inbound_list,
            "regional": regional,
        }

    # ── Metadata ──
    # ── Sparkline distributions ──
    # One histogram per (metric, agency_type) for the statewide peer
    # distribution. Per-agency local histograms are computed
    # per-report (see peer_sample_local above) — they depend on
    # which peers are within 25 miles of each agency.
    sparkline_state = {}  # metric -> { type: {bins, min, max} }
    for metric in ["cameras", "vehicles_30d", "hotlist_hits_30d", "searches_30d", "outbound"]:
        sparkline_state[metric] = {}
        for t, series in series_by_type[metric].items():
            if len(series) >= MIN_PEER_SAMPLE:
                sparkline_state[metric][t] = histogram(sorted(series))
        if series_all[metric]:
            sparkline_state[metric]["all"] = histogram(sorted(series_all[metric]))
    sparkline_state["cameras_per_sqmi"] = {}
    for t, series in density_series_by_type.items():
        if len(series) >= MIN_PEER_SAMPLE:
            sparkline_state["cameras_per_sqmi"][t] = histogram(sorted(series))
    if density_series_all:
        sparkline_state["cameras_per_sqmi"]["all"] = histogram(sorted(density_series_all))
    sparkline_state["downstream"] = {}
    for t, series in downstream_series_by_type.items():
        if len(series) >= MIN_PEER_SAMPLE:
            sparkline_state["downstream"][t] = histogram(sorted(series))
    if downstream_series_all:
        sparkline_state["downstream"]["all"] = histogram(sorted(downstream_series_all))

    metadata = {
        "ca_crawled_total": len(ca_crawled),
        "ca_all_total": len(ca_all),
        "population_source": population_meta(),
        "type_totals": dict(type_totals),
        "sparkline_state": sparkline_state,
        "checklist_sb34": [
            {
                "id": item["id"],
                "label_pass": item.get("label_pass"),
                "label_fail": item.get("label_fail"),
                "label_unknown": item.get("label_unknown"),
                "detail": item.get("detail"),
            }
            for item in SB34_CHECKLIST
        ],
        "checklist_transparency": [
            {
                "id": item["id"],
                "label_pass": item.get("label_pass"),
                "label_fail": item.get("label_fail"),
            }
            for item in TRANSPARENCY_CHECKLIST
        ],
        "regional_radius_km": REGIONAL_RADIUS_KM,
        "min_peer_sample": MIN_PEER_SAMPLE,
    }

    out = {
        "metadata": metadata,
        "reports": reports,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out) + "\n")
    print(f"Wrote {OUT_PATH}: {len(reports)} agencies")


if __name__ == "__main__":
    main()
