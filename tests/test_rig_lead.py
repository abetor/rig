"""Tests for lead orchestration, guardrails, resumption, and tool wiring."""




from __future__ import annotations

import argparse

import pytest

from rig.db import dao
from rig.entry import agent_cli, cli, lead


@pytest.fixture
def conn():
    return dao.connect(":memory:")



def test_ledger_counts_only_worker_spawns_but_sums_all_cost(conn):
    sid = dao.create_session(conn, name="s")
    dao.log_run(conn, session_id=sid, kind="invoke", agent="worker", cost=0.05)
    dao.log_run(conn, session_id=sid, kind="invoke", agent="sub", cost=0.03)
    dao.log_run(conn, session_id=sid, kind="invoke", agent="lead", cost=0.10)
    dao.log_run(conn, session_id=sid, kind="note", agent="", cost=None)
    count, spent = agent_cli._ledger(conn, sid)
    assert count == 2
    assert spent == pytest.approx(0.18)


def test_caps_from_config_else_defaults(conn):
    s1 = dao.create_session(conn, name="a", config={"max_spawns": 5, "budget_cap": 2.0})
    assert agent_cli._caps(conn, s1) == (5, 2.0)
    s2 = dao.create_session(conn, name="b")
    assert agent_cli._caps(conn, s2) == (agent_cli.DEFAULT_MAX_SPAWNS, agent_cli.DEFAULT_BUDGET_CAP)
    s3 = dao.create_session(conn, name="c", config={"budget_cap": None})
    assert agent_cli._caps(conn, s3)[1] is None


def test_guard_passes_clean_session(conn):
    sid = dao.create_session(conn, name="s")
    assert agent_cli._guard(conn, sid) is None


def test_guard_refuses_over_spawn_cap(conn):
    sid = dao.create_session(conn, name="s", config={"max_spawns": 2})
    for _ in range(2):
        dao.log_run(conn, session_id=sid, kind="invoke", agent="worker", cost=0.01)
    reason = agent_cli._guard(conn, sid)
    assert reason is not None and "spawn-cap" in reason


def test_guard_refuses_over_budget_cap(conn):
    sid = dao.create_session(conn, name="s", config={"budget_cap": 0.05})
    dao.log_run(conn, session_id=sid, kind="invoke", agent="worker", cost=0.10)
    reason = agent_cli._guard(conn, sid)
    assert reason is not None and "budget-cap" in reason


def test_cmd_spawn_returns_REFUSED_before_calling_agent(tmp_path):

    repo = str(tmp_path)
    conn = dao.open_project(repo)
    sid = dao.create_session(conn, name="t", config={"max_spawns": 0})
    args = argparse.Namespace(repo=repo, session=sid, prompt="x", prompt_file=None,
                              harness="claude", model="m", perms="auto", timeout=5.0)
    assert agent_cli.cmd_spawn(args) == agent_cli.REFUSED


def test_ledger_counts_active_reservations_as_spawns(conn):


    sid = dao.create_session(conn, name="s")
    dao.log_run(conn, session_id=sid, kind="invoke", agent="worker", cost=0.01)
    assert agent_cli._ledger(conn, sid)[0] == 1
    ids = dao.try_reserve_spawns(conn, sid, slots=1, count_committed=lambda c: 1, max_spawns=9)
    assert agent_cli._ledger(conn, sid)[0] == 2
    dao.release_spawns(conn, ids)
    assert agent_cli._ledger(conn, sid)[0] == 1


def test_cmd_spawn_releases_reservation_after_call(tmp_path, monkeypatch):


    from rig.core.result import Result
    repo = str(tmp_path)
    conn = dao.open_project(repo)
    sid = dao.create_session(conn, name="t", config={"max_spawns": 5})

    def fake_agent(adapter, prompt, **kw):

        dao.log_run(kw["conn"], session_id=kw["session_id"], kind="invoke", agent="worker",
                    prompt=prompt, result="done", ok=True, cost=0.02)
        return Result(ok=True, text="done", exit_code=0, cost_usd=0.02)

    monkeypatch.setattr("rig.core.launch.agent", fake_agent)
    args = argparse.Namespace(repo=repo, session=sid, prompt="do x", prompt_file=None,
                              harness="claude", model="m", perms="auto", timeout=5.0)
    assert agent_cli.cmd_spawn(args) == 0
    assert dao.count_active_reservations(conn, sid) == 0
    assert agent_cli._ledger(conn, sid)[0] == 1



