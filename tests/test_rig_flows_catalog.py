"""Catalog and contract tests for built-in flows."""















from __future__ import annotations

import json
import os
import subprocess

from rig.core.result import Result
from rig.flow import resolve
from rig.flow.execute import PhaseExecutor
from rig.flow.phase import Phase
from rig.tools.spawn import Budget, Spawner
from rig.transport.base import ExecResult
from rig.util import content

CATALOG = ["feature/small", "feature/large", "bug/small", "bug/large",
           "project/bootstrap", "arch/design", "review/only", "research/audit",
           "refactor/migrate", "test/backfill"]
CONVERSE_FLOWS = ["feature/large", "bug/large", "project/bootstrap", "arch/design",
                  "refactor/migrate"]



def test_catalog_flows_resolve_and_validate():

    for name in CATALOG:
        phases = resolve.resolve_phases(name)
        assert phases, name
        assert all(p.agent for p in phases), name


def test_base_is_alias_of_feature_small():


    base = resolve.resolve_phases("base")
    small = resolve.resolve_phases("feature/small")
    assert [vars(p) for p in base] == [vars(p) for p in small]


def test_converse_flows_cut_correctly():


    for name in CONVERSE_FLOWS:
        pre, gate, post = resolve.split_at_gate(resolve.resolve_phases(name))
        assert gate is not None and gate.agent == "converse" and gate.about == "plan", name
        pre_notifies = [p for p in pre if p.agent == "notify"]

        assert pre_notifies and all(p.action == "reply" for p in pre_notifies), name
        assert any(p.agent == "plan" for p in pre), name
        assert any(p.agent == "units" for p in post), name
        assert not any(p.agent == "units" for p in pre), name


def test_bug_flows_start_with_repro_barrier():

    for name in ("bug/small", "bug/large"):
        first = resolve.resolve_phases(name)[0]
        assert first.agent == "repro" and first.kind == "readonly", name
        assert first.perms == "read" and first.on_fail == "stop", name


def test_bug_plans_demand_regression_test():

    for name in ("bug/small", "bug/large"):
        txt = content.load(f"{name}/prompts/plan.md", default="")
        assert "regression test" in txt and "fail" in txt, name


def test_bootstrap_gate_agreement_is_explicit():


    phases = resolve.resolve_phases("project/bootstrap")
    ping = next(p for p in phases if p.name == "notify-plan")
    assert ".rig/gate.json" in ping.prompt
    plan_txt = content.load("project/bootstrap/prompts/plan.md", default="")
    assert "gate.json" in plan_txt and "do not bypass" in plan_txt


def test_readonly_flows_have_no_writers():

    for name in ("review/only", "research/audit"):
        phases = resolve.resolve_phases(name)
        assert not any(p.kind == "writer" for p in phases), name
        assert all(p.perms == "read" for p in phases if p.agent in ("scout", "review")), name


def test_role_prompts_follow_s5_contract():


    for fname in ("scout", "plan", "critic", "judge", "worker", "repro"):
        txt = content.load(f"prompts/{fname}.md", default="")
        assert "cannot see" in txt.lower() or "only" in txt.lower(), fname
    for fname in ("plan", "worker"):
        assert "need_human" in content.load(f"prompts/{fname}.md", default=""), fname
    assert "reproduced" in content.load("prompts/repro.md", default="")



class _Caps:
    schema_output = True; effort = True; native_resume = True; agents_md_native = True


class TextAdapter:

    name = "fake"

    def __init__(self, text="", ok=True, status=None):
        self._text, self._ok, self._status = text, ok, status
        self.invoked = []

    def invoke(self, prompt, *, model, cwd, perms, effort, system_context_path,
               resume_session_id, timeout, risk, schema_path, allowed_tools=None, confine=False):
        self.invoked.append({"prompt": prompt, "perms": perms})
        return Result(ok=self._ok, text=self._text, exit_code=0 if self._ok else 1,
                      status=self._status, error=None if self._ok else "fail")

    def inject_context(self, method_text, cwd):
        return f"{cwd}/.rig/system-prompt.md"

    def capabilities(self):
        return _Caps()


