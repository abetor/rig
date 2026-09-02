"""End-to-end CLI lifecycle tests with real Git and deterministic fake agents."""





from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from rig.core.result import Result
from rig.db import dao
from rig.entry import cli
from rig.flow.execute import PhaseExecutor
from rig.memory import WARM, boot
from rig.tools.spawn import Spawner
from rig.transport.local import LocalRunner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="is required git in PATH")


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=False)


def _seed(tmp_path) -> str:
    repo = str(tmp_path / "proj")
    os.makedirs(repo)
    with open(os.path.join(repo, ".gitignore"), "w") as fh:
        fh.write(".rig/\n__pycache__/\n*.pyc\n.pytest_cache/\n")
    _git(repo, "init", "-q"); _git(repo, "config", "user.email", "s@x"); _git(repo, "config", "user.name", "s")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "seed")

    os.makedirs(os.path.join(repo, ".rig"), exist_ok=True)
    with open(os.path.join(repo, ".rig", "gate.json"), "w") as fh:
        fh.write('["python3", "-m", "pytest", "-q"]')
    return repo


class _Caps:
    schema_output = True; effort = True; native_resume = True; agents_md_native = True


class ScriptedWorker:
    name = "worker"

    def __init__(self, runner):
        self.runner = runner

    def invoke(self, prompt, *, model, cwd, perms, effort, system_context_path,
               resume_session_id, timeout, risk, schema_path, allowed_tools=None, confine=False):
        task = prompt.split("YOUR TASK", 1)[-1]
        if "area" in task:
            with open(os.path.join(cwd, "geometry.py"), "a") as fh:
                fh.write("def area(r):\n    return 3.14159 * r * r\n")
            with open(os.path.join(cwd, "test_geometry.py"), "a") as fh:
                fh.write("from geometry import area\n\ndef test_area():\n    assert abs(area(2)-12.56636)<1e-4\n")
        return Result(ok=True, text="complete", exit_code=0, cost_usd=0.0)

    def inject_context(self, method_text, cwd):
        return f"{cwd}/.rig/system-prompt.md"

    def capabilities(self):
        return _Caps()


class Planner:
    name = "planner"

    def __init__(self, runner):
        self.runner = runner

    def invoke(self, prompt, *, model, cwd, perms, effort, system_context_path,
               resume_session_id, timeout, risk, schema_path, allowed_tools=None, confine=False):
        if "SCOUT (read-only)" in prompt[:160]:
            return Result(ok=True, text="python project; gate pytest", exit_code=0)
        plan = {"goal": "geometry area", "gate_cmd": ["python3", "-m", "pytest", "-q"],
                "units": [{"id": "u1_geo", "instruction": "Creation geometry.py with area and test — area."}]}
        return Result(ok=True, text=json.dumps(plan), exit_code=0)

    def capabilities(self):
        return _Caps()


class Judge:
    name = "judge"

    def __init__(self, runner):
        self.runner = runner

    def invoke(self, prompt, *, model, cwd, perms, effort, system_context_path,
               resume_session_id, timeout, risk, schema_path, allowed_tools=None, confine=False):
        applied = True if schema_path else None
        if "PLAN CRITIC" in prompt.upper():
            return Result(ok=True, text='{"sound": true, "issues": [], "why": "ok"}', exit_code=0, schema_applied=applied)
        return Result(ok=True, text='{"ok": true, "why": "ok"}', exit_code=0, schema_applied=applied)

    def capabilities(self):
        return _Caps()


def _executor(repo, conn, sid):
    runner = LocalRunner()
    spawner = Spawner(adapter=ScriptedWorker(runner), repo=repo, model="m", conn=conn, session_id=sid, max_depth=4)
    return PhaseExecutor(spawner=spawner, judge_adapter=Judge(runner), judge_model="jm",
                         planner_adapter=Planner(runner), planner_model="pm",
                         runner=runner, repo=repo, conn=conn, cwd=repo, dimensions=("correctness",),
                         judge_schema=os.path.join(repo, ".rig", "verdict.json"))