def test_compose_has_task_session_and_all_tools():

    p = lead.compose_lead_prompt("implement X", 7)
    assert "implement X" in p
    assert "7" in p
    for tool in ("recall", "run-flow", "spawn", "fanout", "remember", "journal"):
        assert tool in p, f"tool {tool} not visible lead in prompt"


def _agent_cli_subcommands():

    ap = agent_cli._build_parser()
    subs = [a for a in ap._actions if isinstance(a, argparse._SubParsersAction)]
    return set(subs[0].choices.keys())


def test_lead_prompt_surfaces_every_agent_cli_subcommand():


    surfaced = lead.compose_lead_prompt("t", 1) + "\n" + lead.LEAD_METHOD
    missing = [c for c in _agent_cli_subcommands() if c not in surfaced]
    assert not missing, f"subcommands rig-tool not visible lead (drift parser↔prompt): {missing}"


def test_record_lead_end_writes_session_status_and_note(tmp_path):

    import subprocess
    from types import SimpleNamespace as NS
    from rig.transport.local import LocalRunner
    repo = str(tmp_path)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@l",
                    "commit", "--allow-empty", "-q", "-m", "init"], check=True)
    conn = dao.open_project(repo)
    sid = dao.create_session(conn, name="s", task="X")
    rec = lead.record_lead_end(conn, LocalRunner(), repo, sid, NS(ok=True, text="complete\nSTATUS: done"))
    assert rec["status"] == "done"
    s = dao.get_session(conn, sid)
    assert s["status"] == "done" and s["head"]
    notes = [r for r in dao.list_runs(conn, session_id=sid) if r["kind"] == "note"]
    assert notes and "complete" in notes[-1]["result"]


def test_compose_injects_memory_and_journal():
    p = lead.compose_lead_prompt("t", 1, memory=["lesson-alpha"], journal="[plan/ok] something")
    assert "lesson-alpha" in p
    assert "[plan/ok] something" in p
    assert "RESUME INTERRUPTED WORK" in p


def test_compose_no_journal_no_resume_marker():
    p = lead.compose_lead_prompt("t", 1)
    assert "RESUME INTERRUPTED WORK" not in p


def test_compose_injects_human_reply():

    p = lead.compose_lead_prompt("t", 1, human_reply="use Postgres, not SQLite")
    assert "HUMAN RESPONSE" in p and "use Postgres" in p


def test_persist_resume_id_stores_harness_session(conn):


    from rig.core.result import Result
    sid = dao.create_session(conn, name="s", flow="lead")
    ex = lead.LeadFlowExecutor(conn, _FakeRunner(), "/repo", lead_harness="claude")
    ex._persist_resume_id(sid, Result(ok=True, text="x", exit_code=0, session_id="harness-abc"))
    cfg = dao.get_session(conn, sid)["config"]
    assert cfg["lead_resume_id"] == "harness-abc" and cfg["lead_harness"] == "claude"

    ex._persist_resume_id(sid, Result(ok=True, text="x", exit_code=0, session_id=None))
    assert dao.get_session(conn, sid)["config"]["lead_resume_id"] == "harness-abc"