def _planner_ex(adapter, runner=None):
    return PhaseExecutor(planner_adapter=adapter, planner_model="pm",
                         judge_adapter=adapter, judge_model="jm",
                         runner=runner, repo="/repo", cwd="/repo",
                         dimensions=("correctness",), judge_schema="/repo/.rig/verdict.json")



def test_repro_reproduced_is_green_and_threads_context():
    ad = TextAdapter('to find. {"reproduced": true, "mechanism": "off-by-one in f()", "repro": "pytest -k x", "why": "ok"}')
    res = _planner_ex(ad)(Phase(name="repro", agent="repro", perms="read"), {"task": "crashes f"})
    assert res.green is True and res.need_human is False
    assert "reproduced" in res.output["context"] and res.output["repro"]["mechanism"].startswith("off-by-one")
    assert ad.invoked[0]["perms"] == "read"
    assert "crashes f" in ad.invoked[0]["prompt"]


def test_repro_not_reproduced_escalates_need_human():
    ad = TextAdapter('{"reproduced": false, "why": "behavior accordance dock"}')
    res = _planner_ex(ad)(Phase(name="repro", agent="repro"), {"task": "bug?"})
    assert res.green is False and res.need_human is True and "NOT reproduction" in res.detail


def test_repro_no_verdict_fails_closed_to_human():
    ad = TextAdapter("much prose without JSON")
    res = _planner_ex(ad)(Phase(name="repro", agent="repro"), {"task": "x"})
    assert res.green is False and res.need_human is True and "fail-closed" in res.detail


def test_repro_infra_fail_is_not_escalation():

    ad = TextAdapter("", ok=False)
    res = _planner_ex(ad)(Phase(name="repro", agent="repro"), {"task": "x"})
    assert res.green is False and res.need_human is False



class DiffRunner:

    def __init__(self):
        self.calls = []

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.calls.append(cmd)
        if "diff" in cmd:
            return ExecResult(0, "DIFF-WORKTREE-MARK", "", 0.0)
        return ExecResult(0, "", "", 0.0)

    def write_file(self, path, content):
        pass


class RecJudge(TextAdapter):
    def __init__(self):
        super().__init__(text='{"ok": true, "why": "ok"}')

    def invoke(self, prompt, **kw):
        r = super().invoke(prompt, **kw)
        return Result(ok=True, text=self._text, exit_code=0,
                      schema_applied=(True if kw.get("schema_path") else None))


def test_review_only_falls_back_to_task_and_git_diff():

    judge = RecJudge()
    runner = DiffRunner()
    ex = PhaseExecutor(judge_adapter=judge, judge_model="jm", runner=runner, repo="/repo", cwd="/repo",
                       dimensions=("correctness",), judge_schema="/repo/.rig/verdict.json")
    res = ex(Phase(name="review", agent="review", perms="read"), {"task": "verify changes login"})
    assert res.green is True
    assert any("diff" in c for c in runner.calls)
    jp = judge.invoked[0]["prompt"]
    assert "DIFF-WORKTREE-MARK" in jp and "verify changes login" in jp


def test_review_explicit_ctx_diff_wins():
    judge = RecJudge()
    runner = DiffRunner()
    ex = PhaseExecutor(judge_adapter=judge, judge_model="jm", runner=runner, repo="/repo", cwd="/repo",
                       dimensions=("correctness",), judge_schema="/repo/.rig/verdict.json")
    ex(Phase(name="review", agent="review"), {"task": "t", "diff": "EXPLICIT-DIFF"})
    assert "EXPLICIT-DIFF" in judge.invoked[0]["prompt"] and not any("diff" in c for c in runner.calls)



def test_planner_self_reports_need_human_halts():
    ad = TextAdapter('{"status": "need_human", "why": "two mutually exclusive demand"}',
                     status={"status": "need_human"})
    res = _planner_ex(ad)(Phase(name="plan", agent="plan"), {"task": "contradiction"})
    assert res.green is False and res.need_human is True and "need_human" in res.detail



def test_build_executor_wires_budget_cap():
    from rig.entry.cli import build_executor
    from rig.transport.local import LocalRunner
    ex = build_executor(LocalRunner(), "/repo", None, None, worker="claude", judge="claude",
                        worker_model="m", judge_model="m", budget_cap=2.5)
    assert ex.spawner.budget.cap == 2.5
    ex2 = build_executor(LocalRunner(), "/repo", None, None, worker="claude", judge="claude",
                         worker_model="m", judge_model="m")
    assert ex2.spawner.budget.cap is None


