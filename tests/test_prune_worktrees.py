"""Tests for scripts/prune_worktrees.py.

`git worktree remove` deletes gitignored files without a word, so the one
thing this script must never do is remove a worktree holding a gitignored
file that can't be regenerated (a worktree-level .claude/ draft, a .duckdb,
scratch notes). Each test builds a throwaway origin + primary checkout with
worktrees under .claude/worktrees/, the same layout new_worktree.sh makes.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import prune_worktrees as pw  # noqa: E402

MAKEFILE = (
    "GEN := \\\n\tdocs/data/gen.json \\\n\tdocs/data/gen2.json\n"
    "GEN_DIRS := docs/data/audit\n\n"
    "clean:\n\trm -f $(GEN) .make/stamp \\\n\t      docs/data/extra.json\n\trm -rf $(GEN_DIRS)\n\t@echo done\n"
)
GITIGNORE = ".venv/\n__pycache__/\n.claude/\n.make/\n.DS_Store\ndocs/data/\nnotes/\n*.duckdb\n.env*\n"
LATER = time.time() + 30 * 86400  # "now" far enough ahead that every worktree is idle


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def primary(tmp_path, monkeypatch):
    for k, v in {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    origin = tmp_path / "origin.git"
    run("git", "init", "-q", "--bare", "-b", "main", str(origin))
    repo = (tmp_path / "primary").resolve()
    run("git", "init", "-q", "-b", "main", str(repo))
    write(repo / "Makefile", MAKEFILE)
    write(repo / ".gitignore", GITIGNORE)
    write(repo / "README.md")
    run("git", "add", "-A", cwd=repo)
    run("git", "commit", "-qm", "init", cwd=repo)
    run("git", "remote", "add", "origin", str(origin), cwd=repo)
    run("git", "push", "-qu", "origin", "main", cwd=repo)
    return repo


def add_worktree(primary, name):
    """Worktree on branch <name> with one commit (the PR's commit). Returns (path, oid)."""
    wt = primary / ".claude" / "worktrees" / name
    run("git", "worktree", "add", "-q", "-b", name, str(wt), "refs/remotes/origin/main", cwd=primary)
    write(wt / f"{name}.txt")
    run("git", "add", "-A", cwd=wt)
    run("git", "commit", "-qm", name, cwd=wt)
    return wt, run("git", "rev-parse", "HEAD", cwd=wt)


def verdict(primary, wt, oids, now=LATER, idle_days=7, busy=()):
    entry = next(w for w in pw.list_worktrees(primary) if Path(w["worktree"]).resolve() == wt)
    main_clean = pw.make_clean_paths(MAKEFILE, primary)
    return pw.assess(entry, primary, now, idle_days, main_clean, set(busy),
                     pr_lookup=lambda repo, branch: [(7, set(oids))] if oids is not None else [])


def test_make_clean_paths_follows_variables_and_continuations(primary):
    files, dirs = pw.make_clean_paths(MAKEFILE, primary)
    assert files == {"docs/data/gen.json", "docs/data/gen2.json", ".make/stamp", "docs/data/extra.json"}
    assert dirs == {"docs/data/audit"}


def test_make_clean_paths_without_makefile_is_empty(primary):
    assert pw.make_clean_paths("", primary) == (set(), set())
    assert pw.make_clean_paths("all:\n\ttrue\n", primary) == (set(), set())


def test_merged_idle_worktree_with_only_regenerable_ignored_files_is_eligible(primary):
    wt, oid = add_worktree(primary, "done")
    for rel in [".venv/lib/site.py", "sub/.venv/x", "__pycache__/m.pyc", ".DS_Store", ".make/stamp",
                "docs/data/gen.json", "docs/data/audit/a.json", ".claude/settings.local.json"]:
        write(wt / rel)
    v = verdict(primary, wt, {oid})
    assert v.eligible, v.reasons


@pytest.mark.parametrize("rel", [".claude/drafts/letter.md", "notes/scratch.md", "audit.duckdb",
                                 ".env.local", "docs/data/hand_made.json"])
def test_unregenerable_ignored_file_keeps_the_worktree(primary, rel):
    wt, oid = add_worktree(primary, "has-work")
    write(wt / ".venv/lib/site.py")
    write(wt / rel, "only copy")
    v = verdict(primary, wt, {oid})
    assert not v.eligible
    assert v.unsafe_files == [rel]


def test_ignored_file_identical_to_primary_copy_is_safe_but_a_differing_one_is_not(primary):
    wt, oid = add_worktree(primary, "twin")
    write(primary / "notes/a.md", "same")
    write(wt / "notes/a.md", "same")
    assert verdict(primary, wt, {oid}).eligible
    write(wt / "notes/a.md", "edited in the worktree")
    assert verdict(primary, wt, {oid}).unsafe_files == ["notes/a.md"]


def test_origin_main_makefile_covers_a_file_the_branch_makefile_predates(primary):
    wt, oid = add_worktree(primary, "old-branch")
    write(wt / "Makefile", "clean:\n\trm -f docs/data/gen.json\n")
    run("git", "commit", "-qam", "older Makefile", cwd=wt)
    write(wt / "docs/data/gen2.json")  # only origin/main's Makefile lists it
    head = run("git", "rev-parse", "HEAD", cwd=wt)
    assert verdict(primary, wt, {oid, head}).eligible


def test_untracked_file_keeps_the_worktree(primary):
    wt, oid = add_worktree(primary, "untracked")
    write(wt / "draft.md")
    v = verdict(primary, wt, {oid})
    assert any("uncommitted/untracked" in r for r in v.reasons)


def test_recent_activity_keeps_the_worktree(primary):
    wt, oid = add_worktree(primary, "recent")
    v = verdict(primary, wt, {oid}, now=time.time())
    assert any(r.startswith("active") for r in v.reasons)


def test_recently_edited_ignored_file_counts_as_activity(primary):
    wt, oid = add_worktree(primary, "ignored-edit")
    old = time.time() - 30 * 86400
    for p in wt.rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))
    gitdir = Path(run("git", "rev-parse", "--absolute-git-dir", cwd=wt))
    for meta in ("index", "logs/HEAD", "HEAD"):
        os.utime(gitdir / meta, (old, old))
    assert verdict(primary, wt, {oid}, now=time.time()).eligible
    write(wt / "docs/data/gen.json")  # regenerable, but touching it means someone's working here
    assert any(r.startswith("active") for r in verdict(primary, wt, {oid}, now=time.time()).reasons)


