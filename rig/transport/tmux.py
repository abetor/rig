"""Detached tmux transport for background agent processes."""
from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid
from typing import Callable, Optional

from .base import ExecResult, Handle, Runner, RiskTier
from .local import LocalRunner, _file_logger, build_env, guard


class _TmuxHandle(Handle):
    """Control a detached process running in a tmux session."""

    _POLL_INTERVAL = 0.05

    def __init__(self, runner: "TmuxRunner", session: str, log_path: str, status_path: str):
        self._runner = runner
        self._session = session
        self.log_path = log_path
        self._status_path = status_path
        self._rc: Optional[int] = None

    def _read_status(self) -> Optional[int]:
        try:
            with open(self._status_path) as fh:
                txt = fh.read().strip()
        except FileNotFoundError:
            return None
        if not txt:
            return None
        try:
            return int(txt)
        except ValueError:
            return None

    def poll(self) -> Optional[int]:
        if self._rc is not None:
            return self._rc
        rc = self._read_status()
        if rc is not None:
            self._rc = rc
            return rc
        if self._runner._has_session(self._session):
            return None
        rc = self._read_status()
        self._rc = rc if rc is not None else -9
        return self._rc

    def wait(self, timeout: Optional[float] = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self._session, timeout)
            time.sleep(self._POLL_INTERVAL)

    def kill(self) -> None:
        self._runner._kill_session(self._session)
        if self._rc is None:
            rc = self._read_status()
            self._rc = rc if rc is not None else -9


class TmuxRunner(Runner):
    def __init__(
        self,
        *,
        tmux_bin: str = "tmux",
        session_prefix: str = "rig",
        log: Optional[Callable[[dict], None]] = None,
        log_path: Optional[str] = None,
        allow_catastrophic: bool = False,
        allow_high_risk: bool = False,
    ):
        self._tmux = tmux_bin
        self._prefix = session_prefix
        self._log = log or (_file_logger(log_path) if log_path else (lambda ev: None))
        self._allow = allow_catastrophic
        self._allow_high = allow_high_risk
        self._local = LocalRunner(
            log=self._log, allow_catastrophic=allow_catastrophic, allow_high_risk=allow_high_risk
        )

    def _tmux_run(self, args: list[str], **kw) -> "subprocess.CompletedProcess":
        return subprocess.run([self._tmux, *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, **kw)

    def _has_session(self, session: str) -> bool:
        return self._tmux_run(["has-session", "-t", session]).returncode == 0

    def _kill_session(self, session: str) -> None:
        self._tmux_run(["kill-session", "-t", session])

    def spawn(self, cmd, *, cwd, log_path, env=None, risk="medium", worker=True, session=None) -> Handle:
        guard(cmd, risk, log=self._log, allow_catastrophic=self._allow, allow_high_risk=self._allow_high)
        full_env = build_env(env, worker=worker)  # Workers receive only the allowlisted environment [A2].
        log_abs = os.path.abspath(log_path)
        os.makedirs(os.path.dirname(log_abs), exist_ok=True)
        status_path = log_abs + ".status"
        status_tmp = log_abs + ".status.tmp"
        for p in (status_path, status_tmp):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

        sess = session or f"{self._prefix}-{uuid.uuid4().hex[:12]}"
        env_assign = " ".join(shlex.quote(f"{k}={v}") for k, v in full_env.items())
        inner = " ".join(shlex.quote(c) for c in cmd)
        qlog, qstatus, qtmp = shlex.quote(log_abs), shlex.quote(status_path), shlex.quote(status_tmp)
        wrapped = (
            f"env -i {env_assign} {inner} > {qlog} 2>&1; rc=$?; "
            f"printf %s \"$rc\" > {qtmp}; mv -f {qtmp} {qstatus}; exit $rc"
        )

        self._log({"ev": "spawn", "backend": "tmux", "session": sess, "cmd": cmd,
                   "cwd": cwd, "worker": worker, "log": log_abs})
        r = self._tmux_run(["new-session", "-d", "-s", sess, "sh", "-c", wrapped], cwd=cwd)
        if r.returncode != 0:
            self._log({"ev": "spawn_error", "backend": "tmux", "rc": r.returncode, "stderr": r.stderr})
            raise RuntimeError(f"tmux new-session failed (rc={r.returncode}): {r.stderr.strip()}")
        open(log_abs, "a").close()
        return _TmuxHandle(self, sess, log_abs, status_path)

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False) -> ExecResult:
        return self._local.exec(cmd, cwd=cwd, env=env, timeout=timeout, risk=risk, worker=worker)

    def read_file(self, path: str) -> str:
        return self._local.read_file(path)

    def write_file(self, path: str, content: str) -> None:
        self._local.write_file(path, content)

    def exists(self, path: str) -> bool:
        return self._local.exists(path)
