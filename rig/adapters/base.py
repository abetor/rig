"""Provider-specific agent CLI adapters.

An adapter defines what a CLI can do, builds its command line, parses its output, injects system
context, and exposes only the authentication environment it needs. Process placement remains the
runner's responsibility, so adapters and transports can be combined independently [G1].

Permission modes are selected per invocation and translated into CLI-specific flags here. ``auto``
is intentionally not the default [A7]. Schema requests fail closed: if a CLI cannot apply a requested
schema, the adapter returns a non-successful result and the consumer abstains [B1]. Both synchronous
and detached calls explicitly restore the provider credentials removed by worker environment
confinement [A3].
"""
from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from ..core.result import InvokeResult, Result, apply_timeout
from ..core.types import ENGINE_DIR, VALID_PERMS  # one source [abstractions-1]; re-export below
from ..transport.base import Handle, RiskTier, Runner

# Re-export these constants for legacy imports. Their canonical definitions live in core.types.


def system_prompt_path(cwd: str, method_text: str) -> str:
    """Return the content-addressed path for a system prompt under ``.rig`` [G5/D1].

    Different methods get different files, so concurrent lead and worker sessions cannot overwrite
    one another. Reusing the same method is idempotent. The directory is ignored by Git [A5].
    """
    h = hashlib.sha256(method_text.encode("utf-8")).hexdigest()[:12]
    return os.path.join(cwd, ENGINE_DIR, f"system-prompt-{h}.md")


@dataclass
class Capabilities:
    """CLI capabilities used by the engine to degrade explicitly when a feature is unavailable [G6]."""
    effort: bool            # configurable reasoning effort
    schema_output: bool     # schema-constrained output
    native_resume: bool     # native session resumption
    agents_md_native: bool  # native AGENTS.md support; otherwise inject a system prompt


class SchemaNotApplied(Exception):
    """The adapter could not apply a requested schema, so the invocation must fail closed [B1]."""


class HarnessAdapter(ABC):
    name: str = "harness"

    def __init__(self, runner: Runner):
        self.runner = runner

    # ---- [G1] The five provider-specific operations ----
    @abstractmethod
    def capabilities(self) -> Capabilities:
        """Describe the CLI features available to the engine [G6]."""

    @abstractmethod
    def build_cmd(self, prompt: str, *, model: str, perms: str = "default",
                  effort: Optional[str] = None, system_context_path: Optional[str] = None,
                  resume_session_id: Optional[str] = None,
                  schema_path: Optional[str] = None,
                  allowed_tools: Optional[str] = None) -> list[str]:
        """Build the CLI argument vector.

        Permissions are translated per call. A requested but unusable schema raises
        ``SchemaNotApplied`` [B1]. ``allowed_tools`` is an explicit allowlist where supported.
        """

    @abstractmethod
    def parse_output(self, stdout: str, exit_code: int) -> Result:
        """Parse provider output into a normalized ``Result`` [G2]."""

    @abstractmethod
    def inject_context(self, method_text: str, cwd: str) -> str:
        """Write a dedicated system prompt without overwriting project instruction files [G5]."""

    @abstractmethod
    def auth_env(self) -> dict:
        """Return only the provider credentials that must survive environment confinement [A3]."""

    # ---- Shared orchestration through the runner; subclasses should not override it ----
    def invoke(self, prompt: str, *, model: str, cwd: str, perms: str = "default",
               effort: Optional[str] = "medium", system_context_path: Optional[str] = None,
               resume_session_id: Optional[str] = None, timeout: Optional[float] = None,
               risk: RiskTier = "medium", schema_path: Optional[str] = None,
               allowed_tools: Optional[str] = None, confine: bool = False) -> Result:
        # confine=True runs an untrusted synchronous worker with a scrubbed environment [A2]. Only
        # provider credentials are restored. Filesystem isolation remains the backend or VM's job.
        try:
            cmd = self.build_cmd(prompt, model=model, perms=perms, effort=effort,
                                 system_context_path=system_context_path,
                                 resume_session_id=resume_session_id, schema_path=schema_path,
                                 allowed_tools=allowed_tools)
        except SchemaNotApplied as e:
            # Do not run unconstrained when the caller requested a schema [B1].
            return Result(ok=False, text="", exit_code=-1, schema_applied=False,
                          error=f"schema not applied: {e}")
        res = self.runner.exec(cmd, cwd=cwd, env=self.auth_env(), timeout=timeout, risk=risk,
                               worker=confine)  # auth is restored after the rest of the environment is scrubbed
        out = self.parse_output(res.stdout, res.exit_code)
        if schema_path is not None:
            out.schema_applied = True  # build_cmd succeeded with the requested schema
        if getattr(res, "timed_out", False):
            # A timeout has no terminal success state, even if partial output looks complete [G3].
            out = apply_timeout(out, timed_out=True, exit_code=res.exit_code)
        return out

    def spawn(self, prompt: str, *, model: str, cwd: str, log_path: str, perms: str = "default",
              effort: Optional[str] = "medium", system_context_path: Optional[str] = None,
              resume_session_id: Optional[str] = None, risk: RiskTier = "medium") -> Handle:
        cmd = self.build_cmd(prompt, model=model, perms=perms, effort=effort,
                             system_context_path=system_context_path,
                             resume_session_id=resume_session_id)
        return self.runner.spawn(cmd, cwd=cwd, log_path=log_path, env=self.auth_env(),  # [A3]
                                 risk=risk, worker=True)


__all__ = ["Capabilities", "HarnessAdapter", "SchemaNotApplied", "InvokeResult", "Result",
           "ENGINE_DIR", "VALID_PERMS"]
