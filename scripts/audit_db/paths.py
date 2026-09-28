"""Where the built databases live. The code is in git (scripts/audit_db/); truth.duckdb and derived.duckdb are build outputs
kept out of git, in the primary checkout's .claude/audit_db/ (gitignored). Never a worktree's own .claude/: that goes away
with the worktree. Found through git's common dir, so every worktree resolves to the same place. AUDIT_DB_DIR overrides;
the CLIs that have --audit-dir let it override both, with no git needed.
"""
import fcntl
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

CODE = Path(__file__).parent   # this directory: scripts, SQL, authored JSON facts, docs/


def audit_dir():
    if os.environ.get("AUDIT_DB_DIR"):
        return Path(os.environ["AUDIT_DB_DIR"])
    return primary_checkout() / ".claude" / "audit_db"


def primary_checkout():
    """The repo's primary checkout (git's common dir), where the local-only .claude/ material lives; the same from any
    worktree."""
    try:
        r = subprocess.run(["git", "-C", str(CODE), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True)
        err = r.stderr.strip() if r.returncode else None
    except FileNotFoundError:
        err = "git is not installed"
    if err is not None:
        raise SystemExit(f"cannot locate the primary checkout through git ({err or 'git failed'}): set AUDIT_DB_DIR, "
                         "or pass --audit-dir (and --evidence) to the tools that take them")
    return Path(r.stdout.strip()).parent


def evidence_dir():
    """The MuckRock evidence corpus: the primary checkout's .claude/local_evidence/muckrock-ca-audit-logs (the originals,
    catalog.json / catalog2.json / catalog_requests.json, MANIFEST_v2.txt). Local only, never in git.
    (Always the git-derived location: AUDIT_DB_DIR moves the databases, not the evidence; build_chunks --verify uses
    the evidence_dir truth was built from.)"""
    return primary_checkout() / ".claude" / "local_evidence" / "muckrock-ca-audit-logs"


def sql_str(v):
    """A SQL string literal: v as text, single quotes doubled."""
    return "'" + str(v).replace("'", "''") + "'"


# Every DuckDB process spills into its own directory under a spill root: a directory these tools own outright, because
# they sweep it. DuckDB 1.5 names its temp files by block size only (duckdb_temp_storage_S96K-0.tmp), so processes that
# spill into one directory read each other's files: queries fail, or return wrong results. The default root is
# <audit dir>/spill, untouched by the OS temp cleaner and a name no earlier version used (they spilled into
# <tmp>/alpr_duck_tmp and <build tmp>/duck_tmp themselves, and DuckDB deletes a temp directory it created, contents
# included, when it closes). Spill files hold released text and plates, so a root is made private (0700), and a root that
# cannot be written falls back to ~/.cache/alpr_audit_spill, never the shared /tmp.
#
# Whether a directory's process is alive is decided by a lock, not by a pid or a host name (pids are reused, and macOS
# can change the host name with the network). Each process holds an exclusive flock on <root>/<owner>.lock for as long
# as it lives, and the kernel drops it however the process ends; its directories are <owner>-<random>. A sweep removes
# the directories and lock file of every owner whose lock it can take, and <owner>-<random> directories with no lock
# file. A lock file appears only once it is locked (it is renamed into place), and a lock that cannot be checked (a file
# system without flock) counts as held. DuckDB CLI sessions (init.sql) do not spill: see there.
SPILL, FALLBACK = "spill", "alpr_audit_spill"
OWNED_DIR = re.compile(r"([0-9a-f]{12})-[0-9a-f]{8}")   # <owner>-<random>: one DuckDB instance's directory
LOCK_FILE = re.compile(r"([0-9a-f]{12})\.lock")         # <owner>.lock: held by the owning process while it lives
_owners = {}   # (root, pid) -> (owner, fd): this process's lock in each root, open until it exits


def owner(root):
    """This process's owner id in root, locking <root>/<owner>.lock the first time; the lock is held until the process
    exits. Raises OSError when root cannot hold a lock (not writable, or no flock)."""
    root = Path(root)
    key = (str(root), os.getpid())   # a forked child is a new owner
    if key not in _owners:
        o = uuid.uuid4().hex[:12]
        tmp = root / f".{o}.lock.new"
        fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.rename(tmp, root / f"{o}.lock")   # visible under its name only once locked
        except OSError:
            os.close(fd)
            tmp.unlink(missing_ok=True)
            raise
        _owners[key] = (o, fd)
    return _owners[key][0]


def _mine(name):
    m = OWNED_DIR.fullmatch(name)
    return bool(m) and any(o == m.group(1) for (_, pid), (o, _) in _owners.items() if pid == os.getpid())


def sweep_owned(lock_root, owned):
    """Remove each path of owned ([(owner, path)]) whose owner is dead: its lock in lock_root is free, or gone. Free
    locks are removed too, after their paths, so no sweep ever sees an owner's paths without its lock. A lock that
    cannot be checked counts as held. Returns the paths removed."""
    lock_root = Path(lock_root)
    try:
        locks = {m.group(1): e for e in lock_root.iterdir() if (m := LOCK_FILE.fullmatch(e.name))}
    except OSError:
        return []
    taken = {}
    for o, lock in locks.items():
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:   # another sweep removed it meanwhile
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # fails while the owner lives (even in this process)
            taken[o] = (lock, fd)
        except OSError:
            os.close(fd)
    gone = []
    try:
        for o, path in owned:
            if o in taken or (o not in locks and not (lock_root / f"{o}.lock").exists()):
                shutil.rmtree(path, ignore_errors=True)
                if not path.exists():
                    gone.append(path)
    finally:
        for lock, fd in taken.values():
            lock.unlink(missing_ok=True)
            os.close(fd)
    return gone


def sweep_spill(root):
    """Remove what processes that died without closing DuckDB (SIGKILL, OOM, a closed terminal) left in a spill root:
    the <owner>-<random> directories of dead owners (sweep_owned), and their lock files. Anything else (a file or
    symlink with such a name included) is left alone.
    Returns the directories removed."""
    try:
        owned = [(m.group(1), e) for e in Path(root).iterdir()
                 if (m := OWNED_DIR.fullmatch(e.name)) and e.is_dir() and not e.is_symlink()]
    except OSError:
        return []
    return sweep_owned(root, owned)


def _private_dir(d):
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = d.stat()
    if st.st_uid == os.getuid() and st.st_mode & 0o077:
        d.chmod(0o700)


def spill_root(root=None):
    """The spill root to use, made absolute (`~` expanded), private and locked for this process: root, default
    <audit dir>/spill. When it cannot be (a read-only audit dir, a file system without flock), ~/.cache/alpr_audit_spill
    instead, with a note on stderr. "." is refused: Path("") is ".", so a caller meaning "" (no spilling) would otherwise
    make the working directory a swept root."""
    if root is not None and os.path.normpath(str(root)) == ".":
        raise ValueError('spill root "." (or Path("")): pass "" to disable spilling, or a directory of its own')
    r = Path(root).expanduser().resolve() if root else audit_dir().resolve() / SPILL
    try:
        _private_dir(r)
        owner(r)
        return r
    except OSError as ex:
        fb = Path.home() / ".cache" / FALLBACK
        _private_dir(fb)
        owner(fb)
        print(f"note: cannot spill under {r} ({ex.strerror or ex}); spilling under {fb}", file=sys.stderr)
        return fb


def duck_temp(root=None):
    """A new spill directory name for this process: <root>/<owner>-<random>. The root is set up (spill_root) and swept
    (sweep_spill); DuckDB creates the directory itself (one level) on the first spill and removes it on close."""
    r = spill_root(root)
    sweep_spill(r)
    return r / f"{owner(r)}-{uuid.uuid4().hex[:8]}"


def duck_connect(database=":memory:", *, read_only=False, spill_root=None, **settings):
    """Open DuckDB with this process's own spill directory (use_spill_dir) and the given settings, e.g.
    duck_connect(path, read_only=True, spill_root=A / "spill", threads=2, memory_limit="2GB"). Every tool opens DuckDB
    through this (or audit_client.connect, which calls it), so none falls back to a shared spill directory, except
    build_derived.py: it is derived.duckdb's only writer, so DuckDB's default derived.duckdb.tmp is its own. spill_root
    must be a directory these tools own (it is swept); "" disables spilling. String settings are quoted and escaped,
    numbers and booleans passed as they are, None skipped. On any error the connection is closed (no lock left)."""
    import duckdb
    con = duckdb.connect(str(database), read_only=read_only)
    try:
        use_spill_dir(con, spill_root)
        for k, v in settings.items():
            if v is None:
                continue
            val = str(v).lower() if isinstance(v, bool) else v if isinstance(v, (int, float)) else sql_str(v)
            con.execute(f"SET {k} = {val}")
    except BaseException:
        con.close()
        raise
    return con


def use_spill_dir(con, root=None):
    """Point a DuckDB instance's spills at its own directory under root (duck_temp) and return it. root "" disables
    spilling (a query that needs more than memory_limit then fails). Settings belong to the instance, and every
    connection to one database file in one process shares it: an instance already spilling into one of this process's
    directories keeps it. DuckDB refuses to move (or disable) an instance that has already spilled: when the change is
    refused, the instance keeps the directory it has, with a note on stderr."""
    import duckdb
    cur = con.execute("SELECT current_setting('temp_directory')").fetchone()[0] or ""
    if root != "" and cur and _mine(Path(cur).name):
        return Path(cur)
    d = None if root == "" else duck_temp(root)
    try:
        con.execute(f"SET temp_directory = {sql_str('' if d is None else d)}")
    except duckdb.Error as ex:
        if not cur or con.execute("SELECT current_setting('temp_directory')").fetchone()[0] != cur:
            raise
        print(f"note: this DuckDB instance keeps its spill directory {cur!r} ({ex})", file=sys.stderr)
        return Path(cur)
    return d
