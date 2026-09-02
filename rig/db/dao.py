"""SQLite data access for sessions, runs, lessons, and orchestration state.

Connections migrate older databases forward [E5]. Runs remain physically
append-only [D3], while lessons support deduplication and higher-level lifecycle
operations. Secrets never enter this layer [D1]. JSON fields are serialized
transparently.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from . import convention, migrate


def now_iso() -> str:
    """Return the current UTC timestamp with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    """Open a database, configure it, and apply pending migrations [E5]."""
    if path != ":memory:":
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Let a second writer wait briefly for cross-process reservation locks [F7].
    conn.execute("PRAGMA busy_timeout = 5000")
    migrate.migrate(conn)
    return conn


def open_project(project: str) -> sqlite3.Connection:
    """Open the conventional database for a project root [O4]."""
    return connect(convention.ensure_db_path(project))


def _row(row) -> Optional[dict]:
    """Convert a SQLite row to a dictionary, preserving ``None``."""
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def _loads(text: str, fallback):
    try:
        return json.loads(text) if text else fallback
    except (json.JSONDecodeError, TypeError, ValueError):
        return fallback


# ----------------------------------------------------------------------------- meta (§9.6)
def get_meta(conn: sqlite3.Connection, key: str, default=None):
    """Read a project setting from ``meta``; secrets belong in the environment [D1]."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


# ----------------------------------------------------------------------------- sessions (§7.2)
def next_session_id(conn: sqlite3.Connection) -> int:
    """Return the next monotonically increasing project session ID (§10.4)."""
    row = conn.execute("SELECT COALESCE(MAX(session_id), 0) + 1 FROM sessions").fetchone()
    return int(row[0])


def create_session(
    conn: sqlite3.Connection,
    *,
    name: str,
    branch: str = "",
    tmux: str = "",
    flow: str = "",
    agent: str = "",
    task: str = "",
    status: str = "active",
    head: str = "",
    in_flight: Optional[list] = None,
    core_version: str = "",
    config: Optional[dict] = None,
) -> int:
    """Create one session record at startup and return its ID (§10.5)."""
    sid = next_session_id(conn)
    ts = now_iso()
    conn.execute(
        "INSERT INTO sessions(session_id, name, branch, tmux, flow, agent, task, status, "
        "head, in_flight, core_version, config, created_ts, updated_ts) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sid, name, branch, tmux, flow, agent, task, status, head,
            json.dumps(list(in_flight or []), ensure_ascii=False),
            core_version,
            json.dumps(dict(config or {}), ensure_ascii=False),
            ts, ts,
        ),
    )
    conn.commit()
    return sid


_SESSION_UPDATABLE = {
    "name", "branch", "tmux", "flow", "agent", "task", "status",
    "head", "in_flight", "core_version", "config", "handoff",
}


def update_session(conn: sqlite3.Connection, session_id: int, **fields) -> None:
    """Update allowed session fields and refresh ``updated_ts``.

    Sessions are mutable state; append-only protection applies only to runs [D3].
    """
    sets, vals = [], []
    for key, val in fields.items():
        if key not in _SESSION_UPDATABLE:
            raise ValueError(f"unknown or immutable session field: {key}")
        if key == "in_flight":
            val = json.dumps(list(val or []), ensure_ascii=False)
        elif key == "config":
            val = json.dumps(dict(val or {}), ensure_ascii=False)
        sets.append(f"{key} = ?")
        vals.append(val)
    sets.append("updated_ts = ?")
    vals.append(now_iso())
    vals.append(session_id)
    conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE session_id = ?", vals)
    conn.commit()


def get_session(conn: sqlite3.Connection, session_id: int) -> Optional[dict]:
    d = _row(conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone())
    if d is not None:
        d["in_flight"] = _loads(d.get("in_flight"), [])
        d["config"] = _loads(d.get("config"), {})
    return d


def last_session_handoff(conn: sqlite3.Connection) -> str:
    """Return the most recently updated nonempty handoff, or an empty string.

    ``updated_ts`` determines recency because an older session may finish after a
    newer one. ``session_id`` breaks timestamp ties.
    """
    row = conn.execute(
        "SELECT handoff FROM sessions WHERE handoff != '' "
        "ORDER BY updated_ts DESC, session_id DESC LIMIT 1").fetchone()
    return row[0] if row is not None else ""


def list_sessions(conn: sqlite3.Connection) -> List[dict]:
    out = []
    for row in conn.execute("SELECT * FROM sessions ORDER BY session_id"):
        d = _row(row)
        d["in_flight"] = _loads(d.get("in_flight"), [])
        d["config"] = _loads(d.get("config"), {})
        out.append(d)
    return out


# ----------------------------------------------------------------------------- runs (§8.1) APPEND-ONLY [D3]
def log_run(
    conn: sqlite3.Connection,
    *,
    session_id: Optional[int] = None,
    kind: str = "invoke",
    agent: str = "",
    adapter: str = "",
    phase: str = "",
    prompt: str = "",
    result: str = "",
    status: str = "",
    ok: bool = False,
    cost: Optional[float] = None,
    cost_origin: str = "",
    resume_id: str = "",
    meta: Optional[dict] = None,
    transcript: str = "",
) -> int:
    """Append an invocation to the audit log and return its run ID [D3].

    Schema triggers prohibit updates and deletes. Prompt, result, and harness
    transcript are stored in full [D11].
    """
    cur = conn.execute(
        "INSERT INTO runs(session_id, ts, kind, agent, adapter, phase, prompt, result, "
        "status, ok, cost, cost_origin, resume_id, meta, transcript) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            session_id, now_iso(), kind, agent, adapter, phase, prompt, result,
            status, 1 if ok else 0, cost, cost_origin, resume_id,
            json.dumps(dict(meta or {}), ensure_ascii=False),
            transcript or "",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[dict]:
    d = _row(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())
    if d is not None:
        d["meta"] = _loads(d.get("meta"), {})
    return d


def list_runs(conn: sqlite3.Connection, session_id: Optional[int] = None) -> List[dict]:
    """List complete run records in chronological order.

    Use the lightweight helpers on hot paths to avoid loading large text fields.
    """
    if session_id is None:
        rows = conn.execute("SELECT * FROM runs ORDER BY run_id")
    else:
        rows = conn.execute("SELECT * FROM runs WHERE session_id = ? ORDER BY run_id", (session_id,))
    out = []
    for row in rows:
        d = _row(row)
        d["meta"] = _loads(d.get("meta"), {})
        out.append(d)
    return out


# Lightweight run queries project in SQL so hot-path counters and journals do not
# load transcripts or hold write locks while large append-only histories grow.

def count_runs(conn: sqlite3.Connection, session_id: Optional[int] = None, *,
               kind: Optional[str] = None, agents=None) -> int:
    """Count matching run rows in SQL without loading them."""
    q, args = "SELECT COUNT(*) FROM runs WHERE 1=1", []
    if session_id is not None:
        q += " AND session_id = ?"
        args.append(session_id)
    if kind is not None:
        q += " AND kind = ?"
        args.append(kind)
    if agents:
        q += f" AND agent IN ({', '.join('?' * len(agents))})"
        args.extend(agents)
    return int(conn.execute(q, args).fetchone()[0])


def sum_run_cost(conn: sqlite3.Connection, session_id: Optional[int] = None) -> float:
    """Sum recorded dollar cost in SQL, treating NULL as zero."""
    if session_id is None:
        row = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM runs").fetchone()
    else:
        row = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM runs WHERE session_id = ?",
                           (session_id,)).fetchone()
    return float(row[0])


def last_run(conn: sqlite3.Connection, *, session_id: Optional[int] = None,
             kind: Optional[str] = None) -> Optional[dict]:
    """Return the latest complete matching run, or ``None``."""
    q, args = "SELECT * FROM runs WHERE 1=1", []
    if session_id is not None:
        q += " AND session_id = ?"
        args.append(session_id)
    if kind is not None:
        q += " AND kind = ?"
        args.append(kind)
    d = _row(conn.execute(q + " ORDER BY run_id DESC LIMIT 1", args).fetchone())
    if d is not None:
        d["meta"] = _loads(d.get("meta"), {})
    return d


# Keep enough result text for journals and briefs without loading full blobs.
RESULT_PREVIEW_CHARS = 500


def list_runs_light(conn: sqlite3.Connection, session_id: Optional[int] = None) -> List[dict]:
    """List run metadata and a result preview without loading prompt or transcript blobs."""
    proj = ("SELECT run_id, session_id, ts, kind, agent, adapter, phase, status, ok, cost, "
            f"cost_origin, resume_id, substr(result, 1, {RESULT_PREVIEW_CHARS}) AS result FROM runs")
    if session_id is None:
        rows = conn.execute(proj + " ORDER BY run_id")
    else:
        rows = conn.execute(proj + " WHERE session_id = ? ORDER BY run_id", (session_id,))
    return [_row(row) for row in rows]


# Cross-process spawn reservations [F7].
def count_active_reservations(conn: sqlite3.Connection, session_id: int) -> int:
    """Count active spawn reservations for a session."""
    row = conn.execute(
        "SELECT COUNT(*) FROM spawn_reservations WHERE session_id = ?", (session_id,)
    ).fetchone()
    return int(row[0])


# A process may die after committing a reservation but before releasing it. Sweep
# reservations older than a TTL well above the normal invocation timeout.
RESERVATION_TTL_SECONDS = 1800


def sweep_stale_reservations(conn: sqlite3.Connection, session_id: int,
                             *, ttl_seconds: int = RESERVATION_TTL_SECONDS) -> int:
    """Delete stale reservations for a session and return the number removed."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    cur = conn.execute(
        "DELETE FROM spawn_reservations WHERE session_id = ? AND created_ts < ?", (session_id, cutoff))
    return cur.rowcount


