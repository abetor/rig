"""Fail-soft event recording over ``dao.log_event`` (plan §10, stage 2).

Observability failures must never interrupt work. Engine boundaries are recorded as
events; agent invocations already live in ``runs`` [D11]. Notification hooks remain
in ``flow.notify`` so the core depends only on the database layer.
"""
from __future__ import annotations

from typing import Optional


def emit(conn, *, actor: str = "engine", kind: str = "", name: str = "",
         session_id: Optional[int] = None, params: Optional[dict] = None,
         status: str = "", duration_ms: Optional[int] = None) -> Optional[int]:
    """Record an event, returning ``None`` on any observability failure.

    Roll back a failed insert so an open write transaction cannot retain a reserved
    lock and block every other database writer.
    """
    if conn is None:
        return None
    try:
        from ..db import dao
        return dao.log_event(conn, actor=actor, kind=kind, name=name, session_id=session_id,
                             params=params, status=status, duration_ms=duration_ms)
    except Exception:  # noqa: BLE001 - observability is deliberately fail-soft
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
