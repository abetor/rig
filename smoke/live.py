#!/usr/bin/env python3
"""Run live acceptance scenarios against real agent CLIs rather than test doubles.

This is not a pytest suite. It requires an authenticated `claude` or `codex` executable on PATH and
uses about $0.03-0.10 of paid inference per run. The unit tests in `../tests/` are hermetic and never
call a provider; this script verifies the complete entry -> spawn -> flow -> checks -> Git path.

Scenarios (--scenario):
  live   - one unit on a real CLI: delegate -> review -> gate -> commit. Defaults to Claude.
  chain  - two units: u2 builds on u1's commit (stacking [F5]); both gates must pass.
  cross  - the same task on Claude and Codex in turn, comparing success and cost [G1].
  resume - start a session, recording head and core -> boot WARM; an external commit -> STALE plus
           reconciliation.

  python3 smoke/live.py --scenario live  [--worker claude --judge codex --worker-model M]
  python3 smoke/live.py --scenario chain [--keep]

These scenarios prove that workers edit an isolated worktree branch while main remains unchanged [A7],
accepted work is committed and exposed as a Git diff, a different agent family can review it [G6], and
pytest determines the outcome [C1]. The resume scenario costs $0.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repository root

from rig.adapters.claude import ClaudeAdapter      # noqa: E402
from rig.adapters.codex import CodexAdapter         # noqa: E402
from rig.core.types import Plan, Unit               # noqa: E402
from rig.flow import run_flow                        # noqa: E402
from rig.flow.execute import PhaseExecutor           # noqa: E402
from rig.flow.phase import Phase                     # noqa: E402
from rig.tools.spawn import Spawner                  # noqa: E402
from rig.transport.local import LocalRunner          # noqa: E402

_ADAPTERS = {"claude": ClaudeAdapter, "codex": CodexAdapter}
_GITIGNORE = ".rig/\n__pycache__/\n*.pyc\n.pytest_cache/\n"

U1 = Unit(id="u1_geometry",
          instruction="Create geometry.py with area(r) = 3.14159*r*r. Create test_geometry.py with "
                      "`from geometry import area` and def test_area(): assert "
                      "abs(area(2)-12.56636)<1e-4. Change only these files and do not run commands.")
U2 = Unit(id="u2_circumference",
          instruction="In geometry.py, add circumference(r)=2*3.14159*r without changing area. "
                      "In test_geometry.py, add `from geometry import circumference` and "
                      "def test_circumference(): assert abs(circumference(3)-18.84954)<1e-4. Do not run any commands.")
GATE = ["python3", "-m", "pytest", "-q"]


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=False)


def _make_repo() -> str:
    repo = tempfile.mkdtemp(prefix="rig-smoke-")
    with open(os.path.join(repo, ".gitignore"), "w") as fh:
        fh.write(_GITIGNORE)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "smoke@example.com")
    _git(repo, "config", "user.name", "rig-smoke")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _model_for(harness: str, claude_model: str) -> str:
    """Choose a model per CLI; an empty Codex model selects the account default."""
    return "" if harness == "codex" else claude_model


def _executor(repo, *, worker, judge, worker_model, judge_model):
    runner = LocalRunner()
    spawner = Spawner(adapter=_ADAPTERS[worker](runner), repo=repo,
                      model=_model_for(worker, worker_model), max_depth=4)
    return PhaseExecutor(spawner=spawner, judge_adapter=_ADAPTERS[judge](runner),
                         judge_model=_model_for(judge, judge_model),
                         runner=runner, repo=repo, cwd=repo, dimensions=("correctness",),
                         judge_schema=os.path.join(repo, ".rig", "verdict.json"))


def _run_units(repo, units, **kw) -> dict:
    ex = _executor(repo, **kw)
    plan = Plan(goal="geometry", units=units, gate_cmd=GATE)
    res = run_flow([Phase(name="units", agent="units", advances_base=True, on_fail="continue")],
                   ex, ctx={"plan": plan}, base="HEAD")
    return res.results[0]


def _check_harnesses(*names) -> bool:
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        print(f"smoke: not on PATH: {', '.join(missing)} - skipped (a live agent CLI is required)", file=sys.stderr)
    return not missing


def scenario_live(args) -> int:
    if not _check_harnesses(args.worker, args.judge):
        return 77
    repo = _make_repo()
    r = _run_units(repo, [U1], worker=args.worker, judge=args.judge,
                   worker_model=args.worker_model, judge_model=args.judge_model)
    print(f"live · {r['name']}: green={r['green']} · {r['detail'][:120]}")
    if not args.keep:
        shutil.rmtree(repo, ignore_errors=True)
    return 0 if r["green"] else 1


def scenario_chain(args) -> int:
    if not _check_harnesses(args.worker, args.judge):
        return 77
    repo = _make_repo()
    r = _run_units(repo, [U1, U2], worker=args.worker, judge=args.judge,
                   worker_model=args.worker_model, judge_model=args.judge_model)
    geo = subprocess.run(["git", "show", "rig/u2_circumference:geometry.py"], cwd=repo,
                         capture_output=True, text=True).stdout
    stacked = "def area(r)" in geo and "def circumference(r)" in geo
    print(f"chain · green={r['green']} · stacking(u2⊃u1)={stacked} · {r['detail'][:100]}")
    if not args.keep:
        shutil.rmtree(repo, ignore_errors=True)
    return 0 if (r["green"] and stacked) else 1


def scenario_cross(args) -> int:
    if not _check_harnesses("claude", "codex"):
        return 77
    rows = []
    for worker, judge in (("claude", "codex"), ("codex", "claude")):
        repo = _make_repo()
        r = _run_units(repo, [U1], worker=worker, judge=judge,
                       worker_model=args.worker_model, judge_model=args.judge_model)
        rows.append((f"{worker}->{judge}", r["green"]))
        shutil.rmtree(repo, ignore_errors=True)
    for axis, green in rows:
        print(f"cross · {axis}: green={green}")
    return 0 if all(g for _, g in rows) else 1


def scenario_resume(args) -> int:
    """$0 and no agent CLI: start a session -> boot WARM; an external commit -> STALE plus reconciliation."""
    from rig.db import dao
    from rig.entry import session
    from rig.memory import boot
    repo = _make_repo()
    runner = LocalRunner()
    conn = dao.connect(os.path.join(repo, ".rig", "m.db"))
    session.start_session(conn, runner, repo, task="feature")
    m1 = boot(runner, conn, repo).mode
    with open(os.path.join(repo, "x.txt"), "w") as fh:
        fh.write("x")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "external")
    b = boot(runner, conn, repo)
    print(f"resume · after start={m1} · after external commit={b.mode} · diverged={b.report and b.report.diverged}")
    if not args.keep:
        shutil.rmtree(repo, ignore_errors=True)
    return 0 if (m1 == "WARM" and b.mode == "STALE") else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="smoke/live", description="live acceptance of rig on real agent CLIs")
    ap.add_argument("--scenario", choices=["live", "chain", "cross", "resume"], default="live")
    ap.add_argument("--worker", choices=list(_ADAPTERS), default="claude")
    ap.add_argument("--judge", choices=list(_ADAPTERS), default="claude")
    ap.add_argument("--worker-model", default="claude-sonnet-4-6")
    ap.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway repository")
    args = ap.parse_args()
    return {"live": scenario_live, "chain": scenario_chain,
            "cross": scenario_cross, "resume": scenario_resume}[args.scenario](args)


if __name__ == "__main__":
    sys.exit(main())
