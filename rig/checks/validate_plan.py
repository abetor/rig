"""Validate the structural contract of an execution plan [A1].

The same deterministic validator handles planner output and plans edited on disk.
It enforces safe, unique unit IDs, nonempty instructions and goals, and a list of
command arguments. It reports invalid units instead of silently dropping work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..core.types import VALID_UNIT_ID, Plan, Unit

_KINDS = ("writer", "readonly", "validator")  # [F1] Writers are serial; the others may run in parallel.


@dataclass
class PlanCheck:
    """A validated plan or a concrete rejection reason."""
    ok: bool
    plan: Optional[Plan] = None
    detail: str = ""
    raw: str = ""


def validate_plan(obj, *, raw: str = "") -> PlanCheck:
    """Validate a raw plan without silently dropping invalid content."""
    if not isinstance(obj, dict):
        return PlanCheck(False, detail="plan must be an object", raw=str(obj)[:200])
    goal = obj.get("goal")
    if not (isinstance(goal, str) and goal.strip()):
        return PlanCheck(False, detail="plan has no goal", raw=raw[:400])

    gate_cmd = obj.get("gate_cmd", [])
    if not isinstance(gate_cmd, list) or not all(isinstance(x, str) for x in gate_cmd):
        return PlanCheck(False, detail="gate_cmd must be a list of strings", raw=raw[:400])
    # An empty command is valid input; the gate will return NO-GATE and escalate.

    units_raw = obj.get("units")
    if not isinstance(units_raw, list) or not units_raw:
        return PlanCheck(False, detail="plan units must be a nonempty list", raw=raw[:400])

    units, seen = [], set()
    for i, u in enumerate(units_raw):
        if not isinstance(u, dict):
            return PlanCheck(False, detail=f"unit #{i} not object")
        uid = u.get("id")
        if not (isinstance(uid, str) and VALID_UNIT_ID.match(uid)):
            return PlanCheck(False, detail=f"unit #{i}: invalid id {uid!r} "
                             "(is required ^[A-Za-z0-9_][A-Za-z0-9._-]*$ — anti path-traversal [A1])")
        if uid in seen:
            return PlanCheck(False, detail=f"take id {uid!r}: two unit in branch rig/{uid} → "
                             "second overwrites first when cleanup [F6]")
        seen.add(uid)
        instr = u.get("instruction")
        if not (isinstance(instr, str) and instr.strip()):
            return PlanCheck(False, detail=f"unit {uid!r}: empty instruction (worker no-op)")
        kind = u.get("kind", "writer")
        if kind not in _KINDS:  # Unknown kinds default safely to serial writers [F1].
            kind = "writer"
        units.append(Unit(id=uid, instruction=instr.strip(), kind=kind))

    ctx = obj.get("context", "")
    context = ctx[:8000] if isinstance(ctx, str) else ""   # Bound prose passed to an agent brief.
    return PlanCheck(True, plan=Plan(goal=goal.strip(), units=units, gate_cmd=gate_cmd, context=context))
