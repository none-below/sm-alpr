"""Tests for the per-process DuckDB spill directories (paths.duck_temp, sweep_spill, use_spill_dir).

  uv run --locked --project scripts/audit_db --group dev pytest scripts/audit_db/tests
Skipped under the repo's root environment (no DuckDB there).
"""
import os
import subprocess
import sys
import time
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


def test_names_are_unique_and_the_parent_is_created(tmp_path):
    parent = tmp_path / "a" / "b"
    names = {paths.duck_temp(parent) for _ in range(50)}
    assert len(names) == 50 and parent.is_dir()
    assert all(n.parent == parent and paths.PID_DIR.fullmatch(n.name) and n.name.startswith(f"{os.getpid()}-") for n in names)
    assert not any(n.exists() for n in names)          # DuckDB creates the directory itself, on the first spill


def test_tilde_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = paths.duck_temp("~/spill")
    assert d.parent == tmp_path / "spill" and (tmp_path / "spill").is_dir() and not Path("~").exists()


def test_sweep_removes_dead_and_stale_directories_only(tmp_path):
    dead = tmp_path / f"{dead_pid()}-0123abcd"
    live = tmp_path / f"{os.getpid()}-89abcdef"
    stale_cli, fresh_cli = tmp_path / f"cli-{uuid.uuid4()}", tmp_path / f"cli-{uuid.uuid4()}"
    other = tmp_path / "not-a-spill-dir"
    for d in (dead, live, stale_cli, fresh_cli, other):
        d.mkdir()
        (d / "duckdb_temp_storage_S96K-0.tmp").write_bytes(b"x")
    old = time.time() - paths.CLI_STALE - 60
    for f in (stale_cli, stale_cli / "duckdb_temp_storage_S96K-0.tmp"):
        os.utime(f, (old, old))
    paths.duck_temp(tmp_path)
    assert {p.name for p in tmp_path.iterdir()} == {live.name, fresh_cli.name, other.name}


def test_use_spill_dir_sets_escapes_reuses_and_disables(tmp_path):
    con = duckdb.connect()
    con.execute("SET memory_limit='60MB'; SET threads=2")
    parent = tmp_path / "it's here"                     # an apostrophe in the path
    d = paths.use_spill_dir(con, parent)
    assert d.parent == parent and con.execute("SELECT current_setting('temp_directory')").fetchone()[0] == str(d)
    assert con.execute(SPILL_QUERY).fetchone() == (3000000, sum(range(3000000)))
    assert paths.use_spill_dir(con, parent) == d        # the same instance keeps its directory
    off = duckdb.connect()
    assert paths.use_spill_dir(off, "") is None
    assert off.execute("SELECT current_setting('temp_directory')").fetchone()[0] == ""


def test_concurrent_processes_under_one_parent_are_correct(tmp_path):
    code = ("import sys, duckdb; sys.path.insert(0, sys.argv[1]); import paths\n"
            "c = duckdb.connect(); c.execute(\"SET memory_limit='60MB'; SET threads=2\")\n"
            "paths.use_spill_dir(c, sys.argv[2]); print(c.execute(sys.argv[3]).fetchone())")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(Path(paths.__file__).parent), str(tmp_path), SPILL_QUERY],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    outs = [p.communicate(timeout=300) for p in procs]
    assert all(o.strip() == str((3000000, sum(range(3000000)))) for o, _ in outs), outs
    assert [p.name for p in tmp_path.iterdir()] == []   # every directory removed on close
