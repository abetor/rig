"""Tests for CLI entry points, session startup, initialization, and warmup."""







from __future__ import annotations

from rig.db import dao
from rig.entry import cli, init, session, warmup
from rig.memory import WARM, boot
from rig.memory.reconcile_state import CORE_VERSION
from rig.transport.base import ExecResult


class GitRunner:

    def __init__(self, *, head="H1", branches=None, git_repo=True):
        self._head = head
        self._branches = branches or []
        self._git = git_repo

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        a = cmd[3:] if cmd[:1] == ["git"] else cmd
        if a[:2] == ["rev-parse", "HEAD"]:
            return ExecResult(0, self._head, "", 0.0)
        if a[:1] == ["rev-parse"] and "--is-inside-work-tree" in a:
            return ExecResult(0 if self._git else 128, "true" if self._git else "", "", 0.0)
        if a[:2] == ["branch", "--list"]:
            return ExecResult(0, "\n".join(self._branches), "", 0.0)
        return ExecResult(0, "", "", 0.0)


def _db(tmp_path):
    return dao.connect(str(tmp_path / "m.db"))



def test_session_name_is_branch_is_tmux(tmp_path):
    s = session.start_session(_db(tmp_path), GitRunner(), "/repo", task="to repair login")
    assert s.name == s.branch == s.tmux and s.name == "to-repair-login"


def test_session_id_monotonic(tmp_path):
    conn = _db(tmp_path)
    a = session.start_session(conn, GitRunner(), "/repo", task="a")
    b = session.start_session(conn, GitRunner(), "/repo", task="b")
    assert b.session_id == a.session_id + 1


def test_start_session_writes_session_start_booking_row(tmp_path):


    conn = _db(tmp_path)
    s = session.start_session(conn, GitRunner(), "/repo", task="fix login", flow="lead")
    starts = [r for r in dao.list_runs(conn, session_id=s.session_id) if r["kind"] == "session_start"]
    assert len(starts) == 1
    assert "fix login" in starts[0]["result"]
    assert starts[0]["ok"] == 0


def test_journal_renders_booking_rows_neutral_not_fail(tmp_path):


    from rig.entry import lead
    conn = _db(tmp_path)
    s = session.start_session(conn, GitRunner(), "/repo", task="X")
    dao.log_run(conn, session_id=s.session_id, kind="note", result="handoff")
    dao.log_run(conn, session_id=s.session_id, kind="invoke", agent="worker", phase="exec",
                ok=False, result="real failure")
    brief = lead._journal_brief(conn, s.session_id)
    assert "start/·" in brief and "note/·" in brief
    assert "exec/FAIL" in brief


def test_session_stamps_head_and_core_version(tmp_path):
    conn = _db(tmp_path)
    s = session.start_session(conn, GitRunner(head="HEADSHA"), "/repo", task="x")
    row = dao.get_session(conn, s.session_id)
    assert row["head"] == "HEADSHA" and row["core_version"] == CORE_VERSION


def test_session_start_makes_boot_warm_not_stale(tmp_path):


    conn = _db(tmp_path)
    runner = GitRunner(head="H1", branches=[])
    session.start_session(conn, runner, "/repo", task="feature")
    b = boot(runner, conn, "/repo")
    assert b.mode == WARM


def test_slugify_safe_and_fallback():
    assert session.slugify("Fix: ../etc/passwd!") == "fix-etc-passwd"
    assert session.slugify("   ") == "session"


def test_explicit_name_is_also_slugified(tmp_path):

    s = session.start_session(_db(tmp_path), GitRunner(), "/repo", name="../../evil", task="t")
    assert s.branch == s.name == "evil" and "/" not in s.branch and ".." not in s.branch



def test_init_ready_when_tools_and_harness_present():
    which = lambda n: f"/usr/bin/{n}" if n in ("git", "python3", "tmux", "claude") else None
    r = init.check_readiness(which=which, secrets={})
    assert r.ready and "claude" in r.harnesses and r.missing == []


def test_init_not_ready_without_harness():
    which = lambda n: f"/usr/bin/{n}" if n in ("git", "python3") else None
    r = init.check_readiness(which=which, secrets={})
    assert not r.ready and any("harness" in m for m in r.missing)


def test_init_missing_required_tool():
    which = lambda n: None if n == "git" else f"/usr/bin/{n}"
    r = init.check_readiness(which=which, secrets={})
    assert not r.ready and "git" in r.missing


def test_init_secrets_presence_not_value():
    r = init.check_readiness(which=lambda n: "/x", secrets={"TELEGRAM_BOT_TOKEN": "SECRET"})
    assert r.secrets["TELEGRAM_BOT_TOKEN"] is True
    assert "SECRET" not in init.render_readiness(r)



