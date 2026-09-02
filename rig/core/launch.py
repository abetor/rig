"""The single agent-invocation primitive.

``agent()`` serves leads, executors, judges, and reviewers. It combines an adapter,
its transport, and audit logging without embedding task logic. Calls may run
synchronously, in an index-stable parallel batch [B6], or in the background.
Abstract permissions are mapped by each adapter; isolated calls drop resume state
[B3], and requested schemas fail closed if the harness cannot enforce them [B1].

Every collected invocation is logged once in ``runs`` [D11]. The caller injects
the database connection because a worker's ``cwd`` may be a worktree rather than
the project root. Git operations, guardrails, and flow advancement belong to the
higher layers.
"""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from subprocess import TimeoutExpired
from typing import List, Optional, Sequence, Union

from ..memory.log import log_call
from .result import Result, apply_timeout
from .types import ENGINE_DIR, VALID_PERMS  # Shared core constants; adapters must not redefine them.

# Execution modes. Unknown values fail loudly rather than falling back.
VALID_WAIT = ("sync", "parallel", "background")

# Leads and infrastructure need the full environment. Every other role is an
# untrusted leaf invocation and is confined [A2]. Unknown roles fail closed.
_TRUSTED_ROLES = frozenset({"lead", ""})

# Bound parallel subprocesses even though judge calls are mostly I/O-bound.
_MAX_PARALLEL_WORKERS = 8


def _abstain(reason: str, *, exit_code: int = -1) -> Result:
    """Return a fail-closed abstention for an invalid constrained answer [B1]."""
    return Result(ok=False, text="", exit_code=exit_code, schema_applied=False, error=reason)


def _cost_origin(cost_usd: Optional[float]) -> str:
    """Classify reported dollar cost without inventing an estimate [G4]."""
    return "exact" if cost_usd is not None else "no_pricing"


def _do_invoke(
    adapter,
    prompt: str,
    *,
    model: str,
    cwd: str,
    perms: str,
    effort: Optional[str],
    isolated: bool,
    resume_id: Optional[str],
    system_context_path: Optional[str],
    schema: Optional[str],
    risk: str,
    timeout: Optional[float],
    allowed_tools: Optional[str] = None,
    confine: bool = False,
) -> Result:
    """Invoke synchronously after checking schema capability [B1/G6].

    A harness without constrained output may silently ignore a schema. Refuse the
    call before execution, then defensively reject any result that does not confirm
    the schema was applied.
    """
    if schema is not None and not adapter.capabilities().schema_output:
        # Do not treat unconstrained output as a valid judge verdict [B1/G6].
        return _abstain(
            f"schema requested but adapter {getattr(adapter, 'name', '?')!r} lacks "
            f"schema_output capability [B1/G6]"
        )
    # Isolated judges must not inherit conversation state through resume [B3].
    eff_resume = None if isolated else resume_id
    res = adapter.invoke(
        prompt,
        model=model,
        cwd=cwd,
        perms=perms,
        effort=effort,
        system_context_path=system_context_path,
        resume_session_id=eff_resume,
        timeout=timeout,
        risk=risk,
        schema_path=schema,
        allowed_tools=allowed_tools,
        confine=confine,
    )
    if schema is not None and res.schema_applied is not True:
        # A requested but unapplied schema fails closed [B1].
        res.ok = False
    return res


def _log(
    conn,
    *,
    prompt: str,
    result: Result,
    adapter_name: str,
    perms: str,
    wait: str,
    isolated: bool,
    model: Optional[str],
    schema: Optional[str],
    session_id: Optional[int],
    role: str,
    phase: str,
    effort: Optional[str] = None,
) -> Optional[int]:
    """Log a complete invocation in ``runs``; do nothing without a connection [D11]."""
    if conn is None:
        return None
    status_val = ""
    if result.status:
        status_val = str(result.status.get("status", ""))
    meta = {
        "wait": wait,
        "perms": perms,
        "isolated": isolated,
        "model": model,
        # Preserve requested effort because transcripts do not reliably expose it.
        "effort": effort,
        "schema": schema is not None,
        "schema_applied": result.schema_applied,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
    }
    if result.error:
        meta["error"] = result.error
    return log_call(
        conn,
        prompt=prompt,
        result=result.text or "",
        session_id=session_id,
        agent=role,
        adapter=adapter_name,
        phase=phase,
        status=status_val,
        ok=bool(result.ok),
        cost=result.cost_usd,
        cost_origin=_cost_origin(result.cost_usd),
        resume_id=(result.session_id or ""),  # Native session ID for a later resume [G2].
        meta=meta,
        transcript=result.transcript,  # Keep the harness transcript in our database [D11].
    )


