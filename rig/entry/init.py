"""Read-only startup diagnostics for required tools and agent harnesses.

Git and Python are required, tmux is optional, and at least one of Claude or Codex
must be available. Secret presence is reported without exposing values [D1].
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ..db import convention

# Git and Python are required; tmux adds orchestration windows but is optional.
_TOOLS = (("git", True), ("python3", True), ("tmux", False))
_HARNESSES = ("claude", "codex")
# Report only whether optional notification credentials exist [D1].
_SECRET_HINTS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WEBHOOK_URL")


@dataclass
class Readiness:
    """Startup-readiness report without secret values."""
    ready: bool
    tools: dict = field(default_factory=dict)          # Tool name to availability.
    missing: List[str] = field(default_factory=list)   # Missing required components.
    harnesses: List[str] = field(default_factory=list) # Available agent harnesses.
    secrets: dict = field(default_factory=dict)        # Secret name to presence, never value.


def check_readiness(*, which: Optional[Callable] = None, secrets: Optional[dict] = None) -> Readiness:
    """Inspect local readiness without writing state."""
    which = which or shutil.which

    tools, missing = {}, []
    for name, required in _TOOLS:
        found = which(name) is not None
        tools[name] = found
        if required and not found:
            missing.append(name)
    harnesses = [h for h in _HARNESSES if which(h) is not None]
    if not harnesses:
        missing.append("harness (claude|codex)")     # At least one agent harness is required.

    # Process variables override the user file; tests may inject a complete mapping.
    sec = {k: bool(secrets.get(k) if secrets is not None else convention.get_secret(k))
           for k in _SECRET_HINTS}
    return Readiness(ready=not missing, tools=tools, missing=missing, harnesses=harnesses, secrets=sec)


def render_readiness(r: Readiness) -> str:
    """Render a concise human-readable readiness report."""
    head = "READY" if r.ready else "NOT READY - install missing components"
    L = [f"rig init · {head}"]
    L.append("  tools: " + ", ".join(f"{n}={'found' if ok else 'MISSING'}" for n, ok in r.tools.items()))
    L.append("  harnesses: " + (", ".join(r.harnesses) or "none found (claude or codex required)"))
    L.append("  secrets (env): " + (", ".join(k for k, v in r.secrets.items() if v) or "none detected"))
    if r.missing:
        L.append("  missing: " + ", ".join(r.missing))
    return "\n".join(L)
