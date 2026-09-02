"""Fail-soft invocation heartbeats (plan §10, stage 2).

The context manager creates a liveness row, refreshes it on a background thread,
and removes it on exit. A fresh row means the caller is still waiting; a stale row
suggests the caller died or was killed. The thread opens its own SQLite connection.
In-memory databases and missing connections simply disable periodic updates.
"""
from __future__ import annotations

import contextlib
import os
import threading
from typing import Optional


def _db_path(conn) -> str:
    """Return the main database path, or an empty string for memory databases."""
    try:
        for _, name, path in conn.execute("PRAGMA database_list"):
            if name == "main":
                return path or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


class _Beat:
    """Own a liveness row and its refresh thread."""

    def __init__(self, conn, live_id: Optional[int], thread: Optional[threading.Thread],
                 stop_event: Optional[threading.Event]):
        self._conn = conn
        self.live_id = live_id
        self._thread = thread
        self._stop = stop_event

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self.live_id is not None:
            try:
                from ..db import dao
                dao.liveness_end(self._conn, self.live_id)
            except Exception:  # noqa: BLE001
                pass


def _beat_loop(db_path: str, live_id: int, stop: threading.Event, interval: float) -> None:
    """Refresh one liveness row using a dedicated connection.

    Transient lock errors roll back and retry on the next interval instead of
    permanently killing the heartbeat thread.
    """
    try:
        from ..db import dao
        conn = dao.connect(db_path)
        try:
            while not stop.wait(interval):
                try:
                    dao.liveness_beat(conn, live_id)
                except Exception:  # noqa: BLE001 - transient lock or busy error
                    try:
                        conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def heartbeat(conn, *, session_id: Optional[int] = None, role: str = "", phase: str = "",
              adapter: str = "", label: str = "sync", interval: Optional[float] = None):
    """Maintain a liveness row around an invocation; failures degrade to a no-op."""
    beat = _Beat(conn, None, None, None)
    if conn is not None:
        live_id = None
        try:
            from ..db import dao
            live_id = dao.liveness_start(conn, session_id=session_id, role=role, phase=phase,
                                         adapter=adapter, label=label, pid=os.getpid())
            path = _db_path(conn)
            thread = stop = None
            if path:                       # A second connection cannot share an in-memory database.
                stop = threading.Event()
                thread = threading.Thread(
                    target=_beat_loop,
                    args=(path, live_id, stop, float(interval or dao.BEAT_SECONDS)),
                    daemon=True, name=f"rig-beat-{live_id}")
                thread.start()
            beat = _Beat(conn, live_id, thread, stop)
        except Exception:  # noqa: BLE001 - heartbeats must not interrupt the invocation
            try:                           # Remove a row created before thread startup failed.
                conn.rollback()
                if live_id is not None:
                    from ..db import dao
                    dao.liveness_end(conn, live_id)
            except Exception:  # noqa: BLE001
                pass
            beat = _Beat(conn, None, None, None)
    try:
        yield beat
    finally:
        beat.stop()
