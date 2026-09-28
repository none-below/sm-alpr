"""Where the built databases live. The code is in git (scripts/audit_db/); truth.duckdb and derived.duckdb are build outputs
kept out of git, in the primary checkout's .claude/audit_db/ (gitignored). Never a worktree's own .claude/: that goes away
with the worktree. Found through git's common dir, so every worktree resolves to the same place. AUDIT_DB_DIR overrides;
the CLIs that have --audit-dir let it override both, with no git needed.
"""
import os
import re
import shutil
import subprocess
import time
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


# Every DuckDB process spills into its own directory under one parent. DuckDB 1.5 names its temp files by block size
# only (duckdb_temp_storage_S96K-0.tmp), so processes spilling into one directory read each other's files: queries fail,
# or return wrong results. The parent is <audit dir>/spill: private (inside the user's home, unlike /tmp), untouched by
# the OS temp cleaner, and a name no earlier version used. Earlier versions spilled into <tmp>/alpr_duck_tmp and
# <build tmp>/duck_tmp themselves, and DuckDB deletes a temp directory it created, contents included, when it closes.
SPILL = "spill"
PID_DIR = re.compile(r"(\d+)-[0-9a-f]{8}")                  # a Python tool's directory: <pid>-<random>
CLI_DIR = re.compile(r"cli-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")   # an init.sql session's: cli-<uuid>
CLI_STALE = 24 * 3600                                        # a cli- directory untouched this long is a leftover


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # someone else's process
        return True
    return True


def sweep_spill(parent):
    """Remove spill directories left by processes that died without closing DuckDB (SIGKILL, OOM, a closed terminal):
    <pid>-<random> whose pid is gone, and cli-<uuid> with no file touched for CLI_STALE seconds. Returns what it removed."""
    gone = []
    for d in Path(parent).iterdir() if Path(parent).is_dir() else []:
        m = PID_DIR.fullmatch(d.name)
        if m and d.is_dir() and not _alive(int(m.group(1))):
            gone.append(d)
        elif CLI_DIR.fullmatch(d.name) and d.is_dir():
            newest = max([f.stat().st_mtime for f in d.rglob("*")] + [d.stat().st_mtime])
            if time.time() - newest > CLI_STALE:
                gone.append(d)
    for d in gone:
        shutil.rmtree(d, ignore_errors=True)
    return gone


def duck_temp(parent=None):
    """A new spill directory name for this process: <parent>/<pid>-<random> (parent default <audit dir>/spill, `~`
    expanded). The parent is created and swept of dead processes' directories; DuckDB creates the directory itself (one
    level) on the first spill and removes it when the instance closes."""
    parent = Path(parent).expanduser() if parent else audit_dir() / SPILL
    parent.mkdir(parents=True, exist_ok=True)
    sweep_spill(parent)
    return parent / f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def use_spill_dir(con, parent=None):
    """Point a DuckDB instance's spills at its own directory (duck_temp) and return it. parent "" disables spilling (a
    query that needs more than memory_limit then fails). Settings belong to the instance, and every connection to one
    database file in one process shares it: if the instance already spills into one of this process's directories,
    that directory is kept (switching after a spill is refused)."""
    if parent == "":
        con.execute("SET temp_directory = ''")
        return None
    cur = con.execute("SELECT current_setting('temp_directory')").fetchone()[0] or ""
    m = PID_DIR.fullmatch(Path(cur).name)
    if m and int(m.group(1)) == os.getpid():
        return Path(cur)
    d = duck_temp(parent)
    con.execute("SET temp_directory = '" + str(d).replace("'", "''") + "'")
    return d
