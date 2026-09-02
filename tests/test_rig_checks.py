"""Tests for deterministic gates, plan validation, reconciliation, and review aggregation."""





from __future__ import annotations

import pytest

from rig.checks import gate, reconcile_plan, review
from rig.checks.validate_plan import validate_plan
from rig.core.result import Result
from rig.transport.base import ExecResult


class FR:

    def __init__(self, res):
        self._res = res
        self.calls = []

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.calls.append(cmd)
        return self._res



def test_gate_pass():
    r = gate.run_gate(FR(ExecResult(0, "5 passed", "", 1.0)), ["pytest", "-q"], cwd="/p")
    assert r.ok and r.verdict == "PASS"


def test_gate_fail_is_ground_truth():
    r = gate.run_gate(FR(ExecResult(1, "", "1 failed", 1.0)), ["pytest"], cwd="/p")
    assert not r.ok and r.verdict == "FAIL"


def test_gate_verdict_distinguishes_env_from_code():

    assert gate.run_gate(FR(ExecResult(124, "", "", 1, timed_out=True)), ["x"], cwd="/p").verdict == "TIMEOUT"
    assert gate.run_gate(FR(ExecResult(127, "", "not found", 1)), ["x"], cwd="/p").verdict == "INFRA"
    assert gate.run_gate(FR(ExecResult(126, "", "oserror", 1)), ["x"], cwd="/p").verdict == "INFRA"


def test_gate_signal_death_is_infra_not_fail():



    for code in (-9, -11, -15, 137, 139, 143):
        assert gate.run_gate(FR(ExecResult(code, "", "killed", 1)), ["x"], cwd="/p").verdict == "INFRA", code

    assert gate.run_gate(FR(ExecResult(1, "", "1 failed", 1)), ["x"], cwd="/p").verdict == "FAIL"


def test_gate_real_runner_signal_death_is_infra():



    from rig.transport.local import LocalRunner
    r = gate.run_gate(LocalRunner(),
                      ["python3", "-c", "import os,signal; os.kill(os.getpid(), signal.SIGSEGV)"], cwd=".")
    assert r.verdict == "INFRA"
    assert gate.run_gate(LocalRunner(), ["python3", "-c", "import sys; sys.exit(1)"], cwd=".").verdict == "FAIL"


def test_gate_no_cmd_is_no_gate_no_exec():
    fr = FR(ExecResult(0, "", "", 0))
    r = gate.run_gate(fr, [], cwd="/p")
    assert not r.ok and r.verdict == "NO-GATE" and fr.calls == []


def test_gate_exec_is_worker_cutlegs():



    class Rec:
        kw = None

        def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
            Rec.kw = {"risk": risk, "worker": worker}
            return ExecResult(0, "", "", 0.1)

    assert gate.run_gate(Rec(), ["pytest"], cwd="/p").verdict == "PASS"
    assert Rec.kw["worker"] is True


def test_gate_env_cutlegs_hides_lead_secret_live(monkeypatch):


    from rig.transport.local import LocalRunner
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3cr3t-lead-key")
    r = gate.run_gate(
        LocalRunner(),
        ["python3", "-c", "import os; print(os.environ.get('AWS_SECRET_ACCESS_KEY', 'ABSENT'))"],
        cwd=".")
    assert r.verdict == "PASS" and "ABSENT" in r.detail and "s3cr3t-lead-key" not in r.detail


def test_gate_risk_high_blocked_real_runner_is_infra(tmp_path):



    from rig.transport.local import LocalRunner
    import os
    marker = str(tmp_path / "ran.txt")
    r = gate.run_gate(LocalRunner(), ["python3", "-c", f"open({marker!r}, 'w').write('x')"],
                      cwd=str(tmp_path), risk="high")
    assert not r.ok and r.verdict == "INFRA" and "BLOCKED" in r.detail
    assert not os.path.exists(marker)

    assert gate.run_gate(LocalRunner(), ["python3", "-c", "pass"], cwd=str(tmp_path)).verdict == "PASS"



def _plan(units, goal="goal", gate_cmd=("pytest", "-q")):
    return {"goal": goal, "units": units, "gate_cmd": list(gate_cmd)}


def test_validate_plan_ok():
    pc = validate_plan(_plan([{"id": "u1", "instruction": "implement add()"}]))
    assert pc.ok and pc.plan.goal == "goal" and pc.plan.units[0].id == "u1"


def test_validate_plan_rejects_traversal_id():
    pc = validate_plan(_plan([{"id": "../etc", "instruction": "x"}]))
    assert not pc.ok and "traversal" in pc.detail


def test_validate_plan_rejects_duplicate_id():
    pc = validate_plan(_plan([{"id": "u1", "instruction": "a"}, {"id": "u1", "instruction": "b"}]))
    assert not pc.ok and "take" in pc.detail