def test_lifecycle_checkpoint_then_approve_records_state(tmp_path):
    repo = _seed(tmp_path)
    runner = LocalRunner()
    conn = dao.connect(os.path.join(repo, ".rig", "m.db"))
    ex = _executor(repo, conn, None)


    res = cli.cmd_new(conn, runner, repo, ex, task="to count square", flow="lead/dev-feature")
    assert res["mode"] == "checkpoint"
    assert res["plan"] is not None and res["plan"].units[0].id == "u1_geo"
    sid = res["session"].session_id

    assert "rig/u1_geo" not in subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert not os.path.exists(os.path.join(repo, "geometry.py"))


    ap = cli.cmd_approve(conn, runner, repo, _executor(repo, conn, sid), sid)
    assert ap["ok"] is True
    rec = ap["record"]


    sb = "rig/to-count-square"
    assert rec["status"] == "done" and rec["in_flight"] == [sb]
    assert "def area" in subprocess.run(["git", "show", f"{sb}:geometry.py"], cwd=repo,
                                        capture_output=True, text=True).stdout
    branches = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert "rig/u1_geo" not in branches
    assert "Session branch awaiting human merge" in rec["handoff"]


    s = dao.get_session(conn, sid)
    assert s["status"] == "done" and s["in_flight"] == ["rig/to-count-square"] and s["head"]
    notes = [r for r in dao.list_runs(conn, session_id=sid) if r["kind"] == "note"]
    assert notes and "Sum" in notes[-1]["result"]


class _RecWorker(ScriptedWorker):

    def __init__(self, runner, rec):
        super().__init__(runner)
        self.rec = rec

    def invoke(self, prompt, **kw):
        self.rec.append(prompt)
        return super().invoke(prompt, **kw)


class _ScoutPlanner(Planner):

    def invoke(self, prompt, **kw):
        if "SCOUT (read-only)" in prompt[:160]:
            return Result(ok=True, text="SCOUT-MARK-9931 python project; gate pytest", exit_code=0)
        return super().invoke(prompt, **kw)


def test_scout_context_survives_checkpoint_process_boundary(tmp_path):



    repo = _seed(tmp_path)
    runner = LocalRunner()
    conn = dao.connect(os.path.join(repo, ".rig", "m.db"))
    briefs = []

    def _ex(sid):
        r = LocalRunner()
        sp = Spawner(adapter=_RecWorker(r, briefs), repo=repo, model="m", conn=conn, session_id=sid, max_depth=4)
        return PhaseExecutor(spawner=sp, judge_adapter=Judge(r), judge_model="jm",
                             planner_adapter=_ScoutPlanner(r), planner_model="pm",
                             runner=r, repo=repo, conn=conn, cwd=repo, dimensions=("correctness",),
                             judge_schema=os.path.join(repo, ".rig", "verdict.json"))

    res = cli.cmd_new(conn, runner, repo, _ex(None), task="to count area square", flow="lead/dev-feature")
    sid = res["session"].session_id
    assert res["mode"] == "checkpoint" and not briefs

    assert "SCOUT-MARK-9931" in cli._load_plan(conn, sid).context

    cli.cmd_approve(conn, runner, repo, _ex(sid), sid)
    assert briefs, "worker not recipient brief after approve"
    assert any("SCOUT-MARK-9931" in b for b in briefs), "scout-context TO LOSE on frontier process"


def test_base_flow_is_autonomous_no_checkpoint(tmp_path):

    repo = _seed(tmp_path)
    runner = LocalRunner()
    conn = dao.connect(os.path.join(repo, ".rig", "m.db"))
    res = cli.cmd_new(conn, runner, repo, _executor(repo, conn, None), task="square", flow="base")
    assert res["mode"] == "autonomous" and res["result"].ok
    assert res["record"]["in_flight"] == ["rig/square"]
    assert res["result"].ctx.get("repo") == repo


