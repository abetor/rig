"""Tests for the SQLite schema, migrations, conventions, and data access layer."""




from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess

import pytest

from rig.db import convention, dao, migrate, schema



def test_schema_creates_all_tables(tmp_path):
    conn = dao.connect(str(tmp_path / "s.db"))
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "runs", "lessons", "manifest", "meta"} <= names

    assert migrate.get_version(conn) == schema.SCHEMA_VERSION == migrate.LATEST

    trigs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"runs_block_update", "runs_block_delete"} <= trigs
    conn.close()


def test_schema_status_is_enum_string(tmp_path):

    conn = dao.connect(str(tmp_path / "e.db"))
    ts = dao.now_iso()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sessions(session_id,name,status,created_ts,updated_ts) VALUES(1,'n','BOGUS',?,?)",
            (ts, ts),
        )
    conn.close()



def test_migrate_idempotent(tmp_path):
    p = str(tmp_path / "i.db")
    conn = sqlite3.connect(p)
    n1 = migrate.migrate(conn)
    n2 = migrate.migrate(conn)
    assert n1 == n2 == migrate.LATEST

    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "runs", "lessons", "manifest", "meta", "spawn_reservations"} <= names
    conn.close()


def test_migrate_docat_preserves_data(tmp_path):

    p = str(tmp_path / "m.db")
    conn = dao.connect(p)
    assert migrate.get_version(conn) == migrate.LATEST


    sid = dao.create_session(conn, name="proj-1", task="keep me")
    rid = dao.log_run(conn, session_id=sid, prompt="P", result="R")


    def _v_next(c):
        c.execute("ALTER TABLE sessions ADD COLUMN note TEXT NOT NULL DEFAULT ''")

    migs = list(migrate.MIGRATIONS) + [_v_next]
    new_ver = migrate.migrate(conn, migrations=migs)
    assert new_ver == migrate.LATEST + 1


    s = dao.get_session(conn, sid)
    assert s["task"] == "keep me"
    assert dao.get_run(conn, rid)["result"] == "R"
    assert conn.execute("SELECT note FROM sessions WHERE session_id=?", (sid,)).fetchone()[0] == ""


    assert migrate.migrate(conn, migrations=migs) == migrate.LATEST + 1

    assert migrate.migrate(conn) == migrate.LATEST + 1
    conn.close()


def test_migrate_fresh_install_jumps_to_head_no_replay(monkeypatch):






    head = ["CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT)"]
    monkeypatch.setattr(schema, "create_statements", lambda: head)

    def _base(c):
        for s in schema.create_statements():
            c.execute(s)

    def _v1_to_v2(c):
        c.execute("ALTER TABLE t ADD COLUMN note TEXT")

    conn = sqlite3.connect(":memory:")
    assert migrate.migrate(conn, migrations=[_base, _v1_to_v2], target=2) == 2
    assert migrate.get_version(conn) == 2
    assert "note" in {r[1] for r in conn.execute("PRAGMA table_info(t)")}
    conn.close()




_V1_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    branch       TEXT    NOT NULL DEFAULT '',
    tmux         TEXT    NOT NULL DEFAULT '',
    flow         TEXT    NOT NULL DEFAULT '',
    agent        TEXT    NOT NULL DEFAULT '',
    task         TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'active',
    head         TEXT    NOT NULL DEFAULT '',
    in_flight    TEXT    NOT NULL DEFAULT '[]',
    core_version TEXT    NOT NULL DEFAULT '',
    config       TEXT    NOT NULL DEFAULT '{}',
    created_ts   TEXT    NOT NULL,
    updated_ts   TEXT    NOT NULL
)
"""



_V1_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY,
    session_id  INTEGER REFERENCES sessions(session_id),
    ts          TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'invoke',
    agent       TEXT    NOT NULL DEFAULT '',
    adapter     TEXT    NOT NULL DEFAULT '',
    phase       TEXT    NOT NULL DEFAULT '',
    prompt      TEXT    NOT NULL DEFAULT '',
    result      TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 0,
    cost        REAL,
    cost_origin TEXT    NOT NULL DEFAULT '',
    resume_id   TEXT    NOT NULL DEFAULT '',
    meta        TEXT    NOT NULL DEFAULT '{}'
)
"""


