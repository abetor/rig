"""Tests for the read-only CLI and Textual project viewer."""







import os
import subprocess

import pytest

from rig.db import convention, dao
from rig.entry import view





_CLAUDE_STREAM = "\n".join([
    '{"type":"system","subtype":"init","model":"haiku","cwd":"/x"}',
    '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"over to read file"}]}}',
    '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}',
    '{"type":"user","message":{"content":[{"type":"tool_result","content":"file1\\nfile2"}]}}',
    '{"type":"assistant","message":{"content":[{"type":"text","text":"Complete."}]}}',
    '{"type":"result","subtype":"success","result":"Complete.","total_cost_usd":0.02,'
    '"session_id":"sess-1","is_error":false}',
])


def _seed_project(tmp_path, name="proj") -> str:

    proj = str(tmp_path / name)
    os.makedirs(proj)
    conn = dao.open_project(proj)
    sid = dao.create_session(conn, name="feature-tooltip", branch="rig/feature-tooltip",
                             flow="feature/small", agent="claude", task="to make tooltip",
                             config={"worker_model": "m1"})
    dao.log_run(conn, session_id=sid, kind="invoke", agent="dev", adapter="claude",
                phase="implement", prompt="PROMPT-BODY-12345", result="RESULT-BODY-67890",
                ok=True, cost=0.31, cost_origin="exact", resume_id="r-abc",
                transcript=_CLAUDE_STREAM)
    dao.log_run(conn, session_id=sid, kind="invoke", agent="judge", adapter="claude",
                phase="verify", prompt="item2", result="error net", ok=False)
    dao.add_lesson(conn, session_id=sid, kind="feedback", text="lesson about tests",
                   feature="tooltip")
    dao.upsert_manifest(conn, name="lesson-1", summary="summary lesson", kind="feedback")
    conn.close()
    rig = convention.rig_dir(proj)
    os.makedirs(os.path.join(rig, "logs"))
    with open(os.path.join(rig, "logs", "bg-1.log"), "w", encoding="utf-8") as f:
        f.write("line log\n")
    with open(os.path.join(rig, "system-prompt.md"), "w", encoding="utf-8") as f:
        f.write("system prompt")
    return os.path.realpath(proj)


def _state(proj) -> view.State:
    return view.State(path=proj)




def test_registry_append_dedup_read(tmp_path):
    p = str(tmp_path / "a")
    os.makedirs(p)
    assert convention.register_project(p) is True
    assert convention.register_project(p) is False
    assert convention.read_registry() == [os.path.realpath(p)]


def test_registry_survives_garbage_lines(tmp_path):
    convention.register_project(str(tmp_path / "a"))
    with open(convention.registry_path(), "a", encoding="utf-8") as f:
        f.write("not json\n[1,2]\n")
    with open(convention.registry_path(), "ab") as f:
        f.write(b"\xff\xfe\n")
    assert convention.read_registry() == [os.path.realpath(str(tmp_path / "a"))]
    assert convention.register_project(str(tmp_path / "a")) is False


def test_cli_main_registers_resolved_project(tmp_path):
    from rig.entry import cli
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", repo], check=True)
    assert cli.main(["--path", repo, "--list"]) == 0
    assert os.path.realpath(repo) in convention.read_registry()




def test_projects_lists_registered_with_stats(tmp_path):
    proj = _seed_project(tmp_path)
    convention.register_project(proj)
    st = view.State()
    out = view.execute(st, "projects")
    assert proj in out and "1 sessional" in out and "2 russian academy of sciences" in out
    assert st.items[1] == f"open {proj}"


def test_number_navigation_projects_to_sessions(tmp_path):
    proj = _seed_project(tmp_path)
    convention.register_project(proj)
    st = view.State()
    view.execute(st, "projects")
    out = view.execute(st, "1")
    assert "feature-tooltip" in out and st.path == proj


def test_number_not_in_last_list_is_error(tmp_path):
    st = view.State()
    with pytest.raises(view.ViewError):
        view.execute(st, "7")


def test_sessions_and_session_detail(tmp_path):
    proj = _seed_project(tmp_path)
    st = _state(proj)
    out = view.execute(st, "sessions")
    assert "feature-tooltip" in out and "active" in out and "feature/small" in out
    out = view.execute(st, "session 1")
    assert "to make tooltip" in out
    assert "implement" in out and "verify" in out
    assert "FAIL" in out
    out_runs = view.execute(st, "runs 1")
    assert "implement" in out_runs
    assert "PROMPT-BODY" not in out_runs


