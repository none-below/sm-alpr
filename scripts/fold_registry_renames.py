#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Fold registry rows fractured by Flock renames (driven by detect_renames.py).

When Flock renames an agency X -> X', the registry derives a new slug from the
new display name and mints a SECOND agency_id, splitting one real agency into
two rows. This tool merges each such split back into one row.

Merge rule (matches the hand-verified UCB / Las Vegas Metro folds):
  - CANONICAL survivor = the row whose ``.slug`` is a real crawled portal slug
    (so the crawl binds via ``.slug`` directly); else the field-richest row
    (better geo: place > manual > state-only; then has ORI). Deterministic
    tie-break on agency_id.
  - Preserve the UNION of fields: all ``flock_names`` spellings, richer ``geo``,
    unioned ``ori`` / ``tags`` / ``flags``, ``website`` fallback. ``display_name``
    is set to the CURRENT (post-rename) spelling. ``flock_slugs`` keeps only REAL
    portal slugs (phantom name-derived slugs are dropped — they have no captures
    and ``--prune`` would strip them anyway).
  - Delete the other row(s).

Hard safety guard — a group is SKIPPED (never merged) and reported when its rows
disagree on ``agency_role`` or ``agency_type``. A City PD and a County SO in the
same place are different agencies; this stops the role-word-strip false match
(e.g. "Sacramento SO - CA" vs "Sacramento CA PD").

Transitive: three spellings of one agency (e.g. Banner Elk / (DNU) / [DO NOT USE])
are unioned into a single survivor via connected components.

Format-preserving: reproduces ``json.dumps(indent=2)`` and refuses to write if it
cannot round-trip the file, so the diff is limited to the touched rows.
Dry-run by default.

Usage:
  uv run python scripts/fold_registry_renames.py                  # dry run, high-confidence (>=3 portals)
  uv run python scripts/fold_registry_renames.py --min-portals 2  # include medium
  uv run python scripts/fold_registry_renames.py --write
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import portal_jsons  # noqa: E402
from detect_renames import detect, DATA_DIR  # noqa: E402

REGISTRY_PATH = Path("assets/agency_registry.json")
ARTICLE_DIR = Path("assets/article_registry")
PROBE_STATE = Path("assets/transparency.flocksafety.com/.slug_probe_state.json")


def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def repoint_refs(remap, article_dir=ARTICLE_DIR, probe_state=PROBE_STATE):
    """Repoint agency_id references (dropped id -> survivor id) in committed
    data that keys on agency_id, so folding a row never leaves a dangling
    reference. Covers the article registry (``agencies`` /
    ``primary_subject_agency_ids`` id lists and ``agency_candidates[].agency_id``)
    and the slug-probe cache (top-level keys). Format-preserving. Returns the
    list of changed paths."""
    changed = []
    if not remap:
        return changed
    if article_dir.is_dir():
        for shard in sorted(article_dir.glob("*.json")):
            raw = shard.read_text(encoding="utf-8")
            if not any(d in raw for d in remap):
                continue
            data = json.loads(raw)
            for field in ("agencies", "primary_subject_agency_ids"):
                if isinstance(data.get(field), list):
                    data[field] = _dedup(remap.get(x, x) for x in data[field])
            if isinstance(data.get("agency_candidates"), list):
                seen, newc = set(), []
                for c in data["agency_candidates"]:
                    if isinstance(c, dict) and c.get("agency_id") in remap:
                        c = {**c, "agency_id": remap[c["agency_id"]]}
                    aid = c.get("agency_id") if isinstance(c, dict) else c
                    if aid in seen:
                        continue  # dedup (list is score-sorted; keep first)
                    seen.add(aid)
                    newc.append(c)
                data["agency_candidates"] = newc
            out = json.dumps(data, indent=2) + "\n"   # matches article_store.py
            if out != raw:
                shard.write_text(out)
                changed.append(str(shard))
    if probe_state.exists():
        raw = probe_state.read_text(encoding="utf-8")
        data = json.loads(raw) if any(d in raw for d in remap) else None
        # Probe records nest under .agencies keyed by agency_id. Only rewrite if
        # we can round-trip the file exactly (keeps the diff to the touched keys).
        if isinstance(data, dict) and isinstance(data.get("agencies"), dict) \
                and json.dumps(data, indent=2) + "\n" == raw:
            new_ag = {}
            for k, v in data["agencies"].items():
                new_ag.setdefault(remap.get(k, k), v)  # survivor's own record wins on collision
            if new_ag != data["agencies"]:
                data["agencies"] = new_ag
                probe_state.write_text(json.dumps(data, indent=2) + "\n")
                changed.append(str(probe_state))
    return changed


