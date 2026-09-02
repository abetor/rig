"""End-to-end tests for the data-defined developer feature flow."""







from __future__ import annotations

import json

from rig.core.result import Result
from rig.core.types import Plan, Unit
from rig.flow import resolve, run_flow
from rig.flow.execute import PhaseExecutor
from rig.tools.spawn import Spawner
from rig.transport.base import ExecResult



class GitEcho:

    def __init__(self):
        self.calls = []

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.calls.append(cmd)
        return ExecResult(0, "", "", 0.0)

    def write_file(self, path, content):
        pass

    def worktree_add_bases(self):

        return [c[-1] for c in self.calls if "worktree" in c and "add" in c]


class ProjectRunner:

    def __init__(self, *, on_disk, gate_exit=0):
        self.on_disk = on_disk
        self.gate_exit = gate_exit
        self.calls = []

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.calls.append({"cmd": cmd, "cwd": cwd})
        if "branch" in cmd and "--list" in cmd:
            return ExecResult(0, "\n".join(self.on_disk), "", 0.0)
        if cmd[:1] == ["git"]:
            return ExecResult(0, "", "", 0.0)
        return ExecResult(self.gate_exit, "gate-output", "", 0.1)

    def write_file(self, path, content):
        pass

    def gate_cwds(self):

        return [c["cwd"] for c in self.calls if c["cmd"][:1] != ["git"]]


class _Caps:
    schema_output = True
    effort = True
    native_resume = True
    agents_md_native = True


class WorkerAdapter:
    name = "worker"

    def __init__(self, runner, *, fail_on=None):
        self.runner = runner
        self._fail_on = fail_on

    def invoke(self, prompt, *, model, cwd, perms, effort, system_context_path,
               resume_session_id, timeout, risk, schema_path, allowed_tools=None, confine=False):
        if self._fail_on and self._fail_on in prompt:
            return Result(ok=False, text="", exit_code=1, error="worker fail", cost_usd=0.0)
        return Result(ok=True, text="DIFF", exit_code=0, cost_usd=0.0)

    def inject_context(self, method_text, cwd):
        return f"{cwd}/.rig/system-prompt.md"

    def capabilities(self):
        return _Caps()


class PlannerAdapter:

    name = "planner"

    def __init__(self, runner, plan_json):
        self.runner = runner
        self._plan = plan_json

    def invoke(self, prompt, *, model, cwd, perms, effort, system_context_path,
               resume_session_id, timeout, risk, schema_path, allowed_tools=None, confine=False):
        if "SCOUT (read-only)" in prompt[:160]:
            return Result(ok=True, text="repository on python; gate pytest -q; convention PEP8", exit_code=0)
        return Result(ok=True, text=self._plan, exit_code=0)

    def capabilities(self):
        return _Caps()


class JudgeAdapter:

    name = "judge"

    def __init__(self, runner):
        self.runner = runner

    def invoke(self, prompt, *, model, cwd, perms, effort, system_context_path,
               resume_session_id, timeout, risk, schema_path, allowed_tools=None, confine=False):
        applied = True if schema_path else None
        if "PLAN CRITIC" in prompt.upper():
            return Result(ok=True, text='{"sound": true, "issues": [], "why": "health care"}',
                          exit_code=0, schema_applied=applied)
        return Result(ok=True, text='{"ok": true, "why": "ok"}', exit_code=0, schema_applied=applied)

    def capabilities(self):
        return _Caps()


def _executor(*, units, on_disk, gate_exit=0):
    git = GitEcho()
    plan_json = json.dumps({"goal": "feature X", "gate_cmd": ["pytest", "-q"],
                            "units": [{"id": u, "instruction": f"implement {u}"} for u in units]})
    spawner = Spawner(adapter=WorkerAdapter(git), repo="/repo", model="m", max_depth=4)
    proj = ProjectRunner(on_disk=on_disk, gate_exit=gate_exit)
    ex = PhaseExecutor(
        spawner=spawner, judge_adapter=JudgeAdapter(git), judge_model="jm",
        planner_adapter=PlannerAdapter(git, plan_json), planner_model="pm",
        runner=proj, repo="/repo", cwd="/repo",
        dimensions=("correctness", "security"),
        judge_schema="/repo/.rig/verdict.json", plan_schema="/repo/.rig/plan.json")
    ex._test_git = git
    ex._test_proj = proj
    return ex



def test_devfeature_flow_resolves_and_validates():
    phases = resolve.resolve_phases("lead/dev-feature")
    assert [p.name for p in phases] == ["scout", "plan", "critic", "notify-plan",
                                        "checkpoint", "units", "reconcile", "notify-done"]
    assert phases[1].on_fail == "stop"
    assert next(p for p in phases if p.name == "units").advances_base is True

    notifies = [p for p in phases if p.agent == "notify"]
    assert len(notifies) == 2 and all(p.on_fail == "continue" for p in notifies)