def test_booking_rows_not_marked_fail(tmp_path):

    proj = _seed_project(tmp_path)
    conn = dao.open_project(proj)
    rid = dao.log_run(conn, session_id=1, kind="plan", result="plan")
    conn.close()
    st = _state(proj)
    lines = view.execute(st, "runs").splitlines()
    plan_line = next(ln for ln in lines if ln.startswith(f"{rid} "))
    assert "FAIL" not in plan_line
    assert "booking" in view.execute(st, f"run {rid}")


def test_run_full_text_and_raw_fields(tmp_path):
    proj = _seed_project(tmp_path)
    st = _state(proj)
    out = view.execute(st, "run 1")
    assert "PROMPT-BODY-12345" in out and "RESULT-BODY-67890" in out
    assert "$0.31" in out and "exact" in out and "r-abc" in out
    assert view.execute(st, "prompt 1") == "PROMPT-BODY-12345"
    assert view.execute(st, "result 1") == "RESULT-BODY-67890"


def test_transcript_rendered_and_raw(tmp_path):


    proj = _seed_project(tmp_path)
    st = _state(proj)
    out = view.execute(st, "transcript 1")
    assert "[to invent] over to read file" in out
    assert "[tool] Bash" in out and '"command": "ls"' in out
    assert "[sum tools] file1 file2" in out
    assert "[text] Complete." in out and "[finale] success" in out
    assert view.execute(st, "transcript 1 raw") == _CLAUDE_STREAM
    assert "[to invent] over to read file" in view.execute(st, "run 1")


def test_transcript_absent_is_message(tmp_path):
    proj = _seed_project(tmp_path)
    out = view.execute(_state(proj), "transcript 2")
    assert "transcript no" in out


def test_transcript_survives_old_db_without_column(tmp_path):

    proj = _seed_project(tmp_path)
    db = convention.db_path(proj)
    import sqlite3 as _sq
    conn = _sq.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")

    conn.execute("ALTER TABLE runs DROP COLUMN transcript")
    conn.commit()
    conn.close()
    st = _state(proj)
    assert "transcript no" in view.execute(st, "transcript 1")
    assert "PROMPT-BODY-12345" in view.execute(st, "run 1")


def test_info_shows_tables_and_meta(tmp_path):
    proj = _seed_project(tmp_path)
    out = view.execute(_state(proj), "info")
    assert "sessions 1" in out and "runs 2" in out and "lessons 1" in out
    assert "manifest 1" in out and "spawn_reservations 0" in out
    from rig.db import schema
    assert f"schema_version = {schema.SCHEMA_VERSION}" in out


def test_lessons_and_manifest(tmp_path):
    proj = _seed_project(tmp_path)
    st = _state(proj)
    assert "lesson about tests" in view.execute(st, "lessons")
    assert "summary lesson" in view.execute(st, "manifest")




def test_files_and_cat(tmp_path):
    proj = _seed_project(tmp_path)
    st = _state(proj)
    out = view.execute(st, "files")
    assert "memory.db" in out and "logs/bg-1.log" in out and "system-prompt.md" in out
    assert view.execute(st, "cat logs/bg-1.log") == "line log\n"


def test_cat_traversal_blocked(tmp_path):
    proj = _seed_project(tmp_path)
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    st = _state(proj)
    for evil in ("../../secret.txt", "/etc/hosts", "logs/../../.."):
        with pytest.raises(view.ViewError):
            view.execute(st, f"cat {evil}")


def test_cat_binary_refused(tmp_path):
    proj = _seed_project(tmp_path)
    with pytest.raises(view.ViewError, match="sql"):
        view.execute(_state(proj), "cat memory.db")




def test_sql_select(tmp_path):
    proj = _seed_project(tmp_path)
    out = view.execute(_state(proj), "sql select phase, count(*) c from runs group by 1 order by 1")
    assert "implement" in out and "verify" in out


def test_sql_write_physically_blocked(tmp_path):
    proj = _seed_project(tmp_path)
    st = _state(proj)
    with pytest.raises(view.ViewError, match="readonly|read-only"):
        view.execute(st, "sql insert into lessons(ts, text, created_ts, updated_ts) "
                         "values ('t','x','t','t')")
    conn = dao.open_project(proj)
    assert len(dao.list_lessons(conn)) == 1
    conn.close()


