"""Agent-facing CLI used by the lead orchestrator through shell commands [A2]."""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Tuple

DEFAULT_MAX_SPAWNS = 25
DEFAULT_BUDGET_CAP: Optional[float] = 10.0

REFUSED = 3
FLOW_COMPLETE_MARKER = "[flow complete]"


def _ledger(conn, session_id: int) -> Tuple[int, float]:
    """Return committed spawn count and recorded cost for a session."""
    from ..db import dao
    spawns = dao.count_runs(conn, session_id, kind="invoke", agents=("worker", "sub"))
    spawns += dao.count_active_reservations(conn, session_id)
    return spawns, dao.sum_run_cost(conn, session_id)


def _runs_spawn_count(conn, session_id: int) -> int:
    """Count committed worker and sub-agent invocations for spawn limits."""
    from ..db import dao
    return dao.count_runs(conn, session_id, kind="invoke", agents=("worker", "sub"))


def _caps(conn, session_id: int) -> Tuple[int, Optional[float]]:
    """Resolve session spawn and budget caps from configuration."""
    from ..db import dao
    cfg = (dao.get_session(conn, session_id) or {}).get("config") or {}
    max_spawns = int(cfg.get("max_spawns", DEFAULT_MAX_SPAWNS))
    budget_cap = cfg.get("budget_cap", DEFAULT_BUDGET_CAP)
    budget_cap = None if budget_cap is None else float(budget_cap)
    return max_spawns, budget_cap


def _guard(conn, session_id: int) -> Optional[str]:
    """Return a guardrail refusal reason, or None when a spawn is allowed [F7]."""
    max_spawns, budget_cap = _caps(conn, session_id)
    count, spent = _ledger(conn, session_id)
    if count >= max_spawns:
        return f"spawn-cap exhausted: {count}/{max_spawns} (session.config.max_spawns) [F7]"
    if budget_cap is not None and spent >= budget_cap:
        return f"budget-cap exhausted: ${spent:.4f}/${budget_cap:.2f} (session.config.budget_cap) [F7]"
    return None


def _adapter(harness: str, runner):
    """Build the requested worker adapter through the shared factory."""
    from .cli import make_adapter
    return make_adapter(harness, runner)


def _flow_pid_path(repo: str, session_id: int) -> str:
    """Return the per-session background-flow PID marker path."""
    from ..adapters.base import ENGINE_DIR
    return os.path.join(repo, ENGINE_DIR, f"run-flow-s{session_id}.pid")


