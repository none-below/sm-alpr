"""Where the built databases live. The code is in git (scripts/audit_db/); truth.duckdb and derived.duckdb are build outputs
kept out of git, in the primary checkout's .claude/audit_db/ (gitignored). Never a worktree's own .claude/: that goes away
with the worktree. Found through git's common dir, so every worktree resolves to the same place. AUDIT_DB_DIR overrides;
the CLIs that have --audit-dir let it override both, with no git needed.
"""
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
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


# Every DuckDB process spills into its own directory under a spill root: a directory these tools own outright, because
# they sweep it. DuckDB 1.5 names its temp files by block size only (duckdb_temp_storage_S96K-0.tmp), so processes that
# spill into one directory read each other's files: queries fail, or return wrong results. The default root is
# <audit dir>/spill: private (inside the user's home, unlike /tmp), untouched by the OS temp cleaner, and a name no
# earlier version used (they spilled into <tmp>/alpr_duck_tmp and <build tmp>/duck_tmp themselves, and DuckDB deletes a
# temp directory it created, contents included, when it closes). A root that cannot be created (a read-only audit dir)
# falls back to <per-user tmp>/alpr_audit_spill. init.sql sessions spill into spill-cli-<uuid> directly in the audit dir
# (SQL cannot learn its pid) and are never swept: an idle session still owns its directory, and DuckDB cannot move an
# instance that has spilled.
SPILL, FALLBACK = "spill", "alpr_audit_spill"
HOST = hashlib.sha1(socket.gethostname().encode()).hexdigest()[:6]      # a shared volume can hold other hosts' dirs
PID_DIR = re.compile(r"([0-9a-f]{6})-(\d{1,7})-[0-9a-f]{8}")           # <host>-<pid>-<random>: one process's directory


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, ValueError):   # someone else's process, or not a pid at all: leave it be
        return True
    return True


def sweep_spill(root):
    """Remove the directories that processes of this host left in a spill root when they died without closing DuckDB
    (SIGKILL, OOM, a closed terminal): <host>-<pid>-<random> with this host's tag and a pid that is gone. Other hosts'
    directories and anything else are left alone. Returns what it removed."""
    try:
        entries = list(Path(root).iterdir())
    except OSError:
        return []
    gone = []
    for d in entries:
        m = PID_DIR.fullmatch(d.name)
        try:   # a live process can create or delete its directory while this runs
            if m and m.group(1) == HOST and d.is_dir() and not pid_alive(int(m.group(2))):
                gone.append(d)
        except OSError:
            continue
    for d in gone:
        shutil.rmtree(d, ignore_errors=True)
    return gone


def spill_root(root=None):
    """The spill root to use, created if missing and made absolute: root (`~` expanded), default <audit dir>/spill. When
    it cannot be created (a read-only audit dir), <per-user tmp>/alpr_audit_spill instead, with a note on stderr."""
    r = Path(root).expanduser().resolve() if root else audit_dir().resolve() / SPILL
    try:
        r.mkdir(parents=True, exist_ok=True)
        return r
    except OSError as ex:
        fb = Path(tempfile.gettempdir()).resolve() / FALLBACK
        fb.mkdir(parents=True, exist_ok=True)
        print(f"note: cannot create spill root {r} ({ex.strerror}); spilling under {fb}", file=sys.stderr)
        return fb


def duck_temp(root=None):
    """A new spill directory name for this process: <root>/<host>-<pid>-<random>. The root is created (spill_root) and
    swept (sweep_spill); DuckDB creates the directory itself (one level) on the first spill and removes it on close."""
    r = spill_root(root)
    sweep_spill(r)
    return r / f"{HOST}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def duck_connect(database=":memory:", *, read_only=False, spill_root=None, **settings):
    """Open DuckDB with this process's own spill directory (use_spill_dir) and the given settings, e.g.
    duck_connect(path, read_only=True, spill_root=A / "spill", threads=2, memory_limit="2GB"). Every tool opens DuckDB
    through this (or audit_client.connect, which calls it), so none falls back to a shared spill directory. spill_root
    must be a directory these tools own (it is swept); "" disables spilling. String settings are quoted and escaped,
    numbers and booleans passed as they are, None skipped. On any error the connection is closed (no lock left)."""
    import duckdb
    con = duckdb.connect(str(database), read_only=read_only)
    try:
        use_spill_dir(con, spill_root)
        for k, v in settings.items():
            if v is None:
                continue
            val = str(v).lower() if isinstance(v, bool) else v if isinstance(v, (int, float)) else "'" + str(v).replace("'", "''") + "'"
            con.execute(f"SET {k} = {val}")
    except BaseException:
        con.close()
        raise
    return con


def use_spill_dir(con, root=None):
    """Point a DuckDB instance's spills at its own directory under root (duck_temp) and return it. root "" disables
    spilling (a query that needs more than memory_limit then fails). Settings belong to the instance, and every
    connection to one database file in one process shares it: an instance already spilling into one of this process's
    directories keeps it. DuckDB refuses to move (or disable) an instance that has already spilled; such an instance
    keeps the directory it has, with a note on stderr."""
    import duckdb
    cur = con.execute("SELECT current_setting('temp_directory')").fetchone()[0] or ""
    m = PID_DIR.fullmatch(Path(cur).name)
    if root != "" and m and m.group(1) == HOST and int(m.group(2)) == os.getpid():
        return Path(cur)
    d = None if root == "" else duck_temp(root)
    try:
        con.execute("SET temp_directory = '" + ("" if d is None else str(d).replace("'", "''")) + "'")
    except duckdb.Error as ex:
        if "switch" not in str(ex).lower():
            raise
        print(f"note: this DuckDB instance already spilled into {cur!r} and keeps it", file=sys.stderr)
        return Path(cur) if cur else None
    return d
