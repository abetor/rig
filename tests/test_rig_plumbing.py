"""Integration tests for persistence, prompt assembly, and cross-phase state propagation."""










from __future__ import annotations

import json

import pytest

from rig.core.result import Result
from rig.db import dao
from rig.flow import resolve, run_flow
from rig.flow.execute import PhaseExecutor
from rig.tools.spawn import Spawner
from rig.transport.base import ExecResult


class _Caps:
    schema_output = True
    effort = True
    native_resume = True
    agents_md_native = True


class GitRunner:

    def __init__(self, *, on_disk=(), gate_exit=0):
        self.on_disk = list(on_disk)
        self.gate_exit = gate_exit
        self.calls = []

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.calls.append({"cmd": cmd, "cwd": cwd})
        if "branch" in cmd and "--list" in cmd:
            return ExecResult(0, "\n".join(self.on_disk), "", 0.0)
        if cmd[:1] == ["git"]:
            if "diff" in cmd:
                return ExecResult(0, "DIFF-MARKER-12345", "", 0.0)
            return ExecResult(0, "", "", 0.0)
        return ExecResult(self.gate_exit, "gate-out", "", 0.1)

    def write_file(self, path, content):
        pass

    def read_file(self, path):
        return "{}"


class RecAdapter:

    name = "rec"

    def __init__(self, runner, rec, *, plan_json="", worker_fail_on=None, judge_reject=False):
        self.runner = runner
        self.rec = rec
        self._plan = plan_json
        self._fail = worker_fail_on
        self._judge_reject = judge_reject

    def inject_context(self, method_text, cwd):
        return f"{cwd}/.rig/system-prompt.md"

    def invoke(self, prompt, *, model, cwd, perms, effort, system_context_path,
               resume_session_id, timeout, risk, schema_path, allowed_tools=None, confine=False):
        role = self._role(prompt)
        self.rec.append({"role": role, "prompt": prompt, "perms": perms, "schema": schema_path})
        if role == "scout":
            return Result(ok=True, text="SCOUT-CONTEXT: file core.py; gate pytest; risk race conditions",
                          exit_code=0, cost_usd=0.01)
        if role == "plan":
            return Result(ok=True, text=self._plan, exit_code=0, cost_usd=0.02)
        if role == "critic":
            return Result(ok=True, text='{"sound": true, "issues": [], "why": "ok"}', exit_code=0, cost_usd=0.005)
        if role == "judge":
            v = '{"ok": false, "why": "bad code"}' if self._judge_reject else '{"ok": true, "why": "ok"}'
            return Result(ok=True, text=v, exit_code=0,
                          cost_usd=0.004, schema_applied=(True if schema_path else None))
        if self._fail and self._fail in prompt:
            return Result(ok=False, text="", exit_code=1, error="worker fail", cost_usd=0.0)
        return Result(ok=True, text="DIFF", exit_code=0, cost_usd=0.03)

    @staticmethod
    def _role(p):
        if "SCOUT (read-only)" in p[:160]:
            return "scout"
        if "PLANNER" in p:
            return "plan"
        if "PLAN CRITIC" in p.upper():
            return "critic"
        if "reviewer for dimension" in p:
            return "judge"
        return "worker"

    def capabilities(self):
        return _Caps()


def _build(*, units, on_disk, gate_exit=0, worker_fail_on=None, retry_cap=1, conn=None,
           session_id=None, no_gate=False, judge_reject=False):

    rec = []
    git = GitRunner(on_disk=on_disk, gate_exit=gate_exit)
    plan_json = json.dumps({"goal": "feature X", "gate_cmd": ([] if no_gate else ["pytest", "-q"]),
                            "units": [{"id": u, "instruction": f"implement unit {u}"} for u in units]})
    adapter = RecAdapter(git, rec, plan_json=plan_json, worker_fail_on=worker_fail_on,
                         judge_reject=judge_reject)
    spawner = Spawner(adapter=adapter, repo="/repo", model="m", conn=conn, session_id=session_id, max_depth=4)
    ex = PhaseExecutor(
        spawner=spawner, judge_adapter=adapter, judge_model="jm",
        planner_adapter=adapter, planner_model="pm",
        runner=git, repo="/repo", cwd="/repo", conn=conn,
        dimensions=("correctness", "security"),
        judge_schema="/repo/.rig/verdict.json", unit_retry_cap=retry_cap)
    return ex, rec


