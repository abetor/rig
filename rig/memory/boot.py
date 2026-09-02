"""Classify durable project state and build resume context."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..db import dao
from ..util import git
from . import recall
from .reconcile_state import (BRANCH_PREFIX, CORE_VERSION, RecordedState,
                              read_state, reconcile_stale)

COLD, WARM, STALE, IN_FLIGHT = "COLD", "WARM", "STALE", "IN_FLIGHT"

_MODE_GUIDE = {
    COLD: "First session: no durable memory exists. Scan the repository and establish baseline memory.",
    WARM: "Repository matches the last session. Load memory and handoff, then continue from there.",
    STALE: "State diverged after Git changes or a core upgrade. Reconcile memory against the Git log and state files.",
    IN_FLIGHT: "Unexpected worker branches exist. Inspect, finish, merge, or discard them before starting new work.",
}


@dataclass
class BootResult:
    mode: str
    guide: str                     # Mode guidance plus branch and stale-state recommendations.
    in_flight: list                # Current worker branches reported by Git.
    handoff: str
    manifest: object = None        # recall.Manifest (slice active memory), None when empty
    report: object = None
    context: str = ""              # Rendered guidance, memory, and handoff context.


def read_handoff(conn) -> str:
    """Read handoff."""
    try:
        col = dao.last_session_handoff(conn)
    except Exception:
        col = ""
    if (col or "").strip():
        return col.strip()
    note = dao.last_run(conn, kind="note")   # Load only the latest complete note.
    if note is None:
        return ""
    return (note.get("result") or note.get("prompt") or "").strip()


_ITEM_CAP = 200
_INBOUND_CAP = 500   # Maximum human-answer text included in context.
_MAX_ITEMS = 20


@dataclass
class Continuity:
    """Open decisions, work, and messages carried into a session."""
    board_auto: list = field(default_factory=list)      # Open work that can run autonomously.
    board_human: list = field(default_factory=list)     # Open work waiting on a human.
    decisions: list = field(default_factory=list)       # Active durable decisions.
    inbound: list = field(default_factory=list)         # Unread human messages.
    open_questions: list = field(default_factory=list)  # Unanswered outbound questions.

    def unread_ids(self) -> list:
        """Return unread inbound message IDs."""
        return [m["msg_id"] for m in self.inbound[:_MAX_ITEMS]]

    def render_inbound(self) -> str:
        """Render inbound."""
        if not (self.inbound or self.open_questions):
            return ""
        L = ["## HUMAN COMMUNICATION"]
        for m in self.inbound[:_MAX_ITEMS]:
            ref = (m.get("ref_text") or "").strip()
            prefix = f"response to '{_cut(ref, 120)}': " if ref else ""
            L.append(f"  - {prefix}{_cut(m.get('text', ''), _INBOUND_CAP)}")
        if len(self.inbound) > _MAX_ITEMS:
            L.append(f"  (+{len(self.inbound) - _MAX_ITEMS} incoming not to sew in — pridut track start)")
        if self.open_questions:
            L.append(f"Open questions for the human: {len(self.open_questions)} "
                     "(list with the questions tool); they do not block current work.")
        return "\n".join(L)

    def render_state(self) -> str:
        """Render state."""
        parts = []
        if self.board_auto or self.board_human:
            L = ["## BOARD PROJECT (open items; full list: tool board list)"]
            if self.board_auto:
                L.append("Runnable without human:")
                L += [f"  - #{r['item_id']} {_cut(r['text'], _ITEM_CAP)}" for r in self.board_auto[:_MAX_ITEMS]]
            if self.board_human:
                L.append("Waiting for a human (do not start autonomously):")
                L += [f"  - #{r['item_id']} {_cut(r['text'], _ITEM_CAP)}" for r in self.board_human[:_MAX_ITEMS]]
            hidden = max(0, len(self.board_auto) - _MAX_ITEMS) + max(0, len(self.board_human) - _MAX_ITEMS)
            if hidden:
                L.append(f"  (+{hidden} more items; use board list)")
            parts.append("\n".join(L))
        if self.decisions:
            L = ["## DECISIONS (locked; do not reconsider silently; supersede with: tool decision drop <id>)"]
            L += [f"  - #{r['decision_id']} {_cut(r['text'], _ITEM_CAP)}" for r in self.decisions[:_MAX_ITEMS]]
            if len(self.decisions) > _MAX_ITEMS:
                L.append(f"  (+{len(self.decisions) - _MAX_ITEMS} more; use decision list)")
            parts.append("\n".join(L))
        inbound = self.render_inbound()
        if inbound:
            parts.append(inbound)
        return "\n\n".join(parts)


def _cut(text: str, cap: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= cap else t[:cap] + "..."


def load_continuity(conn) -> Continuity:
    """Load continuity."""
    c = Continuity()
    try:
        c.board_auto = dao.list_board(conn, needs_human=False)
        c.board_human = dao.list_board(conn, needs_human=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        c.decisions = dao.list_decisions(conn)
    except Exception:  # noqa: BLE001
        pass
    try:
        c.inbound = dao.unread_inbound(conn)
        c.open_questions = dao.open_questions(conn)
    except Exception:  # noqa: BLE001
        pass
    return c


def classify(runner, conn, repo: str, *, prefix: str = BRANCH_PREFIX):
    """Classify the value."""
    state = read_state(conn)
    current = git.branches(runner, repo, prefix)
    if state is None:
        return (IN_FLIGHT if current else COLD), current, None  # A branch without state is orphaned work.
    recorded = set(state.in_flight or [])
    unexpected = [b for b in current if b not in recorded]
    if unexpected:
        return IN_FLIGHT, current, state
    if state.core_version != CORE_VERSION:
        return STALE, current, state             # A core upgrade requires reconciliation.
    if state.head and state.head == git.head(runner, repo):
        return WARM, current, state
    return STALE, current, state                 # HEAD moved, is missing, or Git failed.


def boot(runner, conn, repo: str, *, prefix: str = BRANCH_PREFIX) -> BootResult:
    """Classify project state and assemble session-start context."""
    mode, current, state = classify(runner, conn, repo, prefix=prefix)
    handoff = read_handoff(conn)              # Preserve handoff even if its session marker was lost.
    manifest = recall.build_manifest(conn)
    recorded = set(state.in_flight) if state else set()

    guide = f"## MODE ENTRY: {mode}\n{_MODE_GUIDE[mode]}"
    unexpected = [b for b in current if b not in recorded]
    known = [b for b in current if b in recorded]
    if unexpected:
        guide += "\nUnexpected branches to inspect, finish, merge, or discard: " + ", ".join(unexpected)
    if known:
        guide += ("\nGreen-branch (STACK — merge ONE accumulating, NOT checking; break-even branch see. in handoff): "
                  + ", ".join(known))
    if mode == COLD and handoff.strip():
        guide += "\n(!) marker session absence, but handoff FOUND — memory partly to lose, reconciliation against git."

    report = None
    if mode in (STALE, IN_FLIGHT):       # Unexpected branches also require Git diagnosis.
        report = reconcile_stale(runner, conn, repo, prefix=prefix)
        if report.recommendations:
            guide += "\n### RECONCILIATION (diagnosis vs git):\n" + "\n".join(f"- {r}" for r in report.recommendations)

    parts = [guide]
    if manifest.lines:
        parts.append("## MEMORY (dynamic, demand-driven)\n" + "\n".join(manifest.lines))
    if handoff.strip():
        parts.append("## LAST HANDOFF (dynamic)\n" + handoff.strip())
    state = load_continuity(conn).render_state()
    if state:
        parts.append(state)
    context = "\n\n".join(parts) + "\n"
    from ..core.events import emit
    emit(conn, kind="boot", name=mode, params={"in_flight": len(current)})

    return BootResult(mode=mode, guide=guide, in_flight=current, handoff=handoff,
                      manifest=manifest, report=report, context=context)
