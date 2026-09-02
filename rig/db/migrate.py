"""Schema versioning and forward-only, idempotent migrations [E5].

The database stores its version in ``meta``. ``MIGRATIONS[i]`` upgrades version
``i`` to ``i + 1``, and ``LATEST`` matches ``SCHEMA_VERSION``. DDL is additive or
guarded so repeated runs are safe. Project settings may also live in ``meta``, but
secrets remain in the environment [D1].
"""
from __future__ import annotations

import sqlite3
from typing import Callable, List, Optional

from . import schema

# Schema version and nonsecret project settings (§9.6).
META_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_VERSION_KEY = "schema_version"


def _ensure_meta(conn: sqlite3.Connection) -> None:
    """Create the metadata table if needed."""
    conn.execute(META_DDL)


def get_version(conn: sqlite3.Connection) -> int:
    """Return the database schema version, or zero for a new database."""
    _ensure_meta(conn)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_VERSION_KEY,)).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def set_version(conn: sqlite3.Connection, version: int) -> None:
    """Upsert the schema version."""
    _ensure_meta(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_VERSION_KEY, str(int(version))),
    )


def _migrate_0_to_1(conn: sqlite3.Connection) -> None:
    """Create the original tables, indexes, and append-only triggers."""
    for stmt in schema.create_statements():
        conn.execute(stmt)


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """Add the cross-process spawn-reservation ledger and index [F7]."""
    conn.execute(schema._SPAWN_RESERVATIONS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reservations_session ON spawn_reservations(session_id)")


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """Add the native harness transcript to runs [D11]."""
    conn.execute("ALTER TABLE runs ADD COLUMN transcript TEXT NOT NULL DEFAULT ''")


def _migrate_3_to_4(conn: sqlite3.Connection) -> None:
    """Add continuum tables, their indexes, and session handoffs (plan §10)."""
    for stmt in (schema._DECISIONS, schema._BOARD, schema._MESSAGES, schema._ARTIFACTS):
        conn.execute(stmt)
    for idx in ("CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status)",
                "CREATE INDEX IF NOT EXISTS idx_board_status ON board(status)",
                "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)",
                "CREATE INDEX IF NOT EXISTS idx_messages_dir_status ON messages(direction, status)",
                "CREATE INDEX IF NOT EXISTS idx_artifacts_name ON artifacts(name, status)"):
        conn.execute(idx)
    # Guard the non-idempotent ALTER in case a process died before recording the version.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "handoff" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN handoff TEXT NOT NULL DEFAULT ''")


def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """Add append-only events, liveness heartbeats, and indexes."""
    conn.execute(schema._EVENTS)
    conn.execute(schema._LIVENESS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_liveness_session ON liveness(session_id)")
    for trig in schema.trigger_statements():
        if "events_block" in trig:
            conn.execute(trig)


# Ordered upgrades: append each new version to the end.
MIGRATIONS: List[Callable[[sqlite3.Connection], None]] = [
    _migrate_0_to_1,
    _migrate_1_to_2,
    _migrate_2_to_3,
    _migrate_3_to_4,
    _migrate_4_to_5,
]

# The current version equals the number of migration steps.
LATEST = len(MIGRATIONS)
assert LATEST == schema.SCHEMA_VERSION, "MIGRATIONS is out of sync with SCHEMA_VERSION"


def migrate(
    conn: sqlite3.Connection,
    *,
    migrations: Optional[List[Callable[[sqlite3.Connection], None]]] = None,
    target: Optional[int] = None,
) -> int:
    """Upgrade a database to the target version without losing data [E5].

    Existing versions skip completed steps. A database newer than the engine is
    left unchanged. ``migrations`` and ``target`` support migration tests.
    """
    migs = MIGRATIONS if migrations is None else migrations
    latest = len(migs) if target is None else int(target)
    _ensure_meta(conn)
    cur = get_version(conn)
    if cur >= latest:
        conn.commit()
        return cur
    # New databases receive the current schema directly. Replaying historical ALTER
    # steps after creating the current schema would duplicate columns [E5].
    if cur == 0:
        for stmt in schema.create_statements():
            conn.execute(stmt)
        set_version(conn, latest)
        conn.commit()
        return latest
    # Existing databases apply missing incremental steps in order.
    while cur < latest:
        step = migs[cur]          # Upgrade v(cur) to v(cur+1).
        step(conn)
        cur += 1
        set_version(conn, cur)
        conn.commit()             # Record the step and version atomically.
    return cur
