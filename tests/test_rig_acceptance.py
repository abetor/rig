"""End-to-end acceptance tests using real Git repositories and deterministic fake agents."""










from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from rig.core.result import Result
from rig.core.types import Plan, Unit
from rig.flow import run_flow
from rig.flow.execute import PhaseExecutor
from rig.flow.phase import Phase
from rig.tools.spawn import Spawner
from rig.transport.local import LocalRunner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="is required git in PATH")


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


def _seed_repo(tmp_path) -> str:
    repo = str(tmp_path / "proj")
    os.makedirs(repo)
    with open(os.path.join(repo, ".gitignore"), "w") as fh:
        fh.write(".rig/\n__pycache__/\n*.pyc\n.pytest_cache/\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "seed@example.com")
    _git(repo, "config", "user.name", "seed")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")


    os.makedirs(os.path.join(repo, ".rig"), exist_ok=True)
    with open(os.path.join(repo, ".rig", "gate.json"), "w") as fh:
        fh.write('["python3", "-m", "pytest", "-q"]')
    return repo




_SCRIPT = {
    "area": [
        ("geometry.py", "def area(r):\n    return 3.14159 * r * r\n"),
        ("test_geometry.py", "from geometry import area\n\ndef test_area():\n    assert abs(area(2) - 12.56636) < 1e-4\n"),
    ],
    "circumference": [
        ("geometry.py", "\ndef circumference(r):\n    return 2 * 3.14159 * r\n"),
        ("test_geometry.py", "\nfrom geometry import circumference\n\ndef test_circumference():\n    assert abs(circumference(3) - 18.84954) < 1e-4\n"),
    ],
}


class _Caps:
    schema_output = True
    effort = True
    native_resume = True
    agents_md_native = True


class ScriptedWorker:


    name = "worker"

    def __init__(self, runner):
        self.runner = runner

    def invoke(self, prompt, *, model, cwd, perms, effort, system_context_path,
               resume_session_id, timeout, risk, schema_path, allowed_tools=None, confine=False):
        task = prompt.split("YOUR TASK", 1)[-1]
        for marker, files in _SCRIPT.items():
            if marker in task:
                for path, content in files:
                    with open(os.path.join(cwd, path), "a") as fh:
                        fh.write(content)
                return Result(ok=True, text=f"way of writing {marker}", exit_code=0)
        return Result(ok=True, text="noop", exit_code=0)

    def inject_context(self, method_text, cwd):
        return f"{cwd}/.rig/system-prompt.md"

    def capabilities(self):
        return _Caps()


class OkJudge:
    name = "judge"

    def __init__(self, runner):
        self.runner = runner

    def invoke(self, prompt, *, model, cwd, perms, effort, system_context_path,
               resume_session_id, timeout, risk, schema_path, allowed_tools=None, confine=False):
        applied = True if schema_path else None
        return Result(ok=True, text='{"ok": true, "why": "ok"}', exit_code=0, schema_applied=applied)

    def capabilities(self):
        return _Caps()


def _executor(repo):
    runner = LocalRunner()
    spawner = Spawner(adapter=ScriptedWorker(runner), repo=repo, model="m", max_depth=4)
    return PhaseExecutor(
        spawner=spawner, judge_adapter=OkJudge(runner), judge_model="jm",
        runner=runner, repo=repo, cwd=repo, dimensions=("correctness",),
        judge_schema=os.path.join(repo, ".rig", "verdict.json"))


def _chain_plan():
    return Plan(
        goal="geometry: area + circumference",
        gate_cmd=["python3", "-m", "pytest", "-q"],
        units=[
            Unit(id="u1_geo", instruction="Creation geometry.py (area) and test_geometry.py — about geometry."),
            Unit(id="u2_circ", instruction="Add circumference in geometry.py and test — circumference."),
        ])


def test_acceptance_chain_real_git_and_gate(tmp_path):

    repo = _seed_repo(tmp_path)
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()

    ex = _executor(repo)
    phases = [Phase(name="units", agent="units", advances_base=True, on_fail="continue"),
              Phase(name="reconcile", agent="reconcile", on_fail="continue")]
    res = run_flow(phases, ex, ctx={"plan": _chain_plan()}, base="HEAD")


    units_res = next(r for r in res.results if r["name"] == "units")
    assert units_res["green"] and "2/2 green" in units_res["detail"]


    geo_u2 = subprocess.run(["git", "show", "rig/u2_circ:geometry.py"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "def area(r)" in geo_u2 and "def circumference(r)" in geo_u2


    geo_u1 = subprocess.run(["git", "show", "rig/u1_geo:geometry.py"], cwd=repo,
                            capture_output=True, text=True).stdout
    assert "def area(r)" in geo_u1 and "def circumference(r)" not in geo_u1


    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert head_after == head_before
    assert not os.path.exists(os.path.join(repo, "geometry.py"))
    porcelain = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert porcelain == ""


    recon = next(r for r in res.results if r["name"] == "reconcile")
    assert recon["green"] and res.base == "rig/u2_circ"


def test_acceptance_resume_warm_then_stale_real_git(tmp_path):


    from rig.db import dao
    from rig.entry import session
    from rig.memory import STALE, WARM, boot

    repo = _seed_repo(tmp_path)
    runner = LocalRunner()
    conn = dao.connect(os.path.join(repo, ".rig", "m.db"))
    session.start_session(conn, runner, repo, task="feature")

    assert boot(runner, conn, repo).mode == WARM


    with open(os.path.join(repo, "newfile.txt"), "w") as fh:
        fh.write("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "external commit")

    b = boot(runner, conn, repo)
    assert b.mode == STALE and b.report is not None and b.report.diverged
    assert b.report.ahead == 1


def test_acceptance_gate_fail_blocks_green_and_commit(tmp_path):

    repo = _seed_repo(tmp_path)
    ex = _executor(repo)
    plan = Plan(goal="invalid", gate_cmd=["python3", "-m", "pytest", "-q"],
                units=[Unit(id="u_bad", instruction="bad")])

    phases = [Phase(name="units", agent="units", advances_base=True, on_fail="continue")]
    res = run_flow(phases, ex, ctx={"plan": plan}, base="HEAD")
    assert res.results[0]["green"] is False

    rev = subprocess.run(["git", "rev-list", "--count", "HEAD..rig/u_bad"], cwd=repo,
                         capture_output=True, text=True)
    assert rev.stdout.strip() in ("0", "")
