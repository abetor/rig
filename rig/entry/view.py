"""Read-only project, session, run, event, and engine-state viewer."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from typing import Dict, List, Optional, Tuple

from ..db import convention
from ..db.schema import RUN_KIND

MAX_SQL_ROWS = 500
MAX_CAT_BYTES = 5_000_000
SCAN_MAX_DEPTH = 4           # depth `scan` from root
SCAN_SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "Library", ".cache",
                  ".Trash", "dist", "build"}

HELP = """\
rig --view [command]  - read-only rig project viewer (interactive when command is omitted)

projects                  list registered projects (~/.rig/projects.jsonl)
scan <root>               find */.rig/memory.db below root and register the projects
open <path>               select a project (interactive; use --path in one-shot mode)
sessions                  list project sessions
session <id>              show a session and its runs
runs [session_id]         list all runs or runs for one session
run <id>                  show one complete run: prompt, result, transcript, and metadata
prompt <id> / result <id> print a raw text field (for pipelines and agents)
transcript <id> [raw]     show agent activity within a run; raw prints the native stream
info                      show schema version, table counts, and metadata
lessons                   list active memory lessons
events [session_id]       show engine events and current heartbeat state
manifest                  list the memory manifest
files                     list files under .rig/
cat <file>                print a file under .rig/ (for example logs/bg-xxx.log)
sql <query>               run read-only SQL against the project database
repl                      launch the plain REPL without the TUI
q                         quit interactive mode

Without a command in a TTY, opens the full-screen TUI. Install it with `pip3 install textual`;
otherwise rig falls back to the REPL. Example: `rig --path <project> --view runs 3`."""


class ViewError(Exception):
    """User-facing viewer error without an internal traceback."""


class State:
    """Current viewer project and numbered-selection state."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.items: Dict[int, str] = {}



def connect_ro(db_file: str) -> sqlite3.Connection:
    """Open a project database in strict SQLite read-only mode."""
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_file)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    return conn


def _conn_for(path: Optional[str]) -> sqlite3.Connection:
    if not path:
        raise ViewError("project not to throw out: `open <path>` / number from `projects` (one-shot: --path)")
    db = convention.db_path(path)
    if not os.path.exists(db):
        raise ViewError(f"no DATABASE project: {db}")
    try:
        return connect_ro(db)
    except sqlite3.Error as e:
        raise ViewError(f"could not open DATABASE ({db}): {e}")