def _build_v1_db(path: str) -> sqlite3.Connection:

    conn = sqlite3.connect(path)
    runs_triggers = [t for t in schema.trigger_statements() if "runs_block" in t]
    for stmt in schema.table_statements()[:4] + schema.index_statements()[:3] + runs_triggers:
        if "CREATE TABLE IF NOT EXISTS runs" in stmt:
            stmt = _V1_RUNS_DDL
        elif "CREATE TABLE IF NOT EXISTS sessions" in stmt:
            stmt = _V1_SESSIONS_DDL
        conn.execute(stmt)
    migrate.set_version(conn, 1)
    conn.commit()
    return conn


def test_migrate_v1_to_head_preserving_data(tmp_path):


    conn = _build_v1_db(str(tmp_path / "v1.db"))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "spawn_reservations" not in tables
    assert "transcript" not in {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    conn.execute("INSERT INTO sessions(session_id,name,created_ts,updated_ts) VALUES(7,'old','t','t')")
    conn.execute("INSERT INTO runs(ts,prompt,result) VALUES('t','item','r')")
    conn.commit()

    assert migrate.migrate(conn) == migrate.LATEST == 5
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "spawn_reservations" in tables
    assert "transcript" in {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert {"decisions", "board", "messages", "artifacts"} <= tables
    assert "handoff" in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert {"events", "liveness"} <= tables
    assert conn.execute("SELECT name FROM sessions WHERE session_id=7").fetchone()[0] == "old"
    assert conn.execute("SELECT prompt, transcript FROM runs").fetchone() == ("item", "")
    conn.close()


def test_light_run_readers_match_full(tmp_path):


    conn = dao.connect(str(tmp_path / "l.db"))
    sid = dao.create_session(conn, name="s")
    dao.log_run(conn, session_id=sid, kind="invoke", agent="worker", cost=1.5,
                prompt="item" * 1000, result="R" * 1000, transcript="THAT IS" * 100_000)
    dao.log_run(conn, session_id=sid, kind="invoke", agent="judge", cost=0.5)
    dao.log_run(conn, session_id=sid, kind="note", result="handoff-text")
    assert dao.count_runs(conn, sid, kind="invoke", agents=("worker", "sub")) == 1
    assert dao.count_runs(conn, sid) == 3
    assert dao.sum_run_cost(conn, sid) == 2.0
    note = dao.last_run(conn, session_id=sid, kind="note")
    assert note["result"] == "handoff-text"
    light = dao.list_runs_light(conn, sid)
    assert len(light) == 3
    assert light[0]["result"] == "R" * dao.RESULT_PREVIEW_CHARS
    assert "transcript" not in light[0] and "prompt" not in light[0]
    conn.close()


def test_migrate_v2_to_v3_transcript_column(tmp_path):

    conn = _build_v1_db(str(tmp_path / "v2.db"))
    conn.execute(schema._SPAWN_RESERVATIONS)
    migrate.set_version(conn, 2)
    conn.execute("INSERT INTO runs(ts,prompt,result) VALUES('t','old','response')")
    conn.commit()
    assert migrate.migrate(conn) == 5
    assert conn.execute("SELECT transcript FROM runs").fetchone() == ("",)
    conn.execute("INSERT INTO runs(ts,prompt,transcript) VALUES('t','new','{\"type\":\"x\"}')")
    assert conn.execute("SELECT transcript FROM runs WHERE prompt='new'").fetchone()[0].startswith("{")
    import pytest as _pytest
    with _pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE runs SET transcript='x'")
    conn.close()



def test_reserve_grants_under_cap_and_release_frees(tmp_path):
    conn = dao.connect(str(tmp_path / "r.db"))
    sid = dao.create_session(conn, name="s")
    ids = dao.try_reserve_spawns(conn, sid, slots=1, count_committed=lambda c: 0, max_spawns=2)
    assert len(ids) == 1
    assert dao.count_active_reservations(conn, sid) == 1
    dao.release_spawns(conn, ids)
    assert dao.count_active_reservations(conn, sid) == 0
    conn.close()


def test_reserve_refuses_when_runs_plus_reservations_hit_cap(tmp_path):


    conn = dao.connect(str(tmp_path / "c.db"))
    sid = dao.create_session(conn, name="s")
    a = dao.try_reserve_spawns(conn, sid, slots=1, count_committed=lambda c: 1, max_spawns=2)
    assert len(a) == 1
    b = dao.try_reserve_spawns(conn, sid, slots=1, count_committed=lambda c: 1, max_spawns=2)
    assert b == []
    conn.close()


def test_reserve_sweeps_stale_orphan_reservations(tmp_path):


    conn = dao.connect(str(tmp_path / "ttl.db"))
    sid = dao.create_session(conn, name="s")

    conn.execute("INSERT INTO spawn_reservations(session_id, created_ts) VALUES(?, '2000-01-01T00:00:00+00:00')",
                 (sid,))
    conn.commit()
    assert dao.count_active_reservations(conn, sid) == 1

    ids = dao.try_reserve_spawns(conn, sid, slots=1, count_committed=lambda c: 0, max_spawns=1)
    assert len(ids) == 1
    assert dao.count_active_reservations(conn, sid) == 1


def test_sweep_keeps_fresh_reservations(tmp_path):

    conn = dao.connect(str(tmp_path / "fresh.db"))
    sid = dao.create_session(conn, name="s")
    ids = dao.try_reserve_spawns(conn, sid, slots=1, count_committed=lambda c: 0, max_spawns=2)
    assert dao.sweep_stale_reservations(conn, sid) == 0
    assert dao.count_active_reservations(conn, sid) == 1
    conn.close()


def test_reserve_partial_grants_remaining_slots(tmp_path):

    conn = dao.connect(str(tmp_path / "p.db"))
    sid = dao.create_session(conn, name="s")
    ids = dao.try_reserve_spawns(conn, sid, slots=5, count_committed=lambda c: 0, max_spawns=3, partial=True)
    assert len(ids) == 3
    conn.close()


def test_reserve_atomic_across_two_connections_same_db(tmp_path):



    p = str(tmp_path / "shared.db")
    c1 = dao.connect(p)
    sid = dao.create_session(c1, name="s", config={"max_spawns": 2})
    c2 = dao.connect(p)
    g1 = dao.try_reserve_spawns(c1, sid, slots=2, count_committed=lambda c: 0, max_spawns=2)
    assert len(g1) == 2
    g2 = dao.try_reserve_spawns(c2, sid, slots=1, count_committed=lambda c: 0, max_spawns=2)
    assert g2 == []
    dao.release_spawns(c1, g1)
    g3 = dao.try_reserve_spawns(c2, sid, slots=1, count_committed=lambda c: 0, max_spawns=2)
    assert len(g3) == 1
    c1.close(); c2.close()



def test_convention_stable_path(tmp_path):
    proj = str(tmp_path / "proj")
    os.makedirs(proj)
    p1 = convention.db_path(proj)
    p2 = convention.db_path(proj + "/")
    p3 = convention.db_path(os.path.join(proj, "."))
    assert p1 == p2 == p3
    assert p1.endswith(os.path.join(".rig", "memory.db"))


def test_convention_creates_db(tmp_path):
    proj = str(tmp_path / "proj2")
    os.makedirs(proj)
    assert not os.path.exists(convention.db_path(proj))
    conn = dao.open_project(proj)
    assert os.path.exists(convention.db_path(proj))

    sid = dao.create_session(conn, name="x")
    assert dao.get_session(conn, sid)["name"] == "x"
    conn.close()


def test_ensure_rig_dir_writes_self_ignoring_gitignore(tmp_path):
    proj = str(tmp_path / "proj_gi")
    os.makedirs(proj)
    d = convention.ensure_rig_dir(proj)
    gi = os.path.join(d, ".gitignore")
    assert os.path.exists(gi)
    assert "*" in open(gi, encoding="utf-8").read()


def test_ensure_rig_dir_does_not_clobber_existing_gitignore(tmp_path):
    proj = str(tmp_path / "proj_gi2")
    convention.ensure_rig_dir(proj)
    gi = os.path.join(convention.rig_dir(proj), ".gitignore")
    open(gi, "w", encoding="utf-8").write("custom\n")
    convention.ensure_rig_dir(proj)
    assert open(gi, encoding="utf-8").read() == "custom\n"


def test_rig_dir_invisible_to_git_in_foreign_repo(tmp_path):

    git = shutil.which("git")
    if git is None:
        pytest.skip("git inaccessible")
    proj = str(tmp_path / "foreign")
    os.makedirs(proj)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run([git, "-C", proj, "init", "-q"], check=True, env=env)

    open(os.path.join(proj, ".gitignore"), "w").write("node_modules/\n")
    convention.ensure_rig_dir(proj)
    open(convention.db_path(proj), "w").write("fake-db")
    out = subprocess.run([git, "-C", proj, "status", "--porcelain"],
                         capture_output=True, text=True, env=env).stdout
    assert ".rig" not in out, f"`.rig` to ooze in git status:\n{out}"


def test_read_env_secrets_env_over_file_never_db(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".rig").mkdir(parents=True)
    (home / ".rig" / ".env").write_text(
        "# placeholder credentials\nTELEGRAM_TOKEN=fromfile\nexport WEBHOOK='https://example.com/hook'\nNOVAL\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)

    secrets = convention.read_env_secrets()
    assert secrets["TELEGRAM_TOKEN"] == "fromfile"
    assert secrets["WEBHOOK"] == "https://example.com/hook"
    assert "NOVAL" not in secrets

    monkeypatch.setenv("TELEGRAM_TOKEN", "fromenv")
    assert convention.read_env_secrets()["TELEGRAM_TOKEN"] == "fromenv"
    assert convention.get_secret("TELEGRAM_TOKEN") == "fromenv"
    assert convention.get_secret("MISSING", "def") == "def"


def test_secret_never_lands_in_db(tmp_path, monkeypatch):

    home = tmp_path / "home"
    (home / ".rig").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SECRET_TOKEN", "s3cr3t-value")

    proj = str(tmp_path / "proj3")
    os.makedirs(proj)
    conn = dao.open_project(proj)
    sid = dao.create_session(conn, name="run", config={"channel": "ops"})
    dao.log_run(conn, session_id=sid, prompt="hello", result="world")
    conn.close()


    assert convention.get_secret("SECRET_TOKEN") == "s3cr3t-value"

    with open(convention.db_path(proj), "rb") as f:
        blob = f.read()
    assert b"s3cr3t-value" not in blob



def test_dao_roundtrip(tmp_path):
    conn = dao.connect(str(tmp_path / "r.db"))


    sid = dao.create_session(
        conn, name="proj-1", branch="proj-1", tmux="proj-1",
        flow="base", agent="claude", task="do x",
        in_flight=["b1", "b2"], config={"k": "v"},
    )
    assert sid == 1
    assert dao.create_session(conn, name="proj-2") == 2
    s = dao.get_session(conn, sid)
    assert s["name"] == "proj-1" and s["status"] == "active"
    assert s["in_flight"] == ["b1", "b2"] and s["config"] == {"k": "v"}


    dao.update_session(conn, sid, status="done", head="abc", in_flight=["b1"])
    s2 = dao.get_session(conn, sid)
    assert s2["status"] == "done" and s2["head"] == "abc" and s2["in_flight"] == ["b1"]
    with pytest.raises(ValueError):
        dao.update_session(conn, sid, bogus="x")


    rid = dao.log_run(conn, session_id=sid, kind="invoke", agent="worker",
                      adapter="claude", phase="impl", prompt="P", result="R",
                      status="ok", ok=True, meta={"x": 1})
    r = dao.get_run(conn, rid)
    assert r["prompt"] == "P" and r["result"] == "R" and r["ok"] == 1 and r["meta"] == {"x": 1}

    assert dao.log_run(conn, prompt="infra") > 0


    lid = dao.add_lesson(conn, text="always X", kind="principle", session_id=sid, ground_truth=True)
    lid_dup = dao.add_lesson(conn, text="always X", kind="principle", session_id=sid, ground_truth=True)
    assert lid_dup == lid
    assert dao.add_lesson(conn, text="judgement Y", kind="free", status="pending") != lid
    gt = dao.list_lessons(conn, ground_truth=True)
    assert len(gt) == 1 and gt[0]["text"] == "always X"
    assert len(dao.list_lessons(conn, status="pending")) == 1
    dao.set_lesson_status(conn, lid, "retired")
    assert dao.get_lesson(conn, lid)["status"] == "retired"


    mid = dao.upsert_manifest(conn, name="featureA", summary="idx line", feature="A")
    mid2 = dao.upsert_manifest(conn, name="featureA", summary="updated")
    assert mid2 == mid
    assert dao.get_manifest(conn, "featureA")["summary"] == "updated"
    assert [m["name"] for m in dao.list_manifest(conn)] == ["featureA"]


    dao.set_meta(conn, "stack", "python")
    assert dao.get_meta(conn, "stack") == "python"
    assert dao.get_meta(conn, "missing", "d") == "d"
    conn.close()



def test_load_env_into_process_file_into_env_process_wins(tmp_path, monkeypatch):


    envf = tmp_path / ".env"
    envf.write_text("RIG_WORKER_MODEL=m-from-file\nTELEGRAM_BOT_TOKEN=tok-file\n", encoding="utf-8")
    monkeypatch.setattr(convention, "user_env_path", lambda: str(envf))
    monkeypatch.delenv("RIG_WORKER_MODEL", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-env-wins")
    convention.load_env_into_process()
    assert os.environ["RIG_WORKER_MODEL"] == "m-from-file"
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "tok-env-wins"


def test_gt_promotion_refreshes_feature(tmp_path):


    conn = dao.connect(str(tmp_path / "p.db"))

    lid = dao.add_lesson(conn, text="unit u1: gate FAIL — X", kind="principle",
                         feature="", ground_truth=False, status="pending")

    lid2 = dao.add_lesson(conn, text="unit u1: gate FAIL — X", kind="principle",
                          feature="calculator", ground_truth=True, status="active")
    assert lid2 == lid
    row = dao.get_lesson(conn, lid)
    assert row["status"] == "active" and row["ground_truth"] == 1 and row["feature"] == "calculator"
    conn.close()


def test_runs_append_only(tmp_path):
    conn = dao.connect(str(tmp_path / "a.db"))
    sid = dao.create_session(conn, name="s")
    r1 = dao.log_run(conn, session_id=sid, prompt="p1", result="r1")
    r2 = dao.log_run(conn, session_id=sid, prompt="p2", result="r2")
    assert [x["run_id"] for x in dao.list_runs(conn)] == [r1, r2]
    assert [x["run_id"] for x in dao.list_runs(conn, session_id=sid)] == [r1, r2]


    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE runs SET result='hacked' WHERE run_id=?", (r1,))
    with pytest.raises(sqlite3.Error):
        conn.execute("DELETE FROM runs WHERE run_id=?", (r1,))
    assert dao.get_run(conn, r1)["result"] == "r1"


    assert not hasattr(dao, "update_run")
    assert not hasattr(dao, "delete_run")
    conn.close()
