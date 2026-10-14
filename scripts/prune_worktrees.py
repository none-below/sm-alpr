#!/usr/bin/env python3
"""
Remove finished worktrees under .claude/worktrees/ — the counterpart to
scripts/new_worktree.sh. Each worktree grows to ~3GB, so a few weeks of
one-chat-per-worktree fills the disk; this reclaims the ones that are done.

A worktree is removed only when ALL of these hold:
  1. its branch has a merged PR, and every local commit not on origin is one
     of that PR's commits (so nothing committed after the merge);
  2. `git status` is clean — no tracked edits, no untracked files;
  3. every gitignored file is regenerable: an env/cache dir (.venv,
     __pycache__, .pytest_cache, ...), something `make clean` deletes (per the
     worktree's own Makefile or origin/main's), or a byte-identical copy of
     the same path in the primary checkout. Anything
     else gitignored — a worktree-level .claude/, .env, a .duckdb, local-only
     PRA xlsx, scratch notes — keeps the worktree and is listed, because
     `git worktree remove` would delete it without a word;
  4. nothing in it (ignored files included, env dirs excluded) changed in the
     last --idle-days days;
  5. no running process has its cwd inside it, and it isn't locked.

Removal is plain `git worktree remove` (never --force), so git itself also
refuses anything dirty. Branch refs are kept; restore with
`git worktree add .claude/worktrees/<name> <branch>`. Each removal is logged
to <primary>/.claude/worktree_prune_log.tsv.

Usage:
  python3 scripts/prune_worktrees.py              # dry run: report only
  python3 scripts/prune_worktrees.py --apply      # remove the eligible ones
  python3 scripts/prune_worktrees.py --idle-days 14
"""

import argparse
import datetime as dt
import filecmp
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ENV_DIRS = {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules", ".make"}
ENV_FILES = {".DS_Store"}
# Claude Code writes permission approvals here when one is granted in the
# worktree; harness state, not work product.
HARNESS_FILES = {".claude/settings.local.json"}


def git(*args, cwd):
    # --no-optional-locks: `git status` would otherwise refresh and rewrite the
    # index, and that write must not look like activity on the next run.
    return subprocess.run(["git", "--no-optional-locks", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


def primary_checkout(cwd):
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd).strip()
    return Path(common).parent


def make_clean_paths(makefile_text, cwd):
    """Paths a Makefile's `clean` target deletes: (files, dirs), repo-relative.
    Empty if there's no Makefile or no clean target."""
    files, dirs = set(), set()
    if not makefile_text:
        return files, dirs
    r = subprocess.run(["make", "-n", "-f", "-", "clean"], cwd=cwd, input=makefile_text,
                       capture_output=True, text=True)
    if r.returncode:
        return files, dirs
    out = r.stdout
    for line in out.replace("\\\n", " ").splitlines():
        words = shlex.split(line)
        if not words or words[0] != "rm":
            continue
        targets = [w for w in words[1:] if not w.startswith("-")]
        recursive = any(w.startswith("-") and "r" in w for w in words[1:])
        for t in targets:
            (dirs if recursive else files).add(t.rstrip("/"))
    return files, dirs


def is_env(rel):
    parts = rel.rstrip("/").split("/")
    return any(p in ENV_DIRS for p in parts) or parts[-1] in ENV_FILES or rel.endswith(".pyc")


def is_make_clean(rel, files, dirs):
    rel = rel.rstrip("/")
    return rel in files or any(rel == d or rel.startswith(d + "/") for d in dirs)


def walk_files(root, rel):
    """Every file under root/rel (rel may be a file), skipping env dirs."""
    full = root / rel
    if not full.is_dir() or full.is_symlink():
        yield rel.rstrip("/")
        return
    for dirpath, dirnames, filenames in os.walk(full):
        dirnames[:] = [d for d in dirnames if d not in ENV_DIRS]
        for f in filenames:
            r = str(Path(dirpath, f).relative_to(root))
            if not is_env(r):
                yield r


def unsafe_ignored(wt, primary, clean_files, clean_dirs):
    """Gitignored files in wt that removal would destroy for good."""
    out = git("status", "--porcelain=v1", "-z", "--ignored=matching", cwd=wt)
    entries = [e[3:] for e in out.split("\0") if e.startswith("!! ")]
    unsafe = []
    for entry in entries:
        if is_env(entry) or is_make_clean(entry, clean_files, clean_dirs):
            continue
        for rel in walk_files(wt, entry):
            if is_make_clean(rel, clean_files, clean_dirs) or rel in HARNESS_FILES:
                continue
            src, twin = wt / rel, primary / rel
            if src.is_file() and twin.is_file() and filecmp.cmp(src, twin, shallow=False):
                continue
            unsafe.append(rel)
    return unsafe


def newest_mtime(wt, gitdir):
    """Latest change anywhere in the worktree: its files (env dirs skipped)
    and its HEAD reflog (commits, checkouts). Not the index: tools rewrite it
    on a plain status refresh, and staged work already shows as dirty."""
    newest = 0.0
    for meta in ("logs/HEAD", "HEAD"):
        p = Path(gitdir) / meta
        if p.exists():
            newest = max(newest, p.stat().st_mtime)
    for dirpath, dirnames, filenames in os.walk(wt):
        dirnames[:] = [d for d in dirnames if d not in ENV_DIRS and d != ".git"]
        for f in filenames:
            if f == ".git" or f in ENV_FILES:
                continue
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, f)).st_mtime)
            except FileNotFoundError:
                pass
    return newest


def busy_paths():
    """cwds of running processes (best effort; empty if lsof is missing)."""
    try:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-F", "n"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return set()
    return {line[1:] for line in out.splitlines() if line.startswith("n")}