def test_warmup_creates_db_on_first_entry(tmp_path):
    project = str(tmp_path)
    assert warmup.is_fresh(project)
    r1 = warmup.warmup(project)
    assert r1.created is True
    r2 = warmup.warmup(project)
    assert r2.created is False


def test_warmup_runs_scan_only_on_fresh(tmp_path):
    project = str(tmp_path)
    calls = []
    warmup.warmup(project, scan_fn=lambda conn, p: calls.append(p))
    warmup.warmup(project, scan_fn=lambda conn, p: calls.append(p))
    assert calls == [project]



def test_parse_profile_key_agnostic_any_substrate():


    assert warmup.parse_profile('Scouting.\nSum: {"stack": "python", "test_cmd": "pytest -q"}') == \
        {"stack": "python", "test_cmd": "pytest -q"}

    assert warmup.parse_profile('This archive.\n{"genre": "noir", "sources": ["a", "b"], "audience": "18+"}') == \
        {"genre": "noir", "sources": ["a", "b"], "audience": "18+"}
    assert warmup.parse_profile("simply text without json") == {"summary": "simply text without json"}
    assert warmup.parse_profile("   ") == {}


def test_persist_profile_writes_meta(tmp_path):


    import json as _json
    conn = _db(tmp_path)
    prof = warmup.persist_profile(conn, '{"stack": "svelte+python", "harness": "scripts/test/runner.py"}')
    assert prof["stack"] == "svelte+python"
    assert _json.loads(dao.get_meta(conn, "project_profile"))["harness"] == "scripts/test/runner.py"

    empty = dao.connect(":memory:")
    warmup.persist_profile(empty, "   ")
    assert dao.get_meta(empty, "project_profile") is None


def test_scan_fn_persists_profile_without_agents(tmp_path, monkeypatch):



    import json as _json
    conn = _db(tmp_path)
    monkeypatch.setattr(cli, "build_executor", lambda *a, **k: object())

    class _FR:
        ctx = {"context": '{"stack": "go", "test_cmd": "go test ./..."}'}
    monkeypatch.setattr(cli, "run_flow", lambda *a, **k: _FR())
    cli._scan_fn(GitRunner(), "/repo", worker="claude", judge="claude",
                 worker_model="wm", judge_model="jm")(conn, "/repo")
    assert _json.loads(dao.get_meta(conn, "project_profile"))["stack"] == "go"


def test_cmd_new_threads_project_profile_from_meta_into_ctx(tmp_path):


    from rig.flow.runner import PhaseResult
    conn = _db(tmp_path)
    dao.set_meta(conn, "project_profile", '{"stack": "python"}')
    seen = {}

    def fake_exec(phase, ctx):
        seen.setdefault("profile", ctx.get("project_profile"))
        return PhaseResult(green=True, output={})
    cli.cmd_new(conn, GitRunner(), "/repo", fake_exec, task="t", flow="lead/dev-feature")
    assert seen["profile"] == '{"stack": "python"}'


def test_cmd_new_marks_stale_profile_when_head_moved(tmp_path):


    from rig.flow.runner import PhaseResult
    conn = _db(tmp_path)
    dao.set_meta(conn, "project_profile", '{"stack": "python"}')
    dao.set_meta(conn, "project_profile_head", "OLDHEAD")
    seen = {}

    def fake_exec(phase, ctx):
        seen.setdefault("profile", ctx.get("project_profile"))
        return PhaseResult(green=True, output={})
    cli.cmd_new(conn, GitRunner(head="NEWHEAD"), "/repo", fake_exec, task="t", flow="lead/dev-feature")
    assert "may be stale" in seen["profile"] and '{"stack": "python"}' in seen["profile"]

    dao.set_meta(conn, "project_profile_head", "SAME")
    seen2 = {}
    cli.cmd_new(conn, GitRunner(head="SAME"), "/repo",
                lambda ph, c: (seen2.setdefault("p", c.get("project_profile")), PhaseResult(green=True, output={}))[1],
                task="t", flow="lead/dev-feature")
    assert "could to become obsolete" not in seen2["p"]


def test_render_new_surfaces_profile_and_critique_and_env_reminder():


    from types import SimpleNamespace
    res = {"mode": "checkpoint",
           "session": SimpleNamespace(session_id=3, name="feature"),
           "plan": SimpleNamespace(goal="goal", units=[], gate_cmd=["chk"]),
           "profile": '{"stack": "python", "notes": "is required Postgres :5433"}',
           "result": SimpleNamespace(ctx={"critique": ["gate not cover front-unit U3"]})}
    out = cli.render_new(res, "/proj", worker="claude", judge="claude", worker_model="m", judge_model="m")
    assert "Postgres :5433" in out
    assert "gate not cover" in out
    assert "services and test data" in out