def test_viewer_leaves_no_trace_in_foreign_project(tmp_path):

    proj = str(tmp_path / "sb elses")
    os.makedirs(proj)
    st = _state(proj)
    with pytest.raises(view.ViewError):
        view.execute(st, "sessions")
    assert not os.path.exists(convention.rig_dir(proj))




def test_scan_finds_and_registers(tmp_path):
    proj = _seed_project(tmp_path / "deep")
    out = view.execute(view.State(), f"scan {tmp_path}")
    assert proj in out
    assert proj in convention.read_registry()


def test_scan_skips_hidden_and_noise(tmp_path):
    hidden = tmp_path / ".secret" / "p"
    os.makedirs(hidden)
    _seed_project(hidden)
    noise = tmp_path / "node_modules" / "p"
    os.makedirs(noise)
    _seed_project(noise)
    assert view.scan_projects(str(tmp_path)) == []


def test_repl_loop_drills_by_digit_and_quits(tmp_path, monkeypatch, capsys):

    proj = _seed_project(tmp_path)
    convention.register_project(proj)
    lines = iter(["1", "q"])
    monkeypatch.setattr("builtins.input", lambda _="": next(lines))
    assert view.repl(view.State()) == 0
    out = capsys.readouterr().out
    assert proj in out
    assert "feature-tooltip" in out


