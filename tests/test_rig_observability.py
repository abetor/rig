"""Tests for events, heartbeats, stall detection, and observability hooks."""










import contextlib
import io
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from rig.core import events as ev
from rig.core.liveness import heartbeat
from rig.db import dao


def _git_repo(tmp_path) -> str:
    repo = str(tmp_path / "repo")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@l",
                    "commit", "--allow-empty", "-q", "-m", "init"], check=True)
    return repo


def _age(conn, live_id: int, seconds: int) -> None:

    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    conn.execute("UPDATE liveness SET last_beat_ts=?, started_ts=? WHERE live_id=?",
                 (old, old, live_id))
    conn.commit()




def test_events_log_and_list(tmp_path):
    conn = dao.connect(str(tmp_path / "e.db"))
    dao.log_event(conn, actor="lead", kind="tool_call", name="spawn", status="0", duration_ms=42)
    dao.log_event(conn, kind="gate", name="u1", status="PASS", session_id=None)
    rows = dao.list_events(conn)
    assert [r["kind"] for r in rows] == ["tool_call", "gate"]
    assert rows[0]["duration_ms"] == 42 and rows[0]["actor"] == "lead"
    assert dao.list_events(conn, kind="gate")[0]["name"] == "u1"


def test_events_append_only(tmp_path):
    import sqlite3
    conn = dao.connect(str(tmp_path / "a.db"))
    dao.log_event(conn, kind="boot", name="WARM")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE events SET name='hack'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM events")


def test_emit_fail_soft():
    assert ev.emit(None, kind="x") is None

    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("db dead")

    assert ev.emit(Broken(), kind="x") is None




def test_heartbeat_row_lifecycle_and_beat(tmp_path):
    conn = dao.connect(str(tmp_path / "h.db"))
    with heartbeat(conn, role="worker", phase="u1", adapter="claude", interval=0.05) as b:
        rows = dao.list_liveness(conn)
        assert len(rows) == 1 and rows[0]["role"] == "worker" and rows[0]["live_id"] == b.live_id
        first = rows[0]["last_beat_ts"]


        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            time.sleep(0.2)
            if dao.list_liveness(conn)[0]["last_beat_ts"] > first:
                break
        assert dao.list_liveness(conn)[0]["last_beat_ts"] > first
    assert dao.list_liveness(conn) == []


