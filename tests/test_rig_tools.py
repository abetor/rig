"""Tests for agent-facing spawn, memory, and notification tools."""










from __future__ import annotations

import pytest

from rig.core.result import Result
from rig.db import dao
from rig.memory import store
from rig.tools.send import send
from rig.tools.memory import MemoryTool
from rig.tools.spawn import Budget, Spawner, SpawnRefused



class GitRecRunner:



    def __init__(self):
        self.calls = []

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.calls.append({"cmd": cmd, "risk": risk})
        from rig.transport.base import ExecResult
        return ExecResult(0, "", "", 0.0)

    def read_file(self, path):
        return ""

    def git_subcmds(self):
        return [c["cmd"][3] for c in self.calls if c["cmd"][:1] == ["git"] and len(c["cmd"]) > 3]


class FakeHandle:
    def __init__(self):
        self.killed = False
        self.log_path = "/tmp/x.log"

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class FakeAdapter:

    name = "fake"

    def __init__(self, runner, *, cost=0.0):
        self.runner = runner
        self._cost = cost
        self.invoked = []
        self.spawned = []

    def invoke(self, prompt, *, model, cwd, perms, effort, system_context_path,
               resume_session_id, timeout, risk, schema_path, allowed_tools=None, confine=False):
        self.invoked.append({"prompt": prompt, "cwd": cwd})
        return Result(ok=True, text="done", exit_code=0, cost_usd=self._cost)

    def spawn(self, prompt, *, model, cwd, log_path, perms, effort, system_context_path,
              resume_session_id, risk):
        h = FakeHandle()
        h.log_path = log_path
        self.spawned.append({"prompt": prompt, "cwd": cwd, "handle": h})
        return h

    def parse_output(self, stdout, exit_code):
        return Result(ok=(exit_code == 0), text=stdout, exit_code=exit_code)


def _spawner(tmp_path, *, cost=0.0, **kw):
    runner = GitRecRunner()
    ad = FakeAdapter(runner, cost=cost)
    kw.setdefault("model", "m")
    return Spawner(adapter=ad, repo=str(tmp_path), **kw), ad, runner



def test_depth_cap_refuses_over_limit(tmp_path):

    sp, ad, _ = _spawner(tmp_path, depth=2, max_depth=2)
    with pytest.raises(SpawnRefused, match="depth-cap"):
        sp.spawn("task")
    assert ad.invoked == []


def test_depth_cap_allows_within_limit(tmp_path):
    sp, ad, _ = _spawner(tmp_path, depth=0, max_depth=2)
    res = sp.spawn("task")
    assert res.ok and len(ad.invoked) == 1


def test_child_increments_depth_and_caps_subtree(tmp_path):

    sp, _, _ = _spawner(tmp_path, depth=0, max_depth=1)
    child = sp.child()
    assert child.depth == 1 and child.budget is sp.budget
    with pytest.raises(SpawnRefused, match="depth-cap"):
        child.spawn("grandson")


def test_cost_gate_refuses_before_fanout(tmp_path):

    sp, ad, _ = _spawner(tmp_path, budget=Budget(cap=1.0, spent=0.9))
    with pytest.raises(SpawnRefused, match="cost-cap"):
        sp.spawn("dear", est_cost=0.5)
    assert ad.invoked == []


def test_cost_reconciles_reserve_to_actual_on_sync(tmp_path):

    sp, _, _ = _spawner(tmp_path, cost=2.0, budget=Budget(cap=None))
    sp.spawn("x", est_cost=0.5)
    assert sp.budget.spent == pytest.approx(2.0)


def test_cost_reconciles_parallel_batch_to_sum_actual(tmp_path):

    sp, _, _ = _spawner(tmp_path, cost=1.0, budget=Budget(cap=None))
    sp.spawn(["a", "b", "c"], wait="parallel", est_cost=0.5)
    assert sp.budget.spent == pytest.approx(3.0)


def test_cost_reconciles_background_actual_on_collect(tmp_path):

    runner = GitRecRunner()

    class CostAdapter(FakeAdapter):
        def parse_output(self, stdout, exit_code):
            return Result(ok=True, text=stdout, exit_code=exit_code, cost_usd=2.0)

    sp = Spawner(adapter=CostAdapter(runner), repo=str(tmp_path), model="m", budget=Budget(cap=None))
    run = sp.spawn("background", wait="background", est_cost=0.5)
    assert sp.budget.spent == pytest.approx(0.5)
    run.result()
    assert sp.budget.spent == pytest.approx(2.0)


def test_kill_switch_kills_background_subtree(tmp_path):

    sp, ad, _ = _spawner(tmp_path)
    run = sp.spawn("background", wait="background")
    killed = sp.kill_all()
    assert killed == 1 and ad.spawned[0]["handle"].killed is True
    assert run.poll() == 0


def test_gc_removes_worktree_and_branch(tmp_path):

    sp, ad, runner = _spawner(tmp_path)
    res = sp.spawn("in isolation", isolated_worktree=True, unit_id="u1")
    assert res.ok
    assert ad.invoked[0]["cwd"].endswith("/.rig/wt/u1")
    n = sp.gc()
    assert n == 1
    subs = runner.git_subcmds()
    assert "worktree" in subs and "branch" in subs


def test_isolated_worktree_rejects_traversal_id(tmp_path):

    sp, _, _ = _spawner(tmp_path)
    with pytest.raises(ValueError, match="traversal"):
        sp.spawn("evil", isolated_worktree=True, unit_id="../etc")


