#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""Helpers for the parallel catch-up workflow (.github/workflows/catchup-flock.yml).

The refresh workflow is serial by design: one run at a time, ~1 capture per
run before Flock's ~3 req/hr/IP budget ends it. When portals fall behind the
30-day audit window that is too slow, so the catch-up workflow fans a slug list
out across matrix jobs — each its own runner, so its own IP — and one final
job folds their results into the flock-refresh branch.

Three steps, one subcommand each:
  plan     split the slug list into per-job chunks (matrix JSON on stdout)
  collect  in a crawl job: package new captures + the meta-file entries the
           crawl changed for its own slugs
  merge    in the commit job: apply every job's package to the checkout and
           print the paths to stage

Only the meta entries a job actually changed are carried, so a job never
overwrites another run's newer value for a slug it didn't touch.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import FAILED_FILE, load_json, save_json  # noqa: E402

DATA_DIR = Path("assets/transparency.flocksafety.com")
META_FILES = (".content_hashes.json", ".attempts.json", ".rate_limit_burns.json", FAILED_FILE)
_ABSENT = None  # a meta entry the crawl removed (e.g. a cleared burn streak)


def plan(slugs, per_job):
    """Chunk `slugs` (order kept, duplicates dropped) into groups of per_job."""
    seen, ordered = set(), []
    for s in slugs:
        if s and s not in seen:
            seen.add(s)
            ordered.append(s)
    return [" ".join(ordered[i:i + per_job]) for i in range(0, len(ordered), per_job)]


def snapshot(data_dir, slugs):
    """Each meta file's entries for `slugs`, before the crawl."""
    return {
        name: {s: load_json(data_dir / name).get(s, _ABSENT) for s in slugs}
        for name in META_FILES
    }


def collect(data_dir, slugs, before, out_dir):
    """Copy new captures for `slugs` into out_dir/captures and write
    out_dir/meta.json with only the entries that changed since `before`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    captured = []
    for s in slugs:
        src = data_dir / s
        if src.is_dir() and any(src.iterdir()):
            shutil.copytree(src, out_dir / "captures" / s, dirs_exist_ok=True)
            captured.append(s)
    changed = {}
    for name in META_FILES:
        now = load_json(data_dir / name)
        diff = {s: now.get(s, _ABSENT) for s in slugs if now.get(s, _ABSENT) != before[name][s]}
        if diff:
            changed[name] = diff
    (out_dir / "meta.json").write_text(json.dumps(changed, indent=2, sort_keys=True) + "\n")
    return captured


def merge(data_dir, packages):
    """Apply each package dir (from collect) onto data_dir. Returns the paths
    to stage: every new capture file, plus each meta file that changed.

    Never overwrites an existing capture file — a slug the refresh branch
    already captured on the same date keeps that capture, so the two can't
    collide on rebase.
    """
    stage, skipped = [], []
    meta_updates = {}
    for pkg in packages:
        cap = pkg / "captures"
        if cap.is_dir():
            for f in sorted(cap.rglob("*")):
                if not f.is_file():
                    continue
                dst = data_dir / f.relative_to(cap)
                if dst.exists():
                    skipped.append(str(dst))
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                stage.append(str(dst))
        meta_path = pkg / "meta.json"
        if meta_path.is_file():
            for name, entries in json.loads(meta_path.read_text()).items():
                meta_updates.setdefault(name, {}).update(entries)
    for name, entries in meta_updates.items():
        path = data_dir / name
        data = load_json(path)
        for slug, value in entries.items():
            if value is _ABSENT:
                data.pop(slug, None)
            else:
                data[slug] = value
        save_json(path, data)
        stage.append(str(path))
    return stage, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--per-job", type=int, default=2)
    p.add_argument("slugs", nargs="+")
    c = sub.add_parser("snapshot")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("slugs", nargs="+")
    c = sub.add_parser("collect")
    c.add_argument("--before", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("slugs", nargs="+")
    m = sub.add_parser("merge")
    m.add_argument("packages", nargs="+", type=Path)
    args = ap.parse_args()

    if args.cmd == "plan":
        print(json.dumps(plan(args.slugs, args.per_job)))
    elif args.cmd == "snapshot":
        args.out.write_text(json.dumps(snapshot(DATA_DIR, args.slugs)))
    elif args.cmd == "collect":
        before = json.loads(args.before.read_text())
        captured = collect(DATA_DIR, args.slugs, before, args.out)
        print(f"captured: {' '.join(captured) or '(none)'}")
    elif args.cmd == "merge":
        stage, skipped = merge(DATA_DIR, args.packages)
        for s in skipped:
            print(f"already present, kept: {s}", file=sys.stderr)
        print("\n".join(stage))


if __name__ == "__main__":
    main()