def test_beat_thread_survives_transient_failure(tmp_path, monkeypatch):

    calls = {"n": 0}
    real = dao.liveness_beat

    def flaky(conn, live_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked (emulation)")
        return real(conn, live_id)

    monkeypatch.setattr(dao, "liveness_beat", flaky)
    conn = dao.connect(str(tmp_path / "f.db"))
    with heartbeat(conn, role="w", interval=0.05):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and calls["n"] < 3:
            time.sleep(0.1)
    assert calls["n"] >= 3


def test_heartbeat_removes_row_on_exception(tmp_path):
    conn = dao.connect(str(tmp_path / "x.db"))
    with pytest.raises(RuntimeError):
        with heartbeat(conn, role="w", interval=0.05):
            raise RuntimeError("boom")
    assert dao.list_liveness(conn) == []


def test_heartbeat_noop_without_conn():
    with heartbeat(None, role="w") as b:
        assert b.live_id is None


def test_liveness_sweep_reaps_with_stall_evidence(tmp_path):


    conn = dao.connect(str(tmp_path / "s.db"))
    lid = dao.liveness_start(conn, role="w", phase="u9")
    _age(conn, lid, dao.LIVENESS_TTL_SECONDS + 100)
    assert dao.list_liveness(conn) == []
    stalls = dao.list_events(conn, kind="stall")
    assert stalls and stalls[0]["name"] == "w/u9" and stalls[0]["params"]["reaped"] is True

    lid2 = dao.liveness_start(conn, role="x")
    dao.mark_liveness_stalled(conn, lid2)
    _age(conn, lid2, dao.LIVENESS_TTL_SECONDS + 100)
    assert dao.list_liveness(conn) == []
    assert len(dao.list_events(conn, kind="stall")) == 1


def test_emit_rolls_back_failed_insert(tmp_path):


    conn = dao.connect(str(tmp_path / "rb.db"))
    assert ev.emit(conn, kind="tool_call", name="handoff", session_id=777) is None
    assert not conn.in_transaction
    other = dao.connect(str(tmp_path / "rb.db"))
    dao.add_board_item(other, text="to live")
    assert dao.list_board(other)


def test_agent_sync_carries_heartbeat(tmp_path):

    from rig.core.launch import agent
    db = str(tmp_path / "l.db")
    conn = dao.connect(db)
    seen = {}

    class Caps:
        schema_output = True
        native_resume = False

    class FakeAdapter:
        name = "fake"

        def capabilities(self):
            return Caps()

        def invoke(self, prompt, **kw):
            side = dao.connect(db)
            seen["rows"] = dao.list_liveness(side)
            side.close()
            from rig.core.result import Result
            return Result(ok=True, text="ok", exit_code=0)

    res = agent(FakeAdapter(), "p", wait="sync", perms="read", cwd=str(tmp_path),
                model="m", conn=conn, session_id=None, role="worker", phase="u1")
    assert res.ok
    assert len(seen["rows"]) == 1 and seen["rows"][0]["role"] == "worker"
    assert dao.list_liveness(conn) == []




def _tool(argv):
    from rig.entry import agent_cli
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = agent_cli.main(argv)
    return code, buf.getvalue()


def test_tool_call_event_emitted(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    _tool(["questions", "--repo", "."])
    conn = dao.open_project(repo)
    evs = dao.list_events(conn, kind="tool_call")
    assert evs and evs[-1]["name"] == "questions" and evs[-1]["status"] == "0"
    assert evs[-1]["duration_ms"] is not None


def test_journal_renders_pulse_and_reports_stall_once(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    monkeypatch.chdir(repo)
    conn = dao.open_project(repo)
    sid = dao.create_session(conn, name="s")
    dao.liveness_start(conn, session_id=sid, role="worker", phase="u1")
    dead = dao.liveness_start(conn, session_id=sid, role="judge", phase="u2")
    _age(conn, dead, dao.STALL_AFTER_SECONDS + 60)
    code, out = _tool(["journal", "--repo", ".", "--session", str(sid)])
    assert "live" in out and "no heartbeat" in out
    assert len(dao.list_events(conn, kind="stall")) == 1
    _tool(["journal", "--repo", ".", "--session", str(sid)])
    assert len(dao.list_events(conn, kind="stall")) == 1


def test_notify_hooks_default_off_and_on(tmp_path, monkeypatch):
    from rig.flow import notify as fnotify
    conn = dao.connect(str(tmp_path / "n.db"))
    sent = []

    def fake_send(channel, text, target=None):
        sent.append((channel, text))
        from types import SimpleNamespace as NS
        return NS(sent=True, skipped=False, reason="")

    monkeypatch.setattr("rig.tools.send", fake_send)
    fnotify.maybe_notify(conn, kind="stall", text="t")
    assert sent == []
    dao.set_meta(conn, fnotify.NOTIFY_HOOKS_META_KEY, "stall,flow_phase")
    dao.set_meta(conn, "notify_channel", "telegram")
    fnotify.maybe_notify(conn, kind="stall", text="heartbeat to lose")
    assert sent == [("telegram", "heartbeat to lose")]
    notifies = dao.list_events(conn, kind="notify")
    assert notifies and notifies[0]["status"] == "sent" and notifies[0]["params"]["hook"] == "stall"
    fnotify.maybe_notify(conn, kind="session", text="x")
    assert len(sent) == 1




def test_phase_executor_emits_flow_phase_event(tmp_path):
    from rig.flow.execute import PhaseExecutor
    from rig.flow.phase import Phase
    from rig.flow.runner import PhaseResult
    conn = dao.connect(str(tmp_path / "p.db"))
    ex = PhaseExecutor(conn=conn, handlers={"trial": lambda ph, ctx: PhaseResult(green=True)})
    res = ex(Phase(name="phase-1", agent="trial"), {"session_id": None})
    assert res.green
    evs = dao.list_events(conn, kind="flow_phase")
    assert evs and evs[0]["name"] == "phase-1" and evs[0]["status"] == "green"

    def boom(ph, ctx):
        raise RuntimeError("crash")

    ex2 = PhaseExecutor(conn=conn, handlers={"trial": boom})
    with pytest.raises(RuntimeError):
        ex2(Phase(name="phase-2", agent="trial"), {})
    crash = dao.list_events(conn, kind="flow_phase")[-1]
    assert crash["name"] == "phase-2" and crash["status"].startswith("crash:")


def test_view_events_command(tmp_path):
    from rig.entry import view
    repo = _git_repo(tmp_path)
    conn = dao.open_project(repo)
    sid = dao.create_session(conn, name="s")
    dao.log_event(conn, kind="tool_call", name="board", session_id=sid, status="0")
    dao.liveness_start(conn, session_id=sid, role="worker", label="sync")
    out = view.execute(view.State(repo), "events")
    assert "tool_call" in out and "board" in out and "in flight" in out
    out_sid = view.execute(view.State(repo), f"events {sid}")
    assert "board" in out_sid
    with pytest.raises(view.ViewError):
        view.execute(view.State(repo), "events rubbish (")