def test_warm_wake_uses_native_resume_when_live(conn, monkeypatch):


    from rig.core.result import Result
    sid = dao.create_session(conn, name="s", flow="lead")
    dao.update_session(conn, sid, config={"lead_resume_id": "h-1", "lead_harness": "claude"})
    captured = {}

    def fake_agent(adapter, prompt, **kw):
        captured.update(prompt=prompt, resume_id=kw.get("resume_id"))
        return Result(ok=True, text="continuer\nSTATUS: done", exit_code=0)

    class _Caps:
        native_resume = True
    monkeypatch.setattr("rig.core.launch.agent", fake_agent)
    adapter = type("A", (), {"capabilities": lambda self: _Caps()})()
    res = lead._warm_wake(conn, adapter, "/repo", session_id=sid, lead_harness="claude",
                          lead_model="m", timeout=5.0, human_reply="yes, variant WOULD", method_path=None)
    assert res is not None and res.ok
    assert captured["resume_id"] == "h-1"
    assert "yes, variant WOULD" in captured["prompt"] and "TASK" not in captured["prompt"]


def test_warm_wake_skips_when_no_id_or_other_harness(conn):


    sid = dao.create_session(conn, name="s", flow="lead")
    adapter = type("A", (), {"capabilities": lambda self: type("C", (), {"native_resume": True})()})()

    assert lead._warm_wake(conn, adapter, "/repo", session_id=sid, lead_harness="claude",
                           lead_model="m", timeout=5.0, human_reply="x", method_path=None) is None

    dao.update_session(conn, sid, config={"lead_resume_id": "h-1", "lead_harness": "codex"})
    assert lead._warm_wake(conn, adapter, "/repo", session_id=sid, lead_harness="claude",
                           lead_model="m", timeout=5.0, human_reply="x", method_path=None) is None



def test_journal_brief_empty_is_none(conn):
    sid = dao.create_session(conn, name="s")
    assert lead._journal_brief(conn, sid) is None


def test_journal_brief_summarizes_runs(conn):
    sid = dao.create_session(conn, name="s")
    dao.log_run(conn, session_id=sid, kind="invoke", agent="planner", phase="plan",
                ok=True, result="plan ready: 1 unit")
    dao.log_run(conn, session_id=sid, kind="invoke", agent="worker", phase="lead-spawn",
                ok=False, result="sub-agent fail")
    brief = lead._journal_brief(conn, sid)
    assert brief is not None
    assert "plan" in brief and "ok" in brief
    assert "FAIL" in brief


def test_resume_context_includes_mode_and_full_handoff(conn):


    sid = dao.create_session(conn, name="s", head="HEAD123", in_flight=["rig/u1"])
    long_handoff = "Sum PARTLY. " + "detail " * 40 + "END-MARKER-for-120"
    dao.log_run(conn, session_id=sid, kind="note", result=long_handoff)
    ctx = lead._resume_context(_FakeRunner(), conn, "/repo", sid)
    assert ctx is not None
    assert "MODE ENTRY" in ctx
    assert "END-MARKER-for-120" in ctx


def test_cmd_lead_resume_missing_session(conn):
    res = lead.cmd_lead(conn, None, "", task="", resume_sid=999)
    assert res["ok"] is False and "999" in res["detail"]



def test_parser_has_lead_flag():
    args = cli._build_parser().parse_args(["task", "--lead", "--lead-model", "claude-sonnet-4-6"])
    assert args.lead is True
    assert args.task == "task"
    assert args.lead_model == "claude-sonnet-4-6"


def test_parser_lead_defaults_off():
    args = cli._build_parser().parse_args(["task"])
    assert args.lead is False



def test_lead_status_process_ok_is_not_task_done():



    from types import SimpleNamespace as NS
    assert lead._lead_status(NS(ok=True, text="FINAL-REPORT: complete\nSTATUS: done")) == "done"
    assert lead._lead_status(NS(ok=True, text="FINAL-REPORT: to rest\nSTATUS: need_human")) == "need_human"
    assert lead._lead_status(NS(ok=True, text="x\nSTATUS: blocked")) == "need_human"
    assert lead._lead_status(NS(ok=False, text="STATUS: done")) == "partial"

    assert lead._lead_status(NS(ok=True, text="FINAL-REPORT: complete, escalation no")) == "need_human"
    assert lead._lead_status(NS(ok=True, text="Which subcommands necessary? 1..6")) == "need_human"


