"""Reconcile the plan with execution results before handoff [F].

Having every observed branch green does not prove the whole plan ran: a unit may
never have started, or an unrelated branch may remain. This deterministic,
read-only check compares planned units with green and on-disk branches. It does
not decide whether the resulting code satisfies the goal semantically.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from ..core.types import VALID_UNIT_ID

BRANCH_PREFIX = "rig/"


@dataclass
class UnitReconcile:
    """State of one planned unit: done, attempted, pending, or missing."""
    id: str
    status: str
    branch: Optional[str]
    green: bool


@dataclass
class ReconcileReport:
    """Structural reconciliation result, including actionable discrepancies."""
    matches_plan: bool
    goal: Optional[str]
    units: list
    orphan_branches: list
    integration_ready: bool
    discrepancies: list = field(default_factory=list)
    plan_ok: bool = True
    # True when this plan's units are complete and unique. Orphans belong to the
    # overall match check and must not turn a completed run into a partial one.
    units_ok: bool = False


def _units_of(plan_obj) -> Optional[list]:
    if not isinstance(plan_obj, dict):
        return None
    units = plan_obj.get("units")
    return units if isinstance(units, list) else None


def reconcile_report(plan_obj, *, green, on_disk_branches, prefix: str = BRANCH_PREFIX) -> ReconcileReport:
    """Compare a plan with green and on-disk branches without performing I/O."""
    gset = set(green or [])
    hset = set(on_disk_branches or [])
    units_raw = _units_of(plan_obj)
    goal = plan_obj.get("goal") if isinstance(plan_obj, dict) else None
    if not isinstance(goal, str):
        goal = None

    if units_raw is None:
        return ReconcileReport(
            matches_plan=False, goal=goal, units=[], orphan_branches=sorted(hset),
            integration_ready=False, plan_ok=False,
            discrepancies=["plan is missing or malformed: units must be a list"],
        )

    units: list = []
    plan_branches: set = set()
    valid_ids: list = []
    for u in units_raw:
        uid = u.get("id") if isinstance(u, dict) else None
        if not (isinstance(uid, str) and uid):
            units.append(UnitReconcile(id="(no id)", status="missing", branch=None, green=False))
            continue
        if not VALID_UNIT_ID.match(uid):
            # Invalid IDs cannot produce safe branch names, so the plan itself needs repair.
            units.append(UnitReconcile(id=uid, status="missing", branch=None, green=False))
            continue
        br = f"{prefix}{uid}"
        plan_branches.add(br)
        valid_ids.append(uid)
        if br in gset:
            st, gr = "done", True
        elif br in hset:
            st, gr = "attempted", False
        else:
            st, gr = "pending", False
        units.append(UnitReconcile(id=uid, status=st, branch=br, green=gr))

    orphans = sorted(hset - plan_branches)              # On-disk branches outside this plan.
    integration_ready = any(u.green for u in units)     # At least one unit can be integrated.

    disc: list = []
    pend = [u.id for u in units if u.status == "pending"]
    att = [u.id for u in units if u.status == "attempted"]
    miss = [u.id for u in units if u.status == "missing"]
    if miss:
        disc.append(f"units with missing or invalid IDs: {', '.join(miss)}")
    if pend:
        disc.append(f"planned units without branches (not started): {', '.join(pend)}")
    if att:
        disc.append(f"units with branches that are not green: {', '.join(att)}")
    if orphans:
        disc.append(f"branches matching {prefix}* are outside the plan (orphaned or stale): {', '.join(orphans)}")
    dup = sorted(uid for uid, n in Counter(valid_ids).items() if n > 1)
    if dup:
        disc.append(f"duplicate unit IDs share one {prefix}<id> branch: {', '.join(dup)}")

    all_done = bool(units) and all(u.status == "done" for u in units)  # Avoid all([]) == True.
    units_ok = all_done and not dup                     # Completion of this plan only.
    matches = units_ok and not orphans                  # Overall structural match.
    if not units:
        disc.insert(0, "plan has no units to execute")

    return ReconcileReport(
        matches_plan=matches, goal=goal, units=units, orphan_branches=orphans,
        integration_ready=integration_ready, discrepancies=disc, plan_ok=True, units_ok=units_ok,
    )


def render_reconcile(report: ReconcileReport) -> str:
    """Render a plain-text verdict in a stable, human-readable order."""
    head = "PLAN COMPLETE (structurally)" if report.matches_plan else "PLAN/RESULT DIVERGENCE"
    L = [f"reconcile · {head}"]
    if report.goal:
        L.append(f"  goal: {report.goal}")
    if not report.plan_ok:
        L.append("  plan is missing or malformed; there is nothing to reconcile")
        L += [f"    ! {d}" for d in report.discrepancies]
        return "\n".join(L)
    done = sum(1 for u in report.units if u.status == "done")
    L.append(f"  units: {done}/{len(report.units)} done")
    for u in report.units:
        L.append(f"    - {u.id} [{u.status}] {u.branch or '(branch unavailable: invalid id)'}")
    if report.orphan_branches:
        L.append(f"  orphan-branch (outside plan): {', '.join(report.orphan_branches)}")
    if report.discrepancies:
        L.append("  discrepancies to resolve before handoff:")
        L += [f"    ! {d}" for d in report.discrepancies]
    if report.matches_plan:
        L.append("  → structural match. Review the diff against the goal, then merge manually [A7].")
    elif report.integration_ready:
        L.append("  → some units are green, but the structure diverges from the plan.")
    else:
        L.append("  → structural mismatch with no green units; the plan is incomplete.")
    return "\n".join(L)