def _live_flow_pid(path: str) -> Optional[int]:
    """Return the live background-flow PID, or None."""
    try:
        with open(path, encoding="utf-8") as f:
            pid = int((f.read() or "0").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        try:
            os.remove(path)              # Remove a stale marker left by a dead process.
        except OSError:
            pass
        return None
    except PermissionError:
        return pid


def cmd_spawn(args) -> int:
    """Delegate one disposable sub-agent under session guardrails."""
    from ..core.launch import agent
    from ..db import dao
    from ..transport.local import LocalRunner

    repo = os.path.abspath(args.repo)
    conn = dao.open_project(repo)
    reason = _guard(conn, args.session)
    if reason is not None:
        print(f"[rig-tool spawn] FAILURE GUARDRAIL: {reason}", file=sys.stderr)
        print("REFUSED: delegation fan-out limit reached. Complete or cancel existing work first.")
        return REFUSED

    if args.prompt is not None:
        prompt = args.prompt
    elif args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            prompt = f.read()
    else:
        print("[rig-tool spawn] provide --prompt \"...\" or --prompt-file <file>", file=sys.stderr)
        return 2

    max_spawns, _ = _caps(conn, args.session)
    res_ids = dao.try_reserve_spawns(conn, args.session, slots=1,
                                     count_committed=lambda c: _runs_spawn_count(c, args.session),
                                     max_spawns=max_spawns)
    if not res_ids:
        print(f"[rig-tool spawn] FAILURE GUARDRAIL: spawn-cap exhausted (max {max_spawns}) [F7]",
              file=sys.stderr)
        print("REFUSED: delegation fan-out limit reached. Complete or cancel existing work first.")
        return REFUSED
    try:
        adapter = _adapter(args.harness, LocalRunner())
        res = agent(adapter, prompt, wait="sync", perms=args.perms, cwd=repo, model=args.model,
                    conn=conn, session_id=args.session, role="worker", phase="lead-spawn",
                    timeout=args.timeout)
    finally:
        dao.release_spawns(conn, res_ids)
    sys.stdout.write(res.text or "")
    count, spent = _ledger(conn, args.session)
    print(f"\n[rig-tool spawn] ok={res.ok} cost={res.cost_usd} spawns={count} spent=${spent:.4f}",
          file=sys.stderr)
    return 0 if res.ok else 1


def cmd_fanout(args) -> int:
    """Run a read-only parallel batch under shared spawn guardrails."""
    from ..core.launch import agent
    from ..db import dao
    from ..transport.local import LocalRunner

    repo = os.path.abspath(args.repo)
    conn = dao.open_project(repo)
    try:
        prompts = json.loads(args.prompts_json)
        ok_shape = isinstance(prompts, list) and all(isinstance(p, str) for p in prompts)
    except (json.JSONDecodeError, TypeError):
        ok_shape = False
    if not ok_shape:
        print('[rig-tool fanout] --prompts-json must be a JSON array of strings, for example \'["analysis A","analysis B"]\'',
              file=sys.stderr)
        return 2
    if not prompts:
        print("[rig-tool fanout] empty batch", file=sys.stderr)
        return 2
    max_spawns, budget_cap = _caps(conn, args.session)
    count, spent = _ledger(conn, args.session)
    if budget_cap is not None and spent >= budget_cap:
        print(f"[rig-tool fanout] FAILURE GUARDRAIL: budget-cap ${spent:.4f}/${budget_cap:.2f} [F7]",
              file=sys.stderr)
        print("REFUSED: budget exhausted.")
        return REFUSED
    requested = len(prompts)
    res_ids = dao.try_reserve_spawns(conn, args.session, slots=requested,
                                     count_committed=lambda c: _runs_spawn_count(c, args.session),
                                     max_spawns=max_spawns, partial=True)
    if not res_ids:
        print(f"[rig-tool fanout] FAILURE GUARDRAIL: spawn-cap {count}/{max_spawns} [F7]", file=sys.stderr)
        print("REFUSED: session spawn capacity is exhausted.")
        return REFUSED
    if len(res_ids) < requested:
        print(f"[rig-tool fanout] truncated {requested}→{len(res_ids)} by remaining capacity [F7]", file=sys.stderr)
        prompts = prompts[:len(res_ids)]
    try:
        adapter = _adapter(args.harness, LocalRunner())
        results = agent(adapter, prompts, wait="parallel", perms="read", cwd=repo, model=args.model,
                        conn=conn, session_id=args.session, role="worker", phase="lead-fanout",
                        timeout=args.timeout)
    finally:
        dao.release_spawns(conn, res_ids)  # Invocation rows now provide the authoritative count.
    for i, r in enumerate(results):
        print(f"=== sub[{i}] ok={getattr(r, 'ok', False)} ===")
        print((getattr(r, "text", "") or "").strip())
    n_ok = sum(1 for r in results if getattr(r, "ok", False))
    print(f"\n[rig-tool fanout] {n_ok}/{len(results)} ok", file=sys.stderr)
    return 0 if n_ok == len(results) else 1


def cmd_run_flow(args) -> int:
    """Launch a validated deterministic flow in a detached process."""
    from ..db import dao
    from ..flow.phase import load_phases
    from ..flow.resolve import resolve_phases

    repo = os.path.abspath(args.repo)
    conn = dao.open_project(repo)
    _, budget_cap = _caps(conn, args.session)
    _, spent = _ledger(conn, args.session)
    if budget_cap is not None and spent >= budget_cap:
        print(f"[rig-tool run-flow] FAILURE GUARDRAIL: budget-cap ${spent:.4f}/${budget_cap:.2f} [F7]",
              file=sys.stderr)
        print("REFUSED: session budget exhausted; flow was not started.")
        return REFUSED
    try:
        if getattr(args, "flow_json", None):
            phases = load_phases(json.loads(args.flow_json))
            flow_label = "custom(--flow-json)"
        else:
            phases = resolve_phases(args.flow)
            flow_label = args.flow
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"[rig-tool run-flow] could not resolve flow: {e}", file=sys.stderr)
        return 1
    artifact_text = ""
    if getattr(args, "artifact", None):
        art = dao.get_artifact(conn, args.artifact)
        if art is None:
            print(f"[rig-tool run-flow] artifact {args.artifact!r} not found (use artifact list)",
                  file=sys.stderr)
            if getattr(args, "bg_worker", False):
                dao.log_run(conn, session_id=args.session, kind="note",
                            result=f"[flow fail] artifact {args.artifact!r} disappeared before startup")
                try:
                    os.remove(_flow_pid_path(repo, args.session))
                except OSError:
                    pass
            return 1
        artifact_text = art.get("text") or ""

    if not getattr(args, "bg_worker", False):
        import subprocess

        from ..adapters.base import ENGINE_DIR
        pid_path = _flow_pid_path(repo, args.session)
        live = _live_flow_pid(pid_path)
        if live is not None:
            print(f"[rig-tool run-flow] FAILURE: session {args.session} already has a running flow (pid {live}) [F1/F7]",
                  file=sys.stderr)
            print("REFUSED: a flow already runs in the background for this session. "
                  "Wait using rig-tool journal or terminate that PID; a second writer would race on shared worktrees.")
            conn.close()
            return REFUSED
        child = [sys.executable, "-m", "rig.entry.agent_cli", "run-flow", "--_bg-worker",
                 "--repo", repo, "--session", str(args.session),
                 "--harness", args.harness, "--model", args.model,
                 "--judge", (args.judge or args.harness), "--judge-model", (args.judge_model or args.model),
                 "--task", args.task]
        child += (["--flow-json", args.flow_json] if getattr(args, "flow_json", None)
                  else ["--flow", args.flow])
        if getattr(args, "artifact", None):
            child += ["--artifact", args.artifact]
        logpath = os.path.join(repo, ENGINE_DIR, f"run-flow-s{args.session}.log")
        conn.close()
        import rig as _rig_pkg
        rig_root = os.path.dirname(os.path.dirname(os.path.abspath(_rig_pkg.__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = (rig_root + os.pathsep + env["PYTHONPATH"]) if env.get("PYTHONPATH") else rig_root
        with open(logpath, "a", encoding="utf-8") as lf:
            proc = subprocess.Popen(child, cwd=repo, env=env, stdout=lf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        try:                                           # Best-effort mutex and process handle.
            with open(pid_path, "w", encoding="utf-8") as pf:
                pf.write(str(proc.pid))
        except OSError:
            pass
        print(f"[rig-tool run-flow] flow '{flow_label}' started in background for session {args.session}.\n"
              f"Track progress with: rig-tool journal --repo . --session {args.session}. "
              f"New rows mean agents are working. The final '{FLOW_COMPLETE_MARKER}' note summarizes "
              f"green phases, units, and the branch to merge. Intervene only if both the journal and heartbeat stall.")
        return 0

    from ..flow import run_flow
    from ..transport.local import LocalRunner
    from .cli import build_executor
    runner = LocalRunner()
    _, _bcap = _caps(conn, args.session)
    _, _spent = _ledger(conn, args.session)
    ex = build_executor(runner, repo, conn, args.session, worker=args.harness,
                        judge=args.judge or args.harness, worker_model=args.model,
                        judge_model=(args.judge_model or args.model),
                        budget_cap=(None if _bcap is None else max(0.0, _bcap - _spent)))
    pid_path = _flow_pid_path(repo, args.session)
    try:
        flow_name = "" if getattr(args, "flow_json", None) else (args.flow or "")
        fres = run_flow(phases, ex, ctx={"task": args.task, "session_id": args.session,
                                         "flow": flow_name,
                                         "artifact_context": artifact_text}, base="HEAD")
        if fres.ok:
            from ..flow.execute import gc_unit_branches
            sb = fres.ctx.get("session_branch") or getattr(ex, "_session_branch", None)
            gc_unit_branches(LocalRunner(), repo, session_branch=sb,
                             green_branches=fres.ctx.get("green_branches")
                             or getattr(ex, "_units_green_branches", []))
        summary = _render_flow(flow_label, fres)
        dao.log_run(conn, session_id=args.session, kind="note", result=FLOW_COMPLETE_MARKER + " " + summary)
        print(summary)
        return 0 if fres.ok else 1
    except Exception as e:
        dao.log_run(conn, session_id=args.session, kind="note",
                    result=f"[flow fail] {type(e).__name__}: {e}")
        print(f"[rig-tool run-flow] flow failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            os.remove(pid_path)
        except OSError:
            pass


def _render_flow(flow_name: str, fres) -> str:
    """Render the flow summary consumed by the lead."""
    L = [f"FLOW '{flow_name}': {'OK (all green)' if fres.ok else 'INCOMPLETE'}"]
    for r in fres.results:
        mark = "green" if r["green"] else "RED"
        extra = f" attempts={r['attempts']}" if r.get("attempts", 1) > 1 else ""
        L.append(f"  [{mark}] {r['name']}{extra} - {(r.get('detail') or '')[:120]}")
    sb = fres.ctx.get("session_branch")
    integ = fres.ctx.get("integration_branch")
    greens = fres.ctx.get("green_branches") or []
    if sb:
        L.append(f"  session branch for human merge: {sb}")
    elif greens:
        L.append(f"  accepted branch: {', '.join(greens)}")
    if integ and not sb:
        L.append(f"  integration branch for human merge: {integ}")
    if fres.halted:
        L.append("  ATTENTION: the flow stopped at a non-green barrier; inspect the reason above.")
    return "\n".join(L)


def cmd_recall(args) -> int:
    """Recall project memory relevant to a query [A3]."""
    from ..db import dao
    from ..memory import recall

    conn = dao.open_project(os.path.abspath(args.repo))
    rec = recall.load(conn, args.query, k=args.k)
    texts = [l["text"] for l in (rec.always + rec.selected)]
    if not texts:
        print("(no project memory matched this query)")
    else:
        print("PROJECT MEMORY (lessons from prior sessions):")
        print("\n".join(f"- {t}" for t in texts))
    return 0


def cmd_remember(args) -> int:
    """Store an agent-authored lesson as pending human review [D1]."""
    from ..db import dao
    from ..memory import store
    from ..tools.memory import MemoryTool

    conn = dao.open_project(os.path.abspath(args.repo))
    mt = MemoryTool(conn, scope=store.ORCHESTRATOR, session_id=args.session)
    lid = mt.remember(args.text, kind=store.FREE)
    print(f"[rig-tool] lesson #{lid} recorded (pending human promotion)")
    return 0


def cmd_send(args) -> int:
    """Send a progress message through the configured channel."""
    from ..tools import send
    r = send(args.channel, args.text, target=args.target)
    if r.sent:
        print(f"[rig-tool send] {args.channel}: sent")
        return 0
    print(f"[rig-tool send] {args.channel}: {'skipped - ' if r.skipped else 'delivery failed - '}{r.reason}",
          file=sys.stderr)
    return 1



def _open(args):
    from ..db import dao
    return dao.open_project(os.path.abspath(args.repo))


def cmd_decision(args) -> int:
    """Add, list, or supersede durable decisions."""
    from ..db import dao
    conn = _open(args)
    if args.verb == "add":
        if not (args.value or "").strip():
            print("[rig-tool decision] text required: decision add \"<decision and rationale>\"", file=sys.stderr)
            return 2
        did = dao.add_decision(conn, text=args.value.strip(), session_id=args.session,
                               feature=args.feature or "")
        print(f"[rig-tool decision] decision #{did} locked; supersede decision #{did} with drop")
        return 0
    if args.verb == "list":
        rows = dao.list_decisions(conn)
        if not rows:
            print("(no active decisions)")
            return 0
        print(f"ACTIVE DECISIONS ({len(rows)}; supersede explicitly with drop):")
        for r in rows:
            print(f"  #{r['decision_id']} {r['text']}" + (f" [{r['feature']}]" if r.get("feature") else ""))
        return 0
    if args.verb == "drop":
        try:
            dao.supersede_decision(conn, int(args.value), reason=args.reason or "")
        except (TypeError, ValueError) as e:
            print(f"[rig-tool decision] {e if str(e) else 'id required: decision drop <id> --reason ...'}",
                  file=sys.stderr)
            return 1
        print(f"[rig-tool decision] decision #{args.value} superseded")
        return 0
    print(f"[rig-tool decision] unknown verb {args.verb!r} (expected add, list, or drop)", file=sys.stderr)
    return 2


def cmd_board(args) -> int:
    """Add, list, complete, or drop cross-session board items."""
    from ..db import dao
    conn = _open(args)
    if args.verb == "add":
        if not (args.value or "").strip():
            print("[rig-tool board] text required: board add \"<remaining work>\"", file=sys.stderr)
            return 2
        iid = dao.add_board_item(conn, text=args.value.strip(), session_id=args.session,
                                 feature=args.feature or "", needs_human=bool(args.needs_human))
        print(f"[rig-tool board] item #{iid} added"
              + (" (awaiting human input)" if args.needs_human else " (runnable without human input)"))
        return 0
    if args.verb == "list":
        rows = dao.list_board(conn, status=None if args.all else "open")
        if not rows:
            print("(board empty)")
            return 0
        print(f"BOARD ({len(rows)} items):")
        for r in rows:
            who = "awaiting human input" if r["needs_human"] else "runnable"
            st = f"/{r['status']}" if args.all else ""
            print(f"  #{r['item_id']} [{who}{st}] {r['text']}")
        return 0
    if args.verb in ("done", "drop"):
        try:
            dao.set_board_status(conn, int(args.value), "done" if args.verb == "done" else "dropped")
        except (TypeError, ValueError) as e:
            print(f"[rig-tool board] {e if str(e) else f'id required: board {args.verb} <id>'}", file=sys.stderr)
            return 1
        print(f"[rig-tool board] item #{args.value} marked {args.verb}")
        return 0
    print(f"[rig-tool board] unknown verb {args.verb!r} (expected add, list, done, or drop)", file=sys.stderr)
    return 2


def cmd_ask(args) -> int:
    """Queue a non-blocking question for the human."""
    from ..db import dao
    conn = _open(args)
    if not (args.text or "").strip():
        print("[rig-tool ask] text required: ask --text \"<question, 2-3 options, and recommendation>\"",
              file=sys.stderr)
        return 2
    mid = dao.add_message(conn, direction="out", kind="question", text=args.text.strip(),
                          session_id=args.session)
    print(f"[rig-tool ask] question #{mid} recorded (non-blocking; continue work; "
          f"human answer `rig --answer {mid}`)")
    return 0


def cmd_questions(args) -> int:
    """List outbound questions that have not been answered."""
    from ..db import dao
    conn = _open(args)
    rows = dao.open_questions(conn)
    if not rows:
        print("(no open questions)")
        return 0
    print(f"OPEN QUESTIONS ({len(rows)}; non-blocking):")
    for r in rows:
        print(f"  #{r['msg_id']} {r['text']}")
    return 0


def cmd_handoff(args) -> int:
    """Store a complete structured handoff for the current session."""
    from ..db import dao
    conn = _open(args)
    if not (args.text or "").strip():
        print("[rig-tool handoff] text required: handoff --text \"<structured handoff>\"", file=sys.stderr)
        return 2
    if dao.get_session(conn, args.session) is None:
        print(f"[rig-tool handoff] session #{args.session} not found", file=sys.stderr)
        return 1
    dao.update_session(conn, args.session, handoff=args.text.strip())
    print(f"[rig-tool handoff] handoff for session #{args.session} recorded (loaded on the next start)")
    return 0


def cmd_artifact(args) -> int:
    """Put, get, list, or drop versioned orchestration artifacts."""
    from ..db import dao
    conn = _open(args)
    if args.verb == "put":
        if not (args.value or "").strip():
            print("[rig-tool artifact] name required: artifact put <name> ...", file=sys.stderr)
            return 2
        if args.text is not None:
            body = args.text
        elif args.file:
            try:
                with open(args.file, encoding="utf-8") as f:
                    body = f.read()
            except OSError as e:
                print(f"[rig-tool artifact] could not read file: {e}", file=sys.stderr)
                return 1
        elif not sys.stdin.isatty():
            body = sys.stdin.read()
        else:
            body = ""
        if not body.strip():
            print("[rig-tool artifact] empty body: provide --text, --file, or stdin", file=sys.stderr)
            return 2
        aid = dao.put_artifact(conn, name=args.value.strip(), text=body, kind=args.kind or "",
                               session_id=args.session)
        print(f"[rig-tool artifact] {args.value!r} recorded (version #{aid}; read with: artifact get {args.value})")
        return 0
    if args.verb == "get":
        art = dao.get_artifact(conn, (args.value or "").strip())
        if art is None:
            print(f"[rig-tool artifact] artifact {args.value!r} not found (use artifact list)", file=sys.stderr)
            return 1
        sys.stdout.write(art["text"])
        return 0
    if args.verb == "list":
        rows = dao.list_artifacts(conn)
        if not rows:
            print("(no artifacts)")
            return 0
        print(f"ARTIFACTS ({len(rows)}):")
        for r in rows:
            kind = f" [{r['kind']}]" if r.get("kind") else ""
            print(f"  {r['name']}{kind} · {r['size']} characters. · {r['ts']}")
        return 0
    if args.verb == "drop":
        try:
            dao.drop_artifact(conn, (args.value or "").strip())
        except ValueError as e:
            print(f"[rig-tool artifact] {e}", file=sys.stderr)
            return 1
        print(f"[rig-tool artifact] {args.value!r} superseded (pins no longer injected; version retained)")
        return 0
    print(f"[rig-tool artifact] unknown verb {args.verb!r} (expected put, get, list, or drop)", file=sys.stderr)
    return 2


DRIFT_AFTER = 3


def _poll_drift_hint(repo: str, session: int, n_events: int) -> None:
    """Describe whether a detached flow is still making observable progress."""
    try:
        from ..db import convention
        path = os.path.join(convention.ensure_rig_dir(repo), f"journal-poll-{session}.json")
        st = {}
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f) or {}
        except (OSError, ValueError):
            st = {}
        streak = (int(st.get("streak", 0)) + 1) if st.get("events") == n_events else 0
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"events": n_events, "streak": streak}, f)
        if streak >= DRIFT_AFTER:
            print(f"[rig] journal did not grow for {streak} consecutive reads; stop automatic polling. "
                  f"Choose whether to wait longer, redelegate with a better prompt, or escalate STATUS: need_human. "
                  f"If a final marker is already present, act on it instead of polling again.")
    except Exception:
        pass


def _render_liveness(conn, session: int) -> None:
    """Render current heartbeat state and stalled invocations."""
    try:
        from ..core import events as ev
        from ..db import dao
        from ..flow.notify import maybe_notify
        rows = dao.list_liveness(conn, session_id=session)
        if not rows:
            return
        print(f"IN FLIGHT ({len(rows)}; mechanical heartbeat, not self-report):")
        for r in rows:
            who = "/".join(x for x in (r.get("role"), r.get("phase")) if x) or r.get("label", "?")
            age = int(r.get("beat_age_s") or 0)
            try:
                from datetime import datetime, timezone
                started = datetime.fromisoformat(r["started_ts"])
                mins = int((datetime.now(timezone.utc) - started).total_seconds() // 60)
            except (KeyError, ValueError, TypeError):
                mins = -1
            if age > dao.STALL_AFTER_SECONDS:
                print(f"  [{who}] no heartbeat for {age}s (running for about {mins}m); the caller process is probably "
                      f"dead. Treat the work as stalled: redelegate or escalate instead of waiting.")
                if not r.get("stalled"):
                    dao.mark_liveness_stalled(conn, r["live_id"])
                    ev.emit(conn, kind="stall", name=who, session_id=session,
                            params={"beat_age_s": age, "live_id": r["live_id"]})
                    maybe_notify(conn, kind="stall", session_id=session,
                                    text=f"rig #{session}: heartbeat lost for {who} ({age}s); invocation may be stuck")
            else:
                print(f"  [{who}] live for ~{mins}m (heartbeat {age}s ago); still working.")
    except Exception:
        pass


def cmd_journal(args) -> int:
    """Render a compact session journal for lead resume context."""
    from ..db import dao

    conn = dao.open_project(os.path.abspath(args.repo))
    runs = dao.list_runs_light(conn, session_id=args.session)
    if not runs:
        print("(session journal is empty; this is the first execution)")
        _render_liveness(conn, args.session)
        return 0
    print(f"SESSION #{args.session} JOURNAL ({len(runs)} recorded events):")
    for r in runs:
        tag = r.get("phase") or r.get("kind")
        mark = ("ok" if r.get("ok") else "FAIL") if r.get("kind") == "invoke" else "·"
        out = (r.get("result") or "").replace("\n", " ")[:140]
        print(f"  [{tag}/{mark}] {out}")
    _render_liveness(conn, args.session)
    _poll_drift_hint(os.path.abspath(args.repo), args.session, len(runs))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rig-tool", description="agent-facing tools for the lead orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, *, need_session=True):
        p.add_argument("--repo", default=".", help="repository for this session (default: current directory)")
        if need_session:
            p.add_argument("--session", type=int, required=True, help="session id")
        p.add_argument("--harness", default=os.environ.get("RIG_WORKER", "claude"), choices=["claude", "codex"])
        p.add_argument("--model", default=os.environ.get("RIG_WORKER_MODEL", "claude-sonnet-4-6"),
                       help="worker model")

    sp = sub.add_parser("spawn", help="delegate one guarded read-only sub-agent")
    common(sp)
    sp.add_argument("--prompt-file", default=None, help="file containing the prompt")
    sp.add_argument("--prompt", default=None, help="inline prompt (alternative to --prompt-file)")
    sp.add_argument("--perms", default="read", choices=["read", "plan"])
    sp.add_argument("--timeout", type=float, default=240.0)
    sp.set_defaults(fn=cmd_spawn)

    rf = sub.add_parser("run-flow", help="run a deterministic named or custom flow")
    common(rf)
    rf.add_argument("--flow", default="feature/small",
                    help="named flow under flows/ (default: feature/small; also feature/large, bug/small, "
                         "bug/large, arch/design, review/only, or research/audit); human gates auto-pass "
                         "inside lead-driven run-flow")
    rf.add_argument("--flow-json", default=None, help="custom flow JSON using the same phase roles")
    rf.add_argument("--artifact", default=None,
                    help="artifact name to embed in plans and unit briefs (for example dossier or checklist)")
    rf.add_argument("--task", required=True)
    rf.add_argument("--judge", default=None, choices=["claude", "codex"])
    rf.add_argument("--judge-model", default=os.environ.get("RIG_JUDGE_MODEL", ""),
                    help="judge model (empty uses the worker model)")
    rf.add_argument("--_bg-worker", dest="bg_worker", action="store_true", help=argparse.SUPPRESS)
    rf.set_defaults(fn=cmd_run_flow)

    fo = sub.add_parser("fanout", help="run several read-only scouting sub-agents in parallel")
    common(fo)
    fo.add_argument("--prompts-json", required=True, help="JSON array of prompt strings")
    fo.add_argument("--timeout", type=float, default=180.0)
    fo.set_defaults(fn=cmd_fanout)

    rc = sub.add_parser("recall", help="search project memory for a query")
    common(rc, need_session=False)
    rc.add_argument("--query", required=True)
    rc.add_argument("--k", type=int, default=5)
    rc.set_defaults(fn=cmd_recall)

    rm = sub.add_parser("remember", help="record a pending project lesson")
    common(rm)
    rm.add_argument("--text", required=True)
    rm.set_defaults(fn=cmd_remember)

    jr = sub.add_parser("journal", help="show the session journal for resume")
    common(jr)
    jr.set_defaults(fn=cmd_journal)

    sd = sub.add_parser("send", help="send an external message through telegram or webhook")
    sd.add_argument("--channel", required=True, help="notification channel")
    sd.add_argument("--text", required=True)
    sd.add_argument("--target", default=None, help="override recipient (chat id or URL)")
    sd.set_defaults(fn=cmd_send)

    def common_db(p, *, need_session=True):
        p.add_argument("--repo", default=".", help="repository for this session (default: current directory)")
        p.add_argument("--session", type=int, required=need_session, default=None, help="session id")

    dc = sub.add_parser("decision", help="ledger decision: add \"text\" | list | drop <id> --reason")
    common_db(dc)
    dc.add_argument("verb", choices=["add", "list", "drop"])
    dc.add_argument("value", nargs="?", default=None, help="text (add) or id (drop)")
    dc.add_argument("--feature", default="", help="feature label")
    dc.add_argument("--reason", default="", help="why remove (drop)")
    dc.set_defaults(fn=cmd_decision)

    bd = sub.add_parser("board", help="board: add \"text\" [--needs-human] | list [--all] | done <id> | drop <id>")
    common_db(bd)
    bd.add_argument("verb", choices=["add", "list", "done", "drop"])
    bd.add_argument("value", nargs="?", default=None, help="text (add) or id (done/drop)")
    bd.add_argument("--needs-human", action="store_true", help="mark an item as waiting for human input")
    bd.add_argument("--feature", default="", help="feature label")
    bd.add_argument("--all", action="store_true", help="include completed and dropped items")
    bd.set_defaults(fn=cmd_board)

    ak = sub.add_parser("ask", help="queue a non-blocking question for the human")
    common_db(ak)
    ak.add_argument("--text", required=True, help="decision packet: question, 2-3 options, and recommendation")
    ak.set_defaults(fn=cmd_ask)

    qs = sub.add_parser("questions", help="list open questions for the human")
    common_db(qs, need_session=False)
    qs.set_defaults(fn=cmd_questions)

    ho = sub.add_parser("handoff", help="record a structured cross-session handoff")
    common_db(ho)
    ho.add_argument("--text", required=True, help="structured handoff: first step, completed work, remaining work, and questions")
    ho.set_defaults(fn=cmd_handoff)

    ar = sub.add_parser("artifact", help="artifacts: put <name> (--text|--file|stdin) | get <name> | list | drop <name>")
    common_db(ar, need_session=False)
    ar.add_argument("verb", choices=["put", "get", "list", "drop"])
    ar.add_argument("value", nargs="?", default=None, help="artifact name")
    ar.add_argument("--kind", default="", help="kind (convention: dossier, checklist, review, or pins)")
    ar.add_argument("--text", default=None, help="body inline")
    ar.add_argument("--file", default=None, help="body from file")
    ar.set_defaults(fn=cmd_artifact)
    return ap


def _emit_tool_event(args, code: int, duration_ms: int) -> None:
    """Record one agent-facing CLI call in the event timeline."""
    try:
        from ..core import events as ev
        from ..db import dao
        repo = getattr(args, "repo", None)
        if repo is None:
            return
        params = {}
        for key in ("verb", "value", "flow", "channel", "kind", "query"):
            val = getattr(args, key, None)
            if val:
                params[key] = str(val)[:120]
        if getattr(args, "bg_worker", False):
            params["bg"] = "worker"     # Distinguish a background executor from its parent flow.
        conn = dao.open_project(os.path.abspath(repo))
        try:
            ev.emit(conn, actor="lead", kind="tool_call", name=args.cmd,
                    session_id=getattr(args, "session", None), params=params,
                    status=str(code), duration_ms=duration_ms)
        finally:
            conn.close()               # Close the inherited connection before background database work.
    except Exception:
        pass


def main(argv=None) -> int:
    import time

    from ..db import convention
    convention.load_env_into_process()             # Load ~/.rig/.env before parsing environment defaults.
    args = _build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    t0 = time.monotonic()
    try:
        code = args.fn(args)
    except Exception:
        _emit_tool_event(args, -1, int((time.monotonic() - t0) * 1000))
        raise
    _emit_tool_event(args, code, int((time.monotonic() - t0) * 1000))
    return code


if __name__ == "__main__":
    sys.exit(main())
