"""Create and remove isolated Git worktrees safely."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from typing import List, Optional

from ..adapters.base import ENGINE_DIR

WORKTREE_CONFIG = "worktree.json"


@dataclass
class WorktreeConfig:
    """Files and setup commands provisioned into a worktree."""
    link: List[str] = field(default_factory=list)   # Reuse heavy dependencies from the base checkout.
    copy: List[str] = field(default_factory=list)   # Copy small local configuration files.
    setup: List[str] = field(default_factory=list)  # Commands such as npm ci or uv sync.

    def is_empty(self) -> bool:
        return not (self.link or self.copy or self.setup)


@dataclass
class ProvisionReport:
    """Result of provisioning one worktree."""
    linked: List[str] = field(default_factory=list)
    copied: List[str] = field(default_factory=list)
    hidden: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)   # Entries use the format "path: reason".
    setup_ok: Optional[bool] = None


def _safe_rel(path: str) -> Optional[str]:
    """Normalize a safe project-relative path or reject it."""
    if not isinstance(path, str) or not path.strip():
        return None
    raw = path.strip()
    if os.path.isabs(raw) or raw.startswith("~"):
        return None
    norm = os.path.normpath(raw.lstrip("/"))
    if norm == "." or norm == ".." or ".." in norm.split(os.sep):
        return None
    return norm


def read_config(repo: str) -> WorktreeConfig:
    """Read config."""
    try:
        with open(os.path.join(repo, ENGINE_DIR, WORKTREE_CONFIG), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return WorktreeConfig()
    if not isinstance(d, dict):
        return WorktreeConfig()

    def _strlist(v) -> List[str]:
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    return WorktreeConfig(link=_strlist(d.get("link")), copy=_strlist(d.get("copy")),
                          setup=_strlist(d.get("setup")))


def _hide_from_git(runner, repo: str, wt: str, rels: List[str], rep: ProvisionReport) -> None:
    """Add provisioned paths to the worktree's local exclude file."""
    if not rels or runner is None:
        return
    try:
        r = runner.exec(["git", "-C", wt, "rev-parse", "--git-path", "info/exclude"], cwd=repo, risk="low")
        if not getattr(r, "ok", False):
            return
        p = (getattr(r, "stdout", "") or "").strip()
        if not p:
            return
        if not os.path.isabs(p):
            p = os.path.join(wt, p)                     # --git-path is relative to the worktree passed with -C.
        os.makedirs(os.path.dirname(p), exist_ok=True)
        content = ""
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                content = f.read()
        have = {ln.strip() for ln in content.splitlines()}   # Deduplicate exact path entries.
        add = [f"/{rel}" for rel in rels if f"/{rel}" not in have]
        if add:
            sep = "" if (content == "" or content.endswith("\n")) else "\n"
            with open(p, "a", encoding="utf-8") as f:
                f.write(sep + "\n".join(add) + "\n")
        rep.hidden.extend(rels)
    except OSError:
        pass


def provision(runner, repo: str, wt: str, config: Optional[WorktreeConfig] = None) -> ProvisionReport:
    """Provision configured dependencies and files into a worktree."""
    cfg = config if config is not None else read_config(repo)
    rep = ProvisionReport()
    provisioned: List[str] = []

    def _prepare(raw: str):
        """Prepare the requested worktree and return its report."""
        rel = _safe_rel(raw)
        if rel is None:
            return None, f"{raw}: unsafe path"
        src, dst = os.path.abspath(os.path.join(repo, rel)), os.path.join(wt, rel)
        if not os.path.exists(src):
            return None, f"{rel}: no spring"
        if os.path.lexists(dst):
            return None, f"{rel}: already exists in worktree"
        return (src, dst, rel), None

    for raw in cfg.link:
        ok, why = _prepare(raw)
        if ok is None:
            rep.skipped.append(why); continue
        src, dst, rel = ok
        try:
            os.makedirs(os.path.dirname(dst) or wt, exist_ok=True)
            os.symlink(src, dst)
            rep.linked.append(rel); provisioned.append(rel)
        except OSError as e:
            rep.skipped.append(f"{rel}: symlink failed ({e.__class__.__name__})")

    for raw in cfg.copy:
        ok, why = _prepare(raw)
        if ok is None:
            rep.skipped.append(why); continue
        src, dst, rel = ok
        try:
            os.makedirs(os.path.dirname(dst) or wt, exist_ok=True)
            (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)
            rep.copied.append(rel); provisioned.append(rel)
        except (OSError, shutil.Error) as e:
            rep.skipped.append(f"{rel}: copy failed ({e.__class__.__name__})")

    _hide_from_git(runner, repo, wt, provisioned, rep)   # Keep provisioned files out of diffs and commits.

    if cfg.setup and runner is not None:
        try:                                            # Run project setup inside the worktree.
            r = runner.exec(list(cfg.setup), cwd=wt, timeout=600, risk="medium")
            rep.setup_ok = bool(getattr(r, "ok", False))
        except Exception:
            rep.setup_ok = False

    return rep


__all__ = ["WorktreeConfig", "ProvisionReport", "read_config", "provision", "WORKTREE_CONFIG"]