def test_record_lead_end_no_token_marks_and_anchors_report(tmp_path):



    import subprocess
    from types import SimpleNamespace as NS

    from rig.transport.local import LocalRunner
    repo = str(tmp_path)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@l",
                    "commit", "--allow-empty", "-q", "-m", "init"], check=True)
    conn = dao.open_project(repo)
    sid = dao.create_session(conn, name="s")
    long_prefix = "intrigue " * 120
    res = NS(ok=True, text=long_prefix + "\nQUESTIONS:\n1. Which subcommands necessary?\n2. argparse or click?")
    rec = lead.record_lead_end(conn, LocalRunner(), repo, sid, res)
    assert rec["status"] == "need_human"
    assert "omitted STATUS" in rec["handoff"]
    assert "Which subcommands necessary" in rec["handoff"]

    res2 = NS(ok=True, text=long_prefix + "\nFINAL-REPORT: everything complete\nSTATUS: done")
    rec2 = lead.record_lead_end(conn, LocalRunner(), repo, sid, res2)
    assert rec2["status"] == "done" and rec2["handoff"].startswith("FINAL-REPORT")


def test_run_flow_refuses_over_budget(tmp_path):

    repo = str(tmp_path)
    conn = dao.open_project(repo)
    sid = dao.create_session(conn, name="t", config={"budget_cap": 0.01})
    dao.log_run(conn, session_id=sid, kind="invoke", agent="worker", cost=0.50)
    args = argparse.Namespace(repo=repo, session=sid, flow="lead/dev-feature", task="x",
                              harness="claude", model="m", judge=None, judge_model="m")
    assert agent_cli.cmd_run_flow(args) == agent_cli.REFUSED


def test_agent_background_rejects_allowed_tools():

    from rig.core.launch import agent
    with pytest.raises(ValueError, match="allowed_tools"):
        agent(object(), "p", wait="background", model="m", cwd="/tmp", allowed_tools="Bash")



def test_fanout_rejects_non_array_prompts(tmp_path):
    repo = str(tmp_path)
    sid = dao.create_session(dao.open_project(repo), name="t")
    args = argparse.Namespace(repo=repo, session=sid, prompts_json='{"x":1}',
                              harness="claude", model="m", timeout=5.0)
    assert agent_cli.cmd_fanout(args) == 2


def test_fanout_refuses_at_spawn_cap(tmp_path):
    repo = str(tmp_path)
    dao.create_session(dao.open_project(repo), name="t", config={"max_spawns": 0})
    args = argparse.Namespace(repo=repo, session=1, prompts_json='["a","b"]',
                              harness="claude", model="m", timeout=5.0)
    assert agent_cli.cmd_fanout(args) == agent_cli.REFUSED


def test_run_flow_bad_flow_json_returns_error(tmp_path):

    repo = str(tmp_path)
    dao.create_session(dao.open_project(repo), name="t")
    args = argparse.Namespace(repo=repo, session=1, flow="lead/dev-feature", flow_json="{invalid",
                              task="x", harness="claude", model="m", judge=None, judge_model="m")
    assert agent_cli.cmd_run_flow(args) == 1


def test_run_flow_accepts_bare_list_flow_json(tmp_path, monkeypatch):


    repo = str(tmp_path)
    sid = dao.create_session(dao.open_project(repo), name="t")
    spawned = {}
    def _fake_popen(*a, **k):
        spawned["cmd"] = a[0]
        return type("P", (), {"pid": 4242})()
    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    args = argparse.Namespace(repo=repo, session=sid, flow="base",
                              flow_json='[{"name":"scout","agent":"scout","kind":"readonly"}]',
                              task="x", harness="claude", model="m", judge=None, judge_model="m")
    assert agent_cli.cmd_run_flow(args) == 0
    assert spawned.get("cmd")


def test_live_flow_pid_detects_and_cleans(tmp_path):

    import os
    import subprocess
    p = str(tmp_path / "flow.pid")
    assert agent_cli._live_flow_pid(p) is None
    with open(p, "w") as f:
        f.write(str(os.getpid()))
    assert agent_cli._live_flow_pid(p) == os.getpid()
    dead = subprocess.Popen(["true"]); dead.wait()
    with open(p, "w") as f:
        f.write(str(dead.pid))
    assert agent_cli._live_flow_pid(p) is None
    assert not os.path.exists(p)