class BackgroundRun:
    """Handle a detached invocation and collect its result later.

    Logging happens at collection, when both input and output exist [D11]. Repeated
    ``result()`` calls return the cached result and never duplicate the log row.
    """

    def __init__(
        self,
        adapter,
        handle,
        *,
        prompt: str,
        conn,
        session_id: Optional[int],
        perms: str,
        isolated: bool,
        model: Optional[str],
        role: str,
        phase: str,
        effort: Optional[str] = None,
        on_collect=None,
    ):
        self.adapter = adapter
        self.handle = handle
        self.log_path = getattr(handle, "log_path", "")
        self._prompt = prompt
        self._conn = conn
        self._session_id = session_id
        self._perms = perms
        self._isolated = isolated
        self._model = model
        self._effort = effort
        self._role = role
        self._phase = phase
        self._result: Optional[Result] = None
        self._logged = False
        # Run this hook once, when actual cost is known and can be charged [F7].
        self._on_collect = on_collect

    def poll(self) -> Optional[int]:
        """Return ``None`` while running, otherwise the process exit status."""
        return self.handle.poll()

    def kill(self) -> None:
        """Terminate the background process and its process group."""
        self.handle.kill()

    def result(self, timeout: Optional[float] = None) -> Result:
        """Wait, parse the output log, record the call, and return its result [D11]."""
        if self._result is not None:
            return self._result
        try:
            exit_code = self.handle.wait(timeout)
        except TimeoutExpired:
            self.handle.kill()  # A timed-out process has no trustworthy terminal state.
            res = apply_timeout(
                Result(ok=False, text="", exit_code=124), timed_out=True, exit_code=124
            )
        else:
            try:
                stdout = self.adapter.runner.read_file(self.log_path)
            except (OSError, ValueError):
                stdout = ""  # Let the adapter turn an unreadable log into a failed result.
            res = self.adapter.parse_output(stdout, exit_code)
        self._result = res
        if not self._logged:
            _log(
                self._conn,
                prompt=self._prompt,
                result=res,
                adapter_name=getattr(self.adapter, "name", ""),
                perms=self._perms,
                wait="background",
                isolated=self._isolated,
                model=self._model,
                effort=self._effort,
                schema=None,
                session_id=self._session_id,
                role=self._role,
                phase=self._phase,
            )
            self._logged = True
        if self._on_collect is not None:
            try:
                self._on_collect(res)          # Charge actual cost without risking collection [F7].
            except Exception:  # noqa: BLE001
                pass
            self._on_collect = None            # Preserve idempotency across repeated result calls.
        return res


