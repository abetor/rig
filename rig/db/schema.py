"""SQLite schema for sessions, runs, memory, and orchestration state.

Sessions hold mutable execution state and handoffs. Runs and events are physically
append-only audit logs [D3]. Lessons provide typed active memory with lifecycle
operations in ``memory.store`` [D1/D4]. Decisions, board items, messages, and
artifacts form the cross-session continuum. Engine-controlled enums use shared
CHECK constraints; captured or extensible values remain unconstrained. Migrations
own schema versioning [E5], and secrets are never stored here [D1].
"""
from __future__ import annotations

# Version described by this file; migrate.py upgrades databases to it [E5].
SCHEMA_VERSION = 5

# Shared enum values used by schema constraints and validation code.
# Session lifecycle.
SESSION_STATUS = ("active", "done", "need_human", "failed", "partial")
# Passive run kinds (§8.1, §8.3).
RUN_KIND = ("invoke", "session_start", "plan", "note")
# Honest cost provenance [G4]; intentionally extensible and not constrained.
COST_ORIGIN = ("", "exact", "estimated", "no_pricing")
# Active-memory lesson kinds (§8.2). ``feature`` and ``techdebt`` are currently
# software-oriented; making this fully neutral requires a schema migration.
LESSON_KIND = ("principle", "feedback", "feature", "techdebt", "free")
# Lesson lifecycle [D1/D4].
LESSON_STATUS = ("active", "pending", "retired")
# --- continuum (v4, plan §10) ---
# Decisions are superseded rather than deleted so history remains intact.
DECISION_STATUS = ("active", "superseded")
# Board-item lifecycle.
BOARD_STATUS = ("open", "done", "dropped")
# Message direction relative to rig.
MESSAGE_DIRECTION = ("out", "in")
# Message kinds; answers reference their questions.
MESSAGE_KIND = ("question", "info", "answer", "feedback")
# Message delivery and read state.
MESSAGE_STATUS = ("queued", "sent", "read")
# Artifact-version lifecycle.
ARTIFACT_STATUS = ("active", "superseded")
# --- observability (v5, plan §10 stage 2) ---
# Event kinds are documented but intentionally extensible and unconstrained.
EVENT_KIND = ("tool_call", "flow_phase", "gate", "notify", "boot", "session", "stall")


def _in(values) -> str:
    """Render trusted enum values as an SQL list for CHECK constraints."""
    return ", ".join("'" + v + "'" for v in values)


# Idempotent table DDL.

_SESSIONS = f"""
CREATE TABLE IF NOT EXISTS sessions (
    session_id   INTEGER PRIMARY KEY,                 -- Monotonic max+1 within a project (§10.4).
    name         TEXT    NOT NULL,                    -- one name = branch = tmux (§7.2)
    branch       TEXT    NOT NULL DEFAULT '',
    tmux         TEXT    NOT NULL DEFAULT '',
    flow         TEXT    NOT NULL DEFAULT '',          -- Flow name (§10.3).
    agent        TEXT    NOT NULL DEFAULT '',          -- Agent or adapter (§10.3).
    task         TEXT    NOT NULL DEFAULT '',          -- Task text (§10.3).
    status       TEXT    NOT NULL DEFAULT 'active'
                 CHECK(status IN ({_in(SESSION_STATUS)})),
    head         TEXT    NOT NULL DEFAULT '',          -- HEAD at session end, used to detect divergence [E3].
    in_flight    TEXT    NOT NULL DEFAULT '[]',        -- JSON list of unfinished branches [E3].
    core_version TEXT    NOT NULL DEFAULT '',          -- Core and schema version marker [E5].
    config       TEXT    NOT NULL DEFAULT '{{}}',       -- Session configuration as JSON (§7.2).
    handoff      TEXT    NOT NULL DEFAULT '',           -- Complete session handoff (v4, plan §10).
    created_ts   TEXT    NOT NULL,
    updated_ts   TEXT    NOT NULL
)
"""

