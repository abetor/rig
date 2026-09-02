"""Spawn guarded agent work in isolated worktrees."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from ..adapters.base import ENGINE_DIR
from ..core.launch import BackgroundRun, agent
from ..core.result import Result
from ..core.types import VALID_UNIT_ID
from ..util import git, worktree as worktree_env


class SpawnRefused(Exception):
    """Raised when spawn guardrails reject a request."""


@dataclass
class Budget:
    """Shared cost ledger for a tree of spawned agents."""
    cap: Optional[float] = None
    spent: float = 0.0

    def remaining(self) -> float:
        return float("inf") if self.cap is None else max(0.0, self.cap - self.spent)

    def would_exceed(self, est: float) -> bool:
        return self.cap is not None and (self.spent + max(0.0, est or 0.0)) > self.cap

    def charge(self, amount: Optional[float]) -> None:
        self.spent += max(0.0, amount or 0.0)


@dataclass
class Spawner:
    """Launch agents under depth, cost, and worktree guardrails."""
    adapter: object
    repo: str
    model: str
    conn: object = None
    session_id: Optional[int] = None
    depth: int = 0
    max_depth: int = 2
    role: str = "worker"
    budget: Budget = field(default_factory=Budget)
    _children: List[BackgroundRun] = field(default_factory=list)
    _worktrees: List[dict] = field(default_factory=list)

    def _guard(self, est_cost: float) -> None:
        if self.depth + 1 > self.max_depth:                       # Depth cap [F7].
            raise SpawnRefused(
                f"depth-cap: daughter depth {self.depth + 1} > max {self.max_depth} [F7]")
        if self.budget.would_exceed(est_cost):                    # Cost gate before fan-out [F7].
            raise SpawnRefused(
                f"cost-cap: spent {self.budget.spent:.4f} + est {max(0.0, est_cost):.4f} "
                f"> cap {self.budget.cap} [F7]")

    def worktree_path(self, unit_id: str) -> str:
        """Return the isolated worktree path for a unit ID."""
        return os.path.join(self.repo, ENGINE_DIR, "wt", unit_id)

    def _make_worktree(self, unit_id: str, base: str) -> str:
        """Create worktree."""
        if not VALID_UNIT_ID.match(unit_id or ""):
            raise ValueError(f"spawn: invalid unit_id {unit_id!r} (anti path-traversal [A1])")
        branch = f"rig/{unit_id}"
        wt = self.worktree_path(unit_id)
        self._refuse_foreign_green(branch)            # Preserve another session's green work.
        git.worktree_remove(self.adapter.runner, self.repo, wt)   # Remove a stale prior attempt.
        git.branch_delete(self.adapter.runner, self.repo, branch)
        if not git.worktree_add(self.adapter.runner, self.repo, wt, branch, base):
            raise SpawnRefused(f"spawn: could not add a worktree for {unit_id!r} (check branch and base)")
        rep = worktree_env.provision(self.adapter.runner, self.repo, wt)
        self._log_provision(unit_id, rep)             # Keep environment degradation visible in the log.
        self._worktrees.append({"path": wt, "branch": branch})
        return wt

    def _refuse_foreign_green(self, branch: str) -> None:
        """Reject a base branch owned by another green unit."""
        if self.conn is None:
            return
        from ..db import dao
        owners = [s for s in dao.list_sessions(self.conn)
                  if s.get("session_id") != self.session_id and branch in (s.get("in_flight") or [])]
        if owners and git.branch_exists(self.adapter.runner, self.repo, branch):
            raise SpawnRefused(
                f"spawn: branch {branch!r} contains green, unmerged work from session "
                f"{owners[0].get('session_id')} [E3/F6]; merge it or remove it explicitly")

    def _log_provision(self, unit_id: str, rep) -> None:
        """Log provision."""
        if self.conn is None:
            return
        parts: List[str] = []
        if rep.linked:
            parts.append("link=" + ",".join(rep.linked))
        if rep.copied:
            parts.append("copy=" + ",".join(rep.copied))
        if rep.skipped:
            parts.append("skip=" + "; ".join(rep.skipped))
        if rep.setup_ok is not None:
            parts.append("setup=" + ("ok" if rep.setup_ok else "FAIL"))
        if not parts:                                 # No provisioning declaration means no log event.
            return
        try:
            from ..db import dao
            dao.log_run(self.conn, session_id=self.session_id, kind="provision",
                        phase=unit_id, result="; ".join(parts))
        except Exception:
            pass

    def spawn(self, prompt, *, wait: str = "sync", est_cost: float = 0.0,
              isolated_worktree: bool = False, unit_id: Optional[str] = None,
              base: str = "HEAD", cwd: Optional[str] = None, model: Optional[str] = None,
              **agent_kwargs):
        """Launch one agent invocation and reconcile its cost."""
        self._guard(est_cost)
        if isolated_worktree:
            cwd = self._make_worktree(unit_id, base)
        cwd = cwd or self.repo

        self.budget.charge(est_cost)                              # Reserve capacity before execution.
        reserved = max(0.0, est_cost)
        on_collect = None
        if wait == "background":
            on_collect = lambda r: self.budget.charge((getattr(r, "cost_usd", 0.0) or 0.0) - reserved)
        result = agent(
            self.adapter, prompt, wait=wait, cwd=cwd, model=model or self.model,
            conn=self.conn, session_id=self.session_id, role=self.role,
            on_collect=on_collect, **agent_kwargs,
        )

        if wait == "sync" and isinstance(result, Result):
            self.budget.charge((result.cost_usd or 0.0) - reserved)            # Reconcile with actual cost.
        elif wait == "parallel" and isinstance(result, list):
            actual = sum((r.cost_usd or 0.0) for r in result if isinstance(r, Result))
            self.budget.charge(actual - reserved)                              # Reconcile the full batch cost.
        elif wait == "background" and isinstance(result, BackgroundRun):
            self._children.append(result)                        # Track it for cancellation and collection.
        return result

    def child(self, **overrides) -> "Spawner":
        """Create a descendant spawner sharing budget and cancellation state."""
        kw = dict(adapter=self.adapter, repo=self.repo, model=self.model, conn=self.conn,
                  session_id=self.session_id, depth=self.depth + 1, max_depth=self.max_depth,
                  role=self.role, budget=self.budget)
        kw.update(overrides)
        sub = Spawner(**kw)
        sub._children = self._children        # Share cancellation state across descendants.
        sub._worktrees = self._worktrees
        return sub

    def kill_all(self) -> int:
        """Terminate every tracked background invocation."""
        n = 0
        for ch in self._children:
            try:
                ch.kill()
                n += 1
            except Exception:
                pass
        return n

    def gc(self) -> int:
        """Remove tracked temporary worktrees and branches."""
        n = 0
        for w in self._worktrees:
            git.worktree_remove(self.adapter.runner, self.repo, w["path"])
            git.branch_delete(self.adapter.runner, self.repo, w["branch"])
            n += 1
        self._worktrees.clear()
        return n


__all__ = ["Spawner", "Budget", "SpawnRefused"]