def test_run_flow_refuses_concurrent_live_flow(tmp_path):


    import os
    repo = str(tmp_path)
    sid = dao.create_session(dao.open_project(repo), name="t")
    with open(agent_cli._flow_pid_path(repo, sid), "w") as f:
        f.write(str(os.getpid()))
    args = argparse.Namespace(repo=repo, session=sid, flow="base", task="x",
                              harness="claude", model="m", judge=None, judge_model="m")
    assert agent_cli.cmd_run_flow(args) == agent_cli.REFUSED


def test_bg_worker_crash_writes_terminal_note(tmp_path, monkeypatch):


    repo = str(tmp_path)
    sid = dao.create_session(dao.open_project(repo), name="t")

    def _boom(*a, **k):
        raise RuntimeError("phase lopnula")
    monkeypatch.setattr("rig.flow.run_flow", _boom)
    args = argparse.Namespace(repo=repo, session=sid, flow="base", flow_json=None, task="x",
                              harness="claude", model="m", judge="claude", judge_model="m", bg_worker=True)
    assert agent_cli.cmd_run_flow(args) == 1
    notes = [r for r in dao.list_runs(dao.open_project(repo), session_id=sid) if r["kind"] == "note"]
    assert any("[flow fail]" in (n["result"] or "") for n in notes)


def test_parser_has_fanout_and_flowjson():
    p = agent_cli._build_parser()
    a = p.parse_args(["fanout", "--repo", ".", "--session", "1", "--prompts-json", "[]"])
    assert a.fn is agent_cli.cmd_fanout
    b = p.parse_args(["run-flow", "--repo", ".", "--session", "1", "--task", "t",
                      "--flow-json", '{"phases":[]}'])
    assert b.flow_json == '{"phases":[]}'



class _FakeRunner:

    def exec(self, cmd, **kw):
        from rig.transport.base import ExecResult
        return ExecResult(0, "HEAD123", "", 0.0)


def _fake_lead_result(text="FINAL-REPORT: done\nSTATUS: done", ok=True):
    from rig.core.result import Result
    return Result(ok=ok, text=text, exit_code=0 if ok else 1)


def test_orchestrate_flow_resolves_to_two_phases():

    from rig.flow.resolve import resolve_phases
    phases = resolve_phases(lead.LEAD_FLOW)
    assert [p.agent for p in phases] == ["orchestrator", "notify"]

    assert all(p.on_fail == "continue" for p in phases)


def test_orchestrator_method_is_real_data_not_stub():

    from rig.util import content
    md = content.load(lead.LEAD_METHOD_FILE, default="")
    assert md.strip() and "STATUS:" in md and "run-flow" in md


def test_lead_executor_runs_orchestrator_then_notify(conn, monkeypatch):

    import rig.tools as tools_pkg
    from rig.flow import run_flow
    from rig.flow.resolve import resolve_phases
    from rig.tools.send import SendResult
    monkeypatch.setattr(lead, "launch_lead", lambda *a, **k: _fake_lead_result())
    monkeypatch.setattr(lead, "record_lead_end",
                        lambda *a, **k: {"status": "done", "head": "H", "in_flight": ["rig/u1"],
                                         "handoff": "to make X in rig/u1"})
    captured = {}
    monkeypatch.setattr(tools_pkg, "send",
                        lambda ch, txt, payload=None: (captured.update(ch=ch, txt=txt, payload=payload),
                                                       SendResult(channel=ch, sent=True))[1])
    ex = lead.LeadFlowExecutor(conn, _FakeRunner(), "/repo")
    fres = run_flow(resolve_phases(lead.LEAD_FLOW), ex,
                    ctx={"task": "t", "session_id": 1, "notify_channel": "telegram"}, base="HEAD")
    assert [r["name"] for r in fres.results] == ["orchestrator", "notify"]
    assert fres.ok
    assert captured["ch"] == "telegram" and "to make X" in captured["txt"]