# ``runs.session_id`` is nullable so infrastructure calls can be logged outside a session.
_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY,                   -- Monotonic; deletion is blocked by triggers.
    session_id  INTEGER REFERENCES sessions(session_id),
    ts          TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'invoke',     -- Run class (§8.1/§8.3).
    agent       TEXT    NOT NULL DEFAULT '',           -- Agent role.
    adapter     TEXT    NOT NULL DEFAULT '',           -- CLI adapter.
    phase       TEXT    NOT NULL DEFAULT '',           -- Flow phase used for routing (§8.3).
    prompt      TEXT    NOT NULL DEFAULT '',           -- Full input prompt (§8.1).
    result      TEXT    NOT NULL DEFAULT '',           -- Full output result (§8.1).
    status      TEXT    NOT NULL DEFAULT '',           -- Captured parsed status.
    ok          INTEGER NOT NULL DEFAULT 0,            -- Honest parser result [G2].
    cost        REAL,                                  -- NULL when pricing is unavailable [G4].
    cost_origin TEXT    NOT NULL DEFAULT '',           -- exact|estimated|no_pricing [G4].
    resume_id   TEXT    NOT NULL DEFAULT '',           -- Native agent resume ID (§10.4) [G2].
    meta        TEXT    NOT NULL DEFAULT '{}',         -- Extensible JSON metadata.
    transcript  TEXT    NOT NULL DEFAULT ''            -- Native harness event stream [D11].
)
"""

_LESSONS = f"""
CREATE TABLE IF NOT EXISTS lessons (
    lesson_id    INTEGER PRIMARY KEY,
    session_id   INTEGER REFERENCES sessions(session_id),  -- Session provenance [D5].
    ts           TEXT    NOT NULL,
    kind         TEXT    NOT NULL DEFAULT 'free'
                 CHECK(kind IN ({_in(LESSON_KIND)})),
    text         TEXT    NOT NULL,                      -- Lesson body, sanitized by store.py [D2].
    feature      TEXT    NOT NULL DEFAULT '',           -- Optional recall feature.
    ground_truth INTEGER NOT NULL DEFAULT 0,            -- 1 means a gate FAIL supplied evidence [D1].
    status       TEXT    NOT NULL DEFAULT 'active'
                 CHECK(status IN ({_in(LESSON_STATUS)})),  -- active/pending/retired [D1/D4].
    created_ts   TEXT    NOT NULL,
    updated_ts   TEXT    NOT NULL
)
"""

# Recall manifest: known names mapped to concise summaries.
_MANIFEST = """
CREATE TABLE IF NOT EXISTS manifest (
    manifest_id INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,               -- Known name used by validated recall.
    summary     TEXT    NOT NULL DEFAULT '',           -- One-line index summary.
    kind        TEXT    NOT NULL DEFAULT '',
    feature     TEXT    NOT NULL DEFAULT '',
    ref_table   TEXT    NOT NULL DEFAULT '',           -- Optional source table.
    ref_id      INTEGER,
    created_ts  TEXT    NOT NULL,
    updated_ts  TEXT    NOT NULL
)
"""

# Mutable in-flight slots for cross-process spawn limits [F7]. Reservations are
# claimed atomically before invocation and released after the run row is committed.
_SPAWN_RESERVATIONS = """
CREATE TABLE IF NOT EXISTS spawn_reservations (
    res_id      INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(session_id),
    created_ts  TEXT    NOT NULL
)
"""

# --- continuum (v4, plan §10): decisions / board / messages / artifacts ---
# These records mutate only through DAO status and supersede operations. Nullable
# session IDs allow human and maintenance writes outside a session.

_DECISIONS = f"""
CREATE TABLE IF NOT EXISTS decisions (
    decision_id   INTEGER PRIMARY KEY,
    session_id    INTEGER REFERENCES sessions(session_id),
    ts            TEXT    NOT NULL,
    text          TEXT    NOT NULL,                    -- Decision and rationale.
    feature       TEXT    NOT NULL DEFAULT '',         -- Optional recall feature.
    status        TEXT    NOT NULL DEFAULT 'active'
                  CHECK(status IN ({_in(DECISION_STATUS)})),
    superseded_by INTEGER REFERENCES decisions(decision_id),  -- Replacement decision, if any.
    reason        TEXT    NOT NULL DEFAULT '',         -- Reason for superseding.
    created_ts    TEXT    NOT NULL,
    updated_ts    TEXT    NOT NULL
)
"""

_BOARD = f"""
CREATE TABLE IF NOT EXISTS board (
    item_id     INTEGER PRIMARY KEY,
    session_id  INTEGER REFERENCES sessions(session_id),
    ts          TEXT    NOT NULL,
    text        TEXT    NOT NULL,                      -- Remaining work and unblock conditions.
    feature     TEXT    NOT NULL DEFAULT '',
    needs_human INTEGER NOT NULL DEFAULT 0,            -- 1 means autonomous execution is blocked.
    status      TEXT    NOT NULL DEFAULT 'open'
                CHECK(status IN ({_in(BOARD_STATUS)})),
    created_ts  TEXT    NOT NULL,
    updated_ts  TEXT    NOT NULL
)
"""

_MESSAGES = f"""
CREATE TABLE IF NOT EXISTS messages (
    msg_id     INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(session_id),
    ts         TEXT    NOT NULL,
    direction  TEXT    NOT NULL
               CHECK(direction IN ({_in(MESSAGE_DIRECTION)})),
    kind       TEXT    NOT NULL DEFAULT 'info'
               CHECK(kind IN ({_in(MESSAGE_KIND)})),
    ref_id     INTEGER REFERENCES messages(msg_id),    -- Question referenced by an answer.
    text       TEXT    NOT NULL,
    channel    TEXT    NOT NULL DEFAULT '',            -- Delivery transport.
    status     TEXT    NOT NULL DEFAULT 'queued'
               CHECK(status IN ({_in(MESSAGE_STATUS)})),
    created_ts TEXT    NOT NULL,
    updated_ts TEXT    NOT NULL
)
"""

# Artifact kinds are content conventions rather than engine enums [§9.1].
_ARTIFACTS = f"""
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id INTEGER PRIMARY KEY,
    session_id  INTEGER REFERENCES sessions(session_id),
    ts          TEXT    NOT NULL,
    name        TEXT    NOT NULL,                      -- Logical name; reads return its latest active version.
    kind        TEXT    NOT NULL DEFAULT '',           -- Unconstrained content class.
    text        TEXT    NOT NULL,                      -- Markdown body.
    feature     TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'active'
                CHECK(status IN ({_in(ARTIFACT_STATUS)})),
    created_ts  TEXT    NOT NULL,
    updated_ts  TEXT    NOT NULL
)
"""

# --- observability (v5, plan §10 stage 2): events / liveness ---
# Events record engine boundaries, tools, gates, and boot activity. Agent calls stay
# in ``runs``. Events are append-only and written fail-soft through ``core.events``.
_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY,
    session_id  INTEGER REFERENCES sessions(session_id),
    ts          TEXT    NOT NULL,
    actor       TEXT    NOT NULL DEFAULT '',            -- Extensible actor name.
    kind        TEXT    NOT NULL DEFAULT '',            -- Documented by EVENT_KIND, not constrained.
    name        TEXT    NOT NULL DEFAULT '',            -- Subcommand, phase, or channel.
    params      TEXT    NOT NULL DEFAULT '{}',          -- Small JSON parameters, never prompt blobs.
    status      TEXT    NOT NULL DEFAULT '',            -- Exit status, green state, or verdict.
    duration_ms INTEGER                                 -- NULL for instantaneous events.
)
"""

