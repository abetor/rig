"""Run the deterministic outcome gate [C1-C3].

The gate maps command exit status to a verdict and distinguishes code failures from
environment failures. Only FAIL is suitable as ground truth for automatic lessons [D1].
TIMEOUT and INFRA describe the environment, while an absent command yields NO-GATE.
Only PASS is successful, and command failures always produce a definite verdict.
"""
from __future__ import annotations

from typing import Optional

from ..core.types import StepResult


def run_gate(runner, gate_cmd, *, cwd: str, timeout: Optional[float] = 600,
             risk: str = "low") -> StepResult:
    """Return one of PASS, FAIL, TIMEOUT, INFRA, or NO-GATE.

    Only PASS sets ``ok=True``. Worker mode restricts the environment exposed to
    untrusted repository code [A2]. ``risk='high'`` marks an LLM-authored command;
    a runner that disallows it reports INFRA rather than a code failure [C3].
    """
    if not gate_cmd:
        return StepResult(ok=False, verdict="NO-GATE",
                          detail="no gate_cmd: no outcome evidence [C1]; escalation required")
    try:
        r = runner.exec(list(gate_cmd), cwd=cwd, timeout=timeout, risk=risk, worker=True)
    except PermissionError as e:
        return StepResult(ok=False, verdict="INFRA",
                          detail=f"gate BLOCKED (not giant): {e}")
    tail = ((r.stdout or "")[-1200:] + (r.stderr or "")[-400:]).strip()
    if r.timed_out:
        verdict = "TIMEOUT"                       # A hang is not evidence of a code defect.
    elif r.exit_code in (126, 127):
        verdict = "INFRA"                         # Missing or unexecutable binary.
    elif r.exit_code is not None and (r.exit_code < 0 or r.exit_code >= 128):
        verdict = "INFRA"                         # Signal termination is an infrastructure failure.
        # Direct subprocesses use negative signal codes; shell wrappers use 128+N.
        # Handle both so OOMs and external termination do not become code lessons [C1/D1].
    elif r.exit_code == 0:
        verdict = "PASS"
    else:
        verdict = "FAIL"                          # The check ran and rejected the code [C1].
    return StepResult(ok=(verdict == "PASS"), verdict=verdict,
                      detail=f"gate {verdict} (exit {r.exit_code}): {tail[-800:]}")
