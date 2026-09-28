"""Where the built databases live. The code is in git (scripts/audit_db/); truth.duckdb and derived.duckdb are build outputs
kept out of git, in the primary checkout's .claude/audit_db/ (gitignored). Never a worktree's own .claude/: that goes away
with the worktree. Found through git's common dir, so every worktree resolves to the same place. AUDIT_DB_DIR overrides;
the CLIs' --audit-dir overrides both and needs no git at all.
"""
import os
import subprocess
from pathlib import Path

CODE = Path(__file__).parent   # this directory: scripts, SQL, authored JSON facts, docs/


def audit_dir():
    if os.environ.get("AUDIT_DB_DIR"):
        return Path(os.environ["AUDIT_DB_DIR"])
    r = subprocess.run(["git", "-C", str(CODE), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"cannot locate the audit dir through git ({r.stderr.strip() or 'git failed'}): "
                         "pass --audit-dir or set AUDIT_DB_DIR")
    return Path(r.stdout.strip()).parent / ".claude" / "audit_db"