def test_is_approval_recognizes_yes_but_not_feedback():


    for ok in ("ok", "okay", "go", "yes", "y", "approve", "approved", "confirm",
               "confirmed", "LGTM", "lets go", "let's go", "proceed", "+", "  Ok! "):
        assert cli.is_approval(ok), ok
    for fb in ("ok, but add test", "no", "rewrite plan", "since", "aerial photograph",
               "apruv", "approval", "aha!", ""):
        assert not cli.is_approval(fb), fb


def test_split_at_gate_finds_converse():

    from rig.flow.phase import Phase
    from rig.flow.resolve import split_at_gate
    phases = [Phase(name="plan", agent="plan"), Phase(name="conv", agent="converse", about="plan"),
              Phase(name="units", agent="units")]
    pre, gate, post = split_at_gate(phases)
    assert [p.name for p in pre] == ["plan"] and gate.agent == "converse" and gate.about == "plan"
    assert [p.name for p in post] == ["units"]


def test_cmd_new_converse_gate_waits_for_reply(tmp_path):


    from rig.flow.phase import Phase
    from rig.flow.runner import PhaseResult
    import rig.entry.cli as clide
    conn = _db(tmp_path)
    monkey_phases = [Phase(name="plan", agent="plan"), Phase(name="conv", agent="converse", about="plan"),
                     Phase(name="units", agent="units")]
    orig = clide.resolve_phases
    clide.resolve_phases = lambda flow, **k: monkey_phases
    try:
        seen = []

        def fake_exec(phase, ctx):
            seen.append(phase.name)
            from rig.core.types import Plan, Unit
            return PhaseResult(green=True, output={"plan": Plan(goal="g", units=[Unit(id="u1", instruction="x")], gate_cmd=["c"])})
        res = clide.cmd_new(conn, GitRunner(), "/repo", fake_exec, task="t", flow="feature/large")
    finally:
        clide.resolve_phases = orig
    assert res["mode"] == "waiting_reply"
    assert "units" not in seen
    assert dao.get_session(conn, res["session"].session_id)["status"] == "need_human"


def test_cmd_reply_approval_runs_post_segment(tmp_path):

    from rig.flow.phase import Phase
    from rig.flow.runner import PhaseResult
    from rig.core.types import Plan, Unit
    import rig.entry.cli as clide
    conn = _db(tmp_path)
    sid = dao.create_session(conn, name="s", task="t", flow="feature/large")
    clide._save_plan(conn, sid, Plan(goal="g", units=[Unit(id="u1", instruction="x")], gate_cmd=["c"]))
    phases = [Phase(name="plan", agent="plan"), Phase(name="conv", agent="converse", about="plan"),
              Phase(name="units", agent="units", on_fail="continue")]
    orig = clide.resolve_phases
    clide.resolve_phases = lambda flow, **k: phases
    try:
        ran = []
        res = clide.cmd_reply(conn, GitRunner(), "/repo",
                              lambda ph, c: (ran.append(ph.name), PhaseResult(green=True, output={}))[1],
                              sid, "ok")
    finally:
        clide.resolve_phases = orig
    assert res["mode"] == "approved" and ran == ["units"]


def test_cmd_reply_feedback_reworks_plan_and_waits_again(tmp_path):


    from rig.flow.phase import Phase
    from rig.flow.runner import PhaseResult
    from rig.core.types import Plan, Unit
    import rig.entry.cli as clide
    conn = _db(tmp_path)
    sid = dao.create_session(conn, name="s", task="t", flow="feature/large")
    clide._save_plan(conn, sid, Plan(goal="g1", units=[Unit(id="u1", instruction="effort")], gate_cmd=["c"]))
    phases = [Phase(name="scout", agent="scout"), Phase(name="plan", agent="plan"),
              Phase(name="conv", agent="converse", about="plan"), Phase(name="units", agent="units")]
    orig = clide.resolve_phases
    clide.resolve_phases = lambda flow, **k: phases
    try:
        ran = []

        def fake_exec(phase, ctx):
            ran.append(phase.name)
            if phase.agent == "plan":
                assert ctx.get("human_feedback") == "to dilute on two"
                return PhaseResult(green=True, output={"plan": Plan(goal="g2", units=[Unit(id="u1", instruction="new")], gate_cmd=["c"])})
            return PhaseResult(green=True, output={})
        res = clide.cmd_reply(conn, GitRunner(), "/repo", fake_exec, sid, "to dilute on two")
    finally:
        clide.resolve_phases = orig
    assert res["mode"] == "rework"
    assert ran == ["plan"]
    assert "units" not in ran
    assert dao.get_session(conn, sid)["status"] == "need_human"
    assert res["plan"].goal == "g2"