def _workers(rec):
    return [r for r in rec if r["role"] == "worker"]


def _first(rec, role):
    return next(r["prompt"] for r in rec if r["role"] == role)



def test_every_phase_logs_a_run_with_role_and_cost():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s", task="X")
    ex, _ = _build(units=["u1"], on_disk=["rig/u1"], conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex,
             ctx={"task": "implement X", "session_id": sid}, base="HEAD")
    runs = dao.list_runs(conn, session_id=sid)
    roles = [r["agent"] for r in runs]

    for expect in ("scout", "planner", "critic", "worker", "judge"):
        assert expect in roles, f"no run for role {expect}: {roles}"
    assert all(r["kind"] == "invoke" for r in runs)
    assert any((r["cost"] or 0) > 0 for r in runs)

    phases = {r["phase"] for r in runs}
    assert {"scout", "plan", "critic", "review"} <= phases


def test_run_logs_full_prompt_and_result():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s", task="X")
    ex, _ = _build(units=["u1"], on_disk=["rig/u1"], conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex,
             ctx={"task": "unique-task-7788", "session_id": sid}, base="HEAD")
    runs = dao.list_runs(conn, session_id=sid)
    scout = next(r for r in runs if r["agent"] == "scout")
    assert "unique-task-7788" in scout["prompt"]
    assert "SCOUT-CONTEXT" in scout["result"]


def test_gate_fail_writes_ground_truth_lesson_active():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s", task="X")
    ex, _ = _build(units=["u1"], on_disk=["rig/u1"], gate_exit=1, retry_cap=0, conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex,
             ctx={"task": "X", "session_id": sid}, base="HEAD")
    gt = dao.list_lessons(conn, status="active", ground_truth=True)
    assert gt, "gate-FAIL not recorded ground truth lesson"
    assert any("u1" in l["text"] and "FAIL" in l["text"] for l in gt)


def test_no_gate_writes_pending_not_ground_truth():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s", task="X")
    ex, _ = _build(units=["u1"], on_disk=["rig/u1"], no_gate=True, retry_cap=0, conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex,
             ctx={"task": "X", "session_id": sid}, base="HEAD")
    pending = dao.list_lessons(conn, status="pending")
    assert any("did not run" in l["text"].lower() for l in pending)
    assert not dao.list_lessons(conn, status="active", ground_truth=True)


def test_judge_reject_writes_pending_not_ground_truth():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s", task="X")
    ex, _ = _build(units=["u1"], on_disk=["rig/u1"], gate_exit=0, judge_reject=True,
                   retry_cap=0, conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex,
             ctx={"task": "X", "session_id": sid}, base="HEAD")
    pending = dao.list_lessons(conn, status="pending")
    assert any("deviation" in l["text"].lower() for l in pending)
    assert not dao.list_lessons(conn, status="active", ground_truth=True)


def test_delegate_fail_writes_pending_not_active():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s", task="X")
    ex, _ = _build(units=["u1"], on_disk=[], worker_fail_on="unit u1", retry_cap=0, conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex,
             ctx={"task": "X", "session_id": sid}, base="HEAD")
    pending = dao.list_lessons(conn, status="pending")
    active_gt = dao.list_lessons(conn, status="active", ground_truth=True)
    assert any("delegate" in l["text"].lower() for l in pending)
    assert not active_gt