def _confirmed_portal_slugs(data_dir):
    """Slugs with at least one committed snapshot .json on disk."""
    out = set()
    for d in data_dir.iterdir():
        if d.is_dir() and not d.name.startswith(".") and portal_jsons(d):
            out.add(d.name)
    return out


def _geo_rank(geo):
    """Rank a geo by usefulness: having coords dominates, then specificity.
    Kinds: place > cousub/county > manual > state/state-only > ambiguous."""
    if not isinstance(geo, dict):
        return 0
    spec = {"place": 4, "cousub": 3, "county": 3, "manual": 2,
            "state": 1, "state-only": 1, "ambiguous": 0}.get(geo.get("kind"), 0)
    has_coords = geo.get("lat") is not None and geo.get("lng") is not None
    return (10 if has_coords else 0) + spec


def _as_list(v):
    return list(v) if isinstance(v, list) else ([] if v is None else [v])


def _union(*lists):
    seen, out = set(), []
    for lst in lists:
        for x in lst:
            if x not in seen:
                seen.add(x)
                out.append(x)
    return out


class _DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def _serialize(rows):
    return json.dumps(rows, indent=2, ensure_ascii=True) + "\n"


def plan_folds(registry, splits, confirmed, force_ids=frozenset()):
    """Return (merges, skips) for the given split edges. Pure — no mutation.
    A group whose every member id is in ``force_ids`` bypasses the role/type
    guard (a reviewed manual override, e.g. a same-agency city/county
    disambiguator)."""
    by_id = {r["agency_id"]: r for r in registry if r.get("agency_id")}

    # Connected components over split edges; keep per-group edge evidence.
    dsu = _DSU()
    edges = []
    for s in splits:
        a, b = s["old_id"], s["new_id"]
        if a in by_id and b in by_id and a != b:
            dsu.union(a, b)
            edges.append(s)

    groups = {}
    for s in edges:
        root = dsu.find(s["old_id"])
        groups.setdefault(root, {"ids": set(), "edges": []})
        groups[root]["ids"].update([s["old_id"], s["new_id"]])
        groups[root]["edges"].append(s)

    merges, skips = [], []
    for grp in groups.values():
        members = [by_id[i] for i in grp["ids"]]
        roles = {m.get("agency_role") for m in members}
        types = {m.get("agency_type") for m in members}
        label = " | ".join(sorted(m.get("slug") or "(null)" for m in members))
        forced = grp["ids"] <= force_ids
        if not forced and (len(roles) > 1 or len(types) > 1):
            skips.append({"ids": sorted(grp["ids"]), "label": label,
                          "reason": f"role/type mismatch roles={sorted(map(str,roles))} "
                                    f"types={sorted(map(str,types))} — likely NOT the same agency"})
            continue

        # Survivor: prefer a real-portal owner, then richer geo, then has ORI,
        # then deterministic (lowest agency_id).
        def owns_portal(m):
            slugs = [m.get("slug")] + _as_list(m.get("flock_slugs"))
            return any(s in confirmed for s in slugs if s)

        def quality(m):  # higher is better: real portal, then richer geo, then has ORI
            return (owns_portal(m), _geo_rank(m.get("geo")), bool(m.get("ori")))
        best = max(quality(m) for m in members)
        survivor = min((m for m in members if quality(m) == best),
                       key=lambda m: m["agency_id"])  # deterministic tie-break
        others = [m for m in members if m["agency_id"] != survivor["agency_id"]]

        # Current spelling = new-name of the strongest edge in the group.
        cur_edge = max(grp["edges"], key=lambda e: e["portals"])
        current_name = cur_edge["new"]

        all_names = _union(_as_list(survivor.get("flock_names")),
                           *[_as_list(m.get("flock_names")) for m in others],
                           *[[e["old"], e["new"]] for e in grp["edges"]])
        richest_geo = max(members, key=lambda m: _geo_rank(m.get("geo"))).get("geo")
        merged_slugs = [s for s in _union(
            _as_list(survivor.get("flock_slugs")),
            *[_as_list(m.get("flock_slugs")) for m in others],
            [m.get("slug") for m in members],
        ) if s in confirmed]
        active = survivor.get("flock_active_slug")
        if active not in confirmed:
            active = merged_slugs[0] if merged_slugs else None
        website = survivor.get("website") or next(
            (m.get("website") for m in others if m.get("website")), None)

        merges.append({
            "survivor_id": survivor["agency_id"],
            "survivor_slug": survivor.get("slug"),
            "drop_ids": [m["agency_id"] for m in others],
            "portals": cur_edge["portals"],
            "confidence": cur_edge["confidence"],
            "fields": {
                "flock_names": all_names,
                "display_name": current_name,
                "geo": richest_geo,
                "ori": (_union(_as_list(survivor.get("ori")),
                               *[_as_list(m.get("ori")) for m in others]) or None),
                "tags": _union(_as_list(survivor.get("tags")),
                               *[_as_list(m.get("tags")) for m in others]),
                "flags": _union(_as_list(survivor.get("flags")),
                                *[_as_list(m.get("flags")) for m in others]),
                "website": website,
                "flock_slugs": merged_slugs,
                "flock_active_slug": active,
            },
        })
    return merges, skips


