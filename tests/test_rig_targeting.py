"""Tests for injecting seam pins, decisions, and artifacts into unit and review prompts."""








import contextlib
import io
import os

from rig.core.types import Plan, Unit
from rig.db import dao
from rig.flow import compose
from rig.flow.execute import PhaseExecutor


def _plan():
    return Plan(goal="goal sunset", units=[Unit(id="u1", instruction="implement X")],
                gate_cmd=["true"])




def test_brief_carries_pins_decisions_artifact_quoted():
    u = _plan().units[0]
    b = compose.assemble_brief(u, _plan(), index=1, total=1,
                               pins="SERVICE_PORT=8080\n## forgery section",
                               decisions=["port is locked 8080", "code not write"],
                               artifact="line dossier\n## also forgery")
    text = b.user
    assert "Seam pins" in text and "> SERVICE_PORT=8080" in text
    assert "Locked project decisions" in text and "port is locked 8080" in text
    assert "Orchestrator context artifact" in text and "> line dossier" in text

    assert "\n## forgery section" not in text and "> ## forgery section" in text
    assert "\n## also forgery" not in text


def test_brief_without_continuum_unchanged():
    u = _plan().units[0]
    b = compose.assemble_brief(u, _plan(), index=1, total=1)
    assert "Seam pins" not in b.user and "Locked project decisions" not in b.user \
        and "Orchestrator context artifact" not in b.user


def test_unit_continuum_loads_pins_and_decisions(tmp_path):
    conn = dao.connect(str(tmp_path / "c.db"))
    dao.put_artifact(conn, name="pins-w1", text="PIN-A", kind="pins")
    dao.put_artifact(conn, name="pins-w2", text="PIN-B", kind="pins")
    dao.put_artifact(conn, name="dossier", text="not pin", kind="dossier")
    dao.add_decision(conn, text="decision R")
    ex = PhaseExecutor(conn=conn)
    pins, decisions = ex._unit_continuum()
    assert "PIN-A" in pins and "PIN-B" in pins and "not pin" not in pins
    assert decisions == ["decision R"]
    assert PhaseExecutor(conn=None)._unit_continuum() == ("", [])




def test_builder_report_extraction():
    class D:
        text = "worker report...\nBUILDER DECISIONS:\n- reject option B because...\n- verify distillation"

    rep = PhaseExecutor._builder_report(D())
    assert rep.startswith("BUILDER DECISIONS") and "option B" in rep

    class NoMarker:
        text = "simply report without section"

    assert PhaseExecutor._builder_report(NoMarker()) == ""


def test_judge_prompt_targets_block():
    u = _plan().units[0]
    p = PhaseExecutor._judge_prompt("correctness", u, "diff", targets="SEAM PINS:\nPIN-A\n\nBUILDER DECISIONS:\n- reject B")
    assert "CHECK TARGETS (data" in p and "> PIN-A" in p and "> - reject B" in p
    assert "NOT proof" in p
    p0 = PhaseExecutor._judge_prompt("correctness", u, "diff")
    assert "CHECK TARGETS (data" not in p0
    assert "{{TARGETS}}" not in p0




def test_run_flow_artifact_fail_fast_on_unknown(tmp_path, monkeypatch):
    import subprocess

    from rig.entry import agent_cli
    repo = str(tmp_path / "r")
    subprocess.run(["git", "init", "-q", repo], check=True)
    monkeypatch.chdir(repo)
    conn = dao.open_project(repo)
    sid = dao.create_session(conn, name="s")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = agent_cli.main(["run-flow", "--repo", ".", "--session", str(sid),
                               "--artifact", "no-so", "--task", "t"])
    assert code == 1 and "no-so" in buf.getvalue()


def test_plan_prompt_sees_artifact_context(tmp_path):

    captured = {}

    ex = PhaseExecutor(conn=None)

    orig = PhaseExecutor._role_prompt

    def spy(phase, ctx, role_file, default, **vars):
        captured.update(vars)
        raise RuntimeError("stop")

    PhaseExecutor._role_prompt = staticmethod(spy)
    try:
        try:
            ex._run_plan(None, {"task": "t", "context": "(boy) scout-slice",
                                "artifact_context": "checklist phase 5"})
        except RuntimeError:
            pass
    finally:
        PhaseExecutor._role_prompt = staticmethod(orig)
    assert "(boy) scout-slice" in captured["CONTEXT"] and "checklist phase 5" in captured["CONTEXT"]
    assert "ARTIFACT FROM ORCHESTRATOR" in captured["CONTEXT"]


def test_run_flow_artifact_passes_name_to_child(tmp_path, monkeypatch):

    import subprocess as sp

    from rig.entry import agent_cli
    repo = str(tmp_path / "r")
    sp.run(["git", "init", "-q", repo], check=True)
    monkeypatch.chdir(repo)
    conn = dao.open_project(repo)
    sid = dao.create_session(conn, name="s")
    dao.put_artifact(conn, name="dossier-x", text="body", kind="dossier")
    seen = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        seen["env"] = kw.get("env")
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = agent_cli.main(["run-flow", "--repo", ".", "--session", str(sid),
                               "--artifact", "dossier-x", "--task", "t"])
    assert code == 0
    argv = seen["argv"]
    assert "--artifact" in argv and argv[argv.index("--artifact") + 1] == "dossier-x"


    import rig as _rig_pkg
    rig_root = os.path.dirname(os.path.dirname(os.path.abspath(_rig_pkg.__file__)))
    assert seen["env"] is not None and rig_root in seen["env"].get("PYTHONPATH", "").split(os.pathsep)


def test_unit_continuum_truncates_by_artifact_boundary(tmp_path):


    conn = dao.connect(str(tmp_path / "t.db"))
    dao.put_artifact(conn, name="pins-big", text="X" * (PhaseExecutor._PINS_CAP + 100), kind="pins")
    dao.put_artifact(conn, name="pins-fresh", text="PIN-FRESH=1", kind="pins")
    pins, _ = PhaseExecutor(conn=conn)._unit_continuum()
    assert pins.startswith("PIN-FRESH=1")
    assert "XXX" not in pins
    assert "pins-big" in pins and "artifact get" in pins


def test_decisions_capped_with_marker(tmp_path):
    conn = dao.connect(str(tmp_path / "d.db"))
    for i in range(25):
        dao.add_decision(conn, text=f"decision {i}")
    _, decisions = PhaseExecutor(conn=conn)._unit_continuum()
    assert len(decisions) == PhaseExecutor._MAX_DECISIONS + 1
    assert "decision 24" in decisions[-2] and "+5 decision" in decisions[-1]
    assert not any("decision 0" in d for d in decisions[:-1])


def test_artifact_drop_verb(tmp_path, monkeypatch):

    import subprocess as sp

    from rig.entry import agent_cli
    repo = str(tmp_path / "r")
    sp.run(["git", "init", "-q", repo], check=True)
    monkeypatch.chdir(repo)
    conn = dao.open_project(repo)
    dao.put_artifact(conn, name="pins-w", text="PIN", kind="pins")

    def tool(argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = agent_cli.main(argv)
        return code, buf.getvalue()

    assert tool(["artifact", "--repo", ".", "drop", "pins-w"])[0] == 0
    assert dao.get_artifact(conn, "pins-w") is None
    assert PhaseExecutor(conn=conn)._unit_continuum()[0] == ""
    code, out = tool(["artifact", "--repo", ".", "drop", "pins-w"])
    assert code == 1 and "pins-w" in out