def test_unmerged_branch_keeps_the_worktree(primary):
    wt, _ = add_worktree(primary, "open-pr")
    assert "no merged PR for this branch" in verdict(primary, wt, None).reasons


def test_commit_after_the_merged_pr_keeps_the_worktree(primary):
    wt, oid = add_worktree(primary, "kept-going")
    write(wt / "more.txt")
    run("git", "add", "-A", cwd=wt)
    run("git", "commit", "-qm", "after merge", cwd=wt)
    assert "1 local commit(s) not in PR #7" in verdict(primary, wt, {oid}).reasons


def test_busy_or_detached_worktree_is_kept(primary):
    wt, oid = add_worktree(primary, "busy")
    assert "a running process is using it" in verdict(primary, wt, {oid}, busy={str(wt / "scripts")}).reasons
    run("git", "checkout", "-q", "--detach", cwd=wt)
    assert any(r.startswith("detached HEAD") for r in verdict(primary, wt, {oid}).reasons)


def test_apply_removes_only_eligible_worktrees_and_keeps_branches_and_ignored_work(primary, monkeypatch, capsys):
    done, done_oid = add_worktree(primary, "done")
    write(done / ".venv/lib/site.py")
    kept, kept_oid = add_worktree(primary, "kept")
    write(kept / ".claude/drafts/letter.md", "only copy")
    write(primary / ".claude/worktrees/stray/file")  # not a worktree: never touched
    oids = {"done": {done_oid}, "kept": {kept_oid}}
    monkeypatch.setattr(pw, "merged_prs", lambda repo, branch: [(7, oids[branch])])
    monkeypatch.setattr(pw, "busy_paths", set)
    monkeypatch.chdir(primary)

    assert pw.main(["--idle-days", "0"]) == 0  # dry run first: nothing removed
    assert done.exists()

    assert pw.main(["--apply", "--idle-days", "0"]) == 0
    out = capsys.readouterr().out
    assert not done.exists()
    assert "removed done" in out
    assert run("git", "rev-parse", "--verify", "refs/heads/done", cwd=primary)  # branch kept
    assert (kept / ".claude/drafts/letter.md").read_text() == "only copy"
    assert (primary / ".claude/worktrees/stray/file").exists()
    log = (primary / ".claude/worktree_prune_log.tsv").read_text()
    assert "\tdone\tdone\t" in log and "kept" not in log