def try_reserve_spawns(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    slots: int,
    count_committed,
    max_spawns: int,
    partial: bool = False,
) -> List[int]:
    """Atomically claim spawn slots below the session cap [F7].

    ``BEGIN IMMEDIATE`` serializes cross-process check-and-claim operations. The
    committed count and active reservations are read under the same lock after
    stale reservations are swept. Return reservation IDs for release in ``finally``;
    ``partial=False`` is all-or-nothing, while ``True`` grants available capacity.
    """
    if conn.in_transaction:           # ``BEGIN`` inside a transaction would fail.
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        sweep_stale_reservations(conn, session_id)            # Sweep dead-process reservations under lock.
        taken = int(count_committed(conn)) + count_active_reservations(conn, session_id)
        free = max(0, max_spawns - taken)
        grant = min(slots, free) if partial else (slots if free >= slots else 0)
        ids: List[int] = []
        ts = now_iso()
        for _ in range(grant):
            cur = conn.execute(
                "INSERT INTO spawn_reservations(session_id, created_ts) VALUES(?, ?)",
                (session_id, ts),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
        return ids
    except Exception:
        conn.rollback()
        raise


def release_spawns(conn: sqlite3.Connection, res_ids: List[int]) -> None:
    """Release reservations after invocation; repeated release is a no-op."""
    if not res_ids:
        return
    conn.executemany("DELETE FROM spawn_reservations WHERE res_id = ?", [(i,) for i in res_ids])
    conn.commit()


# ----------------------------------------------------------------------------- lessons (§8.2) append+dedup [D3]
def add_lesson(
    conn: sqlite3.Connection,
    *,
    text: str,
    session_id: Optional[int] = None,
    kind: str = "free",
    feature: str = "",
    ground_truth: bool = False,
    status: str = "active",
) -> int:
    """Add or deduplicate an active-memory lesson [D3].

    Verified ground truth promotes a matching pending or retired lesson back to
    active and preserves its authoritative label [D1/D4]. An already active match
    remains an idempotent no-op. Sanitization and status policy belong to
    ``memory.store``; this layer only persists their decisions.
    """
    existing = conn.execute(
        "SELECT lesson_id, status FROM lessons WHERE text = ? AND kind = ?", (text, kind)
    ).fetchone()
    if existing is not None:
        lesson_id = int(existing["lesson_id"])
        if ground_truth and existing["status"] != "active":
            # Refresh the feature too, so the manifest cannot retain a stale
            # reference to a superseded pending lesson.
            if feature:
                conn.execute(
                    "UPDATE lessons SET status='active', ground_truth=1, feature=?, updated_ts=? "
                    "WHERE lesson_id=?", (feature, now_iso(), lesson_id))
            else:
                conn.execute(
                    "UPDATE lessons SET status='active', ground_truth=1, updated_ts=? "
                    "WHERE lesson_id=?", (now_iso(), lesson_id))
            conn.commit()
        return lesson_id
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO lessons(session_id, ts, kind, text, feature, ground_truth, status, "
        "created_ts, updated_ts) VALUES(?,?,?,?,?,?,?,?,?)",
        (session_id, ts, kind, text, feature, 1 if ground_truth else 0, status, ts, ts),
    )
    conn.commit()
    return int(cur.lastrowid)


def set_lesson_status(conn: sqlite3.Connection, lesson_id: int, status: str) -> None:
    """Change lesson status for decay or consolidation [D4]."""
    conn.execute(
        "UPDATE lessons SET status = ?, updated_ts = ? WHERE lesson_id = ?",
        (status, now_iso(), lesson_id),
    )
    conn.commit()


def get_lesson(conn: sqlite3.Connection, lesson_id: int) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM lessons WHERE lesson_id = ?", (lesson_id,)).fetchone())


def list_lessons(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    session_id: Optional[int] = None,
    ground_truth: Optional[bool] = None,
) -> List[dict]:
    """List filtered lessons in ID order; ``None`` disables a filter."""
    clauses, vals = [], []
    if status is not None:
        clauses.append("status = ?"); vals.append(status)
    if kind is not None:
        clauses.append("kind = ?"); vals.append(kind)
    if session_id is not None:
        clauses.append("session_id = ?"); vals.append(session_id)
    if ground_truth is not None:
        clauses.append("ground_truth = ?"); vals.append(1 if ground_truth else 0)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(f"SELECT * FROM lessons{where} ORDER BY lesson_id", vals)
    return [_row(r) for r in rows]


# ----------------------------------------------------------------------------- continuum (v4, plan §10)
# Mutate continuum records through these append/supersede helpers rather than raw SQL.

def add_decision(conn: sqlite3.Connection, *, text: str, session_id: Optional[int] = None,
                 feature: str = "") -> int:
    """Record a durable decision and return its ID."""
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO decisions(session_id, ts, text, feature, created_ts, updated_ts) "
        "VALUES(?,?,?,?,?,?)", (session_id, ts, text, feature, ts, ts))
    conn.commit()
    return int(cur.lastrowid)


