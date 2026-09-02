"""Full-screen, read-only ``rig view`` frontend built with Textual.

Textual is an optional dependency isolated to this module; the interactive launcher
falls back to the plain REPL when it is unavailable. All data access uses the same
read-only queries as ``view.py``.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static

from ..db import convention
from . import view


def _pop_or_exit(app: App) -> None:
    """Pop one screen, or exit from the root screen."""
    if len(app.screen_stack) > 2:
        app.pop_screen()
    else:
        app.exit()


class TableScreen(Screen):
    """Base table screen with filtering, stable cursor position, and refresh."""

    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("slash", "filter", "filter"),
        Binding("r", "reload", "refresh"),
        Binding("a", "auto", "auto 2s"),
        Binding("q", "quit_app", "quit"),
        Binding("j", "cur_down", show=False),
        Binding("k", "cur_up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: List[Tuple[str, tuple]] = []
        self._needle = ""
        self._timer = None

    # Subclass contract.
    def columns(self) -> tuple:
        raise NotImplementedError

    def load_rows(self) -> List[Tuple[str, tuple]]:
        raise NotImplementedError

    def open_key(self, key: str) -> None:
        raise NotImplementedError

    def header_text(self) -> str:
        return ""

    # Frame.
    def compose(self) -> ComposeResult:
        yield Static("", classes="title")
        yield Input(placeholder="filter by substring... (Esc clears)", classes="filter")
        yield DataTable(cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).display = False
        table = self.query_one(DataTable)
        table.add_columns(*self.columns())
        table.focus()
        self.action_reload()

    def action_reload(self) -> None:
        try:
            self._rows = self.load_rows()
            self.query_one(".title", Static).update(self.header_text())
        except view.ViewError as e:
            self.notify(str(e), severity="error")
            self._rows = []
        self._refill()

    def _refill(self) -> None:
        table = self.query_one(DataTable)
        cur = table.cursor_row
        table.clear()
        needle = self._needle.lower()
        for key, cells in self._rows:
            if not needle or any(needle in str(c).lower() for c in cells):
                table.add_row(*[Text(str(c)) for c in cells], key=key)  # Do not parse prompts as markup.
        if table.row_count:
            table.move_cursor(row=min(max(cur, 0), table.row_count - 1))

    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:
        if ev.row_key.value is not None:
            self.open_key(ev.row_key.value)

    # Filtering.
    def action_filter(self) -> None:
        inp = self.query_one(Input)
        inp.display = True
        inp.focus()

    def on_input_changed(self, ev: Input.Changed) -> None:
        self._needle = ev.value
        self._refill()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        self.query_one(DataTable).focus()

    # Navigation and refresh.
    def action_back(self) -> None:
        inp = self.query_one(Input)
        if inp.display:                          # Esc clears the active filter first.
            inp.value = ""
            inp.display = False
            self._needle = ""
            self._refill()
            self.query_one(DataTable).focus()
            return
        _pop_or_exit(self.app)

    def action_auto(self) -> None:
        if self._timer is None:
            self._timer = self.set_interval(2.0, self.action_reload)
            self.notify("auto-refresh: on (2s)")
        else:
            self._timer.stop()
            self._timer = None
            self.notify("auto-refresh: off")

    def action_cur_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cur_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_quit_app(self) -> None:
        self.app.exit()


class ProjectsScreen(TableScreen):
    def columns(self) -> tuple:
        return ("project", "sessions", "runs", "$", "last")

    def header_text(self) -> str:
        return "rig · projects (registry: ~/.rig/projects.jsonl; add with: rig --view scan <root>)"

    def load_rows(self):
        rows = []
        for p in convention.read_registry():
            st = view.project_stats(p)
            if st is None:
                rows.append((p, (p, "", "", "", "database missing")))
            elif st["error"]:
                rows.append((p, (p, "", "", "", f"unreadable: {st['error']}")))
            else:
                rows.append((p, (p, st["sessions"], st["runs"], view._cost(st["cost"]),
                                 view._ts(st["last_ts"]))))
        return rows

    def open_key(self, key: str) -> None:
        self.app.push_screen(SessionsScreen(key))


class SessionsScreen(TableScreen):
    BINDINGS = TableScreen.BINDINGS + [
        Binding("f", "files", "files .rig"),
        Binding("l", "lessons", "lessons"),
        Binding("s", "sql", "sql"),
        Binding("i", "info", "project info"),
    ]

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__()

    def columns(self) -> tuple:
        return ("id", "name", "status", "flow", "branch", "runs", "$", "updated")

    def header_text(self) -> str:
        return f"{self.path} · sessions"

    def load_rows(self):
        return [(str(s["session_id"]),
                 (s["session_id"], view._cut(s["name"], 40), s["status"], s["flow"],
                  view._cut(s["branch"], 30), s["runs"], view._cost(s["cost"]),
                  view._ts(s["updated_ts"])))
                for s in view.q_sessions(self.path)]

    def open_key(self, key: str) -> None:
        self.app.push_screen(RunsScreen(self.path, int(key)))

    def action_files(self) -> None:
        self.app.push_screen(FilesScreen(self.path))

    def action_lessons(self) -> None:
        self.app.push_screen(LessonsScreen(self.path))

    def action_sql(self) -> None:
        self.app.push_screen(SqlScreen(self.path))

    def action_info(self) -> None:
        try:
            info = view.cmd_info(view.State(path=self.path))
        except view.ViewError as e:
            self.notify(str(e), severity="error")
            return
        self.app.push_screen(TextScreen(f"{self.path} · project info", info))


class RunsScreen(TableScreen):
    BINDINGS = SessionsScreen.BINDINGS          # Reuse project actions and base navigation.

    def __init__(self, path: str, session_id: Optional[int] = None) -> None:
        self.path = path
        self.session_id = session_id
        self._head = ""
        super().__init__()

    def columns(self) -> tuple:
        return ("id", "time", "kind", "agent", "phase", "status", "ok", "$", "prompt", "result")

    def header_text(self) -> str:
        return self._head

    def load_rows(self):
        if self.session_id is not None:
            self._head = "\n".join(view._session_head(view.q_session(self.path, self.session_id)))
        else:
            self._head = f"{self.path} · all runs"
        return [(str(r["run_id"]),
                 (r["run_id"], view._ts(r["ts"]), r["kind"], r["agent"], r["phase"],
                  view._cut(r["status"], 16), view._ok(r["kind"], r["ok"]),
                  view._cost(r["cost"]), view._chars(r["plen"]), view._chars(r["rlen"])))
                for r in view.q_runs(self.path, self.session_id)]

    def open_key(self, key: str) -> None:
        self.app.push_screen(RunScreen(self.path, int(key)))

    def action_files(self) -> None:
        self.app.push_screen(FilesScreen(self.path))

    def action_lessons(self) -> None:
        self.app.push_screen(LessonsScreen(self.path))

    def action_sql(self) -> None:
        self.app.push_screen(SqlScreen(self.path))

    def action_info(self) -> None:
        try:
            info = view.cmd_info(view.State(path=self.path))
        except view.ViewError as e:
            self.notify(str(e), severity="error")
            return
        self.app.push_screen(TextScreen(f"{self.path} · project info", info))


class TextBody(Screen):
    """Base text screen with scrolling and pager support."""

    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("o", "pager", "open pager"),
        Binding("q", "quit_app", "quit"),
        Binding("j", "scroll_dn", show=False),
        Binding("k", "scroll_up_", show=False),
    ]

    def pager_text(self) -> str:
        raise NotImplementedError

    def action_back(self) -> None:
        _pop_or_exit(self.app)

    def action_quit_app(self) -> None:
        self.app.exit()

    def action_pager(self) -> None:
        with self.app.suspend():                 # Suspend the alternate screen for the external pager.
            view._page(self.pager_text())

    def action_scroll_dn(self) -> None:
        self.query_one(VerticalScroll).scroll_relative(y=1, animate=False)

    def action_scroll_up_(self) -> None:
        self.query_one(VerticalScroll).scroll_relative(y=-1, animate=False)


class RunScreen(TextBody):
    BINDINGS = TextBody.BINDINGS + [
        Binding("tab", "next_section", "section"),
        Binding("t", "transcript_pager", "open transcript"),
    ]
    _SECTIONS = ("sec-head", "sec-prompt", "sec-result", "sec-transcript")

    def __init__(self, path: str, run_id: int) -> None:
        self.path = path
        self.run_id = run_id
        self._r = None
        self._transcript_rendered = ""
        self._sec = 0
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Static("", classes="title")
        with VerticalScroll():
            yield Static("", classes="section", id="sec-head")
            yield Static("", classes="section", id="sec-prompt")
            yield Static("", classes="section", id="sec-result")
            yield Static("", classes="section", id="sec-transcript")
        yield Footer()

    def on_mount(self) -> None:
        try:
            r = self._r = view.q_run(self.path, self.run_id)
        except view.ViewError as e:
            self.notify(str(e), severity="error")
            return
        # Breadcrumb: project, session context, and run.
        crumb = [self.path]
        head_lines = [view.run_head(r)]
        if r["session_id"]:
            try:
                s = view.q_session(self.path, r["session_id"])
                crumb.append(f"session {s['session_id']} «{s['name']}»")
                head_lines.append(f"context: flow={s['flow'] or '-'} branch={s['branch'] or '-'} "
                                  f"tmux={s['tmux'] or '-'} status={s['status']}")
            except view.ViewError:
                pass                                     # Show the run even if its session disappeared.
        crumb.append(f"run {r['run_id']}")
        self.query_one(".title", Static).update(" · ".join(crumb))
        head = self.query_one("#sec-head", Static)
        head.border_title = "run"
        head.update(Text("\n".join(head_lines)))
        prompt = self.query_one("#sec-prompt", Static)
        prompt.border_title = f"prompt ({view._chars(len(r['prompt']))})"
        prompt.update(Text(r["prompt"] or "(empty)"))
        result = self.query_one("#sec-result", Static)
        result.border_title = f"result ({view._chars(len(r['result']))})"
        result.update(Text(r["result"] or "(empty)"))
        raw = view.field(r, "transcript")
        self._transcript_rendered = view.render_transcript(r["adapter"], raw)
        tr = self.query_one("#sec-transcript", Static)
        tr.border_title = f"transcript: agent activity (raw {view._chars(len(raw))})"
        tr.update(Text(self._transcript_rendered))
        self.query_one(VerticalScroll).focus()

    def pager_text(self) -> str:
        return view.run_text(self._r) if self._r is not None else ""

    def action_transcript_pager(self) -> None:
        with self.app.suspend():
            view._page(self._transcript_rendered)

    def action_next_section(self) -> None:
        self._sec = (self._sec + 1) % len(self._SECTIONS)
        self.query_one(f"#{self._SECTIONS[self._sec]}", Static).scroll_visible(top=True)


class TextScreen(TextBody):
    """Display plain text such as an engine file or full lesson."""

    def __init__(self, title: str, text: str) -> None:
        self._title = title
        self._text = text
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Static(self._title, classes="title")
        with VerticalScroll():
            yield Static(Text(self._text), classes="section")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()

    def pager_text(self) -> str:
        return self._text


class FilesScreen(TableScreen):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__()

    def columns(self) -> tuple:
        return ("file", "size", "")

    def header_text(self) -> str:
        return f"{self.path} · files {convention.DB_DIRNAME}/"

    def load_rows(self):
        return [(f["path"], (f["path"], "" if f["size"] is None else f["size"], f.get("note", "")))
                for f in view.rig_files(self.path)]

    def open_key(self, key: str) -> None:
        try:
            self.app.push_screen(TextScreen(key, view.read_rig_file(self.path, key)))
        except view.ViewError as e:
            self.notify(str(e), severity="error")


class LessonsScreen(TableScreen):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__()

    def columns(self) -> tuple:
        return ("id", "kind", "status", "gt", "feature", "session", "text")

    def header_text(self) -> str:
        return f"{self.path} · lessons memory"

    def load_rows(self):
        return [(str(r["lesson_id"]),
                 (r["lesson_id"], r["kind"], r["status"], "GT" if r["ground_truth"] else "",
                  r["feature"], r["session_id"] or "", view._cut(r["text"], 100)))
                for r in view.q_lessons(self.path)]

    def open_key(self, key: str) -> None:
        try:
            lessons = view.q_lessons(self.path)
        except view.ViewError as e:          # The database may disappear between listing and selection.
            self.notify(str(e), severity="error")
            return
        for r in lessons:
            if str(r["lesson_id"]) == key:
                meta = (f"lesson {r['lesson_id']} · {r['kind']} · {r['status']}"
                        f"{' · ground-truth' if r['ground_truth'] else ''} · {r['ts']}\n\n")
                self.app.push_screen(TextScreen(f"lesson {key} · {self.path}", meta + r["text"]))
                return


class SqlScreen(Screen):
    """Run ad hoc SQL through a physically read-only connection."""

    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("q", "quit_app", "quit", show=False),  # Let the input receive the letter q.
    ]

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Static(f"{self.path} · SQL (read-only; table: sessions, runs, lessons, manifest, meta)",
                     classes="title")
        yield Input(placeholder="select phase, count(*) from runs group by 1   (Enter to execute)")
        yield DataTable(cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        table = self.query_one(DataTable)
        table.clear(columns=True)
        if not ev.value.strip():
            return
        try:
            conn = view._conn_for(self.path)
            try:
                cols, rows, truncated = view.run_sql(conn, ev.value)
            finally:
                conn.close()
        except view.ViewError as e:
            self.notify(str(e), severity="error")
            return
        table.add_columns(*(cols or ("result",)))
        for row in rows:
            table.add_row(*[Text("" if c is None else str(c)) for c in row])
        note = f" (latest {view.MAX_SQL_ROWS})" if truncated else ""
        self.notify(f"{len(rows)} rows{note}")

    def action_back(self) -> None:
        _pop_or_exit(self.app)

    def action_quit_app(self) -> None:
        self.app.exit()


class RigViewApp(App):
    """Full-screen project, session, and run browser for ``rig --view``."""

    CSS = """
    .title { padding: 0 1; height: auto; color: $text-muted; }
    .filter { margin: 0 1; }
    DataTable { height: 1fr; }
    .section { border: round $secondary; padding: 0 1; margin: 0 1 1 1; height: auto; }
    """

    def __init__(self, path: Optional[str] = None) -> None:
        super().__init__()
        self._start_path = path

    def on_mount(self) -> None:
        self.push_screen(ProjectsScreen())      # Esc from a project returns to the project list.
        if self._start_path:
            self.push_screen(SessionsScreen(self._start_path))
