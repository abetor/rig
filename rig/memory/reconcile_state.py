"""Persist and classify durable execution state."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..db import dao
from ..util import git

CORE_VERSION = "v0"

BRANCH_PREFIX = "rig/"

ALIVE = "alive"        # Branch exists and is not merged; work remains unresolved.
MERGED = "merged"      # Branch is already an ancestor of HEAD and can be removed.
GONE = "gone"          # Branch is absent, so the durable marker is stale.


@dataclass
class RecordedState:
    """Durable Git and session state recorded at a boundary."""
    head: str            # Git HEAD recorded at session end.
    in_flight: list
    core_version: str    # Core version when the state was recorded.
    ts: str = ""         # Session update timestamp.
    session_id: int = 0


def read_state(conn) -> Optional[RecordedState]:
    """Read state."""
    sessions = dao.list_sessions(conn)
    if not sessions:
        return None
    last = sessions[-1]  # Sessions are ordered by ID, so the last row is current.
    return RecordedState(
        head=last.get("head") or "",
        in_flight=list(last.get("in_flight") or []),
        core_version=last.get("core_version") or "",
        ts=last.get("updated_ts") or "",
        session_id=int(last.get("session_id") or 0),
    )


@dataclass
class BranchFinding:
    branch: str
    status: str            # ALIVE / MERGED / GONE
    recommendation: str    # Human-readable recovery action.


@dataclass
class StaleReport:
    """Diagnosis of stale state and unresolved worker branches."""
    has_state: bool                       # False means no durable marker exists.
    recorded_head: str
    current_head: str
    head_moved: bool
    ahead: int                            # Commits added since the recorded HEAD.
    head_unreachable: bool                # Recorded HEAD is absent after rewrite or repo replacement.
    recorded_core: str
    current_core: str
    core_upgraded: bool
    branches: list = field(default_factory=list)     # Findings for recorded and on-disk branches.
    recommendations: list = field(default_factory=list)
    diverged: bool = False


def _diagnose_branches(runner, repo, names, on_disk, cur_head) -> list:
    """Classify recorded and on-disk worker branches."""
    on = set(on_disk)
    out = []
    for br in names:
        if br not in on:
            out.append(BranchFinding(br, GONE,
                "branch is absent; remove its stale in-flight marker after confirming the work is merged or discarded."))
        elif cur_head and git.is_ancestor(runner, repo, br, cur_head):
            out.append(BranchFinding(br, MERGED,
                "branch is already an ancestor of HEAD; remove the branch and its in-flight marker."))
        else:
            out.append(BranchFinding(br, ALIVE,
                "branch exists and is not merged; inspect it, then finish, merge, or discard the work."))
    return out


def reconcile_stale(runner, conn, repo: str, *, prefix: str = BRANCH_PREFIX) -> StaleReport:
    """Compare durable session state with the current repository."""
    state = read_state(conn)
    cur_head = git.head(runner, repo)

    on_disk = git.branches(runner, repo, prefix)
    recorded = list(state.in_flight) if state else []
    findings = _diagnose_branches(runner, repo, sorted(set(recorded) | set(on_disk)), on_disk, cur_head)

    if state is None:
        recs = [f"Branch {f.branch}: {f.recommendation}" for f in findings]
        return StaleReport(has_state=False, recorded_head="", current_head=cur_head, head_moved=False,
                           ahead=0, head_unreachable=False, recorded_core="", current_core=CORE_VERSION,
                           core_upgraded=False, branches=findings, recommendations=recs,
                           diverged=bool(findings))

    rec_head = state.head or ""
    head_moved = bool(rec_head) and bool(cur_head) and rec_head != cur_head
    head_unconfirmed = not rec_head or not cur_head
    ahead = 0
    head_unreachable = False
    if head_moved:
        n = git.ahead_count(runner, repo, rec_head, cur_head)
        if n is None:
            head_unreachable = True       # Recorded HEAD is outside current history.
        else:
            ahead = n

    core_upgraded = state.core_version != CORE_VERSION

    recs: list = []
    if core_upgraded:
        recs.append(f"Upgrade core memory ({state.core_version or '∅'} → {CORE_VERSION}): "
                    "review stored state and lessons, and migrate them if necessary.")
    if head_unreachable:
        recs.append(f"Recorded HEAD {rec_head[:12]} was not found in current history after a rewrite or repository change; "
                    "memory may reference removed commits, so inspect the Git log manually.")
    elif head_moved:
        recs.append(f"git advanced by {ahead} commits beyond recorded HEAD {rec_head[:12]}; "
                    "review the intervening Git log before trusting stored memory.")
    elif head_unconfirmed:
        recs.append("HEAD not to check (sth against sth) (recorded HEAD missing or Git inaccessible); "
                    "do not trust stored memory blindly, and inspect repository state manually.")
    for f in findings:
        recs.append(f"Branch {f.branch}: {f.recommendation}")

    diverged = bool(head_moved or core_upgraded or head_unconfirmed or findings)

    return StaleReport(
        has_state=True, recorded_head=rec_head, current_head=cur_head, head_moved=head_moved,
        ahead=ahead, head_unreachable=head_unreachable,
        recorded_core=state.core_version, current_core=CORE_VERSION, core_upgraded=core_upgraded,
        branches=findings, recommendations=recs, diverged=diverged,
    )