def get_decision(conn: sqlite3.Connection, decision_id: int) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)).fetchone())


def list_decisions(conn: sqlite3.Connection, *, status: Optional[str] = "active",
                   feature: Optional[str] = None) -> List[dict]:
    """List decisions, active by default; ``status=None`` includes history."""
    clauses, vals = [], []
    if status is not None:
        clauses.append("status = ?"); vals.append(status)
    if feature is not None:
        clauses.append("feature = ?"); vals.append(feature)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return [_row(r) for r in conn.execute(f"SELECT * FROM decisions{where} ORDER BY decision_id", vals)]


def supersede_decision(conn: sqlite3.Connection, decision_id: int, *, reason: str = "",
                       superseded_by: Optional[int] = None) -> None:
    """Supersede a decision without deleting history; reject unknown IDs."""
    if get_decision(conn, decision_id) is None:
        raise ValueError(f"decision #{decision_id} does not exist")
    conn.execute(
        "UPDATE decisions SET status='superseded', reason=?, superseded_by=?, updated_ts=? "
        "WHERE decision_id=?", (reason, superseded_by, now_iso(), decision_id))
    conn.commit()


def add_board_item(conn: sqlite3.Connection, *, text: str, session_id: Optional[int] = None,
                   feature: str = "", needs_human: bool = False) -> int:
    """Add a board item, optionally blocked on human input."""
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO board(session_id, ts, text, feature, needs_human, created_ts, updated_ts) "
        "VALUES(?,?,?,?,?,?,?)", (session_id, ts, text, feature, 1 if needs_human else 0, ts, ts))
    conn.commit()
    return int(cur.lastrowid)