def apply_merges(registry, merges):
    drop = {i for m in merges for i in m["drop_ids"]}
    patch = {m["survivor_id"]: m["fields"] for m in merges}
    out = []
    for row in registry:
        aid = row.get("agency_id")
        if aid in drop:
            continue
        if aid in patch:
            row = dict(row)
            row.update(patch[aid])
        out.append(row)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    ap.add_argument("--min-portals", type=int, default=3,
                    help="only fold renames seen on >= this many portals (default 3 = high confidence)")
    ap.add_argument("--write", action="store_true", help="apply the folds (default: dry run)")
    ap.add_argument("--force-ids", default="",
                    help="comma-separated agency_ids: merge that group even if rows disagree "
                         "on agency_role/agency_type (reviewed manual override)")
    ap.add_argument("--repoint-from", type=Path, default=None,
                    help="JSON {dropped_id: survivor_id}: only repoint references (article "
                         "registry, probe cache) for folds already applied to the registry; "
                         "does not touch the registry")
    args = ap.parse_args()

    # Repoint-only mode: fix dangling references left by folds applied earlier.
    if args.repoint_from:
        remap = json.loads(args.repoint_from.read_text())
        if args.write:
            changed = repoint_refs(remap)
            print(f"repointed {len(remap)} id(s) across {len(changed)} file(s):")
            for c in changed:
                print(f"  {c}")
        else:
            print(f"DRY RUN — would repoint {len(remap)} id(s). rerun with --write")
        return

    raw = REGISTRY_PATH.read_text(encoding="utf-8")
    if _serialize(json.loads(raw)) != raw:
        sys.exit("ABORT: cannot reproduce registry serialization; refusing to touch it.")
    registry = json.loads(raw)

    res = detect(args.data_dir, min_portals=3)
    splits = [r for r in res["renames"]
              if r["registry_split"] and r["portals"] >= args.min_portals]
    confirmed = _confirmed_portal_slugs(args.data_dir)
    force_ids = frozenset(i.strip() for i in args.force_ids.split(",") if i.strip())
    merges, skips = plan_folds(registry, splits, confirmed, force_ids=force_ids)

    print(f"splits>= {args.min_portals} portals: {len(splits)}  "
          f"-> merges: {len(merges)}  skipped: {len(skips)}\n")
    for m in merges:
        print(f"  MERGE [{m['confidence']}/{m['portals']}p] keep {m['survivor_slug']} "
              f"({m['survivor_id'][:8]}) <- drop {[i[:8] for i in m['drop_ids']]}")
        print(f"        names={m['fields']['flock_names']}")
        print(f"        display={m['fields']['display_name']!r} geo={(m['fields']['geo'] or {}).get('kind')} "
              f"slugs={m['fields']['flock_slugs']}")
    for s in skips:
        print(f"  SKIP  {s['label']}\n        {s['reason']}")

    if args.write and merges:
        new_rows = apply_merges(registry, merges)
        out = _serialize(new_rows)
        REGISTRY_PATH.write_text(out)
        print(f"\nWROTE {REGISTRY_PATH}: {len(registry)} -> {len(new_rows)} rows")
        remap = {d: m["survivor_id"] for m in merges for d in m["drop_ids"]}
        changed = repoint_refs(remap)
        if changed:
            print(f"repointed refs in {len(changed)} file(s): "
                  + ", ".join(c.split('/')[-1] for c in changed))
    elif args.write:
        print("\nnothing to write")
    else:
        print("\nDRY RUN — rerun with --write to apply")


if __name__ == "__main__":
    main()
