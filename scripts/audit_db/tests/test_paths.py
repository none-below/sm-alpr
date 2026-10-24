"""Tests for the per-process DuckDB spill directories (paths.duck_temp, owner, sweep_owned, sweep_spill, spill_root,
use_spill_dir, duck_connect).

  make test-audit-db
Skipped under the repo's root environment (no DuckDB there).
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paths  # noqa: E402

SPILL_QUERY = "SELECT count(*), sum(k) FROM (SELECT k FROM range(3000000) t(k) GROUP BY k)"   # spills at 60MB
CODE = str(Path(paths.__file__).parent)


def other_owner(root, *, wait):
    """Another process that takes an owner id in root and creates a directory under it (as DuckDB would on a spill).
    wait: it stays alive, holding its lock, until its stdin closes. Returns (process, directory)."""
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import paths\n"
            "d = paths.duck_temp(sys.argv[2]); d.mkdir(); (d / 'duckdb_temp_storage_S96K-0.tmp').write_bytes(b'x')\n"
            "print(d, flush=True)\n"
            "if sys.argv[3] == 'wait': sys.stdin.read()")
    p = subprocess.Popen([sys.executable, "-c", code, CODE, str(root), "wait" if wait else "exit"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    d = Path(p.stdout.readline().strip())
    if not wait:
        p.wait()
    return p, d


def test_names_are_unique_absolute_locked_and_private(tmp_path, monkeypatch):
    root = tmp_path / "a" / "b"
    names = {paths.duck_temp(root) for _ in range(50)}
    me = paths.owner(root)
    assert len(names) == 50 and all(n.parent == root and n.name.startswith(f"{me}-") for n in names)
    assert all(paths.OWNED_DIR.fullmatch(n.name) for n in names)
    assert not any(n.exists() for n in names)          # DuckDB creates the directory itself, on the first spill
    assert (root / f"{me}.lock").exists() and root.stat().st_mode & 0o777 == 0o700
    monkeypatch.chdir(tmp_path)
    assert paths.duck_temp("rel").parent == tmp_path.resolve() / "rel"   # made absolute: DuckDB would resolve it per cwd
    for cwd in (".", "./", Path("")):                                   # Path("") is "."; never sweep the working dir
        with pytest.raises(ValueError):
            paths.duck_temp(cwd)


def test_tilde_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = paths.duck_temp("~/spill")
    assert d.parent == tmp_path.resolve() / "spill" and (tmp_path / "spill").is_dir() and not Path("~").exists()


def test_sweep_removes_dead_owners_only(tmp_path):
    mine = paths.duck_temp(tmp_path)                    # first: duck_temp sweeps too
    mine.mkdir()
    live, live_dir = other_owner(tmp_path, wait=True)
    _, dead_dir = other_owner(tmp_path, wait=False)
    lockless = tmp_path / f"{uuid.uuid4().hex[:12]}-0123abcd"     # a leftover whose lock is already gone
    kept = [tmp_path / f"spill-cli-{uuid.uuid4()}", tmp_path / "not-a-spill-dir", tmp_path / "0123abcd-0123abcd"]
    for d in [lockless] + kept:
        d.mkdir()
    (tmp_path / f"{uuid.uuid4().hex[:12]}-89abcdef").write_text("a file, not a directory")
    (tmp_path / f"{uuid.uuid4().hex[:12]}-76543210").symlink_to(kept[1])
    try:
        gone = paths.sweep_spill(tmp_path)
        assert sorted(gone) == sorted([dead_dir, lockless])
        assert live_dir.exists() and mine.exists() and all(d.exists() for d in kept)
        assert not (tmp_path / f"{dead_dir.name[:12]}.lock").exists()   # a dead owner's lock goes with its dirs
    finally:
        live.stdin.close()
        live.wait()
    assert paths.sweep_spill(tmp_path) == [live_dir]                  # once its process is gone, whatever the host name
    assert paths.sweep_spill(tmp_path / "missing") == []


def test_sweep_owned_handles_nested_paths(tmp_path):
    _, dead_dir = other_owner(tmp_path, wait=False)
    o = dead_dir.name[:12]
    nested = tmp_path / "req" / f".tmp-unit-{o}-0123abcd"
    nested.mkdir(parents=True)
    assert paths.sweep_owned(tmp_path, [(o, nested)]) == [nested] and not nested.exists()


def test_unwritable_root_falls_back_to_a_private_home_cache(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ro = tmp_path / "ro"
    (ro / "spill").mkdir(parents=True)                  # the root exists, but cannot be written
    (ro / "spill").chmod(0o500)
    (tmp_path / "ro2").mkdir(mode=0o500)               # the root cannot even be created
    try:
        for root in (ro / "spill", tmp_path / "ro2" / "spill"):
            d = paths.duck_temp(root)
            assert d.parent == tmp_path / "home" / ".cache" / paths.FALLBACK
    finally:
        (ro / "spill").chmod(0o700)
        (tmp_path / "ro2").chmod(0o700)
    assert d.parent.stat().st_mode & 0o777 == 0o700


def test_use_spill_dir_sets_escapes_reuses_and_disables(tmp_path):
    con = duckdb.connect()
    con.execute("SET memory_limit='60MB'; SET threads=2")
    root = tmp_path / "it's here"                       # an apostrophe in the path
    d = paths.use_spill_dir(con, root)
    assert d.parent == root and con.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(d)
    assert con.execute(SPILL_QUERY).fetchone() == (3000000, sum(range(3000000)))
    assert paths.use_spill_dir(con, root) == d          # the same instance keeps its directory
    off = duckdb.connect()
    assert paths.use_spill_dir(off, "") is None
    assert off.execute("SELECT current_setting('temp_directory')").fetchone()[0] == ""


def test_an_instance_that_spilled_elsewhere_keeps_its_directory(tmp_path, capsys):
    con = duckdb.connect()
    manual = tmp_path / "set by hand"
    con.execute(f"SET memory_limit='60MB'; SET threads=2; SET temp_directory={paths.sql_str(manual)}")
    assert con.execute(SPILL_QUERY).fetchone()[0] == 3000000 and manual.is_dir()
    assert paths.use_spill_dir(con, tmp_path / "root") == manual   # DuckDB refuses the switch: no crash, a note
    assert con.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(manual)
    assert "keeps its spill directory" in capsys.readouterr().err


def test_concurrent_processes_under_one_root_are_correct(tmp_path):
    code = ("import sys, duckdb; sys.path.insert(0, sys.argv[1]); import paths\n"
            "c = duckdb.connect(); c.execute(\"SET memory_limit='60MB'; SET threads=2\")\n"
            "paths.use_spill_dir(c, sys.argv[2]); print(c.execute(sys.argv[3]).fetchone())")
    procs = [subprocess.Popen([sys.executable, "-c", code, CODE, str(tmp_path), SPILL_QUERY],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    outs = [p.communicate(timeout=300) for p in procs]
    assert all(o.strip() == str((3000000, sum(range(3000000)))) for o, _ in outs), outs
    assert not [p for p in tmp_path.iterdir() if paths.OWNED_DIR.fullmatch(p.name)]   # every directory removed on close
    paths.sweep_spill(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == []   # and their locks by the next sweep


def test_duck_connect_sets_spill_dir_and_escaped_settings(tmp_path):
    con = paths.duck_connect(spill_root=tmp_path / "sp", threads=1, memory_limit="61MB", enable_progress_bar=False,
                             max_temp_directory_size=None)
    got = dict(con.execute("SELECT name, value FROM duckdb_settings() WHERE name IN ('threads', 'memory_limit', "
                           "'temp_directory', 'enable_progress_bar')").fetchall())
    assert got["threads"] == "1" and got["enable_progress_bar"] == "false" and got["memory_limit"].startswith("58")
    assert Path(got["temp_directory"]).parent == tmp_path / "sp"
    with pytest.raises(duckdb.Error):                   # quoted as one value, which DuckDB then rejects
        paths.duck_connect(spill_root=tmp_path, memory_limit="1GB'; DROP TABLE x; --")


def test_audit_client_spill_roots(tmp_path):
    import audit_client as ac
    for name in ("derived.duckdb", "truth.duckdb"):
        duckdb.connect(str(tmp_path / name)).close()

    def temp(con):
        return con.execute("SELECT current_setting('temp_directory')").fetchone()[0]
    con = ac.connect(tmp_path, threads=1, memory="1GB")
    assert Path(temp(con)).parent == tmp_path / "spill"
    con.close()
    con = ac.connect(tmp_path, threads=1, memory="1GB", temp_dir=tmp_path / "base", max_temp=None)
    assert Path(temp(con)).parent == tmp_path / "base" / "alpr_audit_spill"   # a child it owns, never the base itself
    con.close()
    con = ac.connect(tmp_path, threads=1, memory="1GB", temp_dir="")
    assert temp(con) == ""
    con.close()
    (tmp_path / "truth.duckdb").write_bytes(b"not a database")
    with pytest.raises(duckdb.Error):
        ac.connect(tmp_path, threads=1, memory="1GB")
    duckdb.connect(str(tmp_path / "derived.duckdb")).close()   # the failed connect left no connection holding the file