def test_repl_eof_exits_clean(monkeypatch):
    def _eof(_=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert view.repl(view.State()) == 0




def test_one_shot_prints_plain(tmp_path, capsys):
    proj = _seed_project(tmp_path)
    assert view.main(["sessions"], path=proj) == 0
    assert "feature-tooltip" in capsys.readouterr().out


def test_one_shot_needs_path_for_project_commands(tmp_path, capsys):
    assert view.main(["sessions"], path=None) == 1
    assert "project not to throw out" in capsys.readouterr().err


def test_one_shot_bad_path(tmp_path, capsys):
    assert view.main(["sessions"], path=str(tmp_path / "no")) == 2
    assert "no DATABASE" in capsys.readouterr().err


def test_cli_dispatches_view_before_path_requirement(capsys):
    from rig.entry import cli
    assert cli.main(["--view", "help"]) == 0
    assert "projects" in capsys.readouterr().out


def test_unknown_command(tmp_path):
    with pytest.raises(view.ViewError, match="unknown"):
        view.execute(view.State(), "rubbish")


def test_sql_fetch_stage_error_is_message(tmp_path):


    proj = _seed_project(tmp_path)
    with pytest.raises(view.ViewError, match="sql"):
        view.execute(_state(proj), "sql select abs(x) from (select -9223372036854775808 as x)")


def test_cat_permission_error_is_message(tmp_path):
    proj = _seed_project(tmp_path)
    locked = os.path.join(convention.rig_dir(proj), "logs", "bg-1.log")
    os.chmod(locked, 0)
    try:
        with pytest.raises(view.ViewError, match="not so"):
            view.execute(_state(proj), "cat logs/bg-1.log")
    finally:
        os.chmod(locked, 0o644)


def test_files_unreadable_rig_dir_is_message(tmp_path):
    proj = _seed_project(tmp_path)
    rig = convention.rig_dir(proj)
    os.chmod(rig, 0)
    try:
        with pytest.raises(view.ViewError, match="not so"):
            view.execute(_state(proj), "files")
    finally:
        os.chmod(rig, 0o755)




def test_tui_fallback_to_repl_without_textual(tmp_path, monkeypatch, capsys):

    import sys as _sys
    monkeypatch.setitem(_sys.modules, "rig.entry.view_tui", None)
    monkeypatch.setattr(view.sys.stdin, "isatty", lambda: True)

    def _eof(_=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert view.main([], path=None) == 0
    assert "textual" in capsys.readouterr().err


def test_tui_drill_and_filter(tmp_path):


    pytest.importorskip("textual")
    import asyncio
    from rig.entry import view_tui
    proj = _seed_project(tmp_path)
    convention.register_project(proj)

    async def go():
        app = view_tui.RigViewApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert type(app.screen).__name__ == "ProjectsScreen"
            await pilot.press("enter")
            assert type(app.screen).__name__ == "SessionsScreen"
            await pilot.press("enter")
            assert type(app.screen).__name__ == "RunsScreen"
            from textual.widgets import DataTable, Input, Static
            assert app.screen.query_one(DataTable).row_count == 2
            await pilot.press("slash")
            app.screen.query_one(Input).value = "judge"
            await pilot.pause()
            assert app.screen.query_one(DataTable).row_count == 1
            await pilot.press("escape")
            assert type(app.screen).__name__ == "RunsScreen"
            assert app.screen.query_one(DataTable).row_count == 2
            await pilot.press("enter")
            assert type(app.screen).__name__ == "RunScreen"
            body = str(app.screen.query_one("#sec-prompt", Static).render())
            assert "PROMPT-BODY-12345" in body
            tr = str(app.screen.query_one("#sec-transcript", Static).render())
            assert "[to invent] over to read file" in tr
            title = str(app.screen.query_one(".title", Static).render())
            assert "session 1 «feature-tooltip»" in title
            await pilot.press("escape")
            assert type(app.screen).__name__ == "RunsScreen"
    asyncio.run(go())


def test_tui_sql_and_files_screens(tmp_path):
    pytest.importorskip("textual")
    import asyncio
    from rig.entry import view_tui

    proj = _seed_project(tmp_path)

    async def go():
        app = view_tui.RigViewApp(path=proj)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert type(app.screen).__name__ == "SessionsScreen"
            await pilot.press("s")
            assert type(app.screen).__name__ == "SqlScreen"
            from textual.widgets import DataTable, Input
            app.screen.query_one(Input).value = "select count(*) c from runs"
            await pilot.press("enter")
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.row_count == 1
            await pilot.press("escape")
            await pilot.press("f")
            assert type(app.screen).__name__ == "FilesScreen"
            assert app.screen.query_one(DataTable).row_count >= 3
            await pilot.press("escape")
            await pilot.press("escape")
            assert type(app.screen).__name__ == "ProjectsScreen"
    asyncio.run(go())


def test_tui_db_vanishes_mid_navigation_no_crash(tmp_path):

    pytest.importorskip("textual")
    import asyncio
    from rig.entry import view_tui

    proj = _seed_project(tmp_path)

    async def go():
        app = view_tui.RigViewApp(path=proj)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("l")
            assert type(app.screen).__name__ == "LessonsScreen"
            os.remove(convention.db_path(proj))
            await pilot.press("enter")
            await pilot.pause()
            assert type(app.screen).__name__ == "LessonsScreen"
    asyncio.run(go())




def test_broken_db_is_message_not_traceback(tmp_path):

    proj = str(tmp_path / "p")
    os.makedirs(os.path.join(proj, ".rig"))
    open(os.path.join(proj, ".rig", "memory.db"), "wb").close()
    st = _state(os.path.realpath(proj))
    for cmd in ("sessions", "runs", "run 1", "lessons", "manifest"):
        with pytest.raises(view.ViewError, match="DATABASE not so"):
            view.execute(st, cmd)


def test_cat_nul_byte_is_message_not_crash(tmp_path):
    proj = _seed_project(tmp_path)
    with pytest.raises(view.ViewError):
        view.execute(_state(proj), "cat logs/\x00evil")


def test_unicode_superscript_digit_not_crash(tmp_path):

    with pytest.raises(view.ViewError):
        view.execute(view.State(), "³")


def test_symlinked_rig_dir_blocked(tmp_path):

    outside = tmp_path / "stranger"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    proj = tmp_path / "p2"
    proj.mkdir()
    os.symlink(str(outside), str(proj / ".rig"))
    st = _state(str(proj))
    for cmd in ("cat secret.txt", "files"):
        with pytest.raises(view.ViewError, match="symlink"):
            view.execute(st, cmd)


def test_projects_vanished_db_number_still_mapped(tmp_path):

    ghost = tmp_path / "spectre ("
    ghost.mkdir()
    convention.register_project(str(ghost))
    st = view.State()
    out = view.execute(st, "projects")
    assert "DATABASE to disappear" in out
    with pytest.raises(view.ViewError, match="no DATABASE"):
        view.execute(st, "1")


def test_view_with_task_warns(tmp_path, capsys):
    from rig.entry import cli
    assert cli.main(["implement feature", "--view", "help"]) == 0
    assert "was ignored" in capsys.readouterr().err
