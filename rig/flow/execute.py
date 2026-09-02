"""Execute plans, units, reviews, gates, and integration steps."""
from __future__ import annotations

import json
import os
import re
from typing import Callable, Optional

from ..adapters.base import ENGINE_DIR
from ..checks import gate as gate_check
from ..checks import reconcile_plan as reconcile_check
from ..checks import review as review_check
from ..checks.validate_plan import validate_plan
from ..core.result import Result
from ..core.types import Unit, is_green
from ..util import content
from . import compose
from .phase import Phase
from .runner import PhaseResult, run_flow


def gc_unit_branches(runner, repo: str, *, green_branches, session_branch) -> list:
    """Remove temporary unit branches and worktrees."""
    removed = []
    if not session_branch:
        return removed
    from ..util import git as _git
    for b in green_branches or []:
        if b == session_branch:
            continue
        if _git.is_ancestor(runner, repo, b, session_branch) and _git.branch_delete(runner, repo, b):
            removed.append(b)
    return removed


def _goal_feature(goal: str) -> str:
    """Derive a stable lesson feature from a plan goal."""
    s = re.sub(r"[^0-9a-z\u0400-\u04ff]+", "-", (goal or "").lower()).strip("-")
    return s[:40].strip("-")


class PhaseExecutor:
    """Execute each supported phase against shared project state."""

    def __init__(self, *, spawner=None, judge_adapter=None, judge_model: Optional[str] = None,
                 planner_adapter=None, planner_model: Optional[str] = None,
                 runner=None, repo: str = "", conn=None, cwd: Optional[str] = None,
                 dimensions=None, judge_schema: Optional[str] = None,
                 plan_schema: Optional[str] = None, unit_retry_cap: int = 1,
                 invoke_timeout: Optional[float] = 900.0,
                 unit_est_cost: float = 0.5,
                 handlers: Optional[dict] = None):
        self.spawner = spawner
        self.judge_adapter = judge_adapter
        self.judge_model = judge_model
        self.planner_adapter = planner_adapter
        self.planner_model = planner_model
        self.runner = runner
        self.repo = repo
        self.conn = conn
        self.cwd = cwd or repo
        self.dimensions = tuple(dimensions) if dimensions is not None else review_check.DEFAULT_DIMENSIONS
        self.judge_schema = judge_schema
        self.plan_schema = plan_schema
        self.unit_retry_cap = max(0, int(unit_retry_cap))  # Retry cap within a unit phase.
        self.invoke_timeout = invoke_timeout
        self.unit_est_cost = max(0.0, float(unit_est_cost))
        self._session_branch = None      # Branch from the last unit run, including partial outcomes.
        self._units_green_branches = []  # Last green unit branches used during partial reconciliation.
        self._handlers = {
            "scout": self._run_scout,
            "repro": self._run_repro,
            "plan": self._run_plan,
            "critic": self._run_critic,
            "checkpoint": self._run_checkpoint,
            "converse": self._run_converse,
            "unit": self._run_unit,
            "units": self._run_units,
            "review": self._run_review,
            "gate": self._run_gate,
            "reconcile": self._run_reconcile,
            "notify": self._run_notify,
        }
        if handlers:
            self._handlers.update(handlers)

    def __call__(self, phase, ctx: dict) -> PhaseResult:
        sid = ctx.get("session_id")
        if sid is not None and self.spawner is not None \
                and getattr(self.spawner, "session_id", None) is None:
            self.spawner.session_id = sid
        handler = self._handlers.get(phase.agent)
        if handler is None:
            raise ValueError(f"PhaseExecutor: no handler for role agent={phase.agent!r} "
                             f"(exists {sorted(self._handlers)})")
        import time as _time

        from ..core.events import emit
        from .notify import maybe_notify
        t0 = _time.monotonic()
        try:
            res = handler(phase, ctx)
        except Exception as e:
            emit(self.conn, kind="flow_phase", name=phase.name, session_id=sid,
                 params={"agent": phase.agent, "attempt": ctx.get("attempt")},
                 status=f"crash:{type(e).__name__}",
                 duration_ms=int((_time.monotonic() - t0) * 1000))
            raise
        emit(self.conn, kind="flow_phase", name=phase.name, session_id=sid,
             params={"agent": phase.agent, "attempt": ctx.get("attempt")},
             status=("green" if res.green else "red"),
             duration_ms=int((_time.monotonic() - t0) * 1000))
        maybe_notify(self.conn, kind="flow_phase", session_id=sid,
                     text=f"rig #{sid}: phase {phase.name} — {'green' if res.green else 'RED'}")
        return res

    @staticmethod
    def _role_prompt(phase, ctx: dict, role_file: str, default: str, **vars) -> str:
        """Load a role prompt with a safe fallback."""
        p = ((getattr(phase, "prompt", "") or "") if phase is not None else "").strip()
        if p:
            if re.search(r"\s", p):
                return content.render_text(p, **vars)
            text = content.load(p, default="")
            if not text:
                raise ValueError(f"PhaseExecutor: phase.prompt={p!r} — file no in flows/ "
                                 f"(misprint way? inline-template must to keep blank)")
            return content.render_text(text, **vars)
        flow = (ctx.get("flow") or "").strip()
        if flow:
            text = content.load(f"{flow}/prompts/{role_file}", default="")
            if text:
                return content.render_text(text, **vars)
        return content.render(f"prompts/{role_file}", default=default, **vars)

    @staticmethod
    def _flow_file(ctx: dict, rel: str, default_name: str) -> str:
        """Resolve a content file relative to the active flow."""
        flow = (ctx.get("flow") or "").strip()
        if flow and content.load(f"{flow}/{rel}", default=""):
            return f"{flow}/{rel}"
        return default_name

    def _phase_launch(self, phase, adapter, default_model, session_id=None):
        """Resolve model and effort settings for a phase invocation."""
        model = ((getattr(phase, "model", "") or "") if phase is not None else "").strip()
        effort = ((getattr(phase, "effort", "") or "") if phase is not None else "").strip()
        if model and getattr(adapter, "name", "") == "codex" and model.lower().startswith("claude"):
            from ..core.events import emit
            emit(self.conn, kind="flow_phase", name=getattr(phase, "name", "?"), session_id=session_id,
                 status="model_dropped", params={"model": model, "adapter": "codex"})
            model = ""
        return (model or default_model), (effort or None)

    _PINS_CAP = 4000
    _ARTIFACT_CAP = 6000      # Lead context included in the worker brief.
    _BUILDER_CAP = 1500       # Builder decisions supplied to the judges.

    _MAX_DECISIONS = 20

    def _unit_continuum(self) -> tuple:
        """Load bounded artifact pins and durable decisions for a unit."""
        pins, decisions = "", []
        if self.conn is None:
            return pins, decisions
        try:
            from ..db import dao
            rows = sorted(dao.list_artifacts(self.conn, kind="pins"),
                          key=lambda a: a["artifact_id"], reverse=True)
            parts, dropped, used = [], [], 0
            for a in rows:
                art = dao.get_artifact(self.conn, a["name"])
                body = (art.get("text") or "").strip() if art else ""
                if not body:
                    continue
                if used + len(body) > self._PINS_CAP and parts:
                    dropped.append(a["name"])
                    continue
                parts.append(body[:self._PINS_CAP])
                used += len(body)
            if dropped:
                parts.append("(omitted to keep context bounded; read the current version with: artifact get "
                             + ", ".join(dropped) + ")")
            pins = "\n\n".join(parts)
            allc = [r["text"] for r in dao.list_decisions(self.conn)]
            decisions = allc[-self._MAX_DECISIONS:]                        # IDs increase; the tail is newest.
            if len(allc) > self._MAX_DECISIONS:
                decisions.append(f"(+{len(allc) - self._MAX_DECISIONS} decision older than — decision list)")
        except Exception:
            pins, decisions = "", []
        return pins, decisions

    @classmethod
    def _builder_report(cls, d) -> str:
        """Extract the builder-decision section from a worker report."""
        text = (getattr(d, "text", "") or "")
        idx = text.rfind("BUILDER DECISIONS")
        if idx < 0:
            return ""
        return text[idx:][:cls._BUILDER_CAP].strip()

    def _run_panel(self, unit: Unit, diff: Optional[str], *, session_id=None,
                   prior_green=None, cwd=None,
                   judge_prompt_name: str = "prompts/judge.md",
                   dims=None, votes: int = 1, targets: str = "") -> review_check.PanelResult:
        """Run the configured judge panel for a unit."""
        from ..core.launch import agent
        dims = tuple(dims) if dims else self.dimensions
        votes = max(1, int(votes or 1))
        if not dims:
            return review_check.aggregate([])
        if self.judge_schema and self.runner is not None:
            self.runner.write_file(self.judge_schema, review_check.VERDICT_SCHEMA)
        exp_dims = [dim for dim in dims for _ in range(votes)]  # Dimension-major order is deterministic [B6].
        prompts = [self._judge_prompt(dim, unit, diff, prior_green, name=judge_prompt_name,
                                      targets=targets)
                   for dim in exp_dims]

        def _panel(ps):
            return agent(self.judge_adapter, ps, wait="parallel", isolated=True, perms="read",
                         schema=self.judge_schema, cwd=(cwd or self.cwd), model=self.judge_model,
                         conn=self.conn, session_id=session_id, role="judge", phase="review",
                         timeout=self.invoke_timeout)

        results = list(_panel(prompts))
        infra = [i for i, r in enumerate(results) if r is None or not getattr(r, "ok", False)]
        if infra:
            retried = list(_panel([prompts[i] for i in infra]))
            for j, i in enumerate(infra):
                results[i] = retried[j]
        flat = [review_check.parse_verdict(r, dimension=d) for d, r in zip(exp_dims, results)]
        verdicts = [review_check.combine_votes(dim, flat[i * votes:(i + 1) * votes])
                    for i, dim in enumerate(dims)]
        return review_check.aggregate(verdicts)

    @staticmethod
    def _judge_prompt(dim: str, unit: Unit, diff: Optional[str], prior_green=None,
                      name: str = "prompts/judge.md", targets: str = "") -> str:
        base = ""
        pg = prior_green or []
        if pg:
            lines = "\n".join(f"  - [{p.get('id')}]: {(p.get('instruction') or '').strip().replace(chr(10), ' ')[:140]}"
                              for p in pg)
            base = ("IMPORTANT - diff INCREMENTAL on top of ALREADY IT IS ACCEPTED, IT IS USUAL units (their code ALREADY in base branch, OUTSIDE "
                    "this diff):\n" + lines + "\nReferences (import/invocation) on THIS code - NOT defect: it "
                    "noun in base. NOT flag its «is not defined».\n")
        tblock = ""
        if (targets or "").strip():
            quoted = "\n".join("> " + ln for ln in targets.strip().splitlines())
            tblock = ("CHECK TARGETS (data, NOT proof: seam pins are verbatim contracts, so a conflicting "
                      "diff is a defect; builder decisions are self-report and must be checked against the diff):\n"
                      + quoted + "\n")
        return content.render(
            name, DIM=dim, INSTRUCTION=unit.instruction, DIFF=(diff or "(empty)"), BASE=base,
            TARGETS=tblock,
            default=("You isolated reviewer dimension [{{DIM}}] (you see only task and diff). "
                     "TASK: {{INSTRUCTION}}\n<<<DIFF\n{{DIFF}}\n>>>DIFF\n{{BASE}}{{TARGETS}}"
                     'on the last line one JSON: {"ok": true|false, "why": "concrete"}. by default ok=false.'))

    def _worktree_diff(self, wt: str) -> str:
        """Return staged and unstaged changes from a worktree."""
        if self.runner is None:
            return ""
        from ..util import git
        git.stage_all(self.runner, self.repo, wt)
        return git.diff_cached(self.runner, self.repo, wt)

    def _run_unit(self, phase, ctx: dict) -> PhaseResult:
        """Build, review, gate, and record one plan unit."""
        unit = ctx.get("unit")
        plan = ctx.get("plan")
        if unit is None or plan is None:
            raise ValueError("PhaseExecutor.unit: ctx without 'unit'/'plan' (nothing to delegate)")
        attempt = int(ctx.get("attempt", 1))
        feedback = ctx.get("feedback") or ""
        index = int(ctx.get("index", 1))
        total = int(ctx.get("total", 1))
        prior_green = ctx.get("prior_green", [])
        fix = [feedback] if (attempt > 1 and feedback) else None
        memory = self._recall_for(unit)
        eff_gate, gate_trusted = self._trusted_gate(plan.gate_cmd)
        pins, decisions = self._unit_continuum()
        artifact_ctx = str(ctx.get("artifact_context") or "")[:self._ARTIFACT_CAP]
        brief = compose.assemble_brief(unit, plan, index=index, total=total, prior_green=prior_green,
                                       fix_feedback=fix, attempt=attempt, memory=memory, gate_cmd=eff_gate,
                                       pins=pins, decisions=decisions, artifact=artifact_ctx,
                                       method_name=self._flow_file(ctx, "prompts/worker.md", "prompts/worker.md"))

        base = ctx.get("base", "HEAD")
        sys_path = (self.spawner.adapter.inject_context(brief.system, self.repo)
                    if (brief.system or "").strip() else None)
        try:
            wm, weff = self._phase_launch(phase, self.spawner.adapter,
                                          getattr(self.spawner, "model", None),
                                          session_id=ctx.get("session_id"))  # [stage 4]
            d = self.spawner.spawn(brief.user, isolated_worktree=True, unit_id=unit.id, base=base,
                                   perms=phase.perms, system_context_path=sys_path, model=wm,
                                   effort=weff,
                                   est_cost=self.unit_est_cost,
                                   timeout=self.invoke_timeout)
        except Exception as e:
            self._accumulate(ctx, unit, False, None, None)
            return PhaseResult(green=False, actionable=False, output={"id": unit.id},
                               detail=f"spawn FAIL: {type(e).__name__}: {e}")
        d_ok = isinstance(d, Result) and d.ok
        wstatus = ((getattr(d, "status", None) or {}) if isinstance(d, Result) else {}).get("status")
        if wstatus == "need_human":
            why = str(((getattr(d, "status", None) or {}).get("why") or getattr(d, "text", "") or "")).strip()
            return PhaseResult(green=False, need_human=True, actionable=False, output={"id": unit.id},
                               detail=f"need_human from worker [{unit.id}]: {' '.join(why.split())[:200]}")
        if not d_ok:
            self._accumulate(ctx, unit, False, None, None)
            return PhaseResult(green=False, actionable=False, output={"id": unit.id},
                               detail=f"delegate FAIL: {getattr(d, 'error', '') or ''}")

        wt = self.spawner.worktree_path(unit.id)
        diff = self._worktree_diff(wt)
        builder = self._builder_report(d)
        targets = "\n\n".join(t for t in (
            ("PINS SEAMS:\n" + pins) if pins else "",
            builder,
        ) if t)
        panel = self._run_panel(unit, diff, session_id=ctx.get("session_id"), prior_green=prior_green,
                                cwd=wt,
                                judge_prompt_name=self._flow_file(ctx, "prompts/judge.md", "prompts/judge.md"),
                                dims=phase.dimensions, votes=phase.judge_votes,  # Phase policy from flow.json.
                                targets=targets)
        g = (self._gate_hint(gate_check.run_gate(self.runner, eff_gate, cwd=wt,
                                                 risk=("low" if gate_trusted else "high")), gate_trusted)
             if self.runner else None)
        if g is not None:
            from ..core.events import emit
            emit(self.conn, kind="gate", name=unit.id, session_id=ctx.get("session_id"),
                 params={"trusted": gate_trusted}, status=g.verdict)
        green = is_green(_okwrap(d_ok), _okwrap(panel.ok), g)
        commit_failed = False
        if green and self.runner is not None:
            commit_failed = not self._commit_unit(wt, unit)
            if commit_failed:
                green = False
        self._accumulate(ctx, unit, d_ok, panel, g)   # Gate failure creates a ground-truth lesson [B1/D1].
        actionable = bool((g is not None and g.verdict == "FAIL") or panel.fix_items)
        details = ([f"[gate] {g.detail}"] if (g and g.verdict == "FAIL") else []) + panel.fix_items
        if commit_failed:
            details.append("commit failed: staged work was not recorded on the unit branch")
        fix_detail = "; ".join(details)
        out = {"branch": f"rig/{unit.id}", "id": unit.id, "instruction": unit.instruction, "diff": diff}
        return PhaseResult(green=green, actionable=actionable, output=out,
                           detail=fix_detail or panel.detail)

    def _recall_for(self, unit: Unit) -> list:
        """Recall for."""
        if self.conn is None:
            return []
        try:
            from ..memory import recall
            rec = recall.load(self.conn, unit.instruction, k=5)
            return [l["text"] for l in (rec.always + rec.selected)]
        except Exception:
            return []

    def _accumulate(self, ctx: dict, unit: Unit, d_ok: bool, panel, g) -> None:
        """Persist lessons learned from a unit outcome."""
        if self.conn is None:
            return
        from ..memory import store
        sid = ctx.get("session_id") or getattr(self.spawner, "session_id", None)
        plan = ctx.get("plan")
        feat = _goal_feature(getattr(plan, "goal", "") if plan else "")  # Index the lesson by plan goal.
        try:
            if g is not None and g.verdict == "PASS":
                store.retire_active_by_prefix(self.conn, f"unit {unit.id}: gate FAIL", feature=feat)
            if not d_ok:
                store.write_lesson(self.conn, text=f"unit {unit.id}: delegate FAIL", kind=store.PRINCIPLE,
                                   scope=store.ORCHESTRATOR, session_id=sid, ground_truth=False, feature=feat)
            elif g is not None and g.verdict == "FAIL":
                store.write_lesson(self.conn, text=f"unit {unit.id}: gate FAIL — {(g.detail or '')[:120]}",
                                   kind=store.PRINCIPLE, scope=store.ORCHESTRATOR, session_id=sid,
                                   ground_truth=True, feature=feat)   # Store ground truth as a feature-indexed rule.
            elif g is not None and g.verdict in ("TIMEOUT", "INFRA", "NO-GATE"):
                store.write_lesson(self.conn, text=f"unit {unit.id}: gate did not run ({g.verdict})",
                                   kind=store.PRINCIPLE, scope=store.ORCHESTRATOR, session_id=sid,
                                   ground_truth=False, feature=feat)
            elif panel is not None and not panel.ok:
                store.write_lesson(self.conn, text=f"unit {unit.id}: judge deviation - {(panel.detail or '')[:120]}",
                                   kind=store.PRINCIPLE, scope=store.ORCHESTRATOR, session_id=sid,
                                   ground_truth=False, feature=feat)
        except Exception:
            pass

    def _commit_unit(self, wt: str, unit: Unit) -> bool:
        """Commit the staged result of a green unit."""
        from ..util import git
        return git.commit_staged(self.runner, self.repo, wt, f"rig/{unit.id}: {unit.instruction[:60]}")

    def _trusted_gate(self, plan_gate_cmd):
        """Return whether the gate command came from trusted configuration."""
        try:
            with open(os.path.join(self.repo, ENGINE_DIR, "gate.json"), encoding="utf-8") as f:
                cmd = json.load(f)
            if isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd):
                return cmd, True
        except (OSError, ValueError):
            pass
        return plan_gate_cmd, False

    @staticmethod
    def _gate_hint(g, trusted: bool):
        """Mark an untrusted gate result as advisory."""
        if g is not None and not trusted and "BLOCKED" in (g.detail or ""):
            g.detail += (" | gate_cmd author LLM-plan (trusted no): human to fasten "
                         "commander in <repo>/.rig/gate.json")
        return g

    def _run_gate(self, phase, ctx: dict) -> PhaseResult:
        plan_cmd = ctx.get("gate_cmd") or (ctx.get("plan").gate_cmd if ctx.get("plan") else [])
        gate_cmd, trusted = self._trusted_gate(plan_cmd)
        g = self._gate_hint(gate_check.run_gate(self.runner, gate_cmd, cwd=self.cwd,
                                                risk=("low" if trusted else "high")), trusted)
        return PhaseResult(green=g.ok, actionable=(g.verdict == "FAIL"), output={"verdict": g.verdict},
                           detail=g.detail)

    def _run_review(self, phase, ctx: dict) -> PhaseResult:
        """Review the current diff with the configured judge panel."""
        unit = ctx.get("unit") or Unit(id="review", instruction=(ctx.get("task") or "review change"))
        diff = ctx.get("diff")
        if diff is None and self.runner is not None:
            from ..util import git
            diff = git.diff_head(self.runner, self.repo, self.cwd)
        panel = self._run_panel(unit, diff, session_id=ctx.get("session_id"),
                                prior_green=ctx.get("prior_green"),
                                judge_prompt_name=self._flow_file(ctx, "prompts/judge.md", "prompts/judge.md"),
                                dims=phase.dimensions, votes=phase.judge_votes)  # Phase policy from flow.json.
        return PhaseResult(green=panel.ok, actionable=bool(panel.fix_items),
                           output={"fix_items": panel.fix_items}, detail=panel.detail)

    def _run_plan(self, phase, ctx: dict) -> PhaseResult:
        from ..core.launch import agent
        task = ctx.get("task", "")
        context = ctx.get("context", "")
        art = str(ctx.get("artifact_context") or "").strip()
        if art:
            context = (context + "\n\n" if context else "") + \
                "ARTIFACT FROM ORCHESTRATOR (dossier/checklist — system plan BY HARROW):\n" + art[:self._ARTIFACT_CAP]
        prompt = self._role_prompt(
            phase, ctx, "plan.md", TASK=task, CONTEXT=context,
            default=('Author PLAN by problem. TASK: {{TASK}}\nCONTEXT: {{CONTEXT}}\n'
                     'on the last line one JSON: {"goal":"...","units":[{"id":"x","instruction":"..."}],"gate_cmd":["<command-checks>"]}.'))
        fb = (ctx.get("human_feedback") or "").strip()
        prior = ctx.get("plan")
        if fb and prior is not None:
            from ..core.types import plan_to_dict
            prior_json = json.dumps(plan_to_dict(prior), ensure_ascii=False)
            prompt += (f"\n\nPAST PLAN (to process ITS by feedback, not not start with nought without to cause):\n"
                       f"{prior_json}\nFEEDBACK HUMAN:\n{fb}\n\n"
                       f"To process plan whole with accounting fidbeka. Decisions, some human in fidbeke "
                       f"already provided, not reopen and not to ask again — giant their in carry through. Response MUST "
                       f"to end FULL JSON newer plan (goal+units+gate_cmd) — even if correcting "
                       f"small, rewrite JSON anew whole, one prose without JSON plan not to take:\n"
                       f'{{"goal": "...", "units": [{{"id": "u1", "instruction": "..."}}], "gate_cmd": ["<command-checks>"]}}')
        from ..core.jsonscan import objects_with_key
        if self.plan_schema:
            from ..core.types import PLAN_SCHEMA
            try:
                self.runner.write_file(self.plan_schema, PLAN_SCHEMA)
            except OSError:
                pass
        reason = ""
        for attempt in (1, 2):
            p = prompt if attempt == 1 else (
                prompt + f"\n\nPAST RESPONSE NOT AS A FRIEND ({reason}) — work not use. Answer anew "
                         f"and to end on the last line exactly one JSON-systematic character:\n"
                         f'{{"goal": "...", "units": [{{"id": "u1", "instruction": "..."}}], "gate_cmd": ["<command-checks>"]}}')
            m, eff = self._phase_launch(phase, self.planner_adapter, self.planner_model,
                                        session_id=ctx.get("session_id"))  # [stage 4]
            res = agent(self.planner_adapter, p, wait="sync", perms="plan",
                        schema=(self.plan_schema if attempt > 1 else None),
                        cwd=self.cwd, model=m, effort=eff, conn=self.conn,
                        session_id=ctx.get("session_id"), role="planner", phase="plan")
            pstatus = (getattr(res, "status", None) or {}).get("status")
            if pstatus == "need_human":
                pwhy = str(((getattr(res, "status", None) or {}).get("why")
                            or getattr(res, "text", "") or "")).strip()
                return PhaseResult(green=False, need_human=True, actionable=False,
                                   detail=f"need_human from planner: {' '.join(pwhy.split())[:200]}")
            cands = objects_with_key(getattr(res, "text", "") or "", "units")
            last_fail = None
            for obj in reversed(cands):                # The current plan is normally the last candidate.
                pc = validate_plan(obj, raw=res.text or "")
                if pc.ok:
                    if not pc.plan.context:
                        pc.plan.context = (ctx.get("context", "") or
                                           (getattr(prior, "context", "") if prior is not None else ""))
                    return PhaseResult(green=True, output={"plan": pc.plan, "total": len(pc.plan.units)},
                                       detail=f"plan is valid ({len(pc.plan.units)} units)")
                last_fail = last_fail or pc            # Keep the most relevant validation failure.
            reason = (last_fail.detail if last_fail else "no JSON plan")
        return PhaseResult(green=False, actionable=False, detail=f"plan is invalid: {reason}")

    def _run_checkpoint(self, phase, ctx: dict) -> PhaseResult:
        """Pass an automatic checkpoint when no human gate is configured."""
        return PhaseResult(green=True, output={"checkpoint": "automatic (no interactive gate)"},
                           detail="checkpoint skipped: no human gate configured")

    def _run_converse(self, phase, ctx: dict) -> PhaseResult:
        """Record the human response required by a converse phase."""
        return PhaseResult(green=True, output={"converse": "auto (no entry-split)"},
                           detail="coordination skipped: no human gate configured")

    def _run_scout(self, phase, ctx: dict) -> PhaseResult:
        """Collect structured project context from a scout invocation."""
        from ..core.launch import agent
        prompt = self._role_prompt(
            phase, ctx, "scout.md", TASK=ctx.get("task", ""), CONTEXT=ctx.get("context", ""),
            default=("You SCOUT. To learn repository under task, does not modify. TASK: {{TASK}}\n"
                     "To know TO SQUEEZE summary for planner: relevant files, convention, than verify (gate), risk."))
        profile = (ctx.get("project_profile") or "").strip()
        if profile:
            prompt = f"LIME ABOUT DESIGN (baseline-profile to pass purlin):\n{profile}\n\n" + prompt
        m, eff = self._phase_launch(phase, self.planner_adapter, self.planner_model,
                                    session_id=ctx.get("session_id"))  # [stage 4]
        res = agent(self.planner_adapter, prompt, wait="sync", perms="read", cwd=self.cwd,
                    model=m, effort=eff, conn=self.conn, session_id=ctx.get("session_id"),
                    role="scout", phase="scout")
        prior = (ctx.get("context") or "").strip()
        text = getattr(res, "text", "") or ""
        return PhaseResult(green=bool(getattr(res, "ok", False)),
                           output={"context": (f"{prior}\n\n{text}" if prior else text)},
                           detail="scout summary complete")

    def _run_repro(self, phase, ctx: dict) -> PhaseResult:
        """Ask an agent to reproduce the reported behavior."""
        from ..core.jsonscan import last_object_with_key
        from ..core.launch import agent
        prompt = self._role_prompt(
            phase, ctx, "repro.md", TASK=ctx.get("task", ""), CONTEXT=ctx.get("context", ""),
            default=("You SCOUT REPRODUCTION (read-only). To learn repository and install, "
                     "noun whether declared problem and what its mechanism. TASK: {{TASK}}\n"
                     "NOT giant and not modify — show, AS problem to reproduce (command/test, "
                     "incident before changes). Not to confirm / is required human — reproduced=false.\n"
                     'on the last line one JSON: {"reproduced": true|false, "mechanism": "meaning", '
                     '"repro": "as to show", "why": "short"}. by default reproduced=false.'))
        obj, text = None, ""
        for attempt in (1, 2):
            p = prompt if attempt == 1 else (
                prompt + '\n\nPAST RESPONSE NOT AS A FRIEND (no JSON-verdict with key reproduced) — work '
                         'not use. Answer anew and to end on the last line exactly one JSON:\n'
                         '{"reproduced": true|false, "mechanism": "meaning", "repro": "as to show", "why": "short"}')
            m, eff = self._phase_launch(phase, self.planner_adapter, self.planner_model,
                                        session_id=ctx.get("session_id"))  # [stage 4]
            res = agent(self.planner_adapter, p, wait="sync", perms="read", cwd=self.cwd,
                        model=m, effort=eff, conn=self.conn, session_id=ctx.get("session_id"),
                        role="repro", phase="repro", timeout=self.invoke_timeout)
            text = getattr(res, "text", "") or ""
            if not getattr(res, "ok", False):
                return PhaseResult(green=False, actionable=False,
                                   detail=f"repro: invocation did not complete ({getattr(res, 'error', '') or ''})")
            obj = last_object_with_key(text, "reproduced")
            if isinstance(obj, dict):
                break
        if not isinstance(obj, dict):                  # Missing verdicts fail closed after the retry.
            return PhaseResult(green=False, need_human=True, actionable=False, output={"context": text},
                               detail="repro: no valid JSON verdict; human input required (fail-closed)")
        if not obj.get("reproduced"):
            return PhaseResult(green=False, need_human=True, actionable=False, output={"context": text},
                               detail=f"repro: NOT reproduction - {str(obj.get('why') or '')[:150]}")
        return PhaseResult(green=True, output={"context": text, "repro": obj},
                           detail=f"repro: mechanism established - {str(obj.get('mechanism') or '')[:120]}")

    def _run_critic(self, phase, ctx: dict) -> PhaseResult:
        """Critique the proposed plan before execution."""
        from ..core.jsonscan import last_object_with_key
        from ..core.launch import agent
        plan = ctx.get("plan")
        if plan is None:
            return PhaseResult(green=True, output={"critique": []}, detail="critic: skipped because no plan exists")
        units_txt = "\n".join(f"{i}. [{u.id}] {u.instruction}" for i, u in enumerate(plan.units, 1))
        gate_line = " ".join(plan.gate_cmd) if plan.gate_cmd else "(gate not back)"
        prompt = self._role_prompt(
            phase, ctx, "critic.md", GOAL=plan.goal, UNITS=units_txt, GATE=gate_line,
            default=("You isolated critic PLAN (you see goal, laying out and gate, not contents). GOAL: {{GOAL}}\n"
                     "UNITS:\n{{UNITS}}\nGATE: {{GATE}}\nHealth care whether decomposition (coverage/order/dimensions)? "
                     "Cover whether GATE entire task and all units (or part without checks)? "
                     'on the last line one JSON: {"sound": true|false, "issues": ["..."], "why": "short"}.'))
        m, eff = self._phase_launch(phase, self.judge_adapter, self.judge_model,
                                    session_id=ctx.get("session_id"))  # [stage 4]
        res = agent(self.judge_adapter, prompt, wait="sync", isolated=True, schema=None, perms="read",
                    cwd=self.cwd, model=m, effort=eff, conn=self.conn,
                    session_id=ctx.get("session_id"), role="critic", phase="critic")
        obj = last_object_with_key(getattr(res, "text", "") or "", "sound")
        issues = [str(x) for x in (obj.get("issues") or [])][:20] if isinstance(obj, dict) else []
        return PhaseResult(green=True, output={"critique": issues, "sound": bool(obj and obj.get("sound"))},
                           detail=("critic: " + ("sound" if not issues else f"{len(issues)} issues")))

    def _run_units(self, phase, ctx: dict) -> PhaseResult:
        """Execute every plan unit and collect green branches."""
        plan = ctx.get("plan")
        if plan is None or not plan.units:
            return PhaseResult(green=False, actionable=False, detail="units: plan has no executable units")
        start_base = ctx.get("base", "HEAD")
        from ..util import git
        done_green: set = set()
        sid = ctx.get("session_id")
        if sid is not None and self.conn is not None and self.runner is not None:
            from ..db import dao
            for b in (dao.get_session(self.conn, sid) or {}).get("in_flight", []) or []:
                if b.startswith("rig/") and git.branch_exists(self.runner, self.repo, b):
                    done_green.add(b.split("/", 1)[1])
        meta = {f"unit:{u.id}": (u, i + 1) for i, u in enumerate(plan.units)}
        unit_perms = phase.perms if phase.perms != "default" else "auto"
        unit_phases = [Phase(name=f"unit:{u.id}", agent="unit", advances_base=True, perms=unit_perms,
                             on_fail="retry", retry_cap=self.unit_retry_cap,
                             dimensions=(list(phase.dimensions) if phase.dimensions else None),
                             judge_votes=phase.judge_votes,
                             model=phase.model, effort=phase.effort) for u in plan.units]

        def exec_unit(ph, c):
            unit, index = meta[ph.name]
            if unit.id in done_green:
                return PhaseResult(green=True, actionable=False,
                                   output={"branch": f"rig/{unit.id}", "id": unit.id,
                                           "instruction": unit.instruction, "diff": ""},
                                   detail=f"reused green in-flight unit on idempotent resume [E4]: {unit.id}")
            return self._run_unit(ph, {**c, "unit": unit, "plan": plan,
                                       "index": index, "total": len(plan.units)})

        sub = run_flow(unit_phases, exec_unit, ctx=ctx, base=start_base)
        if self.runner is not None and self.spawner is not None:
            for u in plan.units:
                git.worktree_remove(self.runner, self.repo, self.spawner.worktree_path(u.id))
        green_branches = [f"rig/{r['name'].split(':', 1)[1]}" for r in sub.results if r["green"]]
        self._units_green_branches = green_branches
        integration = sub.base if sub.base != start_base else None
        session_branch = None
        if integration and self.runner is not None and self.conn is not None:
            from ..db import dao as _dao
            sid = ctx.get("session_id")
            nm = (_dao.get_session(self.conn, sid) or {}).get("name") if sid is not None else None
            if nm and git.branch_set(self.runner, self.repo, f"rig/{nm}", integration):
                session_branch = f"rig/{nm}"
        self._session_branch = session_branch
        n_green = sum(1 for r in sub.results if r["green"])
        detail = f"units: {n_green}/{len(sub.results)} green; integration={integration or '-'}"
        if sub.need_human and sub.results:
            detail += f" · {sub.results[-1].get('detail') or ''}"
        elif not sub.ok:
            first_red = next((r for r in sub.results if not r["green"]), None)
            if first_red and (first_red.get("detail") or "").strip():
                detail += f" · {first_red['name']}: {(first_red['detail'] or '').strip()[:160]}"
        return PhaseResult(
            green=sub.ok, actionable=False, need_human=sub.need_human,
            output={"integration_branch": integration, "green_branches": green_branches,
                    "branch": sub.base, "unit_results": sub.results,
                    "session_branch": session_branch},
            detail=detail)

    def _run_notify(self, phase, ctx: dict) -> PhaseResult:
        """Deliver the phase notification."""
        from .notify import notify_phase
        if not ctx.get("green_branches") and self._units_green_branches:
            ctx = {**ctx, "green_branches": list(self._units_green_branches)}
        if not ctx.get("session_branch") and self._session_branch:
            ctx = {**ctx, "session_branch": self._session_branch}
        return notify_phase(phase, ctx)

    def _run_reconcile(self, phase, ctx: dict) -> PhaseResult:
        """Reconcile completed unit branches with the current plan."""
        from ..core.types import plan_to_dict
        from ..util import git
        plan = ctx.get("plan")
        plan_obj = plan_to_dict(plan) if plan is not None else None
        green = ctx.get("green_branches") or self._units_green_branches
        on_disk = git.branches(self.runner, self.repo, reconcile_check.BRANCH_PREFIX) if self.runner else []
        sb = ctx.get("session_branch") or self._session_branch
        if sb:
            on_disk = [b for b in on_disk if b != sb]
        rep = reconcile_check.reconcile_report(plan_obj, green=green, on_disk_branches=on_disk)
        return PhaseResult(green=rep.units_ok, actionable=False,
                           output={"report": rep}, detail=reconcile_check.render_reconcile(rep))


class _okwrap:
    """Adapt a boolean result to the gate's ``ok`` protocol."""
    __slots__ = ("ok",)

    def __init__(self, ok: bool):
        self.ok = bool(ok)