def test_record_keeps_green_branch_on_partial_units(tmp_path):


    repo = _seed(tmp_path)
    runner = LocalRunner()
    conn = dao.connect(os.path.join(repo, ".rig", "m.db"))

    class TwoUnitPlanner(Planner):
        def invoke(self, prompt, **kw):
            if "SCOUT (read-only)" in prompt[:160]:
                return Result(ok=True, text="x", exit_code=0)
            plan = {"goal": "two", "gate_cmd": ["python3", "-m", "pytest", "-q"],
                    "units": [{"id": "u1_geo", "instruction": "Creation geometry.py with area and test — area."},
                              {"id": "u2_bad", "instruction": "demolition — FAILME"}]}
            return Result(ok=True, text=json.dumps(plan), exit_code=0)

    class FailingWorker(ScriptedWorker):
        def invoke(self, prompt, **kw):
            if "FAILME" in prompt:
                return Result(ok=False, text="", exit_code=1, error="fail")
            return super().invoke(prompt, **kw)

    rn = LocalRunner()
    spawner = Spawner(adapter=FailingWorker(rn), repo=repo, model="m", conn=conn, session_id=None, max_depth=4)
    ex = PhaseExecutor(spawner=spawner, judge_adapter=Judge(rn), judge_model="jm",
                       planner_adapter=TwoUnitPlanner(rn), planner_model="pm",
                       runner=rn, repo=repo, conn=conn, cwd=repo, dimensions=("correctness",),
                       judge_schema=os.path.join(repo, ".rig", "verdict.json"))
    res = cli.cmd_new(conn, runner, repo, ex, task="two task", flow="base")
    assert res["mode"] == "autonomous" and res["result"].ok is False

    assert res["record"]["in_flight"] == sorted(["rig/u1_geo", "rig/two-task"])
    assert "rig/two-task" in res["record"]["handoff"]
    branches = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert "rig/u1_geo" in branches


def test_approve_without_plan_fails_gracefully(tmp_path):
    repo = _seed(tmp_path)
    runner = LocalRunner()
    conn = dao.connect(os.path.join(repo, ".rig", "m.db"))
    sid = dao.create_session(conn, name="x", flow="lead/dev-feature")
    res = cli.cmd_approve(conn, runner, repo, _executor(repo, conn, sid), sid)
    assert res["ok"] is False and "no stored plan" in res["detail"]


def test_boot_warm_after_recorded_session(tmp_path):

    repo = _seed(tmp_path)
    runner = LocalRunner()
    conn = dao.connect(os.path.join(repo, ".rig", "m.db"))
    res = cli.cmd_new(conn, runner, repo, _executor(repo, conn, None), task="square", flow="base")
    sid = res["session"].session_id
    b = boot(runner, conn, repo)
    assert b.mode == WARM


def test_autonomous_flow_attributes_worker_runs_to_session(tmp_path):




    repo = _seed(tmp_path)
    conn = dao.connect(os.path.join(repo, ".rig", "m.db"))
    res = cli.cmd_new(conn, LocalRunner(), repo, _executor(repo, conn, None), task="square", flow="base")
    sid = res["session"].session_id
    workers = [r for r in dao.list_runs(conn, session_id=sid)
               if r["kind"] == "invoke" and r["agent"] == "worker"]
    assert workers, "worker not fastened to session (session_id=NULL)"
    orphans = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE kind='invoke' AND agent='worker' AND session_id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


class _NeedHumanWorker(ScriptedWorker):

    def invoke(self, prompt, **kw):
        return Result(ok=True, text='previous units everything to make\n{"status": "need_human", '
                                    '"why": "task already to rinse out units u1 and u2, mine diff empty"}',
                      exit_code=0, status={"status": "need_human",
                                           "why": "task already to rinse out units u1 and u2, mine diff empty"})


