"""Where the built databases live. The code is in git (scripts/audit_db/); truth.duckdb and derived.duckdb are build outputs
kept out of git, in the primary checkout's .claude/audit_db/ (gitignored). Never a worktree's own .claude/: that goes away
with the worktree. Found through git's common dir, so every worktree resolves to the same place. AUDIT_DB_DIR overrides;
the CLIs that have --audit-dir let it override both, with no git needed.
"""
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

CODE = Path(__file__).parent   # this directory: scripts, SQL, authored JSON facts, docs/


def audit_dir():
    if os.environ.get("AUDIT_DB_DIR"):
        return Path(os.environ["AUDIT_DB_DIR"])
    try:
        r = subprocess.run(["git", "-C", str(CODE), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True)
        err = r.stderr.strip() if r.returncode else None
    except FileNotFoundError:
        err = "git is not installed"
    if err is not None:
        raise SystemExit(f"cannot locate the audit dir through git ({err or 'git failed'}): set AUDIT_DB_DIR, "
                         "or pass --audit-dir to the tools that take it")
    return Path(r.stdout.strip()).parent / ".claude" / "audit_db"


def duck_temp(parent=None):
    """A spill directory for one DuckDB process: <parent>/<pid>-<random>, parent default <system tmp>/alpr_duck_tmp.

    DuckDB 1.5 names its temp files by block size only (duckdb_temp_storage_S96K-0.tmp), so two processes that spill
    into one directory read each other's files: the queries fail, or return wrong results. Every connection that can
    spill gets its own directory. DuckDB creates it (one level only, hence the parent is created here) on the first
    spill and removes it on close."""
    parent = Path(parent) if parent else Path(tempfile.gettempdir()) / "alpr_duck_tmp"
    parent.mkdir(parents=True, exist_ok=True)
    return parent / f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