def test_spawn_refuses_to_clobber_foreign_green_branch(tmp_path):




    conn = _db(tmp_path)
    other = dao.create_session(conn, name="other")
    dao.update_session(conn, other, in_flight=["rig/u1"])
    mine = dao.create_session(conn, name="mine")
    sp, ad, runner = _spawner(tmp_path, conn=conn, session_id=mine)
    with pytest.raises(SpawnRefused, match="unmerged"):
        sp.spawn("task", isolated_worktree=True, unit_id="u1")
    assert ad.invoked == []
    assert "branch" not in runner.git_subcmds()


def test_spawn_supersedes_own_session_branch(tmp_path):


    conn = _db(tmp_path)
    mine = dao.create_session(conn, name="mine")
    dao.update_session(conn, mine, in_flight=["rig/u1"])
    sp, ad, _ = _spawner(tmp_path, conn=conn, session_id=mine)
    res = sp.spawn("task", isolated_worktree=True, unit_id="u1")
    assert res.ok and len(ad.invoked) == 1



def _db(tmp_path):
    return dao.connect(str(tmp_path / "m.db"))


def test_leaf_writes_only_own_session(tmp_path):
    conn = _db(tmp_path)
    sid = dao.create_session(conn, name="leaf")
    mem = MemoryTool(conn, scope=store.LEAF, session_id=sid)
    lid = mem.remember("ones, my, your, his lesson", kind=store.FREE)
    assert dao.get_lesson(conn, lid)["session_id"] == sid


def test_leaf_cannot_write_other_session(tmp_path):

    conn = _db(tmp_path)
    mine = dao.create_session(conn, name="leaf")
    other = dao.create_session(conn, name="other")
    mem = MemoryTool(conn, scope=store.LEAF, session_id=mine)
    with pytest.raises(PermissionError):
        mem.remember("in stranger", target_session_id=other)


def test_orchestrator_writes_any_session(tmp_path):
    conn = _db(tmp_path)
    own = dao.create_session(conn, name="orch")
    other = dao.create_session(conn, name="leaf")
    mem = MemoryTool(conn, scope=store.ORCHESTRATOR, session_id=own)
    lid = mem.remember("in stranger it is possible", target_session_id=other)
    assert dao.get_lesson(conn, lid)["session_id"] == other


def test_memory_feedback_is_active_advisory(tmp_path):
    conn = _db(tmp_path)
    sid = dao.create_session(conn, name="orch")
    mem = MemoryTool(conn, scope=store.ORCHESTRATOR, session_id=sid)
    lid = mem.feedback("human tale so")
    row = dao.get_lesson(conn, lid)
    assert row["kind"] == store.FEEDBACK and row["status"] == "active" and row["ground_truth"] == 0


def test_remember_is_always_pending_never_rule(tmp_path):


    conn = _db(tmp_path)
    sid = dao.create_session(conn, name="leaf")
    mem = MemoryTool(conn, scope=store.LEAF, session_id=sid)
    lid = mem.remember("approach X to work", kind=store.PRINCIPLE)
    row = dao.get_lesson(conn, lid)
    assert row["status"] == "pending" and row["ground_truth"] == 0
    import inspect
    assert "ground_truth" not in inspect.signature(mem.remember).parameters


def test_memory_recall_returns_blocks(tmp_path):

    conn = _db(tmp_path)
    sid = dao.create_session(conn, name="orch")

    store.write_lesson(conn, text="always verify bringing in(to)", scope=store.ORCHESTRATOR,
                       session_id=sid, kind=store.PRINCIPLE, ground_truth=True)
    store.write_lesson(conn, text="feature login capricious", scope=store.ORCHESTRATOR,
                       session_id=sid, kind=store.FEATURE, feature="login", ground_truth=True)
    mem = MemoryTool(conn, scope=store.ORCHESTRATOR, session_id=sid)
    rec = mem.recall("login bringing in(to)")
    assert any("verify bringing in(to)" in l["text"] for l in rec.always)
    assert any(l["feature"] == "login" for l in rec.selected)


def test_memory_unknown_scope_raises(tmp_path):
    with pytest.raises(ValueError, match="scope"):
        MemoryTool(_db(tmp_path), scope="god")



def test_send_skips_when_no_cred(tmp_path):

    r = send("telegram", "greeting(s)", env={})
    assert r.skipped is True and r.sent is False and "not he is in optimistic mood" in r.reason


def test_send_delivers_when_cred_present():
    sent = {}

    def deliver(text, target, creds, payload=None):
        sent["text"] = text
        sent["token"] = creds["TELEGRAM_BOT_TOKEN"]
        return True

    r = send("telegram", "complete", target="42",
                  env={"TELEGRAM_BOT_TOKEN": "T"}, deliver=deliver)
    assert r.sent is True and r.skipped is False
    assert sent["text"] == "complete" and sent["token"] == "T"


def test_send_delivery_failure_is_structural():
    r = send("telegram", "x", env={"TELEGRAM_BOT_TOKEN": "T"},
                  deliver=lambda t, tg, c, p=None: False)
    assert r.sent is False and r.skipped is False and "failure" in r.reason


def test_send_delivery_exception_not_raised():

    def boom(text, target, creds, payload=None):
        raise RuntimeError("to depend")
    r = send("telegram", "x", env={"TELEGRAM_BOT_TOKEN": "T"}, deliver=boom)
    assert r.sent is False and "weak" in r.reason


def test_send_unknown_channel_raises():
    with pytest.raises(ValueError, match="unknown channel"):
        send("carrier-pigeon", "x", env={})


def test_send_empty_text_raises():
    with pytest.raises(ValueError, match="empty text"):
        send("telegram", "   ", env={"TELEGRAM_BOT_TOKEN": "T"})