def test_validate_plan_rejects_empty_instruction_and_goal():
    assert not validate_plan(_plan([{"id": "u1", "instruction": "  "}])).ok
    assert not validate_plan(_plan([{"id": "u1", "instruction": "a"}], goal="")).ok


def test_validate_plan_empty_units_rejected():
    assert not validate_plan(_plan([])).ok


def test_validate_plan_unknown_kind_defaults_writer():
    pc = validate_plan(_plan([{"id": "u1", "instruction": "a", "kind": "wizard"}]))
    assert pc.ok and pc.plan.units[0].kind == "writer"



def test_reconcile_all_done_matches():
    rep = reconcile_plan.reconcile_report(
        _plan([{"id": "u1", "instruction": "a"}, {"id": "u2", "instruction": "b"}]),
        green=["rig/u1", "rig/u2"], on_disk_branches=["rig/u1", "rig/u2"])
    assert rep.matches_plan and rep.discrepancies == []


def test_reconcile_partial_lists_discrepancies():
    rep = reconcile_plan.reconcile_report(
        _plan([{"id": "u1", "instruction": "a"}, {"id": "u2", "instruction": "b"},
               {"id": "u3", "instruction": "c"}]),
        green=["rig/u1"], on_disk_branches=["rig/u1", "rig/u2"])
    assert not rep.matches_plan
    byid = {u.id: u.status for u in rep.units}
    assert byid == {"u1": "done", "u2": "attempted", "u3": "pending"}
    assert rep.integration_ready


def test_reconcile_orphan_branch_flagged():
    rep = reconcile_plan.reconcile_report(
        _plan([{"id": "u1", "instruction": "a"}]),
        green=["rig/u1"], on_disk_branches=["rig/u1", "rig/orphan"])


    assert not rep.matches_plan and "rig/orphan" in " ".join(rep.discrepancies)
    assert rep.units_ok is True


def test_reconcile_dup_id_fails_units_ok():

    rep = reconcile_plan.reconcile_report(
        _plan([{"id": "u1", "instruction": "a"}, {"id": "u1", "instruction": "b"}]),
        green=["rig/u1"], on_disk_branches=["rig/u1"])
    assert rep.units_ok is False


def test_reconcile_no_plan_is_not_match():
    rep = reconcile_plan.reconcile_report(None, green=[], on_disk_branches=[])
    assert not rep.matches_plan and not rep.plan_ok


def test_reconcile_invalid_id_is_missing():
    rep = reconcile_plan.reconcile_report(
        _plan([{"id": "../x", "instruction": "a"}]), green=[], on_disk_branches=[])
    assert rep.units[0].status == "missing" and not rep.matches_plan



def test_parse_verdict_ok():
    v = review.parse_verdict(Result(ok=True, text='{"ok": true, "why": "well"}', exit_code=0,
                                    schema_applied=True), dimension="correctness")
    assert v.ok and not v.abstained and v.why == "well"


def test_parse_verdict_reject():
    v = review.parse_verdict(Result(ok=True, text='{"ok": false, "why": "bug"}', exit_code=0,
                                    schema_applied=True), dimension="correctness")
    assert not v.ok and not v.abstained and v.why == "bug"


def test_parse_verdict_schema_not_applied_abstains():

    v = review.parse_verdict(Result(ok=True, text='{"ok": true, "why": "forgery"}', exit_code=0,
                                    schema_applied=False), dimension="security")
    assert not v.ok and v.abstained


def test_parse_verdict_no_schema_requested_abstains():


    v = review.parse_verdict(Result(ok=True, text='{"ok": true, "why": "x"}', exit_code=0,
                                    schema_applied=None))
    assert not v.ok and v.abstained


def test_parse_verdict_no_verdict_abstains():
    v = review.parse_verdict(Result(ok=True, text="chatter without json", exit_code=0,
                                    schema_applied=True))
    assert not v.ok and v.abstained


def test_aggregate_all_ok():
    p = review.aggregate([review.Verdict("correctness", True), review.Verdict("security", True)])
    assert p.ok and p.fix_items == []


def test_aggregate_abstain_is_not_reject():

    p = review.aggregate([review.Verdict("correctness", False, "real bug"),
                          review.Verdict("security", False, "not smog", abstained=True)])
    assert not p.ok
    assert p.fix_items == ["[correctness] real bug"]
    assert "ABSTAIN" in p.detail and "REJECT" in p.detail


def test_aggregate_empty_panel_not_ok():

    assert review.aggregate([]).ok is False


def test_aggregate_fix_items_preserve_dim_order():

    p = review.aggregate([review.Verdict("a", False, "x"), review.Verdict("b", False, "y")])
    assert p.fix_items == ["[a] x", "[b] y"]