def test_unit_spawn_reserves_est_cost_and_cap_refuses():


    from rig.core.types import Plan, Unit
    worker = TextAdapter("DIFF")
    spawner = Spawner(adapter=worker, repo="/repo", model="m", max_depth=3, budget=Budget(cap=0.1))
    ex = PhaseExecutor(spawner=spawner, judge_adapter=RecJudge(), judge_model="jm",
                       runner=DiffRunner(), repo="/repo", cwd="/repo", dimensions=("correctness",),
                       judge_schema="/repo/.rig/verdict.json")
    u = Unit(id="u1", instruction="implement")
    res = ex(Phase(name="u1", agent="unit"), {"unit": u, "plan": Plan(goal="g", units=[u], gate_cmd=["t"]),
                                              "index": 1, "total": 1, "prior_green": []})
    assert res.green is False and "spawn FAIL" in res.detail and "cost-cap" in res.detail
    assert not worker.invoked


def test_bg_flow_budget_is_session_remainder(tmp_path):

    from rig.db import dao
    from rig.entry.agent_cli import _caps, _ledger
    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s", config={"budget_cap": 5.0})
    dao.log_run(conn, session_id=sid, kind="invoke", agent="worker", cost=1.5, result="x")
    bcap, spent = _caps(conn, sid)[1], _ledger(conn, sid)[1]
    assert bcap == 5.0 and spent == 1.5
    assert max(0.0, bcap - spent) == 3.5



def _git(repo, *a):
    subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=False)


def _seed_repo(tmp_path) -> str:
    repo = str(tmp_path / "proj")
    os.makedirs(repo)
    with open(os.path.join(repo, "f.txt"), "w") as fh:
        fh.write("x")
    _git(repo, "init", "-q"); _git(repo, "config", "user.email", "s@x"); _git(repo, "config", "user.name", "s")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "seed")
    return repo


def test_resolve_notify_falls_back_to_meta():
    from rig.db import dao
    from rig.entry.cli import NOTIFY_META_KEY, resolve_notify
    conn = dao.connect(":memory:")
    assert resolve_notify(conn, None) is None
    dao.set_meta(conn, NOTIFY_META_KEY, "telegram")
    assert resolve_notify(conn, None) == "telegram"
    assert resolve_notify(conn, "webhook") == "webhook"


def test_set_notify_writes_meta_and_exits(tmp_path):
    from rig.db import dao
    from rig.entry import cli
    repo = _seed_repo(tmp_path)
    assert cli.main(["--path", repo, "--set-notify", "telegram"]) == 0
    assert dao.get_meta(dao.open_project(repo), cli.NOTIFY_META_KEY, "") == "telegram"
    assert cli.main(["--path", repo, "--set-notify", "none"]) == 0
    assert dao.get_meta(dao.open_project(repo), cli.NOTIFY_META_KEY, "") == ""


def test_notify_prompt_path_form_loads_file(monkeypatch, tmp_path):


    from rig.flow import notify as notify_mod
    from rig.tools.send import SendResult
    import rig.tools as tools_pkg
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "ping.md").write_text("complete #{{session_id}}", encoding="utf-8")
    monkeypatch.setenv("RIG_CONTENT_DIR", str(tmp_path))
    sent = {}
    monkeypatch.setattr(tools_pkg, "send",
                        lambda ch, txt, payload=None: (sent.update(ch=ch, txt=txt),
                                                       SendResult(channel=ch, sent=True))[1])
    ph = Phase(name="n", agent="notify", prompt="custom/ping.md")
    notify_mod.notify_phase(ph, {"notify_channel": "telegram", "session_id": 7})
    assert sent["txt"] == "rig #7\nINFORMATION: complete #7"



def test_parser_flow_default_is_none():
    from rig.entry import cli
    args = cli._build_parser().parse_args(["task"])
    assert args.flow is None


def test_no_flow_routes_to_lead(monkeypatch, tmp_path):
    from rig.entry import cli, lead as lead_mod
    repo = _seed_repo(tmp_path)
    called = {}

    def fake_lead(conn, runner, r, **kw):
        called.update(kw)
        return {"ok": True, "session_id": 1, "result": None, "flow_result": None,
                "record": {"status": "done", "handoff": "ok", "in_flight": []}}
    monkeypatch.setattr(lead_mod, "cmd_lead", fake_lead)
    assert cli.main(["implement feature", "--path", repo]) == 0
    assert called.get("task") == "implement feature"