def test_notify_need_human_pings_on_halt(monkeypatch):


    import rig.tools as tools_pkg
    from rig.tools.send import SendResult
    cap = {}
    monkeypatch.setattr(tools_pkg, "send",
                        lambda ch, txt, payload=None: (cap.update(ch=ch, txt=txt, payload=payload),
                                                       SendResult(channel=ch, sent=True))[1])
    cli._notify_need_human({"status": "need_human", "handoff": "worker to stick decision"}, 5, "telegram",
                           repo="/x/y/proj", flow="feature/small")
    assert cap["ch"] == "telegram" and "rig #5" in cap["txt"] and "need_human" in cap["txt"]
    assert cap["txt"].splitlines()[0] == "rig #5 · proj · feature/small"
    assert "IS REQUIRED RESPONSE" in cap["txt"] and "worker to stick decision" in cap["txt"]
    assert "--approve 5" in cap["txt"]
    assert cap["payload"]["kind"] == "action"
    cap.clear()
    cli._notify_need_human({"status": "done", "handoff": "ok"}, 5, "telegram")
    assert not cap
    cli._notify_need_human({"status": "need_human", "handoff": "x"}, 5, None)
    assert not cap


def test_critic_prompt_includes_gate_for_coverage():

    from rig.util import content
    p = content.render("prompts/critic.md", GOAL="quintal(s)", UNITS="U1", GATE="chk-cmd")
    assert "chk-cmd" in p and "does the gate cover" in p


def test_start_session_consolidates_old_memory(tmp_path):


    conn = _db(tmp_path)
    old = dao.add_lesson(conn, text="obsolete rule", kind="principle", status="active",
                         ground_truth=False)
    keep = dao.add_lesson(conn, text="ground truth", kind="principle", status="active",
                          ground_truth=True)
    conn.execute("UPDATE lessons SET created_ts='2019-01-01T00:00:00+00:00'")
    conn.commit()
    session.start_session(conn, GitRunner(head="H1"), "/repo", task="new task")
    assert dao.get_lesson(conn, old)["status"] == "retired"
    assert dao.get_lesson(conn, keep)["status"] == "active"



def test_resolve_repo_rejects_non_git(tmp_path):
    repo, runner = cli.resolve_repo(str(tmp_path), runner=GitRunner(git_repo=False))
    assert repo is None and runner is None


def test_resolve_repo_accepts_git(tmp_path):
    repo, runner = cli.resolve_repo(str(tmp_path), runner=GitRunner(git_repo=True))
    assert repo == str(tmp_path)