def test_lead_executor_notify_graceful_when_no_channel(conn, monkeypatch):

    from rig.flow import run_flow
    from rig.flow.resolve import resolve_phases
    monkeypatch.setattr(lead, "launch_lead", lambda *a, **k: _fake_lead_result())
    monkeypatch.setattr(lead, "record_lead_end",
                        lambda *a, **k: {"status": "done", "head": "H", "in_flight": [], "handoff": "ok"})
    ex = lead.LeadFlowExecutor(conn, _FakeRunner(), "/repo")
    fres = run_flow(resolve_phases(lead.LEAD_FLOW), ex,
                    ctx={"task": "t", "session_id": 1, "notify_channel": None}, base="HEAD")
    nres = [r for r in fres.results if r["name"] == "notify"][0]
    assert nres["green"] and "not to inquire" in nres["detail"]


def test_cmd_lead_ok_does_not_depend_on_notify_delivery_failure(conn, monkeypatch):

    import rig.tools as tools_pkg
    from rig.tools.send import SendResult
    monkeypatch.setattr(lead, "launch_lead", lambda *a, **k: _fake_lead_result(ok=True))
    monkeypatch.setattr(lead, "record_lead_end",
                        lambda *a, **k: {"status": "done", "head": "H", "in_flight": [], "handoff": "ok"})
    monkeypatch.setattr(tools_pkg, "send",
                        lambda ch, txt, payload=None: SendResult(channel=ch, sent=False, reason="to depend to lie (down)"))
    res = lead.cmd_lead(conn, _FakeRunner(), "/repo", task="t", notify="telegram")
    assert res["ok"] is True
    nres = [r for r in res["flow_result"].results if r["name"] == "notify"][0]
    assert nres["green"] and "NOT to deliver" in nres["detail"]


def test_run_flow_bridge_is_async_spawns_detached_bg_worker(tmp_path, monkeypatch, capsys):



    import subprocess
    from types import SimpleNamespace
    repo = tmp_path / "proj"
    repo.mkdir()
    captured = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: (captured.update(argv=argv, kw=kw), SimpleNamespace(pid=1))[1])
    args = SimpleNamespace(repo=str(repo), session=1, harness="claude", model="wm", judge=None,
                           judge_model="", flow="base", flow_json=None, task="implement X", bg_worker=False)
    rc = agent_cli.cmd_run_flow(args)
    assert rc == 0
    assert "--_bg-worker" in captured["argv"] and "run-flow" in captured["argv"]
    assert "implement X" in captured["argv"]
    assert captured["kw"].get("start_new_session") is True
    out = capsys.readouterr().out
    assert "[flow complete]" in out
    assert "[flow to complete]" not in out


def test_spawn_parser_rejects_write_perms():


    ap = agent_cli._build_parser()
    args = ap.parse_args(["spawn", "--repo", ".", "--session", "1", "--prompt", "x"])
    assert args.perms == "read"
    with pytest.raises(SystemExit):
        ap.parse_args(["spawn", "--repo", ".", "--session", "1", "--prompt", "x", "--perms", "auto"])



def test_journal_drift_hint_fires_after_stall_and_resets(tmp_path, capsys):



    import os
    repo = str(tmp_path)
    conn = dao.open_project(repo)
    sid = dao.create_session(conn, name="s")
    dao.log_run(conn, session_id=sid, kind="invoke", agent="worker", cost=0.0)
    args = argparse.Namespace(repo=repo, session=sid, harness="claude", model="m")
    for _ in range(3):
        assert agent_cli.cmd_journal(args) == 0
        assert "journal did not grow" not in capsys.readouterr().out
    agent_cli.cmd_journal(args)
    out = capsys.readouterr().out
    assert "journal did not grow" in out and "need_human" in out
    dao.log_run(conn, session_id=sid, kind="invoke", agent="worker", cost=0.0)
    agent_cli.cmd_journal(args)
    assert "not to grow (up)" not in capsys.readouterr().out
    assert os.path.exists(os.path.join(repo, ".rig", f"journal-poll-{sid}.json"))