def get_board_item(conn: sqlite3.Connection, item_id: int) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM board WHERE item_id = ?", (item_id,)).fetchone())


def list_board(conn: sqlite3.Connection, *, status: Optional[str] = "open",
               needs_human: Optional[bool] = None) -> List[dict]:
    """List board items, open by default, with an optional human-dependency filter."""
    clauses, vals = [], []
    if status is not None:
        clauses.append("status = ?"); vals.append(status)
    if needs_human is not None:
        clauses.append("needs_human = ?"); vals.append(1 if needs_human else 0)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return [_row(r) for r in conn.execute(f"SELECT * FROM board{where} ORDER BY item_id", vals)]


def set_board_status(conn: sqlite3.Connection, item_id: int, status: str) -> None:
    """Set a board item's status, rejecting unknown IDs."""
    if get_board_item(conn, item_id) is None:
        raise ValueError(f"board item #{item_id} does not exist")
    conn.execute("UPDATE board SET status = ?, updated_ts = ? WHERE item_id = ?",
                 (status, now_iso(), item_id))
    conn.commit()


def add_message(conn: sqlite3.Connection, *, direction: str, text: str, kind: str = "info",
                session_id: Optional[int] = None, ref_id: Optional[int] = None,
                channel: str = "", status: str = "queued") -> int:
    """Record a human/rig message, validating any referenced question."""
    if ref_id is not None and get_message(conn, ref_id) is None:
        raise ValueError(f"message #{ref_id} does not exist; an answer cannot reference it")
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO messages(session_id, ts, direction, kind, ref_id, text, channel, status, "
        "created_ts, updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (session_id, ts, direction, kind, ref_id, text, channel, status, ts, ts))
    conn.commit()
    return int(cur.lastrowid)


def get_message(conn: sqlite3.Connection, msg_id: int) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM messages WHERE msg_id = ?", (msg_id,)).fetchone())


def list_messages(conn: sqlite3.Connection, *, direction: Optional[str] = None,
                  kind: Optional[str] = None, status: Optional[str] = None,
                  session_id: Optional[int] = None) -> List[dict]:
    clauses, vals = [], []
    if direction is not None:
        clauses.append("direction = ?"); vals.append(direction)
    if kind is not None:
        clauses.append("kind = ?"); vals.append(kind)
    if status is not None:
        clauses.append("status = ?"); vals.append(status)
    if session_id is not None:
        clauses.append("session_id = ?"); vals.append(session_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return [_row(r) for r in conn.execute(f"SELECT * FROM messages{where} ORDER BY msg_id", vals)]


def open_questions(conn: sqlite3.Connection) -> List[dict]:
    """List outbound questions that have no inbound answer (plan §10)."""
    rows = conn.execute(
        "SELECT * FROM messages q WHERE q.direction='out' AND q.kind='question' AND NOT EXISTS "
        "(SELECT 1 FROM messages a WHERE a.direction='in' AND a.kind='answer' AND a.ref_id = q.msg_id) "
        "ORDER BY q.msg_id")
    return [_row(r) for r in rows]


def unread_inbound(conn: sqlite3.Connection) -> List[dict]:
    """List unread inbound messages with the referenced question text."""
    rows = conn.execute(
        "SELECT m.*, q.text AS ref_text FROM messages m "
        "LEFT JOIN messages q ON q.msg_id = m.ref_id "
        "WHERE m.direction='in' AND m.status != 'read' ORDER BY m.msg_id")
    return [_row(r) for r in rows]


def mark_messages(conn: sqlite3.Connection, msg_ids: List[int], status: str) -> None:
    """Mark messages with a delivery state; an empty ID list is a no-op."""
    if not msg_ids:
        return
    ts = now_iso()
    conn.executemany("UPDATE messages SET status = ?, updated_ts = ? WHERE msg_id = ?",
                     [(status, ts, i) for i in msg_ids])
    conn.commit()


def put_artifact(conn: sqlite3.Connection, *, name: str, text: str, kind: str = "",
                 session_id: Optional[int] = None, feature: str = "") -> int:
    """Store a new active artifact version and supersede the previous one atomically."""
    ts = now_iso()
    conn.execute("UPDATE artifacts SET status='superseded', updated_ts=? "
                 "WHERE name=? AND status='active'", (ts, name))
    cur = conn.execute(
        "INSERT INTO artifacts(session_id, ts, name, kind, text, feature, created_ts, updated_ts) "
        "VALUES(?,?,?,?,?,?,?,?)", (session_id, ts, name, kind, text, feature, ts, ts))
    conn.commit()
    return int(cur.lastrowid)


def get_artifact(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    """Return the latest active artifact with this name, including its full body."""
    return _row(conn.execute(
        "SELECT * FROM artifacts WHERE name=? AND status='active' "
        "ORDER BY artifact_id DESC LIMIT 1", (name,)).fetchone())


def drop_artifact(conn: sqlite3.Connection, name: str) -> None:
    """Supersede an active artifact while retaining its version history."""
    if get_artifact(conn, name) is None:
        raise ValueError(f"artifact {name!r} does not exist or is already superseded")
    conn.execute("UPDATE artifacts SET status='superseded', updated_ts=? "
                 "WHERE name=? AND status='active'", (now_iso(), name))
    conn.commit()


def list_artifacts(conn: sqlite3.Connection, *, kind: Optional[str] = None) -> List[dict]:
    """List active artifact metadata without loading bodies."""
    q = ("SELECT artifact_id, session_id, ts, name, kind, feature, status, length(text) AS size "
         "FROM artifacts WHERE status='active'")
    vals: List = []
    if kind is not None:
        q += " AND kind = ?"; vals.append(kind)
    return [_row(r) for r in conn.execute(q + " ORDER BY name", vals)]


# ----------------------------------------------------------------------------- observability (v5, plan §10 stage 2)

def log_event(conn: sqlite3.Connection, *, actor: str = "", kind: str = "", name: str = "",
              session_id: Optional[int] = None, params: Optional[dict] = None,
              status: str = "", duration_ms: Optional[int] = None) -> int:
    """Append an engine-boundary event; agent invocations belong in ``runs``."""
    blob = json.dumps(dict(params or {}), ensure_ascii=False)
    if len(blob) > 2000:                       # Replace oversized JSON rather than slicing it into invalid syntax.
        blob = json.dumps({"_truncated": len(blob)}, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO events(session_id, ts, actor, kind, name, params, status, duration_ms) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (session_id, now_iso(), actor, kind, name, blob, status, duration_ms))
    conn.commit()
    return int(cur.lastrowid)


def list_events(conn: sqlite3.Connection, *, session_id: Optional[int] = None,
                kind: Optional[str] = None, limit: int = 200) -> List[dict]:
    """Return the latest timeline entries in chronological order with decoded params."""
    clauses, vals = [], []
    if session_id is not None:
        clauses.append("session_id = ?"); vals.append(session_id)
    if kind is not None:
        clauses.append("kind = ?"); vals.append(kind)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    vals.append(max(1, int(limit)))
    rows = conn.execute(
        f"SELECT * FROM (SELECT * FROM events{where} ORDER BY event_id DESC LIMIT ?) "
        "ORDER BY event_id", vals)
    out = []
    for r in rows:
        d = _row(r)
        d["params"] = _loads(d.get("params"), {})
        out.append(d)
    return out


# A liveness row spans an invocation; the caller refreshes it every ``BEAT_SECONDS``.
BEAT_SECONDS = 20
STALL_AFTER_SECONDS = 90          # More than four missed beats suggests a dead caller.
LIVENESS_TTL_SECONDS = 21600      # Prune abandoned rows after six hours.


def liveness_start(conn: sqlite3.Connection, *, session_id: Optional[int] = None, role: str = "",
                   phase: str = "", adapter: str = "", label: str = "",
                   pid: Optional[int] = None) -> int:
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO liveness(session_id, role, phase, adapter, label, pid, started_ts, last_beat_ts) "
        "VALUES(?,?,?,?,?,?,?,?)", (session_id, role, phase, adapter, label, pid, ts, ts))
    conn.commit()
    return int(cur.lastrowid)


def liveness_beat(conn: sqlite3.Connection, live_id: int) -> None:
    """Refresh a heartbeat using the beat thread's dedicated connection."""
    conn.execute("UPDATE liveness SET last_beat_ts = ? WHERE live_id = ?", (now_iso(), live_id))
    conn.commit()


def liveness_end(conn: sqlite3.Connection, live_id: int) -> None:
    """Remove a completed invocation's liveness row idempotently."""
    conn.execute("DELETE FROM liveness WHERE live_id = ?", (live_id,))
    conn.commit()


def mark_liveness_stalled(conn: sqlite3.Connection, live_id: int) -> None:
    """Mark a stall as reported so repeated readers do not emit duplicates."""
    conn.execute("UPDATE liveness SET stalled = 1 WHERE live_id = ?", (live_id,))
    conn.commit()


def list_liveness(conn: sqlite3.Connection, *, session_id: Optional[int] = None) -> List[dict]:
    """List live invocations with heartbeat age and prune expired rows.

    Before pruning an unreported stale row, append a stall event so evidence is not
    lost. Deletion runs only when the stale-row query finds work, avoiding needless
    write locks on the polling path.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=LIVENESS_TTL_SECONDS)).isoformat(timespec="seconds")
    stale = conn.execute("SELECT live_id, session_id, role, phase, label, stalled FROM liveness "
                         "WHERE last_beat_ts < ?", (cutoff,)).fetchall()
    if stale:
        for r in stale:
            if not r["stalled"]:               # Preserve unreported failure evidence in the event log.
                who = "/".join(x for x in (r["role"], r["phase"]) if x) or r["label"]
                log_event(conn, kind="stall", name=who, session_id=r["session_id"],
                          params={"reaped": True, "live_id": r["live_id"]})
        conn.executemany("DELETE FROM liveness WHERE live_id = ?", [(r["live_id"],) for r in stale])
        conn.commit()
    q = ("SELECT *, CAST((julianday(?) - julianday(last_beat_ts)) * 86400 AS INTEGER) AS beat_age_s "
         "FROM liveness")
    vals: List = [now_iso()]
    if session_id is not None:
        q += " WHERE session_id = ?"; vals.append(session_id)
    return [_row(r) for r in conn.execute(q + " ORDER BY live_id", vals)]


# ----------------------------------------------------------------------------- manifest (recall-index)
def upsert_manifest(
    conn: sqlite3.Connection,
    *,
    name: str,
    summary: str = "",
    kind: str = "",
    feature: str = "",
    ref_table: str = "",
    ref_id: Optional[int] = None,
) -> int:
    """Insert or refresh a manifest entry and return its ID."""
    ts = now_iso()
    conn.execute(
        "INSERT INTO manifest(name, summary, kind, feature, ref_table, ref_id, created_ts, updated_ts) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET summary=excluded.summary, kind=excluded.kind, "
        "feature=excluded.feature, ref_table=excluded.ref_table, ref_id=excluded.ref_id, "
        "updated_ts=excluded.updated_ts",
        (name, summary, kind, feature, ref_table, ref_id, ts, ts),
    )
    conn.commit()
    row = conn.execute("SELECT manifest_id FROM manifest WHERE name = ?", (name,)).fetchone()
    return int(row[0])


def get_manifest(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    return _row(conn.execute("SELECT * FROM manifest WHERE name = ?", (name,)).fetchone())


def delete_manifest(conn: sqlite3.Connection, name: str) -> None:
    """Remove a stale manifest entry; unknown names are a no-op."""
    conn.execute("DELETE FROM manifest WHERE name = ?", (name,))
    conn.commit()


def list_manifest(conn: sqlite3.Connection) -> List[dict]:
    return [_row(r) for r in conn.execute("SELECT * FROM manifest ORDER BY name")]