def test_cli_requires_explicit_path_no_cwd_default(capsys):

    rc = cli.main(["how-then task"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--path" in err
    rc_init = cli.main(["--init"])
    assert rc_init == 0


def test_session_start_then_list(tmp_path):


    conn = _db(tmp_path)
    session.start_session(conn, GitRunner(), "/repo", task="feature X", flow="lead/dev-feature", agent="claude")
    sessions = cli.cmd_list(conn)
    assert len(sessions) == 1 and sessions[0]["flow"] == "lead/dev-feature"
    assert "feature X" in cli.render_list(sessions)


def test_cmd_resume_boots_session(tmp_path):
    conn = _db(tmp_path)
    runner = GitRunner(head="H1")
    s = session.start_session(conn, runner, "/repo", task="t", flow="base")
    res = cli.cmd_resume(conn, runner, "/repo", s.session_id)
    assert res["ok"] and res["mode"] == WARM and res["session"]["session_id"] == s.session_id


def test_cmd_resume_missing_session(tmp_path):
    res = cli.cmd_resume(_db(tmp_path), GitRunner(), "/repo", 999)
    assert not res["ok"] and "999" in res["detail"]


def test_cli_list_empty_message(tmp_path):
    assert "no sessions" in cli.render_list([])


def test_cli_defaults_read_env(monkeypatch):


    monkeypatch.setenv("RIG_WORKER_MODEL", "env-worker-model")
    monkeypatch.setenv("RIG_NOTIFY", "telegram")
    monkeypatch.setenv("RIG_FLOW", "lead/dev-feature")
    args = cli._build_parser().parse_args(["task"])
    assert args.worker_model == "env-worker-model"
    assert args.notify == "telegram" and args.flow == "lead/dev-feature"

    args2 = cli._build_parser().parse_args(["task", "--worker-model", "cli-override"])
    assert args2.worker_model == "cli-override"


def test_handoff_surfaces_orphan_branches():


    from rig.checks.reconcile_plan import ReconcileReport
    from rig.flow.runner import FlowResult
    rep = ReconcileReport(matches_plan=False, goal="g", units=[], orphan_branches=["rig/old1", "rig/old2"],
                          integration_ready=True, units_ok=True)
    fres = FlowResult(ok=True, results=[{"name": "units", "green": True}], ctx={"report": rep})
    txt = cli._handoff_text(fres, in_flight=["rig/u1"])
    assert "Branches outside the plan" in txt and "rig/old1" in txt and "rig/old2" in txt






def test_model_for_harness_blanks_cross_family_claude_default():

    assert cli.model_for_harness("codex", "claude-sonnet-4-6") == ""


def test_model_for_harness_keeps_claude_model_for_claude():
    assert cli.model_for_harness("claude", "claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_model_for_harness_keeps_explicit_codex_model():

    assert cli.model_for_harness("codex", "gpt-5-codex") == "gpt-5-codex"


def test_build_executor_does_not_leak_claude_default_into_codex_worker(tmp_path):
    conn = _db(tmp_path)
    runner = GitRunner(head="H1")
    ex = cli.build_executor(runner, "/repo", conn, session_id=1, worker="codex", judge="claude",
                            worker_model="claude-sonnet-4-6", judge_model="claude-sonnet-4-6")

    assert ex.spawner.model == ""
    assert ex.judge_model == "claude-sonnet-4-6"






def test_render_new_checkpoint_prints_runnable_approve_command():



    from types import SimpleNamespace
    res = {"mode": "checkpoint",
           "session": SimpleNamespace(session_id=7, name="feature"),
           "plan": None}
    out = cli.render_new(res, "/proj/<private-repo> admin", worker="claude", judge="claude",
                         worker_model="claude-opus-4-8", judge_model="claude-opus-4-8")
    assert "python3 -m rig" in out
    assert "'/proj/<private-repo> admin'" in out
    assert "--approve 7" in out
    assert "--worker-model claude-opus-4-8" in out
    assert "--judge-model claude-opus-4-8" in out
    assert "rig --approve 7`" not in out


def test_render_new_judge_defaults_to_worker_in_hint():

    from types import SimpleNamespace
    res = {"mode": "checkpoint", "session": SimpleNamespace(session_id=3, name="x"), "plan": None}
    out = cli.render_new(res, "/p", worker="codex", judge=None, worker_model="wm", judge_model="jm")
    assert "--judge codex" in out



def test_render_lessons_empty_and_pending_first():
    assert "empty" in cli.render_lessons([])
    out = cli.render_lessons([
        {"lesson_id": 1, "status": "active", "ground_truth": 1, "kind": "principle", "feature": "", "text": "rule"},
        {"lesson_id": 2, "status": "pending", "ground_truth": 0, "kind": "free", "feature": "", "text": "suffering humanity"},
    ])
    assert "AWAITING HUMAN REVIEW (1 pending)" in out
    assert out.index("#2") < out.index("#1")
    assert "·gt" in out


def test_cli_lessons_promote_retire_roundtrip(tmp_path, capsys):



    import subprocess

    from rig.memory import store as store_mod
    repo = str(tmp_path)
    subprocess.run(["git", "init", "-q", repo], check=True)
    conn = dao.open_project(repo)
    sid = dao.create_session(conn, name="s")
    lid = store_mod.write_lesson(conn, text="lesson lead", scope=store_mod.ORCHESTRATOR, session_id=sid)
    conn.close()

    assert cli.main(["--path", repo, "--lessons"]) == 0
    out = capsys.readouterr().out
    assert "AWAITING HUMAN REVIEW" in out and "lesson lead" in out

    assert cli.main(["--path", repo, "--promote-lesson", str(lid)]) == 0
    assert "active" in capsys.readouterr().out
    l = dao.get_lesson(dao.open_project(repo), lid)
    assert l["status"] == "active" and not l["ground_truth"]

    assert cli.main(["--path", repo, "--retire-lesson", str(lid)]) == 0
    capsys.readouterr()
    assert dao.get_lesson(dao.open_project(repo), lid)["status"] == "retired"

    assert cli.main(["--path", repo, "--promote-lesson", "999"]) == 1
    assert "999" in capsys.readouterr().err