def agent(
    adapter,
    prompt: Union[str, Sequence[str]],
    *,
    wait: str = "sync",
    perms: str = "default",
    allowed_tools: Optional[str] = None,
    isolated: bool = False,
    schema: Optional[str] = None,
    resume_id: Optional[str] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    effort: Optional[str] = None,
    system_context_path: Optional[str] = None,
    risk: str = "medium",
    conn=None,
    session_id: Optional[int] = None,
    role: str = "",
    phase: str = "",
    log_path: Optional[str] = None,
    max_parallel: int = _MAX_PARALLEL_WORKERS,
    on_collect=None,
) -> Union[Result, List[Result], BackgroundRun]:
    """Invoke one agent synchronously, in parallel, or in the background.

    Invalid modes, permissions, models, working directories, and prompt shapes
    raise before execution. Sync and parallel calls log immediately; background
    calls log when their result is collected [D11].
    """
    if wait not in VALID_WAIT:
        raise ValueError(f"agent: unknown wait mode {wait!r} (expected one of {VALID_WAIT})")
    if perms not in VALID_PERMS:
        raise ValueError(f"agent: unknown permission mode {perms!r} (expected one of {VALID_PERMS})")
    if model is None:
        raise ValueError("agent: a model is required because this CLI cannot infer one")
    if cwd is None:
        raise ValueError("agent: cwd required (where running process)")

    # Confine every untrusted harness call, including synchronous ones [A2].
    # Filesystem isolation remains the backend or VM's responsibility.
    confine = role not in _TRUSTED_ROLES

    # Parallel batch [B6].
    if wait == "parallel":
        if isinstance(prompt, str):
            raise ValueError("agent: wait='parallel' waits prompt = list prompts, have you ever heard of such a thing str")
        prompts: List[str] = list(prompt)
        if not prompts:
            return []  # The flow decides whether an empty batch is acceptable.

        def _safe_invoke(p: str) -> Result:
            # Isolate one invocation failure without crashing the entire batch [B6].
            try:
                return _do_invoke(
                    adapter, p, model=model, cwd=cwd, perms=perms, effort=effort,
                    isolated=isolated, resume_id=resume_id,
                    system_context_path=system_context_path, schema=schema, risk=risk,
                    timeout=timeout, allowed_tools=allowed_tools, confine=confine,
                )
            except Exception as e:  # noqa: BLE001 - isolate one failed dimension
                return _abstain(f"parallel invoke failed: {type(e).__name__}: {e}")

        # ``map`` preserves input order regardless of completion timing [B6]. Log from
        # the main thread because SQLite connections are not shared across threads.
        from .liveness import heartbeat
        with heartbeat(conn, session_id=session_id, role=role, phase=phase,
                       adapter=getattr(adapter, "name", ""), label=f"parallel×{len(prompts)}"):
            with ThreadPoolExecutor(max_workers=min(len(prompts), max(1, max_parallel))) as pool:
                results: List[Result] = list(pool.map(_safe_invoke, prompts))
        for p, r in zip(prompts, results):
            _log(conn, prompt=p, result=r, adapter_name=getattr(adapter, "name", ""),
                 perms=perms, wait="parallel", isolated=isolated, model=model, effort=effort,
                 schema=schema, session_id=session_id, role=role, phase=phase)
        return results

    # Detached execution, collected later.
    if wait == "background":
        if not isinstance(prompt, str):
            raise ValueError("agent: wait='background' waits prompt:str (one background invocation)")
        if schema is not None:
            # Background spawning cannot enforce a schema, so reject the combination [B1].
            raise ValueError("agent: schema incompatible with wait='background' (background not constrains) [B1]")
        if allowed_tools is not None:
            # Background spawning cannot preserve a tool allowlist; fail loudly.
            raise ValueError("agent: allowed_tools is incompatible with wait='background'; spawning cannot preserve the allowlist")
        eff_resume = None if isolated else resume_id  # Isolation means no conversation continuity [B3].
        lp = log_path or os.path.join(cwd, ENGINE_DIR, "logs", f"bg-{uuid.uuid4().hex[:12]}.log")
        handle = adapter.spawn(
            prompt, model=model, cwd=cwd, log_path=lp, perms=perms, effort=effort,
            system_context_path=system_context_path, resume_session_id=eff_resume, risk=risk,
        )
        return BackgroundRun(
            adapter, handle, prompt=prompt, conn=conn, session_id=session_id, perms=perms,
            isolated=isolated, model=model, effort=effort, role=role, phase=phase,
            on_collect=on_collect,
        )

    # One synchronous invocation.
    if not isinstance(prompt, str):
        raise ValueError("agent: wait='sync' waits prompt:str (one invocation)")
    # A fail-soft heartbeat marks the call in flight (plan §10, stage 2).
    from .liveness import heartbeat
    with heartbeat(conn, session_id=session_id, role=role, phase=phase,
                   adapter=getattr(adapter, "name", ""), label="sync"):
        res = _do_invoke(
            adapter, prompt, model=model, cwd=cwd, perms=perms, effort=effort, isolated=isolated,
            resume_id=resume_id, system_context_path=system_context_path, schema=schema, risk=risk,
            timeout=timeout, allowed_tools=allowed_tools, confine=confine,
        )
    _log(conn, prompt=prompt, result=res, adapter_name=getattr(adapter, "name", ""), perms=perms,
         wait="sync", isolated=isolated, model=model, effort=effort, schema=schema,
         session_id=session_id, role=role, phase=phase)
    return res


__all__ = ["agent", "BackgroundRun", "VALID_WAIT"]