# Liveness is a mutable heartbeat ledger, not an agent decision. Each row spans an
# invocation; stale beats let the viewer report a stall once.
_LIVENESS = """
CREATE TABLE IF NOT EXISTS liveness (
    live_id      INTEGER PRIMARY KEY,
    session_id   INTEGER REFERENCES sessions(session_id),
    role         TEXT    NOT NULL DEFAULT '',           -- In-flight invocation role.
    phase        TEXT    NOT NULL DEFAULT '',
    adapter      TEXT    NOT NULL DEFAULT '',
    label        TEXT    NOT NULL DEFAULT '',           -- sync | parallel×N
    pid          INTEGER,                               -- Caller process ID.
    stalled      INTEGER NOT NULL DEFAULT 0,            -- 1 after a stall has been reported.
    started_ts   TEXT    NOT NULL,
    last_beat_ts TEXT    NOT NULL
)
"""

# --- index ---
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_lessons_session ON lessons(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_lessons_status ON lessons(status)",
    "CREATE INDEX IF NOT EXISTS idx_reservations_session ON spawn_reservations(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status)",
    "CREATE INDEX IF NOT EXISTS idx_board_status ON board(status)",
    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_dir_status ON messages(direction, status)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_name ON artifacts(name, status)",
    "CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_liveness_session ON liveness(session_id)",
)

# Physical append-only enforcement for runs [D3] and events.
_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS runs_block_update
    BEFORE UPDATE ON runs
    BEGIN
        SELECT RAISE(ABORT, 'runs is append-only [D3]');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS runs_block_delete
    BEFORE DELETE ON runs
    BEGIN
        SELECT RAISE(ABORT, 'runs is append-only [D3]');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS events_block_update
    BEFORE UPDATE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events is append-only [D3]');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS events_block_delete
    BEFORE DELETE ON events
    BEGIN
        SELECT RAISE(ABORT, 'events is append-only [D3]');
    END
    """,
)


def table_statements() -> list:
    """Return table DDL; migration code owns the metadata table."""
    return [_SESSIONS, _RUNS, _LESSONS, _MANIFEST, _SPAWN_RESERVATIONS,
            _DECISIONS, _BOARD, _MESSAGES, _ARTIFACTS, _EVENTS, _LIVENESS]


def index_statements() -> list:
    return list(_INDEXES)


def trigger_statements() -> list:
    """Return append-only triggers for runs and events [D3]."""
    return list(_TRIGGERS)


def create_statements() -> list:
    """Return complete idempotent DDL for the current schema version."""
    return table_statements() + index_statements() + trigger_statements()