def _q(path: Optional[str], sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    """Execute one read-only project query and normalize database errors."""
    conn = _conn_for(path)
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error as e:
        raise ViewError(f"DATABASE not so: {e}")
    finally:
        conn.close()



_SESSIONS_SQL = ("SELECT s.*, COUNT(r.run_id) runs, COALESCE(SUM(r.cost),0) cost "
                 "FROM sessions s LEFT JOIN runs r ON r.session_id = s.session_id "
                 "GROUP BY s.session_id ORDER BY s.session_id")
_RUNS_SELECT = ("SELECT run_id, session_id, ts, kind, agent, phase, status, ok, cost, "
                "length(prompt) plen, length(result) rlen FROM runs")


def q_sessions(path: Optional[str]) -> List[sqlite3.Row]:
    return _q(path, _SESSIONS_SQL)


def q_session(path: Optional[str], sid: int) -> sqlite3.Row:
    rows = _q(path, "SELECT * FROM sessions WHERE session_id = ?", (sid,))
    if not rows:
        raise ViewError(f"no session {sid} (list — `sessions`)")
    return rows[0]


def q_runs(path: Optional[str], session_id: Optional[int] = None) -> List[sqlite3.Row]:
    """List run metadata and text sizes without loading large bodies."""
    if session_id is None:
        return _q(path, _RUNS_SELECT + " ORDER BY run_id")
    return _q(path, _RUNS_SELECT + " WHERE session_id = ? ORDER BY run_id", (session_id,))


def q_run(path: Optional[str], run_id: int) -> sqlite3.Row:
    rows = _q(path, "SELECT * FROM runs WHERE run_id = ?", (run_id,))
    if not rows:
        raise ViewError(f"no run {run_id} (see `runs`)")
    return rows[0]


def field(row: sqlite3.Row, name: str, default: str = "") -> str:
    """Read an optional column safely from databases created by older versions."""
    return row[name] if name in row.keys() else default


def render_transcript(adapter_name: str, raw: str) -> str:
    """Render a native harness transcript through its adapter."""
    if not raw:
        return "(transcript no: this run predates transcript capture or its adapter returned no stream)"
    try:
        if adapter_name == "claude":
            from ..adapters.claude import render_transcript as rt
        elif adapter_name == "codex":
            from ..adapters.codex import render_transcript as rt
        else:
            return raw
        return rt(raw)
    except Exception:
        return raw


def q_lessons(path: Optional[str]) -> List[sqlite3.Row]:
    return _q(path, "SELECT * FROM lessons ORDER BY lesson_id")


def q_manifest(path: Optional[str]) -> List[sqlite3.Row]:
    return _q(path, "SELECT * FROM manifest ORDER BY manifest_id")


def q_events(path: Optional[str], session_id: Optional[int] = None,
             limit: int = 200) -> List[sqlite3.Row]:
    """Return the latest engine-boundary events."""
    if session_id is None:
        return _q(path, "SELECT * FROM (SELECT * FROM events ORDER BY event_id DESC LIMIT ?) "
                        "ORDER BY event_id", (max(1, int(limit)),))
    return _q(path, "SELECT * FROM (SELECT * FROM events WHERE session_id = ? "
                    "ORDER BY event_id DESC LIMIT ?) ORDER BY event_id",
              (session_id, max(1, int(limit))))


def q_liveness(path: Optional[str]) -> List[sqlite3.Row]:
    return _q(path, "SELECT * FROM liveness ORDER BY live_id")


def project_stats(path: str) -> Optional[dict]:
    """Return summary statistics, None for no database, or an error stub."""
    db = convention.db_path(path)
    if not os.path.exists(db):
        return None
    try:
        conn = connect_ro(db)
        try:
            s = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
            r = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(cost),0) cost, MAX(ts) ts "
                             "FROM runs").fetchone()
            return {"path": path, "sessions": s, "runs": r["c"], "cost": r["cost"],
                    "last_ts": r["ts"] or "", "error": ""}
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"path": path, "sessions": 0, "runs": 0, "cost": 0, "last_ts": "",
                "error": str(e)}


def scan_projects(root: str) -> List[str]:
    """Discover projects containing .rig/memory.db below a root."""
    root = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
    if not os.path.isdir(root):
        raise ViewError(f"not a directory: {root}")
    found: List[str] = []
    for dirpath, dirnames, _ in os.walk(root):
        if os.path.exists(os.path.join(dirpath, convention.DB_DIRNAME, convention.DB_FILENAME)):
            found.append(dirpath)
            dirnames[:] = []
            continue
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= SCAN_MAX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SCAN_SKIP_DIRS]
    return found


def _rig_root(path: str) -> str:
    """Resolve a project's engine directory while rejecting symlink escapes."""
    root = convention.rig_dir(path)
    if os.path.islink(root):
        raise ViewError(f"{convention.DB_DIRNAME}/ — symlink, file server access closed")
    return os.path.realpath(root)


def rig_files(path: str) -> List[dict]:
    """List engine-state files to a bounded depth without expanding worktrees."""
    root = _rig_root(path)
    if not os.path.isdir(root):
        raise ViewError(f"no directory {root}")
    out: List[dict] = []

    def add_file(rel: str, full: str) -> None:
        try:
            st = os.stat(full)
            out.append({"path": rel, "size": st.st_size})
        except OSError:
            out.append({"path": rel, "size": None})

    try:
        names = sorted(os.listdir(root))
    except OSError as e:
        raise ViewError(f"not so {root}: {e}")
    for name in names:
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            add_file(name, full)
            continue
        try:
            entries = sorted(os.listdir(full))
        except OSError:
            entries = []
        if not entries:
            out.append({"path": name + "/", "size": None, "note": "empty"})
        for e in entries:
            fe = os.path.join(full, e)
            if os.path.isdir(fe):
                try:
                    n = len(os.listdir(fe))
                except OSError:
                    n = 0
                out.append({"path": f"{name}/{e}/", "size": None, "note": f"directory ({n} entries)"})
            else:
                add_file(f"{name}/{e}", fe)
    return out


