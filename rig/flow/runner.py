"""Run flow phases in order with checkpoints and human gates."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .phase import Phase


@dataclass
class PhaseResult:
    """Outcome of one flow phase."""
    green: bool
    actionable: bool = False
    output: Optional[dict] = None
    detail: str = ""
    need_human: bool = False          # Executor requests a human decision [T1.4, §3.1].


@dataclass
class FlowResult:
    ok: bool                                  # True for a nonempty flow when every phase is green.
    results: List[dict] = field(default_factory=list)  # name, green, attempts, base, and detail per phase
    base: str = "HEAD"                        # Final integration base from green phases only.
    halted: bool = False
    ctx: dict = field(default_factory=dict)
    need_human: bool = False


def _run_phase(phase: Phase, execute: Callable, ctx: dict, phase_base: str,
               prior_green: list) -> tuple:
    """Run one phase with its configured retry policy."""
    attempt = 0
    feedback = ""
    res = None
    while True:
        attempt += 1
        pctx = {**ctx, "base": phase_base, "prior_green": list(prior_green),
                "attempt": attempt, "feedback": feedback}
        res = execute(phase, pctx)
        if res.green:
            break
        if phase.on_fail != "retry" or not res.actionable or attempt > phase.retry_cap:
            break
        feedback = res.detail  # Feed actionable findings into the next fix attempt.
    return res, attempt


def run_flow(phases: List[Phase], execute: Callable, *, ctx: Optional[dict] = None,
             base: str = "HEAD") -> FlowResult:
    """Run phases in order and carry green outputs forward."""
    ctx = dict(ctx or {})
    prior_green: list = list(ctx.get("prior_green", []))
    results: List[dict] = []
    halted = False
    need_human = False

    for phase in phases:                              # Writer phases are serialized [F1].
        phase_base = base
        res, attempts = _run_phase(phase, execute, ctx, phase_base, prior_green)

        if res.green:
            out = res.output if isinstance(res.output, dict) else {}
            branch = out.get("branch")
            if phase.advances_base and branch:
                base = branch
                prior_green.append({"phase": phase.name, **out})
            ctx = {**ctx, **out, "prior_green": list(prior_green)}  # Pass output to the next phase [F5].

        results.append({"name": phase.name, "green": res.green, "actionable": res.actionable,
                        "attempts": attempts, "base": phase_base, "detail": res.detail})

        if getattr(res, "need_human", False):
            need_human = True
            halted = True
            break
        if not res.green and phase.on_fail == "stop":
            halted = True
            break

    ok = bool(results) and not halted and all(r["green"] for r in results)
    return FlowResult(ok=ok, results=results, base=base, halted=halted, ctx=ctx, need_human=need_human)
