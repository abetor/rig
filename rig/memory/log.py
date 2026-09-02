"""Write and summarize the append-only invocation log."""
from __future__ import annotations

import sqlite3
from typing import List, Optional

from rig.db import dao

_CALL_KIND = "invoke"


def log_call(
    conn: sqlite3.Connection,
    *,
    prompt: str = "",
    result: str = "",
    session_id: Optional[int] = None,
    kind: str = _CALL_KIND,
    agent: str = "",
    adapter: str = "",
    phase: str = "",
    status: str = "",
    ok: bool = False,
    cost: Optional[float] = None,
    cost_origin: str = "",
    resume_id: str = "",
    meta: Optional[dict] = None,
    transcript: str = "",
) -> int:
    """Log call."""
    return dao.log_run(
        conn,
        session_id=session_id,
        kind=kind,
        agent=agent,
        adapter=adapter,
        phase=phase,
        prompt=prompt,
        result=result,
        status=status,
        ok=ok,
        cost=cost,
        cost_origin=cost_origin,
        resume_id=resume_id,
        meta=meta,
        transcript=transcript,
    )


def _is_error(run: dict) -> bool:
    """Return whether error."""
    return run.get("kind") == _CALL_KIND and not run.get("ok")


def summarize(
    conn: sqlite3.Connection,
    *,
    session_id: Optional[int] = None,
    tail: int = 5,
) -> dict:
    """Summarize the value."""
    runs: List[dict] = dao.list_runs(conn, session_id)
    by_kind: dict = {}
    errors: List[dict] = []
    for run in runs:
        k = str(run.get("kind") or "")
        by_kind[k] = by_kind.get(k, 0) + 1
        if _is_error(run):
            errors.append(run)
    return {
        "events": len(runs),
        "by_kind": by_kind,
        "errors": len(errors),
        "recent_errors": errors[-tail:] if tail > 0 else [],
        "tail": runs[-tail:] if tail > 0 else [],
    }