def test_need_human_why_reaches_handoff(tmp_path):



    repo = _seed(tmp_path)
    runner = LocalRunner()
    conn = dao.connect(os.path.join(repo, ".rig", "m.db"))

    def _ex(sid):
        r = LocalRunner()
        sp = Spawner(adapter=_NeedHumanWorker(r), repo=repo, model="m", conn=conn,
                     session_id=sid, max_depth=4)
        return PhaseExecutor(spawner=sp, judge_adapter=Judge(r), judge_model="jm",
                             planner_adapter=Planner(r), planner_model="pm",
                             runner=r, repo=repo, conn=conn, cwd=repo, dimensions=("correctness",),
                             judge_schema=os.path.join(repo, ".rig", "verdict.json"))

    res = cli.cmd_new(conn, runner, repo, _ex(None), task="square", flow="base")
    rec = res["record"]
    assert rec["status"] == "need_human"
    assert "task already to rinse out units u1 and u2" in rec["handoff"]
    assert "to irritate by journal" not in rec["handoff"]


def test_gc_unit_branches_is_deterministic_and_safe(tmp_path):




    import subprocess

    from rig.flow.execute import gc_unit_branches
    repo = _seed(tmp_path)
    rn = LocalRunner()

    def g(*a):
        return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=False)

    base_branch = g("symbolic-ref", "--short", "HEAD").stdout.strip()

    def commit_on(branch, base, fname):
        g("checkout", "-q", "-b", branch, base)
        with open(os.path.join(repo, fname), "w") as fh:
            fh.write("x\n")
        g("add", "-A"); g("-c", "user.email=t@l", "-c", "user.name=t", "commit", "-q", "-m", fname)
        g("checkout", "-q", base_branch)

    commit_on("rig/u1", base_branch, "a.py")
    commit_on("rig/u2", "rig/u1", "b.py")
    commit_on("rig/stray", base_branch, "c.py")
    commit_on("keep-me", base_branch, "d.py")
    g("branch", "-f", "rig/session", "rig/u2")

    removed = gc_unit_branches(rn, repo, green_branches=["rig/u1", "rig/u2", "rig/stray", "rig/session"],
                               session_branch="rig/session")
    assert sorted(removed) == ["rig/u1", "rig/u2"]
    branches = g("branch").stdout
    assert "rig/stray" in branches
    assert "keep-me" in branches and base_branch in branches
    assert "rig/session" in branches
    assert g("status", "--porcelain").stdout.strip() == ""

    assert g("show", "rig/session:a.py").returncode == 0
    assert g("show", "rig/session:b.py").returncode == 0

    assert gc_unit_branches(rn, repo, green_branches=["rig/stray"], session_branch=None) == []


def test_record_tree_watchdog_flags_untracked_bypass(tmp_path):


    repo = _seed(tmp_path)
    runner = LocalRunner()
    conn = dao.connect(os.path.join(repo, ".rig", "m.db"))
    from rig.entry import session as session_mod
    st = session_mod.start_session(conn, runner, repo, task="clean")
    with open(os.path.join(repo, "hacked.py"), "w") as fh:
        fh.write("evil\n")
    from types import SimpleNamespace as NS
    fres = NS(ok=True, need_human=False, results=[{"name": "units", "green": True, "detail": ""}],
              ctx={})
    rec = cli._record_end(conn, runner, repo, st.session_id, fres)
    assert rec["status"] == "need_human" and "AND7-WATCHMAN" in rec["handoff"].upper().replace("A7", "AND7")
    os.remove(os.path.join(repo, "hacked.py"))

    with open(os.path.join(repo, "mine.py"), "w") as fh:
        fh.write("of course\n")
    st2 = session_mod.start_session(conn, runner, repo, task="dirty start")
    rec2 = cli._record_end(conn, runner, repo, st2.session_id, fres)
    assert rec2["status"] == "done" and "WATCHMAN" not in rec2["handoff"]
