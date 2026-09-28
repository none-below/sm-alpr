"""Tests for the per-process DuckDB spill directories (paths.duck_temp, sweep_spill, spill_root, use_spill_dir,
duck_connect).

  uv run --locked --project scripts/audit_db --group dev pytest scripts/audit_db/tests
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


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def mine(pid):
    return f"{paths.HOST}-{pid}-{uuid.uuid4().hex[:8]}"


def test_names_are_unique_absolute_and_the_root_is_created(tmp_path, monkeypatch):
    root = tmp_path / "a" / "b"
    names = {paths.duck_temp(root) for _ in range(50)}
    assert len(names) == 50 and root.is_dir()
    assert all(n.parent == root and paths.PID_DIR.fullmatch(n.name) and n.name.startswith(f"{paths.HOST}-{os.getpid()}-")
               for n in names)
    assert not any(n.exists() for n in names)          # DuckDB creates the directory itself, on the first spill
    monkeypatch.chdir(tmp_path)
    assert paths.duck_temp("rel").parent == tmp_path.resolve() / "rel"   # made absolute: DuckDB would resolve it per cwd
    for cwd in (".", Path("")):                                         # Path("") is "."; never sweep the working dir
        with pytest.raises(ValueError):
            paths.duck_temp(cwd)


def test_tilde_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = paths.duck_temp("~/spill")
    assert d.parent == tmp_path.resolve() / "spill" and (tmp_path / "spill").is_dir() and not Path("~").exists()


def test_sweep_removes_only_this_hosts_dead_processes(tmp_path):
    dead = tmp_path / mine(dead_pid())
    live = tmp_path / mine(os.getpid())
    other_host = tmp_path / f"{'0' * 6 if paths.HOST != '0' * 6 else '1' * 6}-{dead_pid()}-0123abcd"
    cli = tmp_path / f"spill-cli-{uuid.uuid4()}"
    other = tmp_path / "not-a-spill-dir"
    for d in (dead, live, other_host, cli, other):
        d.mkdir()
        (d / "duckdb_temp_storage_S96K-0.tmp").write_bytes(b"x")
    (tmp_path / mine(dead_pid())).write_text("a file, not a directory")
    paths.duck_temp(tmp_path)
    left = {p.name for p in tmp_path.iterdir()}
    assert left == {live.name, other_host.name, cli.name, other.name} | {p.name for p in tmp_path.iterdir() if p.is_file()}
    assert paths.sweep_spill(tmp_path / "missing") == []
    assert paths.pid_alive(2 ** 40) and paths.pid_alive(os.getpid())   # not a pid (OverflowError): left alone


def test_unwritable_root_falls_back_to_per_user_tmp(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    monkeypatch.setattr(paths.tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    try:
        d = paths.duck_temp(ro / "spill")
    finally:
        ro.chmod(0o700)
    assert d.parent == (tmp_path / "tmp").resolve() / paths.FALLBACK


def test_use_spill_dir_sets_escapes_reuses_and_disables(tmp_path):
    con = duckdb.connect()
    con.execute("SET memory_limit='60MB'; SET threads=2")
    root = tmp_path / "it's here"                       # an apostrophe in the path
    d = paths.use_spill_dir(con, root)
    assert d.parent == root and con.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(d)
    assert con.execute(SPILL_QUERY).fetchone() == (3000000, sum(range(3000000)))
    assert paths.use_spill_dir(con, root) == d          # the same instance keeps its directory
    assert paths.use_spill_dir(con, tmp_path / "elsewhere") == d   # and cannot move once it has spilled
    off = duckdb.connect()
    assert paths.use_spill_dir(off, "") is None
    assert off.execute("SELECT current_setting('temp_directory')").fetchone()[0] == ""


def test_concurrent_processes_under_one_root_are_correct(tmp_path):
    code = ("import sys, duckdb; sys.path.insert(0, sys.argv[1]); import paths\n"
            "c = duckdb.connect(); c.execute(\"SET memory_limit='60MB'; SET threads=2\")\n"
            "paths.use_spill_dir(c, sys.argv[2]); print(c.execute(sys.argv[3]).fetchone())")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(Path(paths.__file__).parent), str(tmp_path), SPILL_QUERY],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    outs = [p.communicate(timeout=300) for p in procs]
    assert all(o.strip() == str((3000000, sum(range(3000000)))) for o, _ in outs), outs
    assert [p.name for p in tmp_path.iterdir()] == []   # every directory removed on close


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