def read_rig_file(path: str, rel: str) -> str:
    """Read a file strictly inside the project engine directory [D2]."""
    root = _rig_root(path)
    try:
        full = os.path.realpath(os.path.join(root, rel))
    except ValueError:
        raise ViewError(f"invalid path: {rel!r}")
    if full != root and not full.startswith(root + os.sep):
        raise ViewError(f"path escapes {convention.DB_DIRNAME}/: {rel}")
    if not os.path.isfile(full):
        raise ViewError(f"no file: {rel} (list — `files`)")
    try:
        with open(full, "rb") as f:
            data = f.read(MAX_CAT_BYTES + 1)
    except OSError as e:
        raise ViewError(f"not so {rel}: {e}")
    if b"\x00" in data[:8192]:
        raise ViewError(f"binary file: {rel}" +
                        (" (this is a DATABASE; use `sql`)" if rel.endswith(".db") else ""))
    text = data[:MAX_CAT_BYTES].decode("utf-8", errors="replace")
    if len(data) > MAX_CAT_BYTES:
        text += f"\n... (truncated at {MAX_CAT_BYTES} bytes)"
    return text


def run_sql(conn: sqlite3.Connection, query: str) -> Tuple[List[str], List[tuple], bool]:
    """Execute a bounded query through a read-only connection."""
    try:
        cur = conn.execute(query)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(MAX_SQL_ROWS + 1)
    except sqlite3.Error as e:
        raise ViewError(f"sql: {e}")
    truncated = len(rows) > MAX_SQL_ROWS
    return cols, [tuple(r) for r in rows[:MAX_SQL_ROWS]], truncated



def _table(headers: List[str], rows: List[tuple]) -> str:
    cells = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in cells:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))

    def fmt(r):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip()

    return "\n".join([fmt(headers)] + [fmt(r) for r in cells])


