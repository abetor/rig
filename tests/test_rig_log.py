"""Tests for the append-only invocation log and summaries."""







from __future__ import annotations

import sqlite3

import pytest

from rig.db import dao
from rig.memory import log



def test_log_call_writes_full_prompt_and_result(tmp_path):

    conn = dao.connect(str(tmp_path / "l.db"))
    sid = dao.create_session(conn, name="s")

    prompt = "LINE 1\nLINE 2\n  indentation\n" + "x" * 5000
    result = "OUTPUT:\n{\"ok\": true}\n" + "y" * 5000
    rid = log.log_call(
        conn, session_id=sid, agent="worker", adapter="claude", phase="impl",
        prompt=prompt, result=result, status="ok", ok=True, cost=0.12,
        cost_origin="exact", resume_id="thread-1", meta={"k": "v", "n": 3},
    )
    assert isinstance(rid, int) and rid > 0

    row = dao.get_run(conn, rid)

    assert row["prompt"] == prompt
    assert row["result"] == result
    assert row["session_id"] == sid
    assert row["kind"] == "invoke"
    assert row["agent"] == "worker" and row["adapter"] == "claude" and row["phase"] == "impl"
    assert row["ok"] == 1 and row["status"] == "ok"
    assert row["cost"] == 0.12 and row["cost_origin"] == "exact"
    assert row["resume_id"] == "thread-1"
    assert row["meta"] == {"k": "v", "n": 3}
    conn.close()


def test_log_call_sessionless_allowed(tmp_path):

    conn = dao.connect(str(tmp_path / "infra.db"))
    rid = log.log_call(conn, prompt="ping", result="pong")
    row = dao.get_run(conn, rid)
    assert row["session_id"] is None and row["prompt"] == "ping" and row["result"] == "pong"
    conn.close()



def test_summarize_counts_events_errors_and_tail(tmp_path):
    conn = dao.connect(str(tmp_path / "sum.db"))
    sid = dao.create_session(conn, name="s")

    log.log_call(conn, session_id=sid, prompt="p1", result="r1", ok=True)
    log.log_call(conn, session_id=sid, prompt="p2", result="r2", ok=False)
    log.log_call(conn, session_id=sid, prompt="p3", result="r3", ok=True)
    log.log_call(conn, session_id=sid, prompt="p4", result="r4", ok=False)

    dao.log_run(conn, session_id=sid, kind="session_start", prompt="", result="")

    s = log.summarize(conn)
    assert s["events"] == 5
    assert s["by_kind"] == {"invoke": 4, "session_start": 1}
    assert s["errors"] == 2
    assert sum(s["by_kind"].values()) == s["events"]

    assert [r["result"] for r in s["recent_errors"]] == ["r2", "r4"]
    assert all(r["kind"] == "invoke" and r["ok"] == 0 for r in s["recent_errors"])

    assert s["tail"][-1]["kind"] == "session_start"
    conn.close()


def test_summarize_bookkeeping_not_false_positive_error(tmp_path):


    conn = dao.connect(str(tmp_path / "fp.db"))
    sid = dao.create_session(conn, name="s")

    dao.log_run(conn, session_id=sid, kind="session_start")
    dao.log_run(conn, session_id=sid, kind="note")
    dao.log_run(conn, session_id=sid, kind="plan")

    log.log_call(conn, session_id=sid, prompt="p", result="r", ok=True)

    s = log.summarize(conn)
    assert s["events"] == 4
    assert s["errors"] == 0
    assert s["recent_errors"] == []
    conn.close()


def test_summarize_empty_log_is_zero_not_crash(tmp_path):

    conn = dao.connect(str(tmp_path / "empty.db"))
    s = log.summarize(conn)
    assert s == {"events": 0, "by_kind": {}, "errors": 0, "recent_errors": [], "tail": []}
    conn.close()


def test_summarize_tail_is_bounded_and_chronological(tmp_path):
    conn = dao.connect(str(tmp_path / "tail.db"))
    sid = dao.create_session(conn, name="s")
    for i in range(10):
        log.log_call(conn, session_id=sid, prompt=f"p{i}", result=f"r{i}", ok=(i % 2 == 0))

    s = log.summarize(conn, tail=3)
    assert len(s["tail"]) == 3
    assert [r["result"] for r in s["tail"]] == ["r7", "r8", "r9"]
    assert s["events"] == 10
    assert s["errors"] == 5
    assert len(s["recent_errors"]) == 3
    assert [r["result"] for r in s["recent_errors"]] == ["r5", "r7", "r9"]


    s0 = log.summarize(conn, tail=0)
    assert s0["tail"] == [] and s0["recent_errors"] == []
    assert s0["events"] == 10 and s0["errors"] == 5
    conn.close()


def test_summarize_scoped_by_session(tmp_path):

    conn = dao.connect(str(tmp_path / "scope.db"))
    s1 = dao.create_session(conn, name="a")
    s2 = dao.create_session(conn, name="b")
    log.log_call(conn, session_id=s1, prompt="a1", result="r", ok=True)
    log.log_call(conn, session_id=s1, prompt="a2", result="r", ok=False)
    log.log_call(conn, session_id=s2, prompt="b1", result="r", ok=True)

    assert log.summarize(conn, session_id=s1)["events"] == 2
    assert log.summarize(conn, session_id=s1)["errors"] == 1
    assert log.summarize(conn, session_id=s2)["events"] == 1
    assert log.summarize(conn, session_id=s2)["errors"] == 0
    assert log.summarize(conn)["events"] == 3
    conn.close()



def test_log_is_append_only_cannot_overwrite(tmp_path):

    conn = dao.connect(str(tmp_path / "ao.db"))
    sid = dao.create_session(conn, name="s")
    r1 = log.log_call(conn, session_id=sid, prompt="p1", result="r1", ok=True)
    r2 = log.log_call(conn, session_id=sid, prompt="p2", result="r2", ok=False)


    assert [x["run_id"] for x in dao.list_runs(conn)] == [r1, r2]


    assert not hasattr(log, "update_call")
    assert not hasattr(log, "delete_call")
    assert not hasattr(log, "update_run")
    assert not hasattr(log, "delete_run")


    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE runs SET result='hacked' WHERE run_id=?", (r1,))
    with pytest.raises(sqlite3.Error):
        conn.execute("DELETE FROM runs WHERE run_id=?", (r1,))


    assert dao.get_run(conn, r1)["result"] == "r1"
    assert dao.get_run(conn, r1)["prompt"] == "p1"
    conn.close()


def test_summarize_is_read_only(tmp_path):

    conn = dao.connect(str(tmp_path / "ro.db"))
    sid = dao.create_session(conn, name="s")
    log.log_call(conn, session_id=sid, prompt="p", result="r", ok=True)

    before = log.summarize(conn)
    after = log.summarize(conn)
    assert before == after
    assert len(dao.list_runs(conn)) == 1
    conn.close()