def test_explicit_flow_routes_to_cmd_new(monkeypatch, tmp_path):
    from rig.entry import cli
    repo = _seed_repo(tmp_path)
    called = {}

    monkeypatch.setattr(cli, "_scan_fn", lambda *a, **k: (lambda conn, project: None))
    monkeypatch.setattr(cli, "cmd_new",
                        lambda conn, runner, r, ex, **kw: (called.update(kw),
                                                           {"mode": "autonomous", "session": None,
                                                            "result": None, "record": {"status": "done", "handoff": "kh"},
                                                            "plan": None})[1])
    monkeypatch.setattr(cli, "render_new", lambda res, *a, **k: "ok")
    assert cli.main(["implement feature", "--path", repo, "--flow", "feature/small"]) == 0
    assert called.get("flow") == "feature/small"


def test_env_rig_flow_still_wins(monkeypatch):
    from rig.entry import cli
    monkeypatch.setenv("RIG_FLOW", "feature/large")
    args = cli._build_parser().parse_args(["task"])
    assert args.flow == "feature/large"


def test_converse_notify_plan_templates_carry_plan_and_action():



    import json as _json
    import os as _os
    flows = ["feature/large", "bug/large", "arch/design", "project/bootstrap", "lead/dev-feature",
             "refactor/migrate"]
    for f in flows:
        with open(_os.path.join("flows", f, "flow.json"), encoding="utf-8") as fh:
            doc = _json.load(fh)
        phases = [p for p in doc["phases"] if p.get("name") == "notify-plan"]
        assert phases, f"{f}: no notify-plan phase"
        ph = phases[0]
        assert ph.get("action") in ("reply", "approve"), f"{f}: no action-lever"
        assert "{{plan}}" in ph.get("prompt", ""), f"{f}: template without {{plan}}"
        assert "{{repo}}" not in ph["prompt"] and "<repository>" not in ph["prompt"], f"{f}: path in template"


def test_no_notify_template_duplicates_envelope():



    import glob as _glob
    import json as _json
    for path in _glob.glob("flows/**/flow.json", recursive=True):
        doc = _json.load(open(path, encoding="utf-8"))
        for ph in doc.get("phases", []):
            if ph.get("agent") == "notify" and ph.get("prompt"):
                assert "rig #" not in ph["prompt"], f"{path}: envelope in template ({ph['name']})"


def test_arch_design_variant_panel_structure():




    phases = resolve.resolve_phases("arch/design")
    names = [p.name for p in phases]
    variants = [p for p in phases if p.name.startswith("variant-")]
    assert len(variants) == 3
    assert names.index("scout") < names.index("variant-simple") < names.index("plan")
    lenses = set()
    for p in variants:
        assert p.agent == "scout" and p.kind == "readonly" and p.perms == "read"
        txt = content.load(p.prompt, default="")
        assert txt and "{{TASK}}" in txt, p.name
        assert "{{CONTEXT}}" not in txt, f"{p.name}: lens sees stranger sketch — independence demolition"
        assert "Other designers" in txt, p.name
        lenses.add(txt.splitlines()[0])
    assert len(lenses) == 3
    plan_txt = content.load("arch/design/prompts/plan.md", default="")
    assert "SYNTHESIZER" in plan_txt and "sketch" in plan_txt.lower()


def test_discovery_flows_have_completeness_pass():




    for flow, first, second in (("refactor/migrate", "scout", "coverage"),
                                ("research/audit", "research", "completeness")):
        phases = resolve.resolve_phases(flow)
        names = [p.name for p in phases]
        assert names.index(second) == names.index(first) + 1, flow
        ph = phases[names.index(second)]
        assert ph.agent == "scout" and ph.kind == "readonly" and ph.perms == "read", flow
        assert ph.on_fail == "continue", flow
        txt = content.load(ph.prompt, default="")
        assert "{{CONTEXT}}" in txt and "COMPLETENESS" in txt, flow
        assert "If none" in txt or "If nothing" in txt