def _cut(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _ts(iso: str) -> str:
    """Format an ISO timestamp compactly, preserving unknown input."""
    iso = iso or ""
    if len(iso) >= 16 and iso[4] == "-" and iso[7] == "-":
        return f"{iso[8:10]}.{iso[5:7]} {iso[11:16]}"
    return iso


def _chars(n) -> str:
    if n is None:
        return ""
    n = int(n)
    return str(n) if n < 1000 else f"{n / 1000:.1f}k" if n < 100_000 else f"{n // 1000}k"


def _cost(c) -> str:
    return "" if c in (None, 0) else f"${float(c):.2f}"


def _ok(kind, v) -> str:
    """Render pass/fail only for actual invocation rows."""
    if kind != RUN_KIND[0]:
        return ""
    return "ok" if v else "FAIL"



def cmd_projects(state: State) -> str:
    paths = convention.read_registry()
    if not paths:
        return ("project list is empty (~/.rig/projects.jsonl); projects are registered by `rig --path ...`\n"
                "or discover them with `scan <root>`")
    state.items = {}
    rows = []
    for i, p in enumerate(paths, 1):
        state.items[i] = f"open {p}"
        st = project_stats(p)
        if st is None:
            rows.append((f"[{i}]", p, "", "", "", "DATABASE to disappear"))
            continue
        note = st["error"] and f"unreadable: {st['error']}"
        rows.append((f"[{i}]", p, f"{st['sessions']} sessional", f"{st['runs']} russian academy of sciences",
                     _cost(st["cost"]), note or _ts(st["last_ts"])))
    return _table(["#", "project", "session", "aurora", "$", "last"], rows)


def cmd_scan(state: State, root: str) -> str:
    if not root:
        raise ViewError("scan requires a root, for example `scan ~/projects`")
    found = scan_projects(root)
    if not found:
        return f"under {root}, no projects containing {convention.DB_DIRNAME}/{convention.DB_FILENAME} were found"
    added = [p for p in found if convention.register_project(p)]
    lines = [f"found {len(found)}, registered {len(added)} new projects:"]
    lines += [("  + " if p in added else "  = ") + p for p in found]
    return "\n".join(lines)


def cmd_open(state: State, arg: str) -> str:
    if not arg:
        raise ViewError("open requires a project path")
    p = os.path.realpath(os.path.abspath(os.path.expanduser(arg)))
    if not os.path.exists(convention.db_path(p)):
        raise ViewError(f"no DATABASE project: {convention.db_path(p)}")
    state.path = p
    return cmd_sessions(state)


def cmd_sessions(state: State) -> str:
    rows = q_sessions(state.path)
    if not rows:
        return f"{state.path}: no sessions"
    state.items = {int(r["session_id"]): f"session {r['session_id']}" for r in rows}
    return state.path + "\n" + _table(
        ["id", "name", "status", "flow", "branch", "aurora", "$", "renovation"],
        [(r["session_id"], _cut(r["name"], 40), r["status"], r["flow"], _cut(r["branch"], 30),
          r["runs"], _cost(r["cost"]), _ts(r["updated_ts"])) for r in rows])


def _runs_table(state: State, rows) -> str:
    state.items = {int(r["run_id"]): f"run {r['run_id']}" for r in rows}
    return _table(
        ["id", "sessional", "in time", "kind", "agent", "phase", "status", "ok", "$", "prompt", "result"],
        [(r["run_id"], r["session_id"] or "", _ts(r["ts"]), r["kind"], r["agent"],
          r["phase"], _cut(r["status"], 16), _ok(r["kind"], r["ok"]), _cost(r["cost"]),
          _chars(r["plen"]), _chars(r["rlen"])) for r in rows])


def _session_head(s: sqlite3.Row) -> List[str]:
    return [f"session {s['session_id']} · {s['name']} · {s['status']}",
            f"flow={s['flow'] or '-'} agent={s['agent'] or '-'} branch={s['branch'] or '-'} "
            f"tmux={s['tmux'] or '-'}",
            f"creation {_ts(s['created_ts'])} · renovation {_ts(s['updated_ts'])} · "
            f"head={_cut(s['head'], 12) or '-'} · in_flight={s['in_flight']}",
            f"task: {s['task'] or '-'}",
            f"config: {s['config']}"]


def cmd_session(state: State, arg: str) -> str:
    sid = _int_arg(arg, "session")
    s = q_session(state.path, sid)
    runs = q_runs(state.path, sid)
    body = _runs_table(state, runs) if runs else "no runs"
    return "\n".join(_session_head(s) + [""]) + body


def cmd_runs(state: State, arg: str) -> str:
    rows = q_runs(state.path, _int_arg(arg, "runs") if arg else None)
    return _runs_table(state, rows) if rows else "no runs"


def run_head(r: sqlite3.Row) -> str:
    """Render compact run metadata shared by the REPL and TUI."""
    return "\n".join([
        f"run {r['run_id']} · session {r['session_id'] or '-'} · {r['ts']} · {r['kind']}",
        f"agent={r['agent'] or '-'} adapter={r['adapter'] or '-'} phase={r['phase'] or '-'} · "
        f"{_ok(r['kind'], r['ok']) or 'booking'} · status={r['status'] or '-'}",
        f"cost={_cost(r['cost']) or '-'} ({r['cost_origin'] or 'no origin'}) · "
        f"resume_id={r['resume_id'] or '-'}",
        f"meta: {r['meta']}",
    ])


def run_text(r: sqlite3.Row) -> str:
    """Render a complete run with prompt, result, and transcript."""
    bar = "─" * 60
    transcript = field(r, "transcript")
    return "\n".join([
        run_head(r),
        f"{bar} prompt ({_chars(len(r['prompt']))} chars) {bar}",
        r["prompt"] or "(empty)",
        f"{bar} result ({_chars(len(r['result']))} chars) {bar}",
        r["result"] or "(empty)",
        f"{bar} transcript: agent activity ({_chars(len(transcript))} chars) {bar}",
        render_transcript(r["adapter"], transcript),
    ])


def cmd_run(state: State, arg: str) -> str:
    return run_text(q_run(state.path, _int_arg(arg, "run")))


def cmd_field(state: State, arg: str, name: str) -> str:
    return q_run(state.path, _int_arg(arg, name))[name] or ""


def cmd_transcript(state: State, arg: str) -> str:
    """Render or return the raw transcript for one run."""
    parts = arg.split()
    want_raw = len(parts) > 1 and parts[1].lower() == "raw"
    r = q_run(state.path, _int_arg(parts[0] if parts else "", "transcript"))
    raw = field(r, "transcript")
    if want_raw:
        return raw
    return render_transcript(r["adapter"], raw)


def cmd_info(state: State) -> str:
    """Render schema version, table counts, and project metadata."""
    tables = ("sessions", "runs", "lessons", "manifest", "spawn_reservations")
    counts = []
    for t in tables:
        try:
            counts.append(f"{t} {_q(state.path, f'SELECT COUNT(*) c FROM {t}')[0]['c']}")
        except ViewError:
            counts.append(f"{t} —")
    try:
        meta_rows = _q(state.path, "SELECT key, value FROM meta ORDER BY key")
    except ViewError:
        meta_rows = []
    lines = [state.path or "", " · ".join(counts), "", "meta:"]
    if not meta_rows:
        lines.append("  (empty)")
    for m in meta_rows:
        val = m["value"]
        lines.append(f"  {m['key']} = {val if len(val) <= 2000 else val[:2000] + '… (full: sql)'}")
    return "\n".join(lines)


def cmd_lessons(state: State) -> str:
    rows = q_lessons(state.path)
    if not rows:
        return "no lessons"
    return _table(
        ["id", "kind", "status", "gt", "feature", "sessional", "text"],
        [(r["lesson_id"], r["kind"], r["status"], "GT" if r["ground_truth"] else "",
          r["feature"], r["session_id"] or "", _cut(r["text"], 100)) for r in rows])


def cmd_events(state: State, rest: str) -> str:
    """Render engine events and current heartbeat state."""
    sid = None
    if rest.strip():
        try:
            sid = int(rest.strip())
        except ValueError:
            raise ViewError(f"events expects a numeric session_id, got {rest!r}")
    rows = q_events(state.path, sid)
    live = q_liveness(state.path)
    parts = []
    if rows:
        parts.append(_table(
            ["id", "ts", "sessional", "actor", "kind", "name", "outcome", "ms"],
            [(r["event_id"], r["ts"], r["session_id"] or "", r["actor"], r["kind"],
              _cut(r["name"], 30), _cut(r["status"], 20), r["duration_ms"] or "") for r in rows]))
    else:
        parts.append("no events")
    if live:
        parts.append("in flight (heartbeat):")
        for r in live:
            who = "/".join(x for x in (r["role"], r["phase"]) if x) or r["label"]
            parts.append(f"  [{who}] sessional {r['session_id'] or '-'} · start {r['started_ts']} · "
                         f"heartbeat {r['last_beat_ts']}" + (" · STALL" if r["stalled"] else ""))
    return "\n".join(parts)


def cmd_manifest(state: State) -> str:
    rows = q_manifest(state.path)
    if not rows:
        return "manifest empty"
    return _table(
        ["id", "name", "kind", "feature", "summary"],
        [(r["manifest_id"], r["name"], r["kind"], r["feature"], _cut(r["summary"], 90))
         for r in rows])


def cmd_files(state: State) -> str:
    if not state.path:
        raise ViewError("project not to throw out: `open <path>` / number from `projects` (one-shot: --path)")
    rows = rig_files(state.path)
    return _table(
        ["file", "dimensions", ""],
        [(f["path"], "" if f["size"] is None else f["size"], f.get("note", "")) for f in rows])


def cmd_cat(state: State, arg: str) -> str:
    if not state.path:
        raise ViewError("project not to throw out: `open <path>` / number from `projects` (one-shot: --path)")
    if not arg:
        raise ViewError("cat requires a file under .rig/ (see `files`)")
    return read_rig_file(state.path, arg)


def cmd_sql(state: State, query: str) -> str:
    if not query:
        raise ViewError("sql requires a query, for example `sql select phase, count(*) from runs group by 1`")
    conn = _conn_for(state.path)
    try:
        cols, rows, truncated = run_sql(conn, query)
    finally:
        conn.close()
    if not cols:
        return "(query returned no columns)"
    out = _table(cols, rows) if rows else "(0 rows)"
    if truncated:
        out += f"\n... (showing the first {MAX_SQL_ROWS} rows)"
    return out


def _int_arg(arg: str, cmd: str) -> int:
    try:
        return int(arg)
    except (TypeError, ValueError):
        raise ViewError(f"{cmd} requires a numeric id, got {arg!r}")



def execute(state: State, line: str) -> str:
    """Execute one viewer command for both REPL and one-shot modes."""
    line = (line or "").strip()
    if not line:
        return ""
    try:
        num = int(line)
    except ValueError:
        num = None
    if num is not None:
        mapped = state.items.get(num)
        if not mapped:
            raise ViewError(f"{line}: not in the current list (see `projects`, `sessions`, or `runs`)")
        return execute(state, mapped)
    parts = line.split(None, 1)
    cmd, rest = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
    table = {
        "projects": lambda: cmd_projects(state),
        "scan": lambda: cmd_scan(state, rest),
        "open": lambda: cmd_open(state, rest),
        "sessions": lambda: cmd_sessions(state),
        "session": lambda: cmd_session(state, rest),
        "runs": lambda: cmd_runs(state, rest),
        "run": lambda: cmd_run(state, rest),
        "prompt": lambda: cmd_field(state, rest, "prompt"),
        "result": lambda: cmd_field(state, rest, "result"),
        "transcript": lambda: cmd_transcript(state, rest),
        "info": lambda: cmd_info(state),
        "lessons": lambda: cmd_lessons(state),
        "events": lambda: cmd_events(state, rest),
        "manifest": lambda: cmd_manifest(state),
        "files": lambda: cmd_files(state),
        "cat": lambda: cmd_cat(state, rest),
        "sql": lambda: cmd_sql(state, rest),
        "help": lambda: HELP,
    }
    handler = table.get(cmd)
    if handler is None:
        raise ViewError(f"unknown command {cmd!r}; see `help`")
    try:
        return handler()
    except sqlite3.Error as e:
        raise ViewError(f"DATABASE not so: {e}")



def _page(text: str) -> None:
    """Send long terminal output to a pager and print otherwise."""
    lines = text.count("\n") + 1
    if not sys.stdout.isatty() or lines <= shutil.get_terminal_size().lines - 2:
        print(text)
        return
    pager = shutil.which(os.environ.get("PAGER") or "less")
    if not pager:
        print(text)
        return
    try:
        subprocess.run([pager] + (["-R"] if os.path.basename(pager) == "less" else []),
                       input=text.encode("utf-8", errors="replace"))
    except OSError:
        print(text)


def _launch_interactive(state: State) -> int:
    """Launch the Textual UI or fall back to the plain REPL."""
    try:
        from . import view_tui
    except ImportError:
        print("rig view: install `textual` with `pip3 install textual` for the full-screen interface; "
              "using the plain REPL instead",
              file=sys.stderr)
        return repl(state)
    view_tui.RigViewApp(path=state.path).run()
    return 0


def repl(state: State) -> int:
    _safe_show(state, "projects")
    while True:
        name = os.path.basename(state.path) if state.path else ""
        try:
            line = input(f"rig-view{' [' + name + ']' if name else ''}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line in ("q", "quit", "exit"):
            return 0
        if line:
            _safe_show(state, line)


def _safe_show(state: State, line: str) -> None:
    try:
        _page(execute(state, line))
    except ViewError as e:
        print(f"rig view · {e}")


def main(view_args: List[str], path: Optional[str] = None) -> int:
    """Run interactive viewing or a one-shot viewer command."""
    state = State()
    if path:
        p = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
        if not os.path.exists(convention.db_path(p)):
            print(f"rig view · no DATABASE project: {convention.db_path(p)}", file=sys.stderr)
            return 2
        state.path = p
    if not view_args:
        if not sys.stdin.isatty():
            print(HELP)
            return 0
        return _launch_interactive(state)
    if view_args == ["repl"]:
        return repl(state)
    try:
        print(execute(state, " ".join(view_args)))
        return 0
    except ViewError as e:
        print(f"rig view · {e}", file=sys.stderr)
        return 1
