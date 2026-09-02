"""Create durable session state at entry [E3].

Each session records its starting Git HEAD and core version so boot can distinguish
warm state from divergence. A slug derived from the requested name or task is shared
by the session, branch, and tmux window; the monotonic session ID provides uniqueness.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..db import dao
from ..memory.reconcile_state import CORE_VERSION
from ..util import git

_SLUG_CAP = 40

# Consolidate active memory at session start so the store cannot grow without bound [D4].
DECAY_MAX_AGE_DAYS = 90
DECAY_MAX_ITEMS = 200


def _consolidate_memory(conn) -> None:
    """Apply best-effort age, deduplication, and size limits at session start."""
    try:
        from ..memory import store
        store.consolidate(conn, now_ts=store.now_days(),
                          max_age_days=DECAY_MAX_AGE_DAYS, max_items=DECAY_MAX_ITEMS)
    except Exception:  # noqa: BLE001 - memory maintenance must not block a session
        pass


def slugify(task: str, *, fallback: str = "session") -> str:
    """Convert task text to a bounded branch-safe session name."""
    s = re.sub(r"[^0-9A-Za-z\u0400-\u04ff]+", "-", (task or "").strip().lower()).strip("-")
    s = s[:_SLUG_CAP].strip("-")
    return s or fallback


@dataclass
class StartedSession:
    session_id: int
    name: str
    branch: str
    tmux: str
    head: str


def start_session(conn, runner, repo: str, *, name: Optional[str] = None, task: str = "",
                  flow: str = "", agent: str = "") -> StartedSession:
    """Create a session with a safe name and durable boot markers [E3]."""
    # Names flow into branches, tmux windows, and worktree paths, so even explicit
    # names must be normalized against traversal [A1].
    nm = slugify(name) if (name and name.strip()) else slugify(task)
    head = git.head(runner, repo)                 # Baseline for divergence detection on resume.
    _consolidate_memory(conn)                     # Shared by CLI and lead session paths [D4].
    # Record initial cleanliness so end-of-session checks can detect direct writes
    # outside managed branches [A7]. ``None`` means Git could not answer.
    dirty0 = git.dirty(runner, repo)
    sid = dao.create_session(
        conn, name=nm, branch=nm, tmux=nm, flow=flow, agent=agent, task=task,
        head=head, core_version=CORE_VERSION,     # Required for warm-state classification.
        config={"tree_dirty_at_start": dirty0},
    )
    # Mark the session boundary in the run journal. Non-invocation rows are excluded
    # from pass/fail statistics by their kind.
    dao.log_run(conn, session_id=sid, kind="session_start", agent=agent, phase="start",
                result=f"start session #{sid} '{nm}' (flow={flow or '-'}); task: {(task or '').strip()[:200]}")
    from ..core.events import emit                # Session boundary in the event timeline.
    emit(conn, kind="session", name="start", session_id=sid, params={"flow": flow, "name": nm})
    return StartedSession(session_id=sid, name=nm, branch=nm, tmux=nm, head=head)