def test_base_flow_is_minimal_default():
    phases = resolve.resolve_phases()
    assert [p.name for p in phases] == ["plan", "units", "reconcile", "notify"]


def test_scan_project_flow_is_recon_only():
    phases = resolve.resolve_phases("system/scan-project")
    assert [p.name for p in phases] == ["scout"] and phases[0].kind == "readonly"



def test_units_gc_removes_worktrees_keeps_branches():


    ex = _executor(units=["u1", "u2"], on_disk=["rig/u1", "rig/u2"], gate_exit=0)
    plan = Plan(goal="g", units=[Unit(id="u1", instruction="a"), Unit(id="u2", instruction="b")],
                gate_cmd=["pytest", "-q"])
    ex(resolve.Phase(name="units", agent="units", advances_base=True), {"plan": plan, "task": "x"})
    cmds = [c["cmd"] for c in ex._test_proj.calls]
    removes = [c for c in cmds if "worktree" in c and "remove" in c]
    assert len(removes) >= 2
    assert not any(("branch" in c and "-D" in c) for c in cmds)



def test_devfeature_chain_multiunit_green():


    ex = _executor(units=["u1", "u2"], on_disk=["rig/u1", "rig/u2"], gate_exit=0)
    phases = resolve.resolve_phases("lead/dev-feature")
    res = run_flow(phases, ex, ctx={"task": "implement feature X"}, base="HEAD")
    assert res.ok is True
    units_res = next(r for r in res.results if r["name"] == "units")
    assert units_res["green"] and "2/2 green" in units_res["detail"]
    recon = next(r for r in res.results if r["name"] == "reconcile")
    assert recon["green"]


def test_gate_runs_in_unit_worktree_not_repo():

    ex = _executor(units=["u1"], on_disk=["rig/u1"], gate_exit=0)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex, ctx={"task": "X"}, base="HEAD")
    cwds = ex._test_proj.gate_cwds()
    assert cwds and all(c.endswith("/.rig/wt/u1") for c in cwds)


def test_multiunit_stacks_base_green_only():

    ex = _executor(units=["u1", "u2"], on_disk=["rig/u1", "rig/u2"], gate_exit=0)
    run_flow(resolve.resolve_phases("lead/dev-feature"), ex, ctx={"task": "X"}, base="HEAD")
    bases = ex._test_git.worktree_add_bases()
    assert "HEAD" in bases and "rig/u1" in bases


def test_devfeature_gate_fail_unit_not_green_reconcile_flags():

    ex = _executor(units=["u1"], on_disk=["rig/u1"], gate_exit=1)
    phases = resolve.resolve_phases("lead/dev-feature")
    res = run_flow(phases, ex, ctx={"task": "X"}, base="HEAD")
    units_res = next(r for r in res.results if r["name"] == "units")
    recon = next(r for r in res.results if r["name"] == "reconcile")
    assert units_res["green"] is False
    assert recon["green"] is False
    assert res.ok is False


def test_reconcile_sees_greens_on_partial_success():


    git = GitEcho()
    plan_json = json.dumps({"goal": "g", "gate_cmd": ["pytest", "-q"],
                            "units": [{"id": "u1", "instruction": "implement u1"},
                                      {"id": "u2", "instruction": "implement u2"}]})
    proj = ProjectRunner(on_disk=["rig/u1", "rig/u2"], gate_exit=0)
    ex = PhaseExecutor(
        spawner=Spawner(adapter=WorkerAdapter(git, fail_on="implement u2"), repo="/repo", model="m", max_depth=4),
        judge_adapter=JudgeAdapter(git), judge_model="jm",
        planner_adapter=PlannerAdapter(git, plan_json), planner_model="pm",
        runner=proj, repo="/repo", cwd="/repo", unit_retry_cap=0,
        judge_schema="/s", plan_schema="/s")
    res = run_flow(resolve.resolve_phases("lead/dev-feature"), ex, ctx={"task": "X"}, base="HEAD")
    recon = next(r for r in res.results if r["name"] == "reconcile")
    rep = next(r["detail"] for r in res.results if r["name"] == "reconcile")
    assert recon["green"] is False
    assert "u1 [done]" in rep and "u2 [attempted]" in rep


def test_devfeature_plan_fail_halts_before_units():

    git = GitEcho()
    ex = PhaseExecutor(
        spawner=Spawner(adapter=WorkerAdapter(git), repo="/repo", model="m"),
        judge_adapter=JudgeAdapter(git), judge_model="jm",
        planner_adapter=PlannerAdapter(git, "not-json-garbage ("), planner_model="pm",
        runner=ProjectRunner(on_disk=[]), repo="/repo", cwd="/repo",
        judge_schema="/s", plan_schema="/s")
    phases = resolve.resolve_phases("lead/dev-feature")
    res = run_flow(phases, ex, ctx={"task": "X"}, base="HEAD")
    assert res.halted is True
    assert not any(r["name"] == "units" for r in res.results)
