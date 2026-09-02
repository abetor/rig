"""Human-facing CLI for running flows, resuming sessions, and inspecting state."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from typing import List, Optional

from ..adapters.base import ENGINE_DIR
from ..checks import review as review_check
from ..checks.validate_plan import validate_plan
from ..core.types import plan_to_dict
from ..db import dao
from ..flow import run_flow
from ..flow.execute import PhaseExecutor
from ..flow.resolve import CONVERSE_AGENT, resolve_phases, split_at_checkpoint, split_at_gate
from ..memory import boot as run_boot
from ..tools.spawn import Spawner
from ..util import content, git
from . import init as init_mod
from . import session as session_mod
from . import warmup as warmup_mod


def resolve_repo(repo_arg: str, *, runner=None):
    """Resolve and validate a Git repository before creating engine state."""
    repo = os.path.abspath(repo_arg or ".")
    if not os.path.isdir(repo):
        print(f"rig: folder does not exist: {repo}", file=sys.stderr)
        return None, None
    if runner is None:
        from ..transport.local import LocalRunner
        runner = LocalRunner()
    chk = runner.exec(["git", "-C", repo, "rev-parse", "--is-inside-work-tree"], cwd=repo, risk="low")
    if not (chk.ok and chk.stdout.strip() == "true"):
        print(f"rig: not a Git repository: {repo} (run `git init` first)", file=sys.stderr)
        return None, None
    return repo, runner


def make_adapter(name: str, runner):
    """Build a Claude or Codex adapter using shared per-call permissions."""
    from ..adapters.claude import ClaudeAdapter
    from ..adapters.codex import CodexAdapter
    table = {"claude": ClaudeAdapter, "codex": CodexAdapter}
    if name not in table:
        raise ValueError(f"rig: unknown harness {name!r} (exists {tuple(table)})")
    return table[name](runner)


def model_for_harness(harness: str, model: str) -> str:
    """Return the default model name appropriate for a harness."""
    if harness == "codex" and (model or "").lower().startswith("claude"):
        return ""
    return model


def build_executor(runner, repo: str, conn, session_id: Optional[int], *, worker: str, judge: str,
                   worker_model: str, judge_model: str,
                   budget_cap: Optional[float] = None) -> PhaseExecutor:
    """Construct a phase executor with worker, judge, schemas, and budgets."""
    from ..tools.spawn import Budget
    worker_ad = make_adapter(worker, runner)
    worker_model = model_for_harness(worker, worker_model)
    judge_model = model_for_harness(judge, judge_model)
    spawner = Spawner(adapter=worker_ad, repo=repo, model=worker_model, conn=conn,
                      session_id=session_id, max_depth=4, budget=Budget(cap=budget_cap))
    return PhaseExecutor(
        spawner=spawner, judge_adapter=make_adapter(judge, runner), judge_model=judge_model,
        planner_adapter=worker_ad, planner_model=worker_model,
        runner=runner, repo=repo, conn=conn, cwd=repo,
        judge_schema=os.path.join(repo, ENGINE_DIR, "verdict.json"),
        plan_schema=os.path.join(repo, ENGINE_DIR, "plan.json"))


def _save_plan(conn, session_id: int, plan) -> None:
    """Persist a plan as a durable process-boundary checkpoint."""
    dao.log_run(conn, session_id=session_id, kind="plan", result=json.dumps(plan_to_dict(plan), ensure_ascii=False))


def _load_plan(conn, session_id: int):
    """Load and structurally validate the latest session plan."""
    notes = [r for r in dao.list_runs(conn, session_id=session_id) if r.get("kind") == "plan"]
    if not notes:
        return None
    try:
        pc = validate_plan(json.loads(notes[-1].get("result") or "{}"))
    except (ValueError, json.JSONDecodeError):
        return None
    return pc.plan if pc.ok else None


def _record_end(conn, runner, repo: str, session_id: int, fres, executor=None) -> dict:
    """Persist final session status, HEAD, in-flight branches, and handoff [E3]."""
    head = git.head(runner, repo)
    sess = dao.get_session(conn, session_id) or {}
    green = list(fres.ctx.get("green_branches") or getattr(executor, "_units_green_branches", []) or [])
    sb = fres.ctx.get("session_branch") or getattr(executor, "_session_branch", None)
    removed = []
    if fres.ok and sb:
        from ..flow.execute import gc_unit_branches
        removed = gc_unit_branches(runner, repo, green_branches=green, session_branch=sb)
        green = [b for b in green if b not in removed]
    git_ok, on_disk = git.branches_checked(runner, repo, "rig/")
    prior = set(sess.get("in_flight", []) or [])
    kept_prior = (prior & set(on_disk)) if git_ok else (prior - set(removed))
    in_flight = sorted(set(green) | kept_prior | ({sb} if sb else set()))
    status = "need_human" if getattr(fres, "need_human", False) else ("done" if fres.ok else "partial")
    tree_alert = git.dirty(runner, repo) is True \
        and (sess.get("config") or {}).get("tree_dirty_at_start") is False
    if tree_alert:
        status = "need_human"
    dao.update_session(conn, session_id, status=status, head=head, in_flight=in_flight)
    handoff = _handoff_text(fres, in_flight, session_branch=sb)
    if tree_alert:
        handoff = ("[A7-WATCHMAN] untracked changes appeared outside managed branches; "
                   "inspect and preserve or commit them before continuing. · " + handoff)
    dao.log_run(conn, session_id=session_id, kind="note", result=handoff)
    dao.update_session(conn, session_id, handoff=handoff)
    from ..core.events import emit
    from ..flow.notify import maybe_notify
    emit(conn, kind="session", name="end", session_id=session_id, status=status,
         params={"in_flight": len(in_flight)})
    maybe_notify(conn, kind="session", session_id=session_id,
                 text=f"rig #{session_id}: session completed · {status}")
    return {"status": status, "head": head, "in_flight": in_flight, "handoff": handoff}


def _handoff_text(fres, in_flight: list, session_branch: Optional[str] = None) -> str:
    """Build a concise handoff with green work, failures, and pending branches."""
    done = [r["name"] for r in fres.results if r["green"]]
    notdone = [r["name"] for r in fres.results if not r["green"]]
    L = [f"Summary: {'OK' if fres.ok else 'PARTIAL'} · green phases {len(done)}/{len(fres.results)}"]
    if getattr(fres, "need_human", False):
        ask = (fres.results[-1].get("detail") or "").strip() if fres.results else ""
        if len(ask) > 240:
            ask = ask[:240].rsplit(" ", 1)[0] + "..."
        L.append("HUMAN INPUT REQUIRED (need_human): "
                 + (ask or "executor requested a decision; inspect the journal")
                 + " → decide and continue with --reply or --approve.")
    if notdone:
        L.append("Not closed: " + ", ".join(notdone))
        if not getattr(fres, "need_human", False):
            first_red = next((r for r in fres.results if not r["green"]), None)
            reason = (first_red or {}).get("detail") or ""
            if reason.strip():
                cut_r = reason.strip()[:200]
                if len(reason.strip()) > 200:
                    cut_r = cut_r.rsplit(" ", 1)[0] + "..."
                L.append(f"reason ({first_red['name']}): {cut_r}")
    if session_branch:
        L.append(f"Session branch awaiting human merge: {session_branch}")
        rest = [b for b in in_flight if b != session_branch]
        if rest:
            L.append("Other branches on disk: " + ", ".join(rest))
    elif in_flight:
        L.append("Branches awaiting human merge: " + ", ".join(in_flight))
    report = fres.ctx.get("report")
    orphans = getattr(report, "orphan_branches", None) if report is not None else None
    if orphans:
        L.append("Branches outside the plan (inspect, merge, or remove): " + ", ".join(orphans))
    return " · ".join(L)


def cmd_list(conn) -> List[dict]:
    return dao.list_sessions(conn)


def render_list(sessions: List[dict]) -> str:
    if not sessions:
        return "rig · no sessions (start one with `rig \"<task>\"`)"
    L = ["rig · project sessions"]
    for s in sessions:
        L.append(f"  #{s['session_id']} [{s['status']}] {s['name']} · flow={s.get('flow') or '-'} "
                 f"· {s.get('task', '')[:60]}")
    return "\n".join(L)


def render_lessons(lessons: List[dict]) -> str:
    """Render pending and verified project lessons for human review."""
    if not lessons:
        return "rig · project memory is empty (lessons come from gate failures and agent submissions)"
    pending = [l for l in lessons if l.get("status") == "pending"]
    rest = [l for l in lessons if l.get("status") != "pending"]
    L = ["rig · project lessons (promote: `--promote-lesson ID`; retire: `--retire-lesson ID`)"]
    if pending:
        L.append(f"  AWAITING HUMAN REVIEW ({len(pending)} pending):")
    def line(l):
        gt = "·gt" if l.get("ground_truth") else ""
        feat = f" ·{l['feature']}" if l.get("feature") else ""
        return f"  #{l['lesson_id']} [{l['status']}{gt}] {l.get('kind', '')}{feat} · {(l.get('text') or '')[:100]}"
    L += [line(l) for l in pending] + [line(l) for l in rest]
    return "\n".join(L)


def cmd_new(conn, runner, repo: str, executor, *, task: str, flow: str = "base",
            agent: str = "claude", name: Optional[str] = None, notify: Optional[str] = None) -> dict:
    """Start a session and run the flow through its approval checkpoint."""
    started = session_mod.start_session(conn, runner, repo, name=name, task=task, flow=flow, agent=agent)
    pre, gate, post = split_at_gate(resolve_phases(flow))
    profile = dao.get_meta(conn, warmup_mod.PROFILE_META_KEY, "") or ""
    if profile:
        prof_head = dao.get_meta(conn, warmup_mod.PROFILE_HEAD_META_KEY, "") or ""
        if prof_head and prof_head != git.head(runner, repo):
            profile = "[BASELINE FROM AN EARLIER PROJECT STATE - it may be stale; verify it against the repository]\n" + profile
    fres = run_flow(pre, executor, ctx={"task": task, "session_id": started.session_id,
                                        "notify_channel": notify, "project_profile": profile,
                                        "flow": flow, "repo": repo}, base="HEAD")  # Resolve flow prompts and notification placeholders [§11.1].
    plan = fres.ctx.get("plan")
    if plan is not None:
        _save_plan(conn, started.session_id, plan)
    if gate is None:
        rec = _record_end(conn, runner, repo, started.session_id, fres, executor)
        _notify_need_human(rec, started.session_id, notify, repo=repo, flow=flow)
        return {"mode": "autonomous", "session": started, "result": fres, "record": rec, "plan": plan}
    if gate.agent == CONVERSE_AGENT:               # Pause the session until a reply arrives [§6.4].
        dao.update_session(conn, started.session_id, status="need_human")
        return {"mode": "waiting_reply", "session": started, "result": fres, "plan": plan, "profile": profile}
    return {"mode": "checkpoint", "session": started, "result": fres, "plan": plan, "profile": profile}


def render_new(res: dict, repo: str = "", *, worker: str = "claude", judge: Optional[str] = None,
               worker_model: str = "", judge_model: str = "") -> str:
    s = res["session"]
    if res["mode"] == "autonomous":
        rec = res["record"]
        L = [f"rig · session #{s.session_id} {s.name} · AUTONOMOUS · {rec['status']}\n  {rec['handoff']}"]
        ctx = getattr(res.get("result"), "ctx", {}) or {}
        for it in (ctx.get("fix_items") or [])[:20]:
            L.append(f"  review: {str(it)[:200]}")
        if not (ctx.get("green_branches") or ctx.get("plan")) and (ctx.get("context") or "").strip():
            L.append("---\n" + ctx["context"].strip())
        return "\n".join(L)
    if res["mode"] == "waiting_reply":             # Continue the converse phase through a reply [§6.4].
        plan = res.get("plan")
        L = [f"rig · session #{s.session_id} {s.name} · PLAN READY - WAITING FOR YOUR RESPONSE"]
        if plan is not None:
            L.append(f"  goal: {plan.goal}")
            for i, u in enumerate(plan.units, 1):
                L.append(f"  {i}. [{u.id}] {u.instruction[:80]}")
            L.append(f"  gate: {' '.join(plan.gate_cmd) or '(no)'}")
        L.append("  → reply with 'ok' to proceed, or send plan feedback:")
        L.append(f"     python3 -m rig --path {shlex.quote(repo)} --reply {s.session_id} \"<ok | your changes>\"")
        return "\n".join(L)
    plan = res.get("plan")
    L = [f"rig · session #{s.session_id} {s.name} · PLAN READY - YOUR APPROVAL IS REQUIRED"]
    if plan is not None:
        L.append(f"  goal: {plan.goal}")
        for i, u in enumerate(plan.units, 1):
            L.append(f"  {i}. [{u.id}] {u.instruction[:80]}")
        L.append(f"  gate: {' '.join(plan.gate_cmd) or '(no)'}")
    else:
        L.append("  (no valid plan was produced; inspect the scout and planner output)")
    for it in ((getattr(res.get("result"), "ctx", {}) or {}).get("critique") or [])[:5]:
        L.append(f"  critic: {str(it)[:110]}")
    profile = res.get("profile") or ""
    if profile:
        L.append(f"  profile: {' '.join(profile.split())[:300]}")
    L.append("  ! before --approve, verify that services and test data required by the gate are available; "
             "otherwise units may fail because of the environment rather than their changes")
    j = judge or worker
    cmd = (f"python3 -m rig --path {shlex.quote(repo)} --approve {s.session_id}"
           f" --worker {worker} --worker-model {worker_model}"
           f" --judge {j} --judge-model {judge_model}")
    L.append("  → approve the plan, or revise and rerun it:")
    L.append(f"     {cmd}")
    return "\n".join(L)


def cmd_approve(conn, runner, repo: str, executor, session_id: int, *, notify: Optional[str] = None) -> dict:
    """Continue an approved plan through execution, reconciliation, and notification."""
    s = dao.get_session(conn, session_id)
    if s is None:
        return {"ok": False, "detail": f"session #{session_id} not found"}
    plan = _load_plan(conn, session_id)
    if plan is None:
        return {"ok": False, "detail": f"session #{session_id} has no stored plan (start with `rig \"task\"`)"}
    _, post = split_at_checkpoint(resolve_phases(s.get("flow") or "base"))
    fres = run_flow(post, executor, ctx={"task": s.get("task", ""), "plan": plan,
                                         "session_id": session_id, "notify_channel": notify,
                                         "flow": s.get("flow") or "base", "repo": repo}, base="HEAD")
    rec = _record_end(conn, runner, repo, session_id, fres, executor)
    _notify_need_human(rec, session_id, notify, repo=repo, flow=s.get("flow") or "base")
    return {"ok": fres.ok, "result": fres, "record": rec, "plan": plan}


_APPROVE_RE = re.compile(
    r"^\s*(ok|okay|go|yes|y|approve|approved|confirm|confirmed|lgtm|let'?s go|proceed|\+)"
    r"[\s!.]*$", re.I)


def is_approval(text: str) -> bool:
    return bool(_APPROVE_RE.match(text or ""))


def _continue_action(flow: str) -> str:
    """Resolve how a paused flow should consume a human response."""
    if (flow or "") == "lead":
        return "reply"
    try:
        _, gate, _ = split_at_gate(resolve_phases(flow or "base"))
    except (FileNotFoundError, ValueError):
        return "approve"
    return "reply" if (gate is not None and gate.agent == CONVERSE_AGENT) else "approve"


def _notify_need_human(rec: dict, session_id, notify: Optional[str], *,
                       repo: str = "", flow: str = "") -> None:
    """Notify the human when a flow pauses before its notification phase."""
    if not notify or (rec or {}).get("status") != "need_human":
        return
    try:
        from ..flow.notify import build_model, cut, render_text
        from ..tools import send
        body = cut((rec.get("handoff") or ""), 600).replace(" · ", "\n")
        msg = build_model(session_id=session_id, repo=repo, flow=flow, kind="action",
                          title="executor awaits a decision (need_human)", body=body,
                          action=_continue_action(flow))
        r = send(notify, render_text(msg), payload=msg)
        if r.sent:
            print(f"  notification [{notify}]: sent (need_human)")
    except Exception:
        pass


def cmd_reply(conn, runner, repo: str, executor, session_id: int, text: str, *,
              notify: Optional[str] = None) -> dict:
    """Apply a human response to a paused flow and continue execution."""
    s = dao.get_session(conn, session_id)
    if s is None:
        return {"ok": False, "detail": f"session #{session_id} not found"}
    flow = s.get("flow") or "base"
    phases = resolve_phases(flow)
    pre, gate, post = split_at_gate(phases)
    if gate is None:
        return {"ok": False, "detail": f"flow {flow!r} has no human gate; there is nothing to reply to "
                                       f"(autonomous flows report through notifications)"}
    plan = _load_plan(conn, session_id)
    if is_approval(text):
        if plan is None:
            return {"ok": False, "detail": "no stored plan to approve; run planning first"}
        fres = run_flow(post, executor, ctx={"task": s.get("task", ""), "plan": plan,
                                             "session_id": session_id, "notify_channel": notify,
                                             "flow": flow, "repo": repo}, base="HEAD")
        rec = _record_end(conn, runner, repo, session_id, fres, executor)
        _notify_need_human(rec, session_id, notify, repo=repo, flow=flow)
        return {"ok": fres.ok, "mode": "approved", "result": fres, "record": rec, "plan": plan}
    seg = pre
    about = (getattr(gate, "about", "") or "").strip()
    if about:
        idx = next((i for i, p in enumerate(pre) if p.name == about), None)
        if idx is not None:
            seg = pre[idx:]
    fres = run_flow(seg, executor, ctx={"task": s.get("task", ""), "plan": plan,
                                        "human_feedback": text, "session_id": session_id,
                                        "notify_channel": notify, "flow": flow, "repo": repo}, base="HEAD")
    new_plan = fres.ctx.get("plan")
    if new_plan is not None and new_plan is not plan:
        _save_plan(conn, session_id, new_plan)
    dao.update_session(conn, session_id, status="need_human")   # Wait again if the revision needs an answer.
    return {"ok": fres.ok, "mode": "rework", "result": fres, "plan": new_plan or plan}


def render_reply(res: dict, repo: str = "", session_id: Optional[int] = None) -> str:
    if not res.get("ok") and "result" not in res:
        return f"rig reply · {res.get('detail')}"
    if res.get("mode") == "approved":
        rec = res["record"]
        return f"rig reply · session #{session_id} · APPROVED · {rec['status']}\n  {rec['handoff']}"
    if res.get("mode") == "rework" and not res.get("ok"):
        fres = res.get("result")
        detail = (fres.results[-1]["detail"] if (fres is not None and fres.results) else "no detail")
        return (f"rig reply · session #{session_id} · PLAN REVISION FAILED: {detail}\n"
                f"  the previous plan remains intact; reply again with: "
                f"python3 -m rig --path {shlex.quote(repo)} --reply {session_id} \"<your changes>\"")
    plan = res.get("plan")
    L = [f"rig reply · session #{session_id} · PLAN REVISED - WAITING FOR YOUR RESPONSE"]
    if plan is not None:
        L.append(f"  goal: {plan.goal}")
        for i, u in enumerate(plan.units, 1):
            L.append(f"  {i}. [{u.id}] {u.instruction[:80]}")
        L.append(f"  gate: {' '.join(plan.gate_cmd) or '(no)'}")
    L.append(f"  → python3 -m rig --path {shlex.quote(repo)} --reply {session_id} \"<ok | your changes>\"")
    return "\n".join(L)


def cmd_resume(conn, runner, repo: str, session_id: int) -> dict:
    s = dao.get_session(conn, session_id)
    if s is None:
        return {"ok": False, "detail": f"session #{session_id} not found"}
    b = run_boot(runner, conn, repo)
    return {"ok": True, "session": s, "mode": b.mode, "boot": b}


def render_resume(res: dict) -> str:
    if not res.get("ok"):
        return f"rig resume · {res.get('detail')}"
    s, b = res["session"], res["boot"]
    return (f"rig resume · session #{s['session_id']} {s['name']} · mode {res['mode']}\n" + b.guide)


def print_notify_outcomes(fres) -> None:
    """Print delivery outcomes produced by notification phases."""
    for r in getattr(fres, "results", []) or []:
        if r.get("detail", "").startswith("notification"):
            print("  " + r["detail"])


def _env(name: str, fallback):
    return os.environ.get(name, fallback)


def parse_budget_cap(value) -> Optional[float]:
    """Parse a budget cap, returning None for an explicit unlimited value."""
    from .agent_cli import DEFAULT_BUDGET_CAP
    if value is None or str(value).strip().lower() in ("", "none"):
        return None
    try:
        cap = float(value)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_CAP
    return cap if cap > 0 else None


NOTIFY_META_KEY = "notify_channel"


def resolve_notify(conn, cli_value: Optional[str]) -> Optional[str]:
    """Resolve the effective notification channel from flags, environment, or project state."""
    return cli_value or dao.get_meta(conn, NOTIFY_META_KEY, "") or None


def _build_parser() -> argparse.ArgumentParser:
    from .agent_cli import DEFAULT_BUDGET_CAP
    ap = argparse.ArgumentParser(prog="rig", description="run tasks, approve plans, and inspect sessions")
    ap.add_argument("task", nargs="?", default=None, help="task for a new session; omit to inspect status")
    ap.add_argument("--path", default=None, help="explicit project directory; the current directory is never assumed")
    ap.add_argument("--flow", default=_env("RIG_FLOW", None),
                    help="named flow under flows/ (feature/small, feature/large, bug/small, bug/large, "
                         "project/bootstrap, arch/design, review/only, or research/audit); omit this "
                         "option to let the lead orchestrator triage the task")
    ap.add_argument("--worker", default=_env("RIG_WORKER", "claude"), choices=["claude", "codex"], help="worker harness")
    ap.add_argument("--judge", default=_env("RIG_JUDGE", None), choices=["claude", "codex"], help="judge harness (default: worker)")
    ap.add_argument("--worker-model", default=_env("RIG_WORKER_MODEL", "claude-sonnet-4-6"))
    ap.add_argument("--judge-model", default=_env("RIG_JUDGE_MODEL", ""),
                    help="judge model (empty means the worker model)")
    ap.add_argument("--name", default=None, help="session name (default: task slug), also used for branch and tmux")
    ap.add_argument("--approve", type=int, default=None, metavar="ID", help="approve a plan and run units through reconciliation")
    ap.add_argument("--reply", type=int, default=None, metavar="ID",
                    help="reply in a session: 'ok' proceeds, any other text revises the plan or wakes the lead")
    ap.add_argument("--resume", type=int, default=None, metavar="ID", help="resume a session with boot context")
    ap.add_argument("--answer", type=int, default=None, metavar="MSG_ID",
                    help="answer an agent question: `rig --path . --answer <id> \"text\"`; delivered when the lead resumes")
    ap.add_argument("--inbox", action="store_true",
                    help="show open agent questions and answers awaiting delivery")
    ap.add_argument("--notify", default=_env("RIG_NOTIFY", None),
                    help="final notification channel (telegram or webhook); defaults to the project setting")
    ap.add_argument("--set-notify-hooks", default=None, metavar="EVENTS",
                    help="comma-separated engine event kinds to notify; empty or 'none' disables hooks")
    ap.add_argument("--set-notify", default=None, metavar="CHANNEL",
                    help="store the project notification channel; 'none' clears it")
    ap.add_argument("--budget-cap", default=_env("RIG_BUDGET_CAP", str(DEFAULT_BUDGET_CAP)),
                    help=f"session spawn budget in USD (default {DEFAULT_BUDGET_CAP}; 0 or none means unlimited)")
    ap.add_argument("--lead", action="store_true",
                    help="run the LLM lead orchestrator instead of a named deterministic flow")
    ap.add_argument("--lead-model", default=_env("RIG_LEAD_MODEL", "claude-sonnet-4-6"),
                    help="model for the lead orchestrator")
    ap.add_argument("--list", action="store_true", help="list project sessions")
    ap.add_argument("--init", action="store_true", help="check local startup readiness")
    ap.add_argument("--view", nargs="*", default=None, metavar="COMMAND",
                    help="read-only project/session/run viewer; no command opens the interactive interface")
    ap.add_argument("--lessons", action="store_true",
                    help="list project lessons, including pending lessons awaiting human review")
    ap.add_argument("--promote-lesson", type=int, default=None, metavar="ID",
                    help="promote a pending lesson so recall can use it")
    ap.add_argument("--retire-lesson", type=int, default=None, metavar="ID",
                    help="retire a lesson from recall without deleting its history")
    return ap


_PROFILE_TASK_FALLBACK = (
    "Study the project or material without changing it. Identify its purpose, structure, and verification method. "
    "On the final line, emit one JSON object with the key facts; choose fields appropriate to this project."
)


def _scan_fn(runner, repo: str, *, worker: str, judge: str, worker_model: str, judge_model: str):
    """Run the new-project scout and persist its structured baseline profile."""
    def run(conn, project):
        try:
            task = content.load("system/scan-project/prompts/profile.md", default=_PROFILE_TASK_FALLBACK)
            ex = build_executor(runner, repo, conn, None, worker=worker, judge=judge,
                                worker_model=worker_model, judge_model=judge_model)
            fres = run_flow(resolve_phases("system/scan-project"), ex,
                            ctx={"task": task, "session_id": None})
            prof = warmup_mod.persist_profile(conn, fres.ctx.get("context", ""))
            if prof:                                   # Record HEAD so later boots can detect a stale profile [gap 4].
                dao.set_meta(conn, warmup_mod.PROFILE_HEAD_META_KEY, git.head(runner, repo))
        except Exception:
            pass
    return run


def main(argv=None) -> int:
    from ..db import convention
    convention.load_env_into_process()             # Load ~/.rig/.env before parsing environment defaults.
    args = _build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.init:
        print(init_mod.render_readiness(init_mod.check_readiness()))
        return 0

    if args.view is not None:
        if (args.task or "").strip():
            print("rig: `--view` is read-only; the task argument was ignored (remove --view to run it)",
                  file=sys.stderr)
        from . import view as view_mod
        return view_mod.main(args.view, path=args.path)

    if args.path is None:                          # Require a path instead of silently using the current directory.
        print("rig: explicit `--path <project-directory>` is required; use `--path .` for the current directory",
              file=sys.stderr)
        return 2

    repo, runner = resolve_repo(args.path)
    if repo is None:
        return 2
    convention.register_project(repo)

    if args.list:
        print(render_list(cmd_list(dao.open_project(repo))))
        return 0

    if args.lessons:                               # List lessons pending human review [M5].
        print(render_lessons(dao.list_lessons(dao.open_project(repo))))
        return 0

    if args.promote_lesson is not None or args.retire_lesson is not None:  # Apply a human lesson decision [M5/D1].
        from ..memory import store
        conn = dao.open_project(repo)
        try:
            if args.promote_lesson is not None:
                l = store.promote_lesson(conn, args.promote_lesson)
                print(f"rig · lesson #{l['lesson_id']} → active (now available to recall): {l['text'][:100]}")
            else:
                l = store.retire_lesson(conn, args.retire_lesson)
                print(f"rig · lesson #{l['lesson_id']} → retired (removed from recall): {l['text'][:100]}")
        except ValueError as e:
            print(f"rig · {e}", file=sys.stderr)
            return 1
        return 0

    if args.set_notify is not None:
        conn = dao.open_project(repo)
        val = "" if args.set_notify.strip().lower() in ("", "none") else args.set_notify.strip()
        dao.set_meta(conn, NOTIFY_META_KEY, val)
        print(f"rig · project notification channel: {val or '(cleared)'} (meta {NOTIFY_META_KEY!r})")
        return 0

    if args.set_notify_hooks is not None:
        from ..db.schema import EVENT_KIND
        from ..flow.notify import NOTIFY_HOOKS_META_KEY
        conn = dao.open_project(repo)
        val = "" if args.set_notify_hooks.strip().lower() in ("", "none") else args.set_notify_hooks.strip()
        unknown = [k.strip() for k in val.split(",") if k.strip() and k.strip() not in EVENT_KIND]
        if unknown:
            print(f"rig · WARNING: unknown event kinds {unknown} (valid: {', '.join(EVENT_KIND)}); "
                  f"they will not run", file=sys.stderr)
        dao.set_meta(conn, NOTIFY_HOOKS_META_KEY, val)
        print(f"rig · notification hooks: {val or '(disabled)'} (meta {NOTIFY_HOOKS_META_KEY!r})")
        return 0

    if args.inbox:                                 # Show the project's human-facing message channel [plan §10].
        conn = dao.open_project(repo)
        qs = dao.open_questions(conn)
        pending = [m for m in dao.list_messages(conn, direction="in") if m["status"] != "read"]
        if not qs and not pending:
            print("rig inbox · empty (no open questions or pending answers)")
            return 0
        if qs:
            print(f"rig inbox · open agent questions ({len(qs)}):")
            for q in qs:
                print(f"  #{q['msg_id']} [{q['ts']}] {q['text']}")
            print(f"  → answer with: python3 -m rig --path {shlex.quote(repo)} --answer <id> \"text\"")
        if pending:
            print(f"rig inbox · awaiting lead delivery ({len(pending)}): " +
                  ", ".join(f"#{m['msg_id']}" for m in pending) + " (delivered when the lead starts or resumes)")
        return 0

    if args.answer is not None:
        text = (args.task or "").strip()
        if not text:
            print("rig answer · response text required: `rig --path . --answer <msg_id> \"your response\"`", file=sys.stderr)
            return 2
        conn = dao.open_project(repo)
        target = dao.get_message(conn, args.answer)
        if target is None or target.get("direction") != "out" or target.get("kind") != "question":
            print(f"rig answer · #{args.answer} is not an open question (list open questions with rig --inbox)", file=sys.stderr)
            return 1
        try:
            mid = dao.add_message(conn, direction="in", kind="answer", ref_id=args.answer,
                                  text=text, channel="cli")
        except ValueError as e:
            print(f"rig answer · {e} (open questions: rig --inbox)", file=sys.stderr)
            return 1
        print(f"rig answer · response #{mid} to question #{args.answer} recorded; delivered when "
              f"the lead starts or resumes (force with: rig --reply <sid> or a new task)")
        return 0

    budget_cap = parse_budget_cap(args.budget_cap)

    if args.flow is None and (args.task or "").strip() and args.approve is None \
            and args.reply is None and args.resume is None:
        args.lead = True

    if args.lead:                                  # Run the LLM lead orchestrator [A2].
        if not (args.task and args.task.strip()) and args.resume is None:
            print("rig lead · provide a task (`rig \"<task>\" --lead`) or `--resume <id>`", file=sys.stderr)
            return 2
        conn = dao.open_project(repo)
        from . import lead as lead_mod
        res = lead_mod.cmd_lead(conn, runner, repo, task=(args.task or ""), lead_harness=args.worker,
                                lead_model=args.lead_model, name=args.name, resume_sid=args.resume,
                                notify=resolve_notify(conn, args.notify))
        if not res.get("ok") and "record" not in res:
            print(f"rig lead · {res.get('detail')}", file=sys.stderr)
            return 1
        rec = res["record"]
        print(f"rig lead · session #{res['session_id']} · {rec['status']}\n{rec['handoff']}")
        print_notify_outcomes(res.get("flow_result"))
        return 0 if res["ok"] else 1

    if args.resume is not None:
        conn = dao.open_project(repo)
        res = cmd_resume(conn, runner, repo, args.resume)
        print(render_resume(res))
        return 0 if res.get("ok") else 1

    judge = args.judge or args.worker
    args.judge_model = args.judge_model or args.worker_model

    if args.reply is not None:
        text = (args.task or "").strip()
        if not text:
            print("rig reply · response text required: `rig --path . --reply <id> \"ok | your changes\"`", file=sys.stderr)
            return 2
        conn = dao.open_project(repo)
        s = dao.get_session(conn, args.reply)
        if s is None:
            print(f"rig reply · session #{args.reply} not found", file=sys.stderr)
            return 1
        if (s.get("flow") or "") == "lead":
            from . import lead as lead_mod
            res = lead_mod.cmd_lead(conn, runner, repo, task="", lead_harness=args.worker,
                                    lead_model=args.lead_model, resume_sid=args.reply,
                                    notify=resolve_notify(conn, args.notify), human_reply=text)
            if not res.get("ok") and "record" not in res:
                print(f"rig reply · {res.get('detail')}", file=sys.stderr)
                return 1
            rec = res["record"]
            print(f"rig reply · lead #{res['session_id']} resumed · {rec['status']}\n{rec['handoff']}")
            print_notify_outcomes(res.get("flow_result"))
            return 0 if res["ok"] else 1
        ex = build_executor(runner, repo, conn, args.reply, worker=args.worker, judge=judge,
                            worker_model=args.worker_model, judge_model=args.judge_model,
                            budget_cap=budget_cap)
        res = cmd_reply(conn, runner, repo, ex, args.reply, text, notify=resolve_notify(conn, args.notify))
        print(render_reply(res, repo, args.reply))
        print_notify_outcomes(res.get("result"))
        return 0 if res.get("ok") else 1

    if args.approve is not None:
        conn = dao.open_project(repo)
        ex = build_executor(runner, repo, conn, args.approve, worker=args.worker, judge=judge,
                            worker_model=args.worker_model, judge_model=args.judge_model,
                            budget_cap=budget_cap)
        res = cmd_approve(conn, runner, repo, ex, args.approve, notify=resolve_notify(conn, args.notify))
        if not res.get("ok") and "result" not in res:
            print(f"rig approve · {res.get('detail')}", file=sys.stderr)
            return 1
        rec = res["record"]
        print(f"rig approve · session #{args.approve} · {rec['status']}\n  {rec['handoff']}")
        print_notify_outcomes(res.get("result"))
        return 0 if res["ok"] else 1

    if not (args.task and args.task.strip()):      # With no task or command, show boot status.
        conn = dao.open_project(repo)
        b = run_boot(runner, conn, repo)
        print(f"rig · {os.path.basename(repo)} · mode {b.mode}\n{b.guide}")
        print("  (`rig \"<task>\"` starts a session; `rig --list` lists sessions)")
        return 0

    wr = warmup_mod.warmup(repo, scan_fn=_scan_fn(runner, repo, worker=args.worker, judge=judge,
                                                  worker_model=args.worker_model, judge_model=args.judge_model))
    conn = wr.conn
    ex = build_executor(runner, repo, conn, None, worker=args.worker, judge=judge,
                        worker_model=args.worker_model, judge_model=args.judge_model,
                        budget_cap=budget_cap)
    res = cmd_new(conn, runner, repo, ex, task=args.task, flow=args.flow, agent=args.worker,
                  name=args.name, notify=resolve_notify(conn, args.notify))
    print(render_new(res, repo, worker=args.worker, judge=judge,
                     worker_model=args.worker_model, judge_model=args.judge_model))
    print_notify_outcomes(res.get("result"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
