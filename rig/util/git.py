"""Thin, fail-soft Git command helpers."""
from __future__ import annotations

from typing import Optional


def _git(runner, repo: str, args: list):
    """Run a Git command in the requested repository."""
    return runner.exec(["git", "-C", repo, *args], cwd=repo, risk="low")


def head(runner, repo: str) -> str:
    """Return the current HEAD commit."""
    r = _git(runner, repo, ["rev-parse", "HEAD"])
    return r.stdout.strip() if r.ok else ""


def branches(runner, repo: str, prefix: str) -> list:
    """List local branch names."""
    return branches_checked(runner, repo, prefix)[1]


def branches_checked(runner, repo: str, prefix: str):
    """List local branches and report whether Git succeeded."""
    r = _git(runner, repo, ["branch", "--list", f"{prefix}*", "--format=%(refname:short)"])
    if not r.ok:
        return False, []
    return True, [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def dirty(runner, repo: str):
    """Return whether the worktree has tracked or untracked changes."""
    r = _git(runner, repo, ["status", "--porcelain"])
    if not r.ok:
        return None
    return bool((r.stdout or "").strip())


def branch_exists(runner, repo: str, branch: str) -> bool:
    """Return whether a local branch exists."""
    return _git(runner, repo, ["rev-parse", "--verify", "--quiet", branch]).ok


def is_ancestor(runner, repo: str, anc: str, desc: str) -> bool:
    """Return whether ancestor."""
    return _git(runner, repo, ["merge-base", "--is-ancestor", anc, desc]).ok


def ahead_count(runner, repo: str, base: str, head_: str) -> Optional[int]:
    """Count commits in one revision that are absent from another."""
    if not base or not head_:
        return None
    r = _git(runner, repo, ["rev-list", "--count", f"{base}..{head_}"])
    if not r.ok:
        return None
    try:
        return int(r.stdout.strip())
    except (ValueError, AttributeError):
        return None


def worktree_add(runner, repo: str, path: str, branch: str, base: str = "HEAD") -> bool:
    """Create a detached worktree and branch from a base revision."""
    return runner.exec(["git", "-C", repo, "worktree", "add", "-b", branch, path, base],
                       cwd=repo, risk="medium").ok


def worktree_remove(runner, repo: str, path: str) -> bool:
    """Remove a Git worktree."""
    ok = runner.exec(["git", "-C", repo, "worktree", "remove", "--force", path],
                     cwd=repo, risk="medium").ok
    runner.exec(["git", "-C", repo, "worktree", "prune"], cwd=repo, risk="low")
    return ok


def branch_set(runner, repo: str, branch: str, ref: str) -> bool:
    """Force a local branch to a revision."""
    return runner.exec(["git", "-C", repo, "branch", "-f", branch, ref],
                       cwd=repo, risk="medium").ok


def branch_delete(runner, repo: str, branch: str) -> bool:
    """Delete a local branch."""
    return runner.exec(["git", "-C", repo, "branch", "-D", branch], cwd=repo, risk="medium").ok


def stage_all(runner, repo: str, wt: str) -> bool:
    """Stage all worktree changes."""
    return runner.exec(["git", "-C", wt, "add", "-A", "--", ".", ":!.rig"],
                       cwd=repo, risk="low").ok


def diff_cached(runner, repo: str, wt: str) -> str:
    """Return the staged diff."""
    r = runner.exec(["git", "-C", wt, "diff", "--cached"], cwd=repo, risk="low")
    return r.stdout if getattr(r, "ok", False) else ""


def diff_head(runner, repo: str, cwd: str) -> str:
    """Return tracked and untracked changes relative to HEAD."""
    r = runner.exec(["git", "-C", cwd, "diff", "HEAD"], cwd=repo, risk="low")
    return r.stdout if getattr(r, "ok", False) else ""


def commit_staged(runner, repo: str, wt: str, message: str,
                  author: str = "rig", email: str = "rig@local") -> bool:
    """Commit staged changes with the supplied message."""
    return runner.exec(["git", "-C", wt, "-c", f"user.name={author}", "-c", f"user.email={email}",
                        "commit", "-m", message], cwd=repo, risk="low").ok
