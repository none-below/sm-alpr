#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""
Detect backdated / late-appearing rows in Flock portal search-audit captures.

The portal's ``search_audit_csv`` is a rolling window of recent searches. Each
row carries an ``id`` (UUID) and a ``searchDate``. Across consecutive dated
snapshots of the same agency we ask: did a row appear in a *later* scrape
bearing a timestamp that means it *should already have been visible* in the
*previous* scrape?

For a snapshot pair (OLD, NEW), OLD captured first, we compare by id:
  - APPEARED     id in NEW, not in OLD
  - DISAPPEARED  id in OLD, not in NEW, with searchDate inside NEW's window
                 (so a sliding window can't explain its absence — it was removed)

Naive id-diffing OVERSTATES backdating, because the log also *mutates in place*:
the same logical search is re-emitted with a new id, a slightly shifted
timestamp and/or a different networkCount. That shows up as one DISAPPEARED +
one APPEARED a few minutes apart. We pair those off first (MUTATION) so they
don't masquerade as fabricated rows. What remains — an APPEARED row with no
disappeared counterpart nearby — is a genuine late arrival, classified by where
its searchDate T sits relative to OLD:

  - INTERLEAVED  T <= OLD.last_row_date
        OLD showed searches both before AND after T, yet not T. A rolling window
        cannot explain it; the row was inserted into already-captured history.
        Strongest signal. ``depth_days`` = how far before OLD's newest row.

  - GAP          OLD.last_row_date < T <= OLD.crawl_time
        T predates OLD's capture but sits past the last row OLD showed. Usually
        propagation lag (the tail hadn't synced at OLD's capture). Only computed
        when OLD records crawled_at; suppressed otherwise (no upper bound).

  - (normal)     T > OLD.crawl_time  — happened between captures; not reported.

ID-stability guard: if OLD rows that should still be visible in NEW mostly fail
to rejoin by id, ids are unstable for the pair and every diff is noise — the
pair is flagged id_unstable and suppressed.

Read-only. Reads only the deterministic per-snapshot .json parses (never raw
.html/.txt), per the repo's untrusted-scrape rules.

Usage:
    python3 scripts/audit_backdate.py                 # all agencies, summary
    python3 scripts/audit_backdate.py yuba-city-ca-pd # one or more slugs, verbose
    python3 scripts/audit_backdate.py --json          # full machine-readable dump
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import portal_jsons, parse_iso_dt

DATA_DIR = Path("assets/transparency.flocksafety.com")

# Below this carry-over ratio (with enough expected carry-over to be meaningful)
# the pair's ids are treated as unstable and candidates are suppressed.
CARRY_RATIO_MIN = 0.5
CARRY_EXPECTED_MIN = 8

# An appeared row within this many seconds of a disappeared row is treated as
# the same search re-emitted (a mutation), not a fresh insertion.
MUTATION_TOL_SEC = 15 * 60


def crawl_time(snap, stem):
    """Best estimate of when OLD was captured, as a comparable datetime.

    Prefer the recorded crawled_at; fall back to the last audit-row timestamp
    (a strict lower bound on capture time — keeps us conservative)."""
    dt = parse_iso_dt(snap.get("crawled_at"))
    if dt is not None:
        return dt
    sp = (snap.get("integrity") or {}).get("search_audit_csv", {}).get("date_span") or {}
    return parse_iso_dt(sp.get("max"))


def audit_rows(snap):
    """search_audit_csv rows that carry both an id and a parseable searchDate."""
    out = []
    for r in snap.get("search_audit_csv") or []:
        rid = r.get("id")
        t = parse_iso_dt(r.get("searchDate"))
        if rid and t is not None:
            out.append((rid, t, r))
    return out


def _row(rid, t, r, anchor):
    return {
        "id": rid,
        "searchDate": t.isoformat(),
        "reason": r.get("reason") or r.get("offenseType"),
        "networkCount": r.get("networkCount"),
        # Days the search predates OLD's newest captured row (interleave depth).
        "depth_days": round((anchor - t).total_seconds() / 86400, 2),
    }


def diff_pair(old, new, old_stem):
    """Classify NEW rows absent from OLD, pairing off in-place mutations first.

    Returns a result dict or None when nothing notable changed."""
    old_rows = audit_rows(old)
    new_rows = audit_rows(new)
    if not old_rows or not new_rows:
        return None

    old_ids = {rid for rid, _, _ in old_rows}
    new_ids = {rid for rid, _, _ in new_rows}
    old_crawl = crawl_time(old, old_stem)
    old_last = max(t for _, t, _ in old_rows)
    new_min = min(t for _, t, _ in new_rows)

    # ID-stability guard: OLD rows that fall inside NEW's window should rejoin
    # NEW by id. If they mostly don't, ids are unstable for this pair.
    expected_carry = [(rid, t, r) for rid, t, r in old_rows if t >= new_min]
    rejoined = sum(1 for rid, _, _ in expected_carry if rid in new_ids)
    carry_ratio = rejoined / len(expected_carry) if expected_carry else 1.0
    id_unstable = (
        len(expected_carry) >= CARRY_EXPECTED_MIN and carry_ratio < CARRY_RATIO_MIN
    )
    if id_unstable:
        return {
            "old": old_stem, "old_rows": len(old_rows), "new_rows": len(new_rows),
            "old_crawl": old_crawl.isoformat() if old_crawl else None,
            "old_last_row": old_last.isoformat(), "carry_ratio": round(carry_ratio, 3),
            "id_unstable": True, "churn_pairs": 0,
            "added_observed": 0, "removed_observed": 0, "interior_days": 0,
            "interleaved": [], "gap": [], "orphan_disappeared": 0,
        }

    # Upper bound for "suspicious" appeared rows: anything dated at/before OLD's
    # capture should already have been visible. Without crawled_at we fall back
    # to OLD's last row, restricting to the unambiguous interleaved zone.
    upper = old_crawl if old_crawl is not None else old_last

    # Appeared = id new to this scrape and dated on/before OLD's capture.
    appeared = sorted(
        ((rid, t, r) for rid, t, r in new_rows if rid not in old_ids and t <= upper),
        key=lambda x: x[1],
    )
    # Disappeared = OLD-window rows missing from NEW (a window can't drop these).
    disappeared = sorted(
        ((rid, t, r) for rid, t, r in expected_carry if rid not in new_ids),
        key=lambda x: x[1],
    )

    # Pair each appeared row with the nearest-in-time unused disappeared row.
    # A close pair is the same search re-emitted (the known "repeat plate within
    # an hour, one representative logged" churn) — NOT a fabricated insertion.
    dis_used = [False] * len(disappeared)
    churn_pairs = 0
    unpaired = []
    for rid, t, r in appeared:
        best, best_dt = -1, None
        for j, (_, dt, _) in enumerate(disappeared):
            if dis_used[j]:
                continue
            delta = abs((t - dt).total_seconds())
            if delta <= MUTATION_TOL_SEC and (best_dt is None or delta < best_dt):
                best, best_dt = j, delta
        if best >= 0:
            dis_used[best] = True
            churn_pairs += 1
        else:
            unpaired.append((rid, t, r))

    interleaved, gap = [], []
    for rid, t, r in unpaired:
        if t <= old_last:
            interleaved.append(_row(rid, t, r, old_last))
        elif old_crawl is not None:           # only a real "gap" when crawl known
            gap.append(_row(rid, t, r, old_last))

    orphan_disappeared = dis_used.count(False)

    # ── Swap-immune cross-check: per-DAY net count over the window both scrapes
    # fully observed. The repeat-search re-emission ("one representative logged
    # per plate-hour, which one logged can change") is net-zero within a day —
    # a row leaves and another enters the same hour. So any day where NEW shows
    # MORE rows than OLD is a genuine addition into already-observed history,
    # which that mechanism cannot produce. Boundary days (the day of new_min and
    # the day of old_last) are only partially observed by one side, so exclude
    # them to avoid edge artifacts.
    lo_day, hi_day = new_min.date(), old_last.date()
    old_day = Counter(t.date() for _, t, _ in old_rows if t >= new_min)
    new_day = Counter(t.date() for _, t, _ in new_rows if t <= old_last)
    interior = {d for d in (set(old_day) | set(new_day)) if lo_day < d < hi_day}
    added_observed = sum(max(0, new_day[d] - old_day[d]) for d in interior)
    removed_observed = sum(max(0, old_day[d] - new_day[d]) for d in interior)

    if not (interleaved or gap or churn_pairs or orphan_disappeared
            or added_observed or removed_observed):
        return None
    return {
        "old": old_stem,
        "old_rows": len(old_rows),
        "new_rows": len(new_rows),
        "old_crawl": old_crawl.isoformat() if old_crawl else None,
        "old_last_row": old_last.isoformat(),
        "carry_ratio": round(carry_ratio, 3),
        "id_unstable": False,
        "churn_pairs": churn_pairs,
        "added_observed": added_observed,
        "removed_observed": removed_observed,
        "interior_days": len(interior),
        "interleaved": interleaved,
        "gap": gap,
        "orphan_disappeared": orphan_disappeared,
    }


def analyze_agency(slug_dir):
    jsons = portal_jsons(slug_dir)
    if len(jsons) < 2:
        return None
    pairs = []
    for older, newer in zip(jsons, jsons[1:]):
        try:
            old = json.loads(older.read_text())
            new = json.loads(newer.read_text())
        except json.JSONDecodeError:
            continue
        res = diff_pair(old, new, older.stem)
        if res:
            res["new"] = newer.stem
            pairs.append(res)
    if not pairs:
        return None
    return {"slug": slug_dir.name, "pairs": pairs}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    want_json = "--json" in sys.argv
    filter_slugs = set(args) if args else None

    results = []
    for slug_dir in sorted(DATA_DIR.iterdir()):
        if not slug_dir.is_dir():
            continue
        if filter_slugs and slug_dir.name not in filter_slugs:
            continue
        r = analyze_agency(slug_dir)
        if r:
            results.append(r)

    if want_json:
        print(json.dumps(results, indent=2))
        return

    # interleaved = UNPAIRED late insertions inside a prior scrape's range — the
    # real backdating candidates (mutation churn already paired off). churn =
    # appeared rows matched to a near-simultaneous disappearance (known repeat-
    # search re-emission). gap = unpaired, dated in the tail between OLD's last
    # row and its capture (usually propagation lag).
    def totals(r):
        added = sum(p["added_observed"] for p in r["pairs"])
        removed = sum(p["removed_observed"] for p in r["pairs"])
        il = sum(len(p["interleaved"]) for p in r["pairs"])
        churn = sum(p["churn_pairs"] for p in r["pairs"])
        return added, removed, il, churn

    # Order by ADDED_OBSERVED — the swap-immune net additions — descending.
    results.sort(key=lambda r: (-totals(r)[0], -totals(r)[2], r["slug"]))

    g_added = g_removed = g_il = g_churn = 0
    flagged = []
    verbose = filter_slugs is not None
    for r in results:
        added, removed, il, churn = totals(r)
        g_added += added
        g_removed += removed
        g_il += il
        g_churn += churn
        if added or removed or il or churn:
            flagged.append((r["slug"], added, removed, il, churn))
        if verbose:
            print(f"\n=== {r['slug']} ===")
            for p in r["pairs"]:
                if p["id_unstable"]:
                    print(f"  {p['old']} -> {p['new']}  "
                          f"({p['old_rows']}->{p['new_rows']} rows) "
                          f"[ID_UNSTABLE carry={p['carry_ratio']} — suppressed]")
                    continue
                print(f"  {p['old']} -> {p['new']}  "
                      f"({p['old_rows']}->{p['new_rows']} rows; "
                      f"added_observed={p['added_observed']}, "
                      f"removed_observed={p['removed_observed']}, "
                      f"churn_pairs={p['churn_pairs']}, "
                      f"orphan_disappeared={p['orphan_disappeared']})")
                for e in p["interleaved"]:
                    print(f"    INTERLEAVED  {e['searchDate']}  "
                          f"{e['depth_days']}d deep  net={e['networkCount']}  "
                          f"reason={e['reason']!r}")
                for e in p["gap"]:
                    print(f"    gap          {e['searchDate']}  net={e['networkCount']}  "
                          f"reason={e['reason']!r}")

    if not verbose:
        print(f"{'AGENCY':38} {'ADDED':>6} {'REMOVED':>8} {'INTERLV':>8} {'CHURN':>6}")
        for slug, added, removed, il, churn in flagged:
            print(f"{slug:38} {added:>6} {removed:>8} {il:>8} {churn:>6}")
        print(f"\n{len(flagged)} agencies with changes. Totals: "
              f"{g_added} added_observed, {g_removed} removed_observed, "
              f"{g_il} interleaved, {g_churn} churn-pairs.")
        print("ADDED_OBSERVED = net rows that appeared on days a prior scrape had "
              "ALREADY fully observed. Swap-immune: the repeat-search re-emission")
        print("  issue is net-zero per day, so this column is the true 'should have "
              "been there already' signal. REMOVED = net day-deletions.")
        print("INTERLV = id-level unpaired late rows (corroboration; may still "
              "include un-paired swaps). CHURN = paired re-emissions (benign).")
        print("Re-run with a slug arg for per-row detail, or --json for full dump.")


if __name__ == "__main__":
    main()