def test_scout_prompt_carries_task():
    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s")
    ex, rec = _build(units=["u1"], on_disk=["rig/u1"], conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex,
             ctx={"task": "TASK-ALPHA", "session_id": sid}, base="HEAD")
    assert "TASK-ALPHA" in _first(rec, "scout")


def test_plan_prompt_carries_scout_context():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s")
    ex, rec = _build(units=["u1"], on_disk=["rig/u1"], conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex, ctx={"task": "X", "session_id": sid}, base="HEAD")
    assert "SCOUT-CONTEXT" in _first(rec, "plan")


def test_critic_prompt_carries_units():
    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s")
    ex, rec = _build(units=["u1", "u2"], on_disk=[], conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex, ctx={"task": "X", "session_id": sid}, base="HEAD")
    cp = _first(rec, "critic")
    assert "feature X" in cp and "implement unit u1" in cp


def test_worker_brief_carries_goal_gate_and_scout_context():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s")
    ex, rec = _build(units=["u1"], on_disk=["rig/u1"], conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex, ctx={"task": "X", "session_id": sid}, base="HEAD")
    wb = _workers(rec)[0]["prompt"]
    assert "feature X" in wb
    assert "pytest" in wb
    assert "SCOUT-CONTEXT" in wb


def test_worker_brief_includes_recalled_memory():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s")
    from rig.memory import store
    store.write_lesson(conn, text="unit u1 always to obtain mutex", scope=store.ORCHESTRATOR,
                       session_id=sid, kind=store.PRINCIPLE, ground_truth=True)
    ex, rec = _build(units=["u1"], on_disk=["rig/u1"], conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex, ctx={"task": "X", "session_id": sid}, base="HEAD")
    wb = _workers(rec)[0]["prompt"]
    assert "Project memory" in wb and "mutex" in wb


def test_judge_prompt_carries_diff_and_instruction():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s")
    ex, rec = _build(units=["u1"], on_disk=["rig/u1"], conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex, ctx={"task": "X", "session_id": sid}, base="HEAD")
    jp = _first(rec, "judge")
    assert "DIFF-MARKER-12345" in jp
    assert "implement unit u1" in jp


def test_retry_brief_carries_fix_feedback():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s")
    ex, rec = _build(units=["u1"], on_disk=["rig/u1"], gate_exit=1, retry_cap=1, conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex, ctx={"task": "X", "session_id": sid}, base="HEAD")
    workers = _workers(rec)
    assert len(workers) == 2
    assert "Fix review feedback" not in workers[0]["prompt"]
    assert "Fix review feedback" in workers[1]["prompt"]



def test_every_resolved_phase_fires_in_order():


    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s", task="X")
    ex, _ = _build(units=["u1"], on_disk=["rig/u1"], conn=conn, session_id=sid)
    phases = resolve.resolve_phases("lead/dev-feature")
    fres = run_flow(phases, ex, ctx={"task": "X", "session_id": sid}, base="HEAD")
    assert [r["name"] for r in fres.results] == [p.name for p in phases]
    assert all(r["green"] for r in fres.results)
    assert not fres.halted and fres.ok


def test_prior_green_threads_into_second_unit_brief():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s")
    ex, rec = _build(units=["u1", "u2"], on_disk=["rig/u1", "rig/u2"], conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex, ctx={"task": "X", "session_id": sid}, base="HEAD")
    workers = _workers(rec)
    assert len(workers) == 2
    assert "rig/u1" in workers[1]["prompt"]
    assert "implement unit u1" in workers[1]["prompt"]


def test_first_unit_brief_says_first_not_lying():

    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s")
    ex, rec = _build(units=["u1"], on_disk=["rig/u1"], conn=conn, session_id=sid)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex, ctx={"task": "X", "session_id": sid}, base="HEAD")
    wb = _workers(rec)[0]["prompt"]
    assert "FIRST unit" in wb
    assert "Your worktree start on branch" not in wb
    assert "No earlier unit was accepted" not in wb
