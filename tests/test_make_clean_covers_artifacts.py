# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 zero-below
"""`make clean` must remove every gitignored artifact under docs/.

A generated file that .gitignore knows about but the clean target misses
survives `make clean` and quietly lingers in every worktree that has run a
build — docs/data/audit_departures.json did exactly that, because it is
written by scripts/build_map.py but was never added to BUILD_FILES.

Both sides are derived, not listed here: the artifact set comes from
.gitignore, the removal set from `make -n clean`. Adding a new generated
docs/ artifact to either place without the other fails this test.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _ignored_docs_paths() -> set[str]:
    """Gitignored paths under docs/, as written in .gitignore."""
    out = set()
    for raw in (ROOT / ".gitignore").read_text().splitlines():
        line = raw.strip()
        # Comments, blanks, and un-ignore negations are not artifacts.
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        # Wildcards describe families, not single files clean can rm by name.
        if not line.startswith("docs/") or "*" in line:
            continue
        out.add(line.rstrip("/"))
    return out


def _clean_removes() -> set[str]:
    """Paths `make clean` would delete, from its dry run."""
    proc = subprocess.run(
        ["make", "-n", "clean"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    removed = set()
    # A recipe's `rm` may span lines via backslash continuation; make echoes it
    # verbatim, so rejoin before splitting or the tail paths look unremoved.
    for line in proc.stdout.replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line.startswith("rm "):
            continue
        for tok in line.split()[1:]:
            if tok.startswith("-"):  # flags, e.g. -f / -rf
                continue
            removed.add(tok.rstrip("/"))
    return removed


def test_clean_removes_every_gitignored_docs_artifact():
    ignored = _ignored_docs_paths()
    # Guard the derivation itself: if .gitignore parsing silently returned
    # nothing, the subset assertion below would pass vacuously.
    assert len(ignored) > 5, f"suspiciously few ignored docs/ paths: {ignored}"

    missed = sorted(ignored - _clean_removes())
    assert not missed, (
        "gitignored docs/ artifacts that `make clean` leaves behind: "
        f"{missed}. Add each to BUILD_FILES (if `make build` writes it), "
        "DEPLOY_FILES, PRA_FILES, or BUILD_DIRS in the Makefile."
    )


def test_build_files_are_all_gitignored():
    """Nothing in the Makefile's removal list may be a tracked file.

    `make clean` deleting a committed file would show up as a spurious
    deletion in `git status` rather than a clean tree.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "docs", "assets"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split("\0")
    tracked = {t for t in tracked if t}

    clobbered = sorted(p for p in _clean_removes() if p in tracked)
    assert not clobbered, (
        f"`make clean` would delete tracked file(s): {clobbered}"
    )