def merged_prs(repo, branch):
    """Merged PRs whose head was this branch: [(number, {commit oids})]."""
    out = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "merged",
         "--json", "number,commits", "--limit", "20"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    return [(pr["number"], {c["oid"] for c in pr["commits"]}) for pr in json.loads(out)]


def list_worktrees(repo):
    wts, cur = [], {}
    for line in git("worktree", "list", "--porcelain", cwd=repo).splitlines() + [""]:
        if not line:
            if cur:
                wts.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val or True
    return wts


@dataclass
class Verdict:
    path: Path
    branch: str | None
    head: str
    pr: int | None = None
    reasons: list = field(default_factory=list)
    unsafe_files: list = field(default_factory=list)

    @property
    def eligible(self):
        return not self.reasons


def regenerable_paths(wt, main_clean):
    """`make clean` targets per this worktree's own Makefile, plus origin/main's
    (an older branch's Makefile may predate a generated file main now lists)."""
    mk = wt / "Makefile"
    files, dirs = make_clean_paths(mk.read_text() if mk.is_file() else "", wt)
    return files | main_clean[0], dirs | main_clean[1]


def assess(wt, primary, now, idle_days, main_clean, busy, pr_lookup=None):
    path = Path(wt["worktree"])
    branch = wt.get("branch", "")
    branch = branch.removeprefix("refs/heads/") if isinstance(branch, str) and branch else None
    v = Verdict(path=path, branch=branch, head=wt.get("HEAD", "")[:9])

    if wt.get("locked"):
        v.reasons.append("locked")
    if any(b == str(path) or b.startswith(str(path) + "/") for b in busy):
        v.reasons.append("a running process is using it")

    if branch is None:
        v.reasons.append("detached HEAD (no branch, no PR to check)")
    else:
        prs = (pr_lookup or merged_prs)(primary, branch)
        if not prs:
            v.reasons.append("no merged PR for this branch")
        else:
            v.pr = prs[0][0]
            covered = set().union(*(oids for _, oids in prs))
            local = git("rev-list", "HEAD", "--not", "--remotes=origin", cwd=path).split()
            extra = [c for c in local if c not in covered]
            if extra:
                v.reasons.append(f"{len(extra)} local commit(s) not in PR #{v.pr}")

    dirty = git("status", "--porcelain", cwd=path).splitlines()
    if dirty:
        v.reasons.append(f"{len(dirty)} uncommitted/untracked path(s)")

    v.unsafe_files = unsafe_ignored(path, primary, *regenerable_paths(path, main_clean))
    if v.unsafe_files:
        v.reasons.append(f"{len(v.unsafe_files)} gitignored file(s) that aren't regenerable")

    gitdir = git("rev-parse", "--absolute-git-dir", cwd=path).strip()
    age_days = max(0.0, (now - newest_mtime(path, gitdir)) / 86400)
    if age_days < idle_days:
        v.reasons.append(f"active {age_days:.1f} days ago (< {idle_days})")
    return v


def du_gb(path):
    out = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True).stdout
    return int(out.split()[0]) / 1048576 if out else 0.0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="remove eligible worktrees (default: dry run)")
    ap.add_argument("--idle-days", type=float, default=7, help="minimum days since last change (default 7)")
    args = ap.parse_args(argv)

    primary = primary_checkout(Path.cwd()).resolve()
    root = primary / ".claude" / "worktrees"
    main_mk = subprocess.run(["git", "show", "refs/remotes/origin/main:Makefile"], cwd=primary,
                             capture_output=True, text=True).stdout
    main_clean = make_clean_paths(main_mk, primary)
    busy = busy_paths()
    now = time.time()

    prunable = []
    verdicts = []
    for wt in list_worktrees(primary):
        p = Path(wt["worktree"]).resolve()
        if p.parent != root:
            continue
        if wt.get("prunable") or not p.is_dir():
            prunable.append(p.name)
            continue
        verdicts.append(assess(wt, primary, now, args.idle_days, main_clean, busy))

    eligible = [v for v in verdicts if v.eligible]
    for v in sorted(verdicts, key=lambda v: (not v.eligible, v.path.name)):
        label = "REMOVE" if v.eligible else "keep  "
        pr = f" PR #{v.pr}" if v.pr else ""
        print(f"{label} {v.path.name}  [{v.branch or 'detached'}{pr}]")
        for r in v.reasons:
            print(f"         - {r}")
        for f in v.unsafe_files[:10]:
            print(f"             {f}")
        if len(v.unsafe_files) > 10:
            print(f"             ... +{len(v.unsafe_files) - 10} more")
    if prunable:
        print(f"stale entries (folder already gone): {', '.join(prunable)}")

    sizes = {v.path: du_gb(v.path) for v in eligible}
    print(f"\n{len(eligible)} of {len(verdicts)} eligible, ~{sum(sizes.values()):.1f} GiB")
    if not args.apply:
        if eligible or prunable:
            print("Dry run. Re-run with --apply to remove them.")
        return 0

    log = primary / ".claude" / "worktree_prune_log.tsv"
    failed = 0
    for v in eligible:
        r = subprocess.run(["git", "worktree", "remove", str(v.path)], cwd=primary, capture_output=True, text=True)
        if r.returncode:
            failed += 1
            print(f"FAILED {v.path.name}: {r.stderr.strip()}", file=sys.stderr)
            continue
        with log.open("a") as fh:
            fh.write(f"{dt.date.today()}\t{v.path.name}\t{v.branch}\t{v.head}\tPR #{v.pr}\t{sizes[v.path]:.1f}G\n")
        print(f"removed {v.path.name}")
    if prunable:
        git("worktree", "prune", cwd=primary)
        print(f"pruned {len(prunable)} stale entr{'y' if len(prunable) == 1 else 'ies'}")
    print(f"log: {log}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
